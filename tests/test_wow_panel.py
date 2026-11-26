"""Tests for the WoW fishing panel (wow_gui.py) and the --control channel it drives wow.py through.
No game required.

wow.py itself is replaced by a stand-in script that speaks the same protocol (one word per line in,
@status / @catch lines out), so the panel is tested against exactly what it reads - and T-410 checks
the real wow.py source answers every word the panel sends, so the stand-in cannot drift from it.

T-410 needs nothing. T-406 to T-409 need tkinter and a display; under CI (the CI environment
variable) a skip is a FAILURE, as for the Diablo II panel's T-403.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import os
import re
import tempfile
import textwrap
import time

failures = 0
UNDER_CI = bool(os.environ.get("CI"))
REPO_ROOT = Path(__file__).resolve().parent.parent


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def wait_for(condition, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


print("\nT-410  Every word the panel sends is one wow.py --control answers to")
source = (REPO_ROOT / "wow.py").read_text(encoding="utf-8")
panel_source = (REPO_ROOT / "wow_gui.py").read_text(encoding="utf-8")
handlers = re.search(r"handlers = \{(.*?)\}", source, re.S)
check("wow.py --control registers toggle, pause, resume and quit",
      handlers is not None and all(f'"{word}"' in handlers.group(1)
                                   for word in ("toggle", "pause", "resume", "quit")))
check("...and one auto_pause_<minutes> per choice the panel offers",
      'handlers[f"auto_pause_{minutes}"]' in source and "for minutes in AUTO_PAUSE_CHOICES" in source)
sent = set(re.findall(r'self\.bot\.send\(f?"([a-z_]+)', panel_source))
check(f"the panel only sends those ({sorted(sent)})", sent <= {"toggle", "pause", "resume", "quit", "auto_pause_"})
check("end of input quits wow.py (a closed panel never leaves it pressing keys)",
      "on_close=quit_requested.set" in source)
check("the F5 button and the F5 key call the same function", 'lambda: toggle_pause("control panel")' in source
      and "keyboard.add_hotkey(PAUSE_KEY, toggle_pause)" in source)

try:
    import tkinter as tk
    import gui
    import wow_gui
    from routes import fish_tally
except Exception as exc:  # noqa: BLE001 - no tkinter in this Python
    print(f"\nSKIPPED T-406..T-409: wow_gui.py cannot be imported here ({exc}).")
    if UNDER_CI:
        check("T-406..T-409 need tkinter, which CI's Python includes - it is missing", False)
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)

try:
    root = tk.Tk()
except Exception as exc:  # noqa: BLE001 - no display
    print(f"\nSKIPPED T-406..T-409: no display for a window ({exc}).")
    if UNDER_CI:
        check("T-406..T-409 need a display, which CI's Windows runner has - it is missing", False)
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
root.withdraw()

#The stand-in: answers like wow.py --control. Each status line reports what it was told.
STAND_IN = textwrap.dedent("""\
    import json, sys, threading, time
    state = {"paused": False, "minutes": 30.0, "left": 1800.0, "fishing": 0.0, "reason": None}
    if "--auto-pause" in sys.argv:
        state["minutes"] = float(sys.argv[sys.argv.index("--auto-pause") + 1])
        state["left"] = state["minutes"] * 60
    def status():
        print("@status " + json.dumps({"state": "fishing", "paused": state["paused"],
              "pause_reason": state["reason"], "blocked": None, "auto_pause_left": state["left"],
              "auto_pause_minutes": state["minutes"], "fishing_seconds": state["fishing"],
              "reeled": 3, "missed": 1, "by": {}, "caught": 2, "chat": "reading",
              "dry_run": "--dry-run" in sys.argv}), flush=True)
    print("stand-in: started " + " ".join(sys.argv[1:]), flush=True)
    status()
    for line in sys.stdin:
        word = line.strip()
        print("stand-in: got " + word, flush=True)
        if word == "toggle":
            state["paused"] = not state["paused"]
            state["reason"] = "manual" if state["paused"] else None
        elif word.startswith("auto_pause_"):
            state["minutes"] = float(word.split("_")[-1]); state["left"] = state["minutes"] * 60
        elif word == "catch":
            print('@catch {"fish": "Raw Longjaw Mud Snapper", "count": 1, "how": "read", "time": "12:00:01"}', flush=True)
        elif word == "fish":
            state["fishing"] = 1800.0
        elif word == "quit":
            break
        status()
    print("stand-in: quitting", flush=True)
