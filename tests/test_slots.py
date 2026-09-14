"""Synthetic + real-frame tests for core/slots.py, the cell-colour reader. No game required.

WHY THIS EXISTS: what this reads decides which KEY gets pressed while the owner is being hit. Every
way it can be wrong is quiet. Calling an empty slot "full" presses a dead key during an emergency;
calling a rejuvenation potion "healing" spends the rarest potion on a scratch; calling a full slot
"empty" silently stops all drinking, which from outside looks exactly like the feature not being
switched on. None of that raises. Same reasoning as test_game_state.py, which guards the other
measurement a potion decision rests on.

The real frames are the ones that matter here - the synthetic cases only pin the edges.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_slots.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os

import cv2 as cv
import numpy as np

from core import slots

failures = 0


def check(label, condition):
    global failures
    if condition:
        print(f"  ok   {label}")
    else:
        failures += 1
        print(f"  FAIL {label}")


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = json.load(open(ROOT / "assets" / "belt.json", encoding="utf-8"))
COLORS = CONFIG["colors"]
WHOLE = [0, 0, 1, 1]


def read(img, region=None):
    return slots.read_row(img, region or CONFIG["region"], CONFIG["count"], COLORS,
                          CONFIG.get("min_fill", slots.DEFAULT_MIN_FILL),
                          tuple(CONFIG.get("inset", slots.DEFAULT_INSET)))


print("\n1. The owner's real belt, read off a full frame")
full = cv.imread(str(FIXTURES / "belt_full.png"))
if full is None:
    check("belt_full.png is present", False)
else:
    got = read(full)
    check(f"healing, rejuvenation x2, mana - in that order ({got})",
          got == ["healing", "rejuvenation", "rejuvenation", "mana"])
    #The region is a fraction of the frame, so it has to survive the frame being a different size.
    #This is the property that lets one belt.json serve any capture scale, exactly as meters.json
    #does - and it is worth testing because getting it wrong reads four slots of the wrong pixels
    #and reports something plausible rather than failing.
    half = cv.resize(full, (0, 0), fx=0.5, fy=0.5)
    check(f"...and the same at half resolution ({read(half)})",
          read(half) == ["healing", "rejuvenation", "rejuvenation", "mana"])

print("\n2. An empty slot reads as empty, not as something")
#"if there is no Mana potion in the belt slot, don't be pressing 4 thinking that there is one."
#Measured on the real frames: an occupied slot is 26-37% lit and an empty one is 0.0%, so this is
#not a close call - but it is the single most expensive thing here to get wrong.
empty = cv.imread(str(FIXTURES / "belt_empty_slot.png"))
if empty is None:
    check("belt_empty_slot.png is present", False)
else:
    got = read(empty, WHOLE)
    check(f"the fourth slot is None, the rest unchanged ({got})",
          got == ["healing", "rejuvenation", "rejuvenation", None])
    check("None is a real answer, not a failure - the row itself was read", got is not None)

print("\n3. Nothing to read is a different answer from nothing there")
#A region that cannot be read at all returns None for the WHOLE ROW, which callers must treat as
#"cannot tell" and fall back on. A row of empty slots returns a list of Nones, which means
#"definitely nothing in the belt". Collapsing those two would make an off-screen belt look like an
#empty one and silently stop every potion.
black = np.zeros((60, 250, 3), np.uint8)
check("a blank region reads as four empty slots", read(black, WHOLE) == [None] * 4)
check("a region off the frame reads as None (cannot tell)",
      read(black, [2.0, 2.0, 0.1, 0.1]) is None)
check("a region too narrow to split reads as None",
      read(black, [0.0, 0.0, 0.001, 1.0]) is None)

print("\n4. Synthetic colours land in the right bucket")
#Built from the measured modal hues of the real potions: red 0, blue 118, purple 151.
def swatch(hues):
    img = np.zeros((60, 240, 3), np.uint8)
    for i, hue in enumerate(hues):
        if hue is None:
            continue
        cell = np.zeros((60, 60, 3), np.uint8)
        cell[:, :] = (hue, 200, 200)
        img[:, i * 60:(i + 1) * 60] = cv.cvtColor(cell, cv.COLOR_HSV2BGR)
    return img


check("red/blue/purple/empty", read(swatch([0, 118, 151, None]), WHOLE)
      == ["healing", "mana", "rejuvenation", None])
check("red at the other end of the hue circle is still red",
      read(swatch([178, None, None, None]), WHOLE)[0] == "healing")
#A colour nothing is configured for must read as empty rather than as the nearest thing - the same
#rule text_detection follows when a line ties: report nothing rather than guess.
check("an unconfigured colour (green) is not forced into a bucket",
      read(swatch([60, None, None, None]), WHOLE)[0] is None)
#Dark or washed-out pixels are the slot's own metal frame, not liquid.
dim = np.zeros((60, 240, 3), np.uint8)
dim[:, :] = (30, 20, 20)
check("a dark, unsaturated row is empty, not a colour", read(dim, WHOLE) == [None] * 4)

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
