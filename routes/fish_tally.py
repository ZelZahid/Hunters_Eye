"""What was caught, what it is worth, and how fast it is coming in. No I/O except the two small
files it is told to read and write; no clock (times are passed in).

wow.py reads the chat box and hands each "You receive loot: [...]" line here; the GUI (wow_gui.py)
shows the tally. Same split as routes/fishing.py: the eyes are elsewhere, this is bookkeeping that
tests can drive with plain strings.

NO LIST OF FISH IS BUILT IN. Whatever the chat says was looted is counted under the name it says,
and a name never seen before is simply added (with "price not known yet"). The price table starts
from the tooltips Zel captured on 2026-11-25 (assets/wow/fish_prices.json) and grows from there:
new fish are written to the user's own file, where a price can be filled in (the GUI does it, or
edit the file). OCR can misread a letter, so a reading is first SNAPPED to the closest known name
("Raw Longiaw Mud Snapper" -> "Raw Longjaw Mud Snapper"); only a reading close to nothing known
becomes a new fish.

MONEY is kept in copper, as one integer (100 copper = 1 silver, 100 silver = 1 gold), and only
turned into "1g 2s 3c" for display. Prices are the game's own "Sell Price" line - what a vendor
pays - not an auction estimate.
"""
from __future__ import annotations

import csv
import difflib
import json
import os
import re
import tempfile
from collections import Counter, deque
from pathlib import Path

# ---- reading a loot line ----------------------------------------------------------------------

#"You receive loot: [Raw Longjaw Mud Snapper]" or "...[Raw Brilliant Smallfish]x2". OCR keeps the
#words reliably and the brackets usually (5 of 5 lines measured); whatever follows the closing
#bracket is junk from the box's edge more often than not ("] ia", "] 7"), except a stack count.
#The opening bracket is sometimes read as "(" ("You receive loot: (Gaffer Jack]", measured).
LOOT_LINE = re.compile(r"receive\s*loot\s*[:;.,]?\s*[\[({]?\s*(?P<name>[^\[\](){}]+?)\s*[\])}]"
                       r"(?:\s*[xX×]\s*(?P<count>\d{1,3}))?")
#Without the closing bracket the name's end cannot be told from the junk after it, so such a
#reading is only accepted when it snaps to a fish already known (see Catalog.resolve()).
LOOT_LINE_OPEN = re.compile(r"receive\s*loot\s*[:;.,]?\s*[\[({]?\s*(?P<name>.+?)\s*$")
SANE_NAME = re.compile(r"^(?=.*[A-Za-z]{3})[A-Za-z0-9 ',:.\-]{3,60}$")
SNAP_CUTOFF = 0.86     #"Raw Longiaw Mud Snapper" vs the real name: 0.96; two different real fish
                       #("Raw Longjaw Mud Snapper" / "Raw Slitherskin Mackerel"): 0.47
#...but "17 Pound Catfish" and "19 Pound Catfish" are different fish 0.94 apart, so a reading only
#snaps to a name with exactly the same numbers in it. A misread digit becomes a new name instead,
#which the GUI can merge - better than silently filing one fish under another's price.


def parse_loot_line(text):
    """(name as read, count, closed) for a loot line, or None if `text` is not one.

    closed: whether the closing bracket was read - an unclosed name may have junk at its end.
    """
    if not text:
        return None
    match = LOOT_LINE.search(text)
    if match:
        name = _tidy(match.group("name"))
        count = int(match.group("count")) if match.group("count") else 1
        return (name, max(1, count), True) if name else None
    match = LOOT_LINE_OPEN.search(text)
    if match:
        name = _tidy(match.group("name"))
        return (name, 1, False) if name else None
    return None


def _numbers(text):
    return re.findall(r"\d+", text)


def _tidy(name):
    name = re.sub(r"\s+", " ", name).strip(" .,:;'`\"|")
    return name


# ---- money -------------------------------------------------------------------------------------

MONEY_PART = re.compile(r"(\d+)\s*([gsc])", re.IGNORECASE)