""")
scripts = tempfile.mkdtemp()
stand_in = os.path.join(scripts, "stand_in_wow.py")
Path(stand_in).write_text(STAND_IN, encoding="utf-8")

#The panel's files go to a scratch folder, never the real recordings/wow/.
scratch = Path(tempfile.mkdtemp())
wow_gui.wow.USER_PRICES = scratch / "fish_prices.json"
wow_gui.wow.HISTORY = scratch / "catches.csv"
wow_gui.wow.USER_CHAT_CONFIG = scratch / "chat.json"


class StandInBot(gui.BotProcess):
    """The real BotProcess, but whatever command the panel builds runs the stand-in instead -
    with the panel's own arguments, so the test sees what it asked for."""

    def start(self):
        self.command = [sys.executable, "-u", stand_in] + self.command[3:]
        super().start()


bot = StandInBot(command=["x", "-u", "wow.py", "--control"], cwd=scripts)
panel = wow_gui.FishingPanel(root, bot=bot)
root.update()


def tick_until(condition):
    return wait_for(lambda: (panel._tick() or True) and condition())


def text(widget):
    return str(widget.cget("text"))


print("\nT-406  The fishing panel reads wow.py's @lines - status, catches, the stop reason - and keeps them out of the log")
check("before starting: 'Not running', and the Pause button is off",
      text(panel.state_label) == "Not running" and str(panel.pause_button.cget("state")) == "disabled")
panel.dry_run.set(True)
panel.minutes.set("45")
panel.start_bot()
check("the status line arrives and the header says Fishing", tick_until(lambda: text(panel.state_label) == "Fishing"))
log = panel.log.get("1.0", "end")
check("wow.py was started with --control, --dry-run and the chosen minutes",
      "stand-in: started --control --dry-run --auto-pause 45" in log)
check("@ lines never reach the log", "@status" not in log)
check("a dry run says so in the header", "dry run" in text(panel.detail_label))
bot.send("catch")
check("a @catch line adds the fish to the table", tick_until(lambda: panel.table.exists("Raw Longjaw Mud Snapper")))
check("...and to the latest catches", "Raw Longjaw Mud Snapper" in panel.recent.get(0))
check(f"...at a 12-hour time: wow.py's 12:00:01 is 12:00:01 PM ({panel.recent.get(0)!r})",
      panel.recent.get(0).startswith("12:00:01 PM  "))
check("24-hour times become 12-hour ones: midnight, morning, noon, afternoon",
      [wow_gui.twelve_hour(t) for t in ("00:05:09", "09:30:00", "12:00:00", "16:03:45", "23:59:59")]
      == ["12:05:09 AM", "9:30:00 AM", "12:00:00 PM", "4:03:45 PM", "11:59:59 PM"])
check("anything that is not a time is shown as it came, never a crash",
      [wow_gui.twelve_hour(t) for t in ("?", "", None, "25:00:00", "4:03")]
      == ["?", "", "None", "25:00:00", "4:03"])
panel.handle_line('@stopped {"reason": "pressed the key 3 times and the fishing bar never appeared"}')
check("a stop reason is kept to show once it has exited", "fishing bar never appeared" in panel.stopped_reason)
panel.handle_line("@status {not json")
check("a damaged @ line is ignored, never a crash", panel.status is not None)


print("\nT-407  The auto-pause countdown is shown, warns near the end, and says when it paused itself")
check(f"the countdown shows what wow.py reports: the 45 min it was started with ({text(panel.timer_label)})",
      tick_until(lambda: text(panel.timer_label) == "45:00"))
panel.minutes.set("15")
panel.set_minutes()
check("choosing 15 minutes reaches wow.py, and the countdown follows",
      tick_until(lambda: text(panel.timer_label) == "15:00"))
status = dict(panel.status, auto_pause_left=42.0)
panel.handle_line("@status " + json.dumps(status))
panel._refresh()
check("under a minute left: shown in red", text(panel.timer_label) == "0:42"
      and str(panel.timer_label.cget("foreground")) == wow_gui.RED)
