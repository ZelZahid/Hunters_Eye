"""Tests for the routes of the Pindleskin run (routes/pindle.py): map localization and the walks.

WHY THIS EXISTS: a walk clicks into a live game based on WHERE IT THINKS IT IS, and every way of
being wrong about that looks fine from outside until the character walks into a wall or talks to an
NPC. So the thing tested hardest is the localization - against real screenshots the maps were NOT
built from, with ground truth from the registration that built them (0.3px worst disagreement).

Harrogath's held-out frames are the waypoint->portal walk, taken minutes before the start->portal
walk that map was built from - NPCs elsewhere, a spell glowing on the character, camera positions
the map never saw, one hanging off the map's edge. The temple's held-out frames (21, 24, 28) were
left out of its map on purpose for this. All are stored at the map's own scale, which is exactly
the array the live path produces after its resize, so these numbers are bit-for-bit what runs live.

Section 4 is the regression test for the one real failure found so far: with plain matching, a
temple frame matched the HARROGATH map at 0.630, over its threshold, because both scenes are dark
and share the same bright red portal. Local contrast normalisation took it to 0.090.
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

#Where each held-out screenshot's top-left sits on its map, in source pixels, from the
#registration solve - not from the thing under test.
HARROGATH_HELD_OUT = {
    "route_harrogath_01.png": (1238.4, 979.3),
    "route_harrogath_05.png": (1180.8, 1209.7),
    "route_harrogath_06.png": (1008.0, 1497.7),
    "route_harrogath_09.png": (115.2, 1742.5),
    "route_harrogath_10.png": (-144.0, 1670.5),   #hangs 144px off the map's left edge
}
TEMPLE_HELD_OUT = {
    "route_temple_21.png": (634.0, 705.8),    #on the path, halfway between two map frames
    "route_temple_24.png": (1642.0, 0.0),     #at the doorway, Pindleskin's plate up
    "route_temple_28.png": (1584.4, 57.6),    #at the fighting spot, mid-fight
}
#The arrival IS one of the temple map's own frames (there is only one screenshot of it), so the map
#placing it proves little; the landmark placing it independently is the real test.
TEMPLE_ARRIVAL = ("route_temple_19.png", (0.0, 1282.0))
TEMPLE_FRAMES = ("route_temple_19.png", "route_temple_20.png", "route_temple_21.png",
                 "route_temple_24.png", "route_temple_28.png")
MAX_ERROR_PX = 8.0   #measured worst 3.2px; a click target is ~40px across, so 8 is still exact
#The landmark is the portal ring's bounding box, which the character partly covers at the arrival
#point - coarser than a map match, and only ever used for the first click or two.
MAX_LANDMARK_ERROR_PX = 40.0
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


def located_within(route, held_out, label):
    """Locates each held-out frame; returns their scores and a dict of name -> Where."""
    scores, wheres = [], {}
    for name, truth in held_out.items():
        frame = fixture(name)
        if frame is None:
            continue
        where = route.locate(frame, frame_scale=route.map_scale)
        wheres[name] = where
        if where.position is None:
            check(f"{label} {name} located (score {where.fix.score}, margin {where.fix.margin})",
                  False)
            continue
        error = float(np.linalg.norm(where.camera - np.array(truth)))
        scores.append(where.fix.score)
        check(f"{label} {name}: off by {error:.1f}px (score {where.fix.score:.3f}, "
              f"margin {where.fix.margin:.3f})", error <= MAX_ERROR_PX)
    return scores, wheres


print("1. The routes load and are shaped as expected")
route = pindle.Route.load(pindle.HARROGATH_ROUTE)
temple = pindle.Route.load(pindle.TEMPLE_ROUTE)
check("assets/routes/harrogath_to_nihlathak.json loads", route is not None)
check("assets/routes/nihlathak_temple.json loads", temple is not None)
if route is None or temple is None:
    sys.exit(1)
check("both are matched with local contrast normalisation",
      route.locator.normalize_sigma and temple.locator.normalize_sigma)
check("the path has been densified", len(route.path) > 20)
check("Harrogath's portal is a named point", "portal" in route.points)
check("Harrogath's waypoint is a keep-out zone", "waypoint" in route.keep_out)
check("the temple's town portal is its landmark", pindle.LANDMARK_POINT in temple.points)
check("fits the recorded 1920x1080", route.fits(1920, 1080))
check("fits the same shape at another size (2560x1440)", route.fits(2560, 1440))
check("refuses a different shape (1280x800)", not route.fits(1280, 800))

print("\n2. Nothing to go on -> 'cannot tell', never a guess and never a crash")
locator = route.locator
query_shape = (172, 288)   #the live query at map scale: 0.64 x 270 by 0.6 x 480
check("a None frame -> cannot tell", locator.locate(None).found is None)
check("an empty frame -> cannot tell", locator.locate(np.zeros((0, 0), np.uint8)).found is None)
check("a flat black frame (a loading screen) -> cannot tell",
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
check("no red portal in a black frame",
      pindle.find_red_portal(np.zeros((1080, 1920, 3), np.uint8)) is None)
try:
    MapLocator(None)
    check("MapLocator refuses a missing map", False)
except ValueError:
    check("MapLocator refuses a missing map", True)

print("\n3. Harrogath screenshots the map was NOT built from are located where they really were")
harrogath_scores, harrogath_wheres = located_within(route, HARROGATH_HELD_OUT, "Harrogath")

print("\n4. Neither place is ever mistaken for the other")
temple_on_harrogath = []
for name in TEMPLE_FRAMES:
    frame = fixture(name)
    if frame is not None:
        temple_on_harrogath.append((route.locate(frame, frame_scale=route.map_scale), name))
if temple_on_harrogath:
    worst, worst_name = max(temple_on_harrogath, key=lambda item: item[0].fix.score or 0)
    #The frame that matched at 0.630 before normalisation is route_temple_20.
    check(f"no temple frame is located on the Harrogath map (highest: {worst_name} at "
          f"{worst.fix.score:.3f}; was 0.630 before normalisation)",
          all(w.fix.found is not True for w, _ in temple_on_harrogath))
    if harrogath_scores:
        gap = min(harrogath_scores) - worst.fix.score
        check(f"the weakest Harrogath match beats the best temple look-alike by {gap:.3f} "
              f"(needs >= 0.30)", gap >= 0.30)
pack = fixture("pindle_pack.png")
if pack is not None:
    check("pindle_pack (inside the temple) is not located on Harrogath",
          route.locate(pack).fix.found is not True)
lobby = fixture("lobby.png")
if lobby is not None:
    check("the lobby is not located", route.locate(lobby).fix.found is not True)
for name in HARROGATH_HELD_OUT:
    frame = fixture(name)
    if frame is not None:
        where = temple.locate(frame, frame_scale=temple.map_scale)
        check(f"{name} is not located on the TEMPLE map ({where.fix.score:.3f})",
              where.fix.found is not True)

print("\n5. Temple screenshots the map was NOT built from are located where they really were")
temple_scores, _ = located_within(temple, TEMPLE_HELD_OUT, "temple")
if pack is not None:
    where = temple.locate(pack)
    check(f"pindle_pack.png, from a different run, is located on the temple map "
          f"({where.fix.score:.3f})", where.fix.found is True)

print("\n6. The temple's arrival point: the red portal places it, independently of the map")
frame = fixture(TEMPLE_ARRIVAL[0])
if frame is not None:
    by_map = temple.locate(frame, frame_scale=temple.map_scale)
    #Informational only - this frame is part of the map, so the map finding it is not evidence.
    print(f"       (by the map, in-sample: found={by_map.fix.found}, score {by_map.fix.score:.3f})")
    by_portal = temple.locate_by_landmark(frame, frame_scale=temple.map_scale)
    if by_portal.position is None:
        check("the red portal is found at the arrival point", False)
    else:
        error = float(np.linalg.norm(by_portal.camera - np.array(TEMPLE_ARRIVAL[1])))
        check(f"located by the portal, off by {error:.1f}px", error <= MAX_LANDMARK_ERROR_PX)
    check("Harrogath has no landmark (its portal is its GOAL, not where it starts)",
          route.locate_by_landmark(frame, frame_scale=route.map_scale).position is None)

print("\n7. Where the route says the portal is, is where the portal is - in a screenshot it never saw")
where_09 = harrogath_wheres.get("route_harrogath_09.png")
if where_09 is not None and where_09.position is not None:
    x, y = route.to_screen(route.points["portal"], where_09.camera, (0, 0), where_09.zoom)
    miss = float(np.hypot(x - PORTAL_IN_SHOT_09[0], y - PORTAL_IN_SHOT_09[1]))
    check(f"portal click lands {miss:.0f}px from the ring's measured centre (ring is ~150x240)",
          miss <= 20)
    check("and that spot is inside the click area", route.in_click_area(route.points["portal"],
                                                                        where_09.camera))


def simulate(the_route, start, goal="portal", step_px=150.0, max_steps=80):
    """Walk the Walker: the character covers step_px towards each target before the next click."""
    walker = pindle.Walker(the_route, goal)
    position = np.array(start, float)
    problems, progress = [], 0
    for step in range(max_steps):
        kind, target = walker.decide(position)
        if walker.progress < progress:
            problems.append(f"step {step}: progress went backwards")
        progress = walker.progress
        camera = position - the_route.anchor
        if kind in ("portal", "arrived"):
            if kind == "portal" and not the_route.in_click_area(target, camera):
                problems.append(f"step {step}: portal chosen while off screen")
            return step, kind, problems
        if not the_route.in_click_area(target, camera):
            problems.append(f"step {step}: target {target.round()} is off the click area")
        if the_route.in_keep_out(target):
            problems.append(f"step {step}: target {target.round()} is on the waypoint")
        offset = target - position
        distance = float(np.linalg.norm(offset))
        position = target if distance <= step_px else position + offset / distance * step_px
    return None, None, problems


print("\n8. The walks' decisions, simulated - no game, no mouse")
steps, kind, problems = simulate(route, route.path[0])
check(f"Harrogath: from the start it reaches the portal ({steps} clicks)", kind == "portal")
check("every click is on screen, off the HUD and off the waypoint", not problems)
for problem in problems[:5]:
    print("       ", problem)
steps, kind, problems = simulate(route, route.path[0], step_px=50.0, max_steps=200)
check(f"a slow character gets there too ({steps} clicks)", kind == "portal" and not problems)
direction = route.path[16] - route.path[14]
sideways = np.array([direction[1], -direction[0]]) / np.linalg.norm(direction)
for push in (200.0, -200.0):
    steps, kind, problems = simulate(route, route.path[15] + sideways * push)
    check(f"pushed {push:+.0f}px off the path, it still gets there ({steps} clicks)",
          kind == "portal" and not problems)
kind, _ = pindle.Walker(route).decide(route.path[0])
check("at the start, the portal is not in reach yet", kind == "move")
steps, kind, problems = simulate(temple, temple.path[0], goal=None)
check(f"temple: from the arrival it reaches the fighting spot and STOPS ({steps} clicks)",
      kind == "arrived" and not problems)
for problem in problems[:5]:
    print("       ", problem)
kind, _ = pindle.Walker(temple, None).decide(temple.path[0])
check("the temple walk never tries to go through a portal", kind == "move")

print("\n9. 'F4' stops a walk outright, without waiting for anything")
started = time.perf_counter()
stopped = pindle.walk(route, grab=lambda: (None, None), can_act=lambda: True,
                      in_play=lambda: None, log=lambda *_: None, cancelled=lambda: True)
elapsed = time.perf_counter() - started
#Without the cancel it would sit here for LOST_SECONDS before giving up on a blind grab, so the
#timing is the check: it has to return immediately, not eventually.
check(f"a cancelled walk returns False at once ({elapsed * 1000:.0f}ms)",
      stopped is False and elapsed < 1.0)

print("\n10. The portal is only clicked once its label is READ")
if not text_detection.ocr_available():
    print("  NOT RUNNING - no OCR backend installed, so the label check cannot be tested here.")
    print("  (Live, the walk then clicks WITHOUT reading the label, and says so.)")
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

print("\n11. Cost of one live locate (full-size frame, including its resize) - informational")
if pack is not None:
    frame = cv.cvtColor(pack, cv.COLOR_BGR2BGRA)
    route.locate(frame)
    started = time.perf_counter()
    for _ in range(10):
        route.locate(frame)
    print(f"  {(time.perf_counter() - started) * 100:.1f} ms per locate")

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
