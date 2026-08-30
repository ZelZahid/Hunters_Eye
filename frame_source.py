'''
========================================================================================================
Hunters-Eye: frame sources - where pixels come from
========================================================================================================

The "frame source" seam from CLAUDE.md, made concrete. A frame source is handed a screen region
and hands back BGRA pixels; it knows nothing about what looks at them. A camera source
(cv.VideoCapture) is the next one to slot in here, and nothing above this line should have to
change when it does.

Two backends today:

  DXGI (bettercam)  - Windows Desktop Duplication. The GPU already holds the composited desktop,
                      so this reads it back instead of asking GDI to re-blit it. Measured on an
                      i9-11900K / RTX 5070 at 1920x1080: of the 13.6ms mss spends per frame,
                      12.4ms is GDI's BitBlt alone, and this replaces essentially all of it. The
                      capture thread went 15.3 -> 7.0 CPU-ms per frame and the pipeline's own
                      ceiling went ~58 -> ~177 FPS.
  MSS               - the portable fallback, and what this project used exclusively before. Works
                      on Windows, macOS and Linux. Used automatically wherever DXGI is not
                      available, and as the runtime fallback if DXGI fails mid-session.

TWO PROPERTIES OF THIS MODULE THAT ARE NOT OBVIOUS AND WILL BITE:

1. **A DXGI source is a per-output SINGLETON, so only ONE consumer in the process can have one.**
   bettercam.create() for an output that already has a camera does not raise and does not give
   you a second camera - it prints a notice and hands back the SAME object. Two threads then
   share one duplicator and steal frames from each other: verified directly, the second caller's
   grab() returned None because the first had already consumed the only pending frame. This is
   why detect_text() keeps its own independent mss capture instead of taking a second DXGI
   source. Any second consumer must ask for MSS explicitly (backend=MSS).

2. **grab() returns a VIEW that is only valid until the next grab() on the same source.** That is
   mss's contract (it reuses its own buffer) and this module preserves it rather than copying to
   paper over it, because copying an 8MB BGRA frame every frame is exactly the ~4ms/frame waste
   v0.008 removed. Consume the frame - resize it, convert it, slice it - before grabbing again.
'''
import sys
import threading

import mss
import numpy as np

AUTO = "auto"   #DXGI if it is available and working, else MSS
DXGI = "dxgi"   #require DXGI; raises FrameSourceError if unavailable
MSS = "mss"     #require MSS - what a second consumer in the same process must ask for (see above)


class FrameSourceError(RuntimeError):
    """A backend could not be created, or failed in a way it cannot recover from."""


class _MssSource:
    """Full-screen (or region) capture via mss. Portable, and the fallback for everything else."""

    name = "mss"

    def __init__(self, region):
        self._region = dict(region)
        #mss 10 renamed the factory mss.mss -> mss.MSS and deprecated the old spelling ("will be
        #removed in a future release"). Prefer the new name where it exists so this file does not
        #warn today or break later, without requiring a newer mss than the rest of the project.
        self._sct = getattr(mss, "MSS", mss.mss)()

    def grab(self):
        img = self._sct.grab(self._region)
        #A view over mss's own buffer, never np.array(img) - that copies the whole ~8MB BGRA
        #frame through the array interface every frame. See the module docstring's point 2.
        return np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class _DxgiSource:
    """Windows Desktop Duplication via bettercam.

    Deliberately uses the POLLED grab() rather than bettercam's video_mode/get_latest_frame():
      - get_latest_frame() waits on an Event with NO timeout, so if bettercam's capture thread
        dies the caller blocks forever, and there is no way to time it out from outside. A silent
        hang is the one failure this project's first design priority rules out.
      - it also np.array()-copies every frame out of a ring buffer, which is the same per-frame
        8MB copy v0.008 removed from the mss path. Measured, polling was both faster and
        cheaper: 3.4 vs 5.6 CPU-ms per frame at a matched 90 FPS.
    """

    name = "dxgi"

    def __init__(self, region):
        if not sys.platform.startswith("win"):
            raise FrameSourceError("DXGI Desktop Duplication is Windows-only")
        try:
            import bettercam
        except ImportError as e:
            raise FrameSourceError(f"bettercam not installed ({e})") from e

        left, top = region["left"], region["top"]
        box = (left, top, left + region["width"], top + region["height"])
        try:
            self._cam = bettercam.create(output_idx=0, output_color="BGRA", region=box)
        except Exception as e:
            raise FrameSourceError(f"could not open a DXGI duplicator: {e}") from e
        if self._cam is None:
            raise FrameSourceError("bettercam.create returned no camera")
        self._last = None

    def grab(self):
        #DXGI only hands over a frame when the desktop actually CHANGED since the last call, so
        #None is the normal, expected answer on a static screen - not an error. Re-serving the
        #previous frame keeps the pipeline's cadence steady instead of stalling it; a screen that
        #has not changed has, by definition, nothing new on it to detect.
        frame = self._cam.grab()
        if frame is None:
            return self._last   #None only until the very first frame arrives; caller retries
        self._last = frame
        return frame

    def close(self):
        try:
            self._cam.release()
        except Exception:
            pass


class FrameSource:
    """A frame source that degrades instead of failing.

    Wraps a backend and, if that backend throws at runtime, falls back to mss permanently and
    says so once. DXGI raises DXGI_ERROR_ACCESS_LOST on a resolution change, a UAC prompt, a
    driver reset or a fullscreen transition; bettercam rebuilds its duplicator for some of those,
    but not every path is covered and an uncaught one would take the capture thread down with it.
    The pipeline keeps running at the old speed rather than dying - stability outranks
    performance, in that order, per CLAUDE.md.
    """

    def __init__(self, region, backend=AUTO):
        self._region = dict(region)
        self._lock = threading.Lock()
        self._degraded = False
        self._source = self._open(backend)

    def _open(self, backend):
        if backend == MSS:
            return _MssSource(self._region)
        if backend == DXGI:
            return _DxgiSource(self._region)
        try:
            source = _DxgiSource(self._region)
            print("frame source: DXGI Desktop Duplication (fast path)")
            return source
        except FrameSourceError as e:
            print(f"frame source: mss ({e})")
            return _MssSource(self._region)

    @property
    def name(self):
        return self._source.name

    def grab(self):
        """One frame as a BGRA array, or None if the backend has nothing yet (caller retries).

        The array is only valid until the next grab() on this source - see the module docstring.
        """
        try:
            return self._source.grab()
        except Exception as e:
            with self._lock:
                if self._degraded:
                    raise
                self._degraded = True
                print(f"WARNING: {self._source.name} capture failed ({e}); falling back to mss "
                      f"for the rest of this session.")
                try:
                    self._source.close()
                except Exception:
                    pass
                self._source = _MssSource(self._region)
            return self._source.grab()

    def close(self):
        self._source.close()
