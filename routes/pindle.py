"""Diablo II: Resurrected - the Pindleskin run. So far: the walk from a new game to the red portal.

WHAT EXISTS: walk_to_portal() takes the character from where a new game drops it in Harrogath to
Nihlathak's Temple portal, and through it. Killing Pindleskin and collecting are the next legs.

HOW IT FINDS ITS WAY, and why it is not a list of clicks. legacy/pindle.py clicked three fixed
screen offsets with sleep(1) between them. That works exactly until something differs: an NPC
steps into the path, a click lands a little short, the game hitches for half a second - and then
every later click is relative to a position the character is not actually in, so the error
compounds and nothing notices. Here every step LOOKS first:

    grab the screen -> find where it sits on a map of Harrogath (core/localize.py)
                    -> click the next point along the path that was actually walked

A bump, a short click or a slow frame all correct themselves, because each step starts from where
the character really is. And "where am I on the map" being answerable at all is itself the check
that we are in Harrogath at the expected place: anywhere else scores too low to be located, and
the walk refuses to start rather than clicking blind.

THE MAP AND PATH ARE DATA, not code - assets/routes/harrogath_to_nihlathak.png/.json, built by
tools/build_route_map.py from screenshots of the owner walking this exact route. Nothing here knows
what Harrogath looks like; a different route is a different pair of files. What IS Diablo II
knowledge lives here: that clicking the ground walks there, that clicking the portal enters it,
and that the portal shows a name label when the cursor is over it.

EVERYTHING IS CHECKED BY LOOKING, including the two steps where a wrong click matters:
  - the portal is clicked only after its hover label ("Nihlathak's Temple") is READ on screen -
    something else standing in front of it (a guard, a townsperson) shows a different label, and
    clicking that would start a conversation instead of a run;
  - entering it is confirmed by the map no longer matching while the game HUD is still on screen,
    i.e. we are in a game but no longer in Harrogath - not by assuming a click worked.
"""
from __future__ import annotations

import json
import time
from collections import namedtuple
from pathlib import Path

import cv2 as cv
import numpy as np

from core import actions
from core import text_detection
from core.localize import Fix, MapLocator

ROUTE_FILE = (Path(__file__).resolve().parent.parent / "assets" / "routes"
              / "harrogath_to_nihlathak.json")

#--- Tuning. Distances are in SOURCE pixels (the resolution the route was recorded at). ---------
#The recorded path is only 8 points ~400px apart; this densifies it so there is always a point a
#sensible distance ahead of the character, rather than aiming straight at a point 400px away that
#may be off screen or behind a wall.
PATH_SPACING_PX = 40
#How far ahead along the path each click aims. Far enough that the character is still running
#towards it when the next click lands (a click every CLICK_INTERVAL_SECONDS), close enough that it
#is on screen and the game walks there in a straight line.
LOOKAHEAD_PX = 350
#Where on the game's client area a click may land, as fractions (x, y, w, h). Keeps clicks off the
#HUD - the life/mana text starts at ~81% down, the orbs and skill bar below it - and off the
#portrait and clock at the top, where a click either does nothing or clicks a button.
CLICK_AREA = (0.05, 0.13, 0.90, 0.63)
#Keep-out zones in the route file (the waypoint) are grown by this much. Clicking the ground right
#beside the waypoint platform can land on the platform itself, and that opens the waypoint menu -
#which then swallows every click after it.
KEEP_OUT_MARGIN_PX = 40
CLICK_INTERVAL_SECONDS = 0.45
POLL_SECONDS = 0.1

#Within this distance of the portal, with the portal inside CLICK_AREA, stop walking the path and
#go for the portal. Measured on the recording: the last-but-one screenshot stood 394px from it.
PORTAL_REACH_PX = 550
PORTAL_ATTEMPTS = 3
#Where the hover label appears relative to the portal's centre: measured ~50px above it in two
#screenshots, ~256px wide. Generous, because OCR needs a quiet margin round the text (see
#CLAUDE.md on Tesseract clipping glyphs at a crop edge) and the camera may still be settling.
LABEL_REGION = (-260, -170, 520, 210)   #x, y, w, h relative to the portal centre
#OCR reads the label's stylised font imperfectly - measured: "NIHLATHAK's TeEmMPLe" and
#"lll s Temple" - so either word on its own is enough.
PORTAL_LABEL_WORDS = ("NIHLATHAK", "TEMPLE")
LABEL_WAIT_SECONDS = 1.0
#Entering is confirmed by the map no longer matching for this long while the HUD is up. Long enough
#that one bad frame (a spell flash) cannot fake it.
OFF_MAP_CONFIRM_SECONDS = 1.0
ENTER_TIMEOUT_SECONDS = 12.0

