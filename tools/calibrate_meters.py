"""
Interactive calibration + live preview for game_state.py's HUD meters.

WHY THIS EXISTS AS A SEPARATE TOOL: game_state.py can measure how full a meter is, but it
cannot know WHERE that meter is on your screen or WHAT COLOR it is - that depends on the game,
the resolution, and the UI scale. Rather than hardcoding Diablo II orb coordinates into the
engine (which CLAUDE.md's design priorities specifically rule out), you point this tool at the
relevant part of your own screen once and it writes assets/meters.json.

Just as important, it then shows you a LIVE PREVIEW of what the engine is actually measuring:
the exact pixels being counted, and the resulting percentage, updating in real time. A meter
reading is going to drive real decisions (drink a potion, retreat), and a badly-tuned color
range fails by quietly returning a plausible-but-wrong number rather than by raising an error -
so it has to be validated by eye against the real game before anything acts on it.

USAGE
    python calibrate_meters.py              # calibrate health + mana, then preview
    python calibrate_meters.py --preview    # skip calibration, just preview the saved config
    python calibrate_meters.py --delay 8    # longer countdown before the screenshot is taken

During calibration: drag a box around each orb, then press ENTER (or SPACE). Press 'c' to skip
a meter you do not want to configure. During preview: press 'q' to quit.
"""

from __future__ import annotations

import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import time

import cv2 as cv
import mss
import numpy as np

from core import game_state
import main as pipeline #for CAPTURE_REGION only - see grab_screen() for why this must be shared
from core import window_region

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
CONFIG_PATH = ASSETS_DIR / "meters.json"

# Presets for the two meters this tool asks about by default. These are starting points, not
# gospel - the calibration step measures the actual pixels you selected and warns you if they
# do not fall inside the preset range, which is the signal to widen it in the JSON.
#
# OpenCV HSV: H is 0-179 (NOT 0-359), S and V are 0-255. Red straddles H=0, so it needs two
# ranges; every other color needs one. The S and V floors are what separate "liquid" from the
# dark, desaturated empty portion of the orb - they matter more than the hue bounds do.
PRESETS = {
    "health": {
        # Red, red-wrapped... AND GREEN. Diablo II turns the health globe green while poisoned,
        # which is not a cosmetic detail: with only the red ranges the orb reads 0% the moment
        # poison lands, so a decision layer sees "you are dead" at 90% health - during the one
        # situation where the number matters most. This is why hsv_ranges is a LIST: a meter that
        # changes colour to signal a status needs one range per colour it can legitimately be.
        # Generic, not a Diablo detail - any game that recolours a bar (poisoned, cursed,
        # shielded, overhealed) has this, as does a battery gauge that turns red when low.
        "hsv_ranges": (((0, 70, 40), (10, 255, 255)), ((168, 70, 40), (179, 255, 255)),
                       ((35, 70, 40), (85, 255, 255))),
        "shape": "ellipse",
        "fill_from": "bottom",
        "prompt": "the HEALTH orb (red)",
    },
    "mana": {
        "hsv_ranges": (((95, 70, 40), (130, 255, 255)),),
        "shape": "ellipse",
        "fill_from": "bottom",
        "prompt": "the MANA orb (blue)",
    },
}

# The selection window has to fit on the same screen it is showing a picture of, so the
# screenshot is shrunk to fit before being displayed and the selection is scaled back up
# afterwards. This is purely a display convenience and does not affect the saved coordinates.
MAX_SELECT_WIDTH = 1500
MAX_SELECT_HEIGHT = 850

PREVIEW_PANEL_HEIGHT = 220  # how tall each meter's preview panel is drawn, in pixels


