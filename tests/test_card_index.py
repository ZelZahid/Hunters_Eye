"""Checks that docs/test_cards.md lists exactly the test cards the tests actually run.

WHY THIS EXISTS: the card index is only worth having if it is true. A list that is updated by hand
drifts the first time someone adds a card in a hurry, and a stale list is worse than none - it
says something is tested that is not, or hides a card nobody remembers. So the index is checked
like any other test, and CI fails until the doc and the tests agree.

A card is a section header printed by a test file, in exactly this shape:

    print("\\nV-123  What it checks")

  - the ID is a category letter, a dash and three digits (see docs/test_cards.md for the letters
    and the number blocks each area owns);
  - two spaces, then the title - the same title the index lists.

IDs are never reused or renumbered. When a card is deleted, its ID is retired.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
INDEX = ROOT / "docs" / "test_cards.md"

CARD_IN_TEST = re.compile(r'print\(f?"\\n([A-Z]-\d{3})  (.+?)"\)')
CARD_IN_INDEX = re.compile(r"^\|\s*([A-Z]-\d{3})\s*\|\s*(.+?)\s*\|\s*`?(test_\w+\.py)`?\s*\|")

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


print("\nT-301  The test card index lists exactly the cards the tests run")
in_tests = {}      # id -> (title, file)
duplicates = []
for path in sorted(TESTS.glob("test_*.py")):
    for card, title in CARD_IN_TEST.findall(path.read_text(encoding="utf-8")):
        if card in in_tests:
            duplicates.append(f"{card} in {in_tests[card][1]} and {path.name}")
        in_tests[card] = (title, path.name)
check(f"no card ID is used twice ({duplicates or 'none'})", not duplicates)

in_index = {}
if not INDEX.exists():
    check("docs/test_cards.md exists", False)
else:
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        match = CARD_IN_INDEX.match(line)
        if match:
            card, title, filename = match.groups()
            in_index[card] = (title.replace("\\|", "|"), filename)

check(f"found the cards in tests/ ({len(in_tests)})", len(in_tests) > 0)
missing = sorted(set(in_tests) - set(in_index))
retired = sorted(set(in_index) - set(in_tests))
check(f"every card in tests/ is in the index (missing: {missing or 'none'})", not missing)
check(f"every card in the index is in tests/ (not found: {retired or 'none'})", not retired)
wrong = [card for card in sorted(set(in_tests) & set(in_index)) if in_tests[card] != in_index[card]]
check(f"every listed title and file matches the test (mismatched: {wrong or 'none'})", not wrong)

#Print the exact rows to paste, so fixing the index is a copy, not a hunt.
for card in missing + wrong:
    title, filename = in_tests[card]
    print(f"    | {card} | {title.replace('|', chr(92) + '|')} | {filename} |")


print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
