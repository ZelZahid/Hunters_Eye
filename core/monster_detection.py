"""Neural object detection: find the things a model was trained on, and say where they are.

The fifth detector, and the first one that LEARNED what it is looking for rather than being told.
Template matching is handed a picture, OCR a word list, game_state a colour range, presence a
reference image - each is a rule someone wrote. This is handed a trained model and a class list,
which is what makes it the only one that can find a thing that never looks the same twice: an
animated 3D creature that idles, walks, attacks, turns and gets knocked back.

    import monster_detection
    monster_detection.load()                        # once, at startup
    for hit in monster_detection.detect(frame):     # a BGR frame, any size
        print(hit.name, hit.confidence, hit.box, hit.centre)

KNOWS NOTHING ABOUT ANY GAME, per CLAUDE.md's detector-independence rule. It imports nothing from
the other detectors and none of them import it. It is handed a BGR frame and returns matches. The
class names come from assets/monsters/monsters.json, which tools/train_monster.py wrote from
folder names - so this same module pointed at a model trained on people is the security-robot
project's person detector, with no code change. That is the whole point of the seam.

RUNTIME DEPENDENCY IS onnxruntime ONLY. ultralytics and torch build the model in tools/ and must
never be imported here - they are hundreds of megabytes and this has to run on modest hardware.
Everything degrades to "no detections" with one warning if onnxruntime is missing or the model
has not been built, exactly as text_detection.py does without Tesseract: a missing optional
detector must never stop the pipeline.

INFERENCE IS FULL-FRAME, DELIBERATELY, AND THIS IS THE ONE PLACE THIS PROJECT DOES NOT CROP THE
HUD. Every other detector wanted the viewport crop - OCR for cost, presence for sanity, the
zero-shot auto-labeller for accuracy. This one must not, because the model was TRAINED on whole
frames: tools/label_monsters.py writes full-frame coordinates and train_monster.py feeds whole
images, so cropping at inference would be a train/test mismatch, shifting every coordinate and
changing the letterbox scale. It costs nothing to skip the crop, and it buys something measured:
with HUD pixels present in training as unlabelled background, the model learned the carved
gargoyles beside the orbs are not monsters - the exact false positive that made the zero-shot
experiment unusable. A negative example only teaches if the model sees it.

NMS IS CLASS-AWARE, which matters more here than it looks. A super-unique boss stands IN its
pack, so its box overlaps its minions' boxes heavily. Class-agnostic NMS would treat that overlap
as a duplicate and delete one of them - and the one it deletes is whichever scored lower, which
for a rare class with few training examples is usually the boss. Suppression therefore happens
only WITHIN a class.

RATE AND THREADING ARE THE CALLER'S JOB, not this module's. detect() is a plain synchronous call.
main.py runs it on its own thread at a few Hz and publishes to a shared variable, the pattern OCR
already proved - that is a pipeline decision, and baking it in here would make this module
untestable and useless to a caller that wants one detection from one still image.
"""
import json
import time
from pathlib import Path

import cv2 as cv
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO_ROOT / "assets" / "monsters"
CONFIG_FILE = MODEL_DIR / "monsters.json"

# Letterbox padding. 114 is what YOLO trains with; a different value is a different image to the
# model than any it ever saw, in the border region.
PAD_VALUE = 114

# Overlap above which two boxes OF THE SAME CLASS are one detection. 0.45 is the YOLO default and
# is deliberately loose here: monsters in a pack genuinely overlap, and a tighter value merges
# two real monsters into one box, which reads downstream as "one target" and leaves the other
# alive. Erring toward two boxes for one monster is the cheaper mistake - a second click.
NMS_IOU = 0.45

# Used when monsters.json names no threshold for a class. train_monster.py writes 0.25 per class,
# the YOLO default, and per-class values are the tuning knob: a rare boss and common trash do not
# want the same operating point.
DEFAULT_THRESHOLD = 0.25

_session = None
_class_names = []
_thresholds = {}
_input_size = 640
_input_name = None
_load_error = None
_warned = False