def grab_screen(sct, scale=1.0):
    """One BGR capture of exactly the region main.py detects on, optionally downscaled.

    IMPORTED from main.py rather than defined here, and that is a correctness requirement, not
    tidiness. A meter's region is stored as FRACTIONS of the captured frame, so the calibrator and
    the pipeline must agree on what frame those fractions are OF. They previously did not: this
    grabbed sct.monitors[0] - the whole VIRTUAL desktop, spanning every monitor - while main.py
    grabs only the primary one. On a three-monitor setup (left=-1920, width=5760) that meant an
    orb at x=500 on the primary screen was recorded at fraction 2420/5760 = 0.420, which main.py
    then applied to a 1920-wide frame and read at x=806: a completely different part of the
    screen. Nothing would have raised an error - game_state just returns a plausible-but-wrong
    percentage, or None, which is precisely the failure this whole calibration flow exists to
    catch. The old comment here even claimed it matched main.py, which it had stopped doing.
    See Error_history.txt #6 for the earlier instance of two capture constants drifting apart.
    """
    frame = np.array(sct.grab(pipeline.monitor_area))
    if frame.shape[2] == 4:
        frame = cv.cvtColor(frame, cv.COLOR_BGRA2BGR)
    if scale != 1.0:
        frame = cv.resize(frame, (0, 0), fx=scale, fy=scale)
    return frame


#A fitted region must be at least this fraction of what the user drew, or the fit is rejected as
#not having found the meter at all (e.g. the orb was empty, or the box missed it).
MIN_FIT_AREA_RATIO = 0.10
#A meter that fills from the bottom and is not full fits SHORT, because the missing liquid simply
#isn't there to find. For a round orb a healthy fit is roughly as tall as it is wide; well under
#that means it probably wasn't full when the screenshot was taken.
MIN_FIT_ASPECT = 0.75


def fit_to_content(roi, hsv_ranges):
    """Shrinks a hand-drawn box to the meter actually inside it. Returns (x, y, w, h) or None.

    Hand-drawn boxes include the ornamental frame around a meter, and that is not a cosmetic
    problem - it is the difference between a working reading and pure noise. Measured on a real
    Diablo II health orb: the drawn box was 197x192 while the orb was 139x156, so 43% of the box
    was scenery and 32% of the measured ellipse landed on it. Because that scenery is ANIMATED
    (torchlight, the statues flanking the orb, whatever the character walks past), the reading
    wandered with the artwork rather than the health: the same full orb measured 87% one moment
    and 0% the next, which reads exactly like "the slightest damage drops me to zero".

    Fitting to the meter's own colour removes the guesswork. The blob's sides and bottom are
    reliable regardless of fill level; only its top depends on the meter being full, which is
    why the caller is told to fill up first and why a suspiciously short fit is reported."""
    #game_state's own mask, not a copy of it - the fit must agree exactly with what the engine
    #will later measure, and a second implementation is free to drift away from it (see #27).
    color = game_state._color_mask(roi, hsv_ranges)
    count, _labels, stats, _centroids = cv.connectedComponentsWithStats(color.astype(np.uint8), 8)
    if count <= 1:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv.CC_STAT_AREA]))
    x, y, w, h, area = stats[largest]
    if area < roi.shape[0] * roi.shape[1] * MIN_FIT_AREA_RATIO:
        return None
    return int(x), int(y), int(w), int(h)


def describe_selection(roi, hsv_ranges, shape):
    """Reports how well the preset color range actually matches the pixels the user selected.

    This is the single most useful diagnostic in the tool. An orb is almost never empty when
    you take a calibration screenshot, so the bottom slice of the selection is essentially
    guaranteed to be liquid - if the preset range does not match most of it, the range is wrong
    for this game/monitor and the resulting percentage will be garbage.
    """
    height, width = roi.shape[:2]
    band = roi[int(height * 0.75):, :]
    if band.size == 0:
        return

    shape_mask = game_state._shape_mask(shape, height, width)[int(height * 0.75):, :]
    matched = game_state._color_mask(band, hsv_ranges) & shape_mask
    coverage = matched.sum() / max(1, shape_mask.sum())

    hsv = cv.cvtColor(band, cv.COLOR_BGR2HSV)
    # Only look at pixels that are colored at all - the dark empty part of an orb has no
    # meaningful hue, and averaging it in would drag the measurement toward nonsense.
    vivid = shape_mask & (hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 40)
    print(f"    preset color range matches {coverage * 100:.0f}% of the bottom of your selection")

    if vivid.sum() >= 20:
        h, s, v = (hsv[:, :, i][vivid] for i in range(3))
        print(f"    measured hue {np.percentile(h, 5):.0f}-{np.percentile(h, 95):.0f}, "
              f"sat {np.percentile(s, 5):.0f}-{np.percentile(s, 95):.0f}, "
              f"val {np.percentile(v, 5):.0f}-{np.percentile(v, 95):.0f}")

    if coverage < 0.5:
        print("    WARNING: less than half the selection matched. Either the box includes a lot")
        print("             of surrounding UI art, or this meter is not the color the preset")
        print("             expects. Compare the measured values above against 'hsv_ranges' in")
        print(f"             {CONFIG_PATH.name} and widen them if needed.")


