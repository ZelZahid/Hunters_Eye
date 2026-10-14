"""How long after the screen changes does OCR report the text on it? Where does that time go?

    python tools/measure_ocr_latency.py                  # 60s, the same schedule main.py uses
    python tools/measure_ocr_latency.py --seconds 120
    python tools/measure_ocr_latency.py --interval 0     # try a different OCR_INTERVAL_SECONDS
    python tools/measure_ocr_latency.py --old            # the loop as it was before early rescans

Run it, switch to the game, and teleport (or otherwise make the whole view jump - a camera cut,
a door, a new page) next to something with a label. It PRESSES AND CLICKS NOTHING; it only looks.
Safe alongside main.py for the same reason diagnose_quit.py is: it captures with its own mss,
never a DXGI frame source (a second DXGI consumer steals the pipeline's frames, EH#39). Running
both at once does make the two compete for CPU, so close main.py for the cleanest numbers.

WHY THIS EXISTS: "the labels show up late" is the sum of five different waits, and each wants a
different fix. Measured per scan and reported per jump:

  wait       from the view jumping to the next full OCR scan starting - the OCR_INTERVAL_SECONDS
             throttle, plus however long the scan already in flight takes to finish
  capture    the screen grab and BGRA->BGR conversion
  preprocess binarizing for Tesseract (text_detection._preprocess)
  tesseract  the OCR engine itself
  match      fuzzy-matching what was read against targets.txt, plus the open-panel filter
  catch-up   the extra grab + relocalization main.py does before publishing

A jump is what main.py's _is_view_jump() calls one: the same crop of the middle of the screen
(VIEW_WATCH_REGION) changing suddenly against its own recent baseline. Walking holds a steady
difference; a teleport is a spike above it. With --save, every difference and scan is written to
one CSV timeline, which is what VIEW_JUMP_DIFF / VIEW_JUMP_RATIO were chosen from - re-derive them
from one of those on another game. --jump-diff moves the floor.

The OCR loop below is a REPLICA of main.py's detect_text() using main.py's own constants and
helpers, so it measures what the pipeline does rather than an idealised version of it. If
detect_text() changes shape, this has to follow it.
"""
import sys
from pathlib import Path
#Run directly (python tools/measure_ocr_latency.py), so the repo root has to be on the path before
#any project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import statistics
import threading
import time
from collections import deque

import cv2 as cv
import mss
import numpy as np

import main as pipeline
from core import text_detection

JUMP_COOLDOWN_SECONDS = 0.3 #a teleport's landing can span a couple of frames - count it once
JUMP_LOOK_SECONDS = 0.01


def _new_sct():
    return getattr(mss, "MSS", mss.mss)()


class Stopwatch:
    """Accumulates time spent inside wrapped functions, so a single find_text_matches() call can
    be split into preprocess / tesseract / everything else without editing text_detection.py."""
    def __init__(self):
        self.totals = {}

    def wrap(self, key, fn):
        def timed(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.totals[key] = self.totals.get(key, 0.0) + time.perf_counter() - t0
        return timed

    def take(self):
        totals, self.totals = self.totals, {}
        return totals


def watch_for_jumps(stop, jumps, diffs, threshold):
    """The ground truth for "when did the view jump": looks at main.py's own jump region every
    ~10ms and records the time of every jump in `jumps`.

    Each look is compared with the one from VIEW_WATCH_SECONDS earlier, not the one just before,
    because that is the gap main.py's idle loop compares across - so the distribution in `diffs` is
    the one VIEW_JUMP_DIFF has to be chosen from. Looking every 10ms rather than every 30ms is
    only for the timestamp: it puts the jump within ~10ms of when it really happened."""
    history = []
    #The same ~0.3s of baseline main.py keeps, at this loop's finer cadence.
    recent = deque(maxlen=pipeline.VIEW_JUMP_HISTORY * max(1, round(pipeline.VIEW_WATCH_SECONDS / JUMP_LOOK_SECONDS)))
    back = max(1, round(pipeline.VIEW_WATCH_SECONDS / JUMP_LOOK_SECONDS))
    last_jump = 0.0
    region = pipeline._view_jump_region()
    with _new_sct() as sct:
        while not stop.is_set():
            t = time.perf_counter()
            img = sct.grab(region)
            f = np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)
            history.append(cv.cvtColor(f, cv.COLOR_BGRA2GRAY))
            if len(history) > back:
                d = pipeline._view_difference(history[-1], history.pop(0))
                diffs.append((t, d))
                jumped = pipeline._is_view_jump(d, recent)
                recent.append(d)
                if jumped and t - last_jump >= JUMP_COOLDOWN_SECONDS:
                    jumps.append((t, d))
                    last_jump = t
                    print(f"{t:10.3f}  JUMP (diff {d:.1f})")
            time.sleep(max(0.0, JUMP_LOOK_SECONDS - (time.perf_counter() - t)))


