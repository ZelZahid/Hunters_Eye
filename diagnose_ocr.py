"""Shows exactly what Tesseract reads off the real game, and why each line did or didn't match.

Run this when an item is visibly on screen but isn't being detected. "It isn't detecting" has
three completely different causes that look identical from the outside:

  1. Tesseract never saw the text at all      -> the line simply won't appear below
  2. Tesseract read it, but misread characters -> the line appears, with a FAILED word
  3. Tesseract read it correctly, but no target matches -> the line appears, closest target shown

Only the third is a matching-cutoff problem. The first is a capture/resolution/contrast problem
and no amount of cutoff tuning will fix it; the second is a font problem, fixed by tolerance or
by adding a look-alike entry. Guessing between them wastes a lot of time, hence this script.

    python diagnose_ocr.py              # 5s countdown, then one scan
    python diagnose_ocr.py --delay 10   # more time to alt-tab and get an item on screen
    python diagnose_ocr.py --repeat 5   # several scans, e.g. to see how stable a read is

It captures through exactly the same path as main.py's detect_text() - same monitor, same
OCR_CAPTURE_SCALE, same viewport crop - so what it reports is what the real pipeline sees, not
an approximation of it. The captured viewport is saved as a PNG so the actual pixels Tesseract
was given can be inspected afterwards.
"""
import argparse
import time
from pathlib import Path

import cv2 as cv
import mss
import numpy as np

import main as pipeline
import text_detection


def capture_viewport(sct):
    """The exact frame main.py hands to the OCR call."""
    frame = np.array(sct.grab(pipeline.monitor_area))
    if frame.shape[2] == 4:
        frame = cv.cvtColor(frame, cv.COLOR_BGRA2BGR)
    frame = cv.resize(frame, (0, 0), fx=pipeline.OCR_CAPTURE_SCALE, fy=pipeline.OCR_CAPTURE_SCALE)
    height = frame.shape[0]
    y0 = int(height * pipeline.VIEWPORT_TOP_MARGIN)
    y1 = int(height * (1 - pipeline.VIEWPORT_BOTTOM_MARGIN))
    return frame[y0:y1, :], y0


def read_words(prepared):
    if text_detection._tesserocr_api is not None:
        return text_detection._get_words_tesserocr(prepared), "tesserocr"
    if text_detection._pytesseract_available:
        return text_detection._get_words_pytesseract(prepared), "pytesseract"
    return [], "NONE - Tesseract is not installed"


def describe_line(line_words, targets, cutoff):
    """For one OCR'd line: the best target over every contiguous word-run, matched or not."""
    best = None  # (ratio, text, name, accepted)
    words = [w[0] for w in line_words]
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            text = text_detection._clean_text(" ".join(words[start:end]))
            if not text:
                continue
            for name in targets:
                ratio = text_detection._match_ratio(text, name, cutoff)
                accepted = ratio is not None
                score = ratio if accepted else text_detection.difflib.SequenceMatcher(None, text, name).ratio()
                if best is None or (accepted, score) > (best[3], best[0]):
                    best = (score, text, name, accepted)
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay", type=float, default=5.0, help="countdown before the first scan")
    parser.add_argument("--repeat", type=int, default=1, help="number of scans to run")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between scans")
    args = parser.parse_args()

    targets = text_detection.load_target_items(pipeline.ASSETS_DIR / "targets.txt")
    reported = {n for n, i in targets.items() if not i["ignore"]}
    print(f"{len(targets)} target entries loaded ({len(reported)} reported, "
          f"{len(targets) - len(reported)} ignore/look-alike)")

    print(f"\nSwitch to the game and put an item on the ground. Scanning in {args.delay:.0f}s...")
    time.sleep(args.delay)

    out_dir = Path(__file__).resolve().parent
    with mss.mss() as sct:
        for scan in range(1, args.repeat + 1):
            viewport, y_offset = capture_viewport(sct)
            #Exactly what find_text_matches() does, so this reports what the pipeline really sees.
            prepared = text_detection._preprocess(viewport, text_detection.PREPROCESS_AUTO)
            binarized = prepared.ndim == 2
            t0 = time.perf_counter()
            words, backend = read_words(prepared)
            elapsed = time.perf_counter() - t0

            print(f"\n{'=' * 78}\nSCAN {scan}/{args.repeat}   backend={backend}   "
                  f"{elapsed:.2f}s   viewport={viewport.shape[1]}x{viewport.shape[0]}px "
                  f"(cropped {y_offset}px off the top)")
            print(f"preprocessing: {'binarized (frame read as light-text-on-dark)' if binarized else 'none (frame read as dark-text-on-light)'}\n{'=' * 78}")

            lines = text_detection._group_words_by_line(words)
            if not lines:
                print("  Tesseract read NOTHING on this frame.")
                print("  -> not a matching problem. Check the item was actually on screen, and")
                print("     that it isn't inside the cropped-off HUD margins (VIEWPORT_TOP_MARGIN")
                print("     / VIEWPORT_BOTTOM_MARGIN in main.py).")
            for line_words in lines:
                raw = " ".join(w[0] for w in line_words)
                best = describe_line(line_words, targets, 0.75)
                if best is None:
                    print(f"  read {raw!r}\n      -> no target came close")
                    continue
                score, text, name, accepted = best
                if accepted:
                    kind = ("IGNORED (look-alike, by design)" if targets[name]["ignore"]
                            else "MATCH + AUTO-COLLECT" if targets[name]["to_collect"] else "MATCH (box only)")
                    print(f"  read {raw!r}\n      -> {name!r} {score:.3f}  {kind}")
                else:
                    print(f"  read {raw!r}\n      -> REJECTED, closest was {name!r}: "
                          f"{text_detection._explain(text, name, 0.75)}")

            raw_path = out_dir / f"ocr_diagnostic_{scan}.png"
            cv.imwrite(str(raw_path), viewport)
            prep_path = out_dir / f"ocr_diagnostic_{scan}_prepared.png"
            cv.imwrite(str(prep_path), prepared)
            print(f"\n  {raw_path.name}          - the captured viewport")
            print(f"  {prep_path.name} - the exact pixels Tesseract got")
            print("  If a label is legible in the first and gone in the second, preprocessing is")
            print("  eating it: lower text_detection.BRIGHT_TEXT_THRESHOLD.")

            if scan < args.repeat:
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
