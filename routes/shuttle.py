"""Teleport back and forth over the same spot, on its own - the "bot test mode".

    out to one side -> back to where it started -> out again -> back again -> ...

WHY THIS EXISTS: measuring how long item labels take to reappear after the view jumps
(tools/measure_ocr_latency.py) needs the view to jump, over and over, next to items - and until now
that meant a person sitting at the keyboard teleporting by hand for minutes at a time. This does the
teleporting so a timing test can run with nobody there. tools/teleport_bot.py is the entry point;
this module is the loop itself, with every sensor and the keypress handed in as plain callables so
tests/test_shuttle.py can drive it through a fake game (the routes/pindle.py fight() pattern).

EVERY TELEPORT IS CHECKED BY LOOKING, not assumed. The camera is locked to the character, so a
teleport that happened moves the whole scene by about as far as was travelled, and one that did not
(no mana, the key bound to something else, standing in town, aimed at a wall) moves nothing. How far
the scene moved between a frame from just before the key and frames just after it is measured with
phase correlation (scene_shift() below), which answers "how far did this picture slide" directly -
no feature matching, no thresholds on brightness. That one measurement does three jobs:

  - SAFETY. A key that measurably did nothing, several times in a row, means something is wrong that
    nobody is there to see, so the run stops and says what to check (MAX_MISSES_IN_A_ROW).
  - STAYING PUT. A teleport can land short (blocked ground) and the next one would then overshoot the
    start; over a few minutes that walks the character off the items. Each aim is computed from
    where the character really ended up, so a short landing corrects itself on the next leg - the
    same reason walk_to_portal() replays a path from where the character IS rather than from where
    it should be.
  - TIMING. When the scene was first seen to move after the key, which tools/teleport_bot.py logs
    so a latency measurement can be lined up against the moment the view really jumped.

"CANNOT TELL" IS A THIRD ANSWER HERE TOO. A frame pair too different to compare (a loading screen,
a menu, the window gone) gives no shift rather than a zero shift - and only a CONFIDENT zero counts
as a miss. Collapsing the two would stop a perfectly good run every time a frame came back dark.

Nothing here knows about Diablo II beyond the shape of the problem - "a key moves the camera-locked
character to where the mouse points" - which equally describes any top-down game with a blink.
"""
from __future__ import annotations

import math
import time
from collections import namedtuple

import cv2 as cv
import numpy as np

#The part of the client area compared between frames: the gameplay viewport, minus the HUD. Same
#margins main.py crops OCR to (VIEWPORT_TOP_MARGIN / VIEWPORT_BOTTOM_MARGIN) - restated rather than
#imported because routes/ never imports main. The HUD is drawn on the SCREEN, not in the world, so it
#is identical before and after a teleport and votes for "nothing moved" (EH#46 is this exact trap
#in the route-map stitcher).
VIEW_TOP = 0.08
VIEW_BOTTOM = 0.25
#The character is NOT blanked out, though it also sits still on screen while the world slides past.
#Tried, and measured worse: a blanked box is an identical flat patch in both frames, with sharp edges,
#so it votes for "nothing moved" far louder than the character itself ever did (different-place
#pairs rose from 0.123 to 0.131, and fake-game landings started reading as "cannot tell"). The
#character is small next to the viewport; the real pairs below include it and measure to 0.2px.

#Below this phase-correlation response, the two frames are not alike enough to say how far one slid
#against the other - the answer is "cannot tell", not a shift. MEASURED on the committed route
#frames at 0.25x: genuine pairs scored 0.327-0.595 (the weakest is 72px apart vertically out of 181
#rows compared; another is mid-fight with monsters moving), while all 25 pairings of a Harrogath frame
#with a temple frame scored at most 0.123 (two dark scenes, a large wrong shift) and two frames of
#the same town too far apart to overlap 0.061. 0.2 sits ~1.6x clear of both. tests/test_shuttle.py
#re-measures both sides, so a change that erodes either margin fails there rather than in a game.
MIN_SHIFT_RESPONSE = 0.2

