'''
Hunter's Eye - control panel.

    python gui.py

A window for running the bot without a console: start and stop it, the hotkey actions as buttons,
user_config.txt as a form, and the console output as a live log. Every hotkey still works exactly
as before while it runs - this is a second way in, not a replacement. `python main.py` on its own
is unchanged.

WHY THE BOT RUNS AS A SEPARATE PROCESS rather than inside this window:
  - Stability. A crash in the window cannot take the bot down mid-fight, and a crash in the bot
    leaves the window up with the error in its log - which is exactly when you want to read it.
  - Tk. The overlay is a Tk window that must own its process's main thread (see run_overlay in
    main.py). Two Tk event loops in one process is a fight; two processes each own one, cleanly.
  - Nothing in main.py had to be restructured. It already prints everything worth knowing; this
    window reads that, and sends commands down main.py's --control channel
    (core/command_channel.py) - one word per line, each calling exactly what its hotkey calls.

WHAT IT CANNOT DO, by design: change a setting in a running bot. main.py reads user_config.txt
once at start (CLAUDE.md, "Running"), so Save says so and offers Restart.

Plain tkinter only, so there is nothing extra to install. On Windows run it with `pythonw gui.py`
for no console window at all.
'''
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

#Run directly, so the repo root has to be importable for core - same bootstrap as tools/.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import user_config

MAIN_SCRIPT = ROOT / "main.py"
CONFIG_PATH = ROOT / "user_config.txt"
METERS_PATH = ROOT / "assets" / "meters.json"

POLL_MS = 100                 #how often the window drains the bot's output and checks on it
LOG_MAX_LINES = 5000          #older lines are dropped - a session left running all day must not
                              #grow this window's memory without bound
OUTPUT_QUEUE_MAX = 20000      #lines buffered between the reader thread and the window
STOP_GRACE_SECONDS = 5.0      #after asking the bot to quit, before ending it forcibly
KILL_GRACE_SECONDS = 2.0      #after ending it, before killing it outright

#Lines worth surfacing outside the log. Matched against main.py's own prints, so if one of those
#is reworded, the status line just stops updating - the log still shows everything.
FPS_LINE = re.compile(r"^FPS:\s*([\d.]+)")
ACTIVITY_LINE = re.compile(r"^(next_game|quit_game|pindle|walk|fight|control)\b.*:")
PROBLEM_LINE = re.compile(r"WARNING|Traceback|Error|stopped by the error", re.IGNORECASE)

BOOLEAN_WORDS = {"yes": True, "no": False, "true": True, "false": False, "on": True, "off": False}