def _show_window(name, image=None):
    """Creates a window and forces it in front of whatever is already fullscreen.

    This tool's whole flow is "switch to the game, wait for a countdown, then interact with a
    window" - so by the time the window opens, the GAME is the foreground application and a
    normally-created OpenCV window opens BEHIND it and is never seen. Marking it topmost is what
    makes it appear at all. (If the game is in exclusive-fullscreen mode, nothing can draw over
    it and it has to be switched to Windowed or Borderless first - see the note printed below.)"""
    cv.namedWindow(name, cv.WINDOW_NORMAL)
    try:
        cv.setWindowProperty(name, cv.WND_PROP_TOPMOST, 1)
    except cv.error:
        pass #not fatal - the window still exists, it may just open behind the game
    if image is not None:
        cv.imshow(name, image)
        cv.waitKey(1) #let the window actually paint before selectROI blocks on it
    return name


def calibrate(frame, windows=()):
    """Walks the user through selecting each meter's region.

    Returns (meters, anchor). `windows` is a snapshot of visible windows taken BEFORE any of this
    tool's own UI existed; whichever one a selection lands inside becomes the anchor, so regions
    are stored relative to the game window instead of the screen and survive the game being
    windowed, moved or resized. The user never has to name a window - they already pointed at it.
    """
    frame_h, frame_w = frame.shape[:2]
    display_scale = min(1.0, MAX_SELECT_WIDTH / frame_w, MAX_SELECT_HEIGHT / frame_h)
    display = cv.resize(frame, (0, 0), fx=display_scale, fy=display_scale) if display_scale < 1.0 else frame.copy()

    meters = []
    anchor = None
    for name, preset in PRESETS.items():
        print(f"\n--- {name} ---")
        print(f"Drag a box around {preset['prompt']}, then press ENTER. Press 'c' to skip it.")
        print("Roughly is fine - the box is automatically shrunk to the meter's own colour, so")
        print("some slack around it costs nothing. Do not cut into the meter itself, though.")

        window = _show_window(f"Select {name} - ENTER to confirm, c to skip", display)
        x, y, w, h = cv.selectROI(window, display, showCrosshair=False, fromCenter=False)
        cv.destroyWindow(window)

        if w < 4 or h < 4:
            print(f"    skipped {name}")
            continue

        # Back to full-resolution pixels, then to fractions of the screen.
        x, y, w, h = (int(v / display_scale) for v in (x, y, w, h))

        # Shrink the hand-drawn box to the meter actually inside it - see fit_to_content().
        fit = fit_to_content(frame[y:y + h, x:x + w], preset["hsv_ranges"])
        if fit is None:
            print("    WARNING: couldn't find the meter's colour inside your box, so the box is")
            print("             being used as drawn. If the reading looks wrong, the box probably")
            print("             missed the orb, or the orb was empty when the screenshot was taken.")
        else:
            fx, fy, fw_, fh_ = fit
            shrink = 1 - (fw_ * fh_) / (w * h)
            x, y, w, h = x + fx, y + fy, fw_, fh_
            print(f"    tightened to the {name} itself: {w}x{h}px "
                  f"({shrink * 100:.0f}% of your box was surrounding artwork)")
            if h < w * MIN_FIT_ASPECT:
                print(f"    WARNING: the fit is much wider ({w}) than it is tall ({h}), which usually")
                print(f"             means {name} was not full when the screenshot was taken. Fill it")
                print(f"             up and re-run, or the top of the meter will be cut off.")

        # Which window did this land in? Regions anchored to the game window keep working when
        # it is windowed or moved; screen fractions do not (see window_region.py).
        screen_x = pipeline.monitor_area["left"] + x + w // 2
        screen_y = pipeline.monitor_area["top"] + y + h // 2
        hit = window_region.smallest_containing(windows, screen_x, screen_y)
        if hit is not None and anchor is None:
            #Record the size too. Regions are fractions of the window, which handles the window
            #being MOVED or RESIZED - but not the game re-laying out its HUD at a different
            #resolution or aspect ratio, which it does. Measured on D2R: 1920x1080 -> 1280x800
            #keeps the orbs' vertical position and height to within 0.001 of the same fraction,
            #but shifts them horizontally by 0.033 (42px against a 104px orb). Storing the size
            #lets main.py notice and warn instead of reading half an orb and half a frame.
            anchor = {"window_title": hit[0], "client_size": [hit[1][2], hit[1][3]]}
        if hit is not None and anchor is not None and hit[0] == anchor["window_title"]:
            ax, ay, aw, ah = hit[1]
            abs_x = pipeline.monitor_area["left"] + x
            abs_y = pipeline.monitor_area["top"] + y
            region = ((abs_x - ax) / aw, (abs_y - ay) / ah, w / aw, h / ah)
            print(f"    region: x={x} y={y} w={w} h={h}  -> ({region[0]:.4f}, {region[1]:.4f}, "
                  f"{region[2]:.4f}, {region[3]:.4f}) as fractions of the window")
        else:
            region = (x / frame_w, y / frame_h, w / frame_w, h / frame_h)
            print(f"    region: x={x} y={y} w={w} h={h}  ({region[0]:.4f}, {region[1]:.4f}, "
                  f"{region[2]:.4f}, {region[3]:.4f} as SCREEN fractions - no window anchor)")

        describe_selection(frame[y:y + h, x:x + w], preset["hsv_ranges"], preset["shape"])

        meters.append(game_state.Meter(
            name=name,
            region=region,
            hsv_ranges=preset["hsv_ranges"],
            shape=preset["shape"],
            fill_from=preset["fill_from"],
        ))

    return meters, anchor