panel.handle_line("@status " + json.dumps(dict(status, paused=True, pause_reason="auto")))
panel._refresh()
check("when it paused itself the header says Auto-paused, and the button offers Resume",
      text(panel.state_label) == "Auto-paused" and text(panel.pause_button).startswith("Resume"))


print("\nT-408  Session numbers: fishing time across restarts, catch rate, earnings in gold/silver/copper, unpriced flagged")
panel.reset_session(confirm=False)
bot.send("fish")                       #the stand-in reports 30 minutes of fishing
tick_until(lambda: panel.fishing.total == 1800.0)
for name in ("Raw Longjaw Mud Snapper", "Raw Longjaw Mud Snapper", "19 Pound Catfish", "Gaffer Jack"):
    panel.add_catch(name, 1, "read", "12:00:00")
panel._refresh()
values = {key: text(label) for key, label in panel.session_values.items()}
check(f"4 caught in 30 min of fishing is 8 an hour ({values['rate']})",
      values["caught"].startswith("4") and values["rate"] == "8 an hour")
copper, unpriced = panel.tally.value(panel.catalog)
check(f"worth 2 x 20c + 19s = 19s 40c, Gaffer Jack has no price ({fish_tally.format_money(copper)}, {unpriced})",
      copper == 1940 and unpriced == 1)
shown = "".join(text(w) for w in panel.earned.winfo_children() if w.winfo_manager())
check(f"the earnings read like the game's money ({shown!r})", shown.replace(" ", "") == "19s40c")
check("the fish with no price is flagged in red, with what to do",
      "unpriced" in panel.table.item("Gaffer Jack", "tags") and "no price" in text(panel.money_note))
panel.catalog.set_price("Gaffer Jack", "1s")
panel._refresh()
check("setting its price takes it into the sums at once",
      panel.tally.value(panel.catalog) == (2040, 0) and text(panel.money_note) == "")
first_run = panel.fishing.total
panel.stop_bot()
check("Stop ends wow.py", tick_until(lambda: not bot.running()))
panel.start_bot()
tick_until(lambda: bot.running() and panel.status is not None)
check("restarting wow.py keeps the session's fishing time (it starts its own count at 0)",
      tick_until(lambda: panel.fishing.total == first_run))


print("\nT-409  Pause sends the same toggle as F5; the header follows wow.py's answer")
panel.toggle_pause()
check("Pause: wow.py receives 'toggle' and the header says Paused",
      tick_until(lambda: text(panel.state_label) == "Paused"))
check("...and the countdown shows the full time it will restart from (15 min, chosen before the restart)",
      text(panel.timer_label) == "15:00")
panel.toggle_pause()
check("Resume: back to Fishing", tick_until(lambda: text(panel.state_label) == "Fishing"))
panel.stop_bot()
check("Stop", tick_until(lambda: not bot.running()))
root.update()
check("...and the header says Not running", text(panel.state_label) in ("Not running", "Stopped"))


print("\nT-411  The success rate is shown, and the band typed in Setup is what wow.py is started with")
check("nothing tried yet: '-', not 0% or 100%", wow_gui.success_text(0, 0) == "-")
check(f"4 reeled, 1 missed: {wow_gui.success_text(4, 1)!r}", wow_gui.success_text(4, 1) == "80%   (4 of 5)")
check("with wow.py's band in its status line, the band is shown too",
      wow_gui.success_text(23, 2, {"success_band": [90, 99]}) == "92%   (23 of 25)   kept in 90-99%")
check("a band ending at 100 (off) is not shown as one",
      "kept" not in wow_gui.success_text(23, 2, {"success_band": [100, 100]}))
check(f"the Setup tab starts at wow.py's own band ({panel.band_low.get()}-{panel.band_high.get()})",
      (int(panel.band_low.get()), int(panel.band_high.get())) == wow_gui.wow.SUCCESS_RATE_PERCENT)
panel.band_low.set("85")
panel.band_high.set("95")
command = panel._command(False, "30", panel.success_band())
check(f"85-95 typed: wow.py is started with --success-rate 85-95 ({command[-2:]})",
      command[-2:] == ["--success-rate", "85-95"])
panel.band_low.set("97")
check("a band that is not one (97-95): wow.py keeps its default, and the log says why",
      panel.success_band() is None and "is not one" in panel.log.get("1.0", "end"))
check("the session card has a Success rate row", "success" in panel.session_values)
root.destroy()

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
