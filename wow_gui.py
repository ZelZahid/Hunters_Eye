'''
Hunter's Eye - the WoW fishing panel.

    python wow_gui.py          (on Windows, `pythonw wow_gui.py` for no console window)

A window for wow.py: start and stop it, pause and resume it (the same as F5), the auto-pause
countdown, and what the session is bringing in - every fish caught, how fast, and what it is worth
to a vendor, in gold, silver and copper. F5 and End keep working exactly as before.

!!! wow.py automates an ONLINE game, which Blizzard's terms forbid - see the warning at the top of
wow.py and in docs/wow_fishing.md. This window does not change that.

HOW IT IS BUILT - the same way as the Diablo II control panel (gui.py), and for the same reasons:
  - wow.py runs as a CHILD PROCESS (`wow.py --control`), never inside this window. A crash in one
    cannot take down the other, and closing this window ends the run (wow.py quits when its input
    closes, so there is never an invisible helper left pressing keys).
  - Buttons send one word per line on wow.py's input ("toggle", "auto_pause_30", "quit"); each
    calls exactly what its hotkey calls. wow.py answers with `@status {...}` and `@catch {...}`
    lines, which this window reads (and keeps out of the log view); everything else is the log.
  - The tallies live HERE, so a session survives stopping and restarting the helper. wow.py also
    writes every catch to recordings/wow/catches.csv, which the "All time" tab adds up.

WHAT THE NUMBERS MEAN:
  - Session length: time actually fishing - not paused, WoW in front, wow.py running. It stands
    still otherwise, and rates are per hour of THAT, so a coffee break does not drag them down.
  - Earnings: vendor prices (the tooltip's "Sell Price"), not auction prices. A fish with no price
    yet is counted, but left out of the money and named in red: double-click it to set one.

Plain tkinter only, like gui.py, so there is nothing extra to install.
'''
import base64
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import cv2 as cv  # noqa: E402 - after the path bootstrap, like every entry point here
import numpy as np  # noqa: E402

import gui  # noqa: E402 - BotProcess and the panel's helpers are shared, not copied
import wow  # noqa: E402 - for its file locations and settings; nothing in it starts on import
from core import frame_source, text_feed, window_region  # noqa: E402
from routes import fish_tally  # noqa: E402

WOW_SCRIPT = ROOT / "wow.py"
POLL_MS = 200
LOG_MAX_LINES = 5000
STATUS_STALE_SECONDS = 3.0     #no @status for this long: the numbers on screen are not live
EVENT_LINE = re.compile(r"^@(\w+) (\{.*\})\s*$")
PROBLEM_LINE = re.compile(r"WARNING|Traceback|Error|STOPPED|could not", re.IGNORECASE)

#The look: a dark window with gold headings, like the game's own panels. Colours only ever say
#something - green fishing / fine, amber paused / soon, red stopped / needs you - plus the coins'.
BG, HEADER_BG, FIELD, BUTTON = "#1f2128", "#15161b", "#17191e", "#2c303b"
BORDER, SELECTED = "#3a3f4c", "#34405a"
TEXT, MUTED, ACCENT = "#e8e6e1", "#9aa0ab", "#e3b448"      #ACCENT: the headings' gold
BLUE = "#6cb6ff"
GOLD, SILVER, COPPER = "#f2c94c", "#c7ccd3", "#d9844a"
GREEN, AMBER, RED, GREY = "#4cc764", "#f0b429", "#ff5c5c", "#8a8f98"
LOOP_STATES = {"casting": "about to cast", "waiting for bar": "casting",
               "fishing": "waiting for a bite", "stopped": "stopped"}


