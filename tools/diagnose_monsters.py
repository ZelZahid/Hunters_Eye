"""Why wasn't this monster detected? - what the model actually sees, and how sure it is.

The counterpart to tools/diagnose_ocr.py, for the neural detector. Same reason for existing: "it
isn't detecting" has several causes that look identical from outside, and guessing between them
wastes a lot of time.

    python tools/diagnose_monsters.py                          # every dataset frame
    python tools/diagnose_monsters.py --image shot.png         # one picture
    python tools/diagnose_monsters.py --image shot.png --show-all   # below-threshold too
    python tools/diagnose_monsters.py --compare-labels         # model vs what you labelled
    python tools/diagnose_monsters.py --benchmark              # ms/call on a real frame

THE FOUR CAUSES THIS TELLS APART:
  1. the model never saw it        - no box at any threshold, even with --show-all. More
                                     training data; no threshold will rescue it.
  2. it saw it but scored it low   - a box appears only under --show-all. Lower that class's
                                     threshold in monsters.json, or add examples like it.
  3. it saw something else         - a box on a torch or a statue. Add empty-negative frames
                                     containing that thing (the 'e' key in label_monsters.py).
  4. it is not loaded at all       - no model built, or onnxruntime missing. Reported up front.

--compare-labels is the one worth running after every training run. It scores the model against
the labels you drew, per class, and prints the frames it did worst on. That is the difference
between "it works" and "it works on the four frames I happened to look at" - and because the
dataset frames were TRAINED on, a poor score there is damning rather than merely disappointing:
a model that cannot reproduce its own training labels has a data problem, not a capacity problem.
Held-out frames are the honest measure, so anything under tests/fixtures/ is flagged separately.
"""
import argparse
import sys
from pathlib import Path

import cv2 as cv
import numpy as np

# project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import monster_detection as md

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "assets" / "monsters" / "_dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
CLASSES_FILE = DATASET_DIR / "classes.txt"
OUTPUT_DIR = DATASET_DIR / "diagnosis"

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")

# A predicted box counts as finding a labelled one above this overlap. 0.5 is the usual
# convention and is about "did it find the monster", not "is the box pixel-perfect".
MATCH_IOU = 0.5


def iou(a, b):
    """Intersection over union of two (x, y, w, h) boxes."""
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1, bx1, by1 = ax0 + aw, ay0 + ah, bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    overlap = (ix1 - ix0) * (iy1 - iy0)
    return overlap / float(aw * ah + bw * bh - overlap)


def read_labels(image_path, shape, classes):
    """The boxes a human drew, in frame pixels. None if the frame was never labelled."""
    path = LABELS_DIR / (image_path.stem + ".txt")
    if not path.exists():
        return None
    height, width = shape[:2]
    boxes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        class_id = int(parts[0])
        cx, cy, w, h = (float(v) for v in parts[1:])
        name = classes[class_id] if 0 <= class_id < len(classes) else "class_%d" % class_id
        boxes.append((name, int((cx - w / 2) * width), int((cy - h / 2) * height),
                      int(w * width), int(h * height)))
    return boxes


def draw(frame, detections, truth=None):
    """Predictions in their class colour; hand-drawn labels, if given, as thin white boxes."""
    vis = frame.copy()
    if truth:
        for _name, x, y, w, h in truth:
            cv.rectangle(vis, (x, y), (x + w, y + h), (255, 255, 255), 1)
    for hit in detections:
        colour = (80, 220, 80) if md.class_names().index(hit.name) % 2 == 0 else (60, 120, 255)
        x, y, w, h = hit.box
        cv.rectangle(vis, (x, y), (x + w, y + h), colour, 2)
        cv.circle(vis, hit.centre, 4, colour, -1)
        text = "%s %.2f" % (hit.name, hit.confidence)
        cv.putText(vis, text, (x, max(14, y - 6)), cv.FONT_HERSHEY_SIMPLEX, 0.5,
                   (0, 0, 0), 3, cv.LINE_AA)
        cv.putText(vis, text, (x, max(14, y - 6)), cv.FONT_HERSHEY_SIMPLEX, 0.5,
                   colour, 1, cv.LINE_AA)
    return vis


def report_one(path, show_all, truth_classes, save):
    frame = cv.imread(str(path))
    if frame is None:
        print("  could not read %s" % path)
        return None

    detections = md.detect(frame, min_confidence=0.05 if show_all else None)
    truth = read_labels(path, frame.shape, truth_classes) if truth_classes else None

    thresholds = md.thresholds()
    print("\n%s  (%dx%d)" % (path.name, frame.shape[1], frame.shape[0]))
    if truth is not None:
        print("  labelled: %d box(es)" % len(truth))
    if not detections:
        print("  NOTHING DETECTED at any threshold" if show_all else
              "  nothing above threshold - re-run with --show-all to see what it nearly saw")
    for hit in detections:
        floor = thresholds.get(hit.name, md.DEFAULT_THRESHOLD)
        mark = "  " if hit.confidence >= floor else "  (below %.2f threshold) " % floor
        best = max((iou(hit.box, t[1:]) for t in truth), default=0.0) if truth else None
        overlap = "" if best is None else ("  IoU %.2f%s" % (best, "" if best >= MATCH_IOU
                                                             else "  <- matches no label"))
        print("  %-16s %.3f  at %-22s centre %s%s%s"
              % (hit.name, hit.confidence, hit.box, hit.centre, mark.rstrip(), overlap))

    if save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        target = OUTPUT_DIR / (path.stem + ".jpg")
        cv.imwrite(str(target), draw(frame, detections, truth), [cv.IMWRITE_JPEG_QUALITY, 88])
    return detections, truth