def parse_money(value):
    """Copper from 1234, "1234", "12s 34c", "1g", "19 s" ... None for None/"" ; ValueError if bad."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a price: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"a price cannot be negative: {value!r}")
        return int(round(value))
    text = str(value).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = MONEY_PART.findall(text)
    if not parts or MONEY_PART.sub("", text).strip(" ,"):
        raise ValueError(f"not a price: {value!r} (write it like 19s, 2s 40c, 1g 5s or 40)")
    unit = {"g": 10000, "s": 100, "c": 1}
    return sum(int(number) * unit[letter.lower()] for number, letter in parts)


def split_money(copper):
    """(gold, silver, copper) for a whole number of copper."""
    copper = int(round(copper))
    sign = -1 if copper < 0 else 1
    gold, rest = divmod(abs(copper), 10000)
    silver, cop = divmod(rest, 100)
    return sign * gold, silver, cop


def format_money(copper, unknown="?"):
    """"1g 2s 3c", "45s 0c", "7c" - zero leading units left out. None -> `unknown`."""
    if copper is None:
        return unknown
    gold, silver, cop = split_money(copper)
    if gold:
        return f"{gold}g {silver}s {cop}c"
    if silver:
        return f"{silver}s {cop}c"
    return f"{cop}c"


# ---- the price list ----------------------------------------------------------------------------

class Catalog:
    """Every item known by name: its vendor price (copper, or None = not known yet) and the
    misreadings that mean it.

    Two files: `seed` (in the repo, read only - the prices from the tooltips) and `user` (on this
    PC, written - new fish, prices typed into the GUI, merges). The user file wins where both say
    something, so nothing typed is ever overwritten by an update to the repo's list.
    """

    def __init__(self, seed_path=None, user_path=None):
        self.seed_path = Path(seed_path) if seed_path else None
        self.user_path = Path(user_path) if user_path else None
        self.prices = {}      #name -> copper or None
        self.aliases = {}     #lower-case misreading -> name
        self.user = {}        #what the user file holds, as written back
        self.problems = []    #things wrong in the files, in words (shown, never fatal)
        self.reload()

    def reload(self):
        self.prices.clear()
        self.aliases.clear()
        self.problems.clear()
        seed = self._read(self.seed_path)
        self.user = self._read(self.user_path)
        for source in (seed, self.user):
            for name, entry in source.items():
                self._take(name, entry)

    def _read(self, path):
        if path is None or not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                items = json.load(handle).get("items", {})
            if not isinstance(items, dict):
                raise ValueError("'items' is not a {name: {...}} table")
            return {str(k): (v if isinstance(v, dict) else {"vendor": v}) for k, v in items.items()}
        except (OSError, ValueError, AttributeError) as exc:
            self.problems.append(f"{path.name}: could not read it ({exc}) - its prices are not used")
            return {}

    def _take(self, name, entry):
        name = _tidy(name)
        if not name:
            return
        if "vendor" in entry or name not in self.prices:
            try:
                self.prices[name] = parse_money(entry.get("vendor"))
            except ValueError as exc:
                self.problems.append(f"{name}: {exc}")
                self.prices.setdefault(name, None)
        for alias in entry.get("aliases", []) or []:
            self.aliases[_tidy(str(alias)).lower()] = name

    # -- lookups --

    def names(self):
        return sorted(self.prices)

    def price(self, name):
        return self.prices.get(name)

    def resolve(self, raw, closed=True):
        """The known name a reading means, or the reading itself as a NEW name, or None.

        None when the reading is not usable: unclosed and close to nothing known, or not a
        plausible name at all (stray symbols) - a missed fish is better than a junk entry.
        """
        raw = _tidy(raw or "")
        if not raw:
            return None
        lower = raw.lower()
        for name in self.prices:
            if name.lower() == lower:
                return name
        if lower in self.aliases:
            return self.aliases[lower]
        if closed:
            best = self._closest(lower)
        else:
            #The end of an unclosed reading may be junk: compare only as many letters as each name has.
            best = None
            best_ratio = SNAP_CUTOFF
            for name in self.prices:
                if _numbers(name) != _numbers(lower[:len(name)]):
                    continue
                ratio = difflib.SequenceMatcher(None, lower[:len(name)], name.lower()).ratio()
                if ratio >= best_ratio:
                    best, best_ratio = name, ratio
            return best
        if best is not None:
            return best
        return raw if SANE_NAME.match(raw) else None

    def _closest(self, lower):
        best, best_ratio = None, SNAP_CUTOFF
        candidates = list(self.prices) + list(self.aliases)
        for candidate in candidates:
            if _numbers(candidate) != _numbers(lower):
                continue
            ratio = difflib.SequenceMatcher(None, lower, candidate.lower()).ratio()
            if ratio >= best_ratio:
                best, best_ratio = candidate, ratio
        if best is None:
            return None
        return self.aliases.get(best.lower(), best) if best not in self.prices else best

    # -- changes (all written to the user file at once) --
    #
    #Two programs write the user file - wow.py adds new fish as they are caught, wow_gui.py saves
    #the prices typed into it - so every change re-reads the file first and changes only its own
    #entry. Writing from memory instead would quietly undo whatever the other one saved meanwhile.

    def add(self, name):
        """A fish seen for the first time: listed, price unknown. True if it was new."""
        if name in self.prices:
            return False
        self.reload()
        if name in self.prices:
            return False
        self.prices[name] = None
        self.user.setdefault(name, {})
        self.save()
        return True

    def set_price(self, name, copper):
        copper = parse_money(copper)
        self.reload()
        self.prices[name] = copper
        self.user.setdefault(name, {})["vendor"] = copper
        self.save()

    def merge(self, wrong, right):
        """`wrong` was a misreading of `right`: from now on it counts as `right`."""
        self.reload()
        if wrong == right or right not in self.prices:
            raise ValueError(f"cannot merge {wrong!r} into {right!r}")
        self.prices.pop(wrong, None)
        self.user.pop(wrong, None)
        entry = self.user.setdefault(right, {})
        aliases = entry.setdefault("aliases", [])
        if wrong not in aliases:
            aliases.append(wrong)
        self.aliases[wrong.lower()] = right
        self.save()

    def save(self):
        if self.user_path is None:
            return
        data = {"about": "Your own additions to wow.py's fish prices (assets/wow/fish_prices.json). "
                         "vendor = what a vendor pays for one, in copper (or written like \"2s 40c\"); "
                         "null = not known yet. aliases = misreadings that mean this fish.",
                "items": dict(sorted(self.user.items()))}
        write_json(self.user_path, data)


def write_json(path, data):
    """Written to a temporary file and renamed into place, so a crash mid-write never leaves half a
    file (and with it, every price typed so far) behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# ---- the tally ---------------------------------------------------------------------------------

