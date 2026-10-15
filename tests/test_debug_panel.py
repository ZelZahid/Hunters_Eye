"""Tests for what the F5 debug panel shows beyond the meters: the BOT and ITEMS sections. No game.

WHY THIS EXISTS: the panel is what the owner reads while playing to answer "why is nothing
happening?", so a row that says the wrong thing is worse than no row. Two failures are specific to
it and silent:
  - COST. The panel only redraws when its rows change, and a redraw lands on the GAME's frame time
    (EH#38). A row that changes on its own - a timer with decimals, an FPS counter - turns a
    display into a steady cost on the game, with nothing on screen to say so.
  - OCR READS IT. The overlay is in every capture (EH#49). The ITEMS section shows the name of the
    item being collected, which matches its own target by definition, so auto-collect would chase
    the panel itself unless OCR ignores the panel's rectangle.

The sections are driven by setting main.py's module-level state directly - the same globals the
hotkeys, the auto-collect loop and the sequences write - so nothing here needs a keyboard or a game.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import main

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


NOW = 1_000_000.0


def reset():
    """Every piece of state the sections read, back to "a bot that just started"."""
    main.debug_panel_visible = True
    main.snooze_until = 0.0
    main.potion_snooze_until = 0.0
    main.run_status = None
    main.run_step = None
    main.collect_target = None
    main.last_panel_filter = (0, ())
    main.sequence_cancel.clear()
    with main.text_tracks_lock:
        main.shared_text_tracks = []
    main.actions_allowed = lambda: True


def row(label, rows=None):
    rows = main._bot_section(NOW) + main._items_section(NOW) if rows is None else rows
    return next((r for r in rows if r[0] == label), None)


def value(label, rows=None):
    found = row(label, rows)
    return None if found is None else found[1]


def drawable(rows):
    """Exactly the shape Overlay.draw_panel() unpacks, with nothing it would choke on."""
    for r in rows:
        if not (isinstance(r, tuple) and len(r) == 4):
            return False
        label, text, fraction, color = r
        if not (isinstance(label, str) and isinstance(text, str)):
            return False
        if fraction is not None and not 0.0 <= fraction <= 1.0:
            return False
        if not (len(color) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in color)):
            return False
    return True


original_allowed = main.actions_allowed

print("\nT-501  Every section draws rows the overlay can draw, in every state")
#Each state built from scratch, so a failure names the state it was in.
for name, make in [
    ("just started", lambda: None),
    ("both snoozed", lambda: (setattr(main, "snooze_until", NOW + 4.2),
                              setattr(main, "potion_snooze_until", NOW + 30))),
    ("guards say no", lambda: setattr(main, "actions_allowed", lambda: False)),
    ("run going", lambda: (main._run_started("pindle"),
                           main._run_log("walk: located on the map (match 0.81)."))),
    ("run crashed", lambda: (main._run_started("pindle"),
                             main._run_ended("pindle", None, error=True))),
    ("collecting", lambda: (setattr(main, "collect_target", "Full Rejuvenation Potion"),
                            setattr(main, "last_panel_filter", (2, ("inventory", "stash"))))),
]:
    reset()
    make()
    title, rows = main._debug_panel(NOW)
    check(f"{name}: a title and {len(rows)} drawable rows", isinstance(title, str) and drawable(rows))
reset()
main.debug_panel_visible = False
check("F5 off: nothing at all, so the overlay hides the panel", main._debug_panel(NOW) == (None, None))
reset()
check("before anything has happened: collect on, potions on, run idle, no target",
      (value("collect"), value("potions"), value("run"), value("target"))
      == ("on", "on" if main.POTION_DRINKING_ENABLED else "off", "idle", "none"))
main.actions_allowed = lambda: False
check("when the guards say no, collect and potions read 'paused', not 'on'",
      value("collect") == "paused"
      and value("potions") == ("paused" if main.POTION_DRINKING_ENABLED else "off"))

print("\nT-502  A row only changes when something happens - never on its own")
reset()
main.snooze_until = NOW + 5.5
first = main._debug_panel(NOW)
check("the same state twice gives identical rows (so the overlay skips the redraw)",
      first == main._debug_panel(NOW))
check("a snooze 0.3s later within the same whole second does not change the panel",
      main._debug_panel(NOW) == main._debug_panel(NOW + 0.3))
check("...and it does change once the next whole second ticks over",
      main._debug_panel(NOW) != main._debug_panel(NOW + 0.6))
bot_items = main._bot_section(NOW) + main._items_section(NOW)
check("no value carries a decimal point (no fractions of a second, no FPS)",
      not any("." in r[1] for r in bot_items))

print("\nT-503  A snooze counts down in whole seconds and re-arms by itself")
reset()
main.snooze_until = NOW + 6.2
check("6.2s left reads 'snoozed 7s' - rounded UP, never 'snoozed 0s' while still snoozed",
      value("collect") == "snoozed 7s")
main.snooze_until = NOW + 0.1
check("0.1s left still reads 'snoozed 1s'", value("collect") == "snoozed 1s")
main.snooze_until = NOW - 0.1
check("once it runs out it reads 'on' again, with nothing pressed", value("collect") == "on")
if main.POTION_DRINKING_ENABLED:
    main.potion_snooze_until = NOW + 30
    check("the potion snooze is shown on its own row ('snoozed 30s')", value("potions") == "snoozed 30s")

print("\nT-504  A run's last step outlives the run; the next run starts clean")
reset()
main._run_started("pindle")
check("a running run says so", value("run") == "pindle running")
main._run_log("walk: cannot find where I am on the map (best match 0.41) - stopping.")
main._run_ended("pindle", False)
check("a run that gave up reads 'failed'", value("run") == "pindle failed")
walk = row("walk")
check(f"...and still shows WHY, labelled by what was speaking ({walk})",
      walk is not None and walk[1].startswith("cannot find where I am"))
check("the useful number at the end survives the wrapping ('0.41')",
      any("0.41" in r[1] for r in main._bot_section(NOW)))
main._run_started("pindle")
check("starting the next run clears the old step", row("walk") is None)
main.sequence_cancel.set()
main._run_ended("pindle", False, cancelled=main.sequence_cancel.is_set())
check("F4 during a run reads 'stopped', not 'failed'", value("run") == "pindle stopped")
main._run_ended("pindle", True)
check("a run that got through reads 'done'", value("run") == "pindle done")
reset()
main._run_started("next game")
main._run_ended("next game", None, error=True)
check("a sequence that crashed reads 'error'", value("run") == "next game error")
check("the F7 run's log reaches the panel - pindle_run hands routes/pindle.py _run_log",
      "log=_run_log" in Path(main.__file__).read_text(encoding="utf-8"))

print("\nT-505  A long step wraps onto two rows instead of stretching the panel across the game")
rows = main._step_rows("walk: hovered the portal but never read its label - not clicking. "
                       "(Something is covering it, or it is not the portal.)")
check(f"at most {main.PANEL_STEP_MAX_LINES} rows ({len(rows)})", len(rows) <= main.PANEL_STEP_MAX_LINES)
check(f"none longer than {main.PANEL_STEP_LINE_CHARS} characters",
      all(len(r[1]) <= main.PANEL_STEP_LINE_CHARS for r in rows))
check("cut text says so ('...')", rows[-1][1].endswith("..."))
check("only the first row carries the label", rows[0][0] == "walk" and all(r[0] == "" for r in rows[1:]))
check("a line with no speaker prefix is labelled 'step'", main._step_rows("Conviction on")[0][0] == "step")
check("a target name that would not fit is cut, not left to stretch the plate",
      len(main._clip("A" * 80)) == main.PANEL_STEP_LINE_CHARS)

print("\nT-506  A section that crashes costs one row, not the panel")
reset()


def broken_section(now):
    raise KeyError("boom")


saved = list(main.PANEL_SECTIONS)
main.PANEL_SECTIONS[:] = [broken_section] + saved
try:
    title, rows = main._debug_panel(NOW)
    check("the panel still builds", isinstance(title, str) and drawable(rows))
    check("the broken section shows up as one red 'error' row",
          sum(1 for r in rows if r[1].startswith("error:")) == 1)
    check("...and every other section is still there", row("collect", rows) is not None
          and row("target", rows) is not None)
finally:
    main.PANEL_SECTIONS[:] = saved

print("\nT-507  Adding a value to the panel is one function in PANEL_SECTIONS")
main.PANEL_SECTIONS.append(lambda now: [("battery", "81%", 0.81, (60, 230, 90))])
try:
    title, rows = main._debug_panel(NOW)
    check("a new section's row appears, after the existing ones", rows[-1][0] == "battery")
finally:
    main.PANEL_SECTIONS[:] = saved

print("\nT-508  OCR never reads the debug panel as items")
#A full-resolution frame of the whole captured screen, as detect_text() hands it over.
frame = np.zeros((main.CAPTURE_RECT[3], main.CAPTURE_RECT[2]), np.uint8)
inside = (60, 330, 200, 18, "FULL REJUVENATION POTION", True, None)    #centre (160, 339)
outside = (900, 500, 200, 18, "FULL REJUVENATION POTION", True, None)
saved_panels = main.ui_panels
main.ui_panels = []   #only the debug panel's own mask is under test here
try:
    main.debug_panel_screen_rect = None
    check("panel hidden: nothing is dropped", main._outside_open_panels([inside, outside], frame)
          == [inside, outside])
    main.debug_panel_screen_rect = (main.CAPTURE_RECT[0] + 14, main.CAPTURE_RECT[1] + 310,
                                    main.CAPTURE_RECT[0] + 500, main.CAPTURE_RECT[1] + 560)
    kept = main._outside_open_panels([inside, outside], frame)
    check("panel shown: the item name drawn ON the panel is dropped, the one on the ground kept",
          kept == [outside])
    check("...silently - it is not counted as an item ignored on a game panel",
          main.last_panel_filter == (0, ()))
    small = np.zeros((frame.shape[0] // 2, frame.shape[1] // 2), np.uint8)
    half = tuple(v // 2 if i < 4 else v for i, v in enumerate(inside))
    check("the mask follows the frame's scale (a half-size frame, a half-size match)",
          main._outside_open_panels([half], small) == [])
finally:
    main.ui_panels = saved_panels
    main.debug_panel_screen_rect = None

print("\nT-509  A shrinking panel stays masked where its old text may still be on screen")
main._panel_rects = (None, None)
main._publish_panel_rect((10, 300, 600, 520))
main._publish_panel_rect((10, 300, 420, 520))
check("after shrinking, the mask is still the wider plate (the union of the last two)",
      main.debug_panel_screen_rect == (10, 300, 600, 520))
main._publish_panel_rect(None)
check("hiding the panel clears the mask at once - nothing is drawn, nothing to mask",
      main.debug_panel_screen_rect is None)
main._publish_panel_rect((10, 300, 420, 520))
check("showing it again masks only what is drawn now", main.debug_panel_screen_rect == (10, 300, 420, 520))
main._panel_rects = (None, None)
main.debug_panel_screen_rect = None

reset()
main.actions_allowed = original_allowed
print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
