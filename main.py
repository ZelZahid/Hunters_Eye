'''
========================================================================================================
Hunters-Eye: Real-Time On-Screen Object Detection System
========================================================================================================

Author: Zelgehai Zahid
Repository: https://github.com/zel/Hunters_Eye
'''
import cv2 as cv
import pyautogui
import mss
import numpy as np
import time
import threading
import keyboard
from pathlib import Path
from queue import Queue, Empty

import text_detection
from overlay import Overlay

#Globals
w, h = pyautogui.size() #Captures Screen Resolution [1920x1080]
monitor_area = {"top":0, "left":0, "width": w, "height": h}
#matchTemplate cost scales with frame area - profiling showed 0.5x costs ~43ms/call (~16 FPS
#ceiling on its own), while 0.25x costs ~12ms/call. This is the main FPS lever in this pipeline.
CAPTURE_SCALE = 0.3
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
needle_image = cv.imread(str(ASSETS_DIR / "image1.png"))
needle_image = cv.resize(needle_image, (0,0) , fx=CAPTURE_SCALE, fy=CAPTURE_SCALE)
needle_w = needle_image.shape[1]
needle_h = needle_image.shape[0]
target_items = text_detection.load_target_items(ASSETS_DIR / "targets.txt") #item names to watch for via OCR

#Queues for thread-safe comms
screenshot_queue = Queue(maxsize = 3)
detection_queue = Queue(maxsize = 3)

#OCR is ~10-20x slower per call than image matching. Running it inside the same loop as image
#matching - even throttled - periodically stalls that loop for its full cost. So text detection
#gets its own thread entirely: it publishes its latest results here, and the fast image-matching
#loop just reads whatever's most recent without ever waiting on OCR.
text_tracks_lock = threading.Lock()
shared_text_tracks = [] #list of (x, y, w, h, matched_name), in CAPTURE_SCALE coordinates - written by detect_text(), read by detect_objects()

#Thread 1
def get_screenshot():
    with mss.mss() as sct:
        while True:
            img = sct.grab(monitor_area)
            screenshot = np.array(img)
            if screenshot.shape[2] == 4:
                screenshot = cv.cvtColor(screenshot, cv.COLOR_BGRA2BGR) #changes from BGRA to BGR [ stripping alpha val]
            screenshot = cv.resize(screenshot, (0,0) ,fx=CAPTURE_SCALE, fy=CAPTURE_SCALE) #resizing image for better performance

            #if detection is falling behind, dropping old frams lets pipeline tay real-time and not delayed
            if screenshot_queue.full():
                screenshot_queue.get_nowait() #remove and return an item without waiting, drops oldest screenshot
            screenshot_queue.put(screenshot) #thread-safe put

#Thread 2
def detect_objects():
    print("Detecting Objects...")
    while True:
        screenshot = screenshot_queue.get()
        #detection code-----------

        result = cv.matchTemplate(screenshot, needle_image, method=cv.TM_CCOEFF_NORMED) #returns confidence score
        threshold = 0.60
        max_results = 10 #Limiting results #
        locations = np.where(result >= threshold) #array([334,335]), array [91,92]
        #zip into position tuples:
        locations = list(zip(*locations[::-1]))

        #building list
        rectangles = [[int(x), int(y), needle_w, needle_h] for (x, y) in locations]
        scores = [float(result[y, x]) for (x, y) in locations]
        #collapsing overlapping matches down to their single best-scoring box
        #(cv.groupRectangles doesn't exist in OpenCV 5+, so this is done via NMS instead)
        indices = cv.dnn.NMSBoxes(rectangles, scores, threshold, nms_threshold=0.3)
        rectangles = [rectangles[i] for i in indices]
        if len(rectangles) > max_results:
            print("Too Many Results, raise the threshhold [max_results]")
            rectangles = rectangles[:max_results] #keep the top max_results matches

        #merge in whatever detect_text() last published - never blocks on OCR
        with text_tracks_lock:
            for (tx, ty, tw, th, _) in shared_text_tracks:
                rectangles.append([tx, ty, tw, th])

        #--------------------------
        detected_SS = screenshot.copy()
        detection_queue.put((detected_SS,rectangles)) #puts screenshot and rectangle locations in Queue

