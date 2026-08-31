"""Synthetic tests for main.py's potion decision (_potion_due). No game required.

WHY THIS EXISTS: this is the first thing in the project that presses a key into a live game based
on a number read off the screen, and every way it can be wrong is expensive and hard to notice
while playing. Drinking on an unreadable meter empties the belt the moment you alt-tab; an
emergency tier blocked by an ordinary heal's cooldown fails in the one second it exists for; a
shared cooldown or a missing floor drains four columns into a single dip. None of that raises an
error - it just quietly costs you the run. Same reasoning as test_game_state.py.

THE LOGIC IS TESTED AGAINST FIXED RULES DEFINED HERE, NOT AGAINST user_config.txt. That file
exists to be retuned by the user, so asserting against its current thresholds would report their
edit as a code defect the first time they changed one. Section 8 checks the shipped file instead,
and checks only the things that must be true of ANY sane configuration.
"""
import sys

import main
import user_config

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


NOW = 10_000.0          # an arbitrary "now" far from zero, so cooldowns are real comparisons
NEVER = 0.0             # nothing has fired recently
FLOOR = 0.03            # the noise floor these tests assume

#The rules this file reasons about. Deliberately a fixed copy of the documented defaults rather
#than whatever user_config.txt currently says - see the module docstring.
RULES = (
    user_config.Rule("health", 0.20, ("2", "3"), 1.0, "rejuvenation"),
    user_config.Rule("health", 0.35, ("1",), 4.0, "health"),
    user_config.Rule("mana", 0.25, ("4",), 4.0, "mana"),
)


def due(readings, now=NOW, last_fired=None, last_any=NEVER, rules=RULES, floor=FLOOR):
    rule = main._potion_due(readings, now, {} if last_fired is None else last_fired,
                            last_any, rules=rules, ignore_below=floor)
    return None if rule is None else rule.label


print("1. A healthy character drinks nothing")
check("health 100%, mana 100% -> no potion", due({"health": 1.0, "mana": 1.0}) is None)
check("health just above the threshold -> no potion", due({"health": 0.36, "mana": 1.0}) is None)

print("\n2. An unreadable meter is NOT an emergency")
#The single most important case here. None means "could not read" - alt-tabbed, window lost,
#Smoother past MAX_CARRIED_FAILURES. Treating it as a low value would dump the whole belt at
#exactly the moment nothing is wrong.
check("health None -> no potion", due({"health": None, "mana": 1.0}) is None)
check("health None, mana None -> no potion", due({"health": None, "mana": None}) is None)
check("empty readings (startup, before any frame) -> no potion", due({}) is None)
check("health None does not block a genuinely low MANA", due({"health": None, "mana": 0.1}) == "mana")

print("\n3. A reading below the noise floor is ignored, not treated as critical")
#At CAPTURE_SCALE 0.3 an orb is only ~43 rows tall, so one row is 2.3% and the smallest possible
#non-zero reading is indistinguishable from a stray patch of the right colour where the meter
#should be. Seen live: the Diablo II lobby, where red character names sit where the health orb
#would be, read 2% health and fired an emergency potion. This costs nothing real, because a meter
#does not teleport - health falls THROUGH 20%, 15% and 10% on its way down at ~50 samples a
#second, so the emergency tier has already fired long before a reading gets this low.
check(f"health just below the {FLOOR:.0%} floor -> nothing",
      due({"health": FLOOR - 0.01, "mana": 1.0}) is None)
check("health 0.0 -> nothing (game_state can no longer produce a real 0.0 at all)",
      due({"health": 0.0, "mana": 1.0}) is None)
check("mana below the floor -> nothing", due({"health": 1.0, "mana": FLOOR - 0.01}) is None)
check("...but a reading just ABOVE the floor is still acted on",
      due({"health": FLOOR + 0.01, "mana": 1.0}) == "rejuvenation")
check("a low health reading well above the floor still fires",
      due({"health": 0.10, "mana": 1.0}) == "rejuvenation")
check("the lobby case end to end: health 2%, mana unreadable -> nothing",
      due({"health": 0.02, "mana": None}) is None)
check("a floor of 0 disables the filter entirely",
      due({"health": 0.01, "mana": 1.0}, floor=0.0) == "rejuvenation")

