"""Live check: does everything that READS the game work on your machine, right now? PASS / FAIL.

    python tools/live_check.py                  # guided: about two minutes with the game open
    python tools/live_check.py --only town menu # just some of the steps (names below)
    python tools/live_check.py --replay recordings/live_check_20261016_201500
                                                # re-judge a saved run, no game needed

WHY THIS EXISTS: the test cards (python tests/run_all.py, and CI on every pull request) check every
detector against saved screenshots - but a saved screenshot cannot tell you whether YOUR game, at
YOUR resolution, in YOUR window, is still read correctly today. That used to mean watching the
console and the F5 panel by eye. This does the watching: it walks you through a few game states,
captures each one, judges it with the pipeline's own code, and prints one line per check.

HOW TO RUN IT:
  1. Close main.py / the control panel first (both want the screen capture, see below).
  2. Start Diablo II in a window or borderless window, so you can see this console beside it.
  3. Follow the prompt for each step, get the game into that state, and press F9 (the game can
     stay in front - F9 is a global hotkey). F10 skips a step, End stops.
  4. Read the summary. Every FAIL line says what was expected and what was seen.

IT ONLY LOOKS. Nothing here moves the mouse, clicks or presses a key in the game - you do every
step yourself - so it is safe to run on any character, anywhere.

EVERY RUN IS SAVED to recordings/live_check_<date>_<time>/ (gitignored): one full screenshot per
step, report.txt and report.json. If something fails, hand that folder to Claude: the screenshots
can become committed test fixtures, so the failure is caught by CI from then on instead of by you.
--replay re-judges a saved folder with the current code, which is how a fix is checked without the
game.

THE STEPS (names for --only):
  town       in a game, in town, nothing open, full health and mana
  inventory  the inventory open
  stash      the stash and inventory open (stand next to the stash)
  menu       the Esc menu open - do not click anything
  away       this console clicked, so the game is NOT the window in front
  lobby      left the game: the Create Game screen showing

WHAT IT DOES NOT COVER, deliberately: anything that ACTS - drinking at the right moment, picking
an item up, the F7 run. Those need the bot pressing keys in a live game; see docs/testing.md for
the short list still checked by a person, and tools/teleport_bot.py for the unattended timing run.

Captures with its own mss, never a DXGI frame source: DXGI is a per-output singleton and a second
consumer silently steals the first one's frames (EH#39), which is also why main.py must be closed -
the capture-speed line measures DXGI, and with main.py running it would measure a stolen source.
"""
import sys
from pathlib import Path
#Run directly (python tools/live_check.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import statistics
import time
from datetime import datetime

import cv2 as cv
import numpy as np

import main
from core import frame_source
from core import game_state
from core import text_detection
from core import window_region

ROOT = Path(__file__).resolve().parent.parent
METER_NAMES = [m.name for m in main.meters]
RECORDINGS = ROOT / "recordings"

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "info"
#Readings are taken over this many frames, the way the pipeline smooths them (median of 5), and
#spread over long enough that the in-play check's 0.3s miss debounce has settled.
SAMPLES = 8
SAMPLE_GAP_SECONDS = 0.1
#Town refills both orbs; a reading under this in town is a misread, not a wounded character.
FULL_AT_LEAST = 0.90
#An OCR call must cost well under the interval between calls (CLAUDE.md, main.py's
#OCR_INTERVAL_SECONDS): at or above it, scans run back to back and starve tracking.
OCR_COST_WARN_FRACTION = 0.75
OCR_TIMING_RUNS = 3