def clock(seconds):
    """29:41 for a countdown; 1:02:03 past an hour."""
    seconds = int(max(0, round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def apply_theme(root):
    """Dark, gold-headed ttk styles for the whole window. 'clam' is the one built-in ttk theme that
    takes colours on every OS ('vista' and 'aqua' draw native widgets and ignore most of them), and
    it ships with Tk itself - still nothing to install. Plain tk widgets (the log, the lists) are
    not styled by this; each is coloured where it is made, from the same constants."""
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=BG)
    base = ("TkDefaultFont", 10)
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, troughcolor=FIELD, selectbackground=SELECTED,
                    selectforeground=TEXT, insertcolor=TEXT, arrowcolor=TEXT, focuscolor=BORDER,
                    font=base)
    style.map(".", foreground=[("disabled", GREY)])
    style.configure("Header.TFrame", background=HEADER_BG)
    style.configure("Header.TLabel", background=HEADER_BG, foreground=TEXT)
    style.configure("Title.TLabel", background=HEADER_BG, foreground=ACCENT,
                    font=("TkDefaultFont", 13, "bold"))
    style.configure("State.TLabel", background=HEADER_BG, font=("TkDefaultFont", 12, "bold"))
    style.configure("HeaderCaption.TLabel", background=HEADER_BG, foreground=MUTED)
    style.configure("Header.TCheckbutton", background=HEADER_BG, foreground=MUTED,
                    indicatorbackground=FIELD, indicatorforeground=GREEN)
    style.map("Header.TCheckbutton", background=[("active", HEADER_BG)],
              indicatorbackground=[("selected", FIELD)])
    style.configure("TCheckbutton", indicatorbackground=FIELD, indicatorforeground=GREEN)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("Big.TLabel", font=("TkDefaultFont", 28, "bold"), foreground=GREEN)
    style.configure("Value.TLabel", font=("TkDefaultFont", 11, "bold"))
    for name, colour in (("Green", GREEN), ("Gold", GOLD), ("Blue", BLUE)):
        style.configure(f"{name}.Value.TLabel", font=("TkDefaultFont", 11, "bold"), foreground=colour)
    style.configure("Caption.TLabel", foreground=MUTED)
    style.configure("Problem.TLabel", foreground=RED)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                    font=("TkDefaultFont", 10, "bold"))
    #Buttons: plain ones grey; Start green, Stop red, Pause amber - the colour says what it does.
    style.configure("TButton", background=BUTTON, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BUTTON, darkcolor=BUTTON, padding=(10, 4))
    style.map("TButton", background=[("disabled", BG), ("pressed", BORDER), ("active", "#3a3f4c")],
              lightcolor=[("disabled", BG)], darkcolor=[("disabled", BG)])
    for name, colour, hover in (("Start", "#2f7d3d", "#38944a"), ("Stop", "#8f2d2d", "#a83a3a"),
                                ("Pause", "#8a6514", "#a37819")):
        style.configure(f"{name}.TButton", background=colour, lightcolor=colour, darkcolor=colour,
                        bordercolor=colour, foreground="#ffffff", font=("TkDefaultFont", 10, "bold"))
        style.map(f"{name}.TButton", background=[("disabled", BUTTON), ("pressed", colour), ("active", hover)],
                  lightcolor=[("disabled", BUTTON), ("active", hover)],
                  darkcolor=[("disabled", BUTTON), ("active", hover)],
                  bordercolor=[("disabled", BUTTON)], foreground=[("disabled", GREY)])
    style.configure("TNotebook", background=BG, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                    tabmargins=(8, 4, 8, 0))
    style.configure("TNotebook.Tab", background=BUTTON, foreground=MUTED, bordercolor=BORDER,
                    lightcolor=BUTTON, padding=(14, 5))
    style.map("TNotebook.Tab", background=[("selected", BG), ("active", "#353a46")],
              foreground=[("selected", ACCENT), ("active", TEXT)],
              lightcolor=[("selected", BG)])
    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    bordercolor=BORDER, rowheight=24)
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT)])
    style.configure("Treeview.Heading", background=BUTTON, foreground=ACCENT, bordercolor=BORDER,
                    lightcolor=BUTTON, darkcolor=BUTTON, font=("TkDefaultFont", 10, "bold"))
    style.map("Treeview.Heading", background=[("active", "#353a46")])
    style.configure("TCombobox", background=BUTTON, fieldbackground=FIELD, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
              background=[("active", "#353a46")])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECTED)
    root.option_add("*Menu.background", BUTTON)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", SELECTED)
    for name, colour in (("Green", GREEN), ("Amber", AMBER), ("Red", RED), ("Grey", GREY)):
        style.configure(f"{name}.Horizontal.TProgressbar", background=colour, troughcolor=FIELD,
                        bordercolor=BORDER, lightcolor=colour, darkcolor=colour, thickness=10)
    style.configure("TPanedwindow", background=BG)
    style.configure("Vertical.TScrollbar", background=BUTTON, troughcolor=FIELD, bordercolor=BORDER,
                    arrowcolor=MUTED, lightcolor=BUTTON, darkcolor=BUTTON)
    return style


def dark_text(widget):
    """The plain tk text widgets (log, lists) in the window's colours."""
    widget.configure(background=FIELD, foreground=TEXT, selectbackground=SELECTED,
                     selectforeground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=BORDER)
    if isinstance(widget, tk.Text):
        widget.configure(insertbackground=TEXT)
    return widget


class RunCounter:
    """A number wow.py reports as a running total for ITS run (fishing seconds, reel-ins), kept as a
    total for the whole session across restarts of wow.py, and reset when the session is."""

    def __init__(self):
        self.base = 0.0       #what finished runs added up to
        self.current = 0.0    #the running total of the run in progress

    def update(self, value):
        self.current = float(value or 0)

    def new_run(self):
        self.base += self.current
        self.current = 0.0

    def reset(self):
        self.base = -self.current

    @property
    def total(self):
        return self.base + self.current


class Money(ttk.Frame):
    """An amount shown the way the game shows it: 1g 23s 45c, each part in its coin's colour."""

    def __init__(self, parent, size=11, bold=True):
        super().__init__(parent)
        font = ("TkDefaultFont", size, "bold" if bold else "normal")
        self.parts = []
        for colour in (GOLD, SILVER, COPPER):
            #The number in the coin's colour too, a shade lighter than the letter - as in game.
            number = tk.Label(self, font=font, foreground=colour, background=BG, padx=0)
            unit = tk.Label(self, font=font, foreground=colour, background=BG, padx=0)
            self.parts.append((number, unit))
        self.dash = tk.Label(self, text="-", font=font, foreground=GREY, background=BG)
        self.set(None)

    def set(self, copper):
        for number, unit in self.parts:
            number.pack_forget()
            unit.pack_forget()
        self.dash.pack_forget()
        if copper is None:
            self.dash.pack(side="left")
            return
        gold, silver, cop = fish_tally.split_money(copper)
        shown = [(gold, "g"), (silver, "s"), (cop, "c")]
        first = next((i for i, (value, _) in enumerate(shown) if value), 2)
        for (value, letter), (number, unit) in list(zip(shown, self.parts))[first:]:
            number.configure(text=str(value))
            unit.configure(text=letter + " ")
            number.pack(side="left")
            unit.pack(side="left")


