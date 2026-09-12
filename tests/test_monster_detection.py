"""core/monster_detection.py - the coordinate maths and the suppression rule.

    python tests/test_monster_detection.py

WHAT IS TESTED HERE AND WHY IT IS NOT THE MODEL. Whether the network finds monsters is a
question about training data, answered by looking at pictures (tools/diagnose_monsters.py). What
is tested here is everything AROUND the network, where a bug is invisible: a letterbox mapping
that is off by the pad offset puts every box in the wrong place while every confidence score
still looks healthy, and a suppression rule that ignores class deletes a boss standing in its own
pack without reporting anything missing. Neither fails. Both just quietly give wrong answers.

THE MODEL FILE IS A BUILD ARTIFACT AND IS GITIGNORED, so these tests run against a SYNTHETIC
session rather than assets/monsters/monsters.onnx. That is deliberate: CLAUDE.md records that a
test pointed at a file which may not exist does not fail when it vanishes, it SKIPS, which is
indistinguishable from passing - and that has already silently disabled two test sections in this
repo. The real model is exercised only as an extra at the end, and its absence is reported
loudly rather than passed over.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import monster_detection as md

checks = 0
failures = []


def check(condition, description):
    global checks
    checks += 1
    if not condition:
        failures.append(description)
        print("  FAIL  %s" % description)


class FakeSession:
    """Stands in for onnxruntime. Returns a hand-built (1, 4+nc, anchors) tensor.

    Letting the test state exactly what the network "saw" is the only way to assert what the
    decoding does with it - against a real model the input to this maths is whatever the weights
    happen to produce that day.
    """

    def __init__(self, rows, num_classes):
        # rows: (cx, cy, w, h, [class scores...]) in LETTERBOXED input pixels
        self.num_classes = num_classes
        data = np.zeros((1, 4 + num_classes, max(len(rows), 1)), dtype=np.float32)
        for i, row in enumerate(rows):
            data[0, :, i] = row
        self.data = data

    def run(self, _outputs, _feed):
        return [self.data]

    def get_inputs(self):
        class _In:
            name = "images"
        return [_In()]

    def get_providers(self):
        return ["CPUExecutionProvider"]


def install(rows, names, size=640, thresholds=None):
    """Point the module at a synthetic session with a known class list."""
    md._session = FakeSession(rows, len(names))
    md._class_names = list(names)
    md._thresholds = thresholds or {n: 0.25 for n in names}
    md._input_size = size
    md._input_name = "images"
    md._load_error = None


def reset():
    md._session = None
    md._class_names = []
    md._thresholds = {}
    md._warned = False


def test_detection_contract():
    print("\nthe Detection contract - box + centre, and it unpacks like every other detector")
    hit = md.Detection("defiled_warrior", 0.8, 100, 200, 70, 140)
    check(hit.box == (100, 200, 70, 140), "box is (x, y, w, h)")
    check(hit.centre == (135, 270), "centre is the box centre, integer pixels (got %r)"
          % (hit.centre,))
    check(hit.name == "defiled_warrior" and abs(hit.confidence - 0.8) < 1e-9,
          "name and confidence read back")
    x, y, w, h, name, conf = hit
    check((x, y, w, h, name) == (100, 200, 70, 140, "defiled_warrior"),
          "unpacks as (x, y, w, h, name, confidence)")
    check(tuple(hit)[:4] == (100, 200, 70, 140),
          "the first four fields are the rectangle, like text_detection's matches")


def test_letterbox_geometry():
    print("\nletterbox: aspect preserved, and the padding is recoverable")
    frame = np.zeros((1080, 1920, 3), np.uint8)
    canvas, ratio, pad_x, pad_y = md._letterbox(frame, 640)
    check(canvas.shape == (640, 640, 3), "output is square at the model's input size")
    check(abs(ratio - 640 / 1920) < 1e-9, "ratio fits the LONG side (got %.6f)" % ratio)
    check(pad_x == 0 and pad_y == (640 - 360) // 2,
          "a 16:9 frame is padded top and bottom only (got pad_x=%d pad_y=%d)" % (pad_x, pad_y))
    check((canvas[0, 0] == md.PAD_VALUE).all(),
          "padding is YOLO's 114, not black - black is a colour the model was never padded with")

    tall = np.zeros((1000, 500, 3), np.uint8)
    _, ratio_t, pad_xt, pad_yt = md._letterbox(tall, 320)
    check(abs(ratio_t - 320 / 1000) < 1e-9 and pad_yt == 0 and pad_xt == (320 - 160) // 2,
          "a portrait frame is padded left and right only")


def test_coordinates_map_back():
    print("\na box decoded from the network lands where it was put")
    # One monster, 77x140 frame pixels at frame (1400, 300). Forward-map it into 640-letterbox
    # space by hand, hand THAT to the decoder, and require the original back.
    frame = np.zeros((1080, 1920, 3), np.uint8)
    _, ratio, pad_x, pad_y = md._letterbox(frame, 640)
    fx, fy, fw, fh = 1400, 300, 77, 140
    row = [(fx + fw / 2) * ratio + pad_x, (fy + fh / 2) * ratio + pad_y,
           fw * ratio, fh * ratio, 0.9, 0.0]
    install([row], ["defiled_warrior", "pindle"])

    found = md.detect(frame)
    check(len(found) == 1, "exactly one detection (got %d)" % len(found))
    if found:
        x, y, w, h = found[0].box
        check(abs(x - fx) <= 1 and abs(y - fy) <= 1,
              "position survives the round trip (%d,%d vs %d,%d)" % (x, y, fx, fy))
        check(abs(w - fw) <= 1 and abs(h - fh) <= 1,
              "size survives the round trip (%dx%d vs %dx%d)" % (w, h, fw, fh))
        check(found[0].name == "defiled_warrior", "the argmax class wins")
    reset()


def test_class_aware_suppression():
    print("\nsuppression is WITHIN a class - a boss standing in its pack is not deleted")
    frame = np.zeros((1080, 1920, 3), np.uint8)
    _, ratio, pad_x, pad_y = md._letterbox(frame, 640)

    def row(fx, fy, fw, fh, scores):
        return [(fx + fw / 2) * ratio + pad_x, (fy + fh / 2) * ratio + pad_y,
                fw * ratio, fh * ratio] + scores

    # Two heavily overlapping boxes in the same place: one a common minion scoring high, one the
    # rare boss scoring lower. Class-agnostic NMS deletes the boss; class-aware keeps both.
    rows = [row(1400, 300, 80, 150, [0.90, 0.0]),      # defiled_warrior, high
            row(1405, 305, 80, 150, [0.0, 0.40])]      # pindle, same spot, lower
    install(rows, ["defiled_warrior", "pindle"])
    found = md.detect(frame)
    names = sorted(h.name for h in found)
    check(names == ["defiled_warrior", "pindle"],
          "both classes survive a near-total overlap (got %r)" % (names,))

    # And within one class, a true duplicate IS collapsed.
    rows = [row(1400, 300, 80, 150, [0.90, 0.0]),
            row(1403, 302, 80, 150, [0.70, 0.0])]
    install(rows, ["defiled_warrior", "pindle"])
    found = md.detect(frame)
    check(len(found) == 1, "two boxes on one monster of the SAME class collapse to one (got %d)"
          % len(found))
    reset()


def test_thresholds_and_clipping():
    print("\nper-class thresholds, and boxes clipped to the frame")
    frame = np.zeros((1080, 1920, 3), np.uint8)
    _, ratio, pad_x, pad_y = md._letterbox(frame, 640)

    def row(fx, fy, fw, fh, scores):
        return [(fx + fw / 2) * ratio + pad_x, (fy + fh / 2) * ratio + pad_y,
                fw * ratio, fh * ratio] + scores

    rows = [row(100, 100, 80, 150, [0.30, 0.0]), row(500, 100, 80, 150, [0.0, 0.30])]
    install(rows, ["defiled_warrior", "pindle"],
            thresholds={"defiled_warrior": 0.25, "pindle": 0.50})
    found = md.detect(frame)
    check([h.name for h in found] == ["defiled_warrior"],
          "a class's own threshold decides it, not a global one (got %r)"
          % ([h.name for h in found],))

    found = md.detect(frame, min_confidence=0.1)
    check(len(found) == 2, "min_confidence overrides both, for diagnostics (got %d)" % len(found))

    # A monster half off the left edge: the box must be clipped, or a caller slicing the frame
    # with it gets an empty array.
    install([row(-40, 500, 80, 150, [0.9, 0.0])], ["defiled_warrior", "pindle"])
    found = md.detect(frame)
    check(len(found) == 1 and found[0].x >= 0 and found[0].w > 0,
          "a box running past the frame edge is clipped, not dropped or left negative")
    check(found[0].x + found[0].w <= 1920 and found[0].y + found[0].h <= 1080,
          "and never extends past the far edge either")
    reset()


def test_degrades_without_a_model():
    print("\nno model and no onnxruntime are normal states, not crashes")
    reset()
    check(md.available() is False, "available() is False with nothing loaded")
    check(md.detect(np.zeros((100, 100, 3), np.uint8)) == [],
          "detect() returns [] rather than raising")
    check(md.load(model_dir=Path(__file__).parent / "no_such_folder") is False,
          "load() on a missing folder returns False rather than raising")
    check(md.available() is False, "and leaves the module unavailable")
    install([], ["a"])
    check(md.detect(None) == [] and md.detect(np.zeros((0, 0, 3), np.uint8)) == [],
          "an empty or missing frame returns [] rather than raising")
    reset()


def test_real_model_if_present():
    """The real model, if it has been built. Its ABSENCE is reported, never silently skipped."""
    print("\nthe real model (a build artifact - absent is a valid checkout, not a pass)")
    if not md.load():
        print("  NOT RUN: no model built yet. Run tools/label_monsters.py then "
              "tools/train_monster.py.")
        print("  (this is not a failure, but it means nothing below was checked)")
        return
    import cv2 as cv
    fixture = Path(__file__).resolve().parent / "fixtures" / "pindle_pack.png"
    if not fixture.exists():
        print("  NOT RUN: %s is missing." % fixture.name)
        return
    frame = cv.imread(str(fixture))
    found = md.detect(frame)
    check(len(found) > 0, "the model finds something in a real held-out pack frame")
    check(all(0 <= h.x < frame.shape[1] and 0 <= h.y < frame.shape[0] for h in found),
          "every box lands inside the frame")
    check(all(h.name in md.class_names() for h in found), "every name is a trained class")
    check(found == sorted(found, key=lambda h: h.confidence, reverse=True),
          "results are ordered by confidence, highest first")
    print("  %d detection(s): %s" % (len(found),
                                     ", ".join("%s %.2f" % (h.name, h.confidence)
                                               for h in found[:6])))


def main():
    test_detection_contract()
    test_letterbox_geometry()
    test_coordinates_map_back()
    test_class_aware_suppression()
    test_thresholds_and_clipping()
    test_degrades_without_a_model()
    test_real_model_if_present()

    print("\n%d checks, %d failed" % (checks, len(failures)))
    for description in failures:
        print("  - %s" % description)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
