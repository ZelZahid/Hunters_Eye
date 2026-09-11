"""Tests for the walk from a new game in Harrogath to Nihlathak's portal (routes/pindle.py).

WHY THIS EXISTS: the walk clicks into a live game based on WHERE IT THINKS IT IS, and every way of
being wrong about that looks fine from outside until the character walks into a wall or talks to an
NPC. So the thing tested hardest is the localization - against real screenshots the map was NOT
built from, with ground truth from the registration that built it (0.3px worst disagreement).

The held-out frames are the waypoint->portal walk, taken minutes before the start->portal walk
the map was built from: NPCs elsewhere, a spell glowing on the character, and camera positions the
map never saw - including one (route_harrogath_10) whose left edge hangs off the map entirely.
They are stored at the map's own scale, which is exactly the array the live path produces after
its resize, so these numbers are bit-for-bit what runs live.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

import cv2 as cv
import numpy as np

from core import text_detection
from core.localize import MapLocator
from routes import pindle

failures = 0
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#Where each held-out screenshot's top-left sits on the map, in source pixels, from the
#registration solve - not from the thing under test.
HELD_OUT = {
    "route_harrogath_01.png": (1238.4, 979.3),
    "route_harrogath_05.png": (1180.8, 1209.7),   #the weakest match when measured: 0.758
    "route_harrogath_06.png": (1008.0, 1497.7),
    "route_harrogath_09.png": (115.2, 1742.5),
    "route_harrogath_10.png": (-144.0, 1670.5),   #hangs 144px off the map's left edge
}
MAX_ERROR_PX = 8.0   #measured worst 3.2px; a click target is ~40px across, so 8 is still exact
#The portal's ring measured directly in Screenshot009 (bounding box of its saturated red), i.e.
#independently of the map and the route file.
PORTAL_IN_SHOT_09 = (730.0, 297.5)


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def fixture(name):
    image = cv.imread(str(FIXTURES / name))
    if image is None:
        #Loud, and a FAILURE rather than a skip - see tests/fixtures/README.md for why a test that
        #skips quietly is worse than no test.
        check(f"fixture {name} exists (see tests/fixtures/README.md)", False)
    return image


print("1. The route loads and is shaped as expected")
route = pindle.Route.load()
check("assets/routes/harrogath_to_nihlathak.json loads", route is not None)
if route is None:
    sys.exit(1)
check("the path has been densified", len(route.path) > 20)
check("the portal is a named point", "portal" in route.points)
check("the waypoint is a keep-out zone", "waypoint" in route.keep_out)
check("fits the recorded 1920x1080", route.fits(1920, 1080))
check("fits the same shape at another size (2560x1440)", route.fits(2560, 1440))
check("refuses a different shape (1280x800)", not route.fits(1280, 800))

print("\n2. Nothing to go on -> 'cannot tell', never a guess and never a crash")
locator = route.locator
query_shape = (172, 288)   #the live query at map scale: 0.64 x 270 by 0.6 x 480
check("a None query -> cannot tell", locator.locate(None).found is None)
check("an empty query -> cannot tell", locator.locate(np.zeros((0, 0), np.uint8)).found is None)
check("a flat black query (a loading screen) -> cannot tell",
      locator.locate(np.zeros(query_shape, np.uint8)).found is None)
rng = np.random.default_rng(7)
check("a query bigger than the map -> cannot tell",
      locator.locate(rng.integers(0, 255, (2000, 2000), dtype=np.uint8)).found is None)
check("random noise is never located",
      locator.locate(rng.integers(0, 255, query_shape, dtype=np.uint8)).found is not True)
check("route.locate(None) -> no position", route.locate(None).position is None)
check("a black full-size BGRA frame -> no position",
      route.locate(np.zeros((1080, 1920, 4), np.uint8)).position is None)
check("a frame of the wrong shape -> no position",
      route.locate(np.zeros((800, 1280, 3), np.uint8)).position is None)
try:
    MapLocator(None)
    check("MapLocator refuses a missing map", False)
except ValueError:
    check("MapLocator refuses a missing map", True)

print("\n3. Screenshots the map was NOT built from are located where they really were")
held_scores = []
where_09 = None
for name, truth in HELD_OUT.items():
    frame = fixture(name)
    if frame is None:
        continue
    where = route.locate(frame, frame_scale=route.map_scale)
    if where.position is None:
        check(f"{name} located (score {where.fix.score}, margin {where.fix.margin})", False)
        continue
    error = float(np.linalg.norm(where.camera - np.array(truth)))
    held_scores.append(where.fix.score)
    check(f"{name}: off by {error:.1f}px (score {where.fix.score:.3f}, "
          f"margin {where.fix.margin:.3f})", error <= MAX_ERROR_PX)
    if name == "route_harrogath_09.png":
        where_09 = where

print("\n4. Anywhere else is NOT located - this is also how 'we went through the portal' is seen")
pack = fixture("pindle_pack.png")
if pack is not None:
    inside = route.locate(pack)
    check(f"Nihlathak's Temple is not Harrogath (score {inside.fix.score:.3f})",
          inside.fix.found is False)
    if held_scores:
        #The margin, not just the answer - a threshold that still separates these by a hair is a
        #threshold about to fail on the next dark corner.
        gap = min(held_scores) - inside.fix.score
        check(f"weakest Harrogath match beats the temple by {gap:.3f} (needs >= 0.30)", gap >= 0.30)
        check(f"threshold {locator.threshold} sits between them",
              inside.fix.score < locator.threshold < min(held_scores))
lobby = fixture("lobby.png")
if lobby is not None:
    check("the lobby is not located", route.locate(lobby).fix.found is not True)

print("\n5. Where the route says the portal is, is where the portal is - in a screenshot it never saw")
if where_09 is not None:
    x, y = route.to_screen(route.points["portal"], where_09.camera, (0, 0), where_09.zoom)
    miss = float(np.hypot(x - PORTAL_IN_SHOT_09[0], y - PORTAL_IN_SHOT_09[1]))
    check(f"portal click lands {miss:.0f}px from the ring's measured centre (ring is ~150x240)",
          miss <= 20)
    check("and that spot is inside the click area", route.in_click_area(route.points["portal"],
                                                                        where_09.camera))


def simulate(start, step_px=150.0, max_steps=80):
    """Walk the Walker: the character covers step_px towards each target before the next click."""
    walker = pindle.Walker(route)
    position = np.array(start, float)
    problems, progress = [], 0
    for step in range(max_steps):
        kind, target = walker.decide(position)
        if walker.progress < progress:
            problems.append(f"step {step}: progress went backwards")
        progress = walker.progress
        camera = position - route.anchor
        if kind == "portal":
            if not route.in_click_area(target, camera):
                problems.append(f"step {step}: portal chosen while off screen")
            return step, problems
        if not route.in_click_area(target, camera):
            problems.append(f"step {step}: target {target.round()} is off the click area")
        if route.in_keep_out(target):
            problems.append(f"step {step}: target {target.round()} is on the waypoint")
        offset = target - position
        distance = float(np.linalg.norm(offset))
        position = target if distance <= step_px else position + offset / distance * step_px
    return None, problems


print("\n6. The walk's decisions, simulated - no game, no mouse")
steps, problems = simulate(route.path[0])
check(f"from the start it reaches the portal ({steps} clicks)", steps is not None)
check("every click is on screen, off the HUD and off the waypoint", not problems)
for problem in problems[:5]:
    print("       ", problem)
steps, problems = simulate(route.path[0], step_px=50.0, max_steps=200)
check(f"a slow character gets there too ({steps} clicks)", steps is not None and not problems)
#Pushed off the path (an NPC in the way): it must head back rather than aim somewhere unclickable.
direction = route.path[16] - route.path[14]
sideways = np.array([direction[1], -direction[0]]) / np.linalg.norm(direction)
for push in (200.0, -200.0):
    steps, problems = simulate(route.path[15] + sideways * push)
    check(f"pushed {push:+.0f}px off the path, it still gets there ({steps} clicks)",
          steps is not None and not problems)
kind, _ = pindle.Walker(route).decide(route.path[0])
check("at the start, the portal is not in reach yet", kind == "move")

print("\n7. The portal is only clicked once its label is READ")
if not text_detection.ocr_available():
    print("  NOT RUNNING - no OCR backend installed, so the label check cannot be tested here.")
    print("  (Live, walk_to_portal() then clicks WITHOUT reading the label, and says so.)")
else:
    labelled = fixture("portal_label.png")
    plain = fixture("portal_no_label.png")
    if labelled is not None:
        check("'Nihlathak's Temple' read above the hovered portal",
              pindle.label_showing(labelled, 331, 230))
    if plain is not None:
        check("no label read when nothing is hovered", not pindle.label_showing(plain, 330, 230))
    check("a black frame has no label", not pindle.label_showing(np.zeros((360, 660, 3),
                                                                          np.uint8), 330, 230))

print("\n8. Cost of one live locate (full-size frame, including its resize) - informational")
if pack is not None:
    frame = cv.cvtColor(pack, cv.COLOR_BGR2BGRA)
    route.locate(frame)
    started = time.perf_counter()
    for _ in range(10):
        route.locate(frame)
    print(f"  {(time.perf_counter() - started) * 100:.1f} ms per locate")

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
