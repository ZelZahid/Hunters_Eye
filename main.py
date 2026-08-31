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
from collections import namedtuple
from pathlib import Path
from queue import Queue, Empty

import actions
import game_state
import text_detection
import window_region
import frame_source
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
#Template matching runs on GRAYSCALE, not colour, and this is a pure speed win rather than a
#trade: TM_CCOEFF_NORMED subtracts the mean and normalises across the whole patch, which on a
#3-channel image makes it very nearly colour-blind already. Measured against this needle, a
#fully DESATURATED copy and copies hue-rotated by 30/60/90/120 degrees all still scored 1.000 in
#colour - so the two extra channels were buying no discrimination whatsoever while costing
#17.5ms per frame against 3.2ms for one channel. False-positive headroom on real screen content
#is unchanged too (best non-match: 0.365 grey vs 0.353 colour, both far below the 0.60 threshold).
#If a future needle ever DOES need colour to be told apart from a look-alike, that is a job for an
#HSV pre-mask (see legacy/vision.py) rather than for 3-channel matchTemplate, which cannot do it.
needle_gray = cv.cvtColor(needle_image, cv.COLOR_BGR2GRAY)
target_items = text_detection.load_target_items(ASSETS_DIR / "targets.txt") #item names to watch for via OCR
#HUD meters (health/mana orbs) to read a 0.0-1.0 fill level from - see game_state.py. Empty if
#assets/meters.json doesn't exist yet, which just means no game-state awareness, not a failure.
#Run 'python calibrate_meters.py' to create it.
#One calibration profile per window shape - a game re-lays out its HUD at a different resolution
#or aspect ratio, so the right regions depend on how the game is currently displayed. See
#game_state.Profile and window_region.py.
meter_profiles = game_state.load_profiles(ASSETS_DIR / "meters.json")
meters = list(meter_profiles[0].meters) if meter_profiles else []  #names only, for the panel
meter_anchor = meter_profiles[0].anchor if meter_profiles else None
CAPTURE_RECT = (monitor_area["left"], monitor_area["top"], monitor_area["width"], monitor_area["height"])
#How often to re-locate the anchor window. It only moves when the user drags or resizes it, so
#this does not need to be per-frame - and EnumWindows is far too expensive to run at 45 FPS.
ANCHOR_REFRESH_SECONDS = 1.0
#Meter regions are fractions of the anchor window, which survives the window being moved or
#resized. It does NOT survive the GAME changing resolution or aspect ratio, because the game
#re-lays out its HUD: measured on D2R, 1920x1080 -> 1280x800 held the orbs' vertical position and
#height to within 0.001 of the same fraction but moved them horizontally by 0.033 - 42px against
#a 104px orb, i.e. half off. Compare the live aspect against the calibrated one and say so,
#because the failure is a plausible-but-wrong percentage rather than an error.
ANCHOR_ASPECT_TOLERANCE = 0.02
_anchor_aspect_warned = False

_anchor_lock = threading.Lock()
#A distinct "never resolved" sentinel, NOT None: None is a real, meaningful result here ("the
#window isn't on screen"), so seeding _anchor_rect with it made the first lookup of a missing
#window compare equal to the initial state, skip the update, and hand back the un-rewritten
#screen-relative meters - reading the wrong pixels at exactly the moment we meant to report
#"cannot read". A cache keyed on a value that is itself a valid answer needs its own empty state.
_UNRESOLVED = object()
_anchor_rect = _UNRESOLVED #last known client rect of the anchor window, in screen pixels
_anchor_handle = None      #opaque window handle for the cheap per-frame focus check, see anchor_focused()
_active_profile = None     #the calibration profile matching the current window shape
_anchor_meters = meters    #meters with regions rewritten against the captured frame
_anchor_checked = 0.0


