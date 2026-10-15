"""Bot test mode: teleports out and back over the same spot on its own, so a timing test can run
with nobody at the keyboard.

    python tools/teleport_bot.py                       # 2 minutes, the key from user_config.txt
    python tools/teleport_bot.py --seconds 300 --interval 3
    python tools/teleport_bot.py --dry-run             # everything except pressing the key
    python tools/teleport_bot.py --measure             # also run tools/measure_ocr_latency.py
    python tools/teleport_bot.py --measure -- --save run.csv   # anything after -- goes to it

HOW TO USE IT: stand next to some items on the ground, out of town and away from monsters, with
Teleport on the key user_config.txt names as teleport_key (or pass --key). Start this, switch to the
game during the countdown, and leave it. It points the mouse a short way to one side, presses the
key, waits, points back at where it started, presses again - for --seconds or --count teleports.

IT PRESSES A KEY INTO THE GAME, so it is guarded the way main.py's own actions are:
  - F4 or End stops it at once (as does Ctrl+C in this console).
  - Nothing is pressed while the game is not focused or its HUD is not on screen
    (main.actions_allowed() - the same two guards that stop potions being typed into a lobby).
  - It stops when health reads at or below --stop-below, and waits while mana is at or below
    --mana-floor. A health or mana reading of "cannot tell" never stops or holds it.
  - Every teleport is checked by watching the scene move (routes/shuttle.py). Four in a row that
    visibly did nothing stops the run: the key is not Teleport, or there is no mana, or this is town.
  - The whole run is bounded by --seconds, pauses included.

RUN main.py OR THIS, NOT BOTH. main.py's auto-collect would walk or teleport to the very items this
is hovering over, and the two would fight over the mouse. tools/measure_ocr_latency.py replicates
main.py's OCR loop without acting on anything, which is exactly why it exists - pair this with that.

--measure starts tools/measure_ocr_latency.py alongside (it only looks, never presses), sized to
this run, and prints its report when both finish. That tool lives on the ocr-latency branch until it
merges; without it, --measure says so and nothing starts. --log writes one CSV row per teleport with
both a wall-clock and a perf_counter timestamp; on Windows perf_counter is the same system-wide
clock in every process, so the rows line up with measure_ocr_latency.py's own --save timeline.

Captures with its own mss, never a DXGI frame source (a second DXGI consumer steals the first one's
frames - EH#39).
"""
import sys
from pathlib import Path
#Run directly (python tools/teleport_bot.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import subprocess
import threading
import time

import cv2 as cv
import keyboard
import mss
import numpy as np

import main as pipeline
from core import actions
from core import game_state
from routes import shuttle

ROOT = Path(__file__).resolve().parent.parent
MEASURE_TOOL = ROOT / "tools" / "measure_ocr_latency.py"
#Let OCR find the labels before the first jump, or the first jump measures nothing.
FIRST_TELEPORT_DELAY_SECONDS = 2.0
#Pointing inside the game: this far in from the left and right edges of the client area, and
#vertically inside the same viewport band shuttle.py compares (off the HUD - a click on the belt or
#the orbs is not a teleport).
AIM_SIDE_MARGIN = 0.05


class Sensors:
    """One screenshot per look(), and everything else read off it.

    The pipeline's own helpers do the reading - _refresh_anchor, _update_in_play, actions_allowed -
    the way tools/diagnose_quit.py uses them, so the guards here are the guards main.py runs and not
    a second copy that could drift. Meters are read off the same CAPTURE_SCALE frame main.py reads
    them from, for the same reason: a reading at another scale is a number the pipeline never sees.
    """

    def __init__(self, sct):
        self.sct = sct
        #main.py smooths over 5 frames at ~50 a second and carries a failed read for 15. This polls
        #at ~10-20 a second, so the same ~0.1s and ~1/3s become 3 and 5.
        self.smoother = game_state.Smoother(window=3, max_carried_failures=5)
        self.readings = {}

    def look(self):
        raw = self.sct.grab(pipeline.monitor_area)
        frame = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        s = pipeline.CAPTURE_SCALE
        small = cv.resize(frame, (0, 0), fx=s, fy=s, interpolation=cv.INTER_AREA)
        gray = cv.cvtColor(small, cv.COLOR_BGRA2GRAY)

        meters = pipeline._refresh_anchor(time.time())
        #Same rule as detect_objects(): not in play or not focused -> the pixels where the orbs
        #belong are not the orbs, so every meter reads None rather than whatever is there.
        if pipeline._update_in_play(gray) is False or pipeline.anchor_focused() is False:
            meters = []
        if pipeline.meters:
            bgr = cv.cvtColor(small, cv.COLOR_BGRA2BGR)
            current = (game_state.read_all(bgr, meters) if meters
                       else {m.name: None for m in pipeline.meters})
            self.readings = self.smoother.update(current)

        rect = pipeline.anchor_rect() or pipeline.CAPTURE_RECT
        x0 = int(round((rect[0] - pipeline.CAPTURE_RECT[0]) * s))
        y0 = int(round((rect[1] - pipeline.CAPTURE_RECT[1]) * s))
        x1, y1 = x0 + int(rect[2] * s), y0 + int(rect[3] * s)
        if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
            return None #the window hangs off the captured monitor - a partial view would mislead
        return gray[y0:y1, x0:x1]

    def health(self):
        return self.readings.get("health")

    def mana(self):
        return self.readings.get("mana")


