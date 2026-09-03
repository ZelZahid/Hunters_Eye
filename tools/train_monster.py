"""Turn the labelled dataset into assets/monsters/monsters.onnx.

The build step from docs/monster_detection_plan.txt section 5, and the last piece of step 3.
Run by hand, never by the pipeline - the same category as calibrate_meters.py.

    python tools/train_monster.py                    # train, then export ONNX
    python tools/train_monster.py --check            # validate the dataset and STOP
    python tools/train_monster.py --epochs 200
    python tools/train_monster.py --imgsz 320        # faster, worse on small monsters

    assets/monsters/_dataset/{images,labels,classes.txt}
        -> train  ->  assets/monsters/monsters.onnx
                      assets/monsters/monsters.json

ONE MODEL, ALL CLASSES. Every class in classes.txt trains into a single network, because the
backbone is ~99% of the cost and is identical whether it knows 1 class or 500 - only the head
grows, by one number per class per candidate location. That is what keeps the frame cost flat as
the monster list grows. See section 3 of the plan.

ULTRALYTICS AND TORCH ARE DEVELOPMENT-ONLY. They live here and nowhere else. The pipeline
depends on onnxruntime alone (~12MB model, no torch), which is why this exports ONNX rather than
handing main.py a .pt file. Do not import this module from anything that runs during play.

IT CHECKS THE DATASET BEFORE TRAINING, AND REFUSES ON ANYTHING FATAL. Training silently succeeds
on bad labels - it just learns the wrong thing - and you find out an hour later by watching a
model box torches. Every check below exists because its failure is invisible in the loss curve:
a label out of range, a class id with no entry in classes.txt, an image with no label file at
all (which YOLO reads as "nothing here", teaching the model that visible monsters are
background). --check runs them and stops, so the data can be fixed before committing the time.

ON --imgsz, WHICH MATTERS MORE HERE THAN THE BENCHMARK SUGGESTS. The benchmark says 320 is 4x
cheaper than 640, and in isolation that argues for 320. But these monsters are SMALL: measured
on a real frame, a Defiled Warrior is about 69x105 px in a 1920x1080 capture. Letterboxed to
640 that is roughly 23x35 px - comfortable. To 320 it is 11x17 px, which is close to the
smallest thing a YOLO head can resolve (its finest stride is 8 px, so that object is about two
cells across). Speed you can buy back by inferring less often; resolution you cannot buy back at
all. So the default is 640 and the tradeoff is deliberate rather than inherited.
"""
import argparse
import json
import random
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONSTERS_DIR = REPO_ROOT / "assets" / "monsters"
DATASET_DIR = MONSTERS_DIR / "_dataset"
IMAGES_DIR = DATASET_DIR / "images"
LABELS_DIR = DATASET_DIR / "labels"
CLASSES_FILE = DATASET_DIR / "classes.txt"

OUT_MODEL = MONSTERS_DIR / "monsters.onnx"
OUT_CONFIG = MONSTERS_DIR / "monsters.json"

BASE_MODEL = "yolo11n.pt"   #benchmark winner: faster than yolov8n at every size, 2026-09-03
DEFAULT_IMGSZ = 640
DEFAULT_EPOCHS = 100
VAL_FRACTION = 0.2
SPLIT_SEED = 1234           #fixed, so re-running gives the same split and runs are comparable

#Below this, a model will memorise rather than generalise. Not fatal - a proof-of-loop run on a
#handful of frames is a legitimate thing to do - but it must be said out loud.
MIN_SENSIBLE_IMAGES = 50

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def require(module_name, hint):
    try:
        return __import__(module_name)
    except ImportError:
        print("\ntrain_monster needs '%s', which is not installed (development-only).\n"
              % module_name)
        print("    %s\n" % hint)
        sys.exit(1)