def _refresh_anchor(now):
    """Re-locates the anchor window and rewrites the meter regions against the captured frame.

    Returns the meters to actually read this frame. When an anchor is configured but its window
    cannot be found (game closed, minimised, or dragged onto a monitor we do not capture) this
    returns NO meters, so every reading becomes None - "cannot read" - rather than 0%. Reporting
    a full health orb as empty because the window moved is precisely the confusion game_state.py
    is built to prevent, and it would drive a potion or a retreat.
    """
    global _anchor_rect, _anchor_handle, _anchor_meters, _anchor_checked, _active_profile
    if not meter_profiles or not meter_anchor:
        return meters

    with _anchor_lock:
        if now - _anchor_checked < ANCHOR_REFRESH_SECONDS:
            return _anchor_meters
        _anchor_checked = now

    found = window_region.find_window(meter_anchor.get("window_title", ""))
    rect = None if found is None else found[1]
    with _anchor_lock:
        #The handle is refreshed even when the rect is unchanged, and BEFORE the shortcut below.
        #A game closed and relaunched at the same size and position is a different window with an
        #identical rect - keeping the old handle would leave the focus check comparing against a
        #window that no longer exists, i.e. permanently False, i.e. the guard stuck on.
        _anchor_handle = None if found is None else found[0]
        if rect == _anchor_rect:
            return _anchor_meters
        _anchor_rect = rect
        if rect is None:
            _active_profile = None
            _anchor_meters = []
            return _anchor_meters

        #Pick the profile calibrated for this window shape. select_profile returns None rather
        #than a near-enough match on purpose: measuring the wrong pixels and reporting a
        #confident number is worse than reporting nothing.
        _active_profile = game_state.select_profile(meter_profiles, (rect[2], rect[3]))
        if _active_profile is None:
            _warn_no_profile(rect)
            _anchor_meters = []
            return _anchor_meters

        rebuilt = []
        for m in _active_profile.meters:
            region = window_region.to_frame_fractions(rect, CAPTURE_RECT, m.region)
            if region is not None:
                rebuilt.append(game_state.Meter(m.name, region, m.hsv_ranges, m.shape, m.fill_from))
        _anchor_meters = rebuilt
        return _anchor_meters


def unprofiled_size():
    """(w, h) of the anchor window when NO calibration profile matches its shape, else None.

    Surfaced ON THE PANEL, not just the console: the console scrolls and nobody watching a game
    is reading it, so a console-only warning about a silently wrong reading is a warning nobody
    receives. Observed exactly that way - the warning fired, went unseen, and the readings looked
    confidently wrong on screen."""
    rect = anchor_rect()
    if rect is None or not meter_profiles:
        return None
    return None if _active_profile is not None else (rect[2], rect[3])


def known_profile_sizes():
    return [p.client_size for p in meter_profiles if p.client_size]


def _warn_no_profile(rect):
    """One-time console warning that this window shape has never been calibrated."""
    global _anchor_aspect_warned
    if _anchor_aspect_warned:
        return
    _anchor_aspect_warned = True
    have = ", ".join(f"{w}x{h}" for (w, h) in known_profile_sizes()) or "none"
    print(f"WARNING: no meter profile for a {rect[2]}x{rect[3]} window (calibrated: {have}).")
    print("         A game re-lays out its HUD at a different aspect ratio, so an existing "
          "profile cannot be reused.")
    print("         Run 'python calibrate_meters.py' at this size - it ADDS a profile, it does "
          "not replace the ones you have.")


def anchor_rect():
    """Last known client rect of the anchor window, or None. Used to place the debug panel."""
    with _anchor_lock:
        return None if _anchor_rect is _UNRESOLVED else _anchor_rect


def anchor_focused():
    """True/False/None - is the anchor window the one the user is actually interacting with?

    None means "cannot tell", and is NOT the same as False. There are three ways to get it and
    all three must leave the pipeline behaving exactly as it did before this guard existed:
    no pywin32 (macOS, or a Windows install without it), no meters.json / no "_anchor" in it
    (nothing to anchor to), or the window not currently found. Collapsing None into False would
    disable every meter reading and every action permanently on those setups.

    Cheap enough to call per frame - see window_region.is_foreground().
    """
    with _anchor_lock:
        handle = _anchor_handle
    return window_region.is_foreground(handle)


def actions_allowed():
    """Whether it is safe to drive the real mouse/keyboard right now.

    Allowed unless we POSITIVELY know the anchor window is not focused. The asymmetry is the
    point: an unguarded setup keeps working as before, while a setup that can check gets stopped.
    """
    return anchor_focused() is not False


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
shared_game_state_time = 0.0 #time.time() of the last publish - lets a reader tell "the meter reads 0%"
                              #from "nothing has updated this in seconds because the reader stopped"
GAME_STATE_DEBUG = False #prints the current readings periodically - useful when tuning meters.json
GAME_STATE_PRINT_INTERVAL_SECONDS = 2.0