#No progress for this long means something is in the way that re-clicking will not fix.
STUCK_SECONDS = 4.0
STUCK_DISTANCE_PX = 40
#Not located on the map for this long means we are not where this route starts (or no longer are).
LOST_SECONDS = 3.0
WALK_TIMEOUT_SECONDS = 45.0

#Diablo II: Resurrected has a "Force Move" key binding (Options -> Controls). It walks to the cursor
#WITHOUT interacting with whatever is under it - so an NPC who wanders under a click is walked past
#instead of spoken to. If you bind it, put the key here (e.g. "f8"); None means an ordinary left
#click, which works but can start a conversation with an NPC in the way.
MOVE_KEY = None


Where = namedtuple("Where", "fix position camera zoom")
#fix      - the MapLocator result
#position - the character's position on the map, or None if not located
#camera   - where the frame's top-left sits on the map, or None
#zoom     - screen pixels per source pixel for this game window (1.0 at the recorded resolution)


def _densify(points, spacing):
    dense = [points[0]]
    for a, b in zip(points, points[1:]):
        steps = max(1, int(np.ceil(np.linalg.norm(b - a) / spacing)))
        dense.extend(a + (b - a) * (k / steps) for k in range(1, steps + 1))
    return np.array(dense)


class Route:
    """A map, a path on it, and the conversions between map, frame and screen."""

    def __init__(self, meta, map_gray):
        self.map_scale = float(meta["map_scale"])
        self.source_size = np.array(meta["source_size"], float)
        self.query_view = tuple(meta["query_view"])
        self.anchor = np.array(meta["character_anchor"], float) * self.source_size
        self.path = _densify([np.array(p, float) for p in meta["path"]], PATH_SPACING_PX)
        self.points = {k: np.array(v, float) for k, v in meta.get("points", {}).items()}
        self.keep_out = {k: tuple(v) for k, v in meta.get("keep_out", {}).items()}
        self.locator = MapLocator(map_gray, meta.get("threshold", 0.55),
                                  meta.get("min_margin", 0.2))

    @classmethod
    def load(cls, path=ROUTE_FILE):
        """The route, or None if its files are missing or unusable. Never raises - a missing route
        means "this run is not available", and the program has to keep running without it."""
        path = Path(path)
        try:
            with open(path, encoding="utf-8") as handle:
                meta = json.load(handle)
            map_gray = cv.imread(str(path.parent / meta["map"]), cv.IMREAD_GRAYSCALE)
            if map_gray is None:
                raise ValueError(f"map image {meta['map']} could not be read")
            return cls(meta, map_gray)
        except FileNotFoundError:
            print(f"route: {path} not found - run tools/build_route_map.py to record it.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"route: {path} is unusable ({exc}).")
        return None

    def fits(self, width, height):
        """Whether a game window of this size can use this route.

        Same aspect ratio as the recording only. A game re-lays out its view at a different ASPECT
        (it shows more or less world at the sides), so the map simply does not describe what is on
        screen - the same reason meters.json needs one profile per window shape. At the same aspect
        and a different size the view is assumed to scale uniformly, which is NOT yet verified on
        Diablo II; walk_to_portal() says so when it happens.
        """
        return abs(width / height - self.source_size[0] / self.source_size[1]) < 0.02

    def locate(self, frame, frame_scale=1.0):
        """Where the character is, from one frame of the game's client area. Returns a Where.

        frame_scale is this frame's pixels per pixel of the game window: 1.0 for a live capture.
        Tests pass frames already stored at the map's scale, which then skip the resize entirely -
        so what the test measures is bit-for-bit what the live path computes.
        """
        none = Where(Fix(None, None, None, None, None), None, None, None)
        if frame is None or frame.size == 0:
            return none
        window_w = frame.shape[1] / frame_scale
        window_h = frame.shape[0] / frame_scale
        if not self.fits(window_w, window_h):
            return none
        zoom = window_w / self.source_size[0]

        factor = self.map_scale / (frame_scale * zoom)
        if abs(factor - 1.0) > 1e-6:
            #Downscale BEFORE converting to grey - fewer pixels to convert, identical result.
            frame = cv.resize(frame, None, fx=factor, fy=factor, interpolation=cv.INTER_AREA)
        if frame.ndim == 3:
            frame = cv.cvtColor(frame, cv.COLOR_BGRA2GRAY if frame.shape[2] == 4
                                else cv.COLOR_BGR2GRAY)

        #Must be the same arithmetic tools/build_route_map.py used, or every position is off by a
        #rounding error.
        h, w = frame.shape
        vx, vy, vw, vh = self.query_view
        x0, y0 = int(vx * w), int(vy * h)
        fix = self.locator.locate(frame[y0:y0 + int(vh * h), x0:x0 + int(vw * w)])
        if not fix.found:
            return Where(fix, None, None, zoom)
        camera = np.array([fix.x - x0, fix.y - y0], float) / self.map_scale
        return Where(fix, camera + self.anchor, camera, zoom)

    def to_screen(self, point, camera, origin, zoom):
        """Screen pixel for a map point, given where the game window is and what it shows."""
        x, y = np.asarray(origin, float) + (np.asarray(point, float) - camera) * zoom
        return int(round(x)), int(round(y))

    def in_click_area(self, point, camera):
        fx, fy = (np.asarray(point, float) - camera) / self.source_size
        ax, ay, aw, ah = CLICK_AREA
        return ax <= fx <= ax + aw and ay <= fy <= ay + ah

    def in_keep_out(self, point):
        x, y = point
        m = KEEP_OUT_MARGIN_PX
        return any(x0 - m <= x <= x1 + m and y0 - m <= y <= y1 + m
                   for x0, y0, x1, y1 in self.keep_out.values())


class Walker:
    """Decides where to click next, given where the character is. No clock, no mouse, no screen.

    Kept pure for the reason main.py's _potion_due() is: what it decides is "click into a live
    game", which is not something to validate by playing and hoping. tests/test_route.py drives it
    through a simulated walk.
    """

    def __init__(self, route):
        self.route = route
        self.progress = 0   #index into route.path of the furthest point reached so far

    def decide(self, position):
        """('portal', point) once the portal is in reach, otherwise ('move', point)."""
        route = self.route
        position = np.asarray(position, float)
        camera = position - route.anchor

        #Progress only ever moves FORWARD. Nearest-point-overall would let a character bumped
        #sideways snap back to an earlier stretch of path and walk it again.
        ahead = route.path[self.progress:]
        self.progress += int(np.argmin(np.linalg.norm(ahead - position, axis=1)))

        portal = route.points.get("portal")
        if (portal is not None and np.linalg.norm(portal - position) <= PORTAL_REACH_PX
                and route.in_click_area(portal, camera)):
            return "portal", portal

        #The furthest point along the path that is within reach, on screen and clickable.
        target = None
        for point in route.path[self.progress:]:
            if np.linalg.norm(point - position) > LOOKAHEAD_PX:
                break
            if route.in_click_area(point, camera) and not route.in_keep_out(point):
                target = point
        if target is None:
            #Nothing along the path is clickable from here - the character has been pushed off it.
            #Head back towards the next path point, pulled in until the click lands on screen.
            goal = route.path[min(self.progress + 1, len(route.path) - 1)]
            for t in np.linspace(1.0, 0.1, 10):
                point = position + (goal - position) * t
                if route.in_click_area(point, camera) and not route.in_keep_out(point):
                    target = point
                    break
            else:
                target = goal
        return "move", target


def _move_toward(x, y):
    if MOVE_KEY:
        actions.move_to(x, y)
        time.sleep(actions.MOVE_SETTLE_SECONDS)
        actions.press_key(MOVE_KEY)
    else:
        actions.click_at(x, y)


def label_showing(frame, x, y, zoom=1.0):
    """Whether the portal's name label is on screen above (x, y), in `frame`'s own pixels."""
    rx, ry, rw, rh = (v * zoom for v in LABEL_REGION)
    h, w = frame.shape[:2]
    x0, y0 = max(0, int(x + rx)), max(0, int(y + ry))
    x1, y1 = min(w, int(x + rx + rw)), min(h, int(y + ry + rh))
    if x1 <= x0 or y1 <= y0:
        return False
    crop = frame[y0:y1, x0:x1]
    if crop.ndim == 3 and crop.shape[2] == 4:
        crop = cv.cvtColor(crop, cv.COLOR_BGRA2BGR)
    for text, _box in text_detection.read_lines(crop):
        squashed = "".join(ch for ch in text.upper() if ch.isalnum())
        if any(word in squashed for word in PORTAL_LABEL_WORDS):
            return True
    return False


def _enter_portal(route, portal, grab, can_act, in_play, log):
    """Hover the portal, read its label, click it, and confirm we left Harrogath. True if we did.

    The portal's screen position is recomputed from a fresh frame on every poll, because the
    character is usually still running when this starts and the camera moves with it - a position
    measured once would be stale by the time the label had a chance to appear.
    """
    verify = text_detection.ocr_available()
    if not verify:
        log("walk: no OCR available - clicking the portal WITHOUT reading its label first.")

    deadline = time.monotonic() + LABEL_WAIT_SECONDS
    while True:
        frame, origin = grab()
        where = route.locate(frame) if frame is not None else None
        if where is not None and where.position is not None:
            x, y = route.to_screen(portal, where.camera, origin, where.zoom)
            if not verify or label_showing(frame, x - origin[0], y - origin[1], where.zoom):
                if not can_act():
                    return False
                actions.click_at(x, y)
                log("walk: clicked the portal.")
                break
            if not can_act():
                return False
            actions.move_to(x, y)   #hover, so the label is there on a later frame
        if time.monotonic() > deadline:
            log("walk: hovered the portal but never read its label - not clicking. (Something "
                "may be standing in front of it.)")
            return False
        time.sleep(POLL_SECONDS)

    #Confirm by looking: in a game (HUD up) but no longer on the Harrogath map. A loading screen
    #is neither, so it just keeps waiting.
    deadline = time.monotonic() + ENTER_TIMEOUT_SECONDS
    off_since = None
    while time.monotonic() < deadline:
        frame, _origin = grab()
        on_map = frame is not None and route.locate(frame).fix.found is True
        now = time.monotonic()
        if on_map:
            off_since = None
        else:
            off_since = off_since or now
            if now - off_since >= OFF_MAP_CONFIRM_SECONDS and in_play() is not False:
                log("walk: through the portal.")
                return True
        time.sleep(POLL_SECONDS)
    log("walk: clicked the portal but still in Harrogath.")
    return False


def walk_to_portal(grab, can_act, in_play, route=None, log=print):
    """Walks from the start of a new game in Harrogath through Nihlathak's portal. True on success.

    grab()    -> (frame, (x, y)): a full-resolution frame of the game's client area and the screen
                 position of its top-left, or (None, None) when there is none to be had.
    can_act() -> bool: the pipeline's safety guards (focused, in play). Checked before every click.
    in_play() -> True/False/None: whether the game HUD is on screen.

    Stops and says why - never carries on blind - when it cannot locate itself, stops making
    progress, or runs out of time. Stopping is always safe here: the character is standing in town.
    """
    route = route or Route.load()
    if route is None:
        return False
    walker = Walker(route)

    active = 0.0          #time spent actually walking; pauses do not count against the timeout
    paused = 0.0
    last_click = -1e9
    last_located = time.monotonic()
    moved_from, moved_at = None, time.monotonic()
    portal_attempts = 0
    announced = False

    while active < WALK_TIMEOUT_SECONDS:
        tick = time.monotonic()
        if not can_act():
            if paused == 0.0:
                log("walk: paused - the game is not focused or not in play.")
            if paused >= actions.PAUSE_BUDGET_SECONDS:
                log("walk: paused too long - stopping.")
                return False
            time.sleep(POLL_SECONDS)
            paused += POLL_SECONDS
            last_located = moved_at = time.monotonic()   #a pause is not being lost or stuck
            continue

        frame, origin = grab()
        where = route.locate(frame) if frame is not None else None
        now = time.monotonic()
        if where is None or where.position is None:
            if frame is not None and not announced and not route.fits(frame.shape[1],
                                                                       frame.shape[0]):
                log(f"walk: this route was recorded at {int(route.source_size[0])}x"
                    f"{int(route.source_size[1])}; the game is {frame.shape[1]}x{frame.shape[0]}, "
                    f"a different shape. Re-record the route in this mode.")
                return False
            if now - last_located > LOST_SECONDS:
                score = None if where is None else where.fix.score
                log(f"walk: cannot find where I am on the Harrogath map (best match {score}) - "
                    f"{'not started' if not announced else 'stopping'}. Start from where a new "
                    f"game puts you in Harrogath.")
                return False
            time.sleep(POLL_SECONDS)
            active += time.monotonic() - tick
            continue

        last_located = now
        if not announced:
            announced = True
            log(f"walk: located on the map (match {where.fix.score:.2f}), heading for the portal.")
            if abs(where.zoom - 1.0) > 1e-3:
                log(f"walk: NOTE the game is at {where.zoom:.2f}x the recorded resolution - "
                    f"scaling is assumed, not yet verified.")

        if moved_from is None or np.linalg.norm(where.position - moved_from) > STUCK_DISTANCE_PX:
            moved_from, moved_at = where.position, now
        elif now - moved_at > STUCK_SECONDS:
            log(f"walk: no progress for {STUCK_SECONDS:.0f}s - something is in the way. Stopping.")
            return False

        kind, point = walker.decide(where.position)
        if kind == "portal":
            if _enter_portal(route, point, grab, can_act, in_play, log):
                return True
            portal_attempts += 1
            if portal_attempts >= PORTAL_ATTEMPTS:
                log("walk: could not get through the portal - stopping.")
                return False
            moved_at = time.monotonic()   #standing still while hovering is not being stuck
        elif now - last_click >= CLICK_INTERVAL_SECONDS:
            _move_toward(*route.to_screen(point, where.camera, origin, where.zoom))
            last_click = time.monotonic()

        time.sleep(POLL_SECONDS)
        active += time.monotonic() - tick

    log(f"walk: did not reach the portal within {WALK_TIMEOUT_SECONDS:.0f}s - stopping.")
    return False