def load_classes():
    if not CLASSES_FILE.exists():
        return []
    return [line.strip() for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def check_dataset(classes):
    """Everything that makes a training run worthless. Returns (fatal, warnings, stats)."""
    fatal, warnings = [], []

    images = sorted(p for p in IMAGES_DIR.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES) \
        if IMAGES_DIR.exists() else []
    if not images:
        fatal.append("no images in %s - run tools/capture_frames.py" % IMAGES_DIR)
        return fatal, warnings, {}
    if not classes:
        fatal.append("classes.txt is missing or empty - run tools/autolabel.py")
        return fatal, warnings, {}

    per_class = {name: 0 for name in classes}
    labelled = empty = missing = 0
    boxes_total = 0

    for image in images:
        label = LABELS_DIR / (image.stem + ".txt")
        if not label.exists():
            missing += 1
            continue
        lines = [l.strip() for l in label.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            empty += 1        #a legitimate negative: a scene with nothing in it
            continue
        labelled += 1
        for number, line in enumerate(lines, 1):
            parts = line.split()
            if len(parts) != 5:
                fatal.append("%s line %d: expected 5 fields, got %d" %
                             (label.name, number, len(parts)))
                continue
            try:
                class_id = int(parts[0])
                values = [float(v) for v in parts[1:]]
            except ValueError:
                fatal.append("%s line %d: not numeric" % (label.name, number))
                continue
            if not 0 <= class_id < len(classes):
                #Silently trains a nonexistent class, or shifts every later class's meaning.
                fatal.append("%s line %d: class id %d but classes.txt has %d entries"
                             % (label.name, number, class_id, len(classes)))
                continue
            if any(not 0.0 <= v <= 1.0 for v in values):
                #YOLO coordinates are normalised. Out-of-range means someone wrote pixels, and
                #the box silently lands somewhere else entirely.
                fatal.append("%s line %d: coordinates outside 0..1 (%s) - these must be "
                             "normalised, not pixels" % (label.name, number, parts[1:]))
                continue
            if values[2] <= 0 or values[3] <= 0:
                fatal.append("%s line %d: zero-sized box" % (label.name, number))
                continue
            per_class[classes[class_id]] += 1
            boxes_total += 1

    if missing:
        #Not the same as an empty file. An empty file SAYS "nothing here"; a missing one means
        #the frame was never labelled, and YOLO reads it as background either way - so an
        #unlabelled frame full of monsters actively teaches the model to ignore them.
        warnings.append("%d image(s) have no label file at all. YOLO treats those as EMPTY "
                        "scenes, so any monsters in them teach the model to miss monsters. "
                        "Label them, or move them out of images/." % missing)
    if len(images) < MIN_SENSIBLE_IMAGES:
        warnings.append("only %d images - expect memorisation rather than generalisation. "
                        "Fine to prove the loop, not fine to trust the result." % len(images))
    for name, count in per_class.items():
        if count == 0:
            warnings.append("class %r has no boxes anywhere - it will train as a class the "
                            "model never sees an example of." % name)
    if empty and not labelled:
        fatal.append("every label file is empty - there is nothing to learn")

    stats = {"images": len(images), "labelled": labelled, "empty": empty,
             "missing": missing, "boxes": boxes_total, "per_class": per_class}
    return fatal, warnings, stats


def write_split(classes, seed):
    """Write train/val image lists plus the dataset YAML ultralytics reads.

    Lists of paths rather than a train/ val/ directory tree, so nothing is copied or symlinked -
    Windows needs elevation for symlinks (the same limitation huggingface warns about), and
    copying a few hundred frames twice is pure waste. Ultralytics finds each label by swapping
    '/images/' for '/labels/' in the image path, which is exactly how the dataset is laid out.
    """
    images = sorted(p for p in IMAGES_DIR.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    images = [p for p in images if (LABELS_DIR / (p.stem + ".txt")).exists()]

    shuffled = list(images)
    random.Random(seed).shuffle(shuffled)
    split_at = max(1, int(len(shuffled) * (1 - VAL_FRACTION)))
    train, val = shuffled[:split_at], shuffled[split_at:]
    if not val:                      #tiny dataset: validate on the training data rather than
        val = train[:]               #not at all, so the run still reports a number
    (DATASET_DIR / "train.txt").write_text(
        "\n".join(str(p.resolve()) for p in train) + "\n", encoding="utf-8")
    (DATASET_DIR / "val.txt").write_text(
        "\n".join(str(p.resolve()) for p in val) + "\n", encoding="utf-8")

    yaml_path = DATASET_DIR / "dataset.yaml"
    names = "\n".join("  %d: %s" % (i, n) for i, n in enumerate(classes))
    yaml_path.write_text(
        "path: %s\ntrain: train.txt\nval: val.txt\nnames:\n%s\n"
        % (DATASET_DIR.resolve().as_posix(), names), encoding="utf-8")

    def instances(paths):
        total = 0
        for image in paths:
            label = LABELS_DIR / (image.stem + ".txt")
            total += sum(1 for l in label.read_text(encoding="utf-8").splitlines() if l.strip())
        return total

    return yaml_path, len(train), len(val), instances(train), instances(val)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="validate the dataset and stop")
    ap.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    ap.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--base", default=BASE_MODEL)
    ap.add_argument("--seed", type=int, default=SPLIT_SEED)
    ap.add_argument("--device", default=None, help="'0' for first GPU, 'cpu' to force CPU")
    args = ap.parse_args()

    classes = load_classes()
    fatal, warnings, stats = check_dataset(classes)

    print("Dataset: %s" % DATASET_DIR)
    if stats:
        print("  %d images - %d with boxes, %d empty (negatives), %d unlabelled"
              % (stats["images"], stats["labelled"], stats["empty"], stats["missing"]))
        print("  %d boxes across %d class(es)" % (stats["boxes"], len(classes)))
        for name, count in stats["per_class"].items():
            print("      %-24s %d" % (name, count))

    for warning in warnings:
        print("\n  WARNING: %s" % warning)
    if fatal:
        print("\n  REFUSING TO TRAIN - %d fatal problem(s):" % len(fatal))
        for problem in fatal[:20]:
            print("    - %s" % problem)
        if len(fatal) > 20:
            print("    ... and %d more" % (len(fatal) - 20))
        print("\nTraining on these would silently produce a model that learned the wrong thing.")
        sys.exit(1)

    if args.check:
        print("\n--check only: dataset is usable. Remove --check to train.")
        return

    torch = require("torch", "python -m pip install torch")
    require("ultralytics", "python -m pip install ultralytics")
    from ultralytics import YOLO

    device = args.device
    if device is None:
        device = "0" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("\n  NOTE: training on CPU (torch reports no GPU). This works but is slow -"
              "\n  reckon tens of minutes for a few hundred frames. A CUDA torch build would"
              "\n  cut that a lot; it is not required.")

    yaml_path, n_train, n_val, inst_train, inst_val = write_split(classes, args.seed)
    print("\nSplit: %d train / %d val (seed %d) - %d / %d boxes"
          % (n_train, n_val, args.seed, inst_train, inst_val))
    if inst_val == 0:
        #Every validation metric is computed against the boxes in the val split, so with none
        #there mAP/precision/recall all come back 0.00 no matter how well the model learned.
        #That reads as total failure and is actually no measurement at all - worth saying,
        #because the numbers themselves cannot distinguish the two.
        print("\n  WARNING: the validation split contains NO boxes, so every metric below will"
              "\n  read 0.00 regardless of how training goes. That is not a bad result, it is"
              "\n  no result. Add more labelled frames, or re-run with a different --seed.")
    if inst_train == 0:
        print("\n  REFUSING TO TRAIN - the training split contains no boxes at all.")
        sys.exit(1)
    print("")

    model = YOLO(args.base)      #COCO-pretrained: with a small dataset, transfer learning is
                                 #doing most of the work - training from scratch would need
                                 #orders of magnitude more frames to reach the same place.
    model.train(data=str(yaml_path), epochs=args.epochs, imgsz=args.imgsz,
                batch=args.batch, device=device, seed=args.seed,
                project=str(DATASET_DIR / "runs"), name="train", exist_ok=True,
                flipud=0.0)      #a game camera never turns upside down; teaching that wastes
                                 #capacity a nano model has little of

    exported = Path(model.export(format="onnx", imgsz=args.imgsz))
    OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(exported), str(OUT_MODEL))

    OUT_CONFIG.write_text(json.dumps({
        "model": OUT_MODEL.name,
        "imgsz": args.imgsz,
        "classes": classes,
        #Per class, so a rare boss and common trash can sit at different operating points -
        #they will not want the same one. Starting value only; tune against real frames.
        "thresholds": {name: 0.25 for name in classes},
        "trained_on": {"images": stats["images"], "boxes": stats["boxes"]},
        "base_model": args.base,
    }, indent=2) + "\n", encoding="utf-8")

    print("\nWrote %s" % OUT_MODEL)
    print("Wrote %s" % OUT_CONFIG)
    print("""
Before trusting it, look at the validation numbers ultralytics just printed (mAP50 in
particular) and at the plots under _dataset/runs/train/. A high score on a small dataset
usually means the split leaked - frames captured 1.5s apart are nearly the same picture,
so a random split can put near-duplicates on both sides. The honest test is a run it has
never seen: capture a fresh batch and look at the boxes.
""")


if __name__ == "__main__":
    main()