def current_game_state():
    """Latest HUD readings, e.g. {'health': 0.82, 'mana': 0.44}. Returns a copy, so a caller
    can't be surprised by values changing mid-decision. This is the accessor the coming
    decision layer (potion drinking, the Pindle run, combat) reads from."""
    with game_state_lock:
        return dict(shared_game_state)


def current_game_state_age():
    """Seconds since the HUD readings were last refreshed, or None if they never have been.

    A value on its own cannot distinguish "health really is 12%" from "the reader stopped
    updating three seconds ago and 12% is just what it last saw". That matters for anything
    that acts on the number, and it is exactly what the debug panel has to be able to show."""
    with game_state_lock:
        stamped = shared_game_state_time
    return None if stamped == 0.0 else time.time() - stamped

#The pipeline is PACED, not run flat out, and that is the whole point of the frame source change.
#Capture used to BE the throttle: mss could only produce ~58 frames a second, so the loop never
#had to decide how fast to run. DXGI can produce ~177, and taking all of them costs 3.56 CPU
#cores instead of 0.42 - which is the exact opposite of what this project wants, since design
#priority #2 is "must never noticeably slow down whatever it is watching". Higher FPS is not the
#goal; the same work for less CPU is. Measured end to end on the real pipeline:
#    mss, unpaced (what this was)   55-59 FPS   0.78-0.84 cores   15.3 CPU-ms/frame
#    DXGI, paced to 60              59.7 FPS    0.42 cores         7.0 CPU-ms/frame
#    DXGI, unpaced                   177 FPS    3.56 cores        20.1 CPU-ms/frame
#Frame pacing is also visibly steadier, not just cheaper: p95 frame gap 18.4ms vs 21.9ms and
#worst case 21.1ms vs 32.5ms, because the loop is no longer racing an unpredictable blit.
#60 is chosen rather than higher because nothing downstream can use more: the overlay caps its
#own repaints at BOX_REDRAW_HZ (30), auto-collect clicks coordinates from shared_text_tracks
#rather than from this path at all, and game_state's readings are median-smoothed over 5 frames
#(83ms at 60 FPS). Raise it only if a future consumer genuinely needs lower detection latency,
#and re-measure the CPU cost when you do.
TARGET_FPS = 60
_FRAME_PERIOD = 1.0 / TARGET_FPS

#Thread 1
def get_screenshot():
    #See frame_source.py. This is the ONLY DXGI consumer in the process on purpose - a DXGI
    #duplicator is a per-output singleton, so detect_text() asking for a second one would not get
    #a second one, it would get this one and start stealing frames out from under this thread.
    source = frame_source.FrameSource(monitor_area)
    while True:
        started = time.perf_counter()
        #A VIEW over the backend's own buffer, not a copy - np.array() on it would duplicate a
        #full 1920x1080x4 (~8MB) buffer every single frame (~4ms, plus 400MB/s of memory
        #bandwidth every other thread then shares). It must be consumed before the next grab():
        #the resize immediately below copies the pixels out, and nothing keeps a reference past
        #that point.
        screenshot = source.grab()
        if screenshot is None:
            #DXGI has produced nothing at all yet. Not an error and not a stall - just wait for
            #the first frame rather than spinning on it.
            time.sleep(0.001)
            continue
        #Downscale FIRST, then strip the alpha channel - not the other way round. The result is
        #bit-for-bit identical (verified over a full frame: max difference 0), because
        #INTER_LINEAR mixes each channel independently and alpha is discarded either way. The
        #only thing that changes is how much data the colour conversion has to touch: 576x324
        #pixels instead of 1920x1080.
        screenshot = cv.resize(screenshot, (0,0) ,fx=CAPTURE_SCALE, fy=CAPTURE_SCALE) #resizing image for better performance
        if screenshot.shape[2] == 4:
            screenshot = cv.cvtColor(screenshot, cv.COLOR_BGRA2BGR) #changes from BGRA to BGR [ stripping alpha val]

        #if detection is falling behind, dropping old frams lets pipeline tay real-time and not delayed
        if screenshot_queue.full():
            screenshot_queue.get_nowait() #remove and return an item without waiting, drops oldest screenshot
        screenshot_queue.put(screenshot) #thread-safe put

        #Sleep out the REMAINDER of the frame, timed from when this iteration started - not a
        #fixed sleep after the work. A fixed sleep would make the real period "period + however
        #long the work took", the same off-by-a-call-duration mistake documented for
        #last_ocr_time in detect_text().
        remaining = _FRAME_PERIOD - (time.perf_counter() - started)
        if remaining > 0:
            time.sleep(remaining)