#A teleport counts as LANDED as soon as the scene has moved at least this fraction of the distance
#aimed at - the moment to stop watching and log the time. Half is slack for a landing cut short by
#terrain or the game's own range cap - the same number and reasoning as main.py's
#TELEPORT_MOVED_FRACTION. A move that never gets this far is still a landing (a short one), judged
#once LAND_TIMEOUT_SECONDS is up from the last confident measurement.
LANDED_FRACTION = 0.5
#...and as MISSED only if, by then, the scene has CONFIDENTLY moved less than this fraction.
MISSED_FRACTION = 0.15
#A measured landing within this many screen pixels of where it was aimed is taken as exactly where it
#was aimed. Each measurement carries a pixel or two of noise, and adding every one of them up leg after
#leg is a random walk - in a fake game with perfect landings it drifted the character 15px in six
#teleports. Only a real miss of the mark is worth correcting; the same idea as overlay.py's
#BOX_MOVE_DEADBAND_PX.
ON_TARGET_PX = 12
#How long after the key to keep looking for the jump. A teleport is instant once cast, but the cast
#itself takes a while and that is gear (faster cast rate), so this is generous rather than tight.
LAND_TIMEOUT_SECONDS = 1.5
LOOK_SECONDS = 0.05
#This many confident misses in a row and the run stops. One miss is ordinary (out of mana for a
#moment, a cast lost to being hit); four in a row is a key that is not doing what we think.
MAX_MISSES_IN_A_ROW = 4

#Between teleports: how often to look at health, the guards and the stop key.
POLL_SECONDS = 0.1


Settings = namedtuple("Settings", "distance interval seconds count stop_below mana_floor scale")
Settings.__doc__ = """How the run behaves.

    distance    how far out each leg goes, in screen pixels
    interval    seconds from one teleport to the next - long enough for OCR to find the labels
                again in between, or the latency measurement has nothing to measure
    seconds     the whole run is bounded by this, pauses included (a wait that does not count
                against a limit is an unbounded wait - EH#41)
    count       stop after this many teleports (None = only the time limit)
    stop_below  stop the run once health reads at or below this (a reading of None never does)
    mana_floor  hold off teleporting while mana reads at or below this (None never holds off)
    scale       look() frames are this fraction of real screen pixels
"""

Geometry = namedtuple("Geometry", "character bounds")
Geometry.__doc__ = """Where things are on screen right now, in real screen pixels.

    character   (x, y) - where the character is drawn
    bounds      (left, top, right, bottom) - where it is safe to point: inside the game, off the HUD
"""

Leg = namedtuple("Leg", "number pressed_at aim_x aim_y aimed_px outcome moved_px land_seconds")
Leg.__doc__ = """One teleport, for the log. outcome is 'landed', 'missed' or 'unknown'; moved_px and
land_seconds are None when it could not be measured."""

Outcome = namedtuple("Outcome", "reason legs")


def _never():
    return False


def _prepare(gray):
    """The viewport band of a greyscale frame, as float32 for phaseCorrelate."""
    h = gray.shape[0]
    return gray[int(h * VIEW_TOP):int(h * (1 - VIEW_BOTTOM)), :].astype(np.float32)


def scene_shift(before, after):
    """How far the scene slid from `before` to `after`: (dx, dy, response) in the frames' own pixels,
    or None when the two cannot be compared at all (different shapes, or too small to mean anything).

    The camera is locked to the character, so the scene slides the OPPOSITE way to the character:
    a teleport to the right comes back as a negative dx. The caller flips it.

    A low response means "cannot tell" - see MIN_SHIFT_RESPONSE. This function reports it rather than
    deciding, so a caller can tell a confident zero (the teleport did not happen) from a meaningless
    one (the frames have nothing in common).

    Phase correlation finds the translation directly from the two images' Fourier transforms, and
    normalises away brightness in doing so, which is why a dark catacomb and a lit town both work. It
    assumes the picture moved by pure translation - measured true for this camera before
    routes/pindle.py was built on the same assumption (scale 1.000 +/- 0.0015, rotation within
    +/-0.07 degrees). A game that zooms while moving would need something else.
    """
    if before is None or after is None or before.shape != after.shape:
        return None
    if before.shape[0] < 40 or before.shape[1] < 40:
        return None
    (dx, dy), response = cv.phaseCorrelate(_prepare(before), _prepare(after))
    return float(dx), float(dy), float(response)


