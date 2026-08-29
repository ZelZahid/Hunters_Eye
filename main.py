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

import actions
import game_state
import text_detection
from overlay import Overlay

#Globals
w, h = pyautogui.size() #Captures Screen Resolution [1920x1080]
monitor_area = {"top":0, "left":0, "width": w, "height": h}
#matchTemplate cost scales with frame area - profiling showed 0.5x costs ~43ms/call (~16 FPS
#ceiling on its own), while 0.25x costs ~12ms/call. This is the main FPS lever in this pipeline.
CAPTURE_SCALE = 0.3
SCALE_TO_NATIVE = 1 / CAPTURE_SCALE #detection runs at CAPTURE_SCALE - anything driving real mouse/screen coordinates (the overlay, auto-collect) needs to convert back
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
needle_image = cv.imread(str(ASSETS_DIR / "image1.png"))
needle_image = cv.resize(needle_image, (0,0) , fx=CAPTURE_SCALE, fy=CAPTURE_SCALE)
needle_w = needle_image.shape[1]
needle_h = needle_image.shape[0]
target_items = text_detection.load_target_items(ASSETS_DIR / "targets.txt") #item names to watch for via OCR
#HUD meters (health/mana orbs) to read a 0.0-1.0 fill level from - see game_state.py. Empty if
#assets/meters.json doesn't exist yet, which just means no game-state awareness, not a failure.
#Run 'python calibrate_meters.py' to create it.
meters = game_state.load_meters(ASSETS_DIR / "meters.json")

#Queues for thread-safe comms
screenshot_queue = Queue(maxsize = 3)
detection_queue = Queue(maxsize = 3)

#OCR is ~10-20x slower per call than image matching. Running it inside the same loop as image
#matching - even throttled - periodically stalls that loop for its full cost. So text detection
#gets its own thread entirely: it publishes its latest results here, and the fast image-matching
#loop just reads whatever's most recent without ever waiting on OCR.
text_tracks_lock = threading.Lock()
shared_text_tracks = [] #list of (x, y, w, h, matched_name, to_collect, color), in CAPTURE_SCALE coordinates - written by detect_text(), read by detect_objects() and run_auto_collect()

#Latest HUD meter readings as {name: 0.0-1.0 or None}. Unlike OCR, this does NOT need its own
#thread or its own capture: reading a meter is an HSV threshold over a ~50x50 region, a few
#tenths of a millisecond, so it rides along on the frame detect_objects() already has. A
#dedicated thread would have to grab its own frame (~17-20ms) to do a ~0.3ms measurement.
#None means "could not read", which is deliberately distinct from 0.0 ("empty") - a consumer
#must never treat a failed read as an empty health orb.
game_state_lock = threading.Lock()
shared_game_state = {}
GAME_STATE_DEBUG = False #prints the current readings periodically - useful when tuning meters.json
GAME_STATE_PRINT_INTERVAL_SECONDS = 2.0


def current_game_state():
    """Latest HUD readings, e.g. {'health': 0.82, 'mana': 0.44}. Returns a copy, so a caller
    can't be surprised by values changing mid-decision. This is the accessor the coming
    decision layer (potion drinking, the Pindle run, combat) reads from."""
    with game_state_lock:
        return dict(shared_game_state)

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
    global shared_game_state
    print("Detecting Objects...")
    meter_smoother = game_state.Smoother()
    last_state_print = 0.0

    while True:
        screenshot = screenshot_queue.get()

        #Read the HUD meters off this same frame before anything else touches it. Costs a few
        #tenths of a millisecond, so it does not meaningfully affect this loop's FPS.
        if meters:
            readings = meter_smoother.update(game_state.read_all(screenshot, meters))
            with game_state_lock:
                shared_game_state = readings
            if GAME_STATE_DEBUG and time.time() - last_state_print >= GAME_STATE_PRINT_INTERVAL_SECONDS:
                last_state_print = time.time()
                print("[game state: " + ", ".join(
                    f"{name}={'--' if value is None else format(value * 100, '.0f') + '%'}"
                    for name, value in readings.items()) + "]")

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

        #Image-template matches aren't tied to targets.txt's per-item [color] tags (those only
        #apply to OCR matches), so they always draw in the same default color OCR falls back to
        #when a target has no tag - one shared "default box color" instead of two separate ideas.
        rectangles = [(x, y, w, h, text_detection.DEFAULT_BOX_COLOR) for (x, y, w, h) in rectangles]

        #merge in whatever detect_text() last published - never blocks on OCR
        with text_tracks_lock:
            for (tx, ty, tw, th, _, _, color) in shared_text_tracks:
                rectangles.append((tx, ty, tw, th, color))

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
#The loop-rate/scan-gap/OCR-call-time prints below were how the OCR_INTERVAL_SECONDS and viewport
#margin tuning above was actually diagnosed (see the comments on those constants) - keep them
#available for the next time those need re-tuning, but they're too noisy to leave on by default.
OCR_DEBUG_TIMING = False