#--- one look at the screen, and everything the pipeline would read off it ---------------------
class Look:
    """Several frames of one game state, read the way the pipeline reads them.

    The pipeline's own helpers do the reading (main._refresh_anchor, _update_in_play, _panel_open,
    _find_save_and_exit, ...) the way tools/diagnose_quit.py uses them, so a PASS here means the
    code main.py runs passed - not a second copy of it that could drift.
    """

    def __init__(self, frames):
        self.frames = frames                 #full-resolution BGRA, oldest first
        self.full = frames[-1]
        smoother = game_state.Smoother()
        self.in_play = None
        self.meters = {}
        self.belt = None
        self.panels = {}
        for frame in frames:
            main._anchor_checked = 0.0       #re-locate the window for every frame, not once a second
            active = main._refresh_anchor(time.time())
            small = cv.resize(frame, (0, 0), fx=main.CAPTURE_SCALE, fy=main.CAPTURE_SCALE)
            small = cv.cvtColor(small, cv.COLOR_BGRA2BGR)
            gray = cv.cvtColor(small, cv.COLOR_BGR2GRAY)
            self.in_play = main._update_in_play(gray)
            raw = (game_state.read_all(small, active) if active
                   else {name: None for name in METER_NAMES})
            self.meters = smoother.update(raw)
            main._belt_checked = 0.0
            main._refresh_belt(small, time.time())
            self.belt = main.current_belt()
            self.panels = {p.name: main._panel_open(p, gray) for p in main.ui_panels}
        self.in_play_score = main.in_play_score()
        self.window = main.anchor_rect()
        self.focused = main.anchor_focused()
        self.allowed = main.actions_allowed()
        self.unprofiled = main.unprofiled_size()

    def save_and_exit(self):
        return main._find_save_and_exit(self.full)

    def lobby(self):
        """(form, name read) - either can be None."""
        form = main._lobby_form(self.full)
        if form is None:
            return None, None
        return form, main._read_lobby_game_name(self.full, form)

    def ocr_cost(self):
        """Median seconds of one find_text_matches() on this frame's viewport - the call the OCR
        thread makes, on the crop it makes it on. None when OCR is not available."""
        if not text_detection.ocr_available():
            return None
        frame = cv.cvtColor(self.full, cv.COLOR_BGRA2BGR)
        h = frame.shape[0]
        viewport = frame[int(h * main.VIEWPORT_TOP_MARGIN):int(h * (1 - main.VIEWPORT_BOTTOM_MARGIN))]
        costs = []
        for _ in range(OCR_TIMING_RUNS):
            start = time.perf_counter()
            text_detection.find_text_matches(viewport, main.target_items)
            costs.append(time.perf_counter() - start)
        return statistics.median(costs)

    def facts(self):
        """What was seen, for report.json - and for --replay to put the window back."""
        return {"window": list(self.window) if self.window else None, "focused": self.focused,
                "in_play": self.in_play, "in_play_score": self.in_play_score,
                "meters": self.meters, "belt": self.belt, "panels": self.panels,
                "allowed": self.allowed, "frame_size": [self.full.shape[1], self.full.shape[0]]}


def pct(value):
    return "cannot tell" if value is None else f"{value * 100:.0f}%"


def yn(value):
    return {True: "yes", False: "no", None: "cannot tell"}[value]


#--- the checks, one function per step; each returns [(status, line), ...] ----------------------
def window_checks(look):
    out = []
    title = (main.meter_anchor or {}).get("window_title")
    if not title:
        return [(FAIL, "no game window configured - run tools/calibrate_meters.py once")]
    if look.window is None:
        return [(FAIL, f"the game window '{title}' was not found - is the game running, and is "
                       f"that its exact title? (assets/meters.json, \"_anchor\")")]
    out.append((PASS, f"game window found: {look.window[2]}x{look.window[3]} at "
                      f"({look.window[0]}, {look.window[1]})"))
    if look.unprofiled:
        have = ", ".join(f"{w}x{h}" for w, h in main.known_profile_sizes()) or "none"
        out.append((FAIL, f"no meter calibration for a {look.unprofiled[0]}x{look.unprofiled[1]} "
                          f"window (calibrated: {have}) - run tools/calibrate_meters.py at this size"))
    else:
        out.append((PASS, "a meter calibration matches this window size"))
    return out


def expect(out, label, seen, wanted, why=""):
    """One check: PASS if `seen` is `wanted`, else FAIL saying both - None is its own answer."""
    ok = seen is wanted if wanted in (True, False, None) else seen == wanted
    out.append((PASS if ok else FAIL,
                f"{label}: {yn(seen) if seen in (True, False, None) else seen}"
                + ("" if ok else f" (expected {yn(wanted) if wanted in (True, False, None) else wanted}"
                   f"{' - ' + why if why else ''})")))