class Shuttle:
    """Where to point next so the character keeps crossing the same stretch of ground.

    Kept PURE - no clock, no screen, no keys - so tests can walk it through short landings, misses
    and frames it could not read. Positions are relative to where the character started, in screen
    pixels (which, with the camera locked to the character, are also world distances).
    """

    def __init__(self, distance):
        self.distance = float(distance)
        self.x = 0.0 #where the character is now, relative to the start
        self.y = 0.0
        self.outbound = True #the next leg goes out (True) or back to the start (False)

    def target(self):
        return (self.distance, 0.0) if self.outbound else (0.0, 0.0)

    def aim(self, geometry):
        """(x, y) on screen to point at for the next leg, kept inside geometry.bounds."""
        tx, ty = self.target()
        cx, cy = geometry.character
        left, top, right, bottom = geometry.bounds
        x = min(max(cx + tx - self.x, left), right)
        y = min(max(cy + ty - self.y, top), bottom)
        return int(round(x)), int(round(y))

    def landed(self, moved):
        """The teleport happened. `moved` is how far the character went as (dx, dy) screen pixels,
        or None when it happened but could not be measured - then it is taken to have arrived where
        it was aimed, which is the best guess available and what every unmeasured landing does."""
        tx, ty = self.target()
        if moved is None:
            self.x, self.y = tx, ty
            self.outbound = not self.outbound
            return
        x, y = self.x + moved[0], self.y + moved[1]
        if math.hypot(x - tx, y - ty) <= ON_TARGET_PX:
            x, y = tx, ty #measurement noise, not a missed mark - see ON_TARGET_PX
        self.x, self.y = x, y
        self.outbound = not self.outbound

    def missed(self):
        """Nothing moved, so the next attempt is the same leg again."""


def _watch_landing(look, before, aimed_px, scale, clock, sleep):
    """After the key: ('landed'|'missed'|'unknown', moved (dx, dy) in screen px or None, seconds
    from the key to the scene first being seen to move, or None)."""
    started = clock()
    last = None #the latest CONFIDENT measurement of how far the character went
    first_moved = None
    while True:
        sleep(LOOK_SECONDS)
        now = look()
        elapsed = clock() - started
        shift = scene_shift(before, now)
        if shift is not None and shift[2] >= MIN_SHIFT_RESPONSE:
            #The scene moves the opposite way to the character, and look() frames are `scale` of
            #real pixels - so flip and scale back to get how far the character went on screen.
            last = (-shift[0] / scale, -shift[1] / scale)
            distance = math.hypot(*last)
            if distance > MISSED_FRACTION * aimed_px and first_moved is None:
                first_moved = elapsed
            if distance >= LANDED_FRACTION * aimed_px:
                return "landed", last, elapsed
        if elapsed >= LAND_TIMEOUT_SECONDS:
            if last is None:
                return "unknown", None, None
            if math.hypot(*last) <= MISSED_FRACTION * aimed_px:
                return "missed", None, None
            return "landed", last, first_moved #a short landing - blocked ground, or the range cap