#Thread 3 - OCR-based text detection, fully decoupled from the image-matching loop above
#IMPORTANT: a single OCR call itself costs real time - measured ~0.9s against a full 1920x1080
#real gameplay frame with pytesseract (subprocess-per-call), cut to ~0.42-0.6s via the viewport
#crop below, then to ~0.1-0.26s by switching to tesserocr (in-process bindings - see
#text_detection.py). This interval must be well ABOVE the call's own cost, not below/near it -
#if it isn't, the "wait between calls" throttle does nothing (by the time one call finishes, the
#interval has usually already elapsed), so OCR ends up running back-to-back on nearly every loop
#iteration, starving the cheap relocalization step of any chance to run. Confirmed via the
#"[detect_text loop rate: ...]" diagnostic print below: a 0.3s interval (below the call's own
#cost, back when calls cost ~0.9s) dropped the loop to ~6-9 Hz.
OCR_INTERVAL_SECONDS = 0.4
#OCR needs far more pixel detail than icon matching does - in-game text at CAPTURE_SCALE (0.3)
#shrinks to just a few pixels tall and becomes unreadable. OCR only runs a few times a second,
#so it can afford its own, much less aggressively downscaled, capture.
OCR_CAPTURE_SCALE = 1.0
#Most games render their HUD (health/mana, inventory, minimap, clock) in fixed top/bottom
#margins, with actual gameplay - and item drops - occupying the middle viewport. Tesseract's
#segmentation cost scales with how much visual complexity it has to process, and a busy UI bar
#is expensive per pixel (lots of small icons/numbers) for zero benefit, since item labels never
#render there anyway. Excluding it cut a real measured OCR call from ~0.9s to ~0.42s - more than
#2x - for free (a slice, no extra processing). This is a general HUD-vs-viewport assumption, not
#hardcoded to Diablo II specifically, but it IS an assumption: if an item ever legitimately
#appears very close to the top or bottom edge and goes undetected, narrow these margins.
VIEWPORT_TOP_MARGIN = 0.08
VIEWPORT_BOTTOM_MARGIN = 0.25
#How far (px, at OCR_CAPTURE_SCALE) a tracked item can drift between consecutive relocalization
#checks before we give up finding it. Real gameplay logging showed jumps well beyond 150px
#between checks during active movement/direction changes (confidence 0.08-0.30 on the very next
#check - genuinely outside that window, not a near miss). Raised for headroom; the immediate
#post-OCR catch-up relocalization (see detect_text()) handles the biggest single source of drift.
TRACK_SEARCH_MARGIN = 250
TRACK_MIN_CONFIDENCE = 0.5 #below this, the item has likely been picked up or scrolled off-screen - drop the track

def _relocalize_track(frame, track):
    """Re-finds a previously OCR-matched item in the current frame via a small local
    matchTemplate search (cheap - a small search window against a small patch), instead of
    trusting its last known position. This is what keeps the box glued to the item as the
    game camera pans, in between the much-less-frequent full OCR refreshes."""
    x, y, w, h, matched_name, patch = track
    frame_h, frame_w = frame.shape[:2]
    sx1 = max(0, x - TRACK_SEARCH_MARGIN)
    sy1 = max(0, y - TRACK_SEARCH_MARGIN)
    sx2 = min(frame_w, x + w + TRACK_SEARCH_MARGIN)
    sy2 = min(frame_h, y + h + TRACK_SEARCH_MARGIN)
    search_window = frame[sy1:sy2, sx1:sx2]
    if search_window.shape[0] < h or search_window.shape[1] < w:
        print(f"TRACK LOST '{matched_name}': too close to frame edge to search")
        return None #too close to a frame edge to search meaningfully - drop it

    result = cv.matchTemplate(search_window, patch, cv.TM_CCOEFF_NORMED)
    _, confidence, _, top_left = cv.minMaxLoc(result)
    if confidence < TRACK_MIN_CONFIDENCE:
        print(f"TRACK LOST '{matched_name}': confidence {confidence:.2f} < {TRACK_MIN_CONFIDENCE}")
        return None #lost it - probably picked up or moved off-screen

    new_x = sx1 + top_left[0]
    new_y = sy1 + top_left[1]
    #Re-crop the patch from THIS frame instead of reusing the original OCR-time snapshot.
    #Without this, every relocalization compares against an increasingly stale reference -
    #fine while standing still (nothing around it changes), but it degrades fast while moving,
    #since the background behind the label and rendering context shift out from under it.
    fresh_patch = frame[new_y:new_y + h, new_x:new_x + w].copy()
    return (new_x, new_y, w, h, matched_name, fresh_patch)


