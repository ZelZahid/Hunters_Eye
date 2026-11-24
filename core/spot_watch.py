"""Answering "did something suddenly appear at this spot?" - watching one small patch for a burst.

The question: a small patch of the picture normally holds a steady amount of some kind of pixel
(say "bright and colourless"), and the thing worth reacting to is that amount suddenly jumping
well above its own recent normal. It is handed a patch per frame and answers True / False / None.

WHY ITS OWN DETECTOR: none of the others answers this shape of question. presence.py asks "is
this picture here?", game_state.py "how full is this meter?", sound_match.py the same question as
this one but for sound. Here nothing has a fixed look - what repeats is "much more of this kind
of pixel than a moment ago, right here".

IT KNOWS NOTHING ABOUT ANY GAME. What counts as a pixel of interest (an HSV range) and how big a
jump counts are arguments; wow.py points it at the water under a fishing bobber, where a biting
fish throws up white spray. Pointed at a security camera it answers "did a light come on in that
doorway?" or "did smoke appear over that bin?" with a different range and nothing else changed.

WHY A BASELINE OF ITS OWN, NOT A FIXED COUNT: the normal amount differs per place and moment -
choppy water has white wave crests, calm water almost none (measured on four WoW recordings: a
calm spot held 10-25 such pixels in a 72x72 patch, the same patch over waves 15-40, a bite 100-220).
A fixed count tuned on one is wrong on the other. The baseline is the MEDIAN of the recent counts,
so one odd frame neither shifts it much nor (at a high enough ratio) fires on its own,
and frames that are part of a burst are kept out of it, so a slow burst does not raise its own bar.

THREE ANSWERS, as everywhere in this project (CLAUDE.md, "None is a third answer"): None while
there is no patch or not yet enough history to judge - never False - so the caller cannot mistake
"has not looked long enough" for "looked, and nothing happened".
"""
from __future__ import annotations

from collections import deque

import cv2 as cv
import numpy as np


class SpotWatcher:
    """One spot, watched frame by frame. feed() a BGR patch each frame; reset() when the spot moves.

    pixel_low / pixel_high: the HSV range (OpenCV's 0-180 hue) of the pixels that are counted.
    ratio, margin: a frame "jumps" when its count >= ratio * baseline + margin * patch area.
        The margin is a FRACTION of the patch so the same numbers work at any resolution.
    consecutive: jumping frames needed in a row. 1 by default: a short burst (a fish's spray lasts
        about a quarter of a second) can be only one or two frames at a modest frame rate, and
        asking for two lost 4 of 9 real bites at 12 frames a second. The ratio is what keeps a
        single odd frame out instead (a mouse pointer crossing the patch reached 102 of the 114
        needed at ratio 3; at ratio 2 it fired). Raise it for a source that flickers.
    baseline_frames: how many recent ordinary frames the median baseline is taken over.
    min_history: frames needed before anything is judged; until then the answer is None.
    """

    def __init__(self, pixel_low, pixel_high, ratio=3.0, margin=0.0116, consecutive=1,
                 baseline_frames=40, min_history=10):
        self.pixel_low = np.array(pixel_low, dtype=np.uint8)
        self.pixel_high = np.array(pixel_high, dtype=np.uint8)
        self.ratio = float(ratio)
        self.margin = float(margin)
        self.consecutive = max(1, int(consecutive))
        self.min_history = max(1, int(min_history))
        self._history = deque(maxlen=max(self.min_history, int(baseline_frames)))
        self._streak = 0
        self.fired = False        #True once a burst was reported, until reset()
        self.last = None          #(count, baseline, needed) of the latest frame, for logs and tests

    def reset(self):
        """Forget everything - the spot moved, or a new one is being watched."""
        self._history.clear()
        self._streak = 0
        self.fired = False
        self.last = None

    def count(self, patch_bgr):
        """How many pixels of the patch are in the range - the one number everything is built on."""
        hsv = cv.cvtColor(patch_bgr, cv.COLOR_BGR2HSV)
        return int(cv.countNonZero(cv.inRange(hsv, self.pixel_low, self.pixel_high)))

    def feed(self, patch_bgr):
        """True on the frame a burst is confirmed, False when watched and calm, None = cannot tell.

        Reports a burst ONCE: after that it answers False until reset(), because the caller acts
        on the first report and a second one from the same burst would act twice.
        """
        if patch_bgr is None or patch_bgr.size == 0 or patch_bgr.ndim != 3:
            return None
        count = self.count(patch_bgr)
        area = patch_bgr.shape[0] * patch_bgr.shape[1]
        if len(self._history) < self.min_history:
            self._history.append(count)
            self.last = (count, None, None)
            return None
        baseline = float(np.median(self._history))
        needed = self.ratio * baseline + self.margin * area
        self.last = (count, baseline, needed)
        if count >= needed:
            self._streak += 1
        else:
            self._streak = 0
            self._history.append(count)   #only ordinary frames shape the baseline
        if self.fired:
            return False
        if self._streak >= self.consecutive:
            self.fired = True
            return True
        return False