def check_town(look):
    out = window_checks(look)
    expect(out, "game is the window in front", look.focused, True,
           "click into the game before pressing F9")
    expect(out, f"in play (HUD art score {look.in_play_score or 0:.3f})", look.in_play, True,
           "the HUD art was not recognised - assets/in_play.json")
    for name in METER_NAMES:
        value = look.meters.get(name)
        out.append((PASS if value is not None and value >= FULL_AT_LEAST else FAIL,
                    f"{name} reads {pct(value)}" + ("" if value is not None and value >= FULL_AT_LEAST
                                                    else f" (town refills it - expected at least "
                                                         f"{FULL_AT_LEAST:.0%})")))
    if main.belt_config is None:
        out.append((WARN, "no assets/belt.json - potion keys come from user_config.txt"))
    elif look.belt is None:
        out.append((FAIL, "the potion belt could not be read (cannot tell)"))
    else:
        contents = ", ".join(f"{key}={supply or 'empty'}" for key, supply in look.belt)
        filled = sum(1 for _key, supply in look.belt if supply)
        out.append((PASS if filled else WARN,
                    f"belt read: {contents}" + ("" if filled else " - every slot empty, is that right?")))
    for name, is_open in look.panels.items():
        expect(out, f"{name} panel open", is_open, False, "close it, or its art matches something else")
    expect(out, "Save and Exit button showing", look.save_and_exit() is not None, False,
           "the menu is closed, so this is a false match")
    expect(out, "bot actions allowed", look.allowed, True)
    cost = look.ocr_cost()
    if cost is None:
        out.append((FAIL, "OCR is not available - Tesseract is not installed (see requirements.txt)"))
    else:
        limit = main.OCR_INTERVAL_SECONDS * OCR_COST_WARN_FRACTION
        out.append((PASS if cost < limit else WARN,
                    f"one OCR scan costs {cost:.2f}s here; scans start every "
                    f"{main.OCR_INTERVAL_SECONDS}s" + ("" if cost < limit else
                    " - too close: raise OCR_INTERVAL_SECONDS in main.py above the cost")))
    return out


def check_inventory(look):
    out = []
    if "inventory" not in look.panels:
        return [(FAIL, "no assets/inventory.json - the inventory is never filtered")]
    expect(out, "inventory panel open", look.panels["inventory"], True,
           "items in the bag would be chased as if on the ground")
    expect(out, "still in play with a panel open", look.in_play, True)
    return out


def check_stash(look):
    out = []
    if "stash" not in look.panels:
        return [(FAIL, "no assets/stash.json - the stash is never filtered")]
    expect(out, "stash panel open", look.panels["stash"], True)
    if "inventory" in look.panels:
        expect(out, "inventory panel open beside it", look.panels["inventory"], True)
    return out


def check_menu(look):
    out = []
    where = look.save_and_exit()
    expect(out, "Save and Exit button found (F3 clicks it)", where is not None, True)
    #The menu dims the screen. The orbs must read as before, or not at all - never a low number,
    #which would drink a potion (docs/todolist.md, "a plausible-but-wrong reading in the dimmed menu").
    for name in METER_NAMES:
        value = look.meters.get(name)
        ok = value is None or value >= FULL_AT_LEAST
        out.append((PASS if ok else FAIL,
                    f"{name} under the dimmed menu reads {pct(value)}"
                    + ("" if ok else " - a believable wrong number; this would drink a potion")))
    expect(out, f"still counts as in play (score {look.in_play_score or 0:.3f})", look.in_play, True,
           "F3 refuses to click Save and Exit when this says no")
    return out


def check_away(look):
    out = []
    expect(out, "game is the window in front", look.focused, False,
           "click this console window before pressing F9")
    expect(out, "bot actions allowed", look.allowed, False,
           "keys would be typed into whatever window is in front")
    return out


