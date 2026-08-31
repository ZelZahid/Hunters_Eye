"""
Game-state reading: turns a fixed on-screen HUD element into a number.

This is a third instance of the "detector" seam described in CLAUDE.md, alongside
image-template matching and OCR - but a deliberately different shape of answer. The other
two detectors answer "where is this thing?" (a box + a center point). This one answers
"how full is this thing?" (a single 0.0-1.0 reading), which is what a decision layer
actually needs to act on: drink a potion below 40%, disengage below 25%, and so on.

It knows nothing about Diablo II. A "meter" is just a rectangular screen region, a color to
look for inside it, and a direction it fills from - which describes a health orb, a mana bar,
a stamina strip, a robot's battery gauge, or a progress bar equally well. All of the
game-specific knowledge (where the orbs actually are on screen, what color they are) lives
in a JSON config file, not in code - same philosophy as assets/targets.txt.

WHY ROW-SCANNING AND NOT A PIXEL COUNT: the obvious implementation is "count matching pixels,
divide by total pixels". That is wrong for anything that is not a perfect rectangle. Diablo
II's health orb is a circle, and a circle filled to 25% of its HEIGHT is only about 19.5% full
BY AREA - the bottom of a circle is narrow, so those rows hold fewer pixels than average. (The
two measures happen to agree at exactly 50%, by symmetry, which makes this a nasty bug to spot:
a naive pixel count looks perfectly correct right up until your health is low, which is
precisely when the number matters.) Games fill a globe by height, so this measures the height
of the liquid surface directly and never converts through area at all.

COST: a few tenths of a millisecond per meter - an HSV convert plus a threshold over a region
maybe 50x50 pixels at CAPTURE_SCALE. That is cheap enough to run on every frame of the fast
detection path without measurably touching FPS, which is why main.py reads it from the frame
that path already captured instead of grabbing its own (a second mss capture would cost
~17-20ms, i.e. ~100x more than the actual measurement).
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass

import cv2 as cv
import numpy as np


# A row counts as liquid if at least this fraction of the meter's pixels ON THAT ROW match the
# meter's color. Not 100%, because the game draws a gloss/highlight streak and a darkened rim
# over the liquid, so a genuinely-full row still has non-matching pixels scattered through it.
ROW_FILL_RATIO = 0.45
# Rows at the very top and bottom of an ellipse are only a few pixels wide, so a couple of stray
# matching pixels there can read as a "full" row. Rows narrower than this fraction of the widest
# row are ignored entirely, and the fill percentage is measured only across the rows that remain.
MIN_ROW_WIDTH_RATIO = 0.25
# The liquid surface is found as the highest row that is itself filled AND has at least this
# fraction of the rows below it filled too. See _fill_fraction() for why this global test beats
# the more obvious "walk up from the bottom until you hit an empty row" approach.
SURFACE_CONSISTENCY = 0.7
# Two window shapes count as the same layout within this much aspect-ratio difference,
# so one profile serves 1280x800 and 1600x1000 but not 1280x800 and 1920x1080.
ASPECT_TOLERANCE = 0.02

_shape_mask_cache = {}  # (shape, height, width) -> mask. Rebuilding an ellipse mask every frame
                        # is cheap but pointless: a meter's size never changes at a fixed resolution.


@dataclass(frozen=True)
class Meter:
    """One readable HUD element.

    region: (x, y, w, h) as FRACTIONS of the frame, not pixels. Stored this way so the same
        config works against the fast path's downscaled frame, a full-resolution frame, or a
        different screen resolution, without anyone having to rescale coordinates by hand.
    hsv_ranges: list of (lo, hi) HSV bounds, OR-ed together. It is a list rather than a single
        range because red wraps around the hue circle - red needs one range near H=0 and
        another near H=179, and there is no way to express that as one range.
    shape: "ellipse" (a globe/orb, measured only inside the inscribed ellipse so the square
        region's corners - which contain UI frame art, not liquid - are excluded) or "rect"
        (a plain bar).
    fill_from: which edge the meter fills from as its value rises: bottom/top/left/right.
    """
    name: str
    region: tuple
    hsv_ranges: tuple
    shape: str = "ellipse"
    fill_from: str = "bottom"


def _shape_mask(shape, height, width):
    key = (shape, height, width)
    cached = _shape_mask_cache.get(key)
    if cached is not None:
        return cached

    if shape == "ellipse":
        mask = np.zeros((height, width), dtype=np.uint8)
        cv.ellipse(mask, (width // 2, height // 2), (width // 2, height // 2), 0, 0, 360, 255, -1)
        mask = mask > 0
    else:
        mask = np.ones((height, width), dtype=bool)

    _shape_mask_cache[key] = mask
    return mask


def _color_mask(roi, hsv_ranges):
    """Boolean mask of pixels matching any of the meter's HSV ranges."""
    hsv = cv.cvtColor(roi, cv.COLOR_BGR2HSV)
    combined = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for (lo, hi) in hsv_ranges:
        combined |= cv.inRange(hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
    return combined > 0


def _orient(array, fill_from):
    """Rotates/flips an array so whichever edge the meter fills from ends up at the BOTTOM.
    Lets the single bottom-up row scan below handle all four fill directions without four
    near-identical copies of the scanning logic."""
    if fill_from == "bottom":
        return array
    if fill_from == "top":
        return np.flipud(array)
    if fill_from == "left":
        return np.flipud(array.T)  # columns become rows; original left edge ends up at the bottom
    if fill_from == "right":
        return array.T             # columns become rows; original right edge is already at the bottom
    raise ValueError(f"unknown fill_from {fill_from!r} (expected bottom/top/left/right)")


def _fill_fraction(color_mask, shape_mask):
    """Height of the filled portion as a fraction of the meter's measurable height.

    Returns None if the region has no usable rows at all (a degenerate/off-screen region),
    which callers must treat as "unknown", not as zero - reporting 0% health for a region that
    simply failed to read would be an extremely bad thing to act on.
    """
    row_widths = shape_mask.sum(axis=1)
    if row_widths.max(initial=0) == 0:
        return None

    # Ignore the sliver rows at the ends of an ellipse, then measure the percentage only over
    # the band of rows that are actually wide enough to judge.
    usable = row_widths >= row_widths.max() * MIN_ROW_WIDTH_RATIO
    usable_rows = np.flatnonzero(usable)
    if usable_rows.size == 0:
        return None
    top, bottom = usable_rows[0], usable_rows[-1]

    matched_per_row = (color_mask & shape_mask).sum(axis=1)
    filled = matched_per_row >= row_widths * ROW_FILL_RATIO
    filled &= usable

    band = filled[top:bottom + 1]
    if not band.any():
        return 0.0  # no liquid anywhere - the meter is empty

    # Find the liquid surface: the HIGHEST row that is itself filled and has at least
    # SURFACE_CONSISTENCY of the rows below it filled as well.
    #
    # The obvious alternative - walk up from the bottom and stop at the first empty row (or the
    # first few) - was tried and measurably fails. Games draw a bright specular highlight across
    # the globe, and where that streak crosses the liquid it blanks out a run of rows entirely.
    # A local walk stops dead at that streak and reports the streak's height as the fill level:
    # in synthetic testing a 65%-full orb read as 25%, which is both badly wrong and wrong in the
    # dangerous direction (it would trigger an emergency potion at full health, or - with the
    # streak above the liquid - miss a real emergency). Widening the tolerated gap just moves the
    # failure somewhere else, because the streak's thickness is not a fixed number of rows.
    #
    # Judging each candidate surface against ALL the rows below it is immune to that: a blocked
    # run of rows in the middle of the liquid barely moves the ratio. Requiring the surface row
    # itself to be filled keeps the answer pinned to a real row of liquid instead of drifting up
    # into the empty space above it, and the same test rejects stray color matches floating above
    # an empty meter (a badly-tuned hsv_range picking up nearby UI art), since those have almost
    # nothing filled beneath them.
    height = len(band)
    filled_below = np.cumsum(band[::-1])[::-1]          # filled_below[i] = filled rows in band[i:]
    rows_below = np.arange(height, 0, -1)               # rows_below[i]   = total rows in band[i:]
    consistent = band & (filled_below >= rows_below * SURFACE_CONSISTENCY)

    candidates = np.flatnonzero(consistent)
    if candidates.size == 0:
        return 0.0
    surface = candidates[0]

    return (height - surface) / height


def read_meter(frame, meter, return_debug=False):
    """Reads one meter from a BGR frame. Returns a 0.0-1.0 fill fraction, or None if the region
    could not be read at all. With return_debug=True, returns (value, roi, mask) so a
    calibration/preview tool can show exactly which pixels were counted."""
    frame_h, frame_w = frame.shape[:2]
    rx, ry, rw, rh = meter.region
    x0 = max(0, min(frame_w, int(rx * frame_w)))
    y0 = max(0, min(frame_h, int(ry * frame_h)))
    x1 = max(0, min(frame_w, int((rx + rw) * frame_w)))
    y1 = max(0, min(frame_h, int((ry + rh) * frame_h)))

    if x1 - x0 < 2 or y1 - y0 < 2:
        return (None, None, None) if return_debug else None

    roi = frame[y0:y1, x0:x1]
    color = _color_mask(roi, meter.hsv_ranges)
    shape = _shape_mask(meter.shape, roi.shape[0], roi.shape[1])
    value = _fill_fraction(_orient(color, meter.fill_from), _orient(shape, meter.fill_from))

    if return_debug:
        return value, roi, (color & shape)
    return value


def read_all(frame, meters):
    """Reads every meter from one frame. Returns {name: value-or-None}."""
    return {meter.name: read_meter(frame, meter) for meter in meters}


#How many CONSECUTIVE failed reads a meter may carry on its last good value before the Smoother
#gives up and reports None. A dropped frame and a screen we are no longer looking at both arrive
#as None, and they need opposite treatment - see Smoother's docstring. At ~50 FPS this is a
#little under a third of a second: far longer than any single-frame glitch, far shorter than the
#reaction time of anything acting on the result.
MAX_CARRIED_FAILURES = 15


class Smoother:
    """Median of the last N readings, per meter, with a limit on how long a stale value carries.

    Median rather than a running average on purpose: the failure mode here is a single frame
    reading wildly wrong (a tooltip drawn over the orb on mouse hover, a spell effect flashing
    the same color across the HUD, one torn/dropped capture), and an average absorbs part of a
    bad sample into the result while a median discards it outright. The cost is a few frames of
    lag, which at 40+ FPS is well under a tenth of a second - irrelevant next to how long a
    potion takes to drink.

    A FAILED READ CARRIES THE LAST GOOD VALUE, BUT NOT FOREVER, AND THE DIFFERENCE IS THE WHOLE
    POINT. Two very different things both arrive here as None:

      - one frame failed to read (torn capture, a tooltip over the orb). Reporting None for that
        single frame would make a healthy meter flicker to "unknown" constantly, and a consumer
        that stops acting on every dropped frame is useless. Carrying the last good value is
        right.
      - we are no longer looking at the thing at all (alt-tabbed, window closed, camera
        unplugged). Here the last good value is a frozen number that LOOKS live, and carrying it
        is exactly the confusion this module exists to prevent - worse than useless, because a
        consumer acts on it with full confidence. Reporting None is right.

    Nothing in a single reading distinguishes them; only how LONG it persists does. So the carry
    is capped at MAX_CARRIED_FAILURES consecutive failures, after which the meter reports None
    until a real reading arrives. Note that a caller publishing on every frame gives a consumer
    no other way to notice: a staleness timestamp stays fresh, because the pipeline is still
    running and still publishing - it is the READING that went stale, not the publishing.
    """

    def __init__(self, window=5, max_carried_failures=MAX_CARRIED_FAILURES):
        self._window = window
        self._max_carried_failures = max_carried_failures
        self._samples = {}
        self._failures = {}  # consecutive failed reads per meter, reset by any good one

    def update(self, readings):
        smoothed = {}
        for name, value in readings.items():
            if value is None:
                # Do not feed unknowns into the history - a failed read should not drag the
                # reported value toward zero. Carry the last good median instead, but only for
                # a bounded number of frames (see the docstring).
                failures = self._failures.get(name, 0) + 1
                self._failures[name] = failures
                history = self._samples.get(name)
                if not history or failures > self._max_carried_failures:
                    #Drop the history too: once we have admitted we do not know, a single good
                    #frame arriving later must not be median-blended with values measured before
                    #a gap of unknown length. Whatever happened during that gap is not something
                    #a median over stale samples can represent.
                    self._samples.pop(name, None)
                    smoothed[name] = None
                else:
                    smoothed[name] = float(np.median(history))
                continue
            self._failures[name] = 0
            history = self._samples.setdefault(name, deque(maxlen=self._window))
            history.append(value)
            smoothed[name] = float(np.median(history))
        return smoothed


@dataclass(frozen=True)
class Profile:
    """One calibration: a set of meters, plus what their regions are measured against.

    A game lays its HUD out differently at different window shapes, so ONE set of regions cannot
    serve every resolution - measured on Diablo II, going 1920x1080 -> 1280x800 keeps the orbs'
    vertical position and height but moves them horizontally by 42px, half the width of an orb.
    Rather than making the user recalibrate on every mode switch, the config holds one profile per
    window shape and the caller picks the matching one.

    anchor: {"window_title": str, "client_size": [w, h]}, or None for an old screen-relative
        config. When present, each meter's region is fractions OF THAT WINDOW; when absent they
        are fractions of the whole captured frame, which is what versions before this wrote.
    """
    anchor: dict
    meters: tuple

    @property
    def client_size(self):
        size = (self.anchor or {}).get("client_size")
        return tuple(size) if size and len(size) == 2 else None


def _parse_meters(raw):
    meters = []
    for name, spec in raw.items():
        if name.startswith("_"):  # "_comment" keys, so the generated file can document itself
            continue
        try:
            meters.append(Meter(
                name=name,
                region=tuple(float(v) for v in spec["region"]),
                hsv_ranges=tuple(
                    (tuple(int(v) for v in lo), tuple(int(v) for v in hi))
                    for (lo, hi) in spec["hsv_ranges"]
                ),
                shape=spec.get("shape", "ellipse"),
                fill_from=spec.get("fill_from", "bottom"),
            ))
        except (KeyError, TypeError, ValueError) as error:
            print(f"WARNING: skipping malformed meter '{name}': {error}")
    return meters


def _meters_payload(meters):
    return {
        meter.name: {
            "region": list(meter.region),
            "hsv_ranges": [[list(lo), list(hi)] for (lo, hi) in meter.hsv_ranges],
            "shape": meter.shape,
            "fill_from": meter.fill_from,
        }
        for meter in meters
    }


def load_profiles(path):
    """Loads every calibration profile. Returns [] (with a warning) if the file is missing or
    malformed, rather than raising - a missing HUD config should degrade the program to "no
    game-state awareness", not stop it from running at all.

    Reads both the current multi-profile shape and the older single-profile one, so an existing
    config keeps working untouched."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        print(f"WARNING: no meter config at {path} - game-state reading disabled. "
              f"Run 'python calibrate_meters.py' to create one.")
        return []
    except (json.JSONDecodeError, OSError) as error:
        print(f"WARNING: could not read meter config {path} ({error}) - game-state reading disabled.")
        return []

    if isinstance(raw.get("profiles"), list):
        profiles = []
        for entry in raw["profiles"]:
            if not isinstance(entry, dict):
                continue
            meters = _parse_meters(entry.get("meters") or {})
            if meters:
                profiles.append(Profile(anchor=entry.get("anchor"), meters=tuple(meters)))
        return profiles

    # Legacy single-profile file: meters at the top level, anchor under "_anchor".
    meters = _parse_meters(raw)
    if not meters:
        return []
    return [Profile(anchor=raw.get("_anchor"), meters=tuple(meters))]


def save_profiles(path, profiles):
    payload = {
        "_comment": (
            "Generated by calibrate_meters.py. One profile per window shape: a game re-lays out "
            "its HUD at a different resolution or aspect ratio, so regions calibrated at one size "
            "do not apply at another. Each profile's 'region' values are (x, y, w, h) as fractions "
            "of its anchor window's client area (or of the whole captured screen if it has no "
            "anchor). Fractions keep a profile correct at any capture scale; the anchor keeps it "
            "correct when the window is moved or resized. 'hsv_ranges' are OpenCV HSV bounds "
            "(H 0-179, S 0-255, V 0-255) OR-ed together - red needs two because it wraps around "
            "H=0, and a meter that CHANGES COLOUR to signal a status effect needs one range per "
            "colour it can be."
        ),
        "profiles": [
            {"anchor": p.anchor, "meters": _meters_payload(p.meters)} for p in profiles
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def upsert_profile(profiles, new_profile):
    """Replaces the profile with the same client_size, or appends. Returns a new list.

    Recalibrating at a size you already have should REPLACE that profile, not add a second one
    for the same shape - otherwise the file accumulates stale duplicates and which one wins
    becomes a matter of ordering."""
    size = new_profile.client_size
    out = [p for p in profiles if not (size is not None and p.client_size == size)]
    if size is None:  # unanchored profiles cannot be told apart, so there is only ever one
        out = [p for p in out if p.client_size is not None]
    out.append(new_profile)
    return out


def select_profile(profiles, client_size):
    """The profile matching a window of `client_size`, or None.

    Exact size first, then any profile with the same aspect ratio (the HUD scales with the
    window, so the same profile is valid at 1280x800 and 1600x1000). A profile whose shape does
    not match is NOT returned - measuring the wrong pixels and reporting a confident number is
    worse than reporting nothing, which is the rule game_state follows everywhere."""
    if not profiles:
        return None
    if client_size is None:
        unanchored = [p for p in profiles if p.client_size is None]
        return unanchored[0] if unanchored else None

    w, h = client_size
    for p in profiles:
        if p.client_size == (w, h):
            return p
    if h > 0:
        aspect = w / h
        for p in profiles:
            size = p.client_size
            if size and size[1] > 0 and abs(size[0] / size[1] - aspect) <= ASPECT_TOLERANCE:
                return p
    return None


def save_meters(path, meters, anchor=None):
    """Single-profile convenience wrapper over save_profiles()."""
    save_profiles(path, [Profile(anchor=anchor, meters=tuple(meters))])