#Thread 2
def detect_objects():
    global shared_game_state, shared_game_state_time
    print("Detecting Objects...")
    meter_smoother = game_state.Smoother()
    last_state_print = 0.0

    while True:
        screenshot = screenshot_queue.get()

        #Read the HUD meters off this same frame before anything else touches it. Costs a few
        #tenths of a millisecond, so it does not meaningfully affect this loop's FPS.
        active_meters = _refresh_anchor(time.time())
        if anchor_focused() is False:
            #Not the foreground window -> the pixels where the orbs should be belong to whatever
            #the user alt-tabbed to, so there is nothing here to measure. Checked per frame, not
            #at ANCHOR_REFRESH_SECONDS: focus flips in well under a second and a stale "yes" is
            #exactly the window in which a wrong reading gets acted on.
            active_meters = []
        if meters:
            #Anchor lost, or not focused -> every configured meter reports None, never 0.0. A
            #failed read presented as an empty orb reads as "you are about to die" and would
            #drive a potion or a retreat - see game_state.py on why None and 0.0 stay distinct.
            raw = (game_state.read_all(screenshot, active_meters) if active_meters
                   else {m.name: None for m in meters})
            readings = meter_smoother.update(raw)
            with game_state_lock:
                shared_game_state = readings
                shared_game_state_time = time.time()
            if GAME_STATE_DEBUG and time.time() - last_state_print >= GAME_STATE_PRINT_INTERVAL_SECONDS:
                last_state_print = time.time()
                print("[game state: " + ", ".join(
                    f"{name}={'--' if value is None else format(value * 100, '.0f') + '%'}"
                    for name, value in readings.items()) + "]")

        #detection code-----------

        #Grayscale, and only AFTER game_state has read the frame above - meter reading is an HSV
        #threshold and genuinely needs the colour. See needle_gray for why matching does not.
        frame_gray = cv.cvtColor(screenshot, cv.COLOR_BGR2GRAY) #0.03ms at CAPTURE_SCALE
        result = cv.matchTemplate(frame_gray, needle_gray, method=cv.TM_CCOEFF_NORMED) #returns confidence score
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
        #No .copy() here: get_screenshot() allocates a fresh array per frame and this thread does
        #not mutate it (the grayscale conversion above produced a new array rather than writing
        #back into this one), so once it is handed off nothing else holds a reference to it.
        detection_queue.put((screenshot, rectangles)) #puts screenshot and rectangle locations in Queue

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
#How long to idle between clock checks when this thread has nothing to do - no tracks to
#relocalize and no scan yet due. See the loop in detect_text() for why this matters.
IDLE_POLL_SECONDS = 0.02

