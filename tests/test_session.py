"""Tests for how a bot session reports itself: what main.py prints when it stops. No game required.

WHY THIS EXISTS: the end-of-session line is what the owner reads to see how long a run lasted, in
the console and in the control panel's log. "Total Runtime: 393.40" makes a person do arithmetic;
"6m 33s" does not (owner's request, 2026-10-15).
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


print("\nL-501  Total Runtime reads as minutes and seconds")
cases = {
    393.40: "6m 33s",    #the owner's own example from a real session
    390.0: "6m 30s",
    42.4: "42s",
    0.0: "0s",
    59.6: "1m 0s",       #rounds to the nearest second, and 60s is a minute
    60.0: "1m 0s",
    3725.0: "1h 2m 5s",
    -3.0: "0s",          #a clock step backwards must not print a negative time
}
for seconds, expected in cases.items():
    got = main.format_duration(seconds)
    check(f"{seconds} -> {expected!r} (got {got!r})", got == expected)
source = Path(main.__file__).read_text(encoding="utf-8")
check("both ways of stopping (overlay and debug window) print it that way",
      source.count('print(f"Total Runtime: {format_duration(total_elapsed)}")') == 2
      and 'print(f"Total Runtime: {total_elapsed' not in source)

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