def check_lobby(look):
    out = []
    expect(out, f"in play (score {look.in_play_score or 0:.3f})", look.in_play, False,
           "potions would be typed into the game name box")
    expect(out, "bot actions allowed", look.allowed, False)
    form, name = look.lobby()
    expect(out, "Create Game form found (F3 types into it)", form is not None, True)
    if form is not None:
        out.append((PASS if name else FAIL,
                    f"game name read from the box: {name!r}" + ("" if name else
                    " - F3 could not continue; tools/diagnose_lobby.py shows the crop")))
    return out


STEPS = [
    ("town", "In a game, IN TOWN, nothing open, full health and mana. Stand still.", check_town),
    ("inventory", "Open the INVENTORY (I).", check_inventory),
    ("stash", "Stand at the stash and open it (inventory shows too). F10 to skip.", check_stash),
    ("menu", "Close the panels, then press Esc to open the game MENU. Don't click anything.", check_menu),
    ("away", "Close the menu, then CLICK THIS CONSOLE so the game is not in front.", check_away),
    ("lobby", "Save and Exit to the lobby - the Create Game screen showing.", check_lobby),
]
#A step's screenshot is numbered by its place in the full list, so --only and --replay agree on
#which file is which.
NUMBER = {name: number for number, (name, _prompt, _judge) in enumerate(STEPS, 1)}


#--- running it -----------------------------------------------------------------------------
def capture_speed():
    """[(status, line)] for the capture backend the pipeline would get. main.py must be closed:
    a second DXGI consumer steals frames rather than failing (EH#39)."""
    try:
        source = frame_source.FrameSource(main.monitor_area)
    except Exception as e:                   # noqa: BLE001 - report it, do not die on it
        return [(FAIL, f"screen capture could not start: {type(e).__name__}: {e}")]
    try:
        costs, start = [], time.perf_counter()
        while time.perf_counter() - start < 1.0:
            t = time.perf_counter()
            frame = source.grab()
            if frame is not None:
                costs.append(time.perf_counter() - t)
        name = source.name
    finally:
        source.close()
    if not costs:
        return [(FAIL, f"screen capture ({name}) returned no frame in a second")]
    return [(INFO, f"capture backend {name}: a grab costs {statistics.median(costs) * 1000:.1f}ms "
                   f"(the pipeline asks for {main.TARGET_FPS} a second)")]


def grab_frames(sct):
    frames = []
    for _ in range(SAMPLES):
        raw = sct.grab(main.monitor_area)
        frames.append(np.frombuffer(raw.raw, np.uint8).reshape(raw.height, raw.width, 4).copy())
        time.sleep(SAMPLE_GAP_SECONDS)
    return frames


def wait_for_key():
    """'go', 'skip' or 'stop' - F9 / F10 / End, pressed anywhere (the game can stay in front)."""
    import keyboard
    while True:
        event = keyboard.read_event(suppress=False)
        if event.event_type != "down":
            continue
        if event.name == "f9":
            return "go"
        if event.name == "f10":
            return "skip"
        if event.name == "end":
            return "stop"


def beep():
    try:
        import winsound
        winsound.Beep(880, 120)
    except Exception:                        # noqa: BLE001 - not Windows, or no sound
        print("\a", end="", flush=True)


def report_lines(results):
    """The human report, one line per check, and the counts."""
    lines, counts = [], {PASS: 0, FAIL: 0, WARN: 0}
    for step, checks in results:
        lines.append(f"\n[{step}]")
        if checks is None:
            lines.append("  skipped")
            continue
        for status, line in checks:
            counts[status] = counts.get(status, 0) + 1
            lines.append(f"  {status:<4}  {line}")
    lines.append(f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[WARN]} warnings")
    return lines, counts


def write_report(folder, results, facts):
    lines, counts = report_lines(results)
    header = [f"Hunter's Eye live check, {datetime.now():%Y-%m-%d %H:%M:%S}",
              f"screen {main.monitor_area['width']}x{main.monitor_area['height']}"]
    (folder / "report.txt").write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    (folder / "report.json").write_text(json.dumps(
        {"results": [{"step": s, "checks": c} for s, c in results], "facts": facts,
         "counts": counts}, indent=2, default=str), encoding="utf-8")
    return lines, counts


