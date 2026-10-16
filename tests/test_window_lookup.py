"""window_region's live window lookup, against real windows this test opens. Windows only. No game
required.

WHY THIS EXISTS: every HUD reading is anchored to the game's window - "the health orb is 21%
across the WINDOW" - so finding the right window, and the right part of it, decides which pixels
the meters read. Each way of getting it wrong has happened:

  - the WINDOW rect instead of the CLIENT rect puts every region a title bar's height too low,
    comfortably enough to miss an orb;
  - picking the biggest matching window chose a browser tab titled "... Diablo II: Resurrected
    Cheat Sheet - Google Chrome" over the game itself, so an exact title has to win;
  - focus must be three-valued: None ("cannot tell") has to behave as if the guard did not exist,
    or a machine without window lookup would never read a meter or take an action again.

The geometry it feeds is carded (V-805); this is the half that talks to Windows. It opens two
small windows with made-up titles - one exactly the title searched for, one larger with that title
inside a longer one - and asks window_region about them.

On anything but Windows it skips; under CI (the CI environment variable) a skip is a FAILURE.
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
    cannot_run("window lookup is Windows-only (pywin32)")

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:                            # noqa: BLE001 - older Windows
    ctypes.windll.user32.SetProcessDPIAware()

try:
    import tkinter as tk
    import win32con
    import win32gui
    from core import window_region as wr
except Exception as e:                       # noqa: BLE001
    cannot_run(f"cannot import what this needs ({type(e).__name__}: {e})")

if not wr.available():
    cannot_run("window_region reports no window lookup (pywin32 missing)")

#Made-up titles: nothing else on any machine is called this.
TITLE = "Hunters Eye Lookup Test 7f3a"
LONGER = f"Notes on {TITLE} - Some Browser"


def outer(widget):
    raw = widget.winfo_id()
    return win32gui.GetParent(raw) or raw


def pump(*windows, seconds=0.3):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        for w in windows:
            w.update()
        time.sleep(0.02)


game = tk.Tk()
game.title(TITLE)
game.geometry("320x200+120+120")
browser = tk.Toplevel(game)
browser.title(LONGER)
browser.geometry("520x380+200+160")    #bigger, so a size tiebreak alone would pick it
pump(game)
game_hwnd, browser_hwnd = outer(game), outer(browser)


print("\nV-806  Finding a real window: its CLIENT area, and an exact title beats a bigger window")
found = wr.find_window(TITLE)
check(f"the window is found by its title ({'found' if found else 'None'})", found is not None)
if found:
    handle, rect = found
    check("the exact title wins over a bigger window whose title only contains it",
          handle == game_hwnd)
    #Tk's root coordinates are its client area: inside the title bar and the borders.
    client = (game.winfo_rootx(), game.winfo_rooty(), game.winfo_width(), game.winfo_height())
    check(f"the rect is the CLIENT area {client}, not the window rect: got {rect}",
          all(abs(a - b) <= 1 for a, b in zip(rect, client)))
    wx, wy, wx1, wy1 = win32gui.GetWindowRect(game_hwnd)
    check(f"...which starts below the title bar (client top {rect[1]} > window top {wy})", rect[1] > wy)
check("matching ignores case", (wr.find_window(TITLE.upper()) or (None,))[0] == game_hwnd)
partial = wr.find_window("lookup test 7f3a")
check("with no exact match the larger window wins - documented, not ideal; set the exact title",
      partial is not None and partial[0] == browser_hwnd)
check("an empty title finds nothing", wr.find_window("") is None)
check("find_client_rect is the same answer without the handle",
      found is not None and wr.find_client_rect(TITLE) == found[1])

game.withdraw()
pump(game)
hidden = wr.find_window(TITLE)
check("a hidden window is not found - only the browser, by substring, is left",
      hidden is not None and hidden[0] == browser_hwnd)
browser.withdraw()
pump(game)
check("with neither on screen: None, so meters read 'cannot tell' instead of desktop pixels",
      wr.find_window(TITLE) is None)
game.deiconify()
browser.deiconify()
pump(game, seconds=0.5)


print("\nV-807  Focus is three-valued: True or False for a real window, None when it cannot tell")
check("no handle -> None (cannot tell), never False", wr.is_foreground(None) is None)
#Whichever window Windows says is in front - one of ours or not - is the real "True" case.
#Forcing ours to the front is NOT attempted: the usual trick (a synthetic Alt press so Windows
#allows SetForegroundWindow) also opens the system-menu loop on a Tk window, and that loop holds
#the test inside update() forever - it hung the first CI run of this file.
game.focus_force()
pump(game, seconds=0.4)
in_front = win32gui.GetForegroundWindow()
answer_game, answer_browser = wr.is_foreground(game_hwnd), wr.is_foreground(browser_hwnd)
check(f"real windows get a real answer: {answer_game!r} / {answer_browser!r}",
      isinstance(answer_game, bool) and isinstance(answer_browser, bool))
check("the answers agree with what Windows says is in front",
      answer_game == (in_front == game_hwnd) and answer_browser == (in_front == browser_hwnd))
if in_front:
    check("the window Windows says is in front reads True", wr.is_foreground(in_front) is True)
    ours_behind = browser_hwnd if in_front == game_hwnd else game_hwnd
    check("a window behind it reads False, not None", wr.is_foreground(ours_behind) is False)
else:
    #No foreground window at all happens on a locked or disconnected desktop.
    check("Windows reports some window in front (none: a locked desktop?)", not UNDER_CI)

browser.destroy()
game.destroy()
check("a window that no longer exists is not in front (False, and no exception)",
      wr.is_foreground(game_hwnd) is False)

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
