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

print("\n7. After a teleport, a position is only believed once it has MOVED")
#Seen live: the click fired the instant the teleport was issued and missed the item completely.
#shared_text_tracks still held the pre-teleport coordinates - published milliseconds earlier by
#detect_text - so "is there a track for this item?" was true, and answered with the stale
#position. The camera is locked to the character, so a real teleport shifts the item's screen
#position by about as far as we travelled; nothing having moved means we are still looking at a
#pre-teleport frame, whatever the clock says. A longer delay cannot express that - it is a bet on
#the cast animation, the OCR thread's phase and the frame time all at once.
WAS_AT = (500, 500)          #where the item was when the key was pressed
MIN_SHIFT = 150              #half of a 300px hop


def track_at(x, y, name="ITEM"):
    return (name, x - 30, y - 12, 60, 24)  #a box centred on (x, y)


check("the stale position is refused",
      not main._teleport_moved(track_at(*WAS_AT), *WAS_AT, MIN_SHIFT))
check("a position that barely drifted is refused",
      not main._teleport_moved(track_at(560, 540), *WAS_AT, MIN_SHIFT))
check("one pixel short of the shift is still refused",
      not main._teleport_moved(track_at(500, 500 + MIN_SHIFT - 1), *WAS_AT, MIN_SHIFT))
check("a full hop away is accepted",
      main._teleport_moved(track_at(500, 500 + MIN_SHIFT), *WAS_AT, MIN_SHIFT))
check("direction does not matter - the test is radial",
      main._teleport_moved(track_at(500 - 200, 500 - 200), *WAS_AT, MIN_SHIFT))
#A teleport that never happened (no mana, blocked ground) shifts nothing, so every poll refuses,
#the re-find times out, and the attempt ends WITHOUT clicking. That is the point of the rule:
#a missed pickup rather than a click into empty ground.
check("a teleport that did not happen can never satisfy it",
      not any(main._teleport_moved(track_at(*WAS_AT), *WAS_AT, MIN_SHIFT) for _ in range(5)))
#The freshness window has to outlast what it is waiting for, or the rule only converts a wrong
#click into a guaranteed miss.
check("the re-find window outlasts a full OCR cycle",
      main.TELEPORT_REACQUIRE_SECONDS > main.OCR_INTERVAL_SECONDS)
check("the required shift leaves slack for a short landing",
      0.0 < main.TELEPORT_MOVED_FRACTION < 1.0)

print("\n8. Which item to go for when several are on the ground")
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

print("\n9. Every collectable item has a distinct rank")
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

print("\n10. A key-collected item retries on its own clock, not the click's")
#0.8s between clicks exists because a click means "walk there"; a cast is over the instant it
#lands. Sharing one number made every failed telekinesis grab wait out a walk that never happened.
check("the key retry is a real number", isinstance(main.potion_config.key_collect_retry, float))
check("it is faster than re-clicking", main.potion_config.key_collect_retry < main.CLICK_RETRY_INTERVAL_SECONDS)
#Each attempt already spends AIM_SETTLE_SECONDS letting the game notice the cursor before the key
#is pressed at all, so a retry interval below that cannot make anything happen sooner - it just
#stops describing the real cadence.
check("it is not shorter than one attempt's own settle",
      main.potion_config.key_collect_retry >= actions.AIM_SETTLE_SECONDS)

print("\n11. The shipped user_config.txt is usable")
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

print("\n12. An item already on an open UI panel is not chased")
#The bug this guards was not a detection failure, which is what makes it worth real frames: an
#item in the bag or the stash draws a hover TOOLTIP, which is genuine on-screen text reading
#exactly "Full Rejuvenation Potion". OCR read it correctly and the matcher matched it correctly -
#the text simply does not mean on a panel what it means on the ground. So the filter is
#positional, and it is sound only because the panels are OPAQUE: a ground item behind one could
#not be seen or clicked anyway.
import os

import cv2 as cv
import numpy as np

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
check("both panels are configured",
      sorted(p.name for p in main.ui_panels) == ["inventory", "stash"])


def frame_and_gray(name):
    img = cv.imread(os.path.join(FIXTURES, name))
    return (None, None) if img is None else (img, cv.cvtColor(img, cv.COLOR_BGR2GRAY))


def panel(name):
    return next(p for p in main.ui_panels if p.name == name)