def run_ocr(stop, scans, interval, old):
    """main.py's detect_text() loop, timed stage by stage. Appends one dict per full scan.

    `old` replays the loop as it was before early rescans: the interval alone decides when to scan,
    nothing looks at the screen while idle, and relocalization is unpaced."""
    watch = Stopwatch()
    text_detection._preprocess = watch.wrap("preprocess", text_detection._preprocess)
    if text_detection._tesserocr_api is not None:
        text_detection._get_words_tesserocr = watch.wrap("tesseract", text_detection._get_words_tesserocr)
    else:
        text_detection._get_words_pytesseract = watch.wrap("tesseract", text_detection._get_words_pytesseract)

    local_tracks = []
    last_ocr_time = -1e9
    rescan_now = False
    view_prev = None
    view_prev_time = 0.0
    view_diffs = deque(maxlen=pipeline.VIEW_JUMP_HISTORY)
    jump_region = pipeline._view_jump_region()
    with _new_sct() as sct:
        def grab(grey):
            img = sct.grab(pipeline.monitor_area)
            f = np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)
            f = cv.cvtColor(f, cv.COLOR_BGRA2GRAY if grey else cv.COLOR_BGRA2BGR)
            if pipeline.OCR_CAPTURE_SCALE != 1.0:
                f = cv.resize(f, (0, 0), fx=pipeline.OCR_CAPTURE_SCALE, fy=pipeline.OCR_CAPTURE_SCALE)
            return f

        def look():
            img = sct.grab(jump_region)
            f = np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)
            return cv.cvtColor(f, cv.COLOR_BGRA2GRAY)

        def middle_of(g):
            s = pipeline.OCR_CAPTURE_SCALE
            x0 = int((jump_region["left"] - pipeline.monitor_area["left"]) * s)
            y0 = int((jump_region["top"] - pipeline.monitor_area["top"]) * s)
            return g[y0:y0 + int(jump_region["height"] * s), x0:x0 + int(jump_region["width"] * s)].copy()

        while not stop.is_set():
            loop_start = time.perf_counter()
            since_scan = loop_start - last_ocr_time
            until_scan = interval - since_scan
            if rescan_now and since_scan >= pipeline.OCR_MIN_GAP_SECONDS:
                until_scan = 0
            if until_scan > 0 and not local_tracks:
                if old:
                    time.sleep(min(until_scan, 0.02))
                    continue
                time.sleep(min(until_scan, pipeline.VIEW_WATCH_SECONDS))
                g = look()
                now = time.perf_counter()
                span = (now - view_prev_time) / pipeline.VIEW_WATCH_SECONDS
                diff = pipeline._view_difference(g, view_prev)
                view_prev, view_prev_time = g, now
                if pipeline._is_view_jump(diff, view_diffs, span):
                    rescan_now = True
                if diff is not None and span < 1.5:
                    view_diffs.append(diff)
                continue

            if until_scan <= 0:
                forced = rescan_now
                rescan_now = False
                t_start = time.perf_counter()
                frame = grab(grey=False)
                last_ocr_time = t_start
                frame_grey = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
                frame_h = frame.shape[0]
                y0 = int(frame_h * pipeline.VIEWPORT_TOP_MARGIN)
                y1 = int(frame_h * (1 - pipeline.VIEWPORT_BOTTOM_MARGIN))
                watch.take()
                t_ocr = time.perf_counter()
                results = text_detection.find_text_matches(frame[y0:y1, :], pipeline.target_items)
                results = [(x, y + y0, w, h, n, c, col) for (x, y, w, h, n, c, col) in results]
                results = pipeline._outside_open_panels(results, frame_grey)
                t_matched = time.perf_counter()
                stages = watch.take()
                local_tracks = [(x, y, w, h, n, c, col, frame_grey[y:y + h, x:x + w].copy())
                                for (x, y, w, h, n, c, col) in results]
                frame_time = t_start
                if local_tracks or old:
                    found = len(local_tracks)
                    frame_time = time.perf_counter()
                    frame_grey = grab(grey=True)
                    local_tracks = [t for t in (pipeline._relocalize_track(frame_grey, tr) for tr in local_tracks) if t]
                    rescan_now = not old and len(local_tracks) < found
                view_prev, view_prev_time = middle_of(frame_grey), frame_time
                t_published = time.perf_counter()
                pre = stages.get("preprocess", 0.0)
                tess = stages.get("tesseract", 0.0)
                scans.append({
                    "start": t_start, "published": t_published, "forced": forced,
                    "capture": t_ocr - t_start, #the grab plus the grey convert and crop
                    "preprocess": pre, "tesseract": tess,
                    "match": (t_matched - t_ocr) - pre - tess,
                    "catchup": t_published - t_matched,
                    "names": [t[4] for t in local_tracks],
                })
                s = scans[-1]
                print(f"{t_start:10.3f}  scan{' (early)' if forced else ''}: capture {s['capture']*1000:4.0f}  "
                      f"preprocess {pre*1000:4.0f}  tesseract {tess*1000:4.0f}  match {s['match']*1000:3.0f}  "
                      f"catch-up {s['catchup']*1000:3.0f}  = {(t_published - t_start)*1000:4.0f}ms  {s['names']}")
            else:
                tracked = len(local_tracks)
                frame_time = time.perf_counter()
                g = grab(grey=True)
                local_tracks = [t for t in (pipeline._relocalize_track(g, tr) for tr in local_tracks) if t]
                if not old and len(local_tracks) < tracked:
                    rescan_now = True
                view_prev, view_prev_time = middle_of(g), frame_time
            if local_tracks and not rescan_now and not old:
                time.sleep(max(0.0, pipeline.TRACK_LOOP_SECONDS - (time.perf_counter() - loop_start)))


