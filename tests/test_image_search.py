"""Tests for main.py's reference-image search (_match_needle). No game required.

WHY THIS EXISTS: _match_needle skips the full scan of the score map whenever nothing on it
clears the threshold. That is a speed change that must NOT be a behaviour change, so the checks
below hold it to the exact output of the old always-scan version - on real frames with no needle
in them (the common case, and the one the shortcut is for) and on the same frames with the
needle pasted in, once, twice, and overlapping itself.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2 as cv
import numpy as np

import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def old_match(frame_gray, template, threshold=main.NEEDLE_THRESHOLD, max_results=main.NEEDLE_MAX_RESULTS):
    """The detect_objects() body as it was before the shortcut, kept verbatim as the reference."""
    result = cv.matchTemplate(frame_gray, template, method=cv.TM_CCOEFF_NORMED)
    locations = list(zip(*np.where(result >= threshold)[::-1]))
    h, w = template.shape[:2]
    rectangles = [[int(x), int(y), w, h] for (x, y) in locations]
    scores = [float(result[y, x]) for (x, y) in locations]
    indices = cv.dnn.NMSBoxes(rectangles, scores, threshold, nms_threshold=0.3)
    return [rectangles[i] for i in indices][:max_results]


def frame(name):
    """A committed fixture at CAPTURE_SCALE, greyscale - exactly what detect_objects() matches."""
    image = cv.imread(str(FIXTURES / name))
    image = cv.resize(image, (0, 0), fx=main.CAPTURE_SCALE, fy=main.CAPTURE_SCALE)
    return cv.cvtColor(image, cv.COLOR_BGR2GRAY)


needle = main.needle_gray
nh, nw = needle.shape[:2]
names = ["pindle_pack.png", "in_game_tooltip.png", "lobby.png", "inventory_open.png", "in_game_dim.png"]

print("\nV-501  No needle on screen: nothing found, same as before")
for name in names:
    gray = frame(name)
    check(f"{name}: no matches, and the old version agrees",
          main._match_needle(gray, needle) == [] == old_match(gray, needle))

print("\nV-502  Needle pasted in: identical boxes to the old version")
for name in names:
    gray = frame(name)
    one = gray.copy()
    one[40:40 + nh, 60:60 + nw] = needle
    found = main._match_needle(one, needle)
    check(f"{name}: one copy found at its true position", [60, 40, nw, nh] in found)
    check(f"{name}: one copy - same output as before", found == old_match(one, needle))

    two = one.copy()
    two[200:200 + nh, 300:300 + nw] = needle
    two[203:203 + nh, 305:305 + nw] = needle   #overlapping itself - NMS has to collapse these
    check(f"{name}: three copies, two overlapping - same output as before",
          main._match_needle(two, needle) == old_match(two, needle))

print("\nC-118  The shipped user_config.txt says whether the search runs")
check("image_search is a yes/no the pipeline reads", isinstance(main.IMAGE_SEARCH_ENABLED, bool))

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