class Tally:
    """Catches counted by name, with the times, for one session. Times are seconds of any clock."""

    def __init__(self, recent=200):
        self.counts = Counter()
        self.inferred = 0                   #catches counted from the reel-in, not a new chat line
        self.recent = deque(maxlen=recent)  #(when, name, count, how), newest last

    def add(self, name, count=1, when=None, how="read"):
        self.counts[name] += count
        if how == "inferred":
            self.inferred += count
        self.recent.append((when, name, count, how))

    def merge(self, wrong, right):
        if wrong in self.counts:
            self.counts[right] += self.counts.pop(wrong)
        self.recent = deque(((w, right if n == wrong else n, c, h) for w, n, c, h in self.recent),
                            maxlen=self.recent.maxlen)

    @property
    def total(self):
        return sum(self.counts.values())

    def value(self, catalog):
        """(copper from the fish with a known price, how many caught have no price yet)."""
        copper, unpriced = 0, 0
        for name, count in self.counts.items():
            price = catalog.price(name)
            if price is None:
                unpriced += count
            else:
                copper += price * count
        return copper, unpriced


def per_hour(amount, seconds):
    """`amount` scaled to one hour of `seconds`, or None when too little time has passed to say
    (under a minute, a single catch would read as hundreds an hour)."""
    if seconds is None or seconds < 60:
        return None
    return amount * 3600.0 / seconds


def format_duration(seconds):
    """"1h 02m", "4m 09s", "12s"."""
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ---- the history file --------------------------------------------------------------------------

HISTORY_FIELDS = ("time", "session", "fish", "count", "how")


def append_history(path, when_text, session, name, count, how):
    """One row per catch in a CSV that only grows - every catch ever counted, kept on this PC."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(HISTORY_FIELDS)
        writer.writerow((when_text, session, name, count, how))


def read_history(path):
    """(Counter of every catch by name, number of sessions) from the history file; empty if none.
    A damaged row is skipped, never fatal."""
    counts, sessions = Counter(), set()
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    counts[row["fish"]] += int(row["count"])
                    sessions.add(row["session"])
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return counts, len(sessions)
