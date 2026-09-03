"""Can an off-the-shelf model find a monster with NO training at all?

Step 2 of docs/monster_detection_plan.txt section 9, and the cheaper of the two experiments -
run it first, because a positive result invalidates a lot of downstream work.

Open-vocabulary detectors (OWLv2 here) take a PROMPT rather than a fixed class list. The prompt
can be text ("a skeleton") or, more interestingly for this project, an IMAGE: hand it one crop
of Pindle and it looks for that thing in a new frame. If that works on stylised game art, the
entire training pipeline in sections 5 and 7 of the plan is unnecessary and a monster is
registered by dropping a single image in a folder, with no training step at all.

It is genuinely unknown whether it will. These models are trained on natural photographs and a
Diablo skeleton is nowhere in that distribution the way a dog or a car is. Cheap to find out,
expensive to assume - which is the entire reason this script exists.

    # image prompt: find the thing in query.png, inside scene.png
    python tools/zeroshot_check.py --scene shot.png --query pindle_crop.png

    # text prompt: several at once, cheap to try alongside
    python tools/zeroshot_check.py --scene shot.png --text "a skeleton" "a monster" "a demon"

    # grab the scene off the live screen instead, after a countdown to alt-tab
    python tools/zeroshot_check.py --capture --delay 8 --query pindle_crop.png

Imports NOTHING from the pipeline, so it cannot affect anything currently working.

THREE OUTCOMES, AND THE MIDDLE ONE IS NOT A FAILURE:

  1. It finds the monster, fast enough to run live  -> skip training entirely, this becomes
     the detector. Unlikely; OWLv2 is ViT-based and heavy.
  2. It finds the monster, far too slow to run live -> STILL A WIN. Use it offline as an
     AUTO-LABELLER: point it at a few hundred recorded frames, let it draw the boxes, and it
     has just solved section 7's annotation problem, which is the only genuinely laborious
     part of the plan. A slow model that is right is worth a lot when it runs once per frame
     of a dataset rather than 60 times a second.
  3. It does not find it at all -> the plan proceeds exactly as written, and this cost an hour.

So judge the output image on ACCURACY first and speed second. The timing is printed, but a slow
success and a fast success are both good news, for different reasons.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

DEFAULT_MODEL = "google/owlv2-base-patch16-ensemble"

#Deliberately low. The question is "does it see the thing at all", so a weak-but-correct
#detection is a real signal and a strict threshold would hide it. Tighten later if it works.
DEFAULT_THRESHOLD = 0.10


def require(module_name, install_hint):
    """Import an experiment-only dependency, or explain how to get it and stop."""
    try:
        return __import__(module_name)
    except ImportError:
        print("\nThis experiment needs '%s', which is not installed." % module_name)
        print("It is a development-only dependency - the pipeline never imports it.\n")
        print("    %s\n" % install_hint)
        sys.exit(1)


def capture_screen(delay):
    """Grab the primary display after a countdown, so the game can be brought to the front."""
    import cv2 as cv
    import mss

    for remaining in range(delay, 0, -1):
        print("  capturing in %d... " % remaining, end="\r", flush=True)
        time.sleep(1)
    print(" " * 30, end="\r")

    with mss.mss() as sct:
        monitor = sct.monitors[1]                #[1] is the primary display; [0] is all of them
        raw = sct.grab(monitor)
        #np.frombuffer over a view, not np.array - the latter copies the whole BGRA frame
        #through the array interface (CLAUDE.md's measured-performance notes).
        frame = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        return cv.cvtColor(frame, cv.COLOR_BGRA2BGR)


def to_pil(bgr):
    import cv2 as cv
    from PIL import Image
    return Image.fromarray(cv.cvtColor(bgr, cv.COLOR_BGR2RGB))


def post_process(processor, outputs, target_sizes, threshold, image_guided):
    """Call whichever post-processing name this transformers version exposes.

    The method was renamed across transformers releases. Rather than pinning a version for a
    one-off experiment, try the names in order - the same defensive posture Error_history.txt #1
    recommends after OpenCV 5 removed groupRectangles out from under working code.
    """
    if image_guided:
        return processor.post_process_image_guided_detection(
            outputs=outputs, threshold=threshold, nms_threshold=0.3, target_sizes=target_sizes)

    for name in ("post_process_grounded_object_detection", "post_process_object_detection"):
        fn = getattr(processor, name, None)
        if fn is not None:
            return fn(outputs=outputs, threshold=threshold, target_sizes=target_sizes)
    raise AttributeError("no known post-processing method on %s" % type(processor).__name__)


def annotate(scene, detections, labels, out_path):
    """Draw the boxes and save, because 'did it work' is a visual judgement."""
    import cv2 as cv

    canvas = scene.copy()
    for score, box, label_idx in detections:
        x1, y1, x2, y2 = [int(round(v)) for v in box]
        cv.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
        text = "%s %.2f" % (labels[label_idx] if labels else "match", score)
        #Black plate behind the text, for the same reason overlay.py draws one: thin light
        #glyphs straight onto arbitrary game art are unreadable.
        (tw, th), _ = cv.getTextSize(text, cv.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv.rectangle(canvas, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 0, 0), -1)
        cv.putText(canvas, text, (x1 + 2, y1 - 4),
                   cv.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv.LINE_AA)

    cv.imwrite(str(out_path), canvas)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scene", type=Path, help="the frame to search in")
    ap.add_argument("--capture", action="store_true", help="grab the screen instead of --scene")
    ap.add_argument("--delay", type=int, default=6, help="countdown before --capture")
    ap.add_argument("--query", type=Path, help="image prompt: a crop of the thing to find")
    ap.add_argument("--text", nargs="+", help="text prompts, tried together")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                    help="auto picks cuda when a GPU is present")
    ap.add_argument("--out", type=Path, default=Path("zeroshot_result.png"))
    args = ap.parse_args()

    if not args.query and not args.text:
        ap.error("give it something to look for: --query <image> and/or --text <words>")
    if not args.scene and not args.capture:
        ap.error("give it something to look in: --scene <image> or --capture")

    import cv2 as cv
    torch = require("torch", "python -m pip install torch --index-url "
                             "https://download.pytorch.org/whl/cpu   # dev-only")
    require("transformers", "python -m pip install transformers pillow")
    from transformers import AutoProcessor, Owlv2ForObjectDetection

    if args.capture:
        scene = capture_screen(args.delay)
        cv.imwrite("zeroshot_scene.png", scene)
        print("captured scene saved to zeroshot_scene.png")
    else:
        scene = cv.imread(str(args.scene))
        if scene is None:
            print("Could not read %s" % args.scene)
            sys.exit(1)

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        print("cuda requested but torch reports no GPU - is this a +cpu torch build?")
        sys.exit(1)

    print("loading %s on %s (downloads ~600MB on first run)..." % (args.model, device))
    processor = AutoProcessor.from_pretrained(args.model)
    model = Owlv2ForObjectDetection.from_pretrained(args.model).to(device)
    model.eval()
    if device == "cuda":
        print("  %s" % torch.cuda.get_device_name(0))

    scene_pil = to_pil(scene)
    #Same device as the model outputs, or post-processing hits a device mismatch comparing
    #these against the predicted boxes.
    target_sizes = torch.tensor([[scene.shape[0], scene.shape[1]]]).to(device)

    runs = []
    if args.query:
        query = cv.imread(str(args.query))
        if query is None:
            print("Could not read %s" % args.query)
            sys.exit(1)
        runs.append(("image prompt: %s" % args.query.name, True,
                     {"images": scene_pil, "query_images": to_pil(query)}, None))
    if args.text:
        runs.append(("text prompt: %s" % ", ".join(args.text), False,
                     {"images": scene_pil, "text": [args.text]}, args.text))

    for title, image_guided, processor_kwargs, labels in runs:
        print("\n" + "=" * 70)
        print(title)
        print("=" * 70)

        inputs = processor(return_tensors="pt", **processor_kwargs)
        inputs = {k: (v.to(device) if hasattr(v, "to") else v) for k, v in inputs.items()}

        def run_once():
            with torch.no_grad():
                return (model.image_guided_detection(**inputs) if image_guided
                        else model(**inputs))

        #On GPU the first call pays kernel compilation and allocator warm-up, and CUDA calls are
        #ASYNCHRONOUS - without a synchronize() the timer would stop when the work was merely
        #QUEUED, reporting a number far below the truth. Both matter; neither is optional.
        if device == "cuda":
            run_once()
            torch.cuda.synchronize()

        t0 = time.perf_counter()
        outputs = run_once()
        if device == "cuda":
            torch.cuda.synchronize()
        elapsed_ms = (time.perf_counter() - t0) * 1000

        try:
            results = post_process(processor, outputs, target_sizes,
                                   args.threshold, image_guided)[0]
        except Exception as exc:
            print("  post-processing failed: %s: %s" % (type(exc).__name__, exc))
            continue

        scores = results["scores"].tolist()
        boxes = results["boxes"].tolist()
        label_idxs = results.get("labels")
        label_idxs = label_idxs.tolist() if label_idxs is not None else [0] * len(scores)

        detections = sorted(zip(scores, boxes, label_idxs), key=lambda d: -d[0])
        print("  inference: %.0f ms" % elapsed_ms)
        print("  %d detections above %.2f" % (len(detections), args.threshold))
        for score, box, idx in detections[:15]:
            name = labels[idx] if labels else "match"
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            print("    %-22s %.3f   centre (%4.0f, %4.0f)" % (name, score, cx, cy))

        suffix = "image" if image_guided else "text"
        out = args.out.with_name("%s_%s%s" % (args.out.stem, suffix, args.out.suffix))
        annotate(scene, detections, labels, out)
        print("  annotated result -> %s" % out)

    print("""
Now LOOK AT THE OUTPUT IMAGE. This is a visual judgement, not a score threshold.

  boxes on the right monsters      -> outcome 1 or 2 (see this file's docstring).
                                      Check the ms above: fast enough to run live, or
                                      slow-but-correct, which makes it an auto-labeller
                                      and solves the plan's hardest part anyway.
  boxes on terrain / nothing / all
  over the frame                   -> outcome 3. The plan proceeds as written.

Either way, record the numbers in docs/monster_detection_plan.txt section 9 so the next
session does not run this again to learn the same thing.
""")


if __name__ == "__main__":
    main()