class Detection(tuple):
    """One found thing: the project's standard match contract, box + centre.

    A tuple subclass so it unpacks like every other detector's result ((x, y, w, h, name, conf))
    while still carrying the named access and the centre point that automation actually uses.
    """
    __slots__ = ()

    def __new__(cls, name, confidence, x, y, w, h):
        return super().__new__(cls, (x, y, w, h, name, confidence))

    @property
    def x(self):
        return self[0]

    @property
    def y(self):
        return self[1]

    @property
    def w(self):
        return self[2]

    @property
    def h(self):
        return self[3]

    @property
    def name(self):
        return self[4]

    @property
    def confidence(self):
        return self[5]

    @property
    def box(self):
        return (self[0], self[1], self[2], self[3])

    @property
    def centre(self):
        """Where a caller aims. Integer pixels, because a click is an integer pixel."""
        return (self[0] + self[2] // 2, self[1] + self[3] // 2)

    def __repr__(self):
        return "Detection(%s %.3f at %r centre %r)" % (self.name, self.confidence, self.box,
                                                       self.centre)


def load(model_dir=None, providers=None, prefer_gpu=False):
    """Load the model. Safe to call repeatedly; returns True if detection is available.

    Never raises. A missing model or a missing onnxruntime is a normal state for a checkout that
    has not trained one yet, not an error the pipeline should die on.

    CPU IS THE DEFAULT, AND A GPU IS OPT-IN (prefer_gpu, or an explicit providers list). That is
    the opposite of the usual instinct and it is what the measurements support: 35ms per call at
    640px on this CPU is 0.28 cores at the 8Hz the pipeline runs it at, so the GPU buys nothing
    the design needs. Against that, merely ASKING for CUDA on a machine that cannot provide it
    prints a wall of provider-bridge errors to stderr on every single startup and then silently
    falls back anyway - noise in an unattended program that looks like a fault and is not one.
    (This machine is exactly that case: onnxruntime 1.29 wants CUDA 13 + cuDNN 9 and has 12.9 -
    see monster_detection_plan.txt section 9.) Turn it on deliberately when a model is big enough
    to need it.
    """
    global _session, _class_names, _thresholds, _input_size, _input_name, _load_error, _warned
    if _session is not None:
        return True

    directory = Path(model_dir) if model_dir else MODEL_DIR
    config_path = directory / "monsters.json"
    if not config_path.exists():
        _load_error = ("no model at %s - build one with tools/label_monsters.py then "
                       "tools/train_monster.py" % config_path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        _load_error = "onnxruntime is not installed - python -m pip install onnxruntime"
        return False

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        model_path = directory / config.get("model", "monsters.onnx")
        if not model_path.exists():
            _load_error = "%s names %s, which does not exist" % (config_path.name, model_path.name)
            return False

        names = list(config.get("classes", []))
        if not names:
            _load_error = "%s lists no classes" % config_path.name
            return False

        # onnxruntime SILENTLY SUBSTITUTES CPU when a provider's libraries are missing rather
        # than raising - the trap that made the first benchmark report a fictional CUDA column
        # (monster_detection_plan.txt section 9). So the provider actually OBTAINED is what gets
        # reported below; get_available_providers() lists CUDA on this machine even though
        # constructing it fails, which is why that list cannot be trusted as an answer either.
        if providers:
            wanted = list(providers)
        elif prefer_gpu:
            wanted = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider",
                                  "CPUExecutionProvider") if p in ort.get_available_providers()]
        else:
            wanted = ["CPUExecutionProvider"]
        session = ort.InferenceSession(str(model_path), providers=wanted)

        _session = session
        _class_names = names
        _thresholds = {n: float(config.get("thresholds", {}).get(n, DEFAULT_THRESHOLD))
                       for n in names}
        _input_size = int(config.get("imgsz", 640))
        _input_name = session.get_inputs()[0].name
        _load_error = None
        _warned = False
        print("monster detection: %d class(es) %s at %dpx on %s"
              % (len(names), ", ".join(names), _input_size, session.get_providers()[0]))
        return True
    except Exception as exc:                      # a corrupt model must not take the pipeline down
        _load_error = "could not load %s (%s)" % (config_path.name, exc)
        _session = None
        return False


def available():
    """True if detect() can actually do anything. Callers use this to tell "nothing found" from
    "nothing was looked for" - the same distinction text_detection.ocr_available() exists for."""
    return _session is not None


def class_names():
    return list(_class_names)


def thresholds():
    return dict(_thresholds)


def set_threshold(name, value):
    """Adjust one class's operating point at runtime, for tuning against a live screen."""
    if name in _thresholds:
        _thresholds[name] = float(value)


def _letterbox(frame, size):
    """Scale to fit inside size x size preserving aspect, pad the rest. Returns the mapping back.

    Preserving aspect is not cosmetic: squashing a frame to a square changes every monster's
    proportions, and shape is most of what the model matches on. The measured cost of this step is
    real - 7.0ms of a 37.3ms call at 640px - so it resizes once and does not copy again.
    """
    height, width = frame.shape[:2]
    ratio = min(size / height, size / width)
    new_h, new_w = int(round(height * ratio)), int(round(width * ratio))
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    top, left = (size - new_h) // 2, (size - new_w) // 2
    # INTER_AREA is the right filter for shrinking (which this almost always is) and avoids the
    # aliasing INTER_LINEAR leaves on thin structures like a skeleton's limbs.
    interpolation = cv.INTER_AREA if ratio < 1.0 else cv.INTER_LINEAR
    canvas[top:top + new_h, left:left + new_w] = cv.resize(frame, (new_w, new_h),
                                                           interpolation=interpolation)
    return canvas, ratio, left, top