def build_preview_panel(name, value, roi, mask, fast_value):
    """One meter's preview strip: the raw pixels, the pixels that were counted, and the numbers."""
    if roi is None:
        blank = np.zeros((PREVIEW_PANEL_HEIGHT, 420, 3), dtype=np.uint8)
        cv.putText(blank, f"{name}: unreadable region", (10, 40), cv.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 1)
        return blank

    # Resize to an EXACT height rather than by a scale factor: fx/fy scaling rounds the output
    # height independently for each image, so a scale factor can produce a 219px panel next to a
    # 220px one, and np.hstack raises on the mismatch.
    view_width = max(1, int(roi.shape[1] * PREVIEW_PANEL_HEIGHT / roi.shape[0]))
    view_size = (view_width, PREVIEW_PANEL_HEIGHT)
    roi_view = cv.resize(roi, view_size, interpolation=cv.INTER_NEAREST)
    mask_view = cv.resize(mask.astype(np.uint8) * 255, view_size, interpolation=cv.INTER_NEAREST)
    mask_view = cv.cvtColor(mask_view, cv.COLOR_GRAY2BGR)

    # Draw the measured surface line across the raw view, so a wrong reading is obvious at a
    # glance: the line should sit exactly on the top of the liquid.
    if value is not None:
        surface_y = int(roi_view.shape[0] * (1.0 - value))
        cv.line(roi_view, (0, surface_y), (roi_view.shape[1], surface_y), (0, 255, 255), 2)

    readout = np.zeros((PREVIEW_PANEL_HEIGHT, 300, 3), dtype=np.uint8)
    text = "--" if value is None else f"{value * 100:.0f}%"
    cv.putText(readout, name, (10, 30), cv.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv.putText(readout, text, (10, 90), cv.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 3)
    fast_text = "--" if fast_value is None else f"{fast_value * 100:.0f}%"
    cv.putText(readout, f"full-res: {text}", (10, 140), cv.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    cv.putText(readout, f"fast-path: {fast_text}", (10, 165), cv.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

    return np.hstack([roi_view, mask_view, readout])


def resolved_meters():
    """Meters with their regions expressed against the CAPTURED FRAME, following the anchor.

    Mirrors what main.py does at runtime, so the preview validates the same pixels the pipeline
    will actually measure rather than a screen-relative approximation of them."""
    profiles = game_state.load_profiles(CONFIG_PATH)
    if not profiles:
        return []
    anchor = profiles[0].anchor
    if not anchor:
        return list(profiles[0].meters)
    rect = window_region.find_client_rect(anchor.get("window_title", ""))
    if rect is None:
        safe = anchor.get("window_title", "").encode("ascii", "replace").decode("ascii")
        print(f"WARNING: anchor window '{safe}' not found - is the game running? Falling back to")
        print("         screen-relative regions, which will be in the wrong place if it is windowed.")
        return list(profiles[0].meters)
    capture = (pipeline.monitor_area["left"], pipeline.monitor_area["top"],
               pipeline.monitor_area["width"], pipeline.monitor_area["height"])
    chosen = game_state.select_profile(profiles, (rect[2], rect[3]))
    if chosen is None:
        have = ", ".join("x".join(map(str, p.client_size)) for p in profiles if p.client_size)
        print(f"WARNING: no profile for a {rect[2]}x{rect[3]} window (calibrated: {have or 'none'}).")
        print("         Run this tool WITHOUT --preview at this size to add one.")
        return []
    out = []
    for m in chosen.meters:
        region = window_region.to_frame_fractions(rect, capture, m.region)
        if region is None:
            print(f"WARNING: meter '{m.name}' falls outside the captured screen area.")
            continue
        out.append(game_state.Meter(m.name, region, m.hsv_ranges, m.shape, m.fill_from))
    return out


def preview(meters, fast_scale):
    """Live preview. Left = the raw region with the detected surface drawn on it, middle = the
    exact pixels being counted, right = the numbers.

    Two readings are shown per meter on purpose. 'full-res' is measured from a native-resolution
    capture; 'fast-path' is measured from the same frame downscaled to the resolution main.py's
    detection thread actually runs at. They should agree within a couple of percent. If they do
    not, the meter is too small to measure reliably at that downscale, and main.py should read
    it from a higher-resolution frame instead of the fast path's.
    """
    if not meters:
        print("No meters configured - nothing to preview.")
        return

    print(f"\nLive preview running ({len(meters)} meter(s)). Press 'q' in the preview window to quit.")
    print("Check that the yellow line sits on the top of the liquid, and that the middle panel")
    print("shows the liquid and nothing else. Drink a potion / take damage and watch it follow.")

    window = _show_window("Hunter's Eye - meter calibration preview")

    with mss.mss() as sct:
        while True:
            frame = grab_screen(sct)
            fast_frame = cv.resize(frame, (0, 0), fx=fast_scale, fy=fast_scale)

            panels = []
            for meter in meters:
                value, roi, mask = game_state.read_meter(frame, meter, return_debug=True)
                fast_value = game_state.read_meter(fast_frame, meter)
                panels.append(build_preview_panel(meter.name, value, roi, mask, fast_value))

            width = max(panel.shape[1] for panel in panels)
            padded = [
                np.hstack([p, np.zeros((p.shape[0], width - p.shape[1], 3), dtype=np.uint8)])
                if p.shape[1] < width else p
                for p in panels
            ]
            cv.imshow(window, np.vstack(padded))

            if cv.waitKey(30) & 0xFF == ord("q"):
                break

    cv.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description="Calibrate and preview Hunter's Eye HUD meters.")
    parser.add_argument("--preview", action="store_true",
                        help="skip calibration and just preview the saved config")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="seconds to wait before grabbing the calibration screenshot, so you "
                             "can switch to the game (default: 5)")
    parser.add_argument("--fast-scale", type=float, default=0.3,
                        help="the capture scale main.py's detection thread runs at, used only to "
                             "show whether these meters stay accurate at that resolution. If you "
                             "change CAPTURE_SCALE in main.py, pass the new value here "
                             "(default: 0.3)")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        #Importing main.py (for its capture region) also loads the meter config, which by
        #definition does not exist yet the first time this tool is run - so it prints a warning
        #telling the user to run this very tool. Say so, rather than leaving that looking like
        #a failure.
        print(f"({CONFIG_PATH.name} doesn't exist yet - the warning above is expected, "
              f"creating it is what this tool does.)\n")

    if args.preview:
        preview(resolved_meters(), args.fast_scale)
        return

    print(f"Switch to the game now - taking a screenshot in {args.delay:.0f} seconds.")
    print("IMPORTANT: have both orbs as close to FULL as you can. The box you drag is shrunk to")
    print("fit the meter's own colour, and liquid that isn't there can't be found - calibrating on")
    print("a half-empty orb permanently cuts off its top half.")
    print("AFTER the countdown a selection window opens ON TOP of the game. If you don't see it,")
    print("alt-tab to it - and if it still isn't there, the game is in exclusive-fullscreen mode,")
    print("which nothing can draw over: switch it to Windowed or Borderless and re-run.")
    for remaining in range(int(args.delay), 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)

    # Snapshot the windows BEFORE this tool draws any UI of its own, so the selection can be
    # attributed to the game rather than to our own always-on-top selection window.
    windows = window_region.list_client_rects()
    with mss.mss() as sct:
        frame = grab_screen(sct)
    print(f"Captured {frame.shape[1]}x{frame.shape[0]} - the same region main.py detects on "
          f"(not the full multi-monitor desktop).")

    meters, anchor = calibrate(frame, windows)
    if not meters:
        print("\nNothing selected - no config written.")
        return

    ASSETS_DIR.mkdir(exist_ok=True)
    #ADD to the file rather than replacing it: calibrating at a new window size must not
    #throw away the profile for the size calibrated last week. The same size REPLACES, so
    #re-running to fix a badly drawn box still does what you expect.
    existing = game_state.load_profiles(CONFIG_PATH)
    profiles = game_state.upsert_profile(existing, game_state.Profile(anchor, tuple(meters)))
    game_state.save_profiles(CONFIG_PATH, profiles)
    print(f"\nWrote {CONFIG_PATH} - {len(profiles)} profile(s) now stored:")
    for prof in profiles:
        size = prof.client_size
        label = "x".join(map(str, size)) if size else "screen-relative (no window anchor)"
        print(f"   - {label}{'   <- just calibrated' if prof.anchor is anchor else ''}")
    if anchor:
        safe = anchor["window_title"].encode("ascii", "replace").decode("ascii")
        w, h = anchor["client_size"]
        print(f"Anchored to the window '{safe}' at {w}x{h}.")
        print("These regions keep working if you MOVE or RESIZE that window. A different game")
        print("resolution or aspect ratio gets its own profile - just run this again there and")
        print("BOTH are kept. Hunter's Eye picks the matching one automatically, and shows")
        print("NOT CALIBRATED on the panel if it meets a size you have never calibrated.")
    else:
        print("NOTE: no window anchor was found, so regions are stored relative to the whole")
        print("screen. They will be wrong if the game is windowed and then moved.")

    preview(resolved_meters(), args.fast_scale)


if __name__ == "__main__":
    main()
