"""Reading a row of fixed cells and saying what colour is in each one - or that one is empty.

THE SEVENTH DETECTOR, AND A FIFTH SHAPE OF ANSWER. Template matching says WHERE a thing is, OCR
says WHAT IT SAYS, game_state.py says HOW FULL something is, presence.py says WHETHER something is
there at all, localize.py says WHERE THE CAMERA IS. This one says WHICH OF SEVERAL KNOWN THINGS is
in each of a fixed set of positions - a row of supplies, and how much of each you have to hand.

It exists because a consumer that presses a key needs to know the key is worth pressing. Diablo
II's potion belt is the case it was written for: the belt holds four slots bound to keys 1-4, the
player rearranges them freely, and pressing the key for an empty slot does nothing while pressing
the key for the WRONG potion wastes something rare. Nothing in here knows any of that. It is
handed a region, a number of cells, and a set of named colours, and it reports which named colour
each cell holds. Point it at a row of status lights, a toolbar, a rack of coloured bins, a shelf of
stock, and it answers the same question.

WHY COLOUR AND NOT TEMPLATE MATCHING: the thing being told apart IS a colour. Measured on the
owner's real belt, the three potion types separate on hue alone by a mile - modal hue 0 (red
healing), 118 (blue mana), 151 (purple rejuvenation) - while their glass, highlights and metal
frames are identical art. TM_CCOEFF_NORMED is very nearly colour-blind (see CLAUDE.md's measured
note on that), so it is the wrong tool for exactly this: it would be comparing the parts that do
not differ. This is the case CLAUDE.md describes as "a job for an explicit HSV pre-mask".

AND NOT OCR: there is no text. The slot shows a bottle.
"""
from __future__ import annotations

import cv2 as cv
import numpy as np

#How much of a cell to ignore at each edge, as a fraction of the cell. The cell's border is the
#container's own art - an ornate metal frame in Diablo II's case - which is identical whatever the
#cell holds and would only add pixels that cannot discriminate. Measured against the real belt: at
#these insets an occupied slot reads 26-37% lit and an empty one reads 0.0%.
DEFAULT_INSET = (0.22, 0.18)
#The fraction of a cell that must match a colour before that colour counts as present. Well below
#the 26% an occupied slot measures, and well above the 0.0% an empty one does - the gap is the
#whole margin, and it is enormous because an empty slot contains no saturated pixels at all.
DEFAULT_MIN_FILL = 0.05


def _mask(hsv, ranges):
    """Pixels falling in any of `ranges`, each [h0, s0, v0, h1, s1, v1].

    A LIST of ranges, not one, for the same reason game_state.py's meters take a list: a single
    colour can need two of them. Red wraps around the end of the hue circle, so "red" is
    0-10 OR 170-179 and there is no way to write it as one range.
    """
    total = None
    for lo_h, lo_s, lo_v, hi_h, hi_s, hi_v in ranges:
        m = cv.inRange(hsv, np.array([lo_h, lo_s, lo_v], np.uint8),
                       np.array([hi_h, hi_s, hi_v], np.uint8))
        total = m if total is None else cv.bitwise_or(total, m)
    return total


def read_row(frame_bgr, region, count, colors, min_fill=DEFAULT_MIN_FILL, inset=DEFAULT_INSET):
    """[name or None] for each of `count` cells evenly spaced across `region`.

    frame_bgr: the frame to read, BGR (OpenCV's convention throughout this project).
    region: (x, y, w, h) as FRACTIONS of the frame, so one config survives any capture scale -
        the same choice meters.json makes, and for the same reason.
    colors: {name: [[h0, s0, v0, h1, s1, v1], ...]} - HSV ranges per named colour.
    min_fill: a colour must cover at least this fraction of the cell to count as present.

    A cell whose best colour does not reach min_fill reads None, which means EMPTY - and that is a
    real answer, not a failure. Returns None for the whole row only when the region cannot be read
    at all (off-frame, zero-sized), which is the "cannot tell" case every caller in this project
    has to treat as "behave as if this check did not exist".
    """
    h, w = frame_bgr.shape[:2]
    fx, fy, fw, fh = region
    x0, y0 = int(fx * w), int(fy * h)
    x1, y1 = int((fx + fw) * w), int((fy + fh) * h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 - x0 < count or y1 - y0 < 1:
        return None

    band = frame_bgr[y0:y1, x0:x1]
    hsv = cv.cvtColor(band, cv.COLOR_BGR2HSV)
    bh, bw = hsv.shape[:2]
    cell_w = bw / float(count)
    inset_x, inset_y = inset

    out = []
    for i in range(count):
        cx0 = int(i * cell_w + cell_w * inset_x)
        cx1 = int((i + 1) * cell_w - cell_w * inset_x)
        cy0, cy1 = int(bh * inset_y), int(bh * (1 - inset_y))
        if cx1 - cx0 < 1 or cy1 - cy0 < 1:
            out.append(None)
            continue
        cell = hsv[cy0:cy1, cx0:cx1]
        area = float(cell.shape[0] * cell.shape[1])
        best, best_fill = None, 0.0
        for name, ranges in colors.items():
            fill = float(np.count_nonzero(_mask(cell, ranges))) / area
            if fill > best_fill:
                best, best_fill = name, fill
        #MOST PIXELS WINS, and then it still has to clear min_fill. Ranking before thresholding is
        #what stops a slot being called "empty" because two colours each fell just short, and what
        #stops a stray highlight in the wrong hue outvoting the liquid filling the cell.
        out.append(best if best_fill >= min_fill else None)
    return out