def summarise(jumps, scans, diffs, threshold):
    def ms(xs):
        return f"median {statistics.median(xs)*1000:5.0f}ms  (min {min(xs)*1000:4.0f}, max {max(xs)*1000:4.0f})"

    print("\n=== per full OCR scan ===")
    if scans:
        for key in ("capture", "preprocess", "tesseract", "match", "catchup"):
            print(f"  {key:10s} {ms([s[key] for s in scans])}")
        print(f"  {'total':10s} {ms([s['published'] - s['start'] for s in scans])}   over {len(scans)} scans")

    print("\n=== per jump: view jumped -> labels published ===")
    rows = []
    for (t_jump, _d) in jumps:
        after = [s for s in scans if s["start"] >= t_jump and s["names"]]
        if not after:
            continue
        s = after[0]
        if s["start"] - t_jump > 3.0:
            continue #nothing with a label on screen after this jump - not a measurement
        rows.append((s["published"] - t_jump, s["start"] - t_jump, s))
        print(f"  {(s['published'] - t_jump)*1000:5.0f}ms = wait {(s['start'] - t_jump)*1000:4.0f} + "
              f"scan {(s['published'] - s['start'])*1000:4.0f}   {s['names']}")
    if rows:
        print(f"  latency {ms([r[0] for r in rows])}   over {len(rows)} jumps with labels")
        print(f"  of which waiting for the scan to start: {ms([r[1] for r in rows])}")
    else:
        print("  no jump was followed by a scan that found a label - teleport next to a labelled item")

    if diffs:
        q = np.percentile([d for _, d in diffs], [50, 90, 99, 99.9])
        print(f"\nframe difference percentiles 50/90/99/99.9: {q[0]:.1f} / {q[1]:.1f} / {q[2]:.1f} / "
              f"{q[3]:.1f}   max {max(d for _, d in diffs):.1f}   (jump threshold {threshold})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--delay", type=float, default=3.0, help="countdown before measuring starts")
    parser.add_argument("--interval", type=float, default=pipeline.OCR_INTERVAL_SECONDS,
                        help=f"seconds between full OCR scans (main.py: {pipeline.OCR_INTERVAL_SECONDS})")
    parser.add_argument("--old", action="store_true",
                        help="replay the loop as it was before early rescans, for a before/after")
    parser.add_argument("--jump-diff", type=float, default=pipeline.VIEW_JUMP_DIFF,
                        help=f"the floor under a jump, mean grey difference 0-255 (main.py's VIEW_JUMP_DIFF: {pipeline.VIEW_JUMP_DIFF})")
    parser.add_argument("--save", type=Path, help="also write every frame difference and scan to this CSV")
    args = parser.parse_args()

    pipeline.VIEW_JUMP_DIFF = args.jump_diff #so the replica and the ground truth agree on what a jump is

    for remaining in range(int(args.delay), 0, -1):
        print(f"starting in {remaining}...")
        time.sleep(1)
    print(f"measuring for {args.seconds:.0f}s - interval {args.interval}s"
          f"{' (old loop)' if args.old else ''}. Teleport next to labelled items.\n")

    stop = threading.Event()
    jumps, diffs, scans = [], [], []
    threads = [threading.Thread(target=watch_for_jumps, args=(stop, jumps, diffs, args.jump_diff), daemon=True),
               threading.Thread(target=run_ocr, args=(stop, scans, args.interval, args.old), daemon=True)]
    for t in threads:
        t.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    summarise(jumps, scans, diffs, args.jump_diff)
    if args.save:
        #One timeline, so a teleport's difference can be lined up against the scans around it.
        with open(args.save, "w", encoding="utf-8") as f:
            f.write("time,kind,value,names\n")
            for t, d in diffs:
                f.write(f"{t:.3f},diff,{d:.2f},\n")
            for sc in scans:
                f.write(f"{sc['start']:.3f},scan,{(sc['published'] - sc['start']) * 1000:.0f},{'|'.join(sc['names'])}\n")
        print(f"timeline written to {args.save}")


if __name__ == "__main__":
    main()