def _relocalize_track(frame, track):
    """Re-finds a previously OCR-matched item in the current frame via a small local
    matchTemplate search (cheap - a small search window against a small patch), instead of
    trusting its last known position. This is what keeps the box glued to the item as the
    game camera pans, in between the much-less-frequent full OCR refreshes."""
    x, y, w, h, matched_name, to_collect, color, patch = track
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
    return (new_x, new_y, w, h, matched_name, to_collect, color, fresh_patch)


def detect_text():
    global shared_text_tracks
    local_tracks = [] #(x, y, w, h, matched_name, to_collect, color, patch) - patch is only needed here for tracking
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
                if OCR_DEBUG_TIMING:
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
                if prev_scan_start is not None and OCR_DEBUG_TIMING:
                    print(f"[scan-to-scan gap: {last_ocr_time - prev_scan_start:.3f}s]")
                prev_scan_start = last_ocr_time

                frame_h = frame.shape[0]
                viewport_y0 = int(frame_h * VIEWPORT_TOP_MARGIN)
                viewport_y1 = int(frame_h * (1 - VIEWPORT_BOTTOM_MARGIN))
                viewport = frame[viewport_y0:viewport_y1, :]

                ocr_t0 = time.time()
                ocr_results = text_detection.find_text_matches(viewport, target_items)
                if OCR_DEBUG_TIMING:
                    print(f"[OCR call: {time.time() - ocr_t0:.3f}s]")

                local_tracks = []
                for (tx, ty, tw, th, matched_name, to_collect, color) in ocr_results:
                    ty += viewport_y0 #translate back from viewport-relative to full-frame coordinates
                    patch = frame[ty:ty + th, tx:tx + tw].copy()
                    local_tracks.append((tx, ty, tw, th, matched_name, to_collect, color, patch))
                    print(f"Found '{matched_name}'{' [to collect]' if to_collect else ''} at center {(tx + tw // 2, ty + th // 2)}")

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
                    (int(x * coord_scale), int(y * coord_scale), int(w * coord_scale), int(h * coord_scale), name, to_collect, color)
                    for (x, y, w, h, name, to_collect, color, _) in local_tracks
                ]

#Thread 4 - auto-collect: the first concrete use of the "action" seam from CLAUDE.md (move
#mouse / click). Reads whatever detect_text() published in shared_text_tracks (never blocks
#waiting on it, same as detect_objects()) and, for any track whose item was marked "*" in
#targets.txt, drives actions.click_until_gone() to click it until it's picked up or 5 seconds
#pass. The click loop itself lives in actions.py and knows nothing about item names or OCR -
#this thread is the game-specific glue that feeds it positions.
COLLECT_TIMEOUT_SECONDS = 5.0
#A click on an item in most games (D2R included) means "walk over there and pick it up on
#arrival", not an instant action - re-clicking much faster than that walk takes keeps
#re-issuing the move command, which can retarget/interrupt the character before it ever
#reaches the item (observed as missed pickups). This only paces actual clicks - see
#AUTO_COLLECT_POLL_SECONDS below for how often we still check whether it worked.
CLICK_RETRY_INTERVAL_SECONDS = 0.8
AUTO_COLLECT_POLL_SECONDS = 0.1 #how often to check for a new to-collect item when idle, and (during
                                 #an attempt) how often to re-check if the item is gone yet - kept
                                 #fast so success/position tracking stays responsive even though
                                 #actual clicks fire much less often (CLICK_RETRY_INTERVAL_SECONDS)
SNOOZE_SECONDS = 10.0 #'F4' suppresses auto-collect for this long, then it re-arms itself automatically -
                       #deliberately NOT a plain on/off toggle, since a toggle you forgot to flip back on
                       #means silently missing pickups indefinitely; a snooze can't be forgotten like that.
#Native-screen-pixel radius for "is this the same on-ground item" - both across the short gaps
#between clicks in an active attempt (the item's on-screen position drifts as the camera pans,
#same reason text_detection tracking needs TRACK_SEARCH_MARGIN) and for recognizing a
#previously-given-up item so we don't immediately re-attempt it while it's still sitting there.
ITEM_POSITION_MATCH_RADIUS = 200

snooze_until = 0.0 #module-level; set by the 'F4' hotkey callback, read by run_auto_collect()


def _snooze_auto_collect():
    global snooze_until
    snooze_until = time.time() + SNOOZE_SECONDS
    print(f"Auto-collect snoozed for {SNOOZE_SECONDS:.0f}s")


def _native_collectible_tracks():
    """Returns [(name, x, y, w, h)] for current to-collect OCR tracks, in real screen pixels."""
    with text_tracks_lock:
        tracks = list(shared_text_tracks)
    return [
        (name, int(x * SCALE_TO_NATIVE), int(y * SCALE_TO_NATIVE), int(w * SCALE_TO_NATIVE), int(h * SCALE_TO_NATIVE))
        for (x, y, w, h, name, to_collect, _color) in tracks if to_collect
    ]


