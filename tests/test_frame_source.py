"""Tests for core/frame_source.py (where pixels come from) and window_region's pure geometry.
No game required.

WHY THIS EXISTS: every detector in the project starts from a frame this module handed it, and the
module's whole job is to keep handing frames over when something underneath it goes wrong - DXGI
not available, DXGI dying mid-session on a resolution change, a static screen that produces no
new frame. Each of those, done wrong, is either a crash (the capture thread dies and every
detector goes blind at once) or a silent hang, which is the one failure design priority #1 rules
out. None of it can be seen from outside until the game is already running unwatched.

The fallback paths are driven with FAKE backends, so they are tested on every OS - including the
Windows-only DXGI one - without a GPU and without a screen. Only V-801 captures for real, and it
needs a display: on a developer machine without one it says so and skips, but under CI (the CI
environment variable, which GitHub Actions sets) a skip is a FAILURE, because a check that skips
on the machine meant to run it looks exactly like a check that passes.

window_region's to_frame_fractions() is here too: it is what turns "the health orb is 21% across
the GAME WINDOW" into a place in the captured frame, so it decides which pixels the meters read.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

import numpy as np

from core import frame_source as fs
from core import window_region

failures = 0
UNDER_CI = bool(os.environ.get("CI"))


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


class FakeSource:
    """A backend that hands out numbered frames, and can be told to start failing."""

    def __init__(self, name, fail_after=None, value=7):
        self.name = name
        self.fail_after = fail_after
        self.value = value
        self.grabs = 0
        self.closed = False

    def grab(self):
        self.grabs += 1
        if self.fail_after is not None and self.grabs > self.fail_after:
            raise RuntimeError(f"{self.name} lost the device")
        return np.full((4, 6, 4), self.value, np.uint8)

    def close(self):
        self.closed = True


class patched:
    """Swap frame_source's two backend classes for the duration of a with-block."""

    def __init__(self, dxgi, mss):
        self.dxgi, self.mss = dxgi, mss

    def __enter__(self):
        self.saved = fs._DxgiSource, fs._MssSource
        fs._DxgiSource, fs._MssSource = self.dxgi, self.mss

    def __exit__(self, *_):
        fs._DxgiSource, fs._MssSource = self.saved


REGION = {"left": 0, "top": 0, "width": 64, "height": 48}


print("\nV-801  A real screen region comes back as BGRA pixels of exactly the size asked for")
try:
    source = fs.FrameSource(REGION, backend=fs.MSS)
    frame = source.grab()
    source.close()
except Exception as e:
    frame, error = None, e
else:
    error = None
if error is not None:
    if UNDER_CI:
        check(f"the screen can be captured under CI ({error})", False)
    else:
        print(f"  SKIPPED (no screen to capture here: {error}) - CI runs this one for real")
else:
    check(f"shape is height x width x 4 ({frame.shape})", frame.shape == (48, 64, 4))
    check(f"pixels are uint8 ({frame.dtype})", frame.dtype == np.uint8)
    #Detectors convert BGRA -> BGR by dropping the last channel. That only works if the array is
    #a plain C-ordered block, which is what np.frombuffer gives and np.array(img) would too.
    check("the array is contiguous, so slicing a channel off is free", frame.flags["C_CONTIGUOUS"])


print("\nV-802  No DXGI means mss - never an exception")
def no_dxgi(region):
    raise fs.FrameSourceError("DXGI Desktop Duplication is Windows-only")
with patched(no_dxgi, lambda region: FakeSource("mss")):
    try:
        auto = fs.FrameSource(REGION)
        check(f"auto mode fell back to mss ({auto.name})", auto.name == "mss")
        check("and it still produces a frame", auto.grab() is not None)
    except Exception as e:
        check(f"auto mode did not raise ({e})", False)
    try:
        fs.FrameSource(REGION, backend=fs.DXGI)
        check("asking for DXGI explicitly raises when it is unavailable", False)
    except fs.FrameSourceError:
        check("asking for DXGI explicitly raises when it is unavailable", True)
#The real _DxgiSource, on a platform that is not Windows, must refuse with FrameSourceError -
#the one exception auto mode catches. Anything else would take the capture thread down.
if not sys.platform.startswith("win"):
    try:
        fs._DxgiSource(REGION)
        check("the real DXGI backend refuses off Windows", False)
    except fs.FrameSourceError:
        check("the real DXGI backend refuses off Windows, with FrameSourceError", True)
    except Exception as e:
        check(f"the real DXGI backend refuses with FrameSourceError, not {type(e).__name__}", False)


