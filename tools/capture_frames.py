"""Record gameplay frames to train a monster detector on.

Step 3 of docs/monster_detection_plan.txt: the first real monster end to end. Nothing can be
trained without frames, and there was no way to collect them, so this is the first piece.

    python tools/capture_frames.py                       # every 1.5s until you press End
    python tools/capture_frames.py --interval 3          # slower, more variety per frame
    python tools/capture_frames.py --hotkey              # F9 grabs one frame, End quits
    python tools/capture_frames.py --limit 200           # stop after 200 frames

Frames land in assets/monsters/_dataset/images/ as full-resolution stills. They are NOT sorted
into per-monster folders, and that is deliberate - see "why two folder shapes" below.

WHY TWO FOLDER SHAPES, which is the thing to understand before using this:

  assets/monsters/<class>/    exemplar images of one monster. This is the user-facing
                             registration surface: make a folder, drop pictures of the thing in
                             it. One folder = one class.
  assets/monsters/_dataset/   full frames plus one label file each, which is what actually
                             trains a model.

They are not redundant. A single gameplay frame usually contains SEVERAL kinds of monster at
once, so it cannot live in any one class folder - YOLO wants one label file per frame listing
everything visible in it, with its class. The per-class folders answer "what does a Fallen look
like"; the dataset answers "here is a scene, and here is everything in it". This tool fills the
second. tools/autolabel.py drafts the label files; a human corrects them.

TWO DELIBERATE CHOICES:

  It uses mss, NOT the DXGI path in core/frame_source.py, even though DXGI is much faster.
  A DXGI source is a per-output SINGLETON and asking for a second one does not fail - it hands
  back the first and the two consumers then steal frames from each other (CLAUDE.md, and
  Error_history #39). Capturing while main.py is running is exactly the normal case here, so the
  portable backend is the correct one. Speed is irrelevant when grabbing every 1.5 seconds.

  It SKIPS near-identical frames. Standing still in town for a minute would otherwise produce
  forty copies of one picture, which teaches a model nothing and costs real labelling effort.
  Variety is what a small dataset needs; volume is not. --difference tunes the bar, and the
  skipped count is reported so a run that captured nothing is obvious rather than silent.

Nothing here clicks, types, or presses a key in the game - it only looks. `End` stops it, the
same key main.py uses, and the count so far is printed on exit.
"""
import argparse
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np

try:
    import mss
except ImportError:
    print("capture_frames needs mss:  python -m pip install mss")
    sys.exit(1)

try:
    import keyboard
except ImportError:
    keyboard = None      #hotkey mode and the End key need it; interval mode still works

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "assets" / "monsters" / "_dataset"
IMAGES_DIR = DATASET_DIR / "images"

#Fraction of pixels that must differ by more than NOISE_LEVEL for a frame to count as new.
#Tuned to be forgiving: torches flicker and snow falls in Diablo II even when nothing is
#happening, so a strict bar would treat a genuinely static scene as fresh every time.
DEFAULT_DIFFERENCE = 0.02
NOISE_LEVEL = 18          #per-pixel grey delta below this is flicker, not movement

#Compared at low resolution: this only has to answer "is this basically the same picture", and
#doing it on full frames would cost more than the capture.
COMPARE_WIDTH = 320


def thumbnail(frame_bgr):
    """A small grey copy, for the is-this-the-same-picture test."""
    h, w = frame_bgr.shape[:2]
    small = cv.resize(frame_bgr, (COMPARE_WIDTH, max(1, int(h * COMPARE_WIDTH / w))),
                      interpolation=cv.INTER_AREA)
    return cv.cvtColor(small, cv.COLOR_BGR2GRAY)


def changed_enough(previous, current, threshold):
    """True if `current` differs from `previous` by more than `threshold` of its pixels."""
    if previous is None:
        return True
    delta = cv.absdiff(previous, current)
    moved = np.count_nonzero(delta > NOISE_LEVEL)
    return (moved / delta.size) >= threshold


