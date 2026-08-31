"""Synthetic tests for user_config.py. No game required.

WHY THIS EXISTS: this file is edited by hand, by a person, while not looking at the code - which
means every value reaching it is untrusted input. It decides when the program presses keys into a
live game, so the two failure modes both matter: crashing on a typo (the program is meant to run
unattended, so a config typo must never stop it) and SILENTLY running with a setting nobody
intended, which is worse because nothing looks wrong. Every case below is one of those.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import sys
import tempfile

from core import user_config

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def load(text, **kw):
    path = os.path.join(tempfile.mkdtemp(), "user_config.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return user_config.load(path, **kw)


FALLBACK = (user_config.Rule("health", 0.5, ("9",), 1.0, "fallback"),)

GOOD = """
[settings]
enabled = yes
min_gap = 0.6
snooze = 10
ignore_below = 0.03

[rejuvenation]
meter = health
below = 20%
keys = 2, 3
cooldown = 1.0

[health]
meter = health
below = 35%
keys = 1
cooldown = 4.0
"""

print("1. A well-formed file parses as written")
cfg = load(GOOD)
check("loaded from a file", cfg.loaded)
check("enabled", cfg.enabled is True)
check("min_gap 0.6", cfg.min_gap == 0.6)
check("ignore_below 0.03", cfg.ignore_below == 0.03)
check("two rules", len(cfg.rules) == 2)

print("\n2. File ORDER is rule priority - the emergency tier must stay first")
#Order is the only thing expressing "which rule wins when both match", so it has to survive
#parsing. A dict that reordered sections would silently invert the priority.
check("rejuvenation before health", [r.label for r in cfg.rules] == ["rejuvenation", "health"])

print("\n3. '20%' and '0.20' mean the same thing")
#A percent sign is how a person writes a threshold. Rejecting it - or worse, reading 20% as 20.0 -
#would make the rule fire at every possible value.
a = load(GOOD).rules[0].at_or_below
b = load(GOOD.replace("below = 20%", "below = 0.20")).rules[0].at_or_below
check(f"both parse to 0.20 (got {a} and {b})", a == b == 0.20)
check("a threshold is never > 1.0", all(r.at_or_below <= 1.0 for r in load(GOOD).rules))

print("\n4. Keys are configurable, including reassigning the belt")
swapped = load(GOOD.replace("keys = 2, 3", "keys = 1").replace("keys = 1\ncooldown = 4.0",
                                                               "keys = 4\ncooldown = 4.0"))
check("rejuvenation moved to key 1", swapped.rules[0].keys == ("1",))
check("health moved to key 4", swapped.rules[1].keys == ("4",))
check("multi-key rules keep their order", load(GOOD).rules[0].keys == ("2", "3"))

print("\n5. A missing file is a normal condition, not an error")
missing = user_config.load(os.path.join(tempfile.mkdtemp(), "nope.txt"), default_rules=FALLBACK)
check("does not raise", True)
check("reports it was not loaded", not missing.loaded)
check("falls back to the built-in rules", missing.rules == FALLBACK)
check("falls back to default settings", missing.min_gap == user_config.DEFAULTS["min_gap"])

print("\n6. Bad values fall back to a default and never crash")
bad = load("""
[settings]
min_gap = banana
snooze = -5
ignore_below = 400
enabled = maybe

[rule]
meter = health
below = 20%
keys = 1
cooldown = not-a-number
""", default_rules=FALLBACK)
check("min_gap 'banana' -> default", bad.min_gap == user_config.DEFAULTS["min_gap"])
check("snooze -5 (out of range) -> default", bad.snooze == user_config.DEFAULTS["snooze"])
check("ignore_below 400 (out of range) -> default", bad.ignore_below == user_config.DEFAULTS["ignore_below"])
check("enabled 'maybe' -> default", bad.enabled == user_config.DEFAULTS["enabled"])
check("cooldown 'not-a-number' -> default, rule kept", bad.rules[0].cooldown == 1.0)

print("\n7. A rule naming a meter that does not exist is DROPPED, not repointed")
#Pointing a rule at the wrong meter would drink based on a number measured somewhere else.
#A rule that can never fire is as bad as one that fires wrongly, and much harder to notice.
typo = load(GOOD.replace("meter = health", "meter = helth", 1),
            known_meters={"health", "mana"}, default_rules=FALLBACK)
check("the typo'd rule is gone", "rejuvenation" not in [r.label for r in typo.rules])
check("the valid rule survives", "health" in [r.label for r in typo.rules])

print("\n8. Incomplete rules are dropped, the rest of the file still works")
partial = load(GOOD + "\n[broken]\nmeter = health\n", default_rules=FALLBACK)
check("rule with no 'below'/'keys' dropped", "broken" not in [r.label for r in partial.rules])
check("the good rules survive", len(partial.rules) == 2)
check("empty keys list drops the rule",
      "x" not in [r.label for r in load("[x]\nmeter=health\nbelow=10%\nkeys=  ,  \n",
                                        default_rules=FALLBACK).rules])

print("\n9. A file with no usable rule at all falls back rather than doing nothing")
#Running with zero rules looks identical to "working fine" from outside, which is the worst
#possible way to fail for something whose entire job is to act when you are in trouble.
empty = load("[settings]\nenabled = yes\n", default_rules=FALLBACK)
check("falls back to built-in rules", empty.rules == FALLBACK)

print("\n10. Garbage that is not even INI shape does not crash the program")
for junk in ("!!! not a config at all", "[unclosed\nmeter=health", "\x00\x01\x02", ""):
    try:
        cfg = load(junk, default_rules=FALLBACK)
        ok = cfg.rules == FALLBACK
    except Exception as exc:                      # noqa: BLE001 - that is the thing being tested
        ok = False
        print(f"       raised {type(exc).__name__}: {exc}")
    check(f"{junk[:22]!r} -> defaults, no exception", ok)

print("\n11. 'enabled = no' turns it off without deleting the rules")
off = load(GOOD.replace("enabled = yes", "enabled = no"))
check("enabled is False", off.enabled is False)
check("rules are still parsed", len(off.rules) == 2)

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
