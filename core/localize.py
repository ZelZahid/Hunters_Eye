"""Answering "where on this map is the camera looking?" - template matching turned inside out.

WHY THIS IS SEPARATE FROM THE OTHER DETECTORS: the other template matchers take a small reference
and find it somewhere in a big frame. This takes the big frame and finds where it sits inside an
even bigger reference - a map stitched together from earlier frames. The answer is a POSITION ON
THE MAP, which is what anything that has to get from A to B needs, and what none of the other
detectors can give: they say where a thing is on screen, not where the screen is in the world.

WHY IT WORKS AT ALL, and the assumption it rests on: the map is only a map if the camera moves by
pure translation - no zoom, no rotation, no perspective change as it pans. That was measured, not
assumed, on Diablo II's Harrogath: across every overlapping pair of 15 screenshots the best-fit
similarity transform had scale 1.000 +/- 0.0015 and rotation within +/-0.07 degrees, and one set of
camera offsets agreed with every pair to within 0.3px. A camera that zooms or tilts as it moves
would break that, and tools/build_route_map.py checks for it rather than trusting this docstring.

LOCAL CONTRAST NORMALISATION (normalize_sigma), and the false match that made it necessary. Plain
TM_CCOEFF_NORMED normalises over the whole query, so in a DARK scene one bright object carries
almost the entire score. Harrogath and Nihlathak's Temple are both dark, snowy, and both contain
the same glowing red portal - and a temple frame matched the Harrogath map at 0.630, over a 0.55
threshold. Normalising every small neighbourhood to its own mean and contrast first makes each
patch of stone count as much as the portal does, so a match has to agree on the SCENE, not on
where the one bright thing is. Measured on the same frames:

                        weakest true match   strongest false match   gap
    plain                     0.776                 0.630             0.146
    normalised (sigma 6)      0.726                 0.090             0.636

It costs a little on the true matches and removes almost all of the false one - the right trade.

IT KNOWS NOTHING ABOUT ANY GAME. It is handed a greyscale map and a greyscale frame and reports
where the frame fits. What the map shows, what a position on it means, and what to do about it are
all the caller's business. Pointed at a robot's camera and a floor plan photographed from the same
height, the same code answers "which part of the corridor am I looking at".

Its answer carries True/False/None, and None ("cannot tell") is NOT False - a flat black frame (a
loading screen) or a query bigger than the map is unanswerable, not a "no".
"""
from __future__ import annotations

from collections import namedtuple

import cv2 as cv
import numpy as np


#found: True / False / None-cannot-tell.  x, y: where the QUERY's top-left sits on the map, in map
#pixels (None unless found).  score: best normalised match.  margin: how far the best place beats
#the best DIFFERENT place - see locate().
Fix = namedtuple("Fix", "found x y score margin")
_UNANSWERABLE = Fix(None, None, None, None, None)

#A query with less contrast than this (standard deviation of its grey levels, measured on the RAW
#pixels) carries no evidence of where it is. TM_CCOEFF_NORMED divides by the query's own variance,
#so on a flat frame - a loading screen, a black fade - the score is a division by almost nothing:
#meaningless, and liable to come out high. Refusing to answer is the only honest result.
MIN_QUERY_STD = 4.0
#Added to each neighbourhood's measured contrast before dividing by it. Without a floor, a patch of
#near-uniform black gets divided by nearly zero and its sensor noise is blown up into "texture"
#that matches anything. 4 grey levels is the same bar MIN_QUERY_STD sets for a whole query.
NORMALIZE_FLOOR = 4.0
#Map pixels this close to the edge of the mapped area are not used when normalising. The blur mixes
#in the unmapped zeros beyond the edge, which would draw a bright artificial outline round the map
#that some query could line up with. 9px at map scale is 1.5 sigma at the measured sigma of 6.
EDGE_TRIM_PX = 9


def normalize(gray, sigma):
    """Each pixel's difference from its neighbourhood mean, divided by the neighbourhood's contrast.

    float32 out. Local brightness and local contrast are both taken out, which is exactly what
    stops a single bright object - the whole reason this exists - from dominating a match.
    """
    f = gray.astype(np.float32)
    mean = cv.GaussianBlur(f, (0, 0), sigma)
    diff = f - mean
    spread = np.sqrt(cv.GaussianBlur(diff * diff, (0, 0), sigma)) + NORMALIZE_FLOOR
    return diff / spread