#--- the inventory, with a potion on the ground AND the same potion's tooltip in the bag ---------
frame, gray = frame_and_gray("inventory_open.png")
if frame is None or not text_detection.ocr_available():
    check("inventory_open.png present and OCR available", False)
else:
    check("the inventory is recognised as open", main._panel_open(panel("inventory"), gray) is True)
    #The stash is on the other side of the screen and is NOT open here. If this ever goes True the
    #two references have started matching each other, and a third of the screen stops collecting.
    check("...and the stash is not", main._panel_open(panel("stash"), gray) is False)

    found = text_detection.find_text_matches(frame, main.target_items)
    check(f"both potions are detected first ({len(found)} found)", len(found) == 2)
    kept = main._outside_open_panels(found, gray)
    check("exactly one survives the filter", len(kept) == 1)
    if kept:
        cx = kept[0][0] + kept[0][2] // 2
        x0, _y0, _x1, _y1 = main._panel_keep_out_px(panel("inventory"), frame.shape)
        #The survivor has to be the one on the GROUND, not merely "one of them".
        check(f"and it is the one on the ground (x={cx} < panel edge {x0})", cx < x0)

#--- the stash, open alongside the inventory the way it always is in town -----------------------
frame, gray = frame_and_gray("stash_open.png")
if frame is None or not text_detection.ocr_available():
    check("stash_open.png present and OCR available", False)
