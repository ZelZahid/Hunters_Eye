"""Draft labels with YOUR OWN trained model, so each round of labelling is mostly correcting.

    python tools/prelabel.py                      # draft every frame that has no label yet
    python tools/prelabel.py --threshold 0.3      # fewer, surer boxes
    python tools/prelabel.py --overwrite          # re-draft frames whose draft you never reviewed
    python tools/label_monsters.py                # then review: drafts open as thin "?" boxes

THE LOOP THIS EXISTS FOR. capture frames during real runs -> prelabel -> review in
label_monsters.py -> train_monster.py -> prelabel the next batch with the better model. The first
model is the hardest to get (tools/autolabel.py and hand-drawing); after that every round the
model drafts more of the boxes and the person fixes fewer. It is the cheapest answer to "I don't
want to label every monster by hand" - the measured model already has its precision (zero false
positives on a held-out pack frame); what it lacks is recall, and recall is exactly what a person
reviewing drafts adds back.

THE DRAFTS GO IN labels_draft/, NEVER labels/. The trainer reads every file in labels/ as checked
truth. A machine guess written there trains exactly as if a person had looked at it, and a wrong
box is worse than a missing one - it actively teaches the model the mistake. So a draft only
becomes a label when someone has looked at it in label_monsters.py.

FULL FRAMES, NO HUD CROP - the opposite of autolabel.py, on purpose. OWLv2 had never seen this
game and boxed the HUD gargoyles, so autolabel crops them away. This model was TRAINED on whole
frames, HUD included, and learned the gargoyles are background; cropping would hand it an image
shaped unlike anything it trained on (see core/monster_detection.py).

THE THRESHOLD IS A DRAFTING KNOB, NOT THE DETECTOR'S. A missed monster costs a person drawing one
box; a wrong box costs one right-click. So the default sits a little below the 0.25 the live
detector uses, trading a few extra boxes to delete for fewer to draw. Raise it if the drafts are
mostly junk; lower it if they keep missing monsters you can see.

Frames that already have a REAL label are never touched, with or without --overwrite. That is a
person's work, and nothing here should be able to replace it.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2 as cv

import label_monsters as lm

DEFAULT_THRESHOLD = 0.20


def frames_to_draft(frames, overwrite=False):
    """Frames with no real label, and (unless overwrite) no draft waiting for review either."""
    return [f for f in frames
            if not lm.label_path(f).exists() and (overwrite or not lm.draft_path(f).exists())]


def draft_frames(frames, detect, min_confidence, read=cv.imread, report=print):
    """Run detect() on each frame and write its boxes as a draft. Returns (frames, boxes) written.

    detect is passed in rather than imported so the tests can drive this with a fake model and no
    onnxruntime - the same reason routes/ takes its sensors as callables.
    """
    written = boxes_total = 0
    for n, path in enumerate(frames, 1):
        image = read(str(path))
        if image is None:
            report("  %s unreadable, skipped" % path.name)
            continue
        boxes = [(d.name, d.x, d.y, d.x + d.w, d.y + d.h)
                 for d in detect(image, min_confidence=min_confidence)]
        # An empty list still writes an empty draft: "the model looked and saw nothing" is worth
        # showing for review, and is how a hard negative gets made with one keypress.
        lm.write_labels(path, image.shape, boxes, lm.draft_path(path))
        written += 1
        boxes_total += len(boxes)
        report("  [%d/%d] %-18s %d boxes" % (n, len(frames), path.name, len(boxes)))
    return written, boxes_total


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="minimum confidence for a drafted box (default %.2f)" % DEFAULT_THRESHOLD)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-draft frames that already have an unreviewed draft (real labels "
                         "are never touched)")
    ap.add_argument("--limit", type=int, default=0, help="only do this many frames")
    ap.add_argument("--gpu", action="store_true", help="ask onnxruntime for CUDA")
    args = ap.parse_args()

    from core import monster_detection

    frames = sorted(p for p in lm.IMAGES_DIR.glob("*")
                    if p.suffix.lower() in lm.IMAGE_SUFFIXES) if lm.IMAGES_DIR.exists() else []
    if not frames:
        print("No frames in %s - import some with tools/label_monsters.py --import <folder>, "
              "or record them with tools/capture_frames.py." % lm.IMAGES_DIR)
        return 1

    if not monster_detection.load(prefer_gpu=args.gpu):
        print("No trained model to draft with (%s)." % monster_detection._load_error)
        print("Draw a first batch by hand or with tools/autolabel.py, then train_monster.py.")
        return 1

    todo = frames_to_draft(frames, args.overwrite)
    if args.limit:
        todo = todo[:args.limit]
    print("model classes: %s" % ", ".join(monster_detection.class_names()))
    print("%d frame(s) to draft, %d skipped (already labelled%s)\n"
          % (len(todo), len(frames) - len(todo), "" if args.overwrite else " or drafted"))
    if not todo:
        return 0

    started = time.perf_counter()
    written, boxes = draft_frames(todo, monster_detection.detect, args.threshold)
    elapsed = time.perf_counter() - started
    print("\n%d boxes drafted across %d frames in %.1fs" % (boxes, written, elapsed))
    print("drafts -> %s" % lm.DRAFTS_DIR)
    print("""
NOW REVIEW THEM:  python tools/label_monsters.py --only-unlabelled
Each drafted frame opens with thin "?" boxes. Delete wrong ones (right-click), draw any monster
the model missed, then move on - moving on saves it as a real label. Nothing trains on a draft.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