def detect_text():
    global shared_text_tracks
    local_tracks = [] #(x, y, w, h, matched_name, patch) - patch is only needed here for tracking
    last_ocr_time = 0.0
    prev_scan_start = None #diagnostic only: measures the real gap between successive scan starts
    coord_scale = CAPTURE_SCALE / OCR_CAPTURE_SCALE #converts OCR_CAPTURE_SCALE coords -> CAPTURE_SCALE coords for shared_text_tracks

    #Diagnostic: this loop's own iteration rate. If it's much slower under real gameplay than on
    #an idle desktop (CPU/GPU contention from the game itself), relocalization can't keep up with
    #fast camera movement no matter how large TRACK_SEARCH_MARGIN is - this print tells us if
    #that's actually happening instead of guessing.
    loop_count = 0
    rate_window_start = time.time()

    def grab_frame():
        img = sct.grab(monitor_area)
        f = np.array(img)
        if f.shape[2] == 4:
            f = cv.cvtColor(f, cv.COLOR_BGRA2BGR)
        return cv.resize(f, (0,0), fx=OCR_CAPTURE_SCALE, fy=OCR_CAPTURE_SCALE)

    with mss.mss() as sct:
        while True:
            loop_count += 1
            elapsed = time.time() - rate_window_start
            if elapsed >= 2.0:
                print(f"[detect_text loop rate: {loop_count / elapsed:.1f} Hz, active tracks: {len(local_tracks)}]")
                loop_count = 0
                rate_window_start = time.time()

            frame = grab_frame()

            if time.time() - last_ocr_time >= OCR_INTERVAL_SECONDS:
                #Timestamp the START of the call, not its completion. A call takes ~0.5s and the
                #interval is 0.7s - timing from completion meant the real gap between scans was
                #interval + call duration (~1.2s), not just the interval as intended. Timing from
                #the start means the interval can elapse WHILE the call is running, so the next
                #scan can fire right after this one finishes instead of waiting a full interval again.
                last_ocr_time = time.time()
                if prev_scan_start is not None:
                    print(f"[scan-to-scan gap: {last_ocr_time - prev_scan_start:.3f}s]")
                prev_scan_start = last_ocr_time

                frame_h = frame.shape[0]
                viewport_y0 = int(frame_h * VIEWPORT_TOP_MARGIN)
                viewport_y1 = int(frame_h * (1 - VIEWPORT_BOTTOM_MARGIN))
                viewport = frame[viewport_y0:viewport_y1, :]

                ocr_t0 = time.time()
                ocr_results = text_detection.find_text_matches(viewport, target_items)
                print(f"[OCR call: {time.time() - ocr_t0:.3f}s]")

                local_tracks = []
                for (tx, ty, tw, th, matched_name) in ocr_results:
                    ty += viewport_y0 #translate back from viewport-relative to full-frame coordinates
                    patch = frame[ty:ty + th, tx:tx + tw].copy()
                    local_tracks.append((tx, ty, tw, th, matched_name, patch))
                    print(f"Found '{matched_name}' at center {(tx + tw // 2, ty + th // 2)}")

                #The OCR call above took real time (measured ~0.4-0.6s against a real viewport
                #crop) - the position it returned describes where
                #the item was BEFORE that call started, not now. Left as-is, the very first
                #relocalization attempt has to bridge that whole gap on top of a normal loop
                #iteration, which real testing showed failing almost every time while moving
                #(confidence 0.08-0.30, nowhere close to threshold - genuinely outside the
                #search window, not a near miss). Catch it up immediately against a fresh frame
                #instead of leaving it stale until the next loop iteration.
                catch_up_frame = grab_frame()
                local_tracks = [t for t in (_relocalize_track(catch_up_frame, track) for track in local_tracks) if t is not None]
            else:
                local_tracks = [t for t in (_relocalize_track(frame, track) for track in local_tracks) if t is not None]

            with text_tracks_lock:
                shared_text_tracks = [
                    (int(x * coord_scale), int(y * coord_scale), int(w * coord_scale), int(h * coord_scale), name)
                    for (x, y, w, h, name, _) in local_tracks
                ]

WINDOW_NAME = "Hunters Eye"
FPS_PRINT_INTERVAL_SECONDS = 5.0 #how often to print the FPS line - printing every single frame just floods the console

