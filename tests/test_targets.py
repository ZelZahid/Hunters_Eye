"""Synthetic tests for text_detection.load_target_items() and span ranking. No game required.

WHY THIS EXISTS: assets/targets.txt is hand-edited, long, and its two halves (real targets at the
top, look-alikes at the bottom) are far enough apart that nobody sees them at once. That is
exactly where a name gets listed twice with conflicting flags - which happened, went unnoticed for
a version, and presented as "OCR cannot read this item" rather than as a config mistake. These
tests cover the parsing rules and the one ranking property the file's contents depend on.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

from core import text_detection as td

failures = 0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def load(text):
    """Loads a targets file, returning (items, captured stdout) so warnings can be asserted on."""
    path = os.path.join(tempfile.mkdtemp(), "targets.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        items = td.load_target_items(path)
    return items, buffer.getvalue()


print("1. The line syntax parses as documented")
items, _ = load(
    "# a comment\n"
    "\n"
    "Plain Item\n"
    "Collected Item*\n"
    "Coloured Item [purple]\n"
    "Both* [darkorange]\n"
    "-Look Alike\n"
)
check("comments and blank lines skipped", len(items) == 5)
check("names are upper-cased", "PLAIN ITEM" in items)
check("'*' sets to_collect", items["COLLECTED ITEM"]["to_collect"] is True)
check("no '*' means no collect", items["PLAIN ITEM"]["to_collect"] is False)
check("'[purple]' sets the colour", items["COLOURED ITEM"]["color"] == td.NAMED_COLORS["purple"])
check("no tag falls back to the default", items["PLAIN ITEM"]["color"] == td.DEFAULT_BOX_COLOR)
check("'*' and '[colour]' together", items["BOTH"]["to_collect"] is True
      and items["BOTH"]["color"] == td.NAMED_COLORS["darkorange"])
check("leading '-' sets ignore", items["LOOK ALIKE"]["ignore"] is True)
check("no '-' means not ignored", items["PLAIN ITEM"]["ignore"] is False)

print("\n2. A duplicate name WARNS instead of silently overwriting")
#The actual bug: "Rejuvenation Potion" was listed once as a real target near the top and again as
#a "-" look-alike far below. A plain dict assignment let the look-alike win, so the item was
#matched and thrown away on every frame - which from outside looks exactly like "OCR cannot read
#it", a completely different problem to go hunting for. It silently dropped the colour too.
items, output = load("Thing [purple]\nOther Thing\n-Thing\n")
check("a warning is printed", "listed twice" in output)
check("the warning names both line numbers", "line 1" in output and "line 3" in output)
check("the warning names the item", "Thing" in output)
check("last-one-wins is still what happens (predictable)", items["THING"]["ignore"] is True)
check("an unrelated entry is untouched", items["OTHER THING"]["ignore"] is False)

items, output = load("Alpha\nBravo\nCharlie\n")
check("no warning when there are no duplicates", "listed twice" not in output)

print("\n3. An unrecognised tag warns and falls back, it does not crash")
#Since 2026-09-12 a "[...]" tag can be a colour OR how to collect the item, so an unknown
#word in one is no longer specifically a bad colour - the warning names the tag and lists
#what is accepted, and the line still loads.
items, output = load("Thing [nosuchcolour]\n")
check("warns, naming the tag", "unrecognized tag" in output and "nosuchcolour" in output)
check("falls back to the default", items["THING"]["color"] == td.DEFAULT_BOX_COLOR)
check("the item is still usable", items["THING"]["ignore"] is False)

print("\n4. The shipped targets.txt has no duplicates and reports the small Rejuvenation Potion")
shipped = os.path.join(REPO_ROOT, "assets", "targets.txt")
buffer = io.StringIO()
with redirect_stdout(buffer):
    real = td.load_target_items(shipped)
check("no duplicate warnings from the shipped file", "listed twice" not in buffer.getvalue())
check("'Rejuvenation Potion' is present", "REJUVENATION POTION" in real)
#The regression itself: it must be a REAL target, not a look-alike that gets discarded.
check("...and is NOT ignored", real["REJUVENATION POTION"]["ignore"] is False)
check("...and kept its [purple] colour", real["REJUVENATION POTION"]["color"] == td.NAMED_COLORS["purple"])
check("'Full Rejuvenation Potion' is still a separate, collected target",
      real["FULL REJUVENATION POTION"]["ignore"] is False
      and real["FULL REJUVENATION POTION"]["to_collect"] is True)

print("\n5. Both potions still resolve correctly now that neither is a look-alike")
#This is what made the look-alike line unnecessary, and it is the property that would break if
#span ranking were ever changed back to ratio-first: the key is (words matched, ratio), so a
#three-word line outranks its own two-word tail instead of tying with it.
FULL, SMALL = "FULL REJUVENATION POTION", "REJUVENATION POTION"


def winner(line, names):
    words = line.split()
    best_key, best_names = (0, 0.0), set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            text = " ".join(words[start:end])
            for name in names:
                ratio = td._match_ratio(text, name, 0.75)
                if ratio is None:
                    continue
                key = (end - start, ratio)
                if key > best_key:
                    best_key, best_names = key, {name}
                elif key == best_key:
                    best_names.add(name)
    return None if len(best_names) != 1 else next(iter(best_names))


check("a 'Full Rejuvenation Potion' line reports FULL", winner(FULL, (FULL, SMALL)) == FULL)
check("a 'Rejuvenation Potion' line reports SMALL", winner(SMALL, (FULL, SMALL)) == SMALL)
check("neither is ambiguous", winner(FULL, (FULL, SMALL)) is not None
      and winner(SMALL, (FULL, SMALL)) is not None)
#And a misread full label must still not collapse onto the short name - the failure mode where
#better OCR made detection worse.
check("a misread 'FVLL Rejuvenation Potion' still reports FULL",
      winner("FVLL REJUVENATION POTION", (FULL, SMALL)) == FULL)

print("\n6. Look-alikes still do their job for the runes")
#The mechanism is still load-bearing everywhere else: without these entries a "Ral Rune" gets
#attributed to whichever listed rune it resembles most, and then clicked on.
check("'Ral Rune' is listed and ignored", real.get("RAL RUNE", {}).get("ignore") is True)
check("'Jah Rune' is a real, collected target",
      real["JAH RUNE"]["ignore"] is False and real["JAH RUNE"]["to_collect"] is True)
check("a 'Ral Rune' line wins its own match rather than becoming a Jah Rune",
      winner("RAL RUNE", ("RAL RUNE", "JAH RUNE", "MAL RUNE")) == "RAL RUNE")

print("\n7. How to collect an item is a tag, like its colour")
#Added 2026-09-12. A sorceress with Telekinesis bound to a key takes an item from across the
#room, so for her a potion is collected by pointing at it and pressing that key, not by clicking
#it and walking over. That is per-item, and it is data - the parser only carries the string.
tagged, out = load(
    "Default Item*\n"
    "Keyed Item* [key:e]\n"
    "Explicit Click* [click]\n"
    "Both Tags* [purple] [key:f1]\n"
    "Reversed* [key:f1] [purple]\n"
    "Not A Tag* [banana]\n"
    "Empty Key* [key:]\n"
)
check("no tag means click", tagged["DEFAULT ITEM"]["collect_with"] == td.COLLECT_BY_CLICK)
check("'[key:e]' is carried through", tagged["KEYED ITEM"]["collect_with"] == "key:e")
check("'[click]' says the default out loud", tagged["EXPLICIT CLICK"]["collect_with"] == "click")
check("a colour and a key tag coexist",
      tagged["BOTH TAGS"]["collect_with"] == "key:f1"
      and tagged["BOTH TAGS"]["color"] == td.NAMED_COLORS["purple"])
check("and the order of the two does not matter",
      tagged["REVERSED"]["collect_with"] == tagged["BOTH TAGS"]["collect_with"]
      and tagged["REVERSED"]["color"] == tagged["BOTH TAGS"]["color"])
#A tag is stripped whether it was understood or not: OCR output never contains '[' or ']', so a
#leftover tag would make that item impossible to match rather than merely oddly coloured.
check("an unrecognized tag is stripped from the name, not left in it", "NOT A TAG" in tagged)
check("...and warns rather than failing, naming what is accepted",
      "banana" in out and "key:" in out)
check("'[key:]' names no key, so it falls back to clicking",
      tagged["EMPTY KEY"]["collect_with"] == td.COLLECT_BY_CLICK and "no key" in out)
#The real file is what actually runs, so assert on it too - but on the SHAPE of what is in it,
#never on which items happen to carry which tag. targets.txt is the file the user is expected to
#edit, so a test that pins its exact contents fails on an ordinary edit and teaches everyone to
#ignore it. (That happened the day this was written: marking a second potion "[key:e]" broke a
#check that had hard-coded "only one item uses a key".)
collected = {name: spec["collect_with"] for name, spec in real.items() if spec["to_collect"]}
check(f"every collected item resolves to click or a key ({len(collected)} items)",
      all(how == td.COLLECT_BY_CLICK
          or (how.startswith(td.COLLECT_BY_KEY_PREFIX) and how[len(td.COLLECT_BY_KEY_PREFIX):])
          for how in collected.values()))
#The one worth catching, and the reason to look at the shipped file at all: a typo'd key name is
#invisible in the file and only surfaces as "auto-collect quietly does nothing for that item".
_keyboard = None
try:
    import keyboard as _keyboard
except Exception:  # noqa: BLE001 - no keyboard package here is not a test failure
    print("  ..   (keyboard package unavailable - skipping the key-name check)")
if _keyboard is not None:
    bad = []
    for name, how in collected.items():
        if not how.startswith(td.COLLECT_BY_KEY_PREFIX):
            continue
        key = how[len(td.COLLECT_BY_KEY_PREFIX):]
        try:
            _keyboard.key_to_scan_codes(key)
        except Exception:  # noqa: BLE001
            bad.append((name, key))
    check(f"every key named in targets.txt is one this system recognises ({bad or 'all valid'})",
          not bad)

print("\n8. '[exact]' refuses to match a name inside a longer label")
#Added 2026-09-13. A plain "Grand Charm" is worth taking; "Serpent's Grand Charm of Vita" is a
#different item and the owner does not want it. Normally the opposite is wanted - every
#contiguous run of words is tried, which is how "GUL" is found inside "GUL RUNE" - so this is a
#per-item tag, not a change of default.
tagged, out = load("Grand Charm* [exact]\nGul Rune*\n")
check("the tag parses", tagged["GRAND CHARM"]["exact"] is True)
check("an untagged item is unaffected", tagged["GUL RUNE"]["exact"] is False)
check("it does not disturb the other tags",
      tagged["GRAND CHARM"]["to_collect"] is True
      and tagged["GRAND CHARM"]["collect_with"] == td.DEFAULT_COLLECT_WITH)
combined, _ = load("Grand Charm* [exact] [purple] [key:e]\n")
check("it combines with colour and collect-with, in any order",
      combined["GRAND CHARM"]["exact"] is True
      and combined["GRAND CHARM"]["color"] == td.NAMED_COLORS["purple"]
      and combined["GRAND CHARM"]["collect_with"] == "key:e")


def winner_exact(line, specs):
    """winner(), but honouring "exact" the way find_text_matches does."""
    words = line.split()
    best_key, best_names = (0, 0.0), set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            text = " ".join(words[start:end])
            whole_line = (start == 0 and end == len(words))
            for name, spec in specs.items():
                if spec.get("exact") and not (whole_line and len(words) <= len(name.split())):
                    continue
                ratio = td._match_ratio(text, name, 0.75)
                if ratio is None:
                    continue
                key = (end - start, ratio)
                if key > best_key:
                    best_key, best_names = key, {name}
                elif key == best_key:
                    best_names.add(name)
    return None if len(best_names) != 1 else next(iter(best_names))


SPECS = {"GRAND CHARM": {"exact": True}, "SMALL CHARM": {"exact": True},
         "GUL RUNE": {"exact": False}}
check("a plain 'GRAND CHARM' label matches", winner_exact("GRAND CHARM", SPECS) == "GRAND CHARM")
check("a prefixed one does not", winner_exact("SERPENTS GRAND CHARM", SPECS) is None)
check("a suffixed one does not", winner_exact("GRAND CHARM OF VITA", SPECS) is None)
check("both at once does not", winner_exact("FUNGAL SMALL CHARM OF BALANCE", SPECS) is None)
#The tag must not cost the ordinary tolerance for a stylised font - a misread of the PLAIN label
#is still the plain label, and that is the whole reason the cutoffs are loose in the first place.
check("a one-character misread of the plain label still matches",
      winner_exact("SMAL1 CHARM", SPECS) == "SMALL CHARM")
check("an untagged name is still found inside a longer line",
      winner_exact("GUL RUNE DROPPED", SPECS) == "GUL RUNE")

print("\n9. Blue item labels survive preprocessing (the channel, not the threshold)")
#Diablo II draws magic items in BLUE, and cv.COLOR_BGR2GRAY weights blue at 0.114 - so the
#charm labels the owner reported as undetectable measured luminance ~119 with a maximum of 127,
#against a threshold of 150. No threshold could fix that; the information was destroyed by the
#conversion. _preprocess thresholds the strongest CHANNEL instead. These frames are the guard:
#swap it back to luminance and both of these go silent with nothing in any log to say why.
import cv2 as cv
import numpy as np

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
for fixture, want in (("charm_grand.png", "GRAND CHARM"), ("charm_small.png", "SMALL CHARM")):
    path = os.path.join(FIXTURES, fixture)
    frame = cv.imread(path)
    if frame is None:
        check(f"{fixture} is present", False)
        continue
    if not td.ocr_available():
        check(f"{fixture}: OCR unavailable, nothing checked", False)
        continue
    found = [m for m in td.find_text_matches(frame, real) if m[4] == want]
    check(f"{fixture}: {want} is detected end to end", bool(found))
    if not found:
        continue
    #Measure the label's OWN pixels, inside the box that was just matched - a whole-frame colour
    #mask also catches spell glow and torches, which is not what this is about.
    x, y, w, h = found[0][:4]
    box = frame[max(0, y):y + h, max(0, x):x + w]
    b, g, r = (box[:, :, i].astype(int) for i in range(3))
    glyph = (b > 180) & (b - r > 40) & (b - g > 40)
    check(f"{fixture}: the label really is blue text ({glyph.sum()} px)", glyph.sum() > 50)
    lum = cv.cvtColor(box, cv.COLOR_BGR2GRAY)[glyph]
    val = box.max(axis=2)[glyph]
    check(f"{fixture}: dark in luminance - max {lum.max()} <= {td.BRIGHT_TEXT_THRESHOLD}",
          lum.max() <= td.BRIGHT_TEXT_THRESHOLD)
    check(f"{fixture}: bright in the strongest channel - max {val.max()} > {td.BRIGHT_TEXT_THRESHOLD}",
          val.max() > td.BRIGHT_TEXT_THRESHOLD)

print("\n10. A word that identifies nothing gets one more misread character")
#Added 2026-09-14. Seen live: a Super Mana Potion label read "SUPER MANA PeTIeN". SUPER and MANA -
#the two words that say WHICH potion it is - both scored 1.000; POTION scored 0.667 against a
#cutoff of 0.75 and the whole line was thrown away. POTION appears in three target names, so it
#could not have told any of them apart even read perfectly. The per-word rule exists to stop a
#shared word dragging a WRONG name over the line, which is a statement about the distinguishing
#word only.
REAL_SHARED = td.shared_words(real)
check("the shared words come out of the vocabulary, not a list",
      {"POTION", "RUNE", "CHARM"} <= REAL_SHARED)
check("a word unique to one name is not shared", "SUPER" not in REAL_SHARED and "JAH" not in REAL_SHARED)

check("the label that was being thrown away now matches",
      td._match_ratio("SUPER MANA PETIEN", "SUPER MANA POTION", 0.75, REAL_SHARED) is not None)
check("...and still does when it reads perfectly",
      td._match_ratio("SUPER MANA POTION", "SUPER MANA POTION", 0.75, REAL_SHARED) == 1.0)
#The slack is ONE character, not a free pass: a shared word read into nonsense is still a miss.
check("a shared word read into nonsense is still rejected",
      td._match_ratio("SUPER MANA PXXXXX", "SUPER MANA POTION", 0.75, REAL_SHARED) is None)
#And the distinguishing words keep the strict cutoff, which is the whole point.
check("a misread DISTINGUISHING word is still rejected",
      td._match_ratio("SUPER MENE POTION", "SUPER MANA POTION", 0.75, REAL_SHARED) is None)

print("\n11. ...and the look-alike protection still holds with that slack active")
#The existing checks above call _match_ratio with no vocabulary, so they measure the STRICT path.
#Production passes the shared set, so the cases the per-word rule was written for have to be
#re-run with it - otherwise the guard is only tested in a configuration that never runs.
def winner_shared(line, names):
    words = line.split()
    best_key, best_names = (0, 0.0), set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            text = " ".join(words[start:end])
            for name in names:
                ratio = td._match_ratio(text, name, 0.75, REAL_SHARED)
                if ratio is None:
                    continue
                key = (end - start, ratio)
                if key > best_key:
                    best_key, best_names = key, {name}
                elif key == best_key:
                    best_names.add(name)
    return None if len(best_names) != 1 else next(iter(best_names))


check("'RAL RUNE' still wins its own match rather than becoming a Jah Rune",
      winner_shared("RAL RUNE", ("RAL RUNE", "JAH RUNE", "MAL RUNE")) == "RAL RUNE")
check("'RAL RUNE' is not reported as a listed rune when its own entry is gone",
      winner_shared("RAL RUNE", ("JAH RUNE", "LEM RUNE", "GUL RUNE")) is None)
check("'KO RUNE' read as 'KE RUNE' still resolves to Ko, not El",
      winner_shared("KE RUNE", ("KO RUNE", "EL RUNE", "LO RUNE")) == "KO RUNE")
check("a bare 'RUNE' matches nothing, relaxed or not",
      winner_shared("RUNE", ("KO RUNE", "EL RUNE", "LO RUNE")) is None)
check("'FLAWLESS' alone is still not a Flawless Amethyst",
      winner_shared("FLAWLESS", ("FLAWLESS AMETHYST",)) is None)
#A name whose words are ALL shared gets no slack at all, or nothing about it is checked strictly.
#"Rejuvenation Potion" is exactly that - both words also appear in "Full Rejuvenation Potion".
check("a name with no unique word keeps every cutoff strict",
      td._relaxable(["REJUVENATION", "POTION"], REAL_SHARED) == frozenset())
check("...while a name with one unique word relaxes the rest",
      td._relaxable(["FULL", "REJUVENATION", "POTION"], REAL_SHARED)
      == frozenset({"REJUVENATION", "POTION"}))
#Short words never take the slack: a second misread in a 3-letter word is indistinguishable from
#a different item, which is the RAL/MAL case the look-alike entries exist for.
check("a 3-letter shared word would still be held to 0.65",
      td._required_cutoff("GUL", 0.75, relaxed=True) == 0.65)
check("a 2-letter shared word would still be held to 0.5",
      td._required_cutoff("KO", 0.75, relaxed=True) == 0.5)
#The merged-word path keeps the strict cutoffs - it has no word alignment left to verify.
check("a merge/split is not forgiven more than before",
      td._match_ratio("SUPERMANAPETIEN", "SUPER MANA POTION", 0.75, REAL_SHARED) is None)

print("\n12. This program's OWN overlay text must never match a target")
#THIS IS A REGRESSION TEST FOR A BUG THAT SHIPPED. Reported as "the program detected a LO RUNE on
#the text where it says the map name and difficulty", and the text turned out to be the F5 debug
#panel's own "no read". The overlay is composited into the screen we capture, so everything it
#draws goes through OCR exactly like an item label - CLAUDE.md has always said so and recorded
#that none of the panel's words matched a target. Section 10's shared-word slack invalidated that
#recorded check without anyone re-running it, because it took "READ" vs "RUNE" from needing 0.75
#down to needing 0.50, and 2 of 4 characters wrong is a different word, not a misread.
#
#So the check is a test now instead of a sentence in a document. Every string the panel can draw
#is listed here; if the wording changes, add it. If matching is ever loosened again, this fails
#before it reaches a live game.
PANEL_TEXT = [
    "GAME STATE", "health", "mana",
    "no read",                                   #<- the one that actually matched a rune
    "GAME STATE  NOT FOCUSED", "readings", "suspended", "actions", "paused",
    "GAME STATE  window not found", "not on screen", "not on screen (0.30)",
    "GAME STATE  NOT CALIBRATED", "GAME STATE  STALE 4s",
    "RECALIBRATE", "was 1280x800", "now 1920x1080", "readings not trustworthy",
    "run calibrate_meters.py", "0%", "58%", "100%",
]


def best_match_for(line):
    """What find_text_matches would report for this line, ignoring ranking subtleties: any target
    that matches at all is a false positive here, because none of this is an item."""
    words = td._clean_text(line).split()
    hits = set()
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            text = " ".join(words[start:end])
            if not text:
                continue
            for name, spec in real.items():
                if spec.get("exact") and not (start == 0 and end == len(words)):
                    continue
                if td._match_ratio(text, name, 0.75, REAL_SHARED) is not None:
                    hits.add((text, name))
    return hits


panel_hits = {}
for line in PANEL_TEXT:
    hits = best_match_for(line)
    if hits:
        panel_hits[line] = hits
check(f"none of the {len(PANEL_TEXT)} panel strings matches any target "
      f"({panel_hits if panel_hits else 'clean'})", not panel_hits)

#And the specific pair, spelled out, so a failure says what broke rather than just "something".
check("'NO READ' is not a LO RUNE",
      td._match_ratio("NO READ", "LO RUNE", 0.75, REAL_SHARED) is None)
check("...nor a KO RUNE, nor an IO RUNE",
      td._match_ratio("NO READ", "KO RUNE", 0.75, REAL_SHARED) is None
      and td._match_ratio("NO READ", "IO RUNE", 0.75, REAL_SHARED) is None)
#The floor is what stops it, so pin the two measured anchors it sits between: a 4-letter shared
#word must not be allowed two wrong characters, and the real POTION misread must still pass.
check(f"a 4-letter shared word keeps a real cutoff ({td._required_cutoff('RUNE', 0.75, True):.2f})",
      td._required_cutoff("RUNE", 0.75, relaxed=True) >= td.SHARED_WORD_FLOOR)
check("'READ' vs 'RUNE' (0.50) is below that floor",
      td._word_ratio("READ", "RUNE") < td._required_cutoff("RUNE", 0.75, relaxed=True))
check("'PETIEN' vs 'POTION' (0.67) is still above it",
      td._word_ratio("PETIEN", "POTION") >= td._required_cutoff("POTION", 0.75, relaxed=True))
#Slack may only ever loosen. A caller passing a base cutoff below the floor must not find the
#"relaxed" cutoff has become STRICTER than the plain one.
check("slack never comes out stricter than no slack",
      all(td._required_cutoff(w, base, relaxed=True) <= td._required_cutoff(w, base)
          for w in ("RUNE", "POTION", "REJUVENATION") for base in (0.4, 0.55, 0.75, 0.9)))

print("\n13. '[exact]' is not fooled when Tesseract cuts a label into two lines")
#Added 2026-09-14 (Error_history.txt #51). "Grand Charm of Inertia" was read as two lines,
#"GRAND CHARM 6F" and "INERTIA", and collected as a plain Grand Charm. Two holes, one check each.
#
#Hole 1: the three-word line matched the two-word name through the merge/split path.
check("_match_ratio ALONE accepts 'GRAND CHARM 6F' (0.909 despaced) - which is why [exact] needs more",
      td._match_ratio("GRAND CHARM 6F", "GRAND CHARM", 0.75) is not None)
check("an [exact] line with an extra word is refused", winner_exact("GRAND CHARM 6F", SPECS) is None)
check("...but a MERGE of the plain label still matches", winner_exact("GRANDCHARM", SPECS) == "GRAND CHARM")
#Hole 2: the rest of the label was on another Tesseract line. These are the measured boxes.
GRAND_CHARM_OF = [("GRAND", 768, 533, 74, 15), ("CHARM", 858, 533, 76, 15), ("6F", 951, 537, 23, 12)]
INERTIA = [("INERTIA", 991, 533, 88, 15)]
PLAIN = [("GRAND", 768, 533, 74, 15), ("CHARM", 858, 533, 76, 15)]
check("the word on the next Tesseract line counts as a row neighbour",
      td._has_row_neighbour(GRAND_CHARM_OF, [GRAND_CHARM_OF, INERTIA]))
OF_INERTIA = [("OF", 951, 537, 23, 12), ("INERTIA", 991, 533, 88, 15)]
check("...and so does the split falling the other way ('GRAND CHARM' / 'OF INERTIA')",
      td._has_row_neighbour(PLAIN, [PLAIN, OF_INERTIA]))
check("a plain label on its own has none", not td._has_row_neighbour(PLAIN, [PLAIN]))
check("a label stacked directly below is not on the same row",
      not td._has_row_neighbour(PLAIN, [PLAIN, [("GUL", 768, 555, 40, 15)]]))
check("a speck that cleans to nothing is not a word",
      not td._has_row_neighbour(PLAIN, [PLAIN, [("|", 940, 530, 5, 20)]]))
check("a label a normal gap away is a separate label",
      not td._has_row_neighbour(PLAIN, [PLAIN, [("GUL", 934 + 40, 533, 40, 15)]]))

#End to end, on the real frame. It only measures the split if Tesseract still splits it, so say so.
path = os.path.join(FIXTURES, "charm_grand_of_inertia.png")
frame = cv.imread(path)
if frame is None:
    check("charm_grand_of_inertia.png is present", False)
elif not td.ocr_available():
    check("charm_grand_of_inertia.png: OCR unavailable, nothing checked", False)
else:
    found = [m for m in td.find_text_matches(frame, real) if m[4] == "GRAND CHARM"]
    check(f"'Grand Charm of Inertia' is not a Grand Charm ({found or 'nothing matched'})", not found)
    if td._tesserocr_api is not None:
        grouped = [" ".join(w[0] for w in line) for line in
                   td._group_words_by_line(td._get_words_tesserocr(td._preprocess(frame, td.PREPROCESS_AUTO)))]
        print(f"  (note) Tesseract's lines on this frame: {grouped} - "
              f"{'still split, so this measures the bug' if 'INERTIA' in grouped else 'NOT split any more'}")

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