def _relocalize_track(frame, track):
    """Re-finds a previously OCR-matched item in the current frame via a small local
    matchTemplate search (cheap - a small search window against a small patch), instead of
    trusting its last known position. This is what keeps the box glued to the item as the
    game camera pans, in between the much-less-frequent full OCR refreshes.

    `frame` and the track's stored patch are both SINGLE-CHANNEL. "Cheap" was doing a lot of
    work in that sentence before: at OCR_CAPTURE_SCALE=1.0 the search window is ~700x530 native
    pixels, and in colour that measured 27-33ms PER TRACKED ITEM, every loop iteration - so three
    items on the ground cost this thread ~100ms a pass and saturated a core exactly when
    something was worth tracking. In grey it is 5.7ms. Accuracy is not the price: over 20
    perturbed relocalizations (drift, dimming, added noise) grey found the correct position 20/20
    against colour's 18/20, and scored equal or higher confidence on every single one. Same
    reason as needle_gray - TM_CCOEFF_NORMED is already all but colour-blind."""
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

    def _grab(grey):
        """One capture at OCR_CAPTURE_SCALE, as BGR or as a single channel.

        Two details worth keeping. The resize is SKIPPED at scale 1.0 (where it sits now):
        cv.resize at fx=1.0 is not a no-op, it is a full-resolution copy, and it was costing
        ~3.7ms on every capture to produce an identical array. And the grey path converts BGRA
        straight to one channel instead of building a 3-channel full-resolution intermediate it
        would throw away a moment later."""
        img = sct.grab(monitor_area)
        #A view over mss's buffer, not a copy - see get_screenshot(). Consumed by the cvtColor
        #immediately below, which is what produces the array this actually returns.
        f = np.frombuffer(img.raw, dtype=np.uint8).reshape(img.height, img.width, 4)
        if f.shape[2] == 4:
            f = cv.cvtColor(f, cv.COLOR_BGRA2GRAY if grey else cv.COLOR_BGRA2BGR)
        elif grey:
            f = cv.cvtColor(f, cv.COLOR_BGR2GRAY)
        if OCR_CAPTURE_SCALE != 1.0:
            f = cv.resize(f, (0,0), fx=OCR_CAPTURE_SCALE, fy=OCR_CAPTURE_SCALE)
        return f

    def grab_frame():
        """BGR frame - what the OCR detector is handed."""
        return _grab(grey=False)

    def grab_frame_grey():
        """Single-channel frame - all track relocalization needs. See _relocalize_track()."""
        return _grab(grey=True)

    with mss.mss() as sct:
        while True:
            loop_count += 1
            elapsed = time.time() - rate_window_start
            if elapsed >= 2.0:
                if OCR_DEBUG_TIMING:
                    print(f"[detect_text loop rate: {loop_count / elapsed:.1f} Hz, active tracks: {len(local_tracks)}]")
                loop_count = 0
                rate_window_start = time.time()

            #Do not capture a frame this iteration would only throw away. With no tracks to
            #relocalize and no scan due, everything below is a no-op - but the capture itself is
            #not free: a full-resolution grab plus conversion measured ~21ms, and this loop ran
            #it ~45 times a second, so an idle thread was burning an entire CPU core and roughly
            #400MB/s of memory bandwidth competing with the two threads that actually set the
            #FPS. Nothing on screen is the normal case, so this is most of the time.
            until_scan = OCR_INTERVAL_SECONDS - (time.time() - last_ocr_time)
            if until_scan > 0 and not local_tracks:
                time.sleep(min(until_scan, IDLE_POLL_SECONDS))
                continue

            if until_scan <= 0:
                frame = grab_frame()
                #Timestamp the START of the call, not its completion. A call takes ~0.5s and the
                #interval is 0.7s - timing from completion meant the real gap between scans was
                #interval + call duration (~1.2s), not just the interval as intended. Timing from
                #the start means the interval can elapse WHILE the call is running, so the next
                #scan can fire right after this one finishes instead of waiting a full interval again.
                last_ocr_time = time.time()
                if prev_scan_start is not None and OCR_DEBUG_TIMING:
                    print(f"[scan-to-scan gap: {last_ocr_time - prev_scan_start:.3f}s]")
                prev_scan_start = last_ocr_time

                #Patches are cropped from the grey view of this same frame, since that is what
                #relocalization matches against from here on.
                frame_grey = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
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
                    patch = frame_grey[ty:ty + th, tx:tx + tw].copy()
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
                catch_up_frame = grab_frame_grey()
                local_tracks = [t for t in (_relocalize_track(catch_up_frame, track) for track in local_tracks) if t is not None]
            else:
                frame_grey = grab_frame_grey()
                local_tracks = [t for t in (_relocalize_track(frame_grey, track) for track in local_tracks) if t is not None]

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
    was_blocked = False  #so the console says it once per transition, not once per poll tick

    while True:
        #Do not drive the mouse at screen coordinates when the game is not the window those
        #coordinates now belong to - the clicks would land in whatever is in front of it. This is
        #a hard stop rather than a warning: unlike a wrong meter reading, a stray click is already
        #an action taken by the time anyone could react to it.
        if not actions_allowed():
            if not was_blocked:
                was_blocked = True
                print("Auto-collect paused - game window is not focused.")
            time.sleep(AUTO_COLLECT_POLL_SECONDS)
            continue
        if was_blocked:
            was_blocked = False
            print("Auto-collect resumed - game window focused again.")

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
            poll_interval=AUTO_COLLECT_POLL_SECONDS,
            #Also mid-attempt, not just before starting one: an attempt runs for up to
            #COLLECT_TIMEOUT_SECONDS and clicks repeatedly throughout, so alt-tabbing one click
            #into it would otherwise let the rest of them land somewhere else entirely.
            is_paused=lambda: time.time() < snooze_until or not actions_allowed(),
        )

        if success:
            print(f"Auto-collect: picked up '{target_name}'")
        else:
            print(f"Auto-collect: gave up on '{target_name}' after {COLLECT_TIMEOUT_SECONDS:.0f}s - releasing mouse control")
            abandoned.append((target_name, last_known["x"], last_known["y"]))



