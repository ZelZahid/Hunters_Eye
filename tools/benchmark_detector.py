"""How many milliseconds does a neural detector actually cost on THIS machine?

Step 1 of docs/monster_detection_plan.txt section 9. It answers two design questions that
everything downstream depends on, and that are not worth guessing at:

  1. Can inference run every frame, or does it have to run at ~5-10 Hz with
     _relocalize_track() filling the gaps between inferences?
  2. Is a GPU required, or merely nice to have?

It benchmarks the path that will ACTUALLY SHIP - onnxruntime executing an exported ONNX file -
not ultralytics/PyTorch. Those are different numbers and only one of them matters: the plan
keeps torch as a development-only dependency, so measuring torch inference would be measuring
something the pipeline will never run. ultralytics is used here ONLY to download and export the
model, once, and only if the .onnx file isn't already sitting there.

    python tools/benchmark_detector.py                     # the full sweep
    python tools/benchmark_detector.py --sizes 640         # just one input size
    python tools/benchmark_detector.py --models yolov8n    # just one model
    python tools/benchmark_detector.py --image shot.png    # measure against a real frame
    python tools/benchmark_detector.py --threads 4         # cap onnxruntime's CPU threads

This imports NOTHING from the pipeline, so it cannot affect anything currently working, and it
needs no sys.path bootstrap for the same reason.

WHAT THE NUMBERS MEAN. Three costs are reported separately because they behave differently:
preprocess (letterbox + normalise, scales with input size), inference (the network itself), and
postprocess (score filter + NMS, scales with how many things are DETECTED, not with input size).
Postprocess is the one that surprises people - it is pure Python/OpenCV and on a busy frame it
can rival inference.

The "cores" columns are the number that actually decides the design, in the same terms
CLAUDE.md already uses for the capture thread (0.42 cores paced vs 3.56 unpaced): a cost of
40ms/frame is 2.4 cores if you run it every frame at 60 FPS, and 0.32 cores at 8 Hz. Design
priority #2 is about not stealing CPU from the game, not about the benchmark's own headline.

Median is reported rather than mean, for the same reason game_state.py medians its readings:
the failure mode is one sample being wildly wrong (a scheduler hiccup, a background task), and
a median discards it outright while a mean absorbs part of it.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

#Where exported models get cached. Kept out of assets/ - these are downloaded/derived
#artifacts of a benchmark, not project data anyone edits.
MODEL_DIR = Path(__file__).resolve().parent / "_benchmark_models"

DEFAULT_MODELS = ["yolov8n", "yolo11n"]
DEFAULT_SIZES = [640, 416, 320]

#Frame rates to express the cost as CPU cores at. 60 is main.py's TARGET_FPS (i.e. what running
#detection on every frame would mean); 8 is the plan's proposed inference rate with
#relocalization between; 5 is the low end of that range.
REPORT_HZ = [60, 8, 5]

#Defined once rather than at each call site: whichever of them fires first is the only
#message the user ever sees, so it has to be the complete one - including the GPU builds,
#which are the whole point of running this on a machine that has a GPU.
ORT_INSTALL_HINT = ("python -m pip install onnxruntime              # CPU\n"
                    "    python -m pip install onnxruntime-directml   # any DX12 GPU (Windows)\n"
                    "    python -m pip install onnxruntime-gpu        # NVIDIA CUDA")

SCORE_THRESHOLD = 0.25   #only affects postprocess cost, not inference
NMS_THRESHOLD = 0.45


def require(module_name, install_hint):
    """Import a benchmark-only dependency, or explain exactly how to get it and stop.

    These are deliberately NOT in the pipeline's install: a missing one means "you cannot run
    this benchmark yet", never "the project is broken".
    """
    try:
        return __import__(module_name)
    except ImportError:
        print("\nThis benchmark needs '%s', which is not installed." % module_name)
        print("It is a development-only dependency - the pipeline itself never imports it.\n")
        print("    %s\n" % install_hint)
        sys.exit(1)


def export_onnx(model_name, size):
    """Fetch the pretrained model and export it to ONNX at a fixed input size.

    Runs once per (model, size) and then never again - the .onnx is cached in MODEL_DIR. This is
    the only place ultralytics/torch is touched.
    """
    out = MODEL_DIR / ("%s_%d.onnx" % (model_name, size))
    if out.exists():
        return out

    require("ultralytics",
            'python -m pip install ultralytics   # heavy (pulls torch); dev-only, one time')
    from ultralytics import YOLO

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print("  exporting %s at %d (one-time, downloads weights if needed)..." % (model_name, size))
    model = YOLO("%s.pt" % model_name)
    exported = Path(model.export(format="onnx", imgsz=size, verbose=False))
    exported.replace(out)
    return out


def letterbox(frame, size):
    """Resize preserving aspect ratio and pad to a square, which is what YOLO expects.

    Squashing to square instead would distort every object in the frame and quietly cost
    accuracy, so the padding is not optional even though it looks like busywork.
    """
    import cv2 as cv

    h, w = frame.shape[:2]
    scale = min(size / w, size / h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    resized = cv.resize(frame, (new_w, new_h), interpolation=cv.INTER_LINEAR)

    canvas = np.full((size, size, 3), 114, dtype=np.uint8)  #114 is YOLO's conventional pad grey
    top, left = (size - new_h) // 2, (size - new_w) // 2
    canvas[top:top + new_h, left:left + new_w] = resized
    return canvas


def preprocess(frame, size):
    """BGR uint8 frame -> the NCHW float32 batch the network takes."""
    import cv2 as cv

    padded = letterbox(frame, size)
    rgb = cv.cvtColor(padded, cv.COLOR_BGR2RGB)
    blob = rgb.astype(np.float32) / 255.0
    return np.ascontiguousarray(blob.transpose(2, 0, 1)[None])   #HWC -> NCHW


def postprocess(output):
    """Score filter + NMS on a YOLOv8/11 head, returning how many boxes survived.

    Output is (1, 4 + num_classes, num_anchors) - box coords first, then one score per class.
    Uses cv.dnn.NMSBoxes rather than cv.groupRectangles, which OpenCV 5 removed from its Python
    bindings (Error_history.txt #1).
    """
    import cv2 as cv

    preds = output[0].T                       #(anchors, 4 + num_classes)
    scores = preds[:, 4:]
    best = scores.max(axis=1)
    keep = best > SCORE_THRESHOLD
    if not np.any(keep):
        return 0

    boxes_xywh = preds[keep, :4]
    confidences = best[keep]
    #cx,cy,w,h -> x,y,w,h, which is what NMSBoxes expects
    boxes = np.column_stack([
        boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2,
        boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2,
        boxes_xywh[:, 2],
        boxes_xywh[:, 3],
    ])
    idxs = cv.dnn.NMSBoxes(boxes.tolist(), confidences.tolist(),
                           SCORE_THRESHOLD, NMS_THRESHOLD)
    return len(idxs)


def bench_one(onnx_path, provider, frame, size, runs, warmup, threads):
    """Time preprocess / inference / postprocess separately. Returns medians in ms, or None."""
    ort = require("onnxruntime", ORT_INSTALL_HINT)

    opts = ort.SessionOptions()
    if threads:
        opts.intra_op_num_threads = threads
    try:
        session = ort.InferenceSession(str(onnx_path), opts, providers=[provider])
    except Exception as exc:                      #a provider can be listed but fail to init
        print("      %-24s unavailable (%s)" % (provider, type(exc).__name__))
        return None

    #ASKING FOR A PROVIDER IS NOT GETTING IT. onnxruntime does not raise when a provider's
    #native libraries are missing - it prints a warning to stderr and SILENTLY SUBSTITUTES CPU,
    #so the session runs fine and every number looks plausible while measuring something else
    #entirely. Seen exactly that here: onnxruntime-gpu 1.29 wants CUDA 13, the machine had 12.9,
    #and "CUDA" timings came back identical to CPU because they WERE CPU. The only honest
    #source of truth is what the session reports back, so report that and never the request.
    actual = session.get_providers()[0]
    if actual != provider:
        print("      %-24s FELL BACK to %s - not measured" % (provider, actual))
        return None

    input_name = session.get_inputs()[0].name

    #Warmup matters more than it looks: the first call pays lazy kernel compilation, memory
    #arena allocation and (on GPU) driver spin-up. Timing it would report a number the pipeline
    #never sees again.
    for _ in range(warmup):
        session.run(None, {input_name: preprocess(frame, size)})

    pre_ms, inf_ms, post_ms, detections = [], [], [], 0
    for _ in range(runs):
        t0 = time.perf_counter()
        blob = preprocess(frame, size)
        t1 = time.perf_counter()
        outputs = session.run(None, {input_name: blob})
        t2 = time.perf_counter()
        detections = postprocess(outputs[0])
        t3 = time.perf_counter()

        pre_ms.append((t1 - t0) * 1000)
        inf_ms.append((t2 - t1) * 1000)
        post_ms.append((t3 - t2) * 1000)

    return {
        "provider": provider,
        "pre": statistics.median(pre_ms),
        "inf": statistics.median(inf_ms),
        "post": statistics.median(post_ms),
        "inf_p90": sorted(inf_ms)[int(len(inf_ms) * 0.9) - 1],
        "detections": detections,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--sizes", nargs="+", type=int, default=DEFAULT_SIZES)
    ap.add_argument("--runs", type=int, default=50, help="timed iterations per configuration")
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--threads", type=int, default=0,
                    help="cap onnxruntime CPU threads (0 = its default, all cores)")
    ap.add_argument("--image", type=Path,
                    help="a real screenshot to measure against; random noise if omitted")
    ap.add_argument("--providers", nargs="+",
                    help="override which execution providers to try")
    args = ap.parse_args()

    ort = require("onnxruntime", ORT_INSTALL_HINT)

    if args.image:
        import cv2 as cv
        frame = cv.imread(str(args.image))
        if frame is None:
            print("Could not read %s" % args.image)
            sys.exit(1)
        source = "%s (%dx%d)" % (args.image.name, frame.shape[1], frame.shape[0])
    else:
        #Random noise is fine for INFERENCE, which is fixed-cost regardless of content - unlike
        #Tesseract, whose cost scales with visual complexity (CLAUDE.md records a 0.9s real
        #frame vs a much cheaper synthetic one). It is NOT representative for postprocess,
        #which scales with how many detections survive - pass --image for that number.
        frame = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)
        source = "random noise 1920x1080 (postprocess cost not representative - use --image)"

    available = args.providers or ort.get_available_providers()
    print("\nonnxruntime %s" % ort.__version__)
    print("providers available: %s" % ", ".join(available))
    print("input frame: %s" % source)
    print("threads: %s" % (args.threads or "default (all cores)"))
    print("runs: %d timed, %d warmup, median reported\n" % (args.runs, args.warmup))

    rows = []
    for model_name in args.models:
        for size in args.sizes:
            print("%s @ %d" % (model_name, size))
            try:
                onnx_path = export_onnx(model_name, size)
            except Exception as exc:
                print("      export failed: %s: %s" % (type(exc).__name__, exc))
                continue

            for provider in available:
                result = bench_one(onnx_path, provider, frame, size,
                                   args.runs, args.warmup, args.threads)
                if result is None:
                    continue
                result.update(model=model_name, size=size)
                rows.append(result)
                print("      %-24s pre %5.1f  inf %6.1f  post %5.1f  total %6.1f ms"
                      % (provider, result["pre"], result["inf"],
                         result["post"], result["pre"] + result["inf"] + result["post"]))
    if not rows:
        print("\nNothing ran.")
        return

    print("\n" + "=" * 78)
    print("TOTAL ms/frame, and what that costs in CPU cores at various inference rates")
    print("=" * 78)
    header = "%-10s %5s %-22s %8s" % ("model", "size", "provider", "ms")
    header += "".join("%9s" % ("%dHz" % hz) for hz in REPORT_HZ)
    print(header)
    print("-" * 78)
    for r in sorted(rows, key=lambda r: r["pre"] + r["inf"] + r["post"]):
        total = r["pre"] + r["inf"] + r["post"]
        line = "%-10s %5d %-22s %8.1f" % (r["model"], r["size"], r["provider"][:22], total)
        line += "".join("%9.2f" % (total / 1000.0 * hz) for hz in REPORT_HZ)
        print(line)

    print("""
Reading this:
  60Hz column = running detection on every frame at main.py's TARGET_FPS.
  8Hz / 5Hz   = the plan's design, inferring periodically with _relocalize_track()
                keeping boxes glued in between (~5.7ms per tracked item).
  Anything at or below ~0.3 cores is comfortably affordable. Above ~1.0 core it is
  competing with the game, which design priority #2 says it must not do.

  If the 8Hz column is affordable but the 60Hz one is not, that is the expected
  result and the plan already assumes it - it is not a problem to solve.
""")


if __name__ == "__main__":
    main()
