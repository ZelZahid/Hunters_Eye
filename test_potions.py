"""Synthetic tests for main.py's potion decision (_potion_due). No game required.

WHY THIS EXISTS: this is the first thing in the project that presses a key into a live game
based on a number read off the screen, and every way it can be wrong is expensive and hard to
notice while playing. Drinking on an unreadable meter empties the belt the moment you alt-tab;
an emergency tier blocked by an ordinary heal's cooldown fails in the one second it exists for;
a shared cooldown or a missing floor drains four columns into a single dip. None of that raises
an error - it just quietly costs you the run. Same reasoning as test_game_state.py.
"""
import sys

import main

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


NOW = 10_000.0          # an arbitrary "now" far from zero, so cooldowns are real comparisons
COLD = {}               # nothing has fired yet
NEVER = 0.0             # ...and nothing has fired recently either


def due(readings, now=NOW, last_fired=None, last_any=NEVER):
    rule = main._potion_due(readings, now, COLD if last_fired is None else last_fired, last_any)
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

print("\n3. 0.0 is a real reading and IS acted on")
#The other half of the None/0.0 distinction: an orb that really is empty must still trigger.
check("health 0.0 -> rejuvenation", due({"health": 0.0, "mana": 1.0}) == "rejuvenation")

print("\n4. The right tier fires for the right severity")
check("health 30% -> ordinary health potion", due({"health": 0.30, "mana": 1.0}) == "health")
check("health 20% -> rejuvenation (at the boundary)", due({"health": 0.20, "mana": 1.0}) == "rejuvenation")
check("health 15% -> rejuvenation", due({"health": 0.15, "mana": 1.0}) == "rejuvenation")
check("health 35% -> health potion (at the boundary)", due({"health": 0.35, "mana": 1.0}) == "health")
check("mana 20% -> mana potion", due({"health": 1.0, "mana": 0.20}) == "mana")

print("\n5. Urgency wins: critical health outranks everything else pending")
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
      due({"health": 0.0}, last_any=NOW - 0.1) is None)
check("...and stops blocking once the floor has passed",
      due({"health": 0.0}, last_any=NOW - (main.POTION_MIN_GAP_SECONDS + 0.01)) == "rejuvenation")

print("\n8. Sustained low health drinks at the cooldown rate, not the poll rate")
#Simulates the real loop: poll every POTION_POLL_SECONDS for 10s at a flat 30% health, applying
#both the per-rule cooldown and the global floor exactly as run_potion_drinking() does.
fired, last_fired, last_any = [], {}, NEVER
t = NOW
for _ in range(int(10.0 / main.POTION_POLL_SECONDS)):
    t += main.POTION_POLL_SECONDS
    rule = main._potion_due({"health": 0.30, "mana": 1.0}, t, last_fired, last_any)
    if rule is not None:
        fired.append(rule.label)
        last_fired[rule.label] = t
        last_any = t
check(f"10s at 30% health -> {len(fired)} potions, not {int(10.0 / main.POTION_POLL_SECONDS)}",
      2 <= len(fired) <= 4)
check("every one of them was the ordinary health tier", set(fired) == {"health"})

print("\n9. Alternating keys spread across both rejuvenation columns")
rejuv = next(r for r in main.POTION_RULES if r.label == "rejuvenation")
used = [rejuv.keys[i % len(rejuv.keys)] for i in range(6)]
check(f"6 emergency drinks use both columns evenly: {used}",
      used.count("2") == 3 and used.count("3") == 3)
check("the ordinary health tier has exactly one key", len(
    next(r for r in main.POTION_RULES if r.label == "health").keys) == 1)

print("\n10. The configured belt matches the player's actual bindings")
bindings = {r.label: r.keys for r in main.POTION_RULES}
check("health potion on '1'", bindings["health"] == ("1",))
check("rejuvenation on '2' and '3'", bindings["rejuvenation"] == ("2", "3"))
check("mana potion on '4'", bindings["mana"] == ("4",))

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
