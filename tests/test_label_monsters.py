"""tools/label_monsters.py - the label file it writes, which nothing downstream can sanity-check.

    python tests/test_label_monsters.py

WHY THIS IS TESTED AT ALL. A wrong label does not fail. It trains, it reports a loss curve, and
it teaches the model something nobody asked for - and the cost of finding that out is a training
run plus a live session wondering why the detector boxes a torch. That is the same reason
test_game_state.py exists for the meter measurement: the failure mode is a plausible-but-wrong
number, not an error. The interactive window is not tested (it needs a display and a hand); every
pure function behind it is.

The four properties, each guarding a distinct silent failure:
  - a box survives the pixel -> normalised -> pixel round trip, so what you drew is what trains
  - an EMPTY file and a MISSING file stay different answers, because they are opposite intents
  - class ids are append-only, because a shifted id rewrites the meaning of every existing label
  - a box past the frame edge is clipped to the visible part, not moved
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import label_monsters as lm

SHAPE = (1080, 1920, 3)
checks = 0
failures = []


def check(condition, description):
    global checks
    checks += 1
    if not condition:
        failures.append(description)
        print("  FAIL  %s" % description)


def sandbox(tmp):
    """Point the module at a throwaway dataset. It addresses folders through module globals."""
    lm.MONSTERS_DIR = tmp / "monsters"
    lm.DATASET_DIR = tmp / "monsters" / "_dataset"
    lm.IMAGES_DIR = lm.DATASET_DIR / "images"
    lm.LABELS_DIR = lm.DATASET_DIR / "labels"
    lm.CLASSES_FILE = lm.DATASET_DIR / "classes.txt"
    lm.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    lm.LABELS_DIR.mkdir(parents=True, exist_ok=True)
    image = lm.IMAGES_DIR / "frame_0001.png"
    image.write_bytes(b"not a real png - only the path is used")
    return image


def test_round_trip(image):
    print("\nround trip: what was drawn is what trains")
    boxes = [("defiled_warrior", 100, 200, 169, 305),     # ~69x105, a real Defiled Warrior
             ("pindle", 800, 400, 880, 520),
             ("defiled_warrior", 0, 0, 12, 14)]           # tiny, top-left corner
    lm.write_labels(image, SHAPE, boxes)
    back = lm.read_labels(image, SHAPE, lm.load_classes())
    check(back == boxes, "boxes survive pixel -> normalised -> pixel exactly (got %r)" % (back,))

    text = lm.label_path(image).read_text(encoding="utf-8")
    values = [float(v) for line in text.splitlines() for v in line.split()[1:]]
    check(all(0.0 <= v <= 1.0 for v in values),
          "every written coordinate is normalised 0..1 - pixels here are trainer-fatal")
    check(all(len(line.split()) == 5 for line in text.splitlines() if line.strip()),
          "every line is exactly 5 fields")


def test_empty_is_not_missing(image):
    print("\nan empty label and a missing label are opposite intents")
    lm.label_path(image).unlink(missing_ok=True)
    check(lm.read_labels(image, SHAPE, []) is None,
          "no label file reads as None (never labelled)")

    lm.write_labels(image, SHAPE, [])
    check(lm.label_path(image).exists(), "an empty box list still WRITES a file")
    check(lm.label_path(image).read_text(encoding="utf-8") == "", "and that file is empty")
    check(lm.read_labels(image, SHAPE, []) == [],
          "an empty file reads as [] (a hard negative: looked, found nothing)")


def test_ids_are_append_only(image):
    print("\nclass ids never shift")
    lm.CLASSES_FILE.write_text("defiled_warrior\npindle\n", encoding="utf-8")
    before = lm.load_classes()
    lm.write_labels(image, SHAPE, [("zombie", 10, 10, 90, 120), ("pindle", 200, 200, 280, 320)])
    after = lm.load_classes()
    check(after[:len(before)] == before,
          "existing classes keep their ids (%r -> %r)" % (before, after))
    check(after == ["defiled_warrior", "pindle", "zombie"], "the new class is appended last")

    ids = [int(line.split()[0]) for line in
           lm.label_path(image).read_text(encoding="utf-8").splitlines() if line.strip()]
    check(sorted(ids) == [1, 2], "written ids index into classes.txt correctly (got %r)" % ids)

    # A class merely OFFERED by a folder must not be committed until a box actually uses it.
    (lm.MONSTERS_DIR / "mephisto").mkdir(parents=True, exist_ok=True)
    check("mephisto" in lm.registered_folders(), "a folder is offered as a class")
    check("mephisto" not in lm.load_classes(),
          "but an unused class is NOT written to classes.txt")


def test_edge_clipping(image):
    print("\na monster half off screen is clipped, not moved")
    lm.CLASSES_FILE.write_text("zombie\n", encoding="utf-8")
    # Half off the left edge: visible part is x 0..30, so cx=15/1920, w=30/1920.
    lm.write_labels(image, SHAPE, [("zombie", -40, 500, 30, 620)])
    parts = lm.label_path(image).read_text(encoding="utf-8").split()
    cx, w = float(parts[1]), float(parts[3])
    check(abs(cx - 15 / 1920) < 1e-6, "centre is the visible part's centre (got %.6f)" % cx)
    check(abs(w - 30 / 1920) < 1e-6, "width is the visible part's width, not the full box "
                                     "(got %.6f, full box would be %.6f)" % (w, 70 / 1920))

    # Entirely off frame: must produce no line at all - a zero-sized box is trainer-fatal.
    lm.write_labels(image, SHAPE, [("zombie", -200, 100, -50, 260)])
    check(lm.label_path(image).read_text(encoding="utf-8").strip() == "",
          "a box entirely off frame is dropped, not written zero-sized")


def test_import_deduplicates(tmp):
    print("\nimport is by content, so re-running it is a no-op")
    source = tmp / "shots"
    source.mkdir(parents=True, exist_ok=True)
    (source / "Screenshot019.png").write_bytes(b"frame-a")
    (source / "Screenshot020.png").write_bytes(b"frame-b")

    check(lm.import_frames(source) == 2, "both new frames import")
    check(lm.import_frames(source) == 0, "the same folder imports nothing the second time")

    # Same bytes under a different name is the same frame; different bytes are not.
    (source / "renamed.png").write_bytes(b"frame-a")
    (source / "Screenshot021.png").write_bytes(b"frame-c")
    check(lm.import_frames(source) == 1, "a renamed duplicate is skipped, a new frame is taken")

    names = sorted(p.name for p in lm.IMAGES_DIR.glob("frame_*.png"))
    check(len(set(names)) == len(names), "no imported frame overwrote another")


def main():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        image = sandbox(tmp)
        test_round_trip(image)
        test_empty_is_not_missing(image)
        test_ids_are_append_only(image)
        test_edge_clipping(image)
        test_import_deduplicates(tmp)

    print("\n%d checks, %d failed" % (checks, len(failures)))
    for description in failures:
        print("  - %s" % description)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