def _track_near(tracks, name, x, y):
    """Among tracks with a matching name, returns whichever is within ITEM_POSITION_MATCH_RADIUS
    of (x, y) - disambiguates same-named items on screen at once and tolerates camera-pan drift."""
    for (n, tx, ty, tw, th) in tracks:
        if n != name:
            continue
        cx, cy = tx + tw // 2, ty + th // 2
        if (cx - x) ** 2 + (cy - y) ** 2 <= ITEM_POSITION_MATCH_RADIUS ** 2:
            return (n, tx, ty, tw, th)
    return None


def _is_abandoned(abandoned, name, cx, cy):
    """True if (name, cx, cy) is within ITEM_POSITION_MATCH_RADIUS of a previously-given-up
    attempt - i.e. this is almost certainly the same physical item we already failed to collect."""
    return any(
        a_name == name and (a_x - cx) ** 2 + (a_y - cy) ** 2 <= ITEM_POSITION_MATCH_RADIUS ** 2
        for (a_name, a_x, a_y) in abandoned
    )


def run_auto_collect():
    keyboard.add_hotkey('f4', _snooze_auto_collect)
    print(f"Auto-collect running - press 'F4' anytime to snooze it for {SNOOZE_SECONDS:.0f}s.")

    #Items we gave up on (5s of clicking, still on the ground): (name, x, y) at the moment we gave
    #up, so we don't immediately re-attempt the same physical item every poll tick. Pruned below
    #once that item is no longer seen nearby, so a *future* drop of the same item there is still
    #eligible - this only suppresses retrying the exact instance we already failed on.
    abandoned = []

    while True:
        if time.time() < snooze_until:
            time.sleep(AUTO_COLLECT_POLL_SECONDS)
            continue

        tracks = _native_collectible_tracks()

        #forget abandoned entries whose item is no longer sitting there (picked up, moved
        #off-screen, scene changed) - otherwise a future drop of the same item nearby would be
        #wrongly skipped forever instead of just "we already failed on this one instance"
        abandoned = [
            (name, x, y) for (name, x, y) in abandoned
            if _track_near(tracks, name, x, y) is not None
        ]

        candidates = [
            (name, x, y, w, h) for (name, x, y, w, h) in tracks
            if not _is_abandoned(abandoned, name, x + w // 2, y + h // 2)
        ]
        if not candidates:
            time.sleep(AUTO_COLLECT_POLL_SECONDS)
            continue

        cursor_x, cursor_y = pyautogui.position()
        target_name, tx, ty, tw, th = min(
            candidates, key=lambda t: (t[1] + t[3] // 2 - cursor_x) ** 2 + (t[2] + t[4] // 2 - cursor_y) ** 2
        )
        last_known = {"x": tx + tw // 2, "y": ty + th // 2}
        print(f"Auto-collect: attempting '{target_name}' at ({last_known['x']}, {last_known['y']})")

        def get_position(name=target_name, last_known=last_known):
            match = _track_near(_native_collectible_tracks(), name, last_known["x"], last_known["y"])
            if match is None:
                return None
            _, mx, my, mw, mh = match
            last_known["x"], last_known["y"] = mx + mw // 2, my + mh // 2
            return last_known["x"], last_known["y"]

        success = actions.click_until_gone(
            get_position, timeout=COLLECT_TIMEOUT_SECONDS, click_interval=CLICK_RETRY_INTERVAL_SECONDS,
            poll_interval=AUTO_COLLECT_POLL_SECONDS, is_paused=lambda: time.time() < snooze_until,
        )

        if success:
            print(f"Auto-collect: picked up '{target_name}'")
        else:
            print(f"Auto-collect: gave up on '{target_name}' after {COLLECT_TIMEOUT_SECONDS:.0f}s - releasing mouse control")
            abandoned.append((target_name, last_known["x"], last_known["y"]))


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
        for (x, y, w, h, color) in rectangles:
            cv.rectangle(screenshot, (x, y), (x + w, y + h), color[::-1], 2) #color is (r,g,b) - cv uses BGR

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

    quit_requested = threading.Event()
    #the overlay is click-through and can never take keyboard focus, so quitting needs a
    #global hotkey (same pattern legacy/pindle.py already uses) instead of a window keypress
    keyboard.add_hotkey("end", quit_requested.set)
    print("Overlay running - press 'End' anytime to quit.")

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
            (int(x * SCALE_TO_NATIVE), int(y * SCALE_TO_NATIVE), int(w * SCALE_TO_NATIVE), int(h * SCALE_TO_NATIVE), color)
            for (x, y, w, h, color) in rectangles
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
    t4 = threading.Thread(target=run_auto_collect, daemon=True)
    t1.start()
    t2.start()
    t3.start()
    t4.start()

    try:
        run_overlay()
    except NotImplementedError as e:
        print(f"WARNING: {e}\nFalling back to the plain debug window.")
        run_debug_window()

if __name__ == "__main__":
    main()