def compare_labels(frames, classes):
    """Score the model against the boxes a human drew. Per class, plus the worst frames."""
    stats = {name: {"labelled": 0, "found": 0, "spurious": 0} for name in classes}
    worst = []
    for path in frames:
        frame = cv.imread(str(path))
        if frame is None:
            continue
        truth = read_labels(path, frame.shape, classes)
        if truth is None:
            continue
        detections = md.detect(frame)
        unmatched = list(range(len(detections)))
        missed = 0
        for name, x, y, w, h in truth:
            stats.setdefault(name, {"labelled": 0, "found": 0, "spurious": 0})
            stats[name]["labelled"] += 1
            hit_index = None
            for i in unmatched:
                if detections[i].name == name and iou(detections[i].box, (x, y, w, h)) >= MATCH_IOU:
                    hit_index = i
                    break
            if hit_index is None:
                missed += 1
            else:
                stats[name]["found"] += 1
                unmatched.remove(hit_index)
        for i in unmatched:
            stats.setdefault(detections[i].name, {"labelled": 0, "found": 0, "spurious": 0})
            stats[detections[i].name]["spurious"] += 1
        worst.append((missed + len(unmatched), missed, len(unmatched), len(truth), path.name))

    print("\n--- model vs. the labels you drew (IoU >= %.1f counts as found) ---" % MATCH_IOU)
    print("%-20s %8s %8s %8s %10s" % ("class", "labelled", "found", "missed", "spurious"))
    for name, s in stats.items():
        print("%-20s %8d %8d %8d %10d"
              % (name, s["labelled"], s["found"], s["labelled"] - s["found"], s["spurious"]))
    total_labelled = sum(s["labelled"] for s in stats.values())
    total_found = sum(s["found"] for s in stats.values())
    total_spurious = sum(s["spurious"] for s in stats.values())
    if total_labelled:
        print("\nrecall %.0f%% (%d of %d found), %d box(es) with no matching label"
              % (100.0 * total_found / total_labelled, total_found, total_labelled, total_spurious))
        print("NOTE: these frames were TRAINED on, so this is the model reproducing its own "
              "answers.\n      It is a floor, not a measure of how it will do on a new screen - "
              "a poor score\n      here means a data problem. Held-out frames are the honest test.")
    worst.sort(reverse=True)
    if worst and worst[0][0]:
        print("\nworst frames:")
        for bad, missed, spurious, labelled, name in worst[:5]:
            if not bad:
                break
            print("  %-18s %d labelled, %d missed, %d spurious" % (name, labelled, missed, spurious))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", help="one image instead of the whole dataset")
    ap.add_argument("--show-all", action="store_true",
                    help="report boxes below their class threshold too - tells 'never saw it' "
                         "from 'saw it and was unsure'")
    ap.add_argument("--compare-labels", action="store_true",
                    help="score the model against the boxes you drew")
    ap.add_argument("--benchmark", action="store_true", help="ms per call on a real frame")
    ap.add_argument("--gpu", action="store_true", help="try CUDA/DirectML instead of CPU")
    ap.add_argument("--limit", type=int, default=0, help="only do this many frames")
    ap.add_argument("--no-save", action="store_true", help="do not write annotated images")
    args = ap.parse_args()

    if not md.load(prefer_gpu=args.gpu):
        print("Monster detection is not available.")
        print("  %s" % (md._load_error or "unknown reason"))
        print("\nBuild a model with:")
        print("  python tools/label_monsters.py --import assets/zelScreenshots")
        print("  python tools/label_monsters.py")
        print("  python tools/train_monster.py")
        return 1

    print("thresholds: %s" % ", ".join("%s=%.2f" % kv for kv in md.thresholds().items()))

    classes = ([line.strip() for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()] if CLASSES_FILE.exists() else [])

    if args.image:
        frames = [Path(args.image)]
    else:
        frames = sorted(p for p in IMAGES_DIR.glob("*")
                        if p.suffix.lower() in IMAGE_SUFFIXES) if IMAGES_DIR.exists() else []
        if not frames:
            print("No frames in %s - pass --image, or import some with label_monsters.py."
                  % IMAGES_DIR)
            return 1
    if args.limit:
        frames = frames[:args.limit]

    if args.benchmark:
        frame = cv.imread(str(frames[0]))
        ms = md.benchmark(frame)
        print("\n%.1f ms per call on %s (%dpx input)" % (ms, frames[0].name, md._input_size))
        for hz in (5, 8, 15, 60):
            print("   at %2d Hz: %.2f CPU cores" % (hz, ms * hz / 1000.0))
        print("\nThe pipeline runs detection on its own thread at a few Hz, so the figure that\n"
              "matters is the cores column, not the milliseconds.")
        return 0

    if args.compare_labels:
        compare_labels(frames, classes)
        return 0

    for path in frames:
        report_one(path, args.show_all, classes, not args.no_save)
    if not args.no_save:
        print("\nAnnotated frames written to %s" % OUTPUT_DIR)
        print("White boxes are what you labelled; coloured boxes are what the model found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
