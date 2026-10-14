"""Tests for the bot test mode's teleport loop (routes/shuttle.py). No game required.

WHY THIS EXISTS: the shuttle presses a key into a live game with nobody watching, and decides
whether each press worked by measuring how far the scene slid. Both halves fail quietly. A shift
measurement that is wrong in sign walks the character away from the items a leg at a time; one that
reads "nothing moved" on a frame it simply could not compare stops a good run; one that reads
"moved" on noise lets a dead key be pressed for minutes. So:

  1. scene_shift() is measured against REAL frames with known camera positions (the route fixtures
     test_route.py already uses, with ground truth from the registration that built the maps) -
     including a mid-fight pair with monsters moving - and against pairs of DIFFERENT places, which
     must come back below MIN_SHIFT_RESPONSE ("cannot tell"), never as a confident shift.
  2. Shuttle's aiming is walked through short landings and misses.
  3. run() is driven end to end through a fake game: a textured world, a camera locked to the
     character, a key that teleports after a cast delay - or lands short, or does nothing - and the
     guards, health and stop key.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_shuttle.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import cv2 as cv
import numpy as np

from routes import shuttle

failures = 0
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_SCALE = 0.25 #the route fixtures are stored at the map's scale - see tests/fixtures/README.md

#Camera positions from test_route.py's ground truth, in source (full-resolution) pixels.
CAMERA = {
    "route_harrogath_01.png": (1238.4, 979.3),
    "route_harrogath_05.png": (1180.8, 1209.7),
    "route_harrogath_06.png": (1008.0, 1497.7),
    "route_temple_24.png": (1642.0, 0.0),
    "route_temple_28.png": (1584.4, 57.6),
}
SAME_PLACE_PAIRS = (
    ("route_temple_24.png", "route_temple_28.png"),       #mid-fight, monsters moving
    ("route_harrogath_01.png", "route_harrogath_05.png"),
    ("route_harrogath_05.png", "route_harrogath_06.png"), #72px apart out of 181 rows compared
)
HARROGATH = ("route_harrogath_01.png", "route_harrogath_05.png", "route_harrogath_06.png",
             "route_harrogath_09.png", "route_harrogath_10.png")
TEMPLE = ("route_temple_19.png", "route_temple_20.png", "route_temple_21.png",
          "route_temple_24.png", "route_temple_28.png")
MAX_SHIFT_ERROR = 1.5 #in fixture pixels, i.e. 6 real pixels


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def gray_fixture(name):
    image = cv.imread(str(FIXTURES / name))
    if image is None:
        #A failure, not a skip - see tests/fixtures/README.md on tests that skip quietly.
        check(f"fixture {name} exists", False)
        return None
    return cv.cvtColor(image, cv.COLOR_BGR2GRAY)


print("1. scene_shift() on real frames")
for a, b in SAME_PLACE_PAIRS:
    fa, fb = gray_fixture(a), gray_fixture(b)
    if fa is None or fb is None:
        continue
    #The scene slides the opposite way to the camera.
    want = (-(CAMERA[b][0] - CAMERA[a][0]) * FIXTURE_SCALE, -(CAMERA[b][1] - CAMERA[a][1]) * FIXTURE_SCALE)
    dx, dy, response = shuttle.scene_shift(fa, fb)
    error = math.hypot(dx - want[0], dy - want[1])
    check(f"{a} -> {b}: ({dx:.1f}, {dy:.1f}) vs truth ({want[0]:.1f}, {want[1]:.1f}), "
          f"off by {error:.1f}px, response {response:.3f}",
          error <= MAX_SHIFT_ERROR and response >= shuttle.MIN_SHIFT_RESPONSE)

frame = gray_fixture("route_temple_28.png")
if frame is not None:
    dx, dy, response = shuttle.scene_shift(frame, frame.copy())
    check(f"a frame against itself is a CONFIDENT zero (the 'it did nothing' case): "
          f"({dx:.1f}, {dy:.1f}), response {response:.3f}",
          abs(dx) < 0.5 and abs(dy) < 0.5 and response >= shuttle.MIN_SHIFT_RESPONSE)

worst = 0.0
for a in HARROGATH:
    for b in TEMPLE:
        fa, fb = gray_fixture(a), gray_fixture(b)
        if fa is not None and fb is not None:
            worst = max(worst, shuttle.scene_shift(fa, fb)[2])
check(f"two different places never give a confident shift: worst response {worst:.3f} "
      f"< {shuttle.MIN_SHIFT_RESPONSE}", worst < shuttle.MIN_SHIFT_RESPONSE)
fa, fb = gray_fixture("route_harrogath_01.png"), gray_fixture("route_harrogath_10.png")
if fa is not None and fb is not None:
    response = shuttle.scene_shift(fa, fb)[2]
    check(f"the same town, too far apart to overlap: response {response:.3f} < "
          f"{shuttle.MIN_SHIFT_RESPONSE}", response < shuttle.MIN_SHIFT_RESPONSE)
if frame is not None:
    check("frames of different shapes cannot be compared -> None",
          shuttle.scene_shift(frame, frame[:-1]) is None)
    check("no frame -> None", shuttle.scene_shift(frame, None) is None)


print("2. Shuttle - where to point")
place = shuttle.Geometry((960, 524), (96, 86, 1824, 810))
s = shuttle.Shuttle(300)
check("the first leg goes out", s.aim(place) == (1260, 524))
s.landed((300, 0))
check("the second comes back to the start", s.aim(place) == (660, 524))
s.landed((-300, 0))
check("...and it is back where it began", (s.x, s.y) == (0.0, 0.0))
s.landed((150, 20)) #out, but cut short and pushed down a little
check("after a short landing, the way back aims at the START, not a fixed distance",
      s.aim(place) == (810, 504))
s.landed((-148, -18))
check("a landing within ON_TARGET_PX of the mark counts as on it (noise is not added up)",
      (s.x, s.y) == (0.0, 0.0))
s = shuttle.Shuttle(300)
s.missed()
check("a miss repeats the same leg", s.aim(place) == (1260, 524))
s.landed(None)
check("a landing it could not measure is taken as arriving where it aimed", (s.x, s.y) == (300.0, 0.0))
s = shuttle.Shuttle(2000)
check("the aim stays inside the bounds", s.aim(place) == (1824, 524))


print("3. run() through a fake game")
SCREEN_W, SCREEN_H = 1920, 1080
CHARACTER = (960, 524)
SCALE = 0.3
rng = np.random.default_rng(7)
WORLD = cv.resize((rng.random((90, 140)) * 255).astype(np.uint8), (1400, 900),
                  interpolation=cv.INTER_CUBIC)


class FakeGame:
    """A camera locked to a character standing on a textured world, and a teleport key."""

    def __init__(self, lands=1.0, cast_delay=0.3, health=0.9, mana=0.8, allowed=True, dark=False,
                 first_lands=None):
        self.t = 0.0
        self.x = self.y = 0.0 #the character, relative to the start, in screen pixels
        self.lands = lands
        self.first_lands = first_lands
        self.cast_delay = cast_delay
        self.pending = None
        self.presses = []
        self.health_value = health
        self.mana_value = mana
        self.allowed = allowed
        self.dark = dark
        self.stop = False

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.pending and self.t >= self.pending[0]:
            _, dx, dy = self.pending
            self.x += dx
            self.y += dy
            self.pending = None

    def teleport(self, x, y):
        fraction = self.first_lands if self.first_lands is not None and not self.presses else self.lands
        self.presses.append((self.t, x, y))
        self.pending = (self.t + self.cast_delay, (x - CHARACTER[0]) * fraction, (y - CHARACTER[1]) * fraction)

    def look(self):
        w, h = int(SCREEN_W * SCALE), int(SCREEN_H * SCALE)
        if self.dark:
            return np.zeros((h, w), np.uint8)
        left = int(round(400 + self.x * SCALE))
        top = int(round(300 + self.y * SCALE))
        return WORLD[top:top + h, left:left + w]

    def geometry(self):
        return place

    def run(self, **overrides):
        values = dict(distance=300, interval=2.5, seconds=60, count=6, stop_below=0.35,
                      mana_floor=0.10, scale=SCALE)
        max_misses = overrides.pop("max_misses", shuttle.MAX_MISSES_IN_A_ROW)
        values.update(overrides)
        self.log = []
        return shuttle.run(self.look, self.geometry, lambda: self.allowed,
                           lambda: self.health_value, lambda: self.mana_value, self.teleport,
                           shuttle.Settings(**values), log=self.log.append, max_misses=max_misses,
                           clock=self.clock, sleep=self.sleep, cancelled=lambda: self.stop)


game = FakeGame()
outcome = game.run()
check(f"six teleports, all seen landing: {[leg.outcome for leg in outcome.legs]}",
      outcome.reason == "count" and [leg.outcome for leg in outcome.legs] == ["landed"] * 6)
check(f"...and after an even number it is back where it started ({game.x:.0f}, {game.y:.0f})",
      math.hypot(game.x, game.y) <= 6)
moved = [round(leg.moved_px) for leg in outcome.legs]
check(f"each move measured at about the 300px aimed: {moved}", all(abs(m - 300) <= 6 for m in moved))
seen = [round(leg.land_seconds, 2) for leg in outcome.legs]
check(f"the jump is seen just after the cast delay: {seen}", all(0.3 <= t <= 0.4 for t in seen))
gaps = [b[0] - a[0] for a, b in zip(game.presses, game.presses[1:])]
check(f"teleports are spaced by the interval: {[round(g, 2) for g in gaps]}",
      all(2.5 <= g <= 2.7 for g in gaps))

game = FakeGame(first_lands=0.4)
outcome = game.run()
check(f"a landing cut short still counts as landed: {round(outcome.legs[0].moved_px)}px of 300",
      outcome.legs[0].outcome == "landed" and abs(outcome.legs[0].moved_px - 120) <= 6)
check(f"...and is corrected on the way back - ends at ({game.x:.0f}, {game.y:.0f})",
      outcome.reason == "count" and math.hypot(game.x, game.y) <= 6)

game = FakeGame(lands=0.0)
outcome = game.run()
check(f"a key that does nothing stops the run after {shuttle.MAX_MISSES_IN_A_ROW} tries "
      f"({outcome.reason}, {len(outcome.legs)} presses)",
      outcome.reason == "misses" and len(outcome.legs) == shuttle.MAX_MISSES_IN_A_ROW)
check("...and the misses are what it says stopped it",
      any("did nothing" in line for line in game.log))
game = FakeGame(lands=0.0)
outcome = game.run(max_misses=None)
check("with misses not counted (a dry run) it carries on to the count",
      outcome.reason == "count" and len(outcome.legs) == 6)

game = FakeGame(dark=True)
outcome = game.run()
check(f"frames it cannot compare are 'unknown', never misses - the run is not stopped "
      f"({outcome.reason}, {[leg.outcome for leg in outcome.legs]})",
      outcome.reason == "count" and all(leg.outcome == "unknown" for leg in outcome.legs))

game = FakeGame(health=0.30)
outcome = game.run()
check("health at or below stop_below stops it before any press",
      outcome.reason == "health" and not game.presses)
game = FakeGame(health=None)
outcome = game.run()
check("health that cannot be read (None) never stops it", outcome.reason == "count")

game = FakeGame(allowed=False)
outcome = game.run(seconds=10)
check("not focused / not in play: nothing pressed, and the run still ends on time",
      outcome.reason == "time" and not game.presses)
check("...saying once why it is waiting, not on every poll",
      sum("waiting" in line for line in game.log) == 1)

game = FakeGame(mana=0.05)
outcome = game.run(seconds=10)
check("low mana holds it off", outcome.reason == "time" and not game.presses)
game = FakeGame(mana=None)
outcome = game.run()
check("mana that cannot be read (None) does not hold it off", outcome.reason == "count")

game = FakeGame()
game.stop = True
outcome = game.run()
check("the stop key stops it before anything is pressed", outcome.reason == "cancelled" and not game.presses)

summary = shuttle.summary(FakeGame().run())
check(f"summary reads sensibly: {summary.splitlines()[0]!r}", "6 landed" in summary)


print()
print(f"{'all checks passed' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
