"""Tests for the Pindleskin fight (routes/pindle.py's fight()) - no game required.

WHY THIS EXISTS: the fight presses an attack key into a live game, on its own, possibly with
nobody watching. Its sensors fail quietly - a plate test that is a little off casts at bare floor
or never casts at all, and nothing raises - and its one safety action (leaving the game on low
health) only matters in exactly the situation nobody wants to create to test it. So the sensors
are measured on real frames, and the loop is driven by fakes through the situations that matter:
a kill, low health, not allowed to act, and something that moves but is not a monster.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2 as cv
import numpy as np

from core import text_detection
from routes import pindle

failures = 0
FIXTURES = Path(__file__).resolve().parent / "fixtures"
#The pack's moving region between fight_motion_27 and _28, located by hand in the full screenshots.
PACK_CENTRE = (1392, 285)


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def fixture(name):
    image = cv.imread(str(FIXTURES / name))
    if image is None:
        check(f"fixture {name} exists (see tests/fixtures/README.md)", False)
    return image


print("1. The monster plate: up with a monster under the cursor, and only then")
pindleskin = fixture("plate_pindleskin.png")
warrior = fixture("plate_defiled_warrior.png")
nothing = fixture("plate_none.png")
if pindleskin is not None and warrior is not None and nothing is not None:
    with_plate = [pindle.plate_red_fraction(f) for f in (pindleskin, warrior)]
    without = pindle.plate_red_fraction(nothing)
    check(f"Pindleskin's plate is up ({with_plate[0]:.2f} red)", pindle.plate_showing(pindleskin))
    check(f"a Defiled Warrior's plate is up ({with_plate[1]:.2f} red)", pindle.plate_showing(warrior))
    check(f"no plate with nothing hovered ({without:.2f} red)", not pindle.plate_showing(nothing))
    check(f"margin {min(with_plate) - without:.2f} between them (needs >= 0.5)",
          min(with_plate) - without >= 0.5)
    check("a BGRA frame gives the same answer",
          pindle.plate_showing(cv.cvtColor(pindleskin, cv.COLOR_BGR2BGRA)))
    check("no frame -> no plate", not pindle.plate_showing(None))
    if text_detection.ocr_available():
        check(f"the plate reads PINDLESKIN ({pindle.plate_name(pindleskin)!r})",
              pindle.PINDLESKIN_WORD in pindle.plate_name(pindleskin))
        check(f"a warrior's plate does not ({pindle.plate_name(warrior)!r})",
              pindle.PINDLESKIN_WORD not in pindle.plate_name(warrior))
    else:
        print("  NOT RUNNING the name checks - no OCR backend installed.")

print("\n2. Movement from a still camera points at the pack")
a, b = fixture("fight_motion_27.png"), fixture("fight_motion_28.png")
if a is not None and b is not None:
    spots = pindle.motion_candidates(a, b, frame_scale=0.5)
    check(f"something moved ({len(spots)} spots)", len(spots) > 0)
    if spots:
        first = np.array(spots[0]) / 0.5   #back to full-size pixels
        miss = float(np.linalg.norm(first - np.array(PACK_CENTRE)))
        check(f"the biggest one is the pack ({miss:.0f}px from its centre)", miss <= 150)
    check("the same frame twice -> nothing moved", pindle.motion_candidates(a, a.copy(), 0.5) == [])
    check("frames of different sizes -> nothing, not a crash",
          pindle.motion_candidates(a, b[:100], 0.5) == [])


class World:
    """A fake game: a clock that only moves when the fight sleeps, one spot with a monster on it,
    and a plate that is up exactly when the cursor is on that spot and the monster is alive."""

    def __init__(self, hits_to_kill=5, health=0.9, allowed=True, spot=(500, 300), real=True):
        self.now = 0.0
        self.hits_left = hits_to_kill
        self.level = health
        self.allowed = allowed
        self.spot = spot
        self.real = real            #False: the spot moves but there is no monster on it
        self.cursor = None
        self.casts = 0
        self.bailed = 0
        self.hovers = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def grab(self):
        alive = self.real and self.hits_left > 0
        on_it = self.cursor == self.spot
        return (pindleskin if alive and on_it else nothing).copy(), (0, 0)

    def candidates(self, first, second):
        return [self.spot] if self.hits_left > 0 else []

    def hover(self, x, y):
        self.cursor = (x, y)
        self.hovers.append((x, y))

    def cast(self):
        #Only a cast with a monster actually under the cursor counts - which is what the fight
        #promises to check before every press.
        self.casts += 1
        if self.real and self.cursor == self.spot:
            self.hits_left -= 1

    def fight(self, cancelled=lambda: False):
        return pindle.fight(self.grab, self.hover, self.cast, lambda: self.allowed,
                            lambda: self.level, self.bail, log=lambda *_: None,
                            candidates=self.candidates, clock=self.clock, sleep=self.sleep,
                            cancelled=cancelled)

    def bail(self):
        self.bailed += 1


print("\n3. The fight, driven through a fake game")
if pindleskin is not None and nothing is not None:
    world = World(hits_to_kill=5)
    result = world.fight()
    check(f"a monster that takes 5 hits is killed and the fight ends ({result!r})", result == "done")
    check(f"exactly 5 casts, every one with the monster under the cursor ({world.casts})",
          world.casts == 5 and world.hits_left == 0)
    check("the cursor went to the moving spot", world.hovers and world.hovers[0] == (500, 300))
    check("it did not leave the game", world.bailed == 0)
    check(f"and it stopped within the quiet period, not at the timeout ({world.now:.1f}s)",
          world.now < pindle.FIGHT_TIMEOUT_SECONDS)

    world = World(hits_to_kill=5, health=0.15)
    result = world.fight()
    check(f"at 15% health it leaves the game ({result!r})", result == "chickened")
    check("left exactly once, without a single cast", world.bailed == 1 and world.casts == 0)

    world = World(hits_to_kill=5, health=None)
    result = world.fight()
    check("an unreadable health orb (None) is NOT low health - it fights on",
          result == "done" and world.bailed == 0 and world.casts == 5)

    world = World(hits_to_kill=5, allowed=False)
    result = world.fight()
    check(f"not allowed to act -> no cursor, no casts, and it gives up ({result!r})",
          result == "stopped" and not world.hovers and world.casts == 0)

    world = World(hits_to_kill=5, real=False)
    result = world.fight()
    check(f"something moves but there is no monster on it -> no casts ({world.casts}), "
          f"and it still finishes ({result!r})", world.casts == 0 and result == "done")

    #Health drops to the chicken level mid-fight: it must stop casting and leave right away.
    world = World(hits_to_kill=50)
    original_cast = world.cast

    def cast_and_get_hurt():
        original_cast()
        if world.casts == 3:
            world.level = 0.10

    world.cast = cast_and_get_hurt
    result = world.fight()
    check(f"health collapsing mid-fight -> leaves after the cast in flight ({world.casts} casts, "
          f"{result!r})", result == "chickened" and world.bailed == 1 and world.casts == 3)

print("\n4. 'F4' stops the fight outright - it does not resume like the auto-collect snooze")
if pindleskin is not None and nothing is not None:
    world = World(hits_to_kill=50)
    result = world.fight(cancelled=lambda: True)
    check(f"cancelled before it starts -> nothing touched ({result!r})",
          result == "cancelled" and not world.hovers and world.casts == 0)

    world = World(hits_to_kill=50)
    result = world.fight(cancelled=lambda: world.casts >= 2)
    check(f"cancelled mid-attack -> stops at once ({world.casts} casts, {result!r})",
          result == "cancelled" and world.casts == 2)
    check("and it never left the game on the way out", world.bailed == 0)

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