def geometry():
    """Where the character is and where it is safe to point, in real screen pixels."""
    rect = pipeline.anchor_rect() or pipeline.CAPTURE_RECT
    left, top, width, height = rect
    cx, cy, _ = pipeline._character_screen_position()
    bounds = (left + AIM_SIDE_MARGIN * width, top + shuttle.VIEW_TOP * height,
              left + (1 - AIM_SIDE_MARGIN) * width, top + (1 - shuttle.VIEW_BOTTOM) * height)
    return shuttle.Geometry((cx, cy), bounds)


def resolve_key(requested):
    """The teleport key, validated, or None with the reason printed."""
    key = (requested if requested is not None else pipeline.potion_config.teleport_key).strip()
    if not key:
        print("No teleport key: set teleport_key in user_config.txt, or pass --key.")
        return None
    try:
        keyboard.key_to_scan_codes(key)
    except Exception as exc:  # noqa: BLE001 - report a typo instead of a traceback
        print(f"{key!r} is not a key this system recognises ({exc}).")
        return None
    return key


def describe_guards():
    """Says up front which guards can actually see anything, since 'cannot tell' lets actions
    through - correct for main.py, but worth knowing before leaving a bot unattended."""
    pipeline._refresh_anchor(time.time())
    focused = pipeline.anchor_focused()
    lines = []
    if pipeline.in_play_check is None:
        lines.append("  no assets/in_play.json - it cannot tell a lobby from a game")
    if focused is None:
        lines.append("  the game window cannot be found (no pywin32, no _anchor in meters.json, or "
                     "the game is not running) - it cannot tell whether the game has focus")
    if not pipeline.meters:
        lines.append("  no assets/meters.json - it cannot read health, so --stop-below does nothing")
    if lines:
        print("WARNING: running with fewer guards than usual:\n" + "\n".join(lines))


def start_measuring(seconds, extra):
    if not MEASURE_TOOL.exists():
        print(f"--measure needs {MEASURE_TOOL.relative_to(ROOT)}, which is not on this branch yet "
              f"(it is on ocr-latency). Run it from that checkout in a second console instead.")
        return None
    command = [sys.executable, str(MEASURE_TOOL), "--seconds", f"{seconds:.0f}", "--delay", "0", *extra]
    print("starting: " + " ".join(command[1:]))
    return subprocess.Popen(command, cwd=str(ROOT))