print("\nV-803  A backend that dies mid-session degrades to mss, and the frame still arrives")
dying = FakeSource("dxgi", fail_after=2, value=1)
spare = FakeSource("mss", value=2)
with patched(lambda region: dying, lambda region: spare):
    src = fs.FrameSource(REGION)
    first = [int(src.grab()[0, 0, 0]) for _ in range(2)]
    check(f"frames come from DXGI while it works ({first})", first == [1, 1])
    third = src.grab()
    check("the grab that hit the failure still returns a frame", third is not None)
    check(f"...from mss ({third[0, 0, 0]})", third is not None and third[0, 0, 0] == 2)
    check(f"the source now reports mss ({src.name})", src.name == "mss")
    check("the dead backend was closed", dying.closed)
    check("later grabs keep coming from mss", src.grab()[0, 0, 0] == 2)
#Falling back is a one-time rescue, not a loop: if mss itself then fails there is nothing left to
#fall back to, and retrying forever would be a silent hang. It must raise so the caller sees it.
dying2 = FakeSource("dxgi", fail_after=0)
broken_mss = FakeSource("mss", fail_after=1)
with patched(lambda region: dying2, lambda region: broken_mss):
    src = fs.FrameSource(REGION)
    src.grab()   #DXGI fails -> falls back to mss, which serves its one good frame
    try:
        src.grab()
        check("a failure AFTER falling back raises instead of looping", False)
    except RuntimeError:
        check("a failure AFTER falling back raises instead of looping", True)


print("\nV-804  DXGI's 'no new frame' re-serves the last frame - a static screen is not an error")
class FakeCam:
    def __init__(self, frames):
        self.frames = list(frames)

    def grab(self):
        return self.frames.pop(0) if self.frames else None

a = np.zeros((2, 2, 4), np.uint8)
b = np.ones((2, 2, 4), np.uint8)
dxgi = fs._DxgiSource.__new__(fs._DxgiSource)   #skip __init__: no GPU or bettercam needed
dxgi._cam, dxgi._last = FakeCam([None, a, None, None, b, None]), None
got = [dxgi.grab() for _ in range(6)]
check("before the first frame arrives: None (the caller retries)", got[0] is None)
check("a new frame is passed straight through", got[1] is a)
check("no new frame -> the previous frame again, not None", got[2] is a and got[3] is a)
check("the next real frame replaces it", got[4] is b and got[5] is b)


print("\nV-805  A region given as fractions of a WINDOW lands on the right pixels of the capture")
#A 1280x800 game window at (320, 140) on a 1920x1080 capture starting at the screen origin.
win, cap = (320, 140, 1280, 800), (0, 0, 1920, 1080)
got = window_region.to_frame_fractions(win, cap, (0.5, 0.5, 0.25, 0.25))
want = ((320 + 640) / 1920, (140 + 400) / 1080, 320 / 1920, 200 / 1080)
check(f"the window's centre quarter maps to its screen position ({got})",
      got is not None and np.allclose(got, want))
check("the whole window maps to exactly the window's own rectangle",
      np.allclose(window_region.to_frame_fractions(win, cap, (0, 0, 1, 1)),
                  (320 / 1920, 140 / 1080, 1280 / 1920, 800 / 1080)))
#The None cases are the important half: a window on a monitor we are not capturing must read
#"cannot tell", never fractions that point at bare desktop - that is how meters read 0% once.
check("a window entirely off the capture -> None, not zeros",
      window_region.to_frame_fractions((2000, 0, 800, 600), cap, (0, 0, 1, 1)) is None)
check("a zero-sized window -> None", window_region.to_frame_fractions((0, 0, 0, 600), cap, (0, 0, 1, 1)) is None)
check("a zero-sized capture -> None", window_region.to_frame_fractions(win, (0, 0, 0, 0), (0, 0, 1, 1)) is None)
#Which window a point is in: the innermost one, not the desktop or a maximised window behind it.
rects = [("Desktop", (0, 0, 1920, 1080)), ("Game", (320, 140, 1280, 800)), ("Tooltip", (400, 200, 100, 50))]
check("the point picks the smallest window containing it",
      window_region.smallest_containing(rects, 410, 210)[0] == "Tooltip")
check("outside the inner windows, the next one out", window_region.smallest_containing(rects, 1000, 600)[0] == "Game")
check("a point in no window -> None", window_region.smallest_containing(rects, 5000, 5000) is None)


print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