#--- Potion drinking ---------------------------------------------------------------------------
#The first consumer of game_state that ACTS on what it reads, and the whole sensor -> decision ->
#action loop end to end: read a number off the screen, decide, press a key.
#
#This block is Diablo II INTEGRATION, not engine - same status as run_auto_collect(). The belt
#layout is one player's key bindings and the thresholds are playstyle, so both live here in data
#rather than being spread through logic. Nothing below knows what a "potion" is beyond "a key to
#press when a meter gets low", which is the same shape as a drone landing itself on low battery.
#
#PotionRule: when `meter` reads at or below `at_or_below`, press one of `keys`, then hold off on
#THIS rule for `cooldown` seconds.
PotionRule = namedtuple("PotionRule", "meter at_or_below keys cooldown label")

#Order matters: the FIRST matching rule wins each tick, so more urgent rules come first.
#Each rule keeps its OWN cooldown, which is what lets the emergency tier fire immediately after
#an ordinary heal - taking a big hit one second after drinking a healing potion is exactly when
#a rejuvenation is needed, and a single shared cooldown would swallow it.
POTION_RULES = (
    #Rejuvenation heals instantly and fully, so it is the emergency tier, not the everyday one.
    #Keys 2 and 3 are BOTH rejuvenation columns and are used alternately: nothing here can see how
    #many potions a column has left (belt counting is not built), so alternating means a run of
    #emergencies drains both columns evenly instead of emptying one and then pressing a dead key.
    PotionRule("health", 0.20, ("2", "3"), 1.0, "rejuvenation"),
    #Ordinary healing potion. Heals over several seconds rather than instantly, so the cooldown
    #has to outlast the heal - re-checking before it lands reads a still-low orb and drinks again,
    #which is how a belt empties in one second.
    PotionRule("health", 0.35, ("1",), 4.0, "health"),
    PotionRule("mana", 0.25, ("4",), 4.0, "mana"),
)
#A floor under EVERY potion press regardless of which rule fired. The per-rule cooldowns above are
#the tuning knob; this is the safety net, so a mistuned threshold or a misreading orb cannot empty
#the belt in a second no matter what combination of rules matches.
POTION_MIN_GAP_SECONDS = 0.6
POTION_POLL_SECONDS = 0.15
POTION_SNOOZE_SECONDS = 10.0 #'F6', same re-arming snooze as auto-collect's 'F4' - see SNOOZE_SECONDS
POTION_DRINKING_ENABLED = True #one edit to turn the whole thing off without deleting anything

potion_snooze_until = 0.0


def _snooze_potions():
    global potion_snooze_until
    potion_snooze_until = time.time() + POTION_SNOOZE_SECONDS
    print(f"Potion drinking snoozed for {POTION_SNOOZE_SECONDS:.0f}s")


def _potion_due(readings, now, last_fired, last_any):
    """The first POTION_RULES entry that should fire right now, or None.

    Kept PURE - no key presses, no clock of its own, no globals - so the decision can be tested
    directly. The thing this decides is "press a key into a live game", which is not something to
    validate by playing and hoping; see test_potions.py.
    """
    if now - last_any < POTION_MIN_GAP_SECONDS:
        return None
    for rule in POTION_RULES:
        value = readings.get(rule.meter)
        if value is None:
            continue  #cannot read it -> cannot act on it. NOT an emergency. See run_potion_drinking.
        if value > rule.at_or_below:
            continue
        if now - last_fired.get(rule.label, 0.0) < rule.cooldown:
            continue
        return rule
    return None


