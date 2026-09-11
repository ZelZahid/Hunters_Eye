"""Diablo II: Resurrected - the Pindleskin run.

F7 runs it end to end:

    Harrogath, where a new game starts
      -> walk to Nihlathak's red portal and through it         walk_to_portal()
      -> Conviction on (E)
      -> walk up to the temple doorway                          walk_to_fight_spot()
      -> Fist of the Heavens (R) on whatever is there, until
         nothing is left - leaving the game if health drops    fight()
      -> auto-collect (main.py's own thread) picks up the loot

HOW IT FINDS ITS WAY, and why it is not a list of clicks. legacy/pindle.py clicked three fixed
screen offsets with sleep(1) between them. That works exactly until something differs: an NPC
steps into the path, a click lands a little short, the game hitches for half a second - and then
every later click is relative to a position the character is not actually in, so the error
compounds and nothing notices. Here every step LOOKS first:

    grab the screen -> find where it sits on a map of the area (core/localize.py)
                    -> click the next point along the path that was actually walked

A bump, a short click or a slow frame all correct themselves, because each step starts from where
the character really is. And "where am I on the map" being answerable at all is itself the check
that we are where this route starts: anywhere else scores too low to be located, and the walk
refuses to start rather than clicking blind.

HOW IT FINDS SOMETHING TO HIT, without a monster detector (that is still only a plan - see
docs/monster_detection_plan.txt). Two things this program can already see do the job between them:
  - WHERE to look: standing still, the camera does not move, so anything that changes between two
    frames a tenth of a second apart is something alive - a monster, or a spell hitting one.
    Measured on the fight: the biggest moving region was the pack itself.
  - WHETHER it is a monster: Diablo II draws a red name plate at the top of the screen for the
    monster under the cursor, and for nothing else (the mercenary gets a floating name instead).
    Measured: 87-88% of the plate's middle is plate-red with a monster hovered, at most 2% without.
So the cursor visits the moving spots, and R is pressed only while the plate says a monster is
under it. Aiming at Pindleskin specifically does not matter - Fist of the Heavens spreads to
everything near its target - so any plate will do.

THE MAPS, PATHS AND PLACES ARE DATA, not code - assets/routes/*.png/.json, built by
tools/build_route_map.py from screenshots of the owner doing this run. What IS Diablo II knowledge
lives here: that clicking the ground walks there, that the portal shows a name label on hover, what
the monster plate looks like, and which keys are the aura and the attack on this character.
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

ROUTES_DIR = Path(__file__).resolve().parent.parent / "assets" / "routes"
HARROGATH_ROUTE = ROUTES_DIR / "harrogath_to_nihlathak.json"
TEMPLE_ROUTE = ROUTES_DIR / "nihlathak_temple.json"
ROUTE_FILE = HARROGATH_ROUTE   #the first route, and Route.load()'s default

#--- The character's keys. Diablo II's "quick cast" is on: pressing a skill key casts it at the
#cursor, rather than just assigning it to a mouse button. Character-specific, so if they start
#differing between characters they belong in user_config.txt, not in more constants here.
AURA_KEY = "e"   #Conviction: -resistances on everything nearby. Once, on arrival.
CAST_KEY = "r"   #Fist of the Heavens, cast at whatever monster is under the cursor.

#--- Walking. Distances are in SOURCE pixels (the resolution a route was recorded at). ----------
#The recorded paths are a few points ~400px apart; this densifies them so there is always a point a
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
#Keep-out zones in a route file (Harrogath's waypoint) are grown by this much. Clicking the ground
#right beside the waypoint platform can land on the platform itself, and that opens the waypoint
#menu - which then swallows every click after it.
KEEP_OUT_MARGIN_PX = 40
CLICK_INTERVAL_SECONDS = 0.45
POLL_SECONDS = 0.1
#A walk with no portal to go through is over once the character is this close to the path's end.
ARRIVE_PX = 60

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

#--- The red portal as a LANDMARK: a second, independent way to place the character at the temple's
#arrival point. The map does place it (0.865) - but that is measured on the one screenshot of the
#arrival there is, which is part of the map, and apart from the portal the view is almost pure
#black. The portal itself is the one saturated, bright red ring in the scene, so finding IT says
#where we are, and walk() falls back to it whenever the map cannot answer. (An earlier map could
#NOT place the arrival. That was first blamed on the darkness; it was a mis-stitched map - see
#Error_history.txt #46 - and this landmark is what exposed it.) A route with a point by this name
#gets the fallback; one without does not.
LANDMARK_POINT = "town_portal"
#The ring's colour, measured on six frames: hue within a few degrees of red (either side of the
#wrap at 0), strongly saturated, bright. Torches are orange (hue 10-25) and fail the hue test.
PORTAL_RING_HSV = ((165, 3), 90, 200)    #(hue low, hue high across the wrap), min S, min V
#At full resolution the ring measured 3,357-9,080 pixels; as a fraction of the frame this holds at
#any scale.
PORTAL_MIN_AREA_FRACTION = 0.001
HUD_TOP_FRACTION = 0.79   #the health orb is red too - never look below here

#--- Fighting. ---------------------------------------------------------------------------------
#The monster name plate's red middle, and the line its name is written on, as fractions of the
#client area. Measured at 1920x1080: the red spans x 826-1094 for "Defiled Warrior" and 868-1030
#for "Pindleskin", centred on the screen, at y 30-66; x 900-1020 is inside both.
PLATE_RED_REGION = (0.46875, 0.0315, 0.0625, 0.026)
PLATE_NAME_REGION = (0.3646, 0.0278, 0.2708, 0.0333)
PLATE_RED_HSV = ((168, 12), 80, 40)
#Measured 0.87-0.88 with a plate up and at most 0.02 without, so the middle is safe either way.
PLATE_RED_MIN = 0.5
#What the plate says, uppercased with spaces and punctuation removed. Read exactly by OCR in every
#screenshot ("PINDLESKIN", "DEFILED WARRIOR"). Used to REPORT whether Pindleskin was seen - any red
#plate is worth casting at, since Fist of the Heavens spreads to everything near its target.
PINDLESKIN_WORD = "PINDLESKIN"

#Where to look for movement: the world part of the frame, minus the character's own box (it moves
#with every cast). Fractions of the client area.
MOTION_REGION = (0.0, 0.10, 1.0, 0.69)
CHARACTER_BOX = (0.4635, 0.346, 0.073, 0.185)
#Half resolution is plenty to see a monster move and is a quarter of the work. The threshold and
#minimum size are what separate a monster (measured: the pack was one region of ~31,000 full-size
#pixels, a single warrior ~600-1,300) from falling snow (a few pixels) and torch flicker.
MOTION_SCALE = 0.5
MOTION_THRESHOLD = 25
MOTION_MIN_AREA_PX = 600      #in full-size pixels
MOTION_GAP_SECONDS = 0.12
MAX_CANDIDATES = 6
#After moving the cursor, how long before the plate is expected on screen: a frame or two at 60 FPS.
HOVER_SETTLE_SECONDS = 0.06
CAST_INTERVAL_SECONDS = 0.3
#Casting at one hovered spot this long without a break means re-scan anyway - the pack moves.
MAX_ON_TARGET_SECONDS = 6.0
#Done when this many scans in a row find nothing to hit AND nothing has been hit for this long.
QUIET_SCANS = 4
QUIET_SECONDS = 4.0
FIGHT_TIMEOUT_SECONDS = 90.0
#Leave the game (Save and Exit) at or below this much health. The emergency potion fires at 30%
#(user_config.txt), so this sits below it: the potion gets its chance first. Chosen by the program,
#not yet by the owner - change it here.
CHICKEN_BELOW = 0.20


def _never():
    """The default "has this been cancelled?" - nothing here cancels unless a caller says so.

    STOPPING IS NOT PAUSING, and the two are deliberately different. A pause (`can_act` going
    false) suspends an attempt and resumes it; a cancel ends the run and does not resume, because
    whoever pressed the key has stopped watching the screen the run was acting on. main.py's F4
    does both at once to different things: it snoozes auto-collect, which re-arms itself, and
    cancels the run, which does not.
    """
    return False


Where = namedtuple("Where", "fix position camera zoom")
#fix      - the MapLocator result (for a landmark fix: found=True with no score)
#position - the character's position on the map, or None if not located
#camera   - where the frame's top-left sits on the map, or None
#zoom     - screen pixels per source pixel for this game window (1.0 at the recorded resolution)


def _densify(points, spacing):
    dense = [points[0]]
    for a, b in zip(points, points[1:]):
        steps = max(1, int(np.ceil(np.linalg.norm(b - a) / spacing)))
        dense.extend(a + (b - a) * (k / steps) for k in range(1, steps + 1))
    return np.array(dense)


def _region(frame, fractions):
    h, w = frame.shape[:2]
    x, y, rw, rh = fractions
    return frame[int(y * h):int((y + rh) * h), int(x * w):int((x + rw) * w)]


def _bgr(image):
    if image.ndim == 3 and image.shape[2] == 4:
        return cv.cvtColor(image, cv.COLOR_BGRA2BGR)
    return image


def _hue_mask(bgr, hsv_spec):
    (low, high), min_s, min_v = hsv_spec
    hsv = cv.cvtColor(bgr, cv.COLOR_BGR2HSV)
    hue = hsv[..., 0]
    return ((hue >= low) | (hue <= high)) & (hsv[..., 1] >= min_s) & (hsv[..., 2] >= min_v)


def find_red_portal(frame):
    """Centre of the biggest bright red ring in `frame`, in its own pixels, or None."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    mask = _hue_mask(_bgr(frame), PORTAL_RING_HSV).astype(np.uint8)
    mask[int(HUD_TOP_FRACTION * h):] = 0
    count, _labels, stats, _centres = cv.connectedComponentsWithStats(mask)
    if count < 2:
        return None
    biggest = 1 + int(np.argmax(stats[1:, cv.CC_STAT_AREA]))
    x, y, bw, bh, area = stats[biggest]
    if area < PORTAL_MIN_AREA_FRACTION * h * w:
        return None
    #The bounding box's centre is the hole's centre - the ring's own pixels average to the same
    #place only when none of it is hidden, and the character usually stands in front of it.
    return x + bw / 2.0, y + bh / 2.0


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
                                  meta.get("min_margin", 0.2), meta.get("normalize_sigma"))

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
        Diablo II; walk() says so when it happens.
        """
        return abs(width / height - self.source_size[0] / self.source_size[1]) < 0.02

    def _window(self, frame, frame_scale):
        """(zoom) for this frame, or None if the window's shape does not fit the route."""
        if frame is None or frame.size == 0:
            return None
        window_w = frame.shape[1] / frame_scale
        window_h = frame.shape[0] / frame_scale
        if not self.fits(window_w, window_h):
            return None
        return window_w / self.source_size[0]

    def locate(self, frame, frame_scale=1.0):
        """Where the character is, from one frame of the game's client area. Returns a Where.

        frame_scale is this frame's pixels per pixel of the game window: 1.0 for a live capture.
        Tests pass frames already stored at the map's scale, which then skip the resize entirely -
        so what the test measures is bit-for-bit what the live path computes.
        """
        none = Where(Fix(None, None, None, None, None), None, None, None)
        zoom = self._window(frame, frame_scale)
        if zoom is None:
            return none

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
        fix = self.locator.locate(frame, (x0, y0, int(vw * w), int(vh * h)))
        if not fix.found:
            return Where(fix, None, None, zoom)
        camera = np.array([fix.x - x0, fix.y - y0], float) / self.map_scale
        return Where(fix, camera + self.anchor, camera, zoom)

    def locate_by_landmark(self, frame, frame_scale=1.0):
        """Where the character is, from the red portal alone - see LANDMARK_POINT. Returns a Where
        (position None when this route has no landmark or the ring is not on screen)."""
        none = Where(Fix(None, None, None, None, None), None, None, None)
        point = self.points.get(LANDMARK_POINT)
        zoom = self._window(frame, frame_scale)
        if point is None or zoom is None:
            return none
        ring = find_red_portal(frame)
        if ring is None:
            return none
        camera = point - np.array(ring, float) / frame_scale / zoom
        return Where(Fix(True, None, None, None, None), camera + self.anchor, camera, zoom)

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
    through simulated walks.

    goal: the name of a point to go THROUGH (the Harrogath route's "portal"), or None to walk to
    the end of the path and stop there (the temple route's fighting spot).
    """

    def __init__(self, route, goal="portal"):
        self.route = route
        self.goal = goal
        self.progress = 0   #index into route.path of the furthest point reached so far

    def decide(self, position):
        """('portal', point) once the goal is in reach; ('arrived', point) at the end of a path
        with no goal; otherwise ('move', point)."""
        route = self.route
        position = np.asarray(position, float)
        camera = position - route.anchor

        #Progress only ever moves FORWARD. Nearest-point-overall would let a character bumped
        #sideways snap back to an earlier stretch of path and walk it again.
        ahead = route.path[self.progress:]
        self.progress += int(np.argmin(np.linalg.norm(ahead - position, axis=1)))

        portal = route.points.get(self.goal) if self.goal else None
        if (portal is not None and np.linalg.norm(portal - position) <= PORTAL_REACH_PX
                and route.in_click_area(portal, camera)):
            return "portal", portal
        if self.goal is None and np.linalg.norm(route.path[-1] - position) <= ARRIVE_PX:
            return "arrived", route.path[-1]

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
    for text, _box in text_detection.read_lines(_bgr(frame[y0:y1, x0:x1])):
        squashed = "".join(ch for ch in text.upper() if ch.isalnum())
        if any(word in squashed for word in PORTAL_LABEL_WORDS):
            return True
    return False


def _enter_portal(route, portal, grab, can_act, in_play, log, cancelled=_never):
    """Hover the portal, read its label, click it, and confirm we left the map. True if we did.

    The portal's screen position is recomputed from a fresh frame on every poll, because the
    character is usually still running when this starts and the camera moves with it - a position
    measured once would be stale by the time the label had a chance to appear.
    """
    verify = text_detection.ocr_available()
    if not verify:
        log("walk: no OCR available - clicking the portal WITHOUT reading its label first.")

    deadline = time.monotonic() + LABEL_WAIT_SECONDS
    while True:
        if cancelled():
            log("walk: stopped before clicking the portal.")
            return False
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

    #Confirm by looking: in a game (HUD up) but no longer on this map. A loading screen is
    #neither, so it just keeps waiting. This is only trustworthy because the maps are matched with
    #local contrast normalisation - with plain matching, the far side's frames scored up to 0.630
    #against this map, over its threshold, because both sides show the same bright portal.
    deadline = time.monotonic() + ENTER_TIMEOUT_SECONDS
    off_since = None
    while time.monotonic() < deadline:
        if cancelled():
            #The click has already gone in, so we may or may not be through - say so rather than
            #reporting either one. The caller stops either way.
            log("walk: stopped while waiting to see whether we went through.")
            return False
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
    log("walk: clicked the portal but still on the same map.")
    return False


def walk(route, grab, can_act, in_play, goal="portal", log=print, cancelled=_never):
    """Walks a route. True once through its goal portal (goal given) or at its end (goal None).

    grab()    -> (frame, (x, y)): a full-resolution frame of the game's client area and the screen
                 position of its top-left, or (None, None) when there is none to be had.
    can_act() -> bool: the pipeline's safety guards (focused, in play). Checked before every click.
    in_play() -> True/False/None: whether the game HUD is on screen.

    Stops and says why - never carries on blind - when it cannot locate itself, stops making
    progress, or runs out of time.
    """
    walker = Walker(route, goal)
    active = 0.0          #time spent actually walking; pauses do not count against the timeout
    paused = 0.0
    last_click = -1e9
    last_located = time.monotonic()
    moved_from, moved_at = None, time.monotonic()
    portal_attempts = 0
    announced = False

    while active < WALK_TIMEOUT_SECONDS:
        tick = time.monotonic()
        if cancelled():
            log("walk: stopped.")
            return False
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
        if frame is not None and (where is None or where.position is None):
            where = route.locate_by_landmark(frame)
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
                log(f"walk: cannot find where I am on the map (best match {score}) - "
                    f"{'not started' if not announced else 'stopping'}.")
                return False
            time.sleep(POLL_SECONDS)
            active += time.monotonic() - tick
            continue

        last_located = now
        if not announced:
            announced = True
            how = ("by the portal" if where.fix.score is None
                   else f"match {where.fix.score:.2f}")
            log(f"walk: located on the map ({how}).")
            if abs(where.zoom - 1.0) > 1e-3:
                log(f"walk: NOTE the game is at {where.zoom:.2f}x the recorded resolution - "
                    f"scaling is assumed, not yet verified.")

        if moved_from is None or np.linalg.norm(where.position - moved_from) > STUCK_DISTANCE_PX:
            moved_from, moved_at = where.position, now
        elif now - moved_at > STUCK_SECONDS:
            log(f"walk: no progress for {STUCK_SECONDS:.0f}s - something is in the way. Stopping.")
            return False

        kind, point = walker.decide(where.position)
        if kind == "arrived":
            log("walk: arrived.")
            return True
        if kind == "portal":
            if _enter_portal(route, point, grab, can_act, in_play, log, cancelled):
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

    log(f"walk: did not get there within {WALK_TIMEOUT_SECONDS:.0f}s - stopping.")
    return False


def walk_to_portal(grab, can_act, in_play, route=None, log=print, cancelled=_never):
    """From the start of a new game in Harrogath, through Nihlathak's portal. True on success."""
    route = route or Route.load(HARROGATH_ROUTE)
    return route is not None and walk(route, grab, can_act, in_play, "portal", log, cancelled)


def walk_to_fight_spot(grab, can_act, in_play, route=None, log=print, cancelled=_never):
    """From the temple's arrival point up to the doorway the owner fights from. True on arrival."""
    route = route or Route.load(TEMPLE_ROUTE)
    return route is not None and walk(route, grab, can_act, in_play, None, log, cancelled)


#--- Fighting ----------------------------------------------------------------------------------
def plate_red_fraction(frame):
    """How much of the monster name plate's middle is plate-red (0-1)."""
    crop = _region(frame, PLATE_RED_REGION)
    return float(_hue_mask(_bgr(crop), PLATE_RED_HSV).mean()) if crop.size else 0.0


def plate_showing(frame):
    """Whether a monster's name plate is up, i.e. whether there is a monster under the cursor."""
    return frame is not None and plate_red_fraction(frame) >= PLATE_RED_MIN


def plate_name(frame):
    """The hovered monster's name, uppercased with only letters kept ('DEFILEDWARRIOR'), or ''."""
    if frame is None:
        return ""
    crop = _region(frame, PLATE_NAME_REGION)
    if crop.size == 0:
        return ""
    text = " ".join(t for t, _box in text_detection.read_lines(_bgr(crop)))
    return "".join(ch for ch in text.upper() if ch.isalpha())


def motion_candidates(frame_a, frame_b, frame_scale=1.0):
    """Where something moved between two frames from a STILL camera, biggest first.

    Returns [(x, y), ...] in the frames' own pixels. Only meaningful while the character stands
    still - a moving camera makes the whole scene "move". The character's own box is left out: it
    moves with every cast.
    """
    if frame_a is None or frame_b is None or frame_a.shape != frame_b.shape:
        return []
    factor = MOTION_SCALE / frame_scale
    grays = []
    for frame in (frame_a, frame_b):
        if abs(factor - 1.0) > 1e-6:
            frame = cv.resize(frame, None, fx=factor, fy=factor, interpolation=cv.INTER_AREA)
        gray = cv.cvtColor(frame, cv.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv.COLOR_BGR2GRAY)
        grays.append(cv.GaussianBlur(gray, (5, 5), 0))
    h, w = grays[0].shape
    keep = np.zeros((h, w), np.uint8)
    mx, my, mw, mh = MOTION_REGION
    keep[int(my * h):int((my + mh) * h), int(mx * w):int((mx + mw) * w)] = 255
    cx, cy, cw, ch = CHARACTER_BOX
    keep[int(cy * h):int((cy + ch) * h), int(cx * w):int((cx + cw) * w)] = 0

    _, moved = cv.threshold(cv.absdiff(grays[0], grays[1]), MOTION_THRESHOLD, 255, cv.THRESH_BINARY)
    moved = cv.bitwise_and(moved, keep)
    moved = cv.morphologyEx(moved, cv.MORPH_OPEN, np.ones((3, 3), np.uint8))   #drops falling snow
    moved = cv.dilate(moved, np.ones((9, 9), np.uint8))   #joins one monster's limbs into one region
    count, _labels, stats, centres = cv.connectedComponentsWithStats(moved)
    min_area = MOTION_MIN_AREA_PX * factor * factor * frame_scale * frame_scale
    found = [(stats[k, cv.CC_STAT_AREA], centres[k]) for k in range(1, count)
             if stats[k, cv.CC_STAT_AREA] >= min_area]
    found.sort(key=lambda item: -item[0])
    return [(float(c[0] / factor), float(c[1] / factor)) for _area, c in found]


def fight(grab, hover, cast, can_act, health, bail, log=print, candidates=motion_candidates,
          clock=time.monotonic, sleep=time.sleep, cancelled=_never):
    """Casts at whatever monsters are in view until none are left. Returns what happened:
    'done', 'chickened' (left the game on low health), 'cancelled', 'timeout' or 'stopped'.

    grab() -> (frame, (x, y)) as for walk();  hover(x, y) moves the cursor;  cast() presses the
    attack key;  can_act() -> bool;  health() -> 0-1 or None;  bail() leaves the game.
    `candidates`, `clock` and `sleep` exist so tests can drive this without a game or real time.

    ONE THING IS CHECKED BEFORE EVERY PRESS OF THE ATTACK KEY: that the plate is up, i.e. that a
    monster is under the cursor right now. Monsters move; a spot that had one a moment ago may
    be bare floor, and a cast at bare floor is a wasted cast at best.
    """
    started = clock()
    last_hit = started
    paused = 0.0
    quiet = 0
    casts = 0
    saw_pindleskin = False

    def too_hurt():
        level = health()
        #None means the orb could not be read, NOT that it is empty (see game_state.py) - it must
        #never trigger leaving. The potion layer keeps working either way.
        if level is not None and level <= CHICKEN_BELOW:
            log(f"fight: health {level:.0%} - leaving the game.")
            bail()
            return True
        return False

    while clock() - started < FIGHT_TIMEOUT_SECONDS:
        if cancelled():
            log(f"fight: stopped after {casts} casts.")
            return "cancelled"
        if not can_act():
            if paused >= actions.PAUSE_BUDGET_SECONDS:
                log("fight: paused too long - stopping.")
                return "stopped"
            sleep(POLL_SECONDS)
            paused += POLL_SECONDS
            continue
        if too_hurt():
            return "chickened"

        first, origin = grab()
        if first is None:
            sleep(POLL_SECONDS)
            continue
        first = first.copy()   #grab() hands back a view that the next grab() overwrites
        sleep(MOTION_GAP_SECONDS)
        second, origin = grab()
        spots = candidates(first, second)

        engaged = False
        for x, y in spots[:MAX_CANDIDATES]:
            if cancelled():
                log(f"fight: stopped after {casts} casts.")
                return "cancelled"
            if not can_act():
                break
            hover(int(origin[0] + x), int(origin[1] + y))
            sleep(HOVER_SETTLE_SECONDS)
            frame, _ = grab()
            if not plate_showing(frame):
                continue

            name = plate_name(frame)
            if PINDLESKIN_WORD in name and not saw_pindleskin:
                saw_pindleskin = True
                log("fight: Pindleskin is here.")
            engaged = True
            on_target = clock()
            while clock() - on_target < MAX_ON_TARGET_SECONDS:
                if cancelled():
                    log(f"fight: stopped after {casts} casts.")
                    return "cancelled"
                if not can_act():
                    break
                #RETURN, not break. too_hurt() has already left the game; breaking out would go
                #round the outer loop, see the same low health, and call bail() a SECOND time -
                #pressing Esc into whatever the first exit left on screen. Caught by
                #test_pindle_fight.py before it ever ran live.
                if too_hurt():
                    return "chickened"
                cast()
                casts += 1
                sleep(CAST_INTERVAL_SECONDS)
                frame, _ = grab()
                if not plate_showing(frame):
                    break
            last_hit = clock()
            break   #re-scan: the pack has moved while we were casting

        if engaged:
            quiet = 0
            continue
        quiet += 1
        if quiet >= QUIET_SCANS and clock() - last_hit >= QUIET_SECONDS:
            log(f"fight: nothing left to hit - {casts} casts"
                f"{', Pindleskin was seen' if saw_pindleskin else ', Pindleskin was never hovered'}.")
            return "done"

    log(f"fight: still going after {FIGHT_TIMEOUT_SECONDS:.0f}s - stopping.")
    return "timeout"


def fight_here(grab, can_act, health, bail, log=print, cancelled=_never):
    """Just the fight, from wherever the character is standing."""
    return fight(grab, actions.move_to, lambda: actions.press_key(CAST_KEY), can_act, health,
                 bail, log, cancelled=cancelled)


def run(grab, can_act, in_play, health, bail, log=print, cancelled=_never):
    """The whole run: portal, Conviction, fighting spot, fight. True if it got through the fight."""
    if not walk_to_portal(grab, can_act, in_play, log=log, cancelled=cancelled):
        return False
    if cancelled():
        log("run: stopped.")
        return False
    if can_act():
        actions.press_key(AURA_KEY)
        log("run: Conviction on.")
    if not walk_to_fight_spot(grab, can_act, in_play, log=log, cancelled=cancelled):
        return False
    return fight_here(grab, can_act, health, bail, log, cancelled) == "done"
