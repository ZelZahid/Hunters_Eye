"""The contract every detector shares, checked across all of them at once. No game required.

WHY THIS EXISTS: CLAUDE.md sets three rules that hold for EVERY detector, and each detector's own
test file checks them only for itself, in its own way, if at all:

  - a detector is handed a BGR frame and returns matches - it must never take the thread that
    called it down, whatever the frame looks like (design priority #1: no crashes, no hangs);
  - None is a third answer, "cannot tell", and is never collapsed into False or 0.0;
  - every detector must work on NON-GAME input - a document, a browser window, a camera frame -
    with only a different config. That is the litmus test for the GUI product and the security
    robot, and nothing else in tests/ ever hands a detector anything but Diablo II.

This file hands every detector the same frames: blank ones, noise, frames far smaller than
anything it was configured for, and plain synthetic pages with text and an icon on them. A new
detector should be added to V-901 and V-905 the day it lands.

OCR cards (V-903, V-904) need Tesseract. Without it they say so and skip on a developer machine,
but under CI (the CI environment variable, which GitHub Actions sets) that is a FAILURE: the CI
machine installs Tesseract precisely so these run, and a skip there would look like a pass.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import tempfile

import cv2 as cv
import numpy as np

import main
from core import game_state, monster_detection, presence, slots
from core import text_detection as td
from routes import pindle

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
UNDER_CI = bool(os.environ.get("CI"))
failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def survives(label, call, valid):
    """Runs call(); a raise is a failure, and so is an answer of the wrong shape."""
    try:
        answer = call()
    except Exception as e:
        check(f"{label}: raised {type(e).__name__}: {e}", False)
        return None
    check(f"{label} -> {short(answer)}", valid(answer))
    return answer


def short(answer):
    text = repr(answer)
    return text if len(text) <= 60 else text[:57] + "..."


def page(height=600, width=900):
    """A plain white page - the background of a document, a browser, a form."""
    return np.full((height, width, 3), 255, np.uint8)


def write(img, text, x, y, scale=1.6, colour=(0, 0, 0), thickness=3):
    cv.putText(img, text, (x, y), cv.FONT_HERSHEY_SIMPLEX, scale, colour, thickness, cv.LINE_AA)


def ocr_ready(card):
    """True if OCR can run; otherwise records a skip, or a failure under CI."""
    if td.ocr_available():
        return True
    if UNDER_CI:
        check(f"{card} needs Tesseract, and CI installs it - it is missing", False)
    else:
        print("  SKIPPED (Tesseract is not installed here) - CI runs this one for real")
    return False


#The shipped configs, so the detectors are exercised exactly as main.py runs them.
profiles = game_state.load_profiles(str(ASSETS / "meters.json"))
meters = profiles[0].meters if profiles else []
belt = json.load(open(ASSETS / "belt.json", encoding="utf-8"))
in_play = presence.load(str(ASSETS / "in_play.json"))
route = pindle.Route.load(pindle.HARROGATH_ROUTE)
check("the shipped meter, belt, in-play and route configs all load",
      bool(meters) and in_play is not None and route is not None)


print("\nV-901  No detector crashes on a frame it was never meant to see")
rng = np.random.default_rng(901)
FRAMES = {
    "black 1920x1080": np.zeros((1080, 1920, 3), np.uint8),
    "white 1920x1080": np.full((1080, 1920, 3), 255, np.uint8),
    "noise 576x324": rng.integers(0, 256, (324, 576, 3), dtype=np.uint8),
    "tiny 8x8": rng.integers(0, 256, (8, 8, 3), dtype=np.uint8),
    "one pixel": np.zeros((1, 1, 3), np.uint8),
    "a 1px strip": np.zeros((1, 500, 3), np.uint8),
}
monster_detection.load(model_dir=tempfile.mkdtemp())   #no model there: the not-loaded path
for name, frame in FRAMES.items():
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    print(f"  -- {name}")
    survives("    meters", lambda: game_state.read_all(frame, meters),
             lambda r: all(v is None or 0.0 <= v <= 1.0 for v in r.values()))
    survives("    belt", lambda: slots.read_row(frame, belt["region"], belt["count"], belt["colors"]),
             lambda r: r is None or (len(r) == belt["count"]
                                     and all(c is None or c in belt["colors"] for c in r)))
    survives("    in-play art", lambda: in_play.check(gray, gray.shape[1]),
             lambda r: r[0] in (None, True, False))
    survives("    map locate", lambda: route.locator.locate(gray),
             lambda r: r.found is not True)
    survives("    monster model (none loaded)", lambda: monster_detection.detect(frame),
             lambda r: r == [])
    #Only frames at least the needle's size: the capture is always a whole monitor, so that is
    #what main.py can hand it. A SMALLER frame does not raise - OpenCV swaps image and template
    #and reports phantom matches - which is parked in docs/todolist.md, not asserted here.
    if gray.shape[0] >= main.needle_gray.shape[0] and gray.shape[1] >= main.needle_gray.shape[1]:
        survives("    image search", lambda: main._match_needle(gray, main.needle_gray),
                 lambda r: r == [])


print("\nV-902  Image search finds an icon on a document page, not just in a game")
#A made-up icon - nothing in it is from Diablo II - pasted onto a page with text around it.
icon = np.full((40, 56, 3), 255, np.uint8)
cv.rectangle(icon, (2, 2), (53, 37), (40, 40, 200), 3)
cv.circle(icon, (18, 20), 9, (30, 140, 30), -1)
cv.line(icon, (32, 10), (50, 30), (0, 0, 0), 3)
doc = page()
write(doc, "Quarterly report", 40, 70)
write(doc, "Status: all systems normal", 40, 520, scale=1.0, thickness=2)
doc[260:300, 610:666] = icon
found = main._match_needle(cv.cvtColor(doc, cv.COLOR_BGR2GRAY), cv.cvtColor(icon, cv.COLOR_BGR2GRAY))
check(f"found exactly once ({found})", len(found) == 1)
check("at exactly where it was pasted, box + size", found == [[610, 260, 56, 40]])
blank = page()
write(blank, "Quarterly report", 40, 70)
check("a page without the icon -> nothing",
      main._match_needle(cv.cvtColor(blank, cv.COLOR_BGR2GRAY), cv.cvtColor(icon, cv.COLOR_BGR2GRAY)) == [])


SIGNS = ["LOADING BAY", "EMERGENCY EXIT", "STAFF ONLY"]
with tempfile.TemporaryDirectory() as tmp:
    vocabulary = os.path.join(tmp, "signs.txt")
    with open(vocabulary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(SIGNS) + "\n")
    targets = td.load_target_items(vocabulary)


def found_names(frame):
    return {m[4]: m[:4] for m in td.find_text_matches(frame, targets)}


print("\nV-903  OCR finds listed words on a white document page, and says where")
if ocr_ready("V-903"):
    doc = page()
    write(doc, "EMERGENCY EXIT", 60, 140)
    write(doc, "Visitors sign in at reception", 60, 330, scale=1.0, thickness=2)
    hits = found_names(doc)
    check(f"the listed sign is found ({sorted(hits)})", "EMERGENCY EXIT" in hits)
    check("nothing unlisted is reported as a sign", set(hits) <= {"EMERGENCY EXIT"})
    if "EMERGENCY EXIT" in hits:
        x, y, w, h = hits["EMERGENCY EXIT"]
        #putText's y is the baseline; the glyphs sit above it. The box only has to land on them.
        check(f"the box sits on the words ({x}, {y}, {w}, {h})",
              40 <= x <= 90 and 90 <= y <= 140 and 300 <= w <= 520)
    lines = [text for text, _box in td.read_lines(doc)]
    check(f"read_lines gives the text as written, case and all ({lines})",
          any("Visitors sign in" in t for t in lines))


print("\nV-904  OCR reads light text on a dark background - a night-time sign, a dark UI")
if ocr_ready("V-904"):
    dark = np.full((300, 900, 3), 25, np.uint8)
    write(dark, "LOADING BAY", 60, 120, colour=(235, 235, 235))
    write(dark, "STAFF ONLY", 60, 240, colour=(255, 200, 60))   #light blue in BGR: a coloured sign
    hits = found_names(dark)
    check(f"white sign found ({sorted(hits)})", "LOADING BAY" in hits)
    check("coloured sign found too - binarized on the strongest channel, not luminance",
          "STAFF ONLY" in hits)
    check("and a plain dark frame reports nothing", found_names(np.full((300, 900, 3), 25, np.uint8)) == {})


print("\nV-905  'Cannot tell' comes back as None from every detector, never as a confident no")
off = (1.5, 1.5, 0.1, 0.1)   #a region entirely off the frame: nothing there can be measured
frame = np.zeros((324, 576, 3), np.uint8)
meter = game_state.Meter("health", off, meters[0].hsv_ranges if meters else ((0, 0, 0), (1, 1, 1)))
check("meter whose region is off the frame -> None, not 0.0", game_state.read_meter(frame, meter) is None)
check("belt whose region is off the frame -> None, not four empty slots",
      slots.read_row(frame, off, belt["count"], belt["colors"]) is None)
check("in-play check with no frame -> (None, None), not False", in_play.check(None, 576) == (None, None))
check("map locate on a flat frame -> found is None, not False",
      route.locator.locate(np.zeros((172, 288), np.uint8)).found is None)
#And the other way round: a confident answer must still be possible, or None is just a crash
#with better manners. A black frame IS readable - the meter is simply not there.
check("a readable frame with no liquid in the orb is a real 0.0 or None - never negative",
      all(v is None or v >= 0.0 for v in game_state.read_all(frame, meters).values()))


print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
