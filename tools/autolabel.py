"""Draft bounding-box labels for captured frames, so a human corrects instead of drawing.

Step 3 of docs/monster_detection_plan.txt, and the payoff from the section 9 experiment. OWLv2
was measured finding 9 of 11 real Defiled Warriors from a plain text prompt with no training,
once the HUD was cropped away. That is not good enough to ACT on - 0.21 on a monster against
0.15 on a torch is not a margin anything should shoot at - but it is easily good enough to draw
a first pass that a person then fixes, which is the laborious part of section 7.

    python tools/autolabel.py --class defiled_warrior
    python tools/autolabel.py --class defiled_warrior --prompt "a skeleton" "an undead warrior"
    python tools/autolabel.py --class pindle --threshold 0.15 --limit 20

Reads every frame in assets/monsters/_dataset/images/ and writes, per frame:
  labels/frame_0001.txt     YOLO format, one line per box: <class> <cx> <cy> <w> <h>, normalised
  preview/frame_0001.jpg    the same boxes drawn on, for flipping through by eye

REVIEW THE PREVIEWS. This drafts labels, it does not produce them. Roughly one box in five was
wrong in the measured run - a torch, the player character, or two boxes on one monster - and a
wrong label is worse than a missing one, because the model is explicitly taught that a torch is
a monster. The previews exist so that flipping through a few hundred frames is fast.

THE HUD IS CROPPED OUT, using main.py's OWN viewport margins rather than new ones. That crop is
what took this from unusable to useful: full-frame, the carved gargoyle beside the mana orb
scored 0.17 and a real monster also scored 0.17, and no threshold separates those. It is also
the third time this project has wanted the same crop - OCR needed it for cost, presence for
sanity, this for accuracy - so it is a property of HUDs, not a Diablo II detail.

COORDINATES ARE WRITTEN RELATIVE TO THE FULL FRAME, not the crop. The model will be trained and
run on whole frames, so a label measured inside a crop would be silently offset upward by the
top margin - about 86px at 1080p. Easy to get wrong, and it would poison every label without
looking wrong in the numbers.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "assets" / "monsters" / "_dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
PREVIEW_DIR = DATASET_DIR / "preview"
CLASSES_FILE = DATASET_DIR / "classes.txt"

MODEL = "google/owlv2-base-patch16-ensemble"

#main.py's values, restated rather than imported: this tool must run standalone, and CLAUDE.md's
#rule is that a constant may be duplicated when correctness does not depend on the two agreeing.
#It does not here - a slightly different crop just means slightly different draft boxes, which a
#human is reviewing anyway. If they drift far apart, re-derive from main.py.
VIEWPORT_TOP_MARGIN = 0.08
VIEWPORT_BOTTOM_MARGIN = 0.25

#Deliberately low. This is a DRAFT: a missed monster costs a human drawing one box, while a
#threshold high enough to be "clean" would miss most of them and defeat the point.
DEFAULT_THRESHOLD = 0.12
NMS_THRESHOLD = 0.45


def require(module_name, hint):
    try:
        return __import__(module_name)
    except ImportError:
        print("\nautolabel needs '%s', which is not installed (development-only).\n" % module_name)
        print("    %s\n" % hint)
        sys.exit(1)


def default_prompts(class_name):
    """Turn a folder name into something a natural-language model has a chance with.

    'defiled_warrior' -> 'a defiled warrior'. Crude on purpose: the measured result came from
    generic prompts ("a skeleton", "an undead warrior"), not from a clever one, and a made-up
    game proper noun like 'pindle' means nothing to a model trained on photographs. Pass
    --prompt explicitly whenever the class name is not an ordinary English description.
    """
    words = class_name.replace("_", " ").strip()
    article = "an" if words[:1].lower() in "aeiou" else "a"
    return ["%s %s" % (article, words)]


def deduplicate(boxes, scores):
    """Collapse overlapping boxes. OWLv2 happily returns several per monster.

    cv.dnn.NMSBoxes, not cv.groupRectangles - OpenCV 5 removed the latter from its Python
    bindings (Error_history.txt #1), and this project already standardised on the former.
    """
    if not boxes:
        return []
    xywh = [[x0, y0, x1 - x0, y1 - y0] for (x0, y0, x1, y1) in boxes]
    keep = cv.dnn.NMSBoxes(xywh, scores, DEFAULT_THRESHOLD * 0.5, NMS_THRESHOLD)
    return [int(i) for i in np.array(keep).flatten()] if len(keep) else []


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--class", dest="class_name", required=True,
                    help="class these boxes belong to, e.g. defiled_warrior")
    ap.add_argument("--prompt", nargs="+", help="text prompts (default: from the class name)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--limit", type=int, default=0, help="only do this many frames")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--overwrite", action="store_true",
                    help="re-label frames that already have a label file (default: skip them, "
                         "so a human's corrections are never silently thrown away)")
    args = ap.parse_args()

    frames = sorted(p for p in IMAGES_DIR.glob("frame_*.*")
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not frames:
        print("No frames in %s - run tools/capture_frames.py first." % IMAGES_DIR)
        sys.exit(1)

    torch = require("torch", "python -m pip install torch")
    require("transformers", "python -m pip install transformers pillow scipy")
    from PIL import Image
    from transformers import AutoProcessor, Owlv2ForObjectDetection

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    prompts = args.prompt or default_prompts(args.class_name)

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    #classes.txt is the class-id mapping the labels refer to. Append-only: an existing class must
    #keep its id, or every label file already written silently changes meaning.
    classes = (CLASSES_FILE.read_text(encoding="utf-8").split()
               if CLASSES_FILE.exists() else [])
    if args.class_name not in classes:
        classes.append(args.class_name)
        CLASSES_FILE.write_text("\n".join(classes) + "\n", encoding="utf-8")
    class_id = classes.index(args.class_name)

    print("class %r -> id %d" % (args.class_name, class_id))
    print("prompts: %s" % ", ".join(repr(p) for p in prompts))
    print("loading %s on %s..." % (MODEL, device))
    processor = AutoProcessor.from_pretrained(MODEL)
    model = Owlv2ForObjectDetection.from_pretrained(MODEL).to(device)
    model.eval()

    todo = [f for f in frames if args.overwrite or not (LABELS_DIR / (f.stem + ".txt")).exists()]
    if args.limit:
        todo = todo[:args.limit]
    print("%d frames to label (%d already done)\n" % (len(todo), len(frames) - len(todo)))

    started = time.perf_counter()
    total_boxes = 0
    for n, path in enumerate(todo, 1):
        frame = cv.imread(str(path))
        if frame is None:
            print("  %s unreadable, skipped" % path.name)
            continue
        h, w = frame.shape[:2]

        top = int(h * VIEWPORT_TOP_MARGIN)
        bottom = int(h * (1 - VIEWPORT_BOTTOM_MARGIN))
        viewport = frame[top:bottom]

        pil = Image.fromarray(cv.cvtColor(viewport, cv.COLOR_BGR2RGB))
        inputs = processor(text=[prompts], images=pil, return_tensors="pt")
        inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model(**inputs)

        sizes = torch.tensor([[viewport.shape[0], viewport.shape[1]]]).to(device)
        post = getattr(processor, "post_process_grounded_object_detection",
                       getattr(processor, "post_process_object_detection", None))
        results = post(outputs=outputs, threshold=args.threshold, target_sizes=sizes)[0]

        boxes = [[float(v) for v in b] for b in results["boxes"].tolist()]
        scores = [float(s) for s in results["scores"].tolist()]
        kept = deduplicate(boxes, scores)

        preview = frame.copy()
        lines = []
        for i in kept:
            x0, y0, x1, y1 = boxes[i]
            #BACK TO FULL-FRAME COORDINATES. The model saw the crop, the label describes the
            #whole frame, so the top margin goes back on before anything is normalised.
            y0 += top
            y1 += top
            x0, x1 = max(0.0, x0), min(float(w), x1)
            y0, y1 = max(0.0, y0), min(float(h), y1)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            lines.append("%d %.6f %.6f %.6f %.6f" % (
                class_id, ((x0 + x1) / 2) / w, ((y0 + y1) / 2) / h, (x1 - x0) / w, (y1 - y0) / h))
            cv.rectangle(preview, (int(x0), int(y0)), (int(x1), int(y1)), (0, 255, 0), 2)
            cv.putText(preview, "%.2f" % scores[i], (int(x0) + 2, int(y0) - 4),
                       cv.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv.LINE_AA)

        #The viewport band, so a review can see instantly whether a miss was outside the crop.
        cv.rectangle(preview, (0, top), (w - 1, bottom), (90, 90, 90), 1)

        (LABELS_DIR / (path.stem + ".txt")).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        cv.imwrite(str(PREVIEW_DIR / (path.stem + ".jpg")), preview,
                   [cv.IMWRITE_JPEG_QUALITY, 88])
        total_boxes += len(lines)
        print("  [%d/%d] %-18s %d boxes" % (n, len(todo), path.name, len(lines)))

    elapsed = time.perf_counter() - started
    print("\n%d boxes across %d frames in %.1fs (%.2fs/frame)"
          % (total_boxes, len(todo), elapsed, elapsed / max(1, len(todo))))
    print("labels  -> %s" % LABELS_DIR)
    print("preview -> %s" % PREVIEW_DIR)
    print("""
NOW REVIEW THE PREVIEWS before training on any of this. Flip through them and expect to
fix roughly one box in five: a torch or the player character boxed as a monster, two boxes
on one monster, or a monster missed entirely. Delete a wrong line from the .txt, or delete
the .txt and draw that frame by hand. A wrong label actively teaches the model the wrong
thing, so it is worse than no label at all.

Frames with an empty .txt are legitimate NEGATIVES - a scene with no monsters is useful
training data and should be kept, not deleted.
""")


if __name__ == "__main__":
    main()