def main():
    argv = sys.argv[1:]
    passthrough = []
    if "--" in argv:
        cut = argv.index("--")
        argv, passthrough = argv[:cut], argv[cut + 1:]

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=120.0, help="how long the whole run may last")
    parser.add_argument("--count", type=int, help="stop after this many teleports")
    parser.add_argument("--interval", type=float, default=2.5,
                        help="seconds from one teleport to the next - long enough for labels to come back")
    parser.add_argument("--distance", type=float, default=0.3,
                        help="how far out each leg goes, as a fraction of the game window's height")
    parser.add_argument("--key", help="the Teleport key (default: teleport_key in user_config.txt)")
    parser.add_argument("--stop-below", type=float, default=0.35, help="stop when health reads at or below this (0-1)")
    parser.add_argument("--mana-floor", type=float, default=0.10, help="wait while mana reads at or below this (0-1)")
    parser.add_argument("--delay", type=float, default=5.0, help="countdown to switch to the game")
    parser.add_argument("--log", type=Path, help="write one CSV row per teleport here")
    parser.add_argument("--measure", action="store_true", help="also run tools/measure_ocr_latency.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="move nothing and press nothing - print where it would point")
    args = parser.parse_args(argv)
    if passthrough and not args.measure:
        parser.error("arguments after -- are for --measure")

    key = resolve_key(args.key)
    if key is None and not args.dry_run:
        return 1
    describe_guards()

    stop = threading.Event()
    try:
        keyboard.add_hotkey("f4", stop.set)
        keyboard.add_hotkey("end", stop.set)
    except Exception as exc:  # noqa: BLE001 - e.g. macOS/Linux without the permissions `keyboard` needs
        print(f"WARNING: could not register F4/End ({exc!r}) - only Ctrl+C in this console stops it.")

    for remaining in range(int(args.delay), 0, -1):
        print(f"switch to the game - starting in {remaining}... (F4 or End stops it)")
        time.sleep(1)

    measurer = None
    if args.measure:
        measurer = start_measuring(args.seconds + FIRST_TELEPORT_DELAY_SECONDS + 3, passthrough)
        if measurer is None:
            return 1

    if args.dry_run:
        def teleport(x, y):
            print(f"  (dry run) would point at ({x}, {y}) and press {key or '(no key set)'}")
    else:
        teleport = actions.press_at(key, settle=pipeline.potion_config.aim_settle)

    log_file = None
    on_leg = None
    if args.log:
        log_file = open(args.log, "w", encoding="utf-8")
        log_file.write("wall_time,perf_counter,teleport,aim_x,aim_y,aimed_px,outcome,moved_px,land_seconds\n")

        def on_leg(leg):
            #Written once the landing is judged - so take the time since the key back off, to
            #stamp the moment of the press.
            since_press = time.monotonic() - leg.pressed_at
            log_file.write(f"{time.time() - since_press:.3f},{time.perf_counter() - since_press:.3f},"
                           f"{leg.number},{leg.aim_x},{leg.aim_y},{leg.aimed_px:.0f},{leg.outcome},"
                           f"{'' if leg.moved_px is None else format(leg.moved_px, '.0f')},"
                           f"{'' if leg.land_seconds is None else format(leg.land_seconds, '.3f')}\n")
            log_file.flush()

    outcome = None
    try:
        with getattr(mss, "MSS", mss.mss)() as sct:
            sensors = Sensors(sct)
            #WARM THE GUARDS UP before anything is pressed. The in-play check holds its previous
            #answer until a miss has lasted IN_PLAY_MISS_GRACE_SECONDS, and its previous answer at
            #startup is None ("cannot tell", which lets actions through) - so a first press straight
            #away would go out before a lobby had been seen for long enough to say no. Looking for
            #the delay OCR needs anyway costs nothing.
            warm_until = time.monotonic() + FIRST_TELEPORT_DELAY_SECONDS
            while time.monotonic() < warm_until:
                sensors.look()
                time.sleep(shuttle.POLL_SECONDS)

            height = (pipeline.anchor_rect() or pipeline.CAPTURE_RECT)[3]
            settings = shuttle.Settings(distance=args.distance * height, interval=args.interval,
                                        seconds=args.seconds, count=args.count,
                                        stop_below=args.stop_below, mana_floor=args.mana_floor,
                                        scale=pipeline.CAPTURE_SCALE)
            print(f"teleporting with {key or '(no key set)'}, {settings.distance:.0f}px out and "
                  f"back, every {settings.interval}s, for up to {settings.seconds:.0f}s"
                  f"{f' or {settings.count} teleports' if settings.count else ''}.")
            outcome = shuttle.run(sensors.look, geometry, pipeline.actions_allowed, sensors.health,
                                  sensors.mana, teleport, settings, on_leg=on_leg,
                                  cancelled=stop.is_set,
                                  dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C).")
    finally:
        if log_file is not None:
            log_file.close()
            print(f"teleport log written to {args.log}")

    if outcome is not None:
        print("\n" + shuttle.summary(outcome))
    if measurer is not None:
        print("\nwaiting for measure_ocr_latency.py to finish its own time and report...")
        try:
            measurer.wait()
        except KeyboardInterrupt:
            measurer.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
