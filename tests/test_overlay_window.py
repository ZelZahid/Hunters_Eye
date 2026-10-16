"""The overlay's two promises, measured on real windows: you can see through it, and clicks go
through it. Windows only (the overlay is). No game required.

WHY THIS EXISTS: CLAUDE.md says to re-verify see-through AND click-through after any change to
core/overlay.py, by hand, with a click-counting button - because they are different pieces of
Win32 state and each has broken on its own before:

  - SEE-THROUGH is Tk's colour key, which the click-through style change silently resets. Without
    the SetLayeredWindowAttributes() call that re-asserts it, the overlay covers the screen in
    solid black - the game disappears behind it.
  - CLICK-THROUGH is WS_EX_TRANSPARENT, set on the OUTER frame (GetParent of Tk's own handle).
    Set on the inner window it does nothing at all, and every click on a drawn box lands on the
    overlay instead of the game.

This file is that by-hand check, done by a machine: a plain green window stands in for the game,
the real Overlay is put over it with a box and the debug panel drawn, and then

  T-105  the screen is captured and every place the overlay draws nothing must show the green
         window - while the box itself must show (an overlay that is simply not on screen would
         pass a see-through test perfectly);
  T-106  Windows is asked which window a click at a drawn pixel goes to, and then a real click is
         sent there and the green window must count it.

The click only ever lands on one of this test's own windows: both are topmost, and the click is
sent only after Windows confirms which window is under the point. The cursor is put back after.

On anything but Windows it skips; under CI (the CI environment variable) a skip is a FAILURE,
because CI runs Windows precisely so this runs.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import platform
import time

UNDER_CI = bool(os.environ.get("CI"))
failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def cannot_run(why):
    if UNDER_CI:
        check(f"CI runs on Windows so this runs - {why}", False)
    else:
        print(f"SKIPPED: {why}")
    sys.exit(1 if failures else 0)


if platform.system() != "Windows":
    cannot_run("the overlay is Windows-only")

#Real pixels, before any window exists: a DPI-unaware process gets its windows stretched by the
#desktop, and then a window's own idea of where it is and the captured pixels disagree.
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:                            # noqa: BLE001 - older Windows
    ctypes.windll.user32.SetProcessDPIAware()

try:
    import mss
    import numpy as np
    import win32api
    import win32con
    import win32gui
    import tkinter as tk
    from core import overlay as ov
except Exception as e:                       # noqa: BLE001
    cannot_run(f"cannot import what this needs ({type(e).__name__}: {e})")

GREEN = (0, 200, 0)            #the stand-in game, as (r, g, b)
BOX_COLOR = (255, 0, 255)      #a detection box - magenta, nothing like the green under it
PLATE = ov.PANEL_BG_COLOR
TOLERANCE = 12                 #per channel; a capture of a flat colour is exact on most machines
OVERLAY_SIZE = (620, 520)
#Everything below in SCREEN pixels. The overlay sits at the screen's top-left corner, so its canvas
#coordinates are screen coordinates too - exactly how main.py hands it boxes.
TARGET = (60, 60, 440, 340)    #x, y, w, h of the stand-in game window, inside the overlay
BOX = (300, 90, 150, 100)      #x, y, w, h of the detection box, over the stand-in
PANEL_ORIGIN = (110, 250)      #the debug panel, over the stand-in and clear of the box


def outer(widget):
    """The OS-level frame of a Tk window - the one that is hit-tested. overlay.py walks to it the
    same way; winfo_id() alone is an inner drawing window."""
    raw = widget.winfo_id()
    return win32gui.GetParent(raw) or raw


def top_level(hwnd):
    return win32gui.GetAncestor(hwnd, win32con.GA_ROOT) if hwnd else 0


#--- the stand-in game, created FIRST so the overlay's topmost lands above it -------------------
clicks = []
target = tk.Tk()
target.overrideredirect(True)
target.attributes("-topmost", True)
target.geometry(f"{TARGET[2]}x{TARGET[3]}+{TARGET[0]}+{TARGET[1]}")
board = tk.Canvas(target, width=TARGET[2], height=TARGET[3], bg="#%02x%02x%02x" % GREEN,
                  highlightthickness=0)
board.pack()
board.bind("<Button-1>", lambda e: clicks.append((e.x_root, e.y_root)))
target.update()

try:
    overlay = ov.Overlay(*OVERLAY_SIZE)
except Exception as e:                       # noqa: BLE001
    target.destroy()
    cannot_run(f"the overlay could not be created ({type(e).__name__}: {e})")
overlay.root.lift()
overlay.draw_rectangles([(*BOX, BOX_COLOR)])
overlay.draw_panel("TEST PANEL", [("health", "50%", 0.5, (200, 40, 40))], origin=PANEL_ORIGIN)


def pump(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        target.update()
        overlay.pump()
        time.sleep(0.02)


pump(0.6)   #let the compositor put both windows on screen

target_hwnd = outer(target)
overlay_hwnd = outer(overlay.root)
with mss.mss() as sct:
    shot = sct.grab({"left": 0, "top": 0, "width": OVERLAY_SIZE[0], "height": OVERLAY_SIZE[1]})
    screen = np.frombuffer(shot.raw, np.uint8).reshape(shot.height, shot.width, 4)[..., [2, 1, 0]]


def is_colour(rgb, want):
    return all(abs(int(a) - int(b)) <= TOLERANCE for a, b in zip(rgb, want))


def fraction(patch, want):
    diff = np.abs(patch.astype(int) - np.array(want)).max(axis=-1)
    return float((diff <= TOLERANCE).mean())


print("\nT-105  The overlay is see-through: what is under it shows wherever it draws nothing")
ex_style = win32gui.GetWindowLong(overlay_hwnd, win32con.GWL_EXSTYLE)
check("the overlay's outer frame is layered (WS_EX_LAYERED)", bool(ex_style & win32con.WS_EX_LAYERED))
tx, ty, tw, th = TARGET
open_ground = [(tx + 20, ty + 20), (tx + tw - 20, ty + 20), (tx + 20, ty + th - 20),
               (BOX[0] + BOX[2] // 2, BOX[1] + BOX[3] // 2)]   #the last is INSIDE the box
for x, y in open_ground:
    check(f"({x}, {y}): the window underneath shows through - {tuple(int(c) for c in screen[y, x])}",
          is_colour(screen[y, x], GREEN))
#The box's left edge, 2px wide: some pixel across it must be the box colour. Without this the
#check above passes for an overlay that never appeared at all.
bx, by = BOX[0], BOX[1] + BOX[3] // 2
edge = screen[by, bx - 3:bx + 4]
check(f"the drawn box is visible on top of it ({fraction(edge, BOX_COLOR):.0%} of the pixels across "
      f"its edge are the box colour)", fraction(edge, BOX_COLOR) > 0)
#The panel plate is stippled: half its pixels are the plate, half show the game through. A strip of
#the plate's bottom padding has no text on it, so it is only ever those two colours.
px, py = PANEL_ORIGIN
plate_bottom = py + ov.PANEL_TITLE_GAP + ov.PANEL_ROW_HEIGHT
strip = screen[plate_bottom + 2:plate_bottom + ov.PANEL_PAD - 2, px:px + 200]
through, plate = fraction(strip, GREEN), fraction(strip, PLATE)
check(f"the panel plate is half see-through: {through:.0%} game, {plate:.0%} plate "
      f"(each between 30% and 70%)", 0.3 <= through <= 0.7 and 0.3 <= plate <= 0.7)


print("\nT-106  The overlay is click-through: a click on a drawn box lands on the window underneath")
check("the overlay's outer frame is click-through (WS_EX_TRANSPARENT)",
      bool(ex_style & win32con.WS_EX_TRANSPARENT))
points = {"on the box's edge": (bx, by),
          "on the panel plate": (px + 150, plate_bottom + ov.PANEL_PAD // 2),
          "on open ground": (tx + 20, ty + 20)}
safe = True
for name, point in points.items():
    hit = top_level(win32gui.WindowFromPoint(point))
    who = ("the window underneath" if hit == target_hwnd else
           "THE OVERLAY" if hit == overlay_hwnd else f"some other window ({hit})")
    check(f"{name} {point}: Windows routes the click to {who}", hit == target_hwnd)
    #Never send a real click toward a window that is not one of ours.
    safe = safe and hit in (target_hwnd, overlay_hwnd)

if safe:
    saved = win32api.GetCursorPos()
    try:
        for name, (x, y) in points.items():
            before = len(clicks)
            win32api.SetCursorPos((x, y))
            pump(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            pump(0.3)
            check(f"a real click {name} reached the window underneath ({len(clicks) - before} counted)",
                  len(clicks) - before == 1)
    finally:
        win32api.SetCursorPos(saved)
else:
    check("no real click sent: something other than this test's own windows is under the points", False)

overlay.close()
target.destroy()
print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
