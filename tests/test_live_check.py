"""tools/live_check.py, the script you run with the game open, judged against real screenshots.
No game required.

WHY THIS EXISTS: the live check replaces watching the console by eye, so it is only worth running
if its PASS means pass and its FAIL means fail. A checker that says PASS to everything is worse
than none - it is the "skip that looks like a pass" this suite is built to avoid, just moved into
a tool. So its judgement is held to the committed fixtures: real screenshots of the states it asks
you for, with the window and focus put back the way --replay puts them back.

Two fixtures are masked where the in-play art sits (inventory_open.png, esc_menu.png - see
tests/fixtures/README.md), so their "still in play" lines are not asserted here; the line each card
does assert is named in it.

Needs a display (main.py asks the screen for its size at import) and Tesseract (the lobby name is
read with OCR). Without them it skips on a developer machine and fails under CI.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
UNDER_CI = bool(os.environ.get("CI"))
failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def cannot_run(why):
    if UNDER_CI:
        check(f"needs a display and Tesseract, and CI has both - {why}", False)
    else:
        print(f"SKIPPED: {why} - CI runs this one for real")
    sys.exit(1 if failures else 0)


try:
    sys.path.insert(0, str(ROOT / "tools"))
    import cv2 as cv
    import live_check as lc
except Exception as e:                      # noqa: BLE001 - no display, whatever
    cannot_run(f"tools/live_check.py could not be imported ({type(e).__name__}: {e})")

if not lc.text_detection.ocr_available():
    cannot_run("Tesseract is not installed")

FULL = [0, 0, 1920, 1080]   #every fixture is a full 1920x1080 capture of a fullscreen game


def judge(fixture, step, window=FULL, focused=True):
    """[(status, line)] from the live check's own judge for `step`, on one committed screenshot."""
    path = FIXTURES / fixture
    if not path.exists():
        check(f"{fixture} is committed (a missing fixture is a failure, never a skip)", False)
        return []
    frame = cv.cvtColor(cv.imread(str(path)), cv.COLOR_BGR2BGRA)
    lc.main.IN_PLAY_MISS_GRACE_SECONDS = 0.0
    lc.replay_window(window, focused, frame.shape)
    judge_step = {name: fn for name, _prompt, fn in lc.STEPS}[step]
    return judge_step(lc.Look([frame] * lc.SAMPLES))


def line(results, starts):
    """(status, text) of the first result whose text starts with `starts`, or (None, '')."""
    return next(((s, t) for s, t in results if t.startswith(starts)), (None, ""))


def all_pass(label, results):
    bad = [f"{s} {t}" for s, t in results if s not in (lc.PASS, lc.INFO)]
    check(f"{label}: every line PASS ({len(results)} lines){'' if not bad else ' - ' + '; '.join(bad)}",
          results and not bad)


print("\nT-302  The live check passes real screenshots of the states it asks for")
all_pass("lobby.png as the lobby step", judge("lobby.png", "lobby"))
all_pass("lobby.png with the game NOT in front, as the away step", judge("lobby.png", "away", focused=False))
all_pass("stash_open.png as the stash step", judge("stash_open.png", "stash"))
status, text = line(judge("inventory_open.png", "inventory"), "inventory panel open")
check(f"inventory_open.png: {status} {text}", status == lc.PASS)
status, text = line(judge("esc_menu.png", "menu"), "Save and Exit button found")
check(f"esc_menu.png: {status} {text}", status == lc.PASS)
town = judge("in_game_dim.png", "town")
for starts in ("game window found", "a meter calibration matches", "game is the window in front",
               "in play", "health reads", "belt read", "inventory panel open", "stash panel open",
               "Save and Exit button showing", "bot actions allowed"):
    status, text = line(town, starts)
    check(f"in_game_dim.png as town: {status} {text}", status == lc.PASS)
status, text = line(town, "one OCR scan costs")
check(f"in_game_dim.png as town: the OCR cost is measured and judged ({status} {text})",
      status in (lc.PASS, lc.WARN))


print("\nT-303  A screen in the wrong state FAILS, and the line says what was expected")
#in_game_dim.png is a dungeon, not town: its mana really is low, and the town step must say so.
status, text = line(town, "mana reads")
check(f"a genuinely low orb fails the 'town refills it' check: {status} {text}",
      status == lc.FAIL and "expected at least" in text)
status, text = line(judge("lobby.png", "town"), "in play")
check(f"the lobby offered as town: {status} {text}", status == lc.FAIL and "expected yes" in text)
wrong = judge("in_game_dim.png", "lobby")
status, text = line(wrong, "in play")
check(f"a game offered as the lobby: {status} {text}", status == lc.FAIL and "expected no" in text)
status, text = line(wrong, "Create Game form found")
check(f"...and no form is found in it: {status} {text}", status == lc.FAIL)
status, text = line(judge("lobby.png", "away", focused=True), "game is the window in front")
check(f"the game still in front at the away step: {status} {text}", status == lc.FAIL)
nowhere = judge("in_game_dim.png", "town", window=None)
check(f"no game window: {nowhere[0][0]} {nowhere[0][1][:70]}...",
      nowhere[0][0] == lc.FAIL and "not found" in nowhere[0][1])


print("\nT-304  --replay re-judges a saved run from the command line")
saved = Path(tempfile.mkdtemp(prefix="live_check_replay_"))
try:
    for step, fixture in (("away", "lobby.png"), ("lobby", "lobby.png"), ("town", "lobby.png")):
        shutil.copy(FIXTURES / fixture, saved / f"{lc.NUMBER[step]:02d}_{step}.png")
    (saved / "report.json").write_text(json.dumps({"facts": {
        "away": {"window": FULL, "focused": False},
        "lobby": {"window": FULL, "focused": True},
        "town": {"window": FULL, "focused": True}}}), encoding="utf-8")

    def replay(*steps):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "live_check.py"), "--replay",
                               str(saved), "--only", *steps], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300, cwd=ROOT)
        return done.returncode, done.stdout + done.stderr

    code, out = replay("away", "lobby")
    check(f"a run that passed replays as passed (exit {code}): "
          f"{next((l for l in out.splitlines() if 'passed,' in l), out.strip()[-80:])}",
          code == 0 and " 0 failed" in out)
    code, out = replay("town")
    check(f"a step that fails replays as failed - exit {code}", code == 1 and "FAIL" in out)
    code, out = replay("nonsense")
    check(f"an unknown step name is refused, naming the real ones (exit {code})",
          code != 0 and "lobby" in out)
finally:
    shutil.rmtree(saved, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