def run_potion_drinking():
    """Drinks a potion when a meter reads low. Reads shared_game_state; presses a belt key.

    THE ONE RULE THAT MATTERS: a meter reading of None means "could not read", and this must then
    do NOTHING. None is not a low value and must never be treated as one - if it were, alt-tabbing
    or losing the game window would look like an emergency and dump the belt. game_state.py keeps
    None and 0.0 distinct for exactly this consumer, and Smoother stops carrying a stale value
    after MAX_CARRIED_FAILURES so a frozen number cannot masquerade as a live one either.
    """
    if not POTION_DRINKING_ENABLED:
        return
    keyboard.add_hotkey('f6', _snooze_potions)
    rules = ", ".join(f"{r.label} <={r.at_or_below:.0%} -> {'/'.join(r.keys)}" for r in POTION_RULES)
    print(f"Potion drinking running ({rules}) - press 'F6' to snooze it for {POTION_SNOOZE_SECONDS:.0f}s.")

    last_fired = {}       #rule label -> when it last fired, so each rule cools down on its own
    next_key = {}         #rule label -> index into rule.keys, for alternating columns
    last_any = 0.0        #when ANY potion was last pressed, for POTION_MIN_GAP_SECONDS
    was_blocked = False

    while True:
        time.sleep(POTION_POLL_SECONDS)
        now = time.time()

        #Same guard as auto-collect: never send input to a window that is not the game. A belt
        #key going to whatever is in front is at best noise and at worst a keystroke in a chat box.
        if not actions_allowed():
            if not was_blocked:
                was_blocked = True
                print("Potion drinking paused - game window is not focused.")
            continue
        if was_blocked:
            was_blocked = False
            print("Potion drinking resumed - game window focused again.")

        if now < potion_snooze_until:
            continue

        readings = current_game_state()
        rule = _potion_due(readings, now, last_fired, last_any)
        if rule is None:
            continue

        #Alternate across a rule's keys. Nothing here can see how many potions a belt column has
        #left, so spreading the presses is the only way to avoid emptying one column and then
        #pressing a dead key during an emergency.
        index = next_key.get(rule.label, 0)
        key = rule.keys[index % len(rule.keys)]
        next_key[rule.label] = index + 1
        actions.press_key(key)
        last_fired[rule.label] = now
        last_any = now
        print(f"Potion: {rule.label} (key '{key}') at {rule.meter} {readings[rule.meter]:.0%}")


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


#--- Game-state debug panel --------------------------------------------------------------------
#A read-out of what game_state.py is currently measuring, drawn on the SAME overlay window as the
#detection boxes but as a separate, independently-redrawn layer (see Overlay.draw_panel). Sharing
#that window rather than opening a second one is deliberate: the overlay's see-through +
#click-through behaviour depends on a pair of fragile Win32 details that have already caused two
#bugs (see CLAUDE.md / Error_history.txt #7, #10), and a second window would be a second copy of
#them to get right and keep right, for no visual difference on screen.
DEBUG_PANEL_STALE_SECONDS = 1.0 #older than this and the panel says so instead of showing a number
DEBUG_PANEL_LOW = 0.25          #at or below this a value is drawn red...
DEBUG_PANEL_MID = 0.50          #...amber up to here, green above
DEBUG_PANEL_BAD_COLOR = (255, 70, 70)
PANEL_INSET_X = 24              #panel position inside the anchor window, in pixels...
PANEL_INSET_Y_FRACTION = 0.30   #...and as a fraction of its height, so it sits sensibly at any size
debug_panel_visible = True #toggled by 'F5'; starts on, since the panel exists to be watched


def _toggle_debug_panel():
    global debug_panel_visible
    debug_panel_visible = not debug_panel_visible
    print(f"Game-state panel {'shown' if debug_panel_visible else 'hidden'} ('F5' to toggle)")


def _value_color(value):
    """Green/amber/red by how low the value is. Colouring by VALUE rather than by meter NAME is
    what keeps this game-agnostic: "low is bad" is equally true of a health orb, a mana globe and
    a robot's battery gauge, whereas "health is red" would only ever be true of Diablo II."""
    if value <= DEBUG_PANEL_LOW:
        return DEBUG_PANEL_BAD_COLOR
    if value <= DEBUG_PANEL_MID:
        return (255, 200, 0)
    return (60, 230, 90)