def detect(frame, min_confidence=None):
    """Find every trained class in a BGR frame. Returns a list of Detection, highest score first.

    Coordinates are in the frame's OWN pixels, whatever size it is - a caller that downscaled
    before calling gets coordinates in that downscaled space and scales them back itself, exactly
    as main.py already does for template and OCR matches.
    """
    global _warned
    if _session is None:
        if not _warned:
            print("WARNING: monster detection unavailable (%s)" % (_load_error or "not loaded"))
            _warned = True
        return []
    if frame is None or frame.size == 0:
        return []

    canvas, ratio, pad_x, pad_y = _letterbox(frame, _input_size)
    # BGR->RGB, HWC->CHW, 0..1 float. This is what ultralytics does at training time; any
    # difference here is a silent accuracy loss, not an error.
    blob = cv.cvtColor(canvas, cv.COLOR_BGR2RGB).transpose(2, 0, 1)[np.newaxis]
    blob = np.ascontiguousarray(blob, dtype=np.float32) / 255.0

    raw = _session.run(None, {_input_name: blob})[0]
    # (1, 4 + num_classes, anchors) -> (anchors, 4 + num_classes). YOLOv8/11 have no separate
    # objectness score: the class score IS the confidence.
    predictions = raw[0].T
    scores_all = predictions[:, 4:]
    best_class = scores_all.argmax(axis=1)
    best_score = scores_all.max(axis=1)

    # Per-class thresholds, applied before NMS so a class's own operating point decides what is
    # even a candidate. A single floor (min_confidence) can override for diagnostics.
    floor = np.array([_thresholds.get(_class_names[i], DEFAULT_THRESHOLD) for i in best_class]) \
        if min_confidence is None else np.full(best_score.shape, float(min_confidence))
    keep = best_score >= floor
    if not keep.any():
        return []
    predictions, best_class, best_score = predictions[keep], best_class[keep], best_score[keep]

    height, width = frame.shape[:2]
    boxes = []
    for centre_x, centre_y, box_w, box_h in predictions[:, :4]:
        x = (centre_x - box_w / 2 - pad_x) / ratio
        y = (centre_y - box_h / 2 - pad_y) / ratio
        boxes.append([int(round(x)), int(round(y)),
                      int(round(box_w / ratio)), int(round(box_h / ratio))])

    detections = []
    # CLASS-AWARE: suppress within a class, never across. See the module docstring - a boss
    # standing in its own pack overlaps it heavily, and cross-class suppression deletes whichever
    # scored lower, which is usually the rarer class.
    for class_id in np.unique(best_class):
        members = np.nonzero(best_class == class_id)[0]
        subset = [boxes[i] for i in members]
        confidences = [float(best_score[i]) for i in members]
        # cv.dnn.NMSBoxes, not cv.groupRectangles - OpenCV 5 removed the latter from its Python
        # bindings (Error_history.txt #1), and this project standardised on the former.
        # SCORE THRESHOLD 0.0, because the per-class thresholds above have already decided what is
        # a candidate and this call must only merge overlaps. Passing the minimum confidence here
        # instead - the obvious-looking choice - drops the lowest-scoring detection every call,
        # silently: NMSBoxes compares strictly, so a box scoring exactly the threshold is
        # discarded. It costs the faintest detection in the frame, which is the one a distant or
        # partly occluded monster produces, and nothing anywhere reports a box going missing.
        kept = cv.dnn.NMSBoxes(subset, confidences, 0.0, NMS_IOU)
        for index in (np.array(kept).flatten() if len(kept) else []):
            x, y, w, h = subset[int(index)]
            # Clip to the frame: a monster half off screen gets a box that runs past the edge, and
            # a caller slicing the frame with it would otherwise get a short array or an empty one.
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(width, x + w), min(height, y + h)
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            detections.append(Detection(_class_names[int(class_id)],
                                        confidences[int(index)], x0, y0, x1 - x0, y1 - y0))

    detections.sort(key=lambda d: d.confidence, reverse=True)
    return detections


def benchmark(frame, runs=20):
    """Measured ms per detect() call on a representative frame, for deciding the inference rate.

    Takes a real frame because postprocess cost scales with the number of DETECTIONS, not with
    input size - a blank frame reports a number that a busy one will not reproduce.
    """
    if _session is None:
        return None
    detect(frame)                      # warm up: the first call allocates the arenas
    start = time.perf_counter()
    for _ in range(runs):
        detect(frame)
    return (time.perf_counter() - start) * 1000.0 / runs
