"""Synthetic tests for main.py's teleport decision (_should_teleport). No game required.

WHY THIS EXISTS: this decides whether to press a movement skill into a live game, and every way
it can be wrong is quiet rather than loud. Teleporting onto an item that is already at your feet
burns mana on every single drop and is easy to miss while playing; teleporting onto a telekinesis
item throws away the whole reason that tag exists; and a missing cooldown turns a failed re-find
into a teleport loop that does nothing but drain mana and stand still. None of that raises an
error. Same reasoning as test_potions.py, which covers the other key this program presses.

The comparison itself is the part most worth pinning down: it is a distance against a threshold,
so getting it backwards produces behaviour that still looks purposeful - it just teleports in
exactly the cases it was supposed to skip.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_auto_collect.py), so the repo root has to be on the path before
#any project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import actions
from core import text_detection

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}")


CLICK = text_detection.COLLECT_BY_CLICK
KEY = text_detection.COLLECT_BY_KEY_PREFIX + "e"
CHARACTER = (960, 524)          #roughly where the character sits on a 1920x1080 screen
MIN_DISTANCE = 270              #25% of 1080, the shipped default
LONG_AGO = main.TELEPORT_COOLDOWN_SECONDS + 1.0


def should(collect_label=CLICK, item=(960, 100), character=CHARACTER,
           min_distance=MIN_DISTANCE, since=LONG_AGO):
    return main._should_teleport(collect_label, item, character, min_distance, since)


print("\n1. Far-away items are worth teleporting onto")
check("straight up the screen", should(item=(960, 524 - 400)))
check("straight down", should(item=(960, 524 + 400)))
check("diagonally - distance is radial, not per-axis",
      should(item=(960 + 300, 524 + 300)))
check("exactly at the threshold counts as far",
      should(item=(960, 524 - MIN_DISTANCE)))

print("\n2. Close items are just clicked - the owner's 'within a few feet' rule")
check("at the character's feet", not should(item=CHARACTER))
check("one pixel inside the threshold", not should(item=(960, 524 - MIN_DISTANCE + 1)))
check("far on neither axis, but close overall",
      not should(item=(960 + 150, 524 + 150)))
check("a bigger threshold makes a previously-far item close",
      not should(item=(960, 100), min_distance=1000))

print("\n3. Telekinesis items are never teleported onto")
#The whole point of [key:e] is taking the item from across the room. Teleporting first spends mana
#removing the exact distance that skill exists to cover.
check("a [key:e] item, however far away", not should(collect_label=KEY, item=(960, 100)))
check("...even at the far corner of the screen",
      not should(collect_label=KEY, item=(0, 0)))
check("an unrecognised label is not treated as a click either",
      not should(collect_label="robot-arm"))

print("\n4. The cooldown stops a failed re-find becoming a teleport loop")
check("just teleported", not should(since=0.0))
check("still inside the cooldown", not should(since=main.TELEPORT_COOLDOWN_SECONDS - 0.01))
check("cooldown elapsed", should(since=main.TELEPORT_COOLDOWN_SECONDS))
#The re-find can take SETTLE + REACQUIRE before it gives up, and the attempt that follows must
#not teleport again. If this fails, the cooldown is shorter than the thing it exists to outlast.
check("the cooldown outlasts a full failed re-find",
      main.TELEPORT_COOLDOWN_SECONDS > main.TELEPORT_SETTLE_SECONDS + main.TELEPORT_REACQUIRE_SECONDS)

print("\n5. An unknown character position means walking, not guessing")
#Same rule as everywhere else in this project: None is "cannot tell", and cannot tell must behave
#the way the program did before the check existed.
check("no character position, far item", not should(character=None, item=(0, 0)))
check("no character position, close item", not should(character=None, item=CHARACTER))

print("\n6. The re-find window can actually outlast one OCR cycle")
#Teleporting moves the camera further than the tracker's search margin, so the track is usually
#lost and a full OCR scan is what finds the item again. A re-find shorter than that scan interval
#would give up before the only thing that can answer has run.
check("re-find outlasts one OCR interval",
      main.TELEPORT_REACQUIRE_SECONDS > main.OCR_INTERVAL_SECONDS)

print("\n7. Which item to go for when several are on the ground")
#Priority is the order of targets.txt, further down winning, and nearest-to-cursor only breaks
#ties. Nearest-to-cursor ALONE was the old rule, and it is exactly the thing being fixed: a
#rejuvenation potion at your feet would be taken before a Ber rune across the room, and the rune
#is the one another player can reach while we are busy.
LOW = "REJUVENATION POTION"   #near the top of targets.txt, so least wanted
HIGH = "ZOD RUNE"             #near the bottom, so most wanted
check("the fixture names really are in that order in targets.txt",
      main._collect_priority(HIGH) > main._collect_priority(LOW))

def track(name, x, y):
    return (name, x - 20, y - 10, 40, 20)  #a box whose centre is (x, y)

AT_CURSOR = track(LOW, 500, 500)
FAR_AWAY = track(HIGH, 1500, 200)
check("the further, better item wins over the nearer, worse one",
      main._pick_target([AT_CURSOR, FAR_AWAY], 500, 500)[0] == HIGH)
check("...and the order the tracks arrive in makes no difference",
      main._pick_target([FAR_AWAY, AT_CURSOR], 500, 500)[0] == HIGH)
check("one candidate is picked whatever it is",
      main._pick_target([AT_CURSOR], 500, 500)[0] == LOW)
#Within one priority, the old rule stands - this is what settles two of the same item down at
#once, where there is nothing else to tell them apart.
near, far = track(HIGH, 520, 500), track(HIGH, 1500, 200)
check("a tie is broken by nearest to the cursor",
      main._pick_target([far, near], 500, 500)[1] == near[1])
check("an item with no entry at all ranks below everything listed",
      main._pick_target([track("NOT A LISTED ITEM", 500, 500), FAR_AWAY], 500, 500)[0] == HIGH)

print("\n8. Every collectable item has a distinct rank")
#Two items sharing a rank would silently fall back to nearest-to-cursor between them, which is
#the behaviour this replaced. They cannot share one while rank is the line number, so this is
#really a check that nothing has started handing out ranks some other way.
collectable = [n for n, spec in main.target_items.items() if spec.get("to_collect")]
ranks = [main._collect_priority(n) for n in collectable]
check(f"{len(collectable)} collectable items, {len(set(ranks))} distinct ranks",
      len(set(ranks)) == len(collectable))
#The one thing the owner actually asked for, asserted against the real file rather than a fixture.
potions = [n for n in collectable if "REJUVENATION" in n]
others = [n for n in collectable if n not in potions]
check("every rejuvenation potion ranks below every other collected item",
      potions and others and
      max(main._collect_priority(n) for n in potions) < min(main._collect_priority(n) for n in others))

print("\n9. A key-collected item retries on its own clock, not the click's")
#0.8s between clicks exists because a click means "walk there"; a cast is over the instant it
#lands. Sharing one number made every failed telekinesis grab wait out a walk that never happened.
check("the key retry is a real number", isinstance(main.potion_config.key_collect_retry, float))
check("it is faster than re-clicking", main.potion_config.key_collect_retry < main.CLICK_RETRY_INTERVAL_SECONDS)
#Each attempt already spends AIM_SETTLE_SECONDS letting the game notice the cursor before the key
#is pressed at all, so a retry interval below that cannot make anything happen sooner - it just
#stops describing the real cadence.
check("it is not shorter than one attempt's own settle",
      main.potion_config.key_collect_retry >= actions.AIM_SETTLE_SECONDS)

print("\n10. The shipped user_config.txt is usable")
#Not asserting the owner's chosen values - those exist to be retuned - only that whatever is in
#the file could not break the feature.
cfg = main.potion_config
check("teleport_min_distance is a sane fraction", 0.0 <= cfg.teleport_min_distance <= 1.0)
check("teleport_key is a string", isinstance(cfg.teleport_key, str))
if cfg.teleport_key:
    import keyboard
    try:
        keyboard.key_to_scan_codes(cfg.teleport_key)
        recognised = True
    except Exception:
        recognised = False
    check(f"teleport_key {cfg.teleport_key!r} is a key this system recognises", recognised)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