def _debug_panel():
    """Returns (title, rows) for Overlay.draw_panel(), or (None, None) when the panel is off.

    Builds one row per meter actually present in meters.json rather than hardcoding health and
    mana, so calibrating a third meter later makes it show up here with no code change."""
    if not debug_panel_visible:
        return None, None
    if not meters:
        return "GAME STATE", [("no meters", "run calibrate_meters.py", None, DEBUG_PANEL_BAD_COLOR)]
    if meter_anchor and anchor_rect() is None:
        #Distinct from "no read": the window itself is gone, so say that rather than showing
        #every meter as unreadable and leaving the cause to be guessed at.
        safe = meter_anchor.get("window_title", "?").encode("ascii", "replace").decode("ascii")
        return "GAME STATE  window not found", [(safe[:22], "not on screen", None, DEBUG_PANEL_BAD_COLOR)]
    if anchor_focused() is False:
        #Distinct again from both "window not found" and "no read": the window is right there, we
        #simply are not looking at it, so the readings are suspended and actions are held. Saying
        #this ON THE PANEL is the whole point - the person who needs to know why nothing is
        #happening is looking at the screen, not at a console that has already scrolled past it.
        return "GAME STATE  NOT FOCUSED", [
            ("readings", "suspended", None, DEBUG_PANEL_BAD_COLOR),
            ("actions", "paused", None, DEBUG_PANEL_BAD_COLOR)]

    age = current_game_state_age()
    if age is None:
        title = "GAME STATE  (waiting)"
    elif age >= DEBUG_PANEL_STALE_SECONDS:
        #Frozen numbers look exactly like correct ones, so say so rather than presenting a stale
        #value as if it were live.
        title = f"GAME STATE  STALE {age:.0f}s"
    else:
        title = "GAME STATE"

    unknown = unprofiled_size()
    if unknown:
        #No profile for this window shape. Say so instead of showing a confident number measured
        #from the wrong pixels - the failure this whole calibration flow exists to make visible.
        have = ", ".join(f"{w}x{h}" for (w, h) in known_profile_sizes()) or "none"
        return "GAME STATE  NOT CALIBRATED", [
            (f"{unknown[0]}x{unknown[1]}", "no profile", None, DEBUG_PANEL_BAD_COLOR),
            ("have", have[:20], None, DEBUG_PANEL_BAD_COLOR)]

    readings = current_game_state()
    rows = []
    for meter in meters:
        value = readings.get(meter.name)
        if value is None:
            #None is NOT 0% - a failed read must never be shown as an empty orb (see game_state.py).
            rows.append((meter.name, "no read", None, DEBUG_PANEL_BAD_COLOR))
            continue
        #Rounded to whole percent before it reaches the overlay: the panel only redraws when its
        #content actually changes, and an unrounded float would differ on nearly every frame,
        #forcing a constant recomposite of an always-on-top layered window for sub-pixel bar
        #movement. Same reasoning as draw_rectangles' skip-if-unchanged check.
        value = round(value, 2)
        rows.append((meter.name, f"{value * 100:3.0f}%", value, _value_color(value)))
    return title, rows


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
    #Kept entirely separate from the detection path: 'F5' only changes what gets DRAWN, so
    #toggling the panel can never disturb item detection, the boxes, or auto-collect.
    keyboard.add_hotkey("f5", _toggle_debug_panel)
    print("Overlay running - press 'End' anytime to quit, 'F5' to toggle the game-state panel.")

    t0 = time.time()
    n_frames = 1
    window_start = t0 #resets every FPS_PRINT_INTERVAL_SECONDS - the printed FPS is an average over just that window, not the whole session
    window_frames = 0
    while not quit_requested.is_set() and overlay.is_open():
        current_size = pyautogui.size()
        if current_size != displayed_size:
            overlay.resize(current_size.width, current_size.height)
            displayed_size = current_size

        #Drawn before the queue read, so the panel keeps updating (and keeps reporting staleness)
        #even through a stretch where no detection frames are arriving at all.
        #Anchored to the watched window when there is one, so the panel travels with the game
        #instead of sitting on bare desktop beside it when the game is windowed.
        rect = anchor_rect()
        panel_origin = None
        if rect is not None:
            panel_origin = (rect[0] + PANEL_INSET_X, rect[1] + int(rect[3] * PANEL_INSET_Y_FRACTION))
        overlay.draw_panel(*_debug_panel(), origin=panel_origin)

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
    t5 = threading.Thread(target=run_potion_drinking, daemon=True)
    t1.start()
    t2.start()
    t3.start()
    t4.start()
    t5.start()

    try:
        run_overlay()
    except NotImplementedError as e:
        print(f"WARNING: {e}\nFalling back to the plain debug window.")
        run_debug_window()

if __name__ == "__main__":
    main()
