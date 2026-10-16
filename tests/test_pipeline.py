"""main.py's threads wired together, on a real screen. No game required.

WHY THIS EXISTS: every stage of the pipeline has cards of its own - the capture fallbacks, the
image search, OCR matching, the meters - but until this file nothing ran the stages TOGETHER, the
way main() starts them: capture -> detect -> OCR, joined by queues and locks. That wiring is where
the worst failures live, and none of them shows up in a test of one stage:

  - a thread that dies on its first frame (every detector goes blind at once, silently);
  - a queue that stops draining, so the overlay freezes on the last thing it drew;
  - coordinates that are right inside a stage and wrong between two of them (a box drawn a long
    way from the thing it found - the capture scale and the OCR scale are different);
  - a box that outlives the thing it marked, because a lost track was never dropped;
  - a capture loop that stopped pacing itself and quietly eats CPU the game needed.

So this puts a real window on the real screen - the image main.py searches for, and the name of an
item from assets/targets.txt - starts the same three threads main() starts, and reads what comes
out of the far end of the pipeline, exactly where the overlay reads it. Nothing here clicks or
presses anything: the two threads that act (auto-collect, potions) are deliberately not started.

It needs a display, and OCR needs Tesseract. On a developer machine without them it says so and
skips; under CI (the CI environment variable, which GitHub Actions sets) a skip is a FAILURE,
because a check that quietly skips on the machine meant to run it looks exactly like a pass.

It takes over a corner of the screen for about twenty seconds - a black panel with a picture and
an item name on it. That is the test, not a glitch.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import threading
import time
from queue import Empty

ROOT = Path(__file__).resolve().parent.parent
UNDER_CI = bool(os.environ.get("CI"))
failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def finish():
    """Exit without waiting on the pipeline's daemon threads. One of them may be inside a
    Tesseract call; tearing the interpreter down around a C library mid-call can crash the exit
    itself, which would read as a failed test after every check has passed."""
    sys.stdout.flush()
    os._exit(1 if failures else 0)


def cannot_run(why):
    if UNDER_CI:
        check(f"needs a display and Tesseract, and CI has both - {why}", False)
    else:
        print(f"SKIPPED: {why} - CI runs this one for real")
    finish()


#main.py asks the screen for its size at import, so no display means no main. Imported BEFORE Tk:
#on Windows, pyautogui marks the process DPI-aware when it loads, and a Tk window created before
#that would be measured in scaled pixels while the capture sees real ones.
try:
    import main
except Exception as e:                      # noqa: BLE001 - no display, no X server, whatever
    cannot_run(f"main.py could not be imported ({type(e).__name__}: {e})")

from core import text_detection as td

try:
    import tkinter as tk
    root = tk.Tk()
except Exception as e:                      # noqa: BLE001 - no Tk, or nowhere to show it
    cannot_run(f"no window can be shown ({type(e).__name__}: {e})")

if not td.ocr_available():
    root.destroy()
    cannot_run("Tesseract is not installed")

#An item that is listed but NOT collected ("Super Mana Potion [blue]", no '*'): nothing in this
#test acts, but a name the bot would chase is the wrong thing to leave on a screen by habit.
TEXT = "Super Mana Potion"
TEXT_FONT = ("Arial", 26, "bold")
STARTUP_SECONDS = 25.0     #first frame + first OCR scan; a cold Tesseract on CI is the slow part
MEASURE_SECONDS = 3.0
GONE_SECONDS = 8.0         #an OCR track is dropped on its next failed relocalization, or next scan
#How far a box may land from where the thing really is, in screen pixels. Detection runs at
#CAPTURE_SCALE (0.3), so one pixel there is 3.3 here - the needle is held to a couple of those.
#OCR boxes are word boxes, padded differently from the font's own box, so they get more room.
NEEDLE_TOLERANCE_PX = 2 * main.SCALE_TO_NATIVE + 1
TEXT_TOLERANCE_PX = 30

needle_h, needle_w = main.needle_gray.shape[:2]
screen_x, screen_y, screen_w, screen_h = main.CAPTURE_RECT


#--- the window the pipeline is pointed at ------------------------------------------------------
#Black, so the white item name reads the way a label reads over dark terrain, and placed inside
#the band OCR scans (main.VIEWPORT_TOP_MARGIN .. 1 - VIEWPORT_BOTTOM_MARGIN of the screen height).
PANEL_W, PANEL_H = 560, 220
root.overrideredirect(True)
root.attributes("-topmost", True)
panel_x = screen_x + min(140, max(0, screen_w - PANEL_W))
panel_y = screen_y + int(screen_h * 0.35)
root.geometry(f"{PANEL_W}x{PANEL_H}+{panel_x}+{panel_y}")
canvas = tk.Canvas(root, width=PANEL_W, height=PANEL_H, bg="black", highlightthickness=0)
canvas.pack()
needle_photo = tk.PhotoImage(file=str(ROOT / "assets" / "image1.png"))
IMAGE_AT = (24, 24)
canvas.create_image(*IMAGE_AT, image=needle_photo, anchor="nw")
text_id = canvas.create_text(24, 130, text=TEXT, fill="white", font=TEXT_FONT, anchor="nw")
for _ in range(20):
    root.update()
    time.sleep(0.02)

origin_x, origin_y = root.winfo_rootx(), root.winfo_rooty()
image_w, image_h = needle_photo.width(), needle_photo.height()
#The image is drawn at native size; main.py's needle is the same file shrunk to CAPTURE_SCALE.
needle_centre = (origin_x + IMAGE_AT[0] + image_w / 2, origin_y + IMAGE_AT[1] + image_h / 2)
tx0, ty0, tx1, ty1 = canvas.bbox(text_id)
text_box = (origin_x + tx0, origin_y + ty0, origin_x + tx1, origin_y + ty1)
text_centre = ((text_box[0] + text_box[2]) / 2, (text_box[1] + text_box[3]) / 2)


#--- the pipeline, started the way main() starts it, minus the two threads that act -------------
main.IMAGE_SEARCH_ENABLED = True   #off in the shipped user_config.txt; this test is about the wiring
crashes = []
threading.excepthook = lambda args: crashes.append(f"{args.thread.name}: {args.exc_type.__name__}: {args.exc_value}")
threads = {name: threading.Thread(target=target, name=name, daemon=True)
           for name, target in (("capture", main.get_screenshot),
                                ("detect", main.detect_objects),
                                ("ocr", main.detect_text))}
for t in threads.values():
    t.start()


def native(box):
    """A CAPTURE_SCALE box -> screen pixels, the conversion the overlay does before drawing."""
    x, y, w, h = box[:4]
    s = main.SCALE_TO_NATIVE
    return (screen_x + x * s, screen_y + y * s, w * s, h * s)


def centre(box):
    x, y, w, h = box
    return (x + w / 2, y + h / 2)


def distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def needle_boxes(rectangles):
    """Image-search boxes are exactly the needle's size; OCR boxes never are."""
    return [native(r) for r in rectangles if (r[2], r[3]) == (needle_w, needle_h)]