class MapLocator:
    """A reference map, plus how sure a match has to be before it counts as "found".

    Two bars, and a match has to clear both:
      threshold  - the match itself must be good. Separates "this is somewhere on the map" from
                   "this is not the map at all" (a different area, a menu).
      min_margin - the best place must beat every OTHER place by this much. Separates "here" from
                   "could be here or there" - a map with two similar-looking corners would otherwise
                   hand back whichever scored a hair higher, which is a confident wrong answer.
                   Same principle as text_detection reporting a tie as nothing rather than a guess.

    normalize_sigma: None for plain matching, or the neighbourhood size (in map pixels) for local
        contrast normalisation - see the module docstring. The two give scores on DIFFERENT scales,
        so threshold and min_margin have to be measured for whichever is used.

    The map's unmapped areas must be exactly 0 and nothing mapped may be 0 -
    tools/build_route_map.py guarantees that, and it is how the mapped area is told apart.
    """

    def __init__(self, map_gray, threshold=0.55, min_margin=0.2, normalize_sigma=None):
        if map_gray is None or map_gray.ndim != 2 or map_gray.size == 0:
            raise ValueError("MapLocator needs a non-empty single-channel map")
        self.map = map_gray
        self.threshold = float(threshold)
        self.min_margin = float(min_margin)
        self.normalize_sigma = normalize_sigma
        if normalize_sigma:
            mapped = cv.erode((map_gray > 0).astype(np.uint8),
                              np.ones((EDGE_TRIM_PX, EDGE_TRIM_PX), np.uint8)) > 0
            self._search = normalize(map_gray, normalize_sigma)
            self._search[~mapped] = 0.0
        else:
            self._search = map_gray

    def locate(self, frame_gray, view=None):
        """Where `view` of `frame_gray` sits on the map. Returns a Fix.

        frame_gray: the frame, single channel, already at the map's scale.
        view: (x, y, w, h) in frame_gray's pixels - the part to look for, or None for all of it.
            Which part is the caller's decision, since only the caller knows where its HUD and its
            own character are. Normalising is done over the WHOLE frame and the view cut out
            afterwards, so the blur has real neighbours at the view's edges instead of padding -
            which is also how it was measured.

        The margin is measured against the best match OUTSIDE a window around the winner, sized to
        a sixth of the query. Without the window the "second best" is just the pixel next door,
        which always scores almost the same and would make every match look ambiguous.
        """
        if frame_gray is None or frame_gray.ndim != 2 or frame_gray.size == 0:
            return _UNANSWERABLE
        if view is None:
            view = (0, 0, frame_gray.shape[1], frame_gray.shape[0])
        vx, vy, vw, vh = (int(v) for v in view)
        raw = frame_gray[vy:vy + vh, vx:vx + vw]
        if raw.size == 0:
            return _UNANSWERABLE
        qh, qw = raw.shape
        mh, mw = self.map.shape
        #A query bigger than the map is not a "no", it is an unanswerable question - and
        #matchTemplate would raise on it.
        if qh > mh or qw > mw:
            return _UNANSWERABLE
        if float(raw.std()) < MIN_QUERY_STD:
            return _UNANSWERABLE

        if self.normalize_sigma:
            query = normalize(frame_gray, self.normalize_sigma)[vy:vy + vh, vx:vx + vw]
        else:
            query = raw
        result = cv.matchTemplate(self._search, query, cv.TM_CCOEFF_NORMED)
        #Flat stretches of the map (the unmapped area) make the denominator vanish there; treat
        #those places as "no match" rather than letting a NaN win.
        np.nan_to_num(result, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, best, _, loc = cv.minMaxLoc(result)

        radius = max(qh, qw) // 6
        x, y = loc
        others = result.copy()
        others[max(0, y - radius):y + radius + 1, max(0, x - radius):x + radius + 1] = -1.0
        second = float(others.max()) if others.size else -1.0

        best = float(best)
        margin = best - second
        found = best >= self.threshold and margin >= self.min_margin
        if not found:
            return Fix(False, None, None, best, margin)
        return Fix(True, x, y, best, margin)
