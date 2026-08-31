"""Answering "is this reference image on screen right now?" - a yes/no, not a position.

WHY THIS IS SEPARATE FROM THE OTHER DETECTORS: it uses template matching, but it answers a
different SHAPE of question. main.py's detector answers "where is this thing?" (boxes + centres),
game_state.py answers "how full is this thing?" (a 0-1 number), and this answers "is this thing
here at all?" (a boolean). That third answer is what a consumer needs before it can trust either
of the other two.

WHY THAT MATTERS ENOUGH TO EXIST: a detector handed a frame has no way of knowing whether the
frame shows what it was calibrated against. Measured on real Diablo II lobby frames, the region
where the health orb belongs contained 7.3% red pixels - other players' names and red armour
rendering exactly where the orb would be - and periodically enough of them lined up to read as a
real 2-4% health. That drove an emergency potion, and because the lobby has a text field focused,
the keypresses were typed into the game name. No amount of tuning the COLOUR test fixes that:
red text and red liquid are the same colour. The only way out is a second, independent piece of
evidence that the HUD is on screen at all, which is what this provides.

IT KNOWS NOTHING ABOUT DIABLO II. It is handed a reference image, a place to look, and a
threshold, and reports whether it found it. The reference image, the region and the threshold all
live in a config file. Pointed at a security camera, the same code answers "is the door frame in
view?" - a sanity check that the camera has not been knocked askew before trusting anything else
measured from that frame.

Its answer is True/False/None, and None ("cannot tell") is NOT False - callers must not collapse
them. No template configured, a template bigger than the region to search, or a degenerate frame
all give None, and on None the pipeline must behave exactly as it did before this file existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2 as cv


class Presence:
    """A reference image plus where to look for it and how sure to be.

    region/search_region are fractions, so one config survives a resolution change. Whether they
    are fractions OF THE SCREEN or OF A WINDOW is the caller's business - it hands over already
    resolved frame fractions (see main.py, which routes them through window_region like the HUD
    meters do, so the check keeps working when the game is windowed or moved).
    """

    def __init__(self, template_bgr, source_size, search_region, threshold):
        #Matched on ONE channel, for the same measured reason the needle is (see main.py's
        #needle_gray): TM_CCOEFF_NORMED subtracts the mean and normalises, which makes it very
        #nearly colour-blind anyway, and greyscale is ~5x cheaper.
        self.template = cv.cvtColor(template_bgr, cv.COLOR_BGR2GRAY)
        self.source_width = float(source_size[0])
        self.search_region = tuple(search_region)
        self.threshold = float(threshold)
        self._scaled = {}  # scale -> resized template, so a resize does not happen every frame

    def _template_at(self, scale):
        key = round(scale, 4)
        cached = self._scaled.get(key)
        if cached is None:
            if scale <= 0:
                return None
            #Size is computed and checked BEFORE resizing, not after. cv.resize RAISES when a
            #scale rounds either dimension to zero ("!dsize.empty()"), so inspecting the result's
            #shape afterwards never runs - it would take the detection thread down instead. The
            #4px floor also rejects a template too small to match anything meaningfully.
            width = int(round(self.template.shape[1] * scale))
            height = int(round(self.template.shape[0] * scale))
            if width < 4 or height < 4:
                return None
            cached = cv.resize(self.template, (width, height),
                               interpolation=cv.INTER_AREA if scale < 1 else cv.INTER_LINEAR)
            self._scaled[key] = cached
        return cached

    def check(self, frame_gray, hud_width, region=None):
        """(found, score) for this frame, or (None, None) when it cannot be judged.

        frame_gray: the frame to search, single channel.
        hud_width: how wide the HUD is drawn in THIS frame's pixels - the game's client width
            already scaled the same way the frame was. The reference was captured at
            source_size, so this is what rescales it; without it the check would silently fail
            the moment the game ran at a different resolution than the reference was cut from.
        region: (x, y, w, h) frame fractions to search, overriding search_region - the caller
            passes this when it has resolved the region against a window anchor.
        """
        if frame_gray is None or frame_gray.size == 0:
            return None, None

        template = self._template_at(hud_width / self.source_width)
        if template is None:
            return None, None

        height, width = frame_gray.shape[:2]
        rx, ry, rw, rh = region if region is not None else self.search_region
        x0 = max(0, min(width, int(rx * width)))
        y0 = max(0, min(height, int(ry * height)))
        x1 = max(x0, min(width, int((rx + rw) * width)))
        y1 = max(y0, min(height, int((ry + rh) * height)))
        window = frame_gray[y0:y1, x0:x1]

        #A template larger than the area to search is not a "no", it is an unanswerable question -
        #matchTemplate would raise. Say "cannot tell" and let the caller carry on unguarded.
        if window.shape[0] < template.shape[0] or window.shape[1] < template.shape[1]:
            return None, None

        score = float(cv.matchTemplate(window, template, cv.TM_CCOEFF_NORMED).max())
        return score >= self.threshold, score


def load(path):
    """Reads a presence config (see assets/in_play.json). Returns None if absent or unusable.

    Never raises: a missing or broken config means "no check available", which every caller has
    to treat as None/unknown anyway. A guard that stops the program from starting is worse than
    the gap it was meant to close.
    """
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"WARNING: could not read {path}: {exc} - in-play check disabled.")
        return None

    template_path = path.parent / meta.get("template", "")
    template = cv.imread(str(template_path))
    if template is None:
        print(f"WARNING: presence template {template_path} could not be loaded "
              f"- in-play check disabled.")
        return None

    try:
        return Presence(template, meta["source_size"], meta["search_region"],
                        meta.get("threshold", 0.6))
    except (KeyError, TypeError, ValueError) as exc:
        print(f"WARNING: {path} is missing or has a bad field ({exc}) - in-play check disabled.")
        return None