class FishingPanel:
    def __init__(self, root, bot=None):
        self.root = root
        self.bot = bot or gui.BotProcess(command=self._command(False))
        self.catalog = fish_tally.Catalog(wow.PRICES, wow.USER_PRICES)
        self.tally = fish_tally.Tally()
        self.fishing = RunCounter()
        self.reeled = RunCounter()
        self.missed = RunCounter()
        self.last_catch = None           #(time.time(), name)
        self.status = None
        self.status_at = 0.0
        self.stopped_reason = ""
        self.was_running = False
        self.check_output = queue.Queue()

        root.title("Hunter's Eye - WoW fishing")
        root.geometry("1040x760")
        root.minsize(860, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(root)

        self._build_header()
        self._build_cards()
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        notebook.add(self._build_catches_tab(notebook), text="Catches")
        notebook.add(self._build_history_tab(notebook), text="All time")
        notebook.add(self._build_log_tab(notebook), text="Log")
        notebook.add(self._build_setup_tab(notebook), text="Setup")
        self.notebook = notebook

        for problem in self.catalog.problems:
            self._append(f"fish prices: {problem}", "problem")
        self.refresh_history()
        self._refresh()
        self.root.after(POLL_MS, self._tick)

    @staticmethod
    def _command(dry_run, minutes=None):
        command = [gui._python_for_child(), "-u", str(WOW_SCRIPT), "--control"]
        if dry_run:
            command.append("--dry-run")
        if minutes:
            command += ["--auto-pause", str(minutes)]
        return command

    # --- the top: state, start/stop -------------------------------------------------------
    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 10), style="Header.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="WoW fishing", style="Title.TLabel").pack(side="left", padx=(0, 18))
        self.state_dot = tk.Label(bar, text="●", font=("TkDefaultFont", 16), background=HEADER_BG)
        self.state_dot.pack(side="left")
        self.state_label = ttk.Label(bar, text="", style="State.TLabel")
        self.state_label.pack(side="left", padx=(4, 12))
        self.detail_label = ttk.Label(bar, text="", style="HeaderCaption.TLabel")
        self.detail_label.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop_bot, style="Stop.TButton")
        self.stop_button.pack(side="right")
        self.start_button = ttk.Button(bar, text="Start fishing", command=self.start_bot, style="Start.TButton")
        self.start_button.pack(side="right", padx=6)
        self.dry_run = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Dry run (press nothing)", variable=self.dry_run,
                        style="Header.TCheckbutton").pack(side="right", padx=8)
        tk.Frame(self.root, height=2, background=ACCENT).pack(fill="x")   #a thin gold rule

    # --- the three cards: auto-pause, this session, earnings -------------------------------
    def _build_cards(self):
        row = ttk.Frame(self.root, padding=(8, 10, 8, 8))
        row.pack(fill="x")
        for column in range(3):
            row.columnconfigure(column, weight=1, uniform="cards")

        timer = ttk.LabelFrame(row, text="Auto-pause", padding=10)
        timer.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.timer_label = ttk.Label(timer, text="30:00", style="Big.TLabel")
        self.timer_label.pack(anchor="w")
        self.timer_caption = ttk.Label(timer, text="", style="Caption.TLabel", wraplength=280)
        self.timer_caption.pack(anchor="w")
        self.timer_bar = ttk.Progressbar(timer, maximum=1.0, mode="determinate",
                                         style="Green.Horizontal.TProgressbar")
        self.timer_bar.pack(fill="x", pady=(6, 6))
        controls = ttk.Frame(timer)
        controls.pack(fill="x")
        self.pause_button = ttk.Button(controls, text="Pause  (F5)", command=self.toggle_pause,
                                       style="Pause.TButton")
        self.pause_button.pack(side="left")
        ttk.Label(controls, text="after").pack(side="left", padx=(12, 4))
        self.minutes = tk.StringVar(value=str(wow.AUTO_PAUSE_MINUTES))
        choice = ttk.Combobox(controls, textvariable=self.minutes, width=4, state="readonly",
                              values=[str(m) for m in wow.AUTO_PAUSE_CHOICES])
        choice.pack(side="left")
        choice.bind("<<ComboboxSelected>>", lambda e: self.set_minutes())
        ttk.Label(controls, text="min").pack(side="left", padx=(4, 0))

        session = ttk.LabelFrame(row, text="This session", padding=10)
        session.grid(row=0, column=1, sticky="nsew", padx=6)
        self.session_values = {}
        #Session length counts only while actually fishing (Zel, 2026-11-25): paused, auto-paused,
        #WoW not in front or wow.py stopped, it stands still - it is the same clock the rates use.
        for line, (key, caption, look) in enumerate((
                ("fishing", "Session length", "Value"), ("caught", "Caught", "Green.Value"),
                ("rate", "Catch rate", "Blue.Value"), ("each", "One every", "Value"),
                ("reeled", "Bites reeled in", "Value"), ("last", "Last catch", "Value"))):
            ttk.Label(session, text=caption, style="Caption.TLabel").grid(row=line, column=0, sticky="w",
                                                                          pady=1)
            value = ttk.Label(session, text="-", style=f"{look}.TLabel")
            value.grid(row=line, column=1, sticky="w", padx=(12, 0))
            self.session_values[key] = value
        ttk.Label(session, text="Counts only while fishing - not while paused.",
                  style="Caption.TLabel").grid(row=line + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        session.columnconfigure(1, weight=1)

        money = ttk.LabelFrame(row, text="Earnings (vendor prices)", padding=10)
        money.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        ttk.Label(money, text="This session", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        self.earned = Money(money, size=16)
        self.earned.grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(money, text="Per hour of fishing", style="Caption.TLabel").grid(row=2, column=0, sticky="w")
        self.hourly = Money(money, size=16)
        self.hourly.grid(row=3, column=0, sticky="w")
        self.money_note = ttk.Label(money, text="", style="Problem.TLabel", wraplength=280)
        self.money_note.grid(row=4, column=0, sticky="w", pady=(8, 0))

    # --- Catches tab ---------------------------------------------------------------------------
    def _build_catches_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)
        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        columns = ("caught", "share", "price", "value", "hourly")
        self.table = ttk.Treeview(left, columns=columns, height=12)
        self.table.heading("#0", text="Fish")
        self.table.column("#0", width=230)
        for column, heading, width in zip(columns, ("Caught", "Share", "Vendor price", "Worth", "Per hour"),
                                          (70, 70, 100, 100, 80)):
            self.table.heading(column, text=heading)
            self.table.column(column, width=width, anchor="e")
        self.table.tag_configure("odd", background="#1c1f26")
        self.table.tag_configure("unpriced", foreground=RED)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-1>", lambda e: self.ask_price())
        self.table.bind("<Button-3>", self._table_menu)
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Set price...", command=self.ask_price).pack(side="left")
        ttk.Button(buttons, text="It's a misreading of...", command=self.ask_merge).pack(side="left", padx=4)
        ttk.Button(buttons, text="New session", command=self.reset_session).pack(side="right")
        ttk.Label(left, style="Caption.TLabel", wraplength=600, text=(
            "Any item the chat says you looted is counted - new fish appear here by themselves. "
            "Double-click one to set what a vendor pays for it (the tooltip's Sell Price).")
        ).pack(fill="x", pady=(6, 0))
        panes.add(left, weight=3)

        right = ttk.LabelFrame(panes, text="Latest catches", padding=4)
        self.recent = dark_text(tk.Listbox(right, height=12, activestyle="none", font=gui._log_font()))
        self.recent.pack(fill="both", expand=True)
        panes.add(right, weight=2)
        return tab

    def _table_menu(self, event):
        row = self.table.identify_row(event.y)
        if not row:
            return
        self.table.selection_set(row)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Set price...", command=self.ask_price)
        menu.add_command(label="It's a misreading of...", command=self.ask_merge)
        menu.tk_popup(event.x_root, event.y_root)

    def _selected_fish(self):
        selection = self.table.selection()
        return selection[0] if selection else None

    def ask_price(self, name=None):
        name = name or self._selected_fish()
        if name is None:
            messagebox.showinfo("Set price", "Pick a fish in the list first.")
            return
        current = self.catalog.price(name)
        answer = simpledialog.askstring(
            "Vendor price", f"What does a vendor pay for ONE {name}?\n\n"
            "Hover over it in your bags: it is the tooltip's 'Sell Price'.\n"
            "Write it like 24, 2s 40c, 19s or 1g 5s (a plain number is copper).",
            initialvalue=fish_tally.format_money(current, "") if current is not None else "",
            parent=self.root)
        if answer is None:
            return
        try:
            self.catalog.set_price(name, answer)
        except ValueError as exc:
            messagebox.showerror("Not a price", str(exc))
            return
        except OSError as exc:
            messagebox.showerror("Could not save", f"{wow.USER_PRICES}:\n{exc}")
            return
        self._append(f"panel: {name} sells for {fish_tally.format_money(self.catalog.price(name))}", "panel")
        self._refresh()

    def ask_merge(self):
        wrong = self._selected_fish()
        if wrong is None:
            messagebox.showinfo("Misreading", "Pick the misread name in the list first.")
            return
        others = [n for n in self.catalog.names() if n != wrong]
        dialog = tk.Toplevel(self.root, background=BG)
        dialog.title("A misreading")
        dialog.transient(self.root)
        ttk.Label(dialog, padding=10, wraplength=380, text=(
            f"'{wrong}' was really...\n\nIts catches move to the fish you pick, and from now on "
            f"this reading counts as that fish.")).pack(fill="x")
        right = tk.StringVar()
        ttk.Combobox(dialog, textvariable=right, values=others, state="readonly", width=40).pack(padx=10)

        def apply():
            if not right.get():
                return
            try:
                self.catalog.merge(wrong, right.get())
            except (ValueError, OSError) as exc:
                messagebox.showerror("Could not merge", str(exc), parent=dialog)
                return
            self.tally.merge(wrong, right.get())
            self._append(f"panel: '{wrong}' now counts as {right.get()}", "panel")
            dialog.destroy()
            self._refresh()

        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Merge", command=apply).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=4)

    def reset_session(self, confirm=True):
        if confirm and self.tally.total and not messagebox.askyesno(
                "New session", "Start counting from zero? (Every catch stays in the All time tab.)"):
            return
        self.tally = fish_tally.Tally()
        for counter in (self.fishing, self.reeled, self.missed):
            counter.reset()
        self.last_catch = None
        self.recent.delete(0, "end")
        self._refresh()

    # --- All time tab ----------------------------------------------------------------------------
    def _build_history_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.history_label = ttk.Label(top, text="", style="Value.TLabel")
        self.history_label.pack(side="left")
        self.history_money = Money(top, size=11)
        self.history_money.pack(side="left", padx=8)
        ttk.Button(top, text="Refresh", command=self.refresh_history).pack(side="right")
        self.history = ttk.Treeview(tab, columns=("caught", "value"), height=12)
        self.history.heading("#0", text="Fish")
        self.history.tag_configure("unpriced", foreground=RED)
        self.history.column("#0", width=260)
        self.history.heading("caught", text="Caught")
        self.history.heading("value", text="Worth")
        self.history.column("caught", width=90, anchor="e")
        self.history.column("value", width=120, anchor="e")
        self.history.tag_configure("odd", background="#1c1f26")
        self.history.pack(fill="both", expand=True, pady=(6, 0))
        ttk.Label(tab, style="Caption.TLabel", text=f"From {wow.HISTORY}, which wow.py adds every "
                  "catch to, with or without this window.").pack(fill="x", pady=(6, 0))
        return tab

    def refresh_history(self):
        counts, sessions = fish_tally.read_history(wow.HISTORY)
        all_time = fish_tally.Tally()
        for name, count in counts.items():
            all_time.add(name, count)
        copper, unpriced = all_time.value(self.catalog)
        self.history.delete(*self.history.get_children())
        for index, (name, count) in enumerate(counts.most_common()):
            price = self.catalog.price(name)
            self.history.insert("", "end", iid=name, text=name, values=(
                count, fish_tally.format_money(price * count) if price is not None else "no price"),
                tags=(("unpriced",) if price is None else ()) + (("odd",) if index % 2 else ()))
        self.history_label.configure(text=f"{all_time.total} caught in {sessions} session(s), worth")
        self.history_money.set(copper)

    # --- Log tab -----------------------------------------------------------------------------
    def _build_log_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)
        self.log = dark_text(ScrolledText(tab, height=10, wrap="none", state="disabled",
                                          font=gui._log_font()))
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("problem", foreground=RED)
        self.log.tag_configure("panel", foreground=BLUE)
        self.log.tag_configure("catch", foreground=GREEN)
        return tab

    def _append(self, line, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    # --- Setup tab: where the chat box is ------------------------------------------------------
    def _build_setup_tab(self, parent):
        tab = ttk.Frame(parent, padding=8)
        ttk.Label(tab, wraplength=900, justify="left", text=(
            "Catches are counted by reading the green \"You receive loot: [...]\" lines in WoW's chat "
            "box. The default is where WoW puts the chat box at 1920x1080. If yours is somewhere "
            "else (or bigger, or a different font size), set it here once: the panel takes a "
            "screenshot of WoW, and you drag a box around the chat text.\n\n"
            "Then press 'Test chat reading' with a loot line on screen: it should list it as LOOT.")
        ).pack(fill="x")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="Set chat area...", command=self.set_chat_area).pack(side="left")
        ttk.Button(buttons, text="Test chat reading", command=self.test_chat).pack(side="left", padx=4)
        ttk.Button(buttons, text="Back to the default area", command=self.default_chat_area).pack(side="left")
        self.chat_where = ttk.Label(tab, style="Caption.TLabel")
        self.chat_where.pack(fill="x")
        self.check_text = dark_text(ScrolledText(tab, height=12, wrap="word", state="disabled",
                                                 font=gui._log_font()))
        self.check_text.pack(fill="both", expand=True, pady=(8, 0))
        self._show_chat_where()
        return tab

    def _show_chat_where(self):
        mine = wow.USER_CHAT_CONFIG.exists()
        config = wow.load_chat_config()
        if config is None:
            self.chat_where.configure(text="The chat settings cannot be read - see the Log tab.")
            return
        x, y, w, h = config["region"]
        self.chat_where.configure(text=(
            f"Reading: {'your own area' if mine else 'the default area'} - from {x:.1%} across and "
            f"{y:.1%} down the WoW window, {w:.1%} wide and {h:.1%} tall; "
            f"lines {config['line_pitch']:.0f} px apart at {config['source_height']:.0f} px tall."))

    def _set_check_text(self, text):
        self.check_text.configure(state="normal")
        self.check_text.delete("1.0", "end")
        self.check_text.insert("end", text)
        self.check_text.configure(state="disabled")

    def test_chat(self):
        self._set_check_text("Reading the chat box...")

        def work():
            try:
                done = subprocess.run([gui._python_for_child(), str(WOW_SCRIPT), "--read-chat"],
                                      cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                                      encoding="utf-8", errors="replace",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.check_output.put(done.stdout + done.stderr)
            except (OSError, subprocess.SubprocessError) as exc:
                self.check_output.put(f"Could not run the check: {exc}")

        #Its own thread: OCR takes a moment, and this window must never freeze waiting on it.
        threading.Thread(target=work, daemon=True).start()

    def default_chat_area(self):
        if wow.USER_CHAT_CONFIG.exists() and messagebox.askyesno(
                "Default chat area", "Forget your own chat area and use the default one?"):
            wow.USER_CHAT_CONFIG.unlink(missing_ok=True)
            self._after_chat_change()

    def _after_chat_change(self):
        self._show_chat_where()
        if self.bot.running():
            self._append("panel: the chat area changed - Stop and Start again to use it.", "panel")

    def set_chat_area(self):
        if not messagebox.askokcancel(
                "Set chat area", "After OK, click on the WoW window so it is in front.\n\n"
                "A screenshot is taken 3 seconds later, then you drag a box around the chat text."):
            return
        self.root.iconify()
        self.root.after(3000, self._grab_for_chat_area)

    def _grab_for_chat_area(self):
        try:
            found = window_region.find_window(wow.WINDOW_TITLE)
            if found is None and window_region.available():
                raise RuntimeError(f"no window called '{wow.WINDOW_TITLE}' - is WoW running?")
            client = found[1] if found is not None else wow._primary_monitor()
            x, y, w, h = client
            source = frame_source.FrameSource({"left": x, "top": y, "width": w, "height": h},
                                              backend=frame_source.MSS)
            try:
                frame = source.grab()
            finally:
                source.close()
            if frame is None:
                raise RuntimeError("the screen could not be captured")
        except Exception as exc:  # noqa: BLE001 - shown, never a crash
            self.root.deiconify()
            messagebox.showerror("Set chat area", f"Could not take the screenshot: {exc}")
            return
        self.root.deiconify()
        ChatAreaDialog(self.root, np.ascontiguousarray(frame[:, :, :3]), wow.load_chat_config(),
                       on_save=self._save_chat_area)

    def _save_chat_area(self, region, crop, window_height):
        config = wow.load_chat_config() or {}
        source_height = config.get("source_height", 1080.0)
        data = {"_comment": "This PC's own chat area for wow.py (set in wow_gui.py's Setup tab). "
                            "Delete this file to go back to assets/wow/chat.json.",
                "region": [round(v, 4) for v in region]}
        pitch = text_feed.estimate_pitch(crop)
        if pitch:
            data["line_pitch"] = round(pitch * source_height / window_height, 1)
        try:
            fish_tally.write_json(wow.USER_CHAT_CONFIG, data)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self._append(f"panel: chat area saved to {wow.USER_CHAT_CONFIG}"
                     + (f" (lines {pitch} px apart)" if pitch else " (no text in it to measure "
                                                                    "the line spacing - kept the old one)"),
                     "panel")
        self._after_chat_change()
        self.test_chat()

    # --- running wow.py ----------------------------------------------------------------------
    def start_bot(self):
        if self.bot.running():
            return
        self.bot.command = self._command(self.dry_run.get(), self.minutes.get())
        self.stopped_reason = ""
        self.status = None
        for counter in (self.fishing, self.reeled, self.missed):
            counter.new_run()
        self._append(f"panel: starting wow.py{' (dry run)' if self.dry_run.get() else ''}...", "panel")
        try:
            self.bot.start()
        except OSError as exc:
            self._append(f"panel: could not start wow.py: {exc}", "problem")
        self._refresh()

    def stop_bot(self):
        if self.bot.running():
            self._append("panel: stopping wow.py...", "panel")
            self.bot.stop()
        self._refresh()

    def toggle_pause(self):
        if not self.bot.send("toggle"):
            self._append("panel: not running - press Start fishing first.", "problem")

    def set_minutes(self):
        if self.bot.running():
            self.bot.send(f"auto_pause_{self.minutes.get()}")

    def _tick(self):
        self.bot.poll()
        self._drain_output()
        try:
            while True:
                self._set_check_text(self.check_output.get_nowait())
        except queue.Empty:
            pass
        self._refresh()
        self.root.after(POLL_MS, self._tick)

    def _drain_output(self):
        for _ in range(500):          #a burst is shown over a few ticks rather than freezing one
            try:
                line = self.bot.output.get_nowait()
            except queue.Empty:
                break
            self.handle_line(line)
        if self.bot.dropped:
            self._append(f"panel: {self.bot.dropped} log lines were dropped because this window "
                         "fell behind.", "problem")
            self.bot.dropped = 0

    def handle_line(self, line):
        """One line of wow.py's output: an @event for this window, or a line for the log."""
        event = EVENT_LINE.match(line)
        if event is None:
            self._append(line, "problem" if PROBLEM_LINE.search(line) else
                         "catch" if "caught:" in line else None)
            return
        try:
            payload = json.loads(event.group(2))
        except ValueError:
            return
        kind = event.group(1)
        if kind == "status":
            self.status = payload
            self.status_at = time.monotonic()
            self.fishing.update(payload.get("fishing_seconds"))
            self.reeled.update(payload.get("reeled"))
            self.missed.update(payload.get("missed"))
        elif kind == "catch":
            self.add_catch(payload.get("fish", "?"), int(payload.get("count", 1)),
                           payload.get("how", "read"), payload.get("time", time.strftime("%H:%M:%S")))
        elif kind == "stopped":
            self.stopped_reason = payload.get("reason", "")

    def add_catch(self, name, count, how, when):
        if self.catalog.price(name) is None and name not in self.catalog.prices:
            self.catalog.reload()          #wow.py has just listed a new fish in the user file
            self.catalog.prices.setdefault(name, None)
        self.tally.add(name, count, time.time(), how)
        self.last_catch = (time.time(), name)
        price = self.catalog.price(name)
        worth = fish_tally.format_money(price * count, "") if price is not None else "no price"
        self.recent.insert(0, f"{when}  {name}{f' x{count}' if count > 1 else ''}  {worth}"
                              f"{'  (inferred)' if how == 'inferred' else ''}")
        self.recent.itemconfigure(0, foreground=RED if price is None else MUTED if how == "inferred"
                                  else TEXT)
        if self.recent.size() > 200:
            self.recent.delete(200, "end")

    # --- painting ----------------------------------------------------------------------------
    def _live_status(self):
        if not self.bot.running() or self.status is None:
            return None
        return self.status if time.monotonic() - self.status_at < STATUS_STALE_SECONDS else None

    def _refresh(self):
        running = self.bot.running()
        if self.was_running and not running:
            for counter in (self.fishing, self.reeled, self.missed):
                counter.new_run()
            self.refresh_history()
        self.was_running = running
        status = self._live_status()
        self._paint_state(running, status)
        self._paint_timer(running, status)
        self._paint_session()
        self._paint_table()
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running and not self.bot.stopping() else "disabled")
        self.pause_button.configure(state="normal" if status else "disabled")

    def _paint_state(self, running, status):
        detail = ""
        if self.bot.stopping():
            text, colour = "Stopping...", AMBER
        elif running and status is None:
            text, colour = "Starting...", AMBER
        elif running:
            if status.get("pause_reason") == "auto":
                text, colour = "Auto-paused", RED
                detail = "nobody pressed F5 for a while - press F5 or Resume to carry on"
            elif status.get("paused"):
                text, colour = "Paused", AMBER
                detail = "F5 or Resume to carry on"
            elif status.get("blocked"):
                text, colour = "Waiting", AMBER
                detail = status["blocked"]
            else:
                text, colour = "Fishing", GREEN
                detail = LOOP_STATES.get(status.get("state"), status.get("state", ""))
            if status.get("dry_run"):
                detail += "  (dry run - no keys pressed)"
            if status.get("chat") != "reading":
                detail += "  - catches are NOT being counted (see the Log tab)"
        elif self.stopped_reason:
            text, colour, detail = "Stopped", RED, self.stopped_reason
        elif self.bot.exit_code not in (None, 0):
            text, colour = "Stopped", RED
            detail = f"wow.py exited with an error (code {self.bot.exit_code}) - see the Log tab"
        else:
            text, colour = "Not running", GREY
        self.state_label.configure(text=text, foreground=colour)
        self.state_dot.configure(foreground=colour)
        self.detail_label.configure(text=detail)
        paused = bool(status and status.get("paused"))
        self.pause_button.configure(text="Resume  (F5)" if paused else "Pause  (F5)")

    def _paint_timer(self, running, status):
        minutes = float(status["auto_pause_minutes"]) if status else float(self.minutes.get())
        total = minutes * 60
        if status is None:
            left, caption, colour = total, (f"Pauses by itself after {minutes:.0f} min without F5, "
                                            "so it never fishes on with nobody here."), GREY
        elif status.get("pause_reason") == "auto":
            left, caption, colour = 0, "Paused itself. F5 (or Resume) to carry on.", RED
        elif status.get("paused"):
            left, caption, colour = total, (f"Paused. Counts down from {clock(total)} again when "
                                            "you resume."), GREY
        else:
            left = float(status.get("auto_pause_left", total))
            caption = "until it pauses by itself. Every F5 starts it over."
            colour = RED if left < 60 else AMBER if left < 300 else GREEN
        self.timer_label.configure(text=clock(left), foreground=colour)
        bar = {GREEN: "Green", AMBER: "Amber", RED: "Red"}.get(colour, "Grey")
        self.timer_bar.configure(style=f"{bar}.Horizontal.TProgressbar")
        self.timer_caption.configure(text=caption)
        self.timer_bar.configure(value=max(0.0, min(1.0, left / total)) if total else 0)

    def _paint_session(self):
        seconds = self.fishing.total
        caught = self.tally.total
        values = self.session_values
        values["fishing"].configure(text=fish_tally.format_duration(seconds))
        values["caught"].configure(text=f"{caught}" + (f"   ({self.tally.inferred} inferred)"
                                                       if self.tally.inferred else ""))
        rate = fish_tally.per_hour(caught, seconds)
        values["rate"].configure(text=f"{rate:.0f} an hour" if rate is not None else "-")
        values["each"].configure(text=fish_tally.format_duration(seconds / caught) if caught else "-")
        reeled, missed = int(self.reeled.total), int(self.missed.total)
        values["reeled"].configure(text=f"{reeled}   ({missed} missed)" if reeled or missed else "-")
        if self.last_catch:
            ago = time.time() - self.last_catch[0]
            values["last"].configure(text=f"{fish_tally.format_duration(ago)} ago")
        else:
            values["last"].configure(text="-")
        copper, unpriced = self.tally.value(self.catalog)
        self.earned.set(copper if caught else None)
        hourly = fish_tally.per_hour(copper, seconds)
        self.hourly.set(int(round(hourly)) if hourly is not None and caught else None)
        self.money_note.configure(text=(f"{unpriced} caught with no price yet - not in these sums. "
                                        "Double-click it below to set one." if unpriced else ""))

    def _paint_table(self):
        seconds = self.fishing.total
        total = self.tally.total
        wanted = [name for name, _ in self.tally.counts.most_common()]
        for gone in set(self.table.get_children()) - set(wanted):
            self.table.delete(gone)
        for index, name in enumerate(wanted):
            count = self.tally.counts[name]
            price = self.catalog.price(name)
            hourly = fish_tally.per_hour(count, seconds)
            values = (count, f"{100 * count / total:.0f}%" if total else "",
                      fish_tally.format_money(price) if price is not None else "set price",
                      fish_tally.format_money(price * count) if price is not None else "-",
                      f"{hourly:.0f}" if hourly is not None else "-")
            tags = (("unpriced",) if price is None else ()) + (("odd",) if index % 2 else ())
            if self.table.exists(name):
                self.table.item(name, values=values, tags=tags)
                self.table.move(name, "", index)
            else:
                self.table.insert("", index, iid=name, text=name, values=values, tags=tags)

    def on_close(self):
        if self.bot.running():
            if not messagebox.askyesno("Quit", "wow.py is running. Closing this window stops it "
                                               "too.\n\nClose anyway?"):
                return
            self.bot.kill_now()
        self.root.destroy()


