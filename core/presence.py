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


class _Reference:
    """One piece of art, where to look for it, and how sure to be."""

    def __init__(self, template_bgr, source_size, search_region, threshold, name=""):
        #Matched on ONE channel, for the same measured reason the needle is (see main.py's
        #needle_gray): TM_CCOEFF_NORMED subtracts the mean and normalises, which makes it very
        #nearly colour-blind anyway, and greyscale is ~5x cheaper.
        self.template = cv.cvtColor(template_bgr, cv.COLOR_BGR2GRAY)
        self.source_width = float(source_size[0])
        self.search_region = tuple(search_region)
        self.threshold = float(threshold)
        self.name = name or "reference"
        self._scaled = {}  # scale -> resized template, so a resize does not happen every frame

    def template_at(self, scale):
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


class Presence:
    """A reference image plus where to look for it and how sure to be.

    region/search_region are fractions, so one config survives a resolution change. Whether they
    are fractions OF THE SCREEN or OF A WINDOW is the caller's business - it hands over already
    resolved frame fractions (see main.py, which routes them through window_region like the HUD
    meters do, so the check keeps working when the game is windowed or moved).
    """

    def __init__(self, references):
        self.references = list(references)

    #Kept so callers written against a single reference still work - both read the FIRST one.
    @property
    def threshold(self):
        return self.references[0].threshold

    @property
    def search_region(self):
        return self.references[0].search_region

    @property
    def template(self):
        return self.references[0].template

    def _template_at(self, scale):
        return self.references[0].template_at(scale)

    def locate(self, frame_gray, hud_width, region=None, resolve_region=None):
        """(found, score, box) where box is (x, y, w, h) in FRAME pixels, or (None, None, None).

        Same match as check(), but it also says WHERE. Two callers want different things from one
        template match: a guard only needs to know the art is there, while something about to
        click needs the position. Sharing the match keeps a single definition of "found".

        The box is in the coordinates of the frame that was passed in, NOT screen pixels - the
        caller knows what scale its frame is at and what the capture origin was, and this does
        not. Converting here would bake one caller's assumptions into all of them.

        WHAT THE TEMPLATE CONTAINS DECIDES WHETHER THE POSITION IS EVEN MEANINGFUL. If several
        things on screen share most of their pixels - Diablo II's menu buttons all use identical
        frame art - a template covering that shared part matches all of them almost equally and
        the returned position is close to arbitrary. Measured on a real menu: a whole-button
        template scored 0.900 on the right button and 0.888 on a different one, a margin of 0.013.
        The template has to be cut down to the part that actually DIFFERS (the words), which took
        the margin to 0.331. A bigger template is not a stronger match - see save_and_exit.json.
        """
        found, score, loc, shape, _ref = self._match(frame_gray, hud_width, region, resolve_region)
        if found is None:
            return None, None, None
        (x0, y0), (tw, th) = loc, shape
        return found, score, (x0, y0, tw, th)

    def check(self, frame_gray, hud_width, region=None, resolve_region=None):
        """(found, score) for this frame, or (None, None) when it cannot be judged.

        frame_gray: the frame to search, single channel.
        hud_width: how wide the HUD is drawn in THIS frame's pixels - the game's client width
            already scaled the same way the frame was. The reference was captured at
            source_size, so this is what rescales it; without it the check would silently fail
            the moment the game ran at a different resolution than the reference was cut from.
        region: (x, y, w, h) frame fractions to search, overriding search_region - the caller
            passes this when it has resolved the region against a window anchor.
        """
        found, score, _loc, _shape, _ref = self._match(frame_gray, hud_width, region, resolve_region)
        return found, score

    def _match(self, frame_gray, hud_width, region, resolve_region=None):
        """(found, score, (x, y) of the best match in frame pixels, (w, h) of the template).

        Returns (None, None, None, None) for anything unanswerable. Shared by check() and
        locate() so there is exactly one definition of "found" and one match per question asked.
        """
        """Best match across every reference. ANY reference over its threshold means found.

        Occlusion during play is normal and LOCAL - a tooltip, a panel, a spell effect covers one
        part of the screen - so a second reference somewhere else is what makes this robust. A
        looser threshold would not: it would trade the thing that stops a lobby matching.

        `region` overrides the search area for every reference (used by single-reference callers);
        `resolve_region` is a callable that maps each reference's own region, which is how a
        caller applies a window anchor without this module knowing what one is.
        """
        if frame_gray is None or frame_gray.size == 0:
            return None, None, None, None, None
        if not hud_width or hud_width <= 0:
            return None, None, None, None, None

        height, width = frame_gray.shape[:2]
        best = (None, None, None, None, None)
        for reference in self.references:
            template = reference.template_at(hud_width / reference.source_width)
            if template is None:
                continue

            area = region if region is not None else reference.search_region
            if resolve_region is not None:
                resolved = resolve_region(area)
                if resolved is not None:
                    area = resolved
            rx, ry, rw, rh = area
            x0 = max(0, min(width, int(rx * width)))
            y0 = max(0, min(height, int(ry * height)))
            x1 = max(x0, min(width, int((rx + rw) * width)))
            y1 = max(y0, min(height, int((ry + rh) * height)))
            wnd = frame_gray[y0:y1, x0:x1]

            #A template larger than the area to search is not a "no", it is an unanswerable
            #question - matchTemplate would raise. Skip it and let the others answer.
            if wnd.shape[0] < template.shape[0] or wnd.shape[1] < template.shape[1]:
                continue

            _, score, _, loc = cv.minMaxLoc(cv.matchTemplate(wnd, template, cv.TM_CCOEFF_NORMED))
            score = float(score)
            #Back into whole-frame coordinates: the match was found inside a cropped window, so
            #the crop origin has to be added back or every position is off by the search region.
            candidate = (score >= reference.threshold, score, (x0 + loc[0], y0 + loc[1]),
                         (template.shape[1], template.shape[0]), reference)
            if best[0] is None or candidate[0] and not best[0] or (
                    candidate[0] == best[0] and score > best[1]):
                best = candidate
            if best[0]:
                break   #one reference proving it is enough; the rest is wasted matching
        return best


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

    #Two shapes are accepted: a "references" list, or a single reference written flat at the top
    #level. The flat form is not legacy to be cleaned up - it is the right shape for something
    #with exactly one piece of art to find (see save_and_exit.json), and requiring a list of one
    #there would be noise.
    entries = meta.get("references")
    if not entries:
        entries = [meta]

    references = []
    for entry in entries:
        template_path = path.parent / entry.get("template", "")
        template = cv.imread(str(template_path))
        if template is None:
            print(f"WARNING: presence template {template_path} could not be loaded - skipped.")
            continue
        try:
            references.append(_Reference(template, entry["source_size"], entry["search_region"],
                                         entry.get("threshold", 0.6), entry.get("name", "")))
        except (KeyError, TypeError, ValueError) as exc:
            print(f"WARNING: {path}: a reference is missing or has a bad field ({exc}) - skipped.")

    if not references:
        print(f"WARNING: {path} defines no usable reference - this check is disabled.")
        return None
    return Presence(references)
