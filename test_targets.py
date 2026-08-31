"""Synthetic tests for text_detection.load_target_items() and span ranking. No game required.

WHY THIS EXISTS: assets/targets.txt is hand-edited, long, and its two halves (real targets at the
top, look-alikes at the bottom) are far enough apart that nobody sees them at once. That is
exactly where a name gets listed twice with conflicting flags - which happened, went unnoticed for
a version, and presented as "OCR cannot read this item" rather than as a config mistake. These
tests cover the parsing rules and the one ranking property the file's contents depend on.
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

import text_detection as td

failures = 0


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

print("\n3. An unrecognised colour warns and falls back, it does not crash")
items, output = load("Thing [nosuchcolour]\n")
check("warns", "unrecognized color" in output)
check("falls back to the default", items["THING"]["color"] == td.DEFAULT_BOX_COLOR)
check("the item is still usable", items["THING"]["ignore"] is False)

print("\n4. The shipped targets.txt has no duplicates and reports the small Rejuvenation Potion")
shipped = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "targets.txt")
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

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