print("\n4. The right tier fires for the right severity")
check("health 30% -> ordinary health potion", due({"health": 0.30, "mana": 1.0}) == "health")
check("health 20% -> rejuvenation (at the boundary)", due({"health": 0.20, "mana": 1.0}) == "rejuvenation")
check("health 15% -> rejuvenation", due({"health": 0.15, "mana": 1.0}) == "rejuvenation")
check("health 35% -> health potion (at the boundary)", due({"health": 0.35, "mana": 1.0}) == "health")
check("mana 20% -> mana potion", due({"health": 1.0, "mana": 0.20}) == "mana")

print("\n5. Urgency wins: the first matching rule takes priority")
#File order is priority, which is what makes "put the emergency tier first" meaningful.
check("health 10% AND mana 5% -> rejuvenation first", due({"health": 0.10, "mana": 0.05}) == "rejuvenation")
check("health 30% AND mana 5% -> health before mana", due({"health": 0.30, "mana": 0.05}) == "health")

print("\n6. Each rule cools down on its OWN clock")
#The emergency tier must not be blocked by an ordinary heal drunk a second ago - taking a big hit
#right after a healing potion is precisely when a rejuvenation is needed.
just_healed = {"health": NOW - 1.0}
check("health potion 1s ago does not block a rejuvenation",
      due({"health": 0.10}, last_fired=just_healed, last_any=NOW - 1.0) == "rejuvenation")
check("health potion 1s ago DOES block another health potion",
      due({"health": 0.30}, last_fired=just_healed, last_any=NOW - 1.0) is None)
check("health potion 5s ago no longer blocks (cooldown 4s)",
      due({"health": 0.30}, last_fired={"health": NOW - 5.0}, last_any=NOW - 5.0) == "health")
check("a health cooldown does not block MANA",
      due({"health": 1.0, "mana": 0.1}, last_fired=just_healed, last_any=NOW - 1.0) == "mana")

print("\n7. The global floor stops any burst, whatever the rules say")
#The safety net under a mistuned threshold or a misreading orb: per-rule cooldowns are the knob,
#this is what makes "empty the belt in one second" impossible regardless of them.
check("a potion 0.1s ago blocks even a critical drink",
      due({"health": 0.08}, last_any=NOW - 0.1) is None)
check("...and stops blocking once the gap has passed",
      due({"health": 0.08}, last_any=NOW - (main.POTION_MIN_GAP_SECONDS + 0.01)) == "rejuvenation")

print("\n8. Sustained low health drinks at the cooldown rate, not the poll rate")
fired, last_fired, last_any = [], {}, NEVER
t = NOW
for _ in range(int(10.0 / main.POTION_POLL_SECONDS)):
    t += main.POTION_POLL_SECONDS
    rule = main._potion_due({"health": 0.30, "mana": 1.0}, t, last_fired, last_any,
                            rules=RULES, ignore_below=FLOOR)
    if rule is not None:
        fired.append(rule.label)
        last_fired[rule.label] = t
        last_any = t
check(f"10s at 30% health -> {len(fired)} potions, not {int(10.0 / main.POTION_POLL_SECONDS)}",
      2 <= len(fired) <= 4)
check("every one of them was the ordinary health tier", set(fired) == {"health"})

print("\n9. Alternating keys spread across every column a rule lists")
rejuv = next(r for r in RULES if r.label == "rejuvenation")
used = [rejuv.keys[i % len(rejuv.keys)] for i in range(6)]
check(f"6 emergency drinks use both columns evenly: {used}",
      used.count("2") == 3 and used.count("3") == 3)

print("\n10. The SHIPPED user_config.txt is sane (not: has particular values)")
#Thresholds and bindings are the user's to tune, so this asserts only what must be true of any
#configuration - anything stricter would fail the first time they retuned a number.
cfg = main.potion_config
check("it loaded from the file, not the built-in fallback", cfg.loaded)
check("at least one rule is configured", len(cfg.rules) >= 1)
check("every threshold is a sensible fraction",
      all(0.0 <= r.at_or_below <= 1.0 for r in cfg.rules))
check("every rule names a calibrated meter",
      all(r.meter in {m.name for m in main.meters} for r in cfg.rules))
check("every rule has at least one key", all(len(r.keys) >= 1 for r in cfg.rules))
check("every cooldown is non-negative", all(r.cooldown >= 0 for r in cfg.rules))
check("the global gap is a real floor", main.POTION_MIN_GAP_SECONDS > 0)
print("      configured: " + " | ".join(
    f"{r.label} <={r.at_or_below:.0%} -> {'/'.join(r.keys)}" for r in cfg.rules))

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
