"""Why didn't F3 click 'Save and Exit'? Run this, then press Esc in the game and watch.

    python tools/diagnose_quit.py

Prints one line per poll with everything quit_game() decides on, and PRESSES AND CLICKS NOTHING -
it only looks. Press Ctrl+C to stop.

It exists because "F3 did not click Save and Exit" has four causes that look identical from
outside, and they want completely different fixes:

  1. The guards said no. quit_game() passes `is_paused=lambda: not actions_allowed()` to
     click_when_seen, so if either guard goes false while the menu is open the click is SUSPENDED,
     not attempted - and a suspended attempt looks exactly like one that never saw the button.
     This is the one worth suspecting first, because opening the Esc menu changes the screen the
     in-play check is reading: it dims everything and draws a panel over the middle. Whether that
     is enough to drop the HUD art below its threshold is a measurement, not a guess - which is
     the whole point of this tool. Note that tests/fixtures/esc_menu.png CANNOT answer it: the
     in-play search regions are masked out of that fixture (13.7% of its pixels are non-black
     there), so scoring it measures black, not the menu.
  2. The button was never found - a menu that did not open, or art that has changed.
  3. The button was found but in the wrong place, so the click went somewhere useless.
  4. Everything above was fine, which means the click landed and something after it failed.

SAFE TO RUN WHILE main.py IS RUNNING, and that is the normal way to use it - it captures with
mss rather than taking a frame source of its own. A DXGI source is a per-output singleton, so a
second consumer would not get a second camera, it would get the pipeline's and start stealing
frames out from under it (see frame_source.py and Error_history.txt #39). Nothing here clicks,
types or presses a key; it only looks.

Read it as: with the menu OPEN, "allowed" must be yes and "save+exit" must be found. Any 'no' on
that line names the cause.
"""
import sys
from pathlib import Path
#Run directly (python tools/diagnose_quit.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

import cv2 as cv
import mss
import numpy as np

import main

POLL_SECONDS = 0.5


def yn(value):
    """True/False/None as three distinct words - None is 'cannot tell' everywhere in this project
    and must never be read as a no."""
    return {True: "yes", False: "NO ", None: "?  "}[value]


def main_():
    if main.save_and_exit_button is None:
        print("No assets/save_and_exit.json - quit_game() cannot run at all.")
        return 1
    if main.in_play_check is None:
        print("No assets/in_play.json - the in-play guard is unavailable (reads '?', not 'no').")

    print(__doc__.strip().splitlines()[0])
    print(f"\nPolling every {POLL_SECONDS}s. Switch to the game and press Esc. Ctrl+C to stop.\n")
    print(f"  {'focused':>8} {'in play':>8} {'allowed':>8}   {'save+exit':<10} {'where':<14} "
          f"score / window")

    with mss.mss() as sct:
        while True:
            raw = sct.grab(main.monitor_area)
            frame = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)

            #REFRESH THE WINDOW ANCHOR EVERY POLL, exactly as the detection thread does. Without
            #this the anchor stays None in a process that is not running the pipeline, and then
            #every line below is measured a different way from the live code: focus reads "cannot
            #tell" instead of yes/no, and the button is searched against the whole screen rather
            #than against the game's client area. A diagnostic that does not reproduce the path it
            #is diagnosing is worse than none - it produces confident numbers about a code path
            #nobody runs. (This cost a wrong conclusion once already while writing it.)
            main._refresh_anchor(time.time())

            #The in-play check runs off the pipeline's downscaled frame, so score it the same way
            #here - reading it at full resolution would report a number the pipeline never sees.
            small = cv.resize(frame, (0, 0), fx=main.CAPTURE_SCALE, fy=main.CAPTURE_SCALE)
            gray = cv.cvtColor(small, cv.COLOR_BGRA2GRAY)
            in_play = main._update_in_play(gray)
            focused = main.anchor_focused()
            allowed = main.actions_allowed()

            #And the button off a FULL-RESOLUTION frame, because that is what quit_game does and
            #why - at 0.3x the right button and a wrong one are 0.12 apart instead of 0.36.
            position = main._find_save_and_exit(frame)
            found = "found" if position else "NOT FOUND"
            where = f"({position[0]}, {position[1]})" if position else "-"

            print(f"  {yn(focused):>8} {yn(in_play):>8} {yn(allowed):>8}   {found:<10} "
                  f"{where:<14} in-play {main.in_play_score():.3f}  window {main.anchor_rect()}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main_())
    except KeyboardInterrupt:
        print("\nStopped.")