def run_debug_window():
    """Fallback for platforms the transparent overlay doesn't support yet (see overlay.py):
    a plain resizable window mirroring the (downscaled) captured frame with boxes drawn on it."""
    t0 = time.time()
    n_frames = 1
    window_start = t0 #resets every FPS_PRINT_INTERVAL_SECONDS - the printed FPS is an average over just that window, not the whole session
    window_frames = 0
    displayed_size = None #(width, height) the window is currently sized to - re-checked below so it adapts if resolution changes

    #WINDOW_NORMAL makes the window resizable and lets its content scale to fill whatever
    #size we set below, instead of being locked to the (tiny, downscaled) captured frame's own pixel size
    cv.namedWindow(WINDOW_NAME, cv.WINDOW_NORMAL)

    while True:
        screenshot, rectangles = detection_queue.get()
        for (x,y,w,h) in rectangles:
            cv.rectangle(screenshot, (x,y), (x+w,y+h), (0,255,0), 2)

        current_size = pyautogui.size() #cheap OS query - re-checked every frame so this stays correct if resolution changes mid-run
        if current_size != displayed_size:
            cv.resizeWindow(WINDOW_NAME, current_size.width, current_size.height)
            displayed_size = current_size

        cv.imshow(WINDOW_NAME, screenshot)

        if cv.waitKey(1) == ord('q'):
            break

        n_frames += 1
        window_frames += 1
        window_elapsed = time.time() - window_start
        if window_elapsed >= FPS_PRINT_INTERVAL_SECONDS:
            print(f"FPS: {window_frames / window_elapsed:.2f}")
            window_start = time.time()
            window_frames = 0

    cv.destroyAllWindows()
    #Final Stats [prints average Runtime FPS]
    t_final = time.time()
    total_elapsed = t_final - t0
    average_FPS = n_frames / total_elapsed
    print("total # of frames:", n_frames)
    print(f"Total Runtime: {total_elapsed:.2f}")
    print(f"Average FPS: {average_FPS:.2f}")


def run_overlay():
    """Draws detection boxes as a transparent, click-through layer directly on top of the
    game - no mirrored window. Runs on the main thread: Tk's event loop must own whichever
    thread created its window, so this can't be a spawned Thread like the others."""
    screen_size = pyautogui.size()
    overlay = Overlay(screen_size.width, screen_size.height)
    displayed_size = screen_size
    scale_to_native = 1 / CAPTURE_SCALE #detection runs at CAPTURE_SCALE - the overlay draws at real screen resolution

    quit_requested = threading.Event()
    #the overlay is click-through and can never take keyboard focus, so quitting needs a
    #global hotkey (same pattern legacy/pindle.py already uses) instead of a window keypress
    keyboard.add_hotkey("q", quit_requested.set)
    print("Overlay running - press 'q' anytime to quit.")

    t0 = time.time()
    n_frames = 1
    window_start = t0 #resets every FPS_PRINT_INTERVAL_SECONDS - the printed FPS is an average over just that window, not the whole session
    window_frames = 0
    while not quit_requested.is_set() and overlay.is_open():
        current_size = pyautogui.size()
        if current_size != displayed_size:
            overlay.resize(current_size.width, current_size.height)
            displayed_size = current_size

        try:
            _, rectangles = detection_queue.get(timeout=0.1)
        except Empty:
            overlay.pump()
            continue

        native_rectangles = [
            (int(x * scale_to_native), int(y * scale_to_native), int(w * scale_to_native), int(h * scale_to_native))
            for (x, y, w, h) in rectangles
        ]
        overlay.draw_rectangles(native_rectangles)
        overlay.pump()

        n_frames += 1
        window_frames += 1
        window_elapsed = time.time() - window_start
        if window_elapsed >= FPS_PRINT_INTERVAL_SECONDS:
            print(f"FPS: {window_frames / window_elapsed:.2f}")
            window_start = time.time()
            window_frames = 0

    overlay.close()
    total_elapsed = time.time() - t0
    average_FPS = n_frames / total_elapsed if total_elapsed > 0 else 0.0
    print("total # of frames:", n_frames)
    print(f"Total Runtime: {total_elapsed:.2f}")
    print(f"Average FPS: {average_FPS:.2f}")


def main():
    print("Starting Hunter's Eye...")

    t1 = threading.Thread(target=get_screenshot, daemon=True)
    t2 = threading.Thread(target=detect_objects, daemon=True)
    t3 = threading.Thread(target=detect_text, daemon=True)
    t1.start()
    t2.start()
    t3.start()

    try:
        run_overlay()
    except NotImplementedError as e:
        print(f"WARNING: {e}\nFalling back to the plain debug window.")
        run_debug_window()

if __name__ == "__main__":
    main()