class ChatAreaDialog:
    """A screenshot of WoW, scaled to fit; drag a box around the chat text, then Save."""

    MAX_SIZE = (1200, 700)

    def __init__(self, root, frame, config, on_save):
        self.frame = frame
        self.on_save = on_save
        height, width = frame.shape[:2]
        self.scale = min(1.0, self.MAX_SIZE[0] / width, self.MAX_SIZE[1] / height)
        shown = cv.resize(frame, None, fx=self.scale, fy=self.scale, interpolation=cv.INTER_AREA) \
            if self.scale < 1.0 else frame
        ok, png = cv.imencode(".png", shown)
        self.window = tk.Toplevel(root, background=BG)
        self.window.title("Drag a box around the chat text")
        self.window.transient(root)
        self.image = tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode("ascii"))
        self.canvas = tk.Canvas(self.window, width=shown.shape[1], height=shown.shape[0],
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.image, anchor="nw")
        if config:
            x, y, w, h = config["region"]
            self.canvas.create_rectangle(x * width * self.scale, y * height * self.scale,
                                         (x + w) * width * self.scale, (y + h) * height * self.scale,
                                         outline="#ffd700", dash=(4, 3), width=2)
        self.box = None
        self.start = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        bar = ttk.Frame(self.window, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text="Yellow dashes: the area read now. Drag from one corner of the chat text "
                            "to the other - include every line, stop short of anything else.").pack(side="left")
        ttk.Button(bar, text="Save", command=self._save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.window.destroy).pack(side="right", padx=4)

    def _press(self, event):
        self.start = (event.x, event.y)
        if self.box is not None:
            self.canvas.delete(self.box)
        self.box = self.canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                outline="#ff3030", width=2)

    def _drag(self, event):
        if self.start is not None:
            self.canvas.coords(self.box, self.start[0], self.start[1], event.x, event.y)

    def _save(self):
        if self.box is None:
            messagebox.showinfo("Set chat area", "Drag a box first.", parent=self.window)
            return
        x0, y0, x1, y1 = self.canvas.coords(self.box)
        height, width = self.frame.shape[:2]
        left, right = sorted((x0 / self.scale, x1 / self.scale))
        top, bottom = sorted((y0 / self.scale, y1 / self.scale))
        left, top = max(0, left), max(0, top)
        right, bottom = min(width, right), min(height, bottom)
        if right - left < 40 or bottom - top < 20:
            messagebox.showinfo("Set chat area", "That box is too small for a chat box.", parent=self.window)
            return
        crop = self.frame[int(top):int(bottom), int(left):int(right)]
        region = (left / width, top / height, (right - left) / width, (bottom - top) / height)
        self.window.destroy()
        self.on_save(region, crop, height)


def main():
    gui._sharp_on_windows()
    root = tk.Tk()
    FishingPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
