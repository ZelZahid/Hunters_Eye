"""Tests for the control panel (gui.py) and the two pieces under it. No game required.

WHY THIS EXISTS: the control panel starts and stops a program that presses keys into a live game,
and rewrites the one file a person hand-edits. Its dangerous failures are quiet ones:
  - a Save that loses the file's comments, or touches a line it was not asked to (the comments
    are where a person learns what each number does);
  - a Save that writes a value the bot then silently replaces with a default;
  - a Stop that does not stop, or a closed panel that leaves an invisible bot running;
  - a bot that freezes because the panel stopped reading its output.
Each section below is one of those.

T-401 and C-119 need nothing. T-402 and T-403 need tkinter (gui.py imports it); T-403 also needs a
display. Either one skipping is a FAILURE under CI (the CI environment variable), where both are
expected to run - a check that quietly skips on the machine meant to run it looks like a pass.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io
import os
import tempfile
import textwrap
import threading
import time

from core import command_channel, user_config

failures = 0
UNDER_CI = bool(os.environ.get("CI"))
REPO_ROOT = Path(__file__).resolve().parent.parent


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


print("\nT-401  The command channel: one word per line, unknown words ignored, errors survived")
print("   (a RuntimeError traceback below is expected - it is the handler that fails on purpose)")
calls, logged, closed = [], [], threading.Event()


def boom():
    raise RuntimeError("handler failed")


thread = command_channel.listen(
    {"Pindle": lambda: calls.append("pindle"), "stop": lambda: calls.append("stop"), "boom": boom},
    stream=io.StringIO("pindle\n\n  STOP  \nfly\nboom\nstop\n"),
    on_close=closed.set, log=logged.append)
thread.join(timeout=5)
check("commands run in order, case and spaces ignored", calls == ["pindle", "stop", "stop"])
check("an unknown command is reported, not guessed at",
      any("unknown command 'fly'" in line for line in logged))
check("a handler that raises is reported and the next command still runs",
      any("'boom' stopped" in line for line in logged) and calls[-1] == "stop")
check("end of input is reported to on_close", closed.is_set())


print("\nC-119  Editing user_config.txt keeps everything it was not asked to change")
SAMPLE = textwrap.dedent("""\
    # Header comment - must survive.
    [settings]

    # Turn it off.
    enabled = yes
    # Seconds.
    min_gap = 0.6
    game_name =

    # ------------------------------------------------------------
    # Rules. Each rule takes:
    #   meter  - which meter to watch. Must be a name from
    #            meters.json.
    #   below  - drink at or below this.

    # The emergency rule.
    [health]
    meter = health
    below = 30%
    keys = 2, 3
    cooldown = 1.0
    """)
sections = user_config.parse_sections(SAMPLE)
check("sections in file order", [s.name for s in sections] == ["settings", "health"])
settings = {e.key: e for e in sections[0].entries}
check("values are the raw text", settings["min_gap"].value == "0.6" and settings["game_name"].value == "")
check("help is the comment directly above", settings["enabled"].help == "Turn it off.")
health = {e.key: e for e in sections[1].entries}
check("a section's help is the comment above its header", sections[1].help == "The emergency rule.")
check("a key documented in a definition list gets that text, continuation lines included",
      health["meter"].help == "which meter to watch. Must be a name from meters.json.")
check("percent values stay as written", health["below"].value == "30%")

edited = user_config.apply_values(SAMPLE, {("settings", "game_name"): "zel01",
                                           ("health", "below"): "25%"})
changed = [(a, b) for a, b in zip(SAMPLE.splitlines(), edited.splitlines()) if a != b]
check("exactly the two asked-for lines changed",
      changed == [("game_name =", "game_name = zel01"), ("below = 30%", "below = 25%")])
check("line count unchanged", len(SAMPLE.splitlines()) == len(edited.splitlines()))
check("clearing a value again restores the original text exactly",
      user_config.apply_values(edited, {("settings", "game_name"): "",
                                        ("health", "below"): "30%"}) == SAMPLE)
check("Windows line endings are kept",
      "\r\n" in user_config.apply_values(SAMPLE.replace("\n", "\r\n"),
                                         {("settings", "min_gap"): "1"}).split("min_gap = 1")[1][:2])
try:
    user_config.apply_values(SAMPLE, {("settings", "no_such_key"): "1"})
    check("a key that is not in the file is an error, not silently appended", False)
except KeyError:
    check("a key that is not in the file is an error, not silently appended", True)
try:
    user_config.apply_values(SAMPLE, {("settings", "game_name"): "a\n[evil]"})
    check("a value with a line break is refused", False)
except ValueError:
    check("a value with a line break is refused", True)

check("a clean file has no warnings", user_config.check(SAMPLE, known_meters={"health"}) == [])
warnings = user_config.check(user_config.apply_values(SAMPLE, {("health", "below"): "250%"}))
check("an out-of-range value is reported exactly as load() would",
      len(warnings) == 1 and "below" in warnings[0] and "outside" in warnings[0])
check("an unknown meter is reported", user_config.check(SAMPLE, known_meters={"mana"}) != [])

real = (REPO_ROOT / "user_config.txt").read_text(encoding="utf-8")
real_entries = [e for s in user_config.parse_sections(real) for e in s.entries]
check("the real user_config.txt: every setting is found", len(real_entries) >= 20)
check("the real user_config.txt: every setting has help text",
      all(e.help for e in real_entries))
check("the real user_config.txt: writing every value back unchanged changes nothing",
      user_config.apply_values(real, {(e.section, e.key): e.value for e in real_entries}) == real)

folder = tempfile.mkdtemp()
target = os.path.join(folder, "user_config.txt")
user_config.save_text(target, edited)
check("save_text writes the text exactly", Path(target).read_text(encoding="utf-8") == edited)
check("save_text leaves no temp file behind", os.listdir(folder) == ["user_config.txt"])


try:
    import gui
except ImportError as exc:  #tkinter is part of the standard installer on Windows and macOS
    gui = None
    print(f"\nSKIPPED T-402, T-403: gui.py cannot be imported here ({exc}).")
    if UNDER_CI:
        check("T-402/T-403 need tkinter, which CI's Python includes - it is missing", False)

if gui is not None:
    print("\nT-402  Starting, commanding, reading and stopping the bot - against a stand-in")
    STAND_IN = textwrap.dedent("""\
        import sys, time
        print("Starting stand-in...")
        print("FPS: 42.50")
        for line in sys.stdin:
            word = line.strip()
            if word == "quit":
                break
            if word == "flood":
                for i in range(5000):
                    print("flood", i)
            print("got", word)
        print("stand-in: quitting")
        """)
    STUBBORN = "import time\nprint('ignoring you', flush=True)\nwhile True: time.sleep(1)\n"
    scripts = tempfile.mkdtemp()
    stand_in = os.path.join(scripts, "stand_in.py")
    stubborn = os.path.join(scripts, "stubborn.py")
    Path(stand_in).write_text(STAND_IN, encoding="utf-8")
    Path(stubborn).write_text(STUBBORN, encoding="utf-8")

    def drain(bot):
        lines = []
        while not bot.output.empty():
            lines.append(bot.output.get_nowait())
        return lines

    bot = gui.BotProcess(command=[sys.executable, "-u", stand_in], cwd=scripts)
    check("not running before start", not bot.running())
    check("a command to a stopped bot reports that it was not sent", bot.send("pindle") is False)
    bot.start()
    check("running after start", bot.running())
    check("a command is delivered", bot.send("pindle"))
    seen = []
    check("its output arrives line by line",
          wait_for(lambda: seen.extend(drain(bot)) or "got pindle" in seen))
    check("output order is kept", seen[:2] == ["Starting stand-in...", "FPS: 42.50"])

    #The real failure here would be a FROZEN BOT, not a slow panel: nothing reads for a while,
    #and the child must still get to finish its 5000 prints.
    bot.send("flood")
    bot.send("after")
    check("the child is never blocked by a panel that is not reading",
          wait_for(lambda: bot.output.qsize() >= 5000, timeout=10))
    check("no output dropped while the queue had room", bot.dropped == 0)
    drain(bot)

    bot.stop()
    check("stopping, then stopped, without the panel ever blocking",
          wait_for(lambda: (bot.poll() or True) and not bot.running()))
    check("clean exit code", bot.exit_code == 0)
    check("it quit because it was asked", "stand-in: quitting" in drain(bot))

    old_grace = gui.STOP_GRACE_SECONDS
    gui.STOP_GRACE_SECONDS = 0.3
    try:
        stuck = gui.BotProcess(command=[sys.executable, "-u", stubborn], cwd=scripts)
        stuck.start()
        wait_for(lambda: not stuck.output.empty())
        stuck.stop()
        check("a bot that ignores Stop is ended after the grace period",
              wait_for(lambda: (stuck.poll() or True) and not stuck.running(), timeout=6))
        check("...and the log says so",
              any("did not stop in time" in line for line in drain(stuck)))
    finally:
        gui.STOP_GRACE_SECONDS = old_grace

    killed = gui.BotProcess(command=[sys.executable, "-u", stand_in], cwd=scripts)
    killed.start()
    killed.kill_now()
    check("closing the window ends the bot before it returns", not killed.running())

    print("\nT-403  The control panel window: settings form, Save, buttons, status")
    check("help text: the file's hard wraps are joined back into paragraphs",
          gui.reflow("one\ntwo\n\nthree") == "one two\n\nthree")
    check("help text: a list item's wrapped lines join; the next item starts a new line",
          gui.reflow("Two:\n  a  first\n     more\n  b  second") == "Two:\n  a  first more\n  b  second")
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - no display
        root = None
        print(f"  SKIPPED: no display for a window ({exc}).")
        if UNDER_CI:
            check("T-403 needs a display, which CI's Windows runner has - it is missing", False)

    if root is not None:
        root.withdraw()
        config_copy = os.path.join(tempfile.mkdtemp(), "user_config.txt")
        Path(config_copy).write_text(real, encoding="utf-8")
        gui.CONFIG_PATH = Path(config_copy)
        quiet_bot = gui.BotProcess(command=[sys.executable, "-u", stand_in], cwd=scripts)
        panel = gui.ControlPanel(root, bot=quiet_bot)
        root.update()
        check("one field per setting in the file", len(panel.fields) == len(real_entries))
        check("yes/no settings are checkboxes",
              isinstance(panel.fields[("settings", "enabled")][0], tk.BooleanVar))
        check("action buttons are disabled while the bot is stopped",
              all(str(b.cget("state")) == "disabled" for b in panel.action_buttons))
        check("nothing to save before an edit", panel._changes() == {})

        panel.fields[("settings", "enabled")][0].set(False)
        panel.fields[("mana", "below")][0].set("25%")
        check("edits are collected, booleans in the file's own words",
              panel._changes() == {("settings", "enabled"): "no", ("mana", "below"): "25%"})
        panel.save_settings()
        saved = Path(config_copy).read_text(encoding="utf-8")
        diff = [(a, b) for a, b in zip(real.splitlines(), saved.splitlines()) if a != b]
        check("Save wrote exactly those two lines",
              diff == [("enabled = yes", "enabled = no"), ("below = 20%", "below = 25%")])
        check("the form reloads from what was saved", panel._changes() == {}
              and panel.fields[("settings", "enabled")][1] == "no")

        panel.start_bot()
        root.update()
        check("starting enables the action buttons",
              all(str(b.cget("state")) == "normal" for b in panel.action_buttons))
        panel.send("pindle", "Pindle run")
        check("a button's command reaches the bot and its reply reaches the log",
              wait_for(lambda: (panel._tick() or True) and "got pindle" in panel.log.get("1.0", "end")))
        check("the FPS line is picked out of the log", panel.last_fps == 42.5)
        panel.stop_bot()
        check("Stop ends the bot",
              wait_for(lambda: (panel._tick() or True) and not quiet_bot.running()))
        root.update()
        check("...and the header says so", panel.state_label.cget("text") == "Stopped")
        root.destroy()


print()
if failures:
    print(f"{failures} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