def run_live(steps):
    import mss
    folder = RECORDINGS / f"live_check_{datetime.now():%Y%m%d_%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    print(__doc__.strip().splitlines()[0])
    print("F9 = this step is ready, F10 = skip it, End = stop. The game can stay in front.\n")
    results = [("setup", capture_speed())]
    for status, line in results[0][1]:
        print(f"  {status:<4}  {line}")
    facts = {}
    with mss.mss() as sct:
        for count, (name, prompt, judge) in enumerate(steps, 1):
            print(f"\nStep {count}/{len(steps)} [{name}]: {prompt}\n  ...then press F9.")
            key = wait_for_key()
            if key == "stop":
                print("Stopped.")
                break
            if key == "skip":
                results.append((name, None))
                print("  skipped")
                continue
            time.sleep(0.3)                  #let the key's own effect (if any) settle
            frames = grab_frames(sct)
            beep()
            cv.imwrite(str(folder / f"{NUMBER[name]:02d}_{name}.png"), frames[-1])
            look = Look(frames)
            checks = judge(look)
            facts[name] = look.facts()
            results.append((name, checks))
            for status, line in checks:
                print(f"  {status:<4}  {line}")
    lines, counts = write_report(folder, results, facts)
    print("\n".join(lines[-1:]))
    print(f"\nSaved to {folder}")
    print("Paste report.txt into the project thread. If anything FAILED, share that folder too - "
          "its screenshots can become test fixtures.")
    return 1 if counts[FAIL] else 0


def run_replay(folder, steps):
    """Re-judges a saved run with today's code. The window is put back as it was recorded -
    position, size and focus - since none of it is in a screenshot."""
    folder = Path(folder)
    saved = json.loads((folder / "report.json").read_text(encoding="utf-8")).get("facts", {})
    #One screenshot repeated stands in for several frames, all taken at once - so a "not in
    #play" must not be held back by the debounce that waits out a brief flicker live.
    main.IN_PLAY_MISS_GRACE_SECONDS = 0.0
    results = []
    for name, _prompt, judge in steps:
        image = folder / f"{NUMBER[name]:02d}_{name}.png"
        if not image.exists():
            results.append((name, None))
            continue
        frame = cv.imread(str(image), cv.IMREAD_UNCHANGED)
        if frame.ndim == 2:
            frame = cv.cvtColor(frame, cv.COLOR_GRAY2BGRA)
        elif frame.shape[2] == 3:
            frame = cv.cvtColor(frame, cv.COLOR_BGR2BGRA)
        recorded = saved.get(name, {})
        replay_window(recorded.get("window"), recorded.get("focused"), frame.shape)
        checks = judge(Look([frame] * SAMPLES))
        results.append((name, checks))
    lines, counts = report_lines(results)
    print("\n".join([f"Replay of {folder}"] + lines))
    return 1 if counts[FAIL] else 0


def replay_window(rect, focused, frame_shape):
    """Makes the pipeline see a recorded window instead of looking for a live one, and sizes the
    capture to the recorded screenshot. Module globals are swapped, the way the tests patch them."""
    h, w = frame_shape[:2]
    main.monitor_area = {"top": 0, "left": 0, "width": w, "height": h}
    main.CAPTURE_RECT = (0, 0, w, h)
    found = None if rect is None else (1, tuple(rect))
    window_region.find_window = lambda _title: found
    window_region.is_foreground = lambda handle: None if handle is None else focused
    main._anchor_rect = main._UNRESOLVED     #forget the previous step's window
    main._in_play_missing_since = None
    main._in_play = None


def pick(only):
    if not only:
        return STEPS
    unknown = set(only) - {name for name, _p, _j in STEPS}
    if unknown:
        sys.exit(f"unknown step(s): {', '.join(sorted(unknown))} - "
                 f"choose from {', '.join(n for n, _p, _j in STEPS)}")
    return [step for step in STEPS if step[0] in only]


def cli():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--only", nargs="+", metavar="STEP", help="run just these steps")
    parser.add_argument("--replay", metavar="FOLDER", help="re-judge a saved run, no game needed")
    args = parser.parse_args()
    steps = pick(args.only)
    if args.replay:
        return run_replay(args.replay, steps)
    return run_live(steps)


if __name__ == "__main__":
    try:
        sys.exit(cli())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(1)