def text_tracks():
    with main.text_tracks_lock:
        tracks = list(main.shared_text_tracks)
    return [native(t) for t in tracks if t[4].casefold() == TEXT.casefold()]


def next_result(timeout=0.05):
    """One (frame, rectangles) off the end of the pipeline - where run_overlay() reads it -
    keeping the panel window responsive while waiting."""
    root.update()
    try:
        return main.detection_queue.get(timeout=timeout)
    except Empty:
        return None


print("\nL-601  Capture -> detect -> OCR, wired together on a real screen")
print(f"  screen {screen_w}x{screen_h}, panel at ({panel_x}, {panel_y})")
first_frame = None
found_needle = found_text = None
frame_shape = None
start = time.perf_counter()
while time.perf_counter() - start < STARTUP_SECONDS and not (found_needle and found_text):
    result = next_result()
    if result is None:
        continue
    frame, rectangles = result
    if first_frame is None:
        first_frame = time.perf_counter() - start
        frame_shape = frame.shape
    boxes = needle_boxes(rectangles)
    if boxes and not found_needle:
        found_needle = (time.perf_counter() - start, boxes)
    tracks = text_tracks()
    if tracks and not found_text:
        found_text = (time.perf_counter() - start, tracks)

check(f"frames come out of the far end of the pipeline "
      f"({'first after %.2fs' % first_frame if first_frame is not None else 'none in %.0fs' % STARTUP_SECONDS})",
      first_frame is not None)
if frame_shape is not None:
    expected = (round(screen_h * main.CAPTURE_SCALE), round(screen_w * main.CAPTURE_SCALE))
    check(f"each one is a BGR frame at CAPTURE_SCALE: {frame_shape} (expected about {expected} x 3)",
          len(frame_shape) == 3 and frame_shape[2] == 3
          and abs(frame_shape[0] - expected[0]) <= 1 and abs(frame_shape[1] - expected[1]) <= 1)
