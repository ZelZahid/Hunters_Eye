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

import argparse
import time
from pathlib import Path

import cv2 as cv
import mss
import numpy as np

import game_state

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
        "hsv_ranges": (((0, 70, 40), (10, 255, 255)), ((168, 70, 40), (179, 255, 255))),
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
    """One full-screen BGR capture, optionally downscaled."""
    monitor = sct.monitors[0]  # the whole virtual screen, matching main.py's monitor_area
    frame = np.array(sct.grab(monitor))
    if frame.shape[2] == 4:
        frame = cv.cvtColor(frame, cv.COLOR_BGRA2BGR)
    if scale != 1.0:
        frame = cv.resize(frame, (0, 0), fx=scale, fy=scale)
    return frame


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


def calibrate(frame):
    """Walks the user through selecting each meter's region. Returns a list of Meter objects."""
    frame_h, frame_w = frame.shape[:2]
    display_scale = min(1.0, MAX_SELECT_WIDTH / frame_w, MAX_SELECT_HEIGHT / frame_h)
    display = cv.resize(frame, (0, 0), fx=display_scale, fy=display_scale) if display_scale < 1.0 else frame.copy()

    meters = []
    for name, preset in PRESETS.items():
        print(f"\n--- {name} ---")
        print(f"Drag a box around {preset['prompt']}, then press ENTER. Press 'c' to skip it.")
        print("Tip: box the orb itself, not the decorative frame around it. A little slack is")
        print("     fine - the ellipse mask ignores the corners anyway.")

        window = f"Select {name} - ENTER to confirm, c to skip"
        x, y, w, h = cv.selectROI(window, display, showCrosshair=False, fromCenter=False)
        cv.destroyWindow(window)

        if w < 4 or h < 4:
            print(f"    skipped {name}")
            continue

        # Back to full-resolution pixels, then to fractions of the screen.
        x, y, w, h = (int(v / display_scale) for v in (x, y, w, h))
        region = (x / frame_w, y / frame_h, w / frame_w, h / frame_h)
        print(f"    region: x={x} y={y} w={w} h={h}  ({region[0]:.4f}, {region[1]:.4f}, "
              f"{region[2]:.4f}, {region[3]:.4f} as screen fractions)")

        describe_selection(frame[y:y + h, x:x + w], preset["hsv_ranges"], preset["shape"])

        meters.append(game_state.Meter(
            name=name,
            region=region,
            hsv_ranges=preset["hsv_ranges"],
            shape=preset["shape"],
            fill_from=preset["fill_from"],
        ))

    return meters


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

    window = "Hunter's Eye - meter calibration preview"
    cv.namedWindow(window, cv.WINDOW_NORMAL)

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

    if args.preview:
        preview(game_state.load_meters(CONFIG_PATH), args.fast_scale)
        return

    print(f"Switch to the game now - taking a screenshot in {args.delay:.0f} seconds.")
    print("Make sure both orbs are visible and at least partly full.")
    for remaining in range(int(args.delay), 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1)

    with mss.mss() as sct:
        frame = grab_screen(sct)
    print(f"Captured {frame.shape[1]}x{frame.shape[0]}.")

    meters = calibrate(frame)
    if not meters:
        print("\nNothing selected - no config written.")
        return

    ASSETS_DIR.mkdir(exist_ok=True)
    game_state.save_meters(CONFIG_PATH, meters)
    print(f"\nWrote {CONFIG_PATH}")

    preview(meters, args.fast_scale)


if __name__ == "__main__":
    main()