def run(look, geometry, can_act, health, mana, teleport, settings, *, log=print, on_leg=None,
        max_misses=MAX_MISSES_IN_A_ROW, clock=time.monotonic, sleep=time.sleep, cancelled=_never):
    """Teleports out and back until the time or the count runs out, or something says stop.

    look() -> a greyscale frame of the game's client area at settings.scale, or None. It is ALSO
        the call that refreshes every other sensor (tools/teleport_bot.py grabs one screenshot per
        call and reads health, mana and the guards off it), so it is called on every poll.
    geometry() -> Geometry or None (the game window cannot be found)
    can_act() -> bool;  health() / mana() -> 0-1 or None;  teleport(x, y) points there and presses
    on_leg(Leg) is told about every teleport, for a log file.
    max_misses: confident misses in a row before stopping; None never stops for that (a dry run,
        where nothing is pressed and so nothing can move).
    `clock`, `sleep` and `cancelled` exist so tests can drive this without a game or real time.

    Returns Outcome(reason, legs): reason is 'time', 'count', 'cancelled', 'health' or 'misses'.
    """
    shuttle = Shuttle(settings.distance)
    started = clock()
    deadline = started + settings.seconds
    legs = []
    misses = 0
    next_press = started #the first teleport goes as soon as everything allows it
    waiting_for = None #what the last pause was about, so it is logged once rather than every poll

    def wait(reason):
        nonlocal waiting_for
        if reason != waiting_for:
            log(f"shuttle: waiting - {reason}")
            waiting_for = reason
        sleep(POLL_SECONDS)

    while True:
        if cancelled():
            log("shuttle: stopped by the stop key.")
            return Outcome("cancelled", legs)
        if clock() >= deadline:
            return Outcome("time", legs)
        if settings.count is not None and len(legs) >= settings.count:
            return Outcome("count", legs)

        before = look()
        level = health()
        #None is "cannot read", never "empty" (see game_state.py) - it must not end the run.
        if level is not None and level <= settings.stop_below:
            log(f"shuttle: health {level:.0%} - stopping. Nobody is at the keyboard to deal with "
                f"whatever is hitting you.")
            return Outcome("health", legs)
        if clock() < next_press:
            sleep(POLL_SECONDS)
            continue
        if not can_act():
            wait("the game is not focused, or not in play")
            continue
        level = mana()
        if level is not None and settings.mana_floor is not None and level <= settings.mana_floor:
            wait(f"mana {level:.0%}")
            continue
        place = geometry()
        if place is None or before is None:
            wait("cannot see the game window")
            continue
        if waiting_for is not None:
            log("shuttle: going again.")
            waiting_for = None

        aim_x, aim_y = shuttle.aim(place)
        aimed_px = math.hypot(aim_x - place.character[0], aim_y - place.character[1])
        if aimed_px < 1.0:
            #Already standing where this leg goes (only possible if the bounds squashed it).
            shuttle.landed((0.0, 0.0))
            sleep(POLL_SECONDS)
            continue
        direction = "out" if shuttle.outbound else "back"
        teleport(aim_x, aim_y)
        pressed_at = clock()
        next_press = pressed_at + settings.interval
        outcome, moved, took = _watch_landing(look, before, aimed_px, settings.scale, clock, sleep)

        if outcome == "missed":
            shuttle.missed()
            misses += 1
        else:
            shuttle.landed(moved)
            #Only a SEEN landing proves the key works. "Could not tell" leaves the count alone.
            if outcome == "landed":
                misses = 0
        leg = Leg(len(legs) + 1, pressed_at, aim_x, aim_y, aimed_px, outcome,
                  None if moved is None else math.hypot(*moved), took)
        legs.append(leg)
        if on_leg is not None:
            on_leg(leg)
        detail = (f"moved {leg.moved_px:.0f}px, first seen moving {took:.2f}s after the key"
                  if outcome == "landed"
                  else "nothing moved" if outcome == "missed" else "could not tell whether it moved")
        log(f"shuttle: teleport {leg.number} ({direction} {aimed_px:.0f}px) - {outcome}: {detail}")
        if max_misses is not None and misses >= max_misses:
            log(f"shuttle: {misses} teleports in a row did nothing - stopping. Check the key is bound "
                f"to Teleport, there is mana, and you are not standing in town.")
            return Outcome("misses", legs)


def summary(outcome):
    """A few lines describing a finished run."""
    legs = outcome.legs
    landed = [leg for leg in legs if leg.outcome == "landed"]
    lines = [f"{len(legs)} teleports: {len(landed)} landed, "
             f"{sum(leg.outcome == 'missed' for leg in legs)} did nothing, "
             f"{sum(leg.outcome == 'unknown' for leg in legs)} could not be measured. "
             f"Ended by: {outcome.reason}."]
    if landed:
        times = sorted(leg.land_seconds for leg in landed)
        lines.append(f"key -> scene moved: median {times[len(times) // 2]:.2f}s "
                     f"(min {times[0]:.2f}, max {times[-1]:.2f})")
    return "\n".join(lines)
