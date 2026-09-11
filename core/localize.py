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

MEASURED, on a map built from 8 screenshots and queried with 7 OTHER screenshots of the same area
taken minutes earlier (different NPC positions, a spell glowing on the character, camera positions
the map was never built from): every one located within 3.2px of the truth, weakest score 0.758
with a margin of 0.454 over the next-best place. The inside of a dungeon scored 0.278. Cost is one
matchTemplate of the query against the whole map: ~13ms for a 1007x711 map.

IT KNOWS NOTHING ABOUT ANY GAME. It is handed a greyscale map and a greyscale query and reports
where the query fits. What the map shows, what a position on it means, and what to do about it are
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

#A query with less contrast than this (standard deviation of its grey levels) carries no evidence
#of where it is. TM_CCOEFF_NORMED divides by the query's own variance, so on a flat frame - a
#loading screen, a black fade - the score is a division by almost nothing: meaningless, and liable
#to come out high. Refusing to answer is the only honest result.
MIN_QUERY_STD = 4.0


class MapLocator:
    """A reference map, plus how sure a match has to be before it counts as "found".

    Two bars, and a match has to clear both:
      threshold  - the match itself must be good. Separates "this is somewhere on the map" from
                   "this is not the map at all" (a different area, a menu).
      min_margin - the best place must beat every OTHER place by this much. Separates "here" from
                   "could be here or there" - a map with two similar-looking corners would otherwise
                   hand back whichever scored a hair higher, which is a confident wrong answer.
                   Same principle as text_detection reporting a tie as nothing rather than a guess.
    """

    def __init__(self, map_gray, threshold=0.55, min_margin=0.2):
        if map_gray is None or map_gray.ndim != 2 or map_gray.size == 0:
            raise ValueError("MapLocator needs a non-empty single-channel map")
        self.map = map_gray
        self.threshold = float(threshold)
        self.min_margin = float(min_margin)

    def locate(self, query_gray):
        """Where `query_gray` sits on the map. Returns a Fix.

        The query should be a crop of the live frame that is mostly static scenery - which part is
        the caller's decision, since only the caller knows where its HUD and its own character are.

        The margin is measured against the best match OUTSIDE a window around the winner, sized to
        a sixth of the query. Without the window the "second best" is just the pixel next door,
        which always scores almost the same and would make every match look ambiguous.
        """
        if query_gray is None or query_gray.ndim != 2 or query_gray.size == 0:
            return _UNANSWERABLE
        qh, qw = query_gray.shape
        mh, mw = self.map.shape
        #A query bigger than the map is not a "no", it is an unanswerable question - and
        #matchTemplate would raise on it.
        if qh > mh or qw > mw:
            return _UNANSWERABLE
        if float(query_gray.std()) < MIN_QUERY_STD:
            return _UNANSWERABLE

        result = cv.matchTemplate(self.map, query_gray, cv.TM_CCOEFF_NORMED)
        #Flat stretches of the MAP (unmapped areas are filled with 0) make the denominator vanish
        #there too; treat those places as "no match" rather than letting a NaN win.
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