else:
    check("the stash is recognised as open", main._panel_open(panel("stash"), gray) is True)
    check("...and so is the inventory, as it always is in town",
          main._panel_open(panel("inventory"), gray) is True)
    found = text_detection.find_text_matches(frame, main.target_items)
    check(f"the stash tooltip is detected first ({len(found)} found)", len(found) == 1)
    check("and is filtered out", main._outside_open_panels(found, gray) == [])
    #With both panels open the world is the STRIP BETWEEN THEM, and it must stay collectable -
    #otherwise this fix quietly costs every town pickup, which is worse than what it fixed.
    h, w = frame.shape[:2]
    middle = [(w // 2 - 20, h // 2 - 10, 40, 20, "ITEM", True, (0, 255, 0))]
    check("a drop between the two panels is still collected",
          main._outside_open_panels(middle, gray) == middle)

#With a panel CLOSED its region must be completely inert, or a third of the screen stops being
#collectable - a far worse bug than the one being fixed, and a silent one.
closed_frame, closed_gray = frame_and_gray("pindle_pack.png")
if closed_frame is None:
    check("pindle_pack.png present", False)
else:
    for name in ("inventory", "stash"):
        check(f"a normal in-game frame is not read as an open {name}",
              main._panel_open(panel(name), closed_gray) is False)
        x0, y0, x1, y1 = main._panel_keep_out_px(panel(name), closed_frame.shape)
        inside = [((x0 + x1) // 2 - 20, (y0 + y1) // 2 - 10, 40, 20, "ITEM", True, (0, 255, 0))]
        check(f"a match in the {name}'s region survives while it is closed",
              main._outside_open_panels(inside, closed_gray) == inside)

#No configs, no filtering - the same None-means-behave-as-before rule the rest of the pipeline
#follows. A missing asset must never quietly stop items being collected.
_saved = main.ui_panels
main.ui_panels = []
try:
    probe = [(1500, 600, 40, 20, "ITEM", True, (0, 255, 0))]
    check("with no panel configs, nothing is filtered",
          main._outside_open_panels(probe, np.zeros((1080, 1920), np.uint8)) == probe)
finally:
    main.ui_panels = _saved

print("\n13. An action that measurably did nothing is retried early")
#The owner's report: the first click or cast often misses, and the pickup then lands on the 2nd or
#3rd attempt - each a full click_interval apart, which is 0.8s of dead time per miss while someone
#else walks off with the item. click_interval is long for one reason: a click means "walk over
#there", and re-issuing it mid-walk retargets the character. That reasoning only holds if the
#click LANDED. The evidence to tell those apart is already being polled: a camera that follows the
#character means an action that landed MOVES THE WORLD, and one that missed changes nothing.
#
#These run on the real clock with small intervals, so they take about a second in total.
POLL = 0.02
STILL = 0.10


def run(positions, click_interval=5.0, retry_if_still=STILL, timeout=0.6):
    """Drives act_until_gone against a scripted sequence of positions. Returns the aim points."""
    aimed = []
    seq = list(positions)

    def get_position():
        return seq.pop(0) if seq else seq_last[0]

    seq_last = [positions[-1]]
    return aimed, actions.act_until_gone(
        get_position, act=lambda x, y: aimed.append((x, y)),
        timeout=timeout, click_interval=click_interval, poll_interval=POLL,
        retry_if_still=retry_if_still, still_px=4)


#A target that never moves: the action is doing nothing, so it should fire repeatedly without
#waiting out the 5s click_interval.
still_positions = [(500, 500)] * 200
aimed, ok = run(still_positions)
check(f"a target that never moves is retried early ({len(aimed)} actions in 0.6s)", len(aimed) > 2)
check("...and the attempt still reports failure, since it never disappeared", ok is False)

#A target that moves after the action - i.e. the click landed and the character is walking - must
#NOT be re-clicked early. This is the behaviour click_interval exists to protect.
moving = [(500 + i * 20, 500) for i in range(200)]
aimed, _ok = run(moving)
check(f"a target that moves is left alone until click_interval ({len(aimed)} action)",
      len(aimed) == 1)

#Jitter is not movement: a tracked position wanders a pixel or two while nothing is happening, and
#treating that as "the action landed" would restore the dead time this removes.
jitter = [(500 + (i % 3), 500 - (i % 2)) for i in range(200)]
aimed, _ok = run(jitter)
check(f"a jittering position still counts as still ({len(aimed)} actions)", len(aimed) > 2)

#Off switch, for the one case RETRY_IF_STILL_SECONDS documents as wrong: a game that does not move
#the camera while the character walks.
aimed, _ok = run(still_positions, retry_if_still=None)
check(f"retry_if_still=None restores the old cadence ({len(aimed)} action)", len(aimed) == 1)

#And the early retry must never outrun a success: a target that disappears is gone, not retried.
gone = [(500, 500), (500, 500), None]
aimed, ok = run(gone)
check("a target that disappears reports success", ok is True)
check("...and is not acted on again afterwards", len(aimed) <= 2)

print("\n14. The settle before acting comes from user_config.txt")
#Raised because the owner reported first-attempt misses on BOTH paths. What a game needs between
#the cursor arriving and the action firing depends on the game and the machine, so it has to be
#tunable while playing rather than compiled in.
check("a click settle is configured", 0.0 <= main.potion_config.click_settle <= 2.0)
check("an aim settle is configured", 0.0 <= main.potion_config.aim_settle <= 2.0)
#A keypress carries no coordinates and is resolved against whatever the game last decided the
#cursor was over, so it needs at least as long as a click, whose button-down carries the position.
check("the key settle is not shorter than the click settle",
      main.potion_config.aim_settle >= main.potion_config.click_settle)
#click_with must actually use it - a settle that is read and ignored is the worst of both.
#See the same note in test_quit_game.py: hold_cursor loops on the clock, so a fake sleep has to
#advance a fake clock or it spins for the real duration.
_slept = []
_clock = [0.0]
_real_sleep, _real_counter = actions.time.sleep, actions.time.perf_counter


def _fake_sleep(seconds):
    _slept.append(seconds)
    _clock[0] += seconds


actions.time.sleep = _fake_sleep
actions.time.perf_counter = lambda: _clock[0]
_real_move, _real_down, _real_up = (actions.pyautogui.moveTo, actions.pyautogui.mouseDown,
                                    actions.pyautogui.mouseUp)
actions.pyautogui.moveTo = lambda *a, **k: None
actions.pyautogui.mouseDown = lambda *a, **k: None
actions.pyautogui.mouseUp = lambda *a, **k: None
try:
    actions.click_with(0.37)(10, 10)
    #The settle is spent HOLDING the cursor against the player's own mouse, so it arrives as many
    #small sleeps rather than one - what matters is the total, plus the click's own hold after it.
    held = sum(_slept) - actions.CLICK_HOLD_SECONDS
    check(f"click_with(0.37) actually holds for 0.37s ({held:.3f}s in {len(_slept)} slices)",
          abs(held - 0.37) < 1e-6 and len(_slept) > 2)
finally:
    actions.time.sleep, actions.time.perf_counter = _real_sleep, _real_counter
    actions.pyautogui.moveTo, actions.pyautogui.mouseDown, actions.pyautogui.mouseUp = (
        _real_move, _real_down, _real_up)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