def _python_for_child():
    """The interpreter to run main.py with.

    The same one running this window - so the same installed packages - except that pythonw.exe
    (what you'd double-click this with on Windows) is swapped for its python.exe sibling. The
    child's output is piped here either way, so no console appears; python.exe just behaves
    exactly like the `python main.py` everything has been tested with.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


class BotProcess:
    """main.py as a child process: start it, send it commands, read what it prints, stop it.

    Knows nothing about Tk - the window polls it - which is what lets the tests drive it with a
    stand-in script instead of the real bot.

    THE OUTPUT READER MUST NEVER BLOCK. If it did, the pipe would fill, the bot's next print()
    would block, and the bot would freeze mid-fight because a window was slow to repaint. So lines
    go into a bounded queue with put_nowait, and if the window ever falls that far behind, lines
    are dropped and counted rather than waited for.
    """

    def __init__(self, command=None, cwd=ROOT):
        self.command = command or [_python_for_child(), "-u", str(MAIN_SCRIPT), "--control"]
        self.cwd = str(cwd)
        self.process = None
        self.output = queue.Queue(maxsize=OUTPUT_QUEUE_MAX)
        self.dropped = 0
        self._stop_asked_at = None
        self._terminated_at = None
        self.exit_code = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running():
            return
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW  #its output comes here; a console would be empty
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=env, creationflags=flags,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._stop_asked_at = None
        self._terminated_at = None
        self.exit_code = None
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            try:
                self.output.put_nowait(line.rstrip("\r\n"))
            except queue.Full:
                self.dropped += 1
        process.stdout.close()

    def send(self, command):
        """Writes one command line. False if the bot is not there to receive it."""
        if not self.running():
            return False
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def stop(self):
        """Asks the bot to quit; poll() finishes the job. Never blocks the caller."""
        if not self.running() or self._stop_asked_at is not None:
            return
        self.send("quit")
        try:
            #End of input means "the panel went away" to main.py, which also quits - a second,
            #independent way for the request to land.
            self.process.stdin.close()
        except OSError:
            pass
        self._stop_asked_at = time.monotonic()

    def stopping(self):
        return self.running() and self._stop_asked_at is not None

    def poll(self):
        """Call regularly. Escalates a stop that is not being honoured; records the exit code."""
        if self.process is None:
            return
        code = self.process.poll()
        if code is not None:
            self.exit_code = code
            return
        if self._stop_asked_at is None:
            return
        now = time.monotonic()
        if self._terminated_at is None and now - self._stop_asked_at > STOP_GRACE_SECONDS:
            #The threads are daemons with no shutdown signal of their own (todolist: "Clean
            #shutdown"), so a run in the middle of a long wait may not reach the quit check soon.
            try:
                self.output.put_nowait("control panel: the bot did not stop in time - ending it.")
            except queue.Full:
                pass
            self.process.terminate()
            self._terminated_at = now
        elif self._terminated_at is not None and now - self._terminated_at > KILL_GRACE_SECONDS:
            self.process.kill()

    def kill_now(self):
        """For closing the window: end the bot without waiting on the event loop."""
        if not self.running():
            return
        self.stop()
        try:
            self.process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()


def reflow(text):
    """Comment text re-wrapped for a resizable box: the file's hard line breaks are joined back
    into paragraphs, while blank lines and indented lines (lists, aligned examples) are kept."""
    paragraphs, current, item_indent = [], [], None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                current, item_indent = [], None
            paragraphs.append("")
        elif item_indent is not None and indent > item_indent:
            current.append(line.strip())  #a list item's wrapped continuation
        elif indent or line.lstrip().startswith(("-", "*")):
            if current:
                paragraphs.append(" ".join(current))
            current, item_indent = [line.rstrip()], indent
        else:
            if item_indent is not None:
                paragraphs.append(" ".join(current))
                current, item_indent = [], None
            current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs).strip()


def _known_meters():
    """Meter names from assets/meters.json, so Settings can say when a rule names one that does not
    exist - the same check main.py makes. None (skip that check) if they cannot be read."""
    try:
        import io
        import contextlib
        from core import game_state
        with contextlib.redirect_stdout(io.StringIO()):  #its "no meter config" warning is main.py's to give
            profiles = game_state.load_profiles(METERS_PATH)
    except Exception:  # noqa: BLE001 - a missing optional check is not worth failing over
        return None
    return {m.name for m in profiles[0].meters} if profiles else None


#Each button sends one word down main.py's --control channel. (label, command, hotkey, what it does)
#In hotkey order, left to right, so the row reads like the keyboard. The hotkey shown on each is the
#one main.py registers for that same function - card T-404 checks it against main.py's source, so a
#rebind there cannot leave a wrong label here.
ACTIONS = (
    ("Next game", "next_game", "F3", "Leave this game and create the next one."),
    ("Stop run", "stop", "F4", "Stop a run, and snooze auto-collect for a few seconds."),
    ("Debug panel", "panel", "F5", "Show or hide the game-state panel over the game."),
    ("Snooze potions", "snooze_potions", "F6", "No potion drinking for 10s. Each press adds 10s more."),
    ("Pindle run", "pindle", "F7", "The Pindleskin run, from a fresh game in Harrogath."),
)

#main.py prints this on every potion snooze, from the button or from F6 alike, with the TOTAL time
#left - so the countdown on the button follows the hotkey too, not just clicks.
POTION_SNOOZE_LINE = re.compile(r"^Potion drinking snoozed for (\d+)s")
SNOOZED_COLOR = "#c0001a"


def _log_font():
    """A monospace font that is easy to read on each OS. Courier, the old choice, is thin and pale
    on Windows; these are each platform's own coding font. Tk quietly substitutes a default for a
    family that is not installed, so a wrong guess costs looks, never a crash."""
    if sys.platform == "win32":
        return ("Consolas", 10)
    if sys.platform == "darwin":
        return ("Menlo", 12)
    return ("DejaVu Sans Mono", 10)


class ControlPanel:
    def __init__(self, root, bot=None):
        self.root = root
        self.bot = bot or BotProcess()
        self.known_meters = _known_meters()
        self.last_fps = None
        self.last_activity = ""
        self.potion_snooze_until = 0.0  #time.monotonic() deadline read from the bot's own log line
        self.restart_pending = False
        self.config_text = ""          #the file as last loaded/saved, to spot edits made elsewhere
        self.fields = {}               #(section, key) -> (tk variable, original value)

        root.title("Hunter's Eye - Control Panel")
        root.geometry("980x680")
        root.minsize(720, 480)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        notebook.add(self._build_run_tab(notebook), text="Run")
        notebook.add(self._build_settings_tab(notebook), text="Settings")

        self.load_settings()
        self._refresh_state()
        self.root.after(POLL_MS, self._tick)

    # --- header: the bot's state, always visible ------------------------------------------
    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.state_dot = tk.Label(bar, text="●", font=("TkDefaultFont", 16))
        self.state_dot.pack(side="left")
        self.state_label = ttk.Label(bar, text="", font=("TkDefaultFont", 11, "bold"))
        self.state_label.pack(side="left", padx=(4, 16))
        self.fps_label = ttk.Label(bar, text="")
        self.fps_label.pack(side="left")
        self.restart_button = ttk.Button(bar, text="Restart", command=self.restart_bot)
        self.restart_button.pack(side="right")
        self.stop_button = ttk.Button(bar, text="Stop bot", command=self.stop_bot)
        self.stop_button.pack(side="right", padx=4)
        self.start_button = ttk.Button(bar, text="Start bot", command=self.start_bot)
        self.start_button.pack(side="right")

    # --- Run tab: hotkey buttons + live log ------------------------------------------------
    def _build_run_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)

        actions = ttk.LabelFrame(tab, text="Actions", padding=8)
        actions.pack(fill="x")
        self.action_buttons = []
        self.buttons = {}  #command -> (button, its normal text)
        ttk.Style(self.root).configure("Snoozed.TButton", foreground=SNOOZED_COLOR)
        for column, (label, command, hotkey, tip) in enumerate(ACTIONS):
            cell = ttk.Frame(actions)
            cell.grid(row=0, column=column, sticky="nsew", padx=4)
            actions.columnconfigure(column, weight=1)
            button = ttk.Button(cell, text=f"{label}  ({hotkey})",
                                command=lambda c=command, l=label: self.send(c, l))
            button.pack(fill="x")
            ttk.Label(cell, text=tip, wraplength=160, foreground="gray40").pack(fill="x", pady=(2, 0))
            self.action_buttons.append(button)
            self.buttons[command] = (button, f"{label}  ({hotkey})")
        ttk.Label(actions, foreground="gray25", wraplength=880, text=(
            "Next game and Pindle run start once you click back into the game: the bot never acts "
            "while another window is in front. The hotkeys keep working as usual."),
        ).grid(row=1, column=0, columnspan=len(ACTIONS), sticky="w", pady=(8, 0))

        self.activity_label = ttk.Label(tab, text="", foreground="gray25")
        self.activity_label.pack(fill="x", pady=(8, 2))

        log_frame = ttk.LabelFrame(tab, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True)
        self.log = ScrolledText(log_frame, height=10, wrap="none", state="disabled",
                                font=_log_font())
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("problem", foreground="#b00020")
        self.log.tag_configure("panel", foreground="#1a5fb4")

        controls = ttk.Frame(log_frame)
        controls.pack(fill="x", pady=(4, 0))
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Follow newest", variable=self.follow).pack(side="left")
        ttk.Button(controls, text="Save log...", command=self.save_log).pack(side="right")
        ttk.Button(controls, text="Clear", command=self.clear_log).pack(side="right", padx=4)
        return tab

    # --- Settings tab: user_config.txt as a form --------------------------------------------
    def _build_settings_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", side="bottom", pady=(8, 0))
        ttk.Button(buttons, text="Save", command=self.save_settings).pack(side="right")
        ttk.Button(buttons, text="Undo changes", command=self.load_settings).pack(side="right", padx=4)
        ttk.Button(buttons, text="Open the file", command=self.open_config_file).pack(side="left")
        self.settings_status = ttk.Label(buttons, text="", foreground="gray25")
        self.settings_status.pack(side="left", padx=8)

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)

        #A scrollable column of fields: Tk has no scrolling frame, so a frame inside a canvas.
        form_outer = ttk.Frame(panes)
        canvas = tk.Canvas(form_outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(form_outer, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._scroll_target = canvas
        canvas.bind_all("<MouseWheel>", self._on_wheel)                      #Windows / macOS
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))  #X11
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        panes.add(form_outer, weight=3)

        help_frame = ttk.LabelFrame(panes, text="What this does", padding=6)
        self.help_text = tk.Text(help_frame, wrap="word", width=40, relief="flat",
                                 font="TkDefaultFont", background=self.root.cget("background"),
                                 state="disabled")
        self.help_text.pack(fill="both", expand=True)
        panes.add(help_frame, weight=2)
        return tab

    def _on_wheel(self, event):
        #Only scroll the form when the pointer is over it - the log has its own scrolling.
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self._scroll_target:
            widget = widget.master
        if widget is self._scroll_target:
            #Windows reports wheel notches as multiples of 120; macOS reports small raw deltas.
            notches = event.delta / 120 if sys.platform == "win32" else event.delta
            self._scroll_target.yview_scroll(int(-notches * 3) or (-1 if event.delta > 0 else 1),
                                             "units")

    def _show_help(self, title, text):
        self.help_text.configure(state="normal")
        self.help_text.delete("1.0", "end")
        self.help_text.insert("end", title + "\n\n", ("title",))
        self.help_text.insert("end", reflow(text) or "No description in the file for this one.")
        self.help_text.tag_configure("title", font=("TkDefaultFont", 10, "bold"))
        self.help_text.configure(state="disabled")

    def load_settings(self):
        """(Re)reads user_config.txt and rebuilds the form - also what "Undo changes" does."""
        for child in self.form.winfo_children():
            child.destroy()
        self.fields.clear()
        try:
            self.config_text = CONFIG_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            self.config_text = ""
            ttk.Label(self.form, text=f"Could not read {CONFIG_PATH.name}: {exc}\n"
                                      f"The bot uses its built-in defaults without it.",
                      foreground="#b00020").pack(anchor="w")
            return

        for section in user_config.parse_sections(self.config_text):
            title = ("General settings" if section.name == user_config.SETTINGS_SECTION
                     else f"Potion rule: {section.name}")
            group = ttk.LabelFrame(self.form, text=title, padding=6)
            group.pack(fill="x", padx=4, pady=4)
            group.columnconfigure(0, minsize=170)  #same label width in every group, so they line up
            group.columnconfigure(1, weight=1)
            if section.help:
                about = ttk.Label(group, text="What is this rule?", foreground="#1a5fb4",
                                  cursor="hand2")
                about.grid(row=0, column=0, columnspan=2, sticky="w")
                about.bind("<Button-1>", lambda e, s=section: self._show_help(f"[{s.name}]", s.help))
            for row, entry in enumerate(section.entries, start=1):
                self._add_field(group, row, entry)
        self._set_settings_status("")
        self._show_help("Settings", "Click a setting to see what it does.\n\n"
                        "Changes are written to user_config.txt when you press Save. The bot "
                        "reads that file when it starts, so a running bot needs a restart to "
                        "use them.")

    def _add_field(self, group, row, entry):
        label = ttk.Label(group, text=entry.key.replace("_", " "))
        label.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=2)
        show = lambda e=None, en=entry: self._show_help(f"{en.key}  [{en.section}]", en.help)
        label.bind("<Button-1>", show)

        boolean = BOOLEAN_WORDS.get(entry.value.strip().lower())
        if boolean is not None:
            variable = tk.BooleanVar(value=boolean)
            widget = ttk.Checkbutton(group, variable=variable, command=show)
        else:
            variable = tk.StringVar(value=entry.value)
            widget = ttk.Entry(group, textvariable=variable)
            widget.bind("<FocusIn>", show)
        widget.grid(row=row, column=1, sticky="we", pady=2)
        variable.trace_add("write", lambda *a: self._set_settings_status("Unsaved changes."))
        self.fields[(entry.section, entry.key)] = (variable, entry.value)

    def _changes(self):
        changes = {}
        for where, (variable, original) in self.fields.items():
            value = variable.get()
            if isinstance(value, bool):
                word = original.strip().lower()
                if BOOLEAN_WORDS[word] == value:
                    continue
                #Keep the file's own vocabulary: a yes/no stays yes/no, a true/false true/false.
                pair = next(p for p in (("yes", "no"), ("true", "false"), ("on", "off")) if word in p)
                changes[where] = pair[0] if value else pair[1]
            elif value.strip() != original.strip():
                changes[where] = value.strip()
        return changes

    def save_settings(self):
        changes = self._changes()
        if not changes:
            self._set_settings_status("Nothing changed.")
            return
        try:
            on_disk = CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            on_disk = ""
        if on_disk != self.config_text:
            #Edited by hand (or by another copy of this window) since it was loaded here. Saving
            #now would quietly throw that edit away - last write wins - so ask instead.
            if not messagebox.askyesno("user_config.txt changed",
                                       "user_config.txt was changed outside this window since it "
                                       "was opened here.\n\nSave anyway? Your changes here will be "
                                       "written on top of the file as it is now.", icon="warning"):
                return
        try:
            new_text = user_config.apply_values(on_disk or self.config_text, changes)
        except (KeyError, ValueError) as exc:
            messagebox.showerror("Cannot save", f"{exc}\n\nPress 'Undo changes' to reload the file.")
            return

        warnings = user_config.check(new_text, known_meters=self.known_meters)
        if warnings:
            listed = "\n".join(w.replace("WARNING: user_config ", "") for w in warnings)
            if not messagebox.askyesno("Some values will not be used",
                                       "The bot would not use these values as written - it would "
                                       f"fall back to its defaults instead:\n\n{listed}\n\n"
                                       "Save anyway?", icon="warning", default="no"):
                return
        try:
            user_config.save_text(CONFIG_PATH, new_text)
        except OSError as exc:
            messagebox.showerror("Cannot save", f"Could not write {CONFIG_PATH}:\n{exc}")
            return
        self.load_settings()
        if self.bot.running():
            self._set_settings_status("Saved. Restart the bot to use the new settings.")
        else:
            self._set_settings_status("Saved.")

    def _set_settings_status(self, text):
        self.settings_status.configure(text=text)

    def open_config_file(self):
        try:
            if sys.platform == "win32":
                os.startfile(CONFIG_PATH)  # noqa: S606 - opens in the user's own editor
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(CONFIG_PATH)])
            else:
                subprocess.Popen(["xdg-open", str(CONFIG_PATH)])
        except OSError as exc:
            messagebox.showerror("Cannot open", str(exc))

    # --- the bot -----------------------------------------------------------------------------
    def start_bot(self):
        if self.bot.running():
            return
        self.last_fps = None
        self._append(f"control panel: starting the bot ({MAIN_SCRIPT.name} --control)...", "panel")
        try:
            self.bot.start()
        except OSError as exc:
            self._append(f"control panel: could not start the bot: {exc}", "problem")
        self._refresh_state()

    def stop_bot(self):
        if self.bot.running():
            self._append("control panel: stopping the bot...", "panel")
            self.bot.stop()
        self._refresh_state()

    def restart_bot(self):
        if self.bot.running():
            self.restart_pending = True
            self.stop_bot()
        else:
            self.start_bot()

    def send(self, command, label):
        if not self.bot.send(command):
            self._append(f"control panel: '{label}' not sent - the bot is not running.", "problem")
            return
        self._append(f"control panel: {label}.", "panel")

    def _tick(self):
        self.bot.poll()
        self._drain_output()
        if self.restart_pending and not self.bot.running():
            self.restart_pending = False
            self.start_bot()
        self._refresh_state()
        self.root.after(POLL_MS, self._tick)

    def _drain_output(self):
        lines = []
        try:
            while len(lines) < 500:  #a burst is shown over a few ticks rather than freezing one
                lines.append(self.bot.output.get_nowait())
        except queue.Empty:
            pass
        for line in lines:
            fps = FPS_LINE.match(line)
            if fps:
                self.last_fps = float(fps.group(1))
            if ACTIVITY_LINE.match(line):
                self.last_activity = line
            snooze = POTION_SNOOZE_LINE.match(line)
            if snooze:
                self.potion_snooze_until = time.monotonic() + int(snooze.group(1))
            self._append(line, "problem" if PROBLEM_LINE.search(line) else None)
        if self.bot.dropped:
            self._append(f"control panel: {self.bot.dropped} log lines were dropped because this "
                         "window fell behind.", "problem")
            self.bot.dropped = 0

    def _append(self, line, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        if self.follow.get():
            self.log.see("end")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialdir=str(ROOT),
                                            initialfile=time.strftime("hunters_eye_%Y%m%d_%H%M%S.txt"),
                                            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if path:
            try:
                Path(path).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Cannot save", str(exc))

    def _refresh_state(self):
        if self.bot.stopping():
            text, color = "Stopping...", "#c88a00"
        elif self.bot.running():
            text, color = "Running", "#2e8b3a"
        elif self.bot.exit_code not in (None, 0):
            text, color = f"Stopped - exited with an error (code {self.bot.exit_code}), see the log", "#b00020"
        else:
            text, color = "Stopped", "gray50"
        self.state_label.configure(text=text)
        self.state_dot.configure(foreground=color)
        running = self.bot.running()
        self.fps_label.configure(text=f"{self.last_fps:.1f} FPS" if running and self.last_fps else "")
        self.activity_label.configure(text=f"Last: {self.last_activity}" if self.last_activity else "")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running and not self.bot.stopping() else "disabled")
        for button in self.action_buttons:
            button.configure(state="normal" if running and not self.bot.stopping() else "disabled")
        self._show_snooze(running)

    def _show_snooze(self, running):
        """The seconds of potion snooze left, in red on its button, while there are any."""
        button, normal = self.buttons["snooze_potions"]
        if not running:
            self.potion_snooze_until = 0.0  #a restarted bot starts unsnoozed
        left = self.potion_snooze_until - time.monotonic()
        if left > 0:
            button.configure(text=f"{normal}   {math.ceil(left)}s", style="Snoozed.TButton")
        else:
            button.configure(text=normal, style="TButton")

    def on_close(self):
        if self.bot.running():
            if not messagebox.askyesno("Quit", "The bot is running. Closing this window stops it "
                                               "too.\n\nClose anyway?"):
                return
            self.bot.kill_now()
        self.root.destroy()


def _sharp_on_windows():
    """Without this, Windows scales the whole window up as a bitmap on a high-DPI screen and every
    letter comes out blurry. Harmless (and skipped) everywhere else."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main():
    _sharp_on_windows()
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