def next_index(images_dir):
    """Carry on numbering from whatever is already there, so a second run does not overwrite."""
    highest = 0
    for path in images_dir.glob("frame_*.*"):
        try:
            highest = max(highest, int(path.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue          #a file someone renamed by hand; ignore it rather than crash
    return highest + 1


def grab(sct, monitor):
    """One full-resolution BGR frame."""
    raw = sct.grab(monitor)
    #np.frombuffer over the buffer, never np.array - the latter copies the whole BGRA frame
    #through the array interface every time (CLAUDE.md's measured-performance notes). It is a
    #view over memory mss may reuse, so it is converted (and thus copied) immediately below.
    frame = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
    return cv.cvtColor(frame, cv.COLOR_BGRA2BGR)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=1.5, help="seconds between frames")
    ap.add_argument("--hotkey", action="store_true", help="F9 grabs one frame instead of a timer")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many (0 = unlimited)")
    ap.add_argument("--delay", type=int, default=6, help="countdown before starting")
    ap.add_argument("--difference", type=float, default=DEFAULT_DIFFERENCE,
                    help="fraction of pixels that must change for a frame to be kept")
    ap.add_argument("--png", action="store_true",
                    help="save lossless PNG instead of JPEG (about 4x the disk for no real "
                         "training benefit - JPEG is what every public detection dataset uses)")
    args = ap.parse_args()

    if args.hotkey and keyboard is None:
        print("--hotkey needs the keyboard package:  python -m pip install keyboard")
        sys.exit(1)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    index = next_index(IMAGES_DIR)
    extension = ".png" if args.png else ".jpg"
    write_args = [] if args.png else [cv.IMWRITE_JPEG_QUALITY, 92]

    print("Saving to %s" % IMAGES_DIR)
    if index > 1:
        print("  %d frames already there - continuing from frame_%04d" % (index - 1, index))
    print("Get the game on screen with monsters in view.")
    print("  End quits." + ("  F9 captures." if args.hotkey else
                            "  Capturing every %.1fs." % args.interval))

    for remaining in range(args.delay, 0, -1):
        print("  starting in %d... " % remaining, end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")

    #Set by the F9 hotkey; read and cleared by the loop. A hotkey callback runs on the keyboard
    #package's own listener thread, so it must not do the capture itself - that would block every
    #other hotkey, including End, for as long as the write takes.
    pending = {"grab": False}
    if args.hotkey:
        keyboard.add_hotkey("F9", lambda: pending.__setitem__("grab", True))

    kept = skipped = 0
    previous = None
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]        #[1] is the primary display; [0] is all of them
            while True:
                if keyboard is not None and keyboard.is_pressed("end"):
                    print("\nEnd pressed.")
                    break

                if args.hotkey:
                    if not pending["grab"]:
                        time.sleep(0.05)
                        continue
                    pending["grab"] = False

                frame = grab(sct, monitor)
                small = thumbnail(frame)

                #In hotkey mode the human asked for this frame, so honour it - they may well be
                #deliberately capturing two similar poses.
                if not args.hotkey and not changed_enough(previous, small, args.difference):
                    skipped += 1
                    print("  kept %d, skipped %d (unchanged)   " % (kept, skipped),
                          end="\r", flush=True)
                    time.sleep(args.interval)
                    continue

                path = IMAGES_DIR / ("frame_%04d%s" % (index, extension))
                cv.imwrite(str(path), frame, write_args)
                previous = small
                index += 1
                kept += 1
                print("  kept %d, skipped %d  -> %s   " % (kept, skipped, path.name),
                      end="\r", flush=True)

                if args.limit and kept >= args.limit:
                    print("\nReached --limit %d." % args.limit)
                    break
                if not args.hotkey:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted.")

    print("\n%d frames saved to %s (%d skipped as unchanged)" % (kept, IMAGES_DIR, skipped))
    if kept == 0:
        print("Nothing was captured. If everything was skipped, the screen was not changing -"
              "\nmove around while capturing, or lower --difference.")
    else:
        print("\nNext: draft labels for these with\n    python tools/autolabel.py")


if __name__ == "__main__":
    main()