if found_needle:
    seconds, boxes = found_needle
    best = min(distance(centre(b), needle_centre) for b in boxes)
    check(f"the image search found the picture after {seconds:.2f}s, {best:.1f}px from where it is "
          f"(allowed {NEEDLE_TOLERANCE_PX:.0f})", best <= NEEDLE_TOLERANCE_PX)
    check(f"exactly one box for one picture ({len(boxes)})", len(boxes) == 1)
else:
    check(f"the image search found the picture on screen (nothing in {STARTUP_SECONDS:.0f}s)", False)
if found_text:
    seconds, tracks = found_text
    best = min(tracks, key=lambda b: distance(centre(b), text_centre))
    gap = distance(centre(best), text_centre)
    overlaps = not (best[0] > text_box[2] or best[0] + best[2] < text_box[0]
                    or best[1] > text_box[3] or best[1] + best[3] < text_box[1])
    check(f"OCR read '{TEXT}' after {seconds:.2f}s and reported it {gap:.1f}px from where it is "
          f"(allowed {TEXT_TOLERANCE_PX})", gap <= TEXT_TOLERANCE_PX and overlaps)
else:
    check(f"OCR read '{TEXT}' on screen and reported it (nothing in {STARTUP_SECONDS:.0f}s)", False)


print("\nL-602  The pipeline paces itself, and every stage is still alive after running")
count = 0
cpu_start, wall_start = time.process_time(), time.perf_counter()
peak_backlog = 0
while time.perf_counter() - wall_start < MEASURE_SECONDS:
    if next_result() is not None:
        count += 1
    peak_backlog = max(peak_backlog, main.screenshot_queue.qsize(), main.detection_queue.qsize())
wall = time.perf_counter() - wall_start
cpu = time.process_time() - cpu_start
fps = count / wall
#The ceiling is the point: capture is paced to TARGET_FPS so it costs the game less CPU, and a loop
#that stopped pacing (DXGI can deliver ~177 FPS) would pass every other check here while eating
#three cores. The floor only catches a stall - CI machines are slow and that is not a failure.
check(f"{fps:.1f} FPS end to end: no faster than TARGET_FPS {main.TARGET_FPS} (+10%), "
      f"and not stalled (at least 5)", 5 <= fps <= main.TARGET_FPS * 1.1)
print(f"  info: {cpu / wall:.2f} CPU cores for the whole process while measuring "
      f"(capture + detect + OCR + this test's own window)")
for name, q in (("screenshot_queue", main.screenshot_queue), ("detection_queue", main.detection_queue)):
    check(f"{name} is bounded (maxsize {q.maxsize}) - an unbounded queue grows without limit "
          f"whenever a consumer falls behind", 1 <= q.maxsize <= 3)
check(f"no queue ever held more than its bound (peak {peak_backlog})", peak_backlog <= 3)
dead = [name for name, t in threads.items() if not t.is_alive()]
check(f"capture, detect and OCR threads all still running ({'dead: ' + ', '.join(dead) if dead else 'all alive'})",
      not dead)
check(f"no thread raised ({'; '.join(crashes) if crashes else 'none'})", not crashes)


print("\nL-603  Something taken off screen stops being reported")
root.withdraw()
hidden_at = time.perf_counter()
needle_gone = text_gone = None
frames_after = 0
while time.perf_counter() - hidden_at < GONE_SECONDS and not (needle_gone and text_gone):
    result = next_result()
    if result is None:
        continue
    frames_after += 1
    elapsed = time.perf_counter() - hidden_at
    if needle_gone is None and not needle_boxes(result[1]):
        needle_gone = elapsed
    if text_gone is None and not text_tracks():
        text_gone = elapsed
check(f"the picture's box went with it "
      f"({'after %.2fs' % needle_gone if needle_gone is not None else 'still there after %.0fs' % GONE_SECONDS})",
      needle_gone is not None)
#A lost OCR track reads as "picked up" to auto-collect, and a track that is NEVER lost is a box
#over bare ground forever - and an item the bot would keep walking to.
check(f"the item name's track was dropped "
      f"({'after %.2fs' % text_gone if text_gone is not None else 'still tracked after %.0fs' % GONE_SECONDS})",
      text_gone is not None)
check(f"frames kept coming while it did ({frames_after})", frames_after > 0)

root.destroy()
print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
finish()
