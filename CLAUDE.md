# CLAUDE.md

Guidance for Claude Code working in this repository.

**This file is deliberately short.** The long-form rationale — every measurement, every
rejected alternative, the full reasoning behind each rule here — lives in
`docs/design_notes.md`. **Read the relevant section there before changing a module**;
nearly every rule below is the compressed form of something measured or something that
cost real debugging time. `docs/Error_history.txt` is the companion: individual bugs with
root cause + fix + lesson (cited below as `EH#n`).

## What this is

Hunter's Eye is a real-time, **purely pixels-in** computer-vision engine: point it at a
screen or camera feed, tell it what to look for (a reference image, a piece of text, a
colour, a trained model's classes), and it reports matches as a bounding box plus centre
point. It never reads game memory, process internals or network traffic — that is what
keeps it usable against any game, or any camera.

Diablo II: Resurrected is the current **test bed**, not the product. Two planned consumers
should shape decisions now: a GUI product where a non-technical user supplies a reference
image or text plus an action, and a patrolling security robot reusing the same engine
against a live camera to spot people.

Treat anything D2R-specific (item images, click sequences, `routes/`, `legacy/`) as
example/integration code — never design core abstractions around it.

## Design priorities

In order, because they conflict and the tradeoff must be deliberate:

1. **Stability** — no crashes, no silent hangs. This runs unattended alongside a game, and
   eventually on a robot with nobody watching the console.
2. **Performance (FPS)** — must never noticeably slow down what it is watching, and must
   run on modest hardware. The constraint the owner cares about most: a correct detector
   that tanks game FPS is a failed detector. **Call out any accuracy/speed tradeoff
   explicitly rather than making it silently.**
3. **Portability** — Windows and macOS from one codebase. OS-specific code (`pywin32`,
   DXGI, Tk overlay) stays isolated behind something that degrades gracefully.
4. **Flexibility for future targets** — nothing in the core detection path may assume
   "this is Diablo II". The seams: **frame source** (screen / window / camera),
   **detector** (template, OCR, meters, presence, map, model, colour, motion), **match
   result** (box + centre — keep this contract stable), **action** (move mouse / press key
   / click).

### Detector independence — a hard rule, not a preference

Every detector is a **separate, self-contained way of finding things, and must stay that
way.** Each is the right tool in a different situation and the project needs all of them,
permanently.

- **Detector modules must not import each other.** Shared needs go in a neutral module
  both import, never in one detector reaching into another.
- **Never delete, gut or let rot a detector because it isn't the current focus.** OCR is
  not "the item-name feature", it is *the ability to read text on a screen* — needed again
  for menus, timers, subtitles, license plates, signage. If a refactor would make an
  out-of-focus detector worse, it is the wrong refactor.
- **Removing or rewriting one detector must not require touching any other.** If it does,
  that coupling is the bug.
- **Every detector must work on non-game input** — a browser window, a document, a webcam
  frame. A detector is handed a BGR frame and returns matches.
- **Diablo II specifics never live inside a detector.** Item names in
  `assets/targets.txt`, meter positions in `assets/meters.json`, sequences in `routes/`.
  `text_detection.py` doesn't know what a rune is; it matches strings from a file.

**Litmus test for any change:** *could this still work pointed at a security camera, or a
browser window, with only a different config file?* If no, the game-specific part is in
the wrong place.

### `None` is a third answer, everywhere

Every sensor in this project returns three things, not two: the answer, the other answer,
and **`None` = "cannot tell"**. `None` is never `False` and never `0.0`, and a caller that
collapses it acts with full confidence on a reading it does not have. Several of the worst
bugs here were exactly that collapse (EH#40, and the lobby potion incident behind
`presence.py`). **A sensor that must fail into a number has to fail toward "do nothing".**
Preserve this split in anything new.

## The docs that are project memory

- **`docs/updates.txt`** — the changelog and **the source of truth for version numbers**;
  read it before assuming what "the current version" is. Starts at `0.001`, `+0.001` per
  significant update, working toward `1.000`. Each version has a matching annotated git tag
  (`v0.001`, ...) on that exact commit — **when asked to roll back to a known-working
  version, check out the tag; don't guess from commit history.** Cutting a version: append
  an entry (plus a one-line AT A GLANCE summary at the top), commit, then
  `git tag -a v0.XXX -m "..."`.
- **`docs/todolist.md`** — working memory: what to do next and why, ideas, open questions,
  known limitations, and **decisions already settled** (so they don't get re-litigated).
  **Read it at the start of a session and check it before proposing what to work on.** Keep
  it current: finished item → DONE, promote the next thing into NOW (exactly one item),
  park anything raised but not acted on in PARKING LOT.
- **`docs/Error_history.txt`** — bugs that took real effort, with root cause + fix +
  lesson. **Check it before debugging something that feels familiar.** Append an entry any
  time a bug takes real effort (not quick typos).
- `docs/design_notes.md` — the long-form rationale behind this file.
  `docs/monster_detection_plan.txt` — the monster detector's full design record.

## Running

No build step. `python -m pip install -r requirements.txt`, then `python main.py`.
`pywin32` is gated behind `sys_platform == "win32"` so the file installs on macOS/Linux
too. **When you add an import anywhere in this repo, add the package to
`requirements.txt` in the same change.** Tesseract is a separate program, not a pip
package (per-OS instructions in `requirements.txt`).

**Global hotkeys** — all via the `keyboard` package, because the overlay is click-through
and can never hold keyboard focus:

| key | effect |
|---|---|
| `End` | quit (`q` in the fallback debug window); prints final average FPS |
| `F3` | next game: quit → read the lobby's game name → increment → create |
| `F4` | snooze auto-collect 10s, **and stop a running F7 run outright** |
| `F5` | toggle the game-state debug panel (display-only; cannot disturb detection) |
| `F6` | snooze potion drinking 10s |
| `F7` | the Pindleskin run from a fresh game in Harrogath (`routes/pindle.py`) |

Snoozes re-arm themselves (a plain toggle can be left off for a whole session by
accident). A stopped F7 run stays stopped — whoever pressed `F4` has stopped watching the
screen the run was acting on.

`user_config.txt` (repo **root**, not `assets/`) holds the settings a *user* edits: potion
thresholds and which supply each rule drinks, cooldowns, teleport key/distance, the
aim/click settles, game name/password. Read once at import — edit and restart. It is at
the root because it is the one file a person is expected to hand-edit; `assets/` holds
data the tools write or the engine reads.

One-time setup before `game_state.py` reports anything:
`python tools/calibrate_meters.py` (re-run after any resolution or UI-scale change).

No linter or CI. Fourteen test files in `tests/`, none needing the game running — run each
with `python tests/<file>`.

## Architecture

### `main.py` — the pipeline, and the Diablo II integration

Four background daemon threads plus a main-thread output loop, connected by two bounded
`queue.Queue`s (maxsize 3, drop-oldest) plus lock-protected shared variables.

1. **`get_screenshot()`** — grabs via `frame_source.FrameSource`, downscales by
   `CAPTURE_SCALE` (0.3), BGRA→BGR, pushes to `screenshot_queue`, then **sleeps out the
   rest of the frame to hold `TARGET_FPS` (60)**. The pacing is the point: DXGI can produce
   ~177 FPS at 3.56 CPU cores against 0.42 paced, and nothing downstream can use more than
   60. **Higher FPS is not the goal; the same work for less CPU is.**
2. **`detect_objects()`** — the fast path. `cv.matchTemplate` against `assets/image1.png`,
   overlaps collapsed via `cv.dnn.NMSBoxes` (**not** `cv.groupRectangles` — OpenCV 5
   removed it from the Python bindings), merged with whatever `detect_text()` last
   published to `shared_text_tracks` (read behind a lock, **never blocks on OCR**).
   Matching runs on a **grayscale** copy, converted *after* `game_state` reads the frame
   (meter reading genuinely needs colour). Every rectangle carries its own `(r,g,b)` draw
   colour. Also reads the HUD meters, the belt and the in-play check off this same frame.
3. **`detect_text()`** — the slow path, fully decoupled on purpose. **Only captures a frame
   when that frame will actually be used** (no tracks to relocalize and no scan due →
   sleep; grabbing unconditionally cost a whole core, EH#36). Own independent `mss` capture
   at `OCR_CAPTURE_SCALE` (1.0); a full OCR pass at most every `OCR_INTERVAL_SECONDS`, and
   between passes re-localizes each found match every iteration via `_relocalize_track()`
   (a small local grayscale `matchTemplate`). Converts coordinates to `CAPTURE_SCALE` space
   before publishing — **consumers only ever deal in `CAPTURE_SCALE` coordinates.**
4. **`run_auto_collect()`** — polls `shared_text_tracks` for items marked `to_collect` and
   drives `actions.act_until_gone()` on the highest-priority one until picked up or
   `COLLECT_TIMEOUT_SECONDS` (5s). Gated on `actions_allowed()` both before *and
   throughout* an attempt (an attempt clicks for up to 5s).
5. **`run_potion_drinking()`** — the first thing acting on a measured **number** rather
   than a found position. See Potions below.
6. **`run_overlay()`** — **main thread, not a spawned one** (Tk's event loop must own the
   thread that created its window). Rescales rectangles back to native
   (`SCALE_TO_NATIVE = 1/CAPTURE_SCALE`) and draws via `overlay.Overlay`; falls back to
   `run_debug_window()` if `Overlay` raises `NotImplementedError` (macOS). Draws the debug
   panel *before* the queue read, so it keeps reporting staleness with no frames arriving.

**Why OCR has its own thread:** inline, it stalled the fast path for the full OCR cost.
Moving it to a thread that merely *publishes* took the pipeline from ~25 to ~44-46 FPS.

**Why OCR needs its own capture resolution:** at `CAPTURE_SCALE` 0.3 in-game text is a few
pixels tall, below what Tesseract can read. **Sharing one frame between different-shaped
detectors is a trap** — same lesson one level up, where the Save-and-Exit button template
takes its own full-resolution capture.

**`OCR_INTERVAL_SECONDS` (0.7) must stay well above a single call's cost** (~0.42-0.6s
post-crop), or OCR runs back-to-back and starves `_relocalize_track()`. Symptom: boxes
track fine standing still but lag badly the moment the camera pans. Re-check it if the
crop margins or call cost change.

**`VIEWPORT_TOP_MARGIN`/`VIEWPORT_BOTTOM_MARGIN`** crop OCR to the gameplay viewport
(~0.9s → ~0.42s, for the cost of a slice). A general HUD-vs-viewport assumption, but it
*is* an assumption: an item rendering very near the top/bottom edge goes undetected.

Three timing lessons worth not re-learning: **time a periodic action's next run from when
it *starts*, not when it finishes** (otherwise the real gap is `interval + call
duration`); **`_relocalize_track()` must refresh its reference patch on every successful
match**, not reuse the OCR-time snapshot; and **the position OCR returns is already stale
by the call's own duration** — grab one more frame right after OCR and relocalize against
*that* before publishing.

### `assets/targets.txt` — the OCR vocabulary and its tags

One item name per line. Leading **`-`** = `ignore` (matched, then discarded — the
look-alike mechanism, load-bearing). Trailing **`*`** = `to_collect`. Optional trailing
`[...]` tags in any order: a colour from `text_detection.NAMED_COLORS` (`[purple]`),
`[click]` (default) or `[key:e]` for how auto-collect acts, and `[exact]` to refuse a match
inside a longer label. Unrecognized values fall back with a load-time warning rather than
crashing or misparsing silently.

**The ORDER of the lines is priority, and further down the file wins.** Auto-collect
attempts one item at a time, so choosing between two on the ground is the whole decision;
nearest-to-cursor only breaks ties. Rejuvenation potions sit at the top (and so rank last)
because telekinesis takes them from anywhere at any time. **Cost: re-grouping the file for
readability silently re-ranks it.** `text_detection.py` records the line number and has no
opinion — file-order-as-priority is `main.py`'s policy.

**`[exact]`** is for when extra words *change what the thing is* (`Grand Charm` vs
`Serpent's Grand Charm of Vita`; `EXIT` vs `NO EXIT`), not when they decorate it. Known
limitation: "the line" is Tesseract's idea of a line, which occasionally sweeps a
neighbour on, and an `[exact]` item is then unmatched that scan — the safe direction. **Tesseract's
line can also be too short, which is not safe**: `Grand Charm of Inertia` came back as
`GRAND CHARM 6F` + `INERTIA` and was collected (EH#51). So an `[exact]` match also needs **no other
word beside it on the same row** (`_has_row_neighbour()`, judged from the boxes, not the grouping)
and **no more words than the name** — the merge/split path scores `GRANDCHARM6F` 0.909 against
`GRANDCHARM`, because a short extra word is only a couple of characters of a long name.

**A duplicate name warns**, because last-one-wins silently hid a real bug for a version: a
target overwritten by a `-` look-alike is indistinguishable from "OCR can't read this".
**When a look-alike entry stops being necessary, delete it.**

### `core/frame_source.py` — where pixels come from

A frame source is handed a screen region and hands back BGRA; it knows nothing about what
looks at them. `cv.VideoCapture` for a camera is the next backend to slot in here.

**DXGI Desktop Duplication** (`bettercam`, Windows-only, optional) replaces essentially all
of the ~12.7ms GDI `BitBlt`: capture thread 15.3 → 7.0 CPU-ms/frame. **`mss`** is the
portable backend, chosen automatically when `bettercam` is absent, on non-Windows, **and as
a runtime fallback if DXGI fails mid-session** (`DXGI_ERROR_ACCESS_LOST` on a resolution
change, UAC prompt, driver reset, fullscreen transition). All three paths are tested,
because stability outranks performance.

- **A DXGI source is a per-output SINGLETON, and asking for a second silently hands you the
  first** — `bettercam.create()` doesn't raise, it returns the same object, and two threads
  then steal frames from each other. This is why `detect_text()` keeps its own `mss`
  capture, and why any second consumer must pass `backend=MSS` explicitly (EH#39).
- **Use the polled `grab()`, never `video_mode`/`get_latest_frame()`** — that path waits on
  a `threading.Event` with **no timeout**, so a dead capture thread hangs the caller
  forever. Polling measured *cheaper* anyway, so avoiding the hang cost nothing.
- **`grab()` returns a VIEW valid only until the next `grab()`** on that source — consume
  it (resize, convert, slice) before grabbing again. Copying an 8MB BGRA frame per frame is
  exactly the waste v0.008 removed.
- DXGI hands over a frame only when the desktop **changed**, so `None` is the normal answer
  on a static screen, not an error; the source re-serves the previous frame.
- **Still on the table, measured**: capturing only the game window is 3.4 vs 7.0
  CPU-ms/frame — the biggest remaining win. Not done because it changes the coordinate
  space every consumer assumes.

### `core/actions.py` — the mouse + keyboard action layer

Knows nothing about item names, OCR or Diablo II. **What is done to a target and how long
to keep doing it are separate arguments**: `act_until_gone(get_position, act, ...)` is the
loop (retry cadence, timeout, pause handling, is-it-gone polling) and `act(x, y)` is the
thing done. `click_at` is the default; `press_at(key)` moves the cursor onto the target and
presses a key there (a skill aimed with the mouse, triggered from the keyboard).
`click_when_seen()` waits for something to appear and clicks it **once** (a menu button is
not a walk-over-and-pick-up). `wait_until()` waits for an *effect*, so a step can check it
worked.

- **Checking "is it gone" and issuing a new click are deliberately different cadences.**
  Many games treat a click on an item as "walk there", so re-clicking faster than the walk
  retargets the character mid-walk (EH#18).
- **Each click is three steps, not one call**: move, wait `MOVE_SETTLE_SECONDS`, press,
  wait `CLICK_HOLD_SECONDS`, release. pyautogui's convenience functions either add their
  own ~0.1s `PAUSE` or collapse press+release into one zero-duration `mouse_event()` that
  some games' per-frame input polling misses entirely. All calls pass `_pause=False`.
- **`press_key()` uses the `keyboard` package, not pyautogui, and they are not
  interchangeable.** pyautogui hardcodes the scancode to `0`; `keyboard` passes the real one
  — **measured** through a raw `WH_KEYBOARD_LL` hook (`scanCode=100` vs `0`). A game reading
  DirectInput/Raw Input looks at the scancode, so pyautogui's arrives empty and the game
  does nothing, with no error. **Expect this from any input library: the convenient call
  sends something, raises nothing, and is silently ignored by the one program it was aimed
  at.**
- **The aim settle is longer than the click settle** (`AIM_SETTLE_SECONDS` 0.20 vs
  `MOVE_SETTLE_SECONDS` 0.05) because of the shape of the Win32 events, not cursor speed: a
  button-down carries the cursor position in its own `lParam`, while a keypress carries no
  coordinates and is resolved against whatever the game separately believes the cursor is.
  The click path gets the position for free; the press path does not. **A threshold
  repurposed for a different job has to be re-derived, not carried across.**
- **Every settle is a HOLD, not a move-then-sleep, because the player's mouse is moving
  too.** `hold_cursor()` re-asserts the position every `CURSOR_HOLD_INTERVAL` (10ms) for the
  settle's length — which is why raising `AIM_SETTLE_SECONDS` made things *worse*. No
  `BlockInput` (needs privileges, can leave a machine with no input at all) and no
  low-level hooks: the player's mouse is simply outvoted for 50-200ms, and only while an
  item is being collected.
- **An action that measurably did nothing is retried early.** In a camera-follows-character
  game an action that **landed moves the world**; one that missed changes nothing.
  `RETRY_IF_STILL_SECONDS` (0.25s) acts again once the target has sat within
  `RETRY_STILL_PX` (4px) of the last aim point for that long. **Switch it off
  (`retry_if_still=None`) for a game that doesn't move its camera while walking** — a
  fixed-camera scene looks identical to a miss.
- **Both settles are `user_config.txt` settings** (`click_settle`, `aim_settle`) — the right
  value depends on the game and machine, and nobody should edit code to try numbers. The
  console reports **which attempt each pickup landed on**, which is what turns "it misses
  the first try" into something tunable.
- **A paused attempt is bounded** (`PAUSE_BUDGET_SECONDS`, 30s). Paused iterations
  deliberately don't consume the timeout — safe only while every `is_paused` eventually goes
  false, which stopped being true when the foreground guard widened auto-collect's callback,
  silently stopping pickups for a session. **"This wait doesn't count against the timeout" is
  the same statement as "this wait is unbounded"** (EH#41).

### Auto-collect specifics (`main.py`)

**Which action an item gets is data** (`[click]`/`[key:e]` in `targets.txt`).
`_collect_actions()` resolves and **validates every key once at startup** — `keyboard`
raises on an unknown name, and an exception inside the auto-collect thread would stop every
pickup for the session with one line on a console nobody is watching. A bad key falls back
to clicking.

**How fast a failed attempt retries depends on the action, not the item.**
`CLICK_RETRY_INTERVAL_SECONDS` (0.8s) exists because a click means "walk there"; a cast is
over the instant it lands, so `user_config.txt`'s `key_collect_retry` (0.35s) covers the
keyed path. It cannot usefully go below `AIM_SETTLE_SECONDS`.

**An item already on an open UI panel is filtered out — and nothing was broken.** Hovering
an item in the bag draws a **tooltip** reading exactly `Full Rejuvenation Potion`: OCR read
it correctly, the matcher matched correctly, and auto-collect drove the mouse to a potion
the player owns. The text simply doesn't mean the same thing on a panel. So the filter is
**positional** (`_outside_open_panels()`), which is sound because these panels are
**opaque** — a ground item behind one couldn't be clicked anyway. Applied **only while a
panel is open**; masking permanently would silently drop a large share of all drops.
`UI_PANEL_CONFIGS` is a **list** — a third panel is a JSON file and a PNG away, and each
config's own region is what lets the stash sit left and the inventory right. Each reference
is the panel's **title bar** (the one part that never holds an item, socket or tooltip —
EH#44's rule that a reference may contain only pixels that cannot change); for the stash it
must **not** include the tab row, since the selected tab is highlighted. **The filter runs
before matches become tracks**, so no box is drawn, no relocalization spent, and a ground
potion can't lose the nearest-to-cursor contest to one in the bag.

**Auto-collect teleports onto a far-away drop, and the hard part is what happens after the
keypress.** Key is `user_config.txt`'s `teleport_key` (blank = off). Three cases never
teleport: an item *resolved* to `[key:e]` (telekinesis already covers the distance), one
within `teleport_min_distance`, and one teleported onto within `TELEPORT_COOLDOWN_SECONDS`.
**The camera is locked to the character**, so teleporting shifts the item's screen position
by exactly the distance that made teleporting worthwhile — every held coordinate is stale
and the OCR track is usually lost outright. **A lost track does not read as "I lost it":
`act_until_gone()` reads `get_position() is None` as SUCCESS**, so it would report a pickup
that never happened. So the teleport happens *before* that loop, and `_teleport_onto()`
re-finds the item with **no position constraint** (the one place in auto-collect that
ignores position, because position is what just became meaningless). **The re-find waits for
a position that has MOVED**, not merely for a position — "is there a track?" is always true
and always answers with the stale one. A longer delay is the wrong fix (cast rate is gear,
scan timing is arbitrary, a frame can always be slow); `TELEPORT_MOVED_FRACTION` (0.5)
requires seeing the shift. A teleport that never happened shifts nothing, times out, and
ends the attempt **without clicking anywhere**. `_should_teleport()` is kept **pure** and
`test_auto_collect.py` drives it.

### Potion drinking (`main.py`)

`POTION_RULES` / `_potion_due()` / `run_potion_drinking()`. **Rules come from
`user_config.txt` at runtime**; `DEFAULT_POTION_RULES` is only the fallback for a missing
file, so behaviour is identical to before that file existed rather than silently doing
nothing. `core/user_config.py` is the loader and is as generic as the rest of the engine: it
parses "when this named meter drops below this level, press these keys, then wait this
long" — a potion belt, a drone returning on low battery, a pump on a low tank.

**Every value is validated and every bad one falls back with a named warning, because a
config file must never be able to stop a program that runs unattended** — nor let it run
with a setting nobody intended, which is worse because nothing looks wrong. Two specifics:
`configparser` needs `interpolation=None` or `below = 20%` raises
`InterpolationSyntaxError` **on access rather than on read**, landing outside any file-level
`try`; and a rule naming a nonexistent meter is **dropped, never repointed**.

A `PotionRule` is `(meter, at_or_below, keys, cooldown, label)`, evaluated **in order,
first match wins**, so urgent tiers come first. Five properties, each guarding a distinct
failure:

- **A reading of `None` fires nothing, ever** — treating "could not read" as "critically
  low" dumps the belt the instant you alt-tab. `0.0` *is* acted on.
- **Each rule cools down on its own clock** — taking a big hit one second after an ordinary
  heal is exactly when the emergency tier is needed, and a shared cooldown would swallow it.
- **`POTION_MIN_GAP_SECONDS` is a floor under every press regardless of rule** — the safety
  net that makes "empty the belt in one second" impossible even with a mistuned threshold.
- **A reading below `ignore_below` (3%) is treated as unreadable, not critical.** At
  `CAPTURE_SCALE` 0.3 one orb row is 2.3%. This costs nothing because **a meter does not
  teleport** — health falls *through* 20/15/10% at ~50 samples/s.
- **A rule with several keys alternates between them** (nothing can count how many potions
  a belt column holds).

`_potion_due()` is kept **pure** — no clock, no globals, no key presses — because what it
decides is "press a key into a live game". `test_potions.py` covers it.

### `main.py`'s scripted sequences — `quit_game()` and `next_game()`

**`quit_game()` is the shape later sequences should follow**: Esc → wait for the "Save and
Exit" button to appear → click → **confirm we actually left** by waiting for `in_play()` to
go false. **Every step waits on detection, never on a clock**, and it returns True only if
the game really exited — `next_game()` is built on it, and one that believes it quit types a
game name into a live world. Runs on its own thread (`run_next_game()`, which refuses a
second concurrent run) — the `keyboard` package runs hotkey callbacks on its listener
thread, and blocking that would stall every other hotkey including `End`.

**`next_game()` (`F3`)**: quit → read the name D2R pre-filled into the lobby's Game Name box
→ increment → type → set/clear password → Enter → confirm a game loaded. On "a game already
exists with that name" it bumps the number and tries once more, then stops rather than
hammering the server.

- **The name is read from the LOBBY, not the in-game map overlay** — the map's ~11px
  stylised text could not be read exactly by any threshold/scale/segmentation tried, and
  **upscaling made it worse** (interpolation blurs thin glyphs before thresholding).
- **Tesseract clips a glyph touching the crop border**, so a crop needs a quiet margin.
- A single threshold that works on one screenshot is tuned *to that screenshot* — the read
  votes across four.
- **OCR cannot determine case here, and `0`/`O` are the same shape in this font** — not
  fixable by tuning. `_read_lobby_game_name()` takes `known` (the name it typed last, and
  `user_config.txt`'s `game_name`) and asks *could the field be showing this?* before *what
  does it say?*. `_readings_support()` checks position by position, folding only `o/O/0` and
  `l/I/1`; the disagreement between the four thresholds covers the rest without a guessed
  confusion table. It can only **resolve, never override**: a known name that doesn't fit
  every reading is ignored, two that both fit resolve to neither (EH#47).
- **The button template is the TEXT, not the button.** All five menu buttons share identical
  frame art, so chrome is nearly all of the correlation: a whole-button template scored
  **0.900 on the right button and 0.888 on a wrong one** — a coin flip that reports success
  while clicking the wrong thing. Text-only took the margin to 0.331. **A bigger template is
  not a stronger match when the things you are telling apart share most of their pixels.**
- **The shared Tesseract engine is not thread-safe** — a second caller took the whole
  process down with **no traceback**, which was the clue (a Python exception in a daemon
  thread prints and leaves everything running). Every use is behind a lock (EH#42).

### `core/overlay.py` — transparent click-through output (Windows-only)

`Overlay` is a borderless always-on-top Tk window using two Windows tricks together: Tk's
`-transparentcolor` (invisible *and* click-through) plus a Win32 `WS_EX_TRANSPARENT` style
bit set via `pywin32` after the window exists, so even drawn outlines never intercept a
click. Neither exists on macOS — `__init__` raises `NotImplementedError` and `main()` falls
back to `run_debug_window()`. **Nobody should try to make the Tk approach cross-platform**;
macOS needs an `NSWindow` via `pyobjc` with `ignoresMouseEvents`.

**This mechanism has bitten twice. Re-verify BOTH properties (see-through *and*
click-through) if this code is touched — they are governed by different Win32 state, so
testing one can pass while the other silently regresses:**

- **Renders solid black**: `SetWindowLong` after Tk set `-transparentcolor` resets the
  layered window's transparency data. Re-assert
  `SetLayeredWindowAttributes(hwnd, colorkey, 0, LWA_COLORKEY)` immediately after.
- **Looks transparent but doesn't click through**: (1) `winfo_id()` can return an inner Tk
  window rather than the outer OS frame that governs hit-testing — walk up via
  `GetParent()`. (2) `SetWindowLong` alone doesn't make Windows re-evaluate hit-testing —
  force it with `SetWindowPos(..., SWP_FRAMECHANGED)`. Visual inspection cannot catch this;
  verify with a click-counting button under the overlay.

**Repaint rate is capped independently of detection FPS** (`BOX_REDRAW_HZ` 30,
`PANEL_REDRAW_HZ` 10), and that decoupling is the point: making detection faster silently
made this window repaint more often (34 → 51/s as a side effect of an FPS optimisation).
This is a fullscreen always-on-top **layered** window — each repaint makes the compositor
redo the whole screen, **a cost paid by the game underneath, so it never shows in our own
FPS number**. It surfaced as cursor flicker (EH#38). These are **display-only** rates;
nothing reads the screen to make a decision. Two details: a skipped repaint must **not**
record its content as drawn (or a box that stops moving right after is stranded forever),
and a cap can only land on `caller_rate/N`. The panel gets the lower cap — 1.17ms per
repaint against 0.35ms for boxes.

**Boxes are not repainted for jitter** (`BOX_MOVE_DEADBAND_PX`, 4px). At `CAPTURE_SCALE` 0.3
one pixel of jitter becomes a 3-4px on-screen jump, forcing a fullscreen layered repaint
every frame forever (~25/s → 0.3/s). Compared against the last **drawn** position, so
genuine slow drift accumulates and is never ignored indefinitely. The box settles up to 4px
out — display-only; auto-collect clicks coordinates from `shared_text_tracks`.

**`TRANSPARENT_COLOR` is near-black (`#010203`), not magenta, and that is a correctness
choice.** The key colour is what antialiasing smears around every glyph, which with magenta
produced a visible red glow around every word (mean red-and-blue over green +36.8 → +0.5).
It is `#010203` rather than `#000000` so genuinely black drawings stay visible. **The
constraint is inverted from what it used to be**: magenta is now a usable box colour and
near-blacks are the ones that would silently vanish (`NAMED_COLORS` is annotated
accordingly). Consequently `PANEL_TEXT_OUTLINE_OFFSETS` is now **empty** — the 8-neighbour
black ring existed to fill the pixels antialiasing samples, and against a dark key it was
painting black next to black (re-measured: no contrast benefit, and it made the panel
1.66ms/repaint against 0.605ms). **If `TRANSPARENT_COLOR` ever moves back to a saturated
colour, put the eight neighbours back with it — the two settings are coupled and only one is
obvious from the symptom.**

**The panel needs its dark backing plate — don't "clean that up".** The transparency is a
colour *key*, not alpha: a pixel is either exactly the key and invisible, or fully opaque.
Text drawn straight onto it gets subpixel-antialiased against the key, and on thin 11px
glyphs the fringe dominates the stroke (measured: requested `(220,220,220)`, mean lit pixel
`(204,140,201)` — visibly pink). The plate is stippled to ~50% (`PANEL_BG_STIPPLE`, measured
47% see-through) since there is no alpha to set. Detection boxes don't need this (a 2px
outline isn't antialiased the same way, and covering the game where an item lies would
defeat the point). Click-through was re-verified specifically over the plate, since it is
the first fully opaque thing the overlay draws.

**The panel plate is sized to its measured content**, not to a constant: `PANEL_WIDTH` (300)
is a **minimum** and `draw_panel` measures with real Tk font metrics and grows to fit. It was
fixed, which was fine while every value was a percentage — but a value column starting at
`PANEL_VALUE_OFFSET` (232px) meant `not on screen (0.30)` (392px), `NOT CALIBRATED` and
`window not found` rendered **in colour, over the game, outside the plate**. A bigger
constant only moves the problem, so the fix is to measure. It stays a *minimum* so an
ordinary `9%` → `100%` doesn't visibly jitter. `test_overlay.py` checks every panel state
`main.py` can produce.

`draw_panel(title, rows)` draws boxes and panel as **separate canvas layers** (tags
`"boxes"`/`"panel"`), each deleted, redrawn and skip-checked on its own so neither erases
the other. It knows nothing about game state — only labels, fractions and colours.

**The overlay is part of the composited screen, so `mss` captures it and the panel's own
text passes through OCR every scan — we are inside our own frame.** Not hypothetical: the
panel's `no read` was matched as a `LO RUNE` and reported for collection (EH#49).
**`test_targets.py` 12 asserts that no string the panel can draw matches any target** — add
to that list if the wording changes. (This paragraph used to make that check in prose, and
prose is what failed: a cutoff change in another file falsified it and nothing re-ran it.)
The plate also occludes what is behind it (`PANEL_TOP_FRACTION` moves it).

Because the overlay is click-through it can never hold keyboard focus, so quitting uses a
**global hotkey** (`keyboard`, bound to `'End'` — not a letter key, which gets pressed
constantly during real gameplay).

### `core/text_detection.py` — OCR detector

Exists because image templates don't scale to "watch for hundreds of item names": each
template needs its own `matchTemplate` pass, so cost grows **linearly** with vocabulary
while OCR's is **constant**. Measured: ~21ms/template at OCR's viewport, so 20
templates/frame ≈ OCR's cost and 80 ≈ 1.7s, over 3x slower. **Template matching only wins
below roughly 15-20 templates**; this is why OCR call *speed* was worth investing in rather
than switching detectors.

**The frame is binarized before it reaches Tesseract, and this matters more than every
cutoff in this module put together.** Tesseract expects a scanned page; a game frame is thin
bright glyphs over dark noisy art. Measured on a real frame with five legible rune labels:
Tesseract read **zero** and returned 57 junk fragments scraped off the terrain; binarizing
read four. **No cutoff can rescue a line that was never read** — this was the actual cause of
a long "it stopped detecting" hunt misdiagnosed twice as a cutoff problem. It is also
*faster*, which is not the usual direction for an accuracy fix: thresholding deletes the
texture generating the false candidates — **362ms → 51ms, 83 words → 7.**

`_preprocess()` has three modes, defaulting to `PREPROCESS_AUTO`, choosing per frame from
median brightness (subsampled every 8th pixel — it only has to be roughly right). "Light
text on dark" equally describes subtitles, HUDs and night signage, while a document is the
exact opposite and must **not** be inverted — verified on all four cases. It never rescales,
so Tesseract's boxes still refer to the caller's pixel grid.

**It thresholds the STRONGEST COLOUR CHANNEL, not perceived brightness.** `COLOR_BGR2GRAY`
weights blue at barely a tenth, and D2R draws magic items in blue: a `Grand Charm` label's
text averaged BGR `(241,110,87)` — plainly legible, luminance **119 with a max of 127**
against a 150 threshold, so the label was erased before Tesseract saw it. **Sweeping the
threshold 150→220 never read it**, which is the tell: *a threshold can only discard what the
conversion kept.* The AUTO light-on-dark *branch* stays on luminance, because "is this frame
dark overall?" genuinely is the perceived-brightness question (EH#48).

`BRIGHT_TEXT_THRESHOLD` (180) is the knob, **re-derived for the new channel rather than
carried across**. **The same number is wrong for dim UI chrome, in the opposite
direction**: at 180 the lobby's name-clash dialog reads as `ALReapy`, at 150 exactly — a
label over terrain needs a high threshold to keep terrain out; a dialog on a flat panel has
no terrain and only loses glyph. So `read_lines()` takes a `threshold` and the lobby readers
pass `DIM_UI_TEXT_THRESHOLD`. **If a label is legible in `ocr_diagnostic_N.png` but gone in
`ocr_diagnostic_N_prepared.png`, that is the number to lower.**

`find_text_matches()` makes **exactly one** OCR call per invocation over the whole frame —
deliberately not one per candidate region. An earlier version OCR'd MSER candidate crops
separately; since each call pays fixed overhead regardless of size, that multiplied into a
10-20x FPS hit. (MSER as a pre-filter was also measured and rejected: ~240ms on its own, and
its candidates' combined box covered 99% of the frame.)

**Matching happens against contiguous word-runs within a line, not the whole line.** Games
render other text on the line Tesseract perceives (D2R's "Rune" suffix), and its grouping
isn't consistent between passes — matching the whole line made both correctness and box size
unstable, and box size is where auto-collect clicks.

**A target name is matched word by word, never as one whole string.** difflib's ratio is
`2*matched/(lenA+lenB)`, so a shared generic word can be most of the string: `"RAL RUNE"`
scores exactly **0.75** against `"JAH RUNE"`, `"LEM RUNE"` and `"GUL RUNE"` on the strength
of `" RUNE"` alone. **No whole-string cutoff can fix this** — the information separating them
is *which* word differs. So `_match_ratio()` requires every word to clear its own
length-scaled cutoff (`_required_cutoff()`) and only then returns the whole-string ratio for
ranking. Differing word counts fall back to a space-stripped compare that must clear
`MERGED_WORD_CUTOFF` (0.9) — deliberately **stricter**, because no structure is left to
verify: a genuine merge/split preserves every character and scores near 1.0, and the floor
keeps out a candidate that is merely a *prefix* (the bare word `"FLAWLESS"` was scoring 0.8
against `"FLAWLESSRUBY"` and reported as a Flawless Ruby). **A whole missing word is not a
transcription artifact and must not be forgiven like one.**

**A word that identifies nothing gets one more misread character.** `POTION` appears in three
target names and could never tell them apart, yet `SUPER MANA PeTIeN` was discarded for
scoring 0.667 on it. `shared_words()` derives from the vocabulary which words appear in more
than one name; those get `base_cutoff - 1/len(word)` — **expressed in characters, not as a
ratio**, since difflib's ratio for *k* misreads in an *n*-letter word is about `1 - k/n`, so
"one more error" scales itself. Two limits keep it safe, and removing either brings back the
bug the per-word rule was written for: it never applies below 4 characters (a 2-3 letter word
is already allowed its one misread, and a second is indistinguishable from a different item),
and never to a name whose words are **all** shared — `Rejuvenation Potion`, a sub-phrase of
`Full Rejuvenation Potion`, keeps the strict cutoff throughout. `SHARED_WORD_FLOOR` (0.6) is
the floor: `no read` on this program's own F5 panel matched a `LO RUNE` because `NO`/`LO` and
`READ`/`RUNE` both scored 0.500. **Two of four characters wrong is a different word, not a
misread** — nothing shorter than six characters gets slack (EH#49; note what made it
intermittent — three rune names tied at 0.571 and a tie is suppressed, so it only surfaced
when a reading broke the tie). `test_targets.py` 10-11 re-runs every look-alike case **with
the slack active**, because a guard tested only in a configuration that never runs is not
tested.

**Fuzzy matching always returns the CLOSEST LISTED name, so anything resembling a target but
not listed gets attributed to whichever it resembles most.** This is the most important
property of this module to understand, and the root cause of `"Ral Rune"` → clicked as
`"Jah Rune"`: `"RAL"` and `"MAL"` are one substitution apart, so no cutoff can tell a
*misread* from a *correct read*. **The fix is not a cutoff — it is to make the vocabulary
complete.** A leading `-` entry wins its own match at 1.000, outscoring the 0.875 it would
score against a real target, then gets dropped. All 16 low runes are listed that way;
**deleting those lines brings the false positives straight back.** Generalises to any
vocabulary with a shared suffix and a short distinguishing part — product codes, license
plates, station names.

**Cutoffs assume a stylized game font, because that is what real input looks like.** D2R's is
Exocet-derived and Tesseract was trained on print faces, so a systematic one-character
misread is *normal*, not bad luck (`GUL`→`GU1`, `BER`→`8ER`, `VEX`→`V3X`).
`_required_cutoff()` allows one bad character in a 3-letter word (0.65), requires ≤2 letters
exact, and gives longer words the base cutoff. **That tolerance is only safe because the
look-alikes are listed — the two mechanisms are a pair**, and loosening one without the other
breaks in one direction or the other. Two calibration mistakes not to repeat: a stricter tier
carried over unchanged from when the ladder applied to whole *names* rather than single
*words* (**a threshold repurposed from a whole string to each of its parts has to be
re-derived**), and requiring 3-letter words to be exact — correct in isolation, but it assumed
clean OCR. **Neither was visible from synthetic test strings; both needed the real font.**

**Short words are compared by character POSITION, not by difflib.** `_word_ratio()` uses
positional agreement for equal-length words ≤3 chars. difflib counts *how many* characters
two strings share, not *where*, which on a 2-letter word throws away the only information
present: `"Ko Rune"` read as `"Ke RUNE"` scored `"KE"` **0.5 against both `"KO"` and
`"EL"`**, tied, and a Ko Rune went uncollected. Positionally: 0.5 vs 0.0. **This is the
better model of what OCR does** — it substitutes a character in place, it does not transpose
or shift. Longer words keep difflib, where a dropped or doubled letter really does shift the
rest.

**Candidate spans are ranked LONGEST-MATCH-FIRST, ratio only breaking ties** — the key is
`(words matched, ratio)`. Ratio-first is wrong whenever one target's name contains another's,
which the look-alike mechanism makes routine: a clean `Full Rejuvenation Potion` and its own
tail both score 1.000 (tie → suppressed), and a slightly misread full span loses to a
perfectly read tail (→ reported as nothing). **A shorter span is not better evidence just
because it scores higher on the little it covers.** A long sloppy span can't win by accident
either, since every span must already clear each per-word cutoff. **Watch the shape of this
bug**: a slightly misread label kept working while a perfectly read one failed, so **better
OCR made detection worse** — exactly backwards from where anyone would look.

**A tie between two different targets is reported as nothing, never as a guess.** This is
what makes the loose short-word cutoffs safe: they permit roughly one bad character per word,
and anything genuinely undecidable afterwards is dropped. It earns its keep on real input — a
bare `"RUNE"` ties across `EL`/`IO`/`KO`/`LO`/`UM` and is suppressed.

**Two diagnostics, for two different questions.** `python tools/diagnose_ocr.py` captures
through the exact path `detect_text()` uses and prints every line Tesseract read and why each
did or didn't match — because "it isn't detecting" has three causes that look identical from
outside: Tesseract never saw the text (capture/contrast — no cutoff will fix it), misread
characters (font), or read it fine and nothing matched (cutoff). Guessing between them wastes
a lot of time. `MATCH_DEBUG = True` is the same breakdown live; far too noisy to leave on.

A short name can still get a genuine false-positive read that no cutoff can catch —
Tesseract's per-word confidence (currently read and discarded) is the next lever.

**Two backends, tried in order at import**, both normalized to the same word tuples before a
shared `_group_words_by_line()`:

1. **`tesserocr`** — in-process bindings reusing one long-lived `PyTessBaseAPI`, **~3.5-4.5x
   faster** (~0.1-0.26s vs ~0.42-0.6s): not just subprocess launch, but no temp-file
   round-trip and no re-initializing the whole engine per call. `GetTSVText(0)` has the same
   columns as `image_to_data` DICT; level `5` rows are words.
2. **`pytesseract`** — portable fallback if `tesserocr` isn't installed or its init fails.
   Slower, but works anywhere the Tesseract binary is installed.

`tesserocr` needs a **prebuilt wheel matching the exact Python version and platform** —
there is no PyPI wheel for Windows, only an sdist needing a full C++ toolchain (EH#15); use
`github.com/simonflueckiger/tesserocr-windows_build`. It also needs `_find_tessdata_dir()`
(currently only the default Windows install path), which is separate from
`pytesseract.tesseract_cmd` (the `.exe`, not the data folder).

`_padded_box()` pads a matched run by `LABEL_PAD_X`/`LABEL_PAD_Y` — Tesseract's boxes cover
only glyphs, while games render a larger semi-transparent panel behind labels. A fixed
margin, not panel-edge detection, deliberately: D2R's panel is often as dark as the
surrounding terrain, making edge-finding unreliable across scenes. Re-tune by eye.

Two layers of defence against the external dependency: an import-time check that disables
text matching with a one-time warning rather than crashing, and a per-call
`try/except TesseractError`, because the Windows build intermittently throws a spurious
error (an "ObjectCache leak" during its own cleanup, unrelated to whether OCR succeeded) —
treated as "skip this scan", not fatal.

Call-site throttling is `main.py`'s job, not this module's.

### `core/game_state.py` — HUD meter reading

The third detector and a **different shape of answer**: not *where* but *how full* — a single
`0.0`-`1.0`. That is the input a decision layer acts on (drink below 40%, disengage below
25%).

A `Meter` is a screen region + a colour + a fill direction, which describes a health orb, a
mana bar, a stamina strip or a robot's battery gauge equally well. All game-specific
knowledge lives in `assets/meters.json` from `calibrate_meters.py` — same "edit data, not
code" philosophy as `targets.txt`. **`region` is stored as fractions, not pixels**, so one
config stays correct at any capture scale (EH#6).

**It reads off the frame `detect_objects()` already captured and does not get its own
thread** — 0.15ms/frame for two orbs, against ~17-20ms a dedicated thread would spend on its
own capture to perform a ~0.3ms measurement. OCR earned a thread at ~100-260ms/call; this
isn't close.

- **`None` and `0.0` are different answers and must stay different.** `None` = "couldn't read
  the region"; `0.0` = "the orb is empty". `_fill_fraction()` itself broke this rule — both
  its `return 0.0` failure branches now return `None`, since neither was a measurement (both
  are what an *absent* meter looks like through a colour mask, as in the lobby, and what a
  *dimmed* one looks like behind the in-game menu). **This cost nothing: a measured reading
  can never be exactly `0.0`** — the minimum is `1/height`, ~0.7% on a 150px orb — so every
  `0.0` the system ever produced came from a failure branch. Note the direction the old bug
  ran: the failure value was the most alarming number in the domain, so an unreadable sensor
  read as maximum urgency and drove the potion layer (EH#40).
- **A failed read carries the last good value, but not forever, and the cap is what makes
  the guard real.** One dropped frame (torn capture, a tooltip over the orb) should carry;
  "we are not looking at the thing at all" (alt-tabbed, camera unplugged) produces a frozen
  number that *looks* live and gets acted on with full confidence. Nothing in a single
  reading tells them apart — only how long it persists. Capped at `MAX_CARRIED_FAILURES`
  (15, ~⅓s at 50 FPS), after which it reports `None` and drops its history, so a good frame
  after a gap isn't median-blended with values from before it. **A staleness timestamp does
  not cover this**: a caller publishing every frame keeps the timestamp fresh — it is the
  *reading* that went stale.
- **Readings are MEDIAN-smoothed over 5 frames, not averaged**: the failure mode is one
  wildly wrong frame (a tooltip over the orb, a spell effect flashing the same colour), and a
  median discards it outright while an average absorbs part of it.
- **Measure fill by HEIGHT, never by pixel count.** A pixel count measures *area*, and for a
  circle area equals height only at 0%, 50% and 100% — a 25%-full circle is 19.5% by area.
  **The error is largest exactly when a health reading matters most, and zero at the midpoint
  where you'd casually test it** (EH#19).
- **Find the liquid surface with a global consistency test, not a local walk up from the
  bottom.** Games draw a specular highlight across a globe; where it crosses the liquid it
  blanks a run of rows, and a local walk stops dead there and reports the streak's height (a
  65% orb read as 25%). The surface is the highest row that is itself filled *and* has ≥
  `SURFACE_CONSISTENCY` of the rows below it filled (EH#20).
- **The surface test is vacuous at the bottom of the band.** `MIN_SURFACE_SUPPORT_ROWS` (1)
  requires enough rows beneath a candidate for that test to have meant anything — the lobby's
  red character names read **2% health** and fired an emergency potion. **Measured, not
  picked**: at 1 full-resolution readings are unchanged; at 2 a real 7% orb at 0.3 scale
  starts reading `None` for no extra protection, which is the wrong trade. Watch for
  `consistent[-0:]`, which is the *whole* array.
- **The last liquid in a globe is the DARKEST liquid, and a value floor tuned on a full orb
  throws it away.** Mana at 23/407 read `no read`: a globe is shaded toward its edges, so a
  nearly empty one does not merely get shorter — the part that survives is the *shaded* part
  (`V=16` against a configured `V >= 40`). **Saturation is what separates liquid from an
  empty orb** (25-40 vs an `S >= 70` floor), so the value floor was doing almost no work and
  all the harm; it is 10 now. General lesson: **a calibration tool samples one moment** — a
  full, brightly-lit orb — so it can never learn the states that only occur in an emergency
  (EH#50).
- **A meter can change COLOUR to signal a status, and that is not cosmetic.** D2R turns the
  health globe **green** while poisoned, and with only red configured the orb read **0% at
  every real fill level** — "you are dead" at 90% health, during the one situation where the
  number matters most. This is why `hsv_ranges` is a *list*: one range per colour a meter can
  legitimately be, OR-ed. Generic — any bar that recolours when poisoned/cursed/low has this,
  as does a battery gauge. **When a meter reads 0% but you can see it is not empty, suspect a
  colour change before the geometry.**
- **Calibration is one profile per window shape** (`Profile`, `select_profile`,
  `upsert_profile`), because a game re-lays out its HUD at a different resolution.
  Calibrating at a new size **adds**; at an existing size **replaces**. Selection is
  exact-size first, then same aspect ratio within `ASPECT_TOLERANCE`. A shape with no match
  selects **nothing** rather than the nearest — the panel shows `NOT CALIBRATED` with the
  sizes that do exist. `load_profiles()` still reads the older single-profile file shape.

The `F5` panel is the live read-out — one row per meter in `meters.json`, no code change for
a newly calibrated one. It preserves the `None`/`0.0` split visually (`no read` with **no bar
at all** vs `0%` with a bar at zero) and reports staleness (`STALE 4s`), since a frozen
number looks exactly like a correct one.

### `core/slots.py` — what is in each of a row of fixed cells

The **seventh detector** and a **fifth shape of answer**: which of several known things is in
each of a fixed set of positions.

**It exists because a consumer that presses a key needs to know the key is worth pressing.**
Potion keys used to be written by hand (`keys = 2, 3`) — a claim about the world that stops
being true the moment the belt is rearranged or a column runs dry, after which the program
presses a dead key while dying, or spends the rarest potion to top up mana. The slots are on
screen, so they can be read instead of declared.

**Colour, not template matching, and that is the point.** The thing being told apart *is* a
colour: the potions share glass, highlights and frame and differ only in the liquid. Measured
on the real belt, modal hue **0** (healing), **118** (mana), **151** (rejuvenation), while
`TM_CCOEFF_NORMED` is very nearly colour-blind — it would compare exactly the parts that do
not differ. This is the case belonging to "an explicit HSV pre-mask".

**Empty is a measurement, not a failure**: occupied reads 26-37% lit, empty reads **0.0%** (an
empty slot contains no saturated pixels at all). `read_row()` returns `None` per empty cell,
and `None` for the *whole row* only when the region can't be read. **Those must not be
collapsed** — a row of `None`s means "the belt is empty", `None` means "I cannot see the
belt", and treating an off-screen belt as empty would silently stop every potion.

`assets/belt.json` holds the region (fractions of the anchor window), the four keys, and one
HSV range list per supply — red needs two ranges because it wraps the hue circle, as
`meters.json` does. Read off `detect_objects()`' frame at `BELT_REFRESH_SECONDS` (0.5s).
**Only the bottom row matters and the belt need not be open** — D2R shows that row
permanently and it is the row bound to 1-4.

**What to do about it is `main.py`'s job, not the detector's.** A rule says
`use = rejuvenation` and `_rule_keys()` resolves it against the belt each time. Three
properties carry the owner's policy, each guarding a distinct waste:

- **A rule that cannot be satisfied is skipped, not obeyed.** With no healing potion the 70%
  rule has no keys, so the loop falls through to the rule below. **That fall-through is the
  entire mechanism behind "drink a rejuvenation at 50%, but only when healing potions are
  gone"** — break it and the backup rule silently never fires.
- **`only_if_missing` makes a rule a last resort**, and requires a *readable* belt: "I cannot
  tell whether you have healing potions" is not a reason to spend a rejuvenation.
- **Nothing spends a rejuvenation potion on mana.** There is deliberately no mana backup rule
  — a judgement about relative value, and `test_potions.py` asserts it so it cannot be added
  back by accident.

An unreadable belt falls back to the `keys` still in `user_config.txt`, so behaviour degrades
to exactly what it was before this existed.

### `core/presence.py` — "is this reference art on screen?"

The fourth detector and a **third shape of answer**: **whether the thing is there at all** (a
boolean). That third answer is what a consumer needs before it can trust the other two.

**It exists because a colour test provably cannot do this job.** The health meter matches
red; the D2R lobby renders other players' names and red armour *exactly where the health orb
belongs* (**7.3% red pixels**), periodically reading as a real 2-4% health. That fired an
emergency potion — and because a lobby has a text field focused, the keypresses were **typed
into the game name** (observed: `z25pin232321213`). Red text and red liquid are the same
colour, so no threshold, floor or consistency test separates them. **The only way out is
evidence of a different kind.** Measured: in game 0.871-0.895, lobby 0.403-0.422; threshold
`0.6`, margin **0.449**, asserted by `test_presence.py` so an eroded guard shows up as a
failure rather than as a potion typed into a text box.

- **A reference template must contain ONLY pixels that cannot change, and "cut a rectangle
  around the thing" does not give you that.** The originals carried a band of *game world*,
  the live life/mana readout, and a slice of moving orb liquid. In an unlit catacomb that band
  is black, and because `TM_CCOEFF_NORMED` normalises over the whole patch, a quarter of the
  template being scenery scored **0.573/0.535 against a 0.6 threshold on a frame with both
  orbs in plain view** — so the panel read `NOT IN GAME` and every action was gated off. **The
  score had effectively been reporting how well-lit the room was.** Recut to the statue frames
  alone (crops chosen by sweeping and measuring the margin, not by eye): **0.940**. Two things
  generalise: a normalised score never says *which* part disagreed (the diagnostic is a
  per-row diff against the correctly aligned patch), and cropping a template does **not**
  change `source_size`, which is the resolution the art was cut *at* (EH#44).
- **TWO references, on opposite corners, and either one is enough.** A tooltip over the right
  orb dropped the score to 0.48 and paused everything while the left orb sat in plain view.
  Occlusion during play is normal and **local**, so the answer is a second piece of evidence
  elsewhere, not a looser threshold — which would trade away the very thing that stops a
  lobby matching. **Two references only buy this if they fail INDEPENDENTLY, and the original
  pair did not** — both included game world, so one dark room took out both at once.
  `assets/in_play.json` takes a `references` list; a single reference may still be written
  flat (`save_and_exit.json`). Each has its own search region, so `main.py` passes a
  `resolve_region` **callback** rather than one region. **"Nothing was occluded" and
  "occlusion is handled" look identical from the result**, so the test blacks out one
  reference's entire search region and requires the other to carry the answer.
- **The negative is debounced; the positive is not.** Seeing the HUD is proof; not seeing it
  might only mean something is briefly covering it. A hit publishes immediately; a miss must
  persist `IN_PLAY_MISS_GRACE_SECONDS` (0.3s). Kept short deliberately: at 0.3s against
  `POTION_MIN_GAP_SECONDS` 0.6 that is at most one stray keypress. This covers *flicker*;
  sustained occlusion is what the second reference is for.
- **Restricted to a search region — a 15x difference, not tidiness**: 3.0ms full-frame (as
  much as the needle match, i.e. it would roughly double the detection thread's cost) vs
  **0.198ms** restricted. Cheap enough to run **every frame**, which matters because a stale
  "yes" is exactly the window where the wrong keys get typed.
- **The search region and template scale both route through the window anchor** — the art is
  at a fixed place in the *window*, not on the screen. **Nothing Diablo-II-specific is in the
  module**: pointed at a security camera the same code answers "is the door frame still in
  view?" before anything else measured from that frame is trusted.
- **`cv.resize` raises when a scale rounds a dimension to zero** (`!dsize.empty()`), so
  compute and check the size *before* resizing; checking the result's shape never runs.

`main.py`'s `actions_allowed()` requires **both** guards to not say no. They fail
differently: **focus** catches "these keys are going to another program", **in-play** catches
"these keys are going to THIS program, which is showing a lobby with a text field focused".

### `core/window_region.py` — anchoring a region to a window

**The distinction this module exists for: a detection box is screen-absolute, a HUD meter is
not.** A box is found by looking at pixels, so it works identically fullscreen or windowed.
"The health orb is 21% across and 84% down" is only meaningful relative to *the window
drawing it* — meters calibrated fullscreen read **0% at full health** once the game went
windowed, because the fractions pointed at bare desktop. Anchoring survives the window being
**moved or resized**. It does **not** survive the game changing its own resolution or aspect
ratio (measured 1920x1080 → 1280x800: vertical held to 0.001, horizontal moved 0.033 = **42px
against a 104px orb**). When the live aspect no longer matches, `main.py` warns on the console
**and the panel replaces its readings with `RECALIBRATE / was ... / now ... / readings not
trustworthy`**. **The panel matters more than the console here**: a console warning about a
silently wrong number is a warning nobody receives, because the person who needs it is looking
at the game — observed exactly that way. **Re-calibrate in whichever mode you actually play
in.**

`find_client_rect(title_substring)` returns the **client** area, not the window rect (measured
33px vertical difference, enough to miss an orb). **An exact title match beats a substring
one, and only then does size break the tie** — searching `"Diablo II"` matched both the game
and a *browser tab* titled "d2r.guide — Diablo II: Resurrected...", and the browser won on
size. Any editor tab, chat window or web page mentioning the game is a candidate.

The user never types a window title: `calibrate_meters.py` snapshots the visible windows
**before drawing any UI of its own** (otherwise its own always-on-top window is what the
point lands in), attributes each hand-drawn selection to the smallest window containing it,
and records that as `"_anchor"`. `game_state.py` stores the anchor but **never interprets
it** — resolving it needs OS-specific window lookup, which is a caller's business, not a
detector's.

**`is_foreground()` has THREE answers, not two.** Nothing used to verify this: alt-tab away
and the meters measure whatever is now in front (typically 0%, i.e. "you are about to die")
while auto-collect keeps clicking coordinates that now belong to a browser. `find_window()`
returns an **opaque handle** purely so this check can be a raw `GetForegroundWindow()`
comparison — **0.3µs**, callable per frame, against 0.151ms for the `EnumWindows` sweep that
finds the window in the first place (~500x). **`None` means "cannot tell" and must never be
collapsed into `False`** — returned without `pywin32`, without an `_anchor`, and when the
window isn't found; on all three the pipeline must behave exactly as before the guard existed
(`actions_allowed()` is `anchor_focused() is not False`).

**It is a hard stop for actions and a suspension for readings, and focus is deliberately the
wrong question.** A windowed game that is visible but unfocused is perfectly readable, so this
says `False` in cases that would have worked. **That direction is the safe one**: a false
`False` costs a paused pickup, a false `True` costs a potion drunk against a browser's pixels.
Occlusion is the question you actually want, and there is no cheap correct answer to it on
Windows. The state is surfaced **on the panel** (`NOT FOCUSED`, readings `suspended`, actions
`paused`), for the reason the `RECALIBRATE` banner exists. Detection and OCR are deliberately
**not** gated — boxes are display-only and a detector must work against any frame, so
stopping them would be a performance decision, not a safety one.

**The anchor's handle is refreshed on every lookup, even when the rect is unchanged** — a game
closed and relaunched at the same size and position is a *different* window with an *identical*
rect, and keeping the old handle leaves the focus check comparing against a window that no
longer exists: permanently `False`, with no way to clear it.

`main.py` re-locates the anchor at most once a second (`ANCHOR_REFRESH_SECONDS`; `EnumWindows`
is far too expensive at 45 FPS) and rewrites each meter's region against the captured frame.
**When the anchor is configured but its window can't be found it reads NO meters, so every
value becomes `None` — never `0.0`.** Reporting a full health orb as empty because the window
moved is exactly the confusion `game_state.py` exists to prevent. The panel says `window not
found` rather than leaving the cause to be guessed at. Everything degrades to `None` without
`pywin32`, and a config with no `_anchor` behaves exactly as it always did.

### `core/localize.py` + `routes/pindle.py` — finding your way by looking

The fifth detector and a **fourth shape of answer**: not where a thing is on screen, but
**where the screen is in the world**. `MapLocator` finds where a crop of the live frame fits
inside a stitched greyscale map — ordinary template matching with the roles swapped. Knows
nothing about any game; same `True`/`False`/`None` contract as `presence.py` (a flat loading
screen is `None`, not `False`).

**It rests on one assumption, measured before anything was built on it: the camera moves by
pure translation** (scale 1.000 ± 0.0015, rotation within ±0.07° across every overlapping pair
of 15 screenshots; offsets agreed to 0.3px and came out as multiples of 14.4px — the camera
snaps to a sub-tile grid). A game that zooms or tilts while panning breaks this, so
`tools/build_route_map.py` checks it on every build rather than trusting this paragraph.

Measured on frames the map was **not** built from: located within 1.7-2.6px, weakest score
0.726 / margin 0.590; no temple frame scores above 0.090 on Harrogath, nor Harrogath above
0.151 on the temple. Threshold 0.4, min margin 0.25.

- **Matched with local contrast normalisation (`normalize_sigma`), and a false match is why.**
  With plain `TM_CCOEFF_NORMED` a temple frame matched the *Harrogath* map at 0.630, over its
  threshold — both places are dark and contain the same bright red portal, and in a dark frame
  a few bright pixels carry a whole-patch normalised score. Normalising each neighbourhood to
  its own mean and contrast first makes every patch of stone count as much as the portal:
  weakest true match 0.776 → 0.726, strongest false 0.630 → **0.090**. **A fixture from one
  corner of a place is not evidence about the rest of it — test the negatives nearest the
  positive** (EH#45). The normalised scores are on a different scale, so each route's
  threshold and margin live in its JSON and were re-derived.
- **The margin matters as much as the score** — a match must beat the best *different* place
  by `min_margin`, or a map with two similar corners hands back whichever scored a hair
  higher. Same "a tie is reported as nothing" rule OCR follows.
- **Anything drawn on the SCREEN rather than in the world breaks stitching, silently.** A chat
  line is identical in every screenshot, so it gave 474 feature matches agreeing on *zero*
  shift between two frames 438px apart — clean consensus, scale 1.000, every check passed, and
  the tool stitched them on top of each other. `build_route_map.py` now masks the chat area
  (`--ignore` for more, e.g. the F5 panel) **and** requires each pair's aligned pixels to
  correlate ≥ 0.4 (chat pair 0.142, genuine pairs 0.539-0.939). **Feature consensus measures
  how the matches agree with each other, not with the scene** (EH#46).
- `0` in a map image means "never seen", so every seen pixel is clamped to ≥ 1 — otherwise the
  black of a dark scene is thrown away as unmapped.
- Measured, not picked: **0.25 map scale** (0.2/0.25/0.33 all accurate at 8/13/29ms), and **a
  query crop centred on the character** rather than the whole viewport — the full-width query
  failed outright on a frame near the map's edge because it could not fit inside the map,
  while the centred crop located all 15. ~18ms per live locate including the resize.

**`walk_to_portal()`** (the first leg of `F7`): every step grabs the client area, locates it
on the route map, and clicks the furthest point along the **recorded path** (where the owner's
character actually stood) that is within `LOOKAHEAD_PX`, on screen, off the HUD and outside
the waypoint keep-out. **Nothing plans a route; it replays one known to be passable**, and
because each step starts from where the character *really* is, a bump or a short click
corrects itself instead of compounding the way `legacy/pindle.py`'s fixed offsets did.
`Walker.decide()` is kept **pure** (no clock, mouse or screen) and `test_route.py` drives it
through simulated walks, including a character pushed 200px off the path.

Four things it does rather than assumes:

- **The waypoint is a keep-out zone** — clicking the ground beside its platform can land on
  the platform and open the waypoint menu, which swallows every later click.
- **The portal is clicked only after its hover label is read** (OCR, `NIHLATHAK` or `TEMPLE`
  — the font reads as `NIHLATHAK's TeEmMPLe`). Anything standing in front shows a different
  label, and clicking *that* starts a conversation. Position is recomputed from a fresh frame
  every poll, because the character is usually still running. Without OCR it clicks
  unverified and says so (`text_detection.ocr_available()` exists for exactly this: "no text"
  and "no OCR" are otherwise indistinguishable).
- **Going through is confirmed by looking**: HUD up (`in_play()`) but no longer on the
  Harrogath map, sustained 1s. A loading screen is neither, so it waits.
- **It stops and says why** (not located 3s, no progress 4s, 45s total) — never carries on
  blind. Stopping is always safe: the character is standing in town.

**The rest of `F7`**: through the portal, `E` (Conviction), `walk_to_fight_spot()` along the
temple route to the doorway the owner fights from, then `fight()`. There is no monster
detector wired in here yet, and two things the program can already see stand in for one:
**where to look — what moved** (standing still the camera doesn't move, so whatever changes
between frames 0.12s apart is alive; measured, the biggest moving region was the pack, 1px
from its centre, with torches and snow filtered by size), and **whether it is a monster — the
red name plate** D2R draws for the monster under the cursor and nothing else (87-88%
plate-red hovered, ≤2% not; a sub-millisecond colour test). The cursor visits moving spots
biggest-first and `R` is pressed only while the plate is up, **re-checked before every
press**. Fist of the Heavens spreads to everything near its target, so which monster doesn't
matter; OCR reads the plate only to report whether Pindleskin was seen. It finishes after 4
empty scans and 4s without a target, and **leaves the game (`quit_game`) at `CHICKEN_BELOW`**
(20%, below the 30% emergency potion), where **a `None` health reading never counts as low**.
`fight()` takes its sensors, clock and sleep as arguments so `test_pindle_fight.py` can drive
it through a fake game — which caught it calling Save and Exit **twice** when health collapsed
mid-attack (the inner loop broke out, the outer loop saw the same low health again) before it
ever ran live. **Unverified assumption worth knowing**: the plate's red is taken to stay up
while a monster is hurt; if D2R drains it with the monster's health, the plate test fails on
nearly-dead monsters and they get re-found by motion instead.

The temple arrival also has a **landmark fallback**: the red portal ring, the one bright
saturated ring in the scene, places the character when the map cannot (`LANDMARK_POINT`).

**`MOVE_KEY`**: D2R has a Force Move binding (Options → Controls) that walks without
interacting with whatever is under the cursor — bind it and an NPC wandering under a click is
walked past instead of spoken to. `None` = ordinary left click.

**The map is only valid at the aspect ratio it was recorded at** (16:9, 1920x1080) — a
different shape shows a different slice of world, so `Route.fits()` refuses it with a message
to re-record. Same aspect at a different size is *assumed* to scale uniformly and **is not
yet verified**; the walk prints a note. **To record a new route**: walk it taking a screenshot
every second or so (consecutive shots must overlap by about half a screen), then run
`tools/build_route_map.py` (its docstring has the exact command used for this one). The
screenshots are scratch; the map and JSON it writes are what gets committed.

### `core/monster_detection.py` — the first detector that LEARNED what to look for

Every other detector is handed a rule someone wrote: template matching a picture, OCR a word
list, `game_state.py` a colour range, `presence.py` a reference image, `localize.py` a map.
This one is handed a *trained model*, which is what lets it find a thing that never looks the
same twice — an animated 3D creature that idles, walks, attacks, turns and gets knocked back.
Template matching was **rejected for monsters on invariance, not cost**: `TM_CCOEFF_NORMED`
has no tolerance for scale, rotation or deformation, so one template covers one animation
frame at one facing at one zoom. Full design record: `docs/monster_detection_plan.txt` — read
it before changing anything here.

- **ONE model with N classes, never one model per monster.** The backbone is ~99% of the
  compute and is byte-for-byte identical whether it knows 1 class or 500; a class adds one
  number per grid cell. **Cost is flat in the number of monsters**; the per-class cost is
  training examples, paid offline. One model per monster would be linear in forward passes,
  would relearn the visual vocabulary from a small dataset each time, and — the part that
  matters most — could never learn "Pindle, **not** Defiled Warrior", since a single-class
  model only learns "this vs background". **Telling look-alikes apart is an argument for one
  multi-class model.**
- **Measured** (i9-11900K, CPU, yolo11n at 640px, 2 classes): **33-35ms/call**. The figure
  that decides the design is cores, not milliseconds: **0.28 cores at 8 Hz**. A GPU is *merely
  nice*, which is why `load()` defaults to CPU and `prefer_gpu=True` is opt-in — merely
  *asking* for CUDA on a machine that cannot provide it prints a wall of provider-bridge
  errors on every startup and then silently falls back anyway. `yolo11n` beats `yolov8n` at
  every input size, which settled the model choice.
- **Inference is FULL-FRAME, and this is the one place the project does not crop the HUD.**
  Every other detector wanted that crop; this one must not, because the model was **trained**
  on whole frames, so cropping would shift every coordinate and change the letterbox scale — a
  train/test mismatch. It also buys something: with HUD pixels present in training as
  unlabelled background, the model learned the carved gargoyles beside the orbs are not
  monsters — the exact false positive that made the zero-shot experiment unusable. **A
  negative example only teaches if the model sees it.**
- **NMS is class-aware, and that is not a detail.** A super-unique boss stands *in* its own
  pack, so its box overlaps its minions' heavily; class-agnostic suppression reads that as a
  duplicate and deletes whichever scored lower — for a rare class with few examples, almost
  always the boss. Suppression happens only *within* a class.
- **`cv.dnn.NMSBoxes`' score threshold must be `0.0`, not the minimum confidence.** The
  per-class thresholds already decided what is a candidate; this call must only merge
  overlaps. Passing `min(confidences)` — the obvious-looking choice — **drops the
  lowest-scoring detection on every call** (it compares strictly), which is precisely what a
  distant or partly occluded monster produces, with nothing anywhere reporting a box going
  missing.
- **A box must be clipped to the frame**, or a caller slicing the frame with it gets a short
  or empty array.

Both of those bugs were silent and caught by tests, not by looking at pictures.

`Detection` is a tuple subclass, so it unpacks as `(x, y, w, h, name, confidence)` like every
other detector's match while also carrying `.box` and `.centre` — the project's standard
contract, now tested by a third *kind* of detector. **Rate and threading are the caller's
job**, deliberately: `detect()` is a plain synchronous call, and baking a thread in would make
it untestable and useless to a caller with one still image.

`tools/diagnose_monsters.py` is the counterpart to `diagnose_ocr.py` — `--show-all` separates
"never saw it" from "saw it and was unsure", `--compare-labels` scores the model against the
boxes you drew. **That number is measured on the frames it trained on**, so a poor score is a
*data* problem, not a capacity one — a floor, not a measure of live performance.

### `tools/calibrate_meters.py` — one-time setup + live validation

`python tools/calibrate_meters.py` — countdown so you can switch to the game, screenshot, drag
a box around each orb, write `assets/meters.json`. `--preview` skips to the preview against
the saved config; `--delay` changes the countdown.

- **Its windows must be created topmost (`_show_window()`), or they never appear.** The flow is
  "switch to the game, wait out a countdown, then interact with a window" — so by the time it
  opens the *game* is foreground, and a normally-created OpenCV window opens behind it and is
  silently never seen. The symptom is "I run it and nothing happens". **Any future interactive
  tool with a countdown has this problem.** Exclusive-fullscreen can't be drawn over at all;
  the tool says so up front.
- **The live preview matters more than the box-dragging**, and is why this is interactive
  rather than a set of constants: a mistuned colour range fails by returning a
  plausible-but-wrong percentage rather than an error, so it must be validated by eye. The
  preview shows the raw region with the detected surface drawn across it, the exact pixels
  being counted and the live percentage, and reports the measured HSV of the pixels you
  selected, warning when they fall outside the preset — the signal to widen `hsv_ranges`.
- **It captures `main.py`'s `monitor_area`, imported, not its own idea of "the screen".**
  Regions are fractions, so the calibrator and the pipeline must agree what frame those are
  fractions *of* — they once didn't (EH#27). Contrast `--fast-scale`, which is deliberately a
  duplicated argument rather than an import: **a mismatch that changes the ANSWER must be
  shared; one that only changes a DISPLAY can be passed.**
- It shows each meter at **both** full resolution and the fast path's downscale
  (`--fast-scale`, default 0.3 to match `CAPTURE_SCALE`). Those should agree within a couple
  of percent; if not, the meter is too small to measure reliably at that downscale and
  `main.py` should read it from a higher-resolution frame instead.

### `legacy/` — earlier, incompatible prototype (reference only, not runnable)

A different architecture built for automating one game: `Window_Capture.py` (`win32gui`-based
per-window capture via `PrintWindow`, plus in-window→screen coordinate mapping), `vision.py`
(reusable template-matching wrapper + an interactive HSV-trackbar tuning GUI), `hsvwindow.py`
(its data holder), `pindle.py` (hardcoded click sequence), `oldmain.py` (single-threaded
integration with a Haar cascade). It references assets not in this repo and won't run — treat
it as prior art when porting ideas (the `Vision` abstraction, HSV tooling, window-specific
capture), not as runnable code.

## Measured performance facts

Profiled on dev hardware — **directional; re-profile on real target hardware before trusting
exact numbers.**

- **Everything on a per-frame path runs on ONE channel, not three, and that is a free 5x —
  not a trade.** `TM_CCOEFF_NORMED` subtracts the mean and normalises across the whole patch,
  which makes it very nearly colour-blind already: a *fully desaturated* needle and copies
  hue-rotated 30/60/90/120° **all still scored 1.000 in colour**. Detection 17.5 → 3.2ms,
  relocalization 27-33 → 5.7ms *per track*, false-positive headroom unchanged, accuracy
  measurably *better* (20/20 vs 18/20 over perturbed cases). **If a detector genuinely needs
  colour to tell two things apart, that is a job for an explicit HSV pre-mask — normalised
  cross-correlation cannot express it, so paying for three channels does not buy it.**
- **`matchTemplate` cost scales with frame area, not template size**: ~43ms/call at 0.5x,
  ~22ms at 0.35x, ~12ms at 0.25x in colour; ~3.2ms at 0.3x greyscale. It is no longer the
  dominant cost — **capture is.**
- **Screen capture sets the frame rate, and with `mss` it is GDI's blit, which no tuning
  reaches.** `sct.grab` ~13.6ms at 1920x1080, of which raw `BitBlt` is ~12.7ms;
  `CAPTURE_SCALE` doesn't touch it (the grab is full-resolution; the resize is 0.3ms) and
  GDI-side `StretchBlt` measured 12.3ms — all of the cost is the blit, none the readback.
  **Not vsync** (this display runs at 239Hz; the earlier "suspiciously close to one refresh"
  reading was a coincidence). The way past it is a different frame source.
- **Wrap `mss`'s buffer with `np.frombuffer(img.raw, ...)`, never `np.array(img)`** — the
  latter copies the whole ~8MB BGRA frame through the array interface every frame (~4ms, and
  400MB/s of bandwidth every other thread then shares). It is a *view* over a buffer the
  library may reuse, so consume it before the next `grab()`.
- **When two cheap operations commute, do the one that SHRINKS the data first.** Downscaling
  before stripping alpha is bit-for-bit identical (verified: max difference 0 over a full
  frame) and runs the colour conversion over 576x324 instead of 1920x1080.
- **A resize or convert with identity parameters is real work** — `cv.resize` at `fx=1.0` is a
  full-resolution copy, not a no-op (~3.7ms per OCR capture to produce an identical array).
- **Measured and rejected, so it doesn't get re-tried**: grabbing small sub-regions around
  each track instead of the whole screen (one 700x530 grab is 6.3ms vs 17.3ms full-screen, but
  *three* cost 22ms — per-call overhead dominates, so it only pays at one track); and capping
  OpenCV's thread count to leave CPU for the game (`matchTemplate` barely parallelises here —
  3.12ms at 16 threads vs 3.26ms at 1).
- **A single real OCR call costs far more than a synthetic benchmark suggests.** Against a
  mostly-blank test image ~150-300ms; against busy gameplay **~0.9s** — Tesseract's
  sparse-text cost scales with visual complexity, not pixel count. **Measure against the real
  thing before tuning around it.**
- `CAPTURE_SCALE` (0.3) is the image-matching accuracy/speed knob — lower is faster but
  shrinks small on-screen items toward unrecognizable.

## Project layout

```
main.py                  # the one entry point - the pipeline, and the Diablo II integration
user_config.txt          # USER-EDITABLE: potion thresholds, teleport key, settles, game name

core/                    # THE ENGINE. Knows nothing about any particular game.
  actions.py             #   mouse + keyboard: click, click-when-seen, type, press, wait-until
  frame_source.py        #   where pixels come from - DXGI or mss, with graceful fallback
  game_state.py          #   HUD meter reading - health/mana as a 0.0-1.0 number
  localize.py            #   "where on this map is the camera looking?"
  monster_detection.py   #   neural detector: find the classes a model was trained on
  overlay.py             #   transparent click-through output layer (Windows-only)
  presence.py            #   "is this reference art on screen?" - the in-play check
  slots.py               #   what is in each of a row of fixed cells - the potion belt
  text_detection.py      #   OCR: find listed names, or read a line of text as-is
  user_config.py         #   loader/validator for user_config.txt
  window_region.py       #   locating a window's client area, so regions can anchor to it

routes/                  # GAME-SPECIFIC scripted sequences, built on core/. Never imports main.
  pindle.py              #   the Pindle run: new game -> red portal -> temple doorway -> fight

tools/                   # run by hand, never by the pipeline
  build_route_map.py     #   stitch route screenshots into assets/routes/<name>.png/.json
  calibrate_meters.py    #   one-time setup: define + live-validate the HUD meters
  label_monsters.py      #   draw the boxes the monster detector trains on; --import frames
  train_monster.py       #   labelled frames -> assets/monsters/monsters.onnx
  diagnose_monsters.py   #   "why wasn't this monster detected?" - and model vs. your labels
  diagnose_ocr.py        #   "why wasn't this item detected?" - what Tesseract actually read
  diagnose_quit.py       #   "why didn't F3 click Save and Exit?" - the guards, live
  diagnose_lobby.py      #   "why was the game name misread?" - dumps the crop it OCR'd

tests/                   # none of them need the game running
  fixtures/              #   REAL frames the tests measure against - committed, see its README
  test_auto_collect.py   #   teleport decision, item ranking, retry cadences, UI panel filter
  test_game_state.py     #   meter fill measurement + Smoother
  test_label_monsters.py #   the YOLO label file: round trip, empty-vs-missing, stable ids
  test_monster_detection.py # letterbox mapping, class-aware NMS, graceful degradation
  test_next_game.py      #   name increment/validation, lobby form location, OCR field read
  test_overlay.py        #   panel layout (needs a display; skips otherwise)
  test_pindle_fight.py   #   the monster plate, motion spots, and the fight loop
  test_potions.py        #   the potion decision, and the belt-driven key resolution
  test_presence.py       #   presence.py + the in-play gate
  test_quit_game.py      #   the quit_game sequence + click_when_seen/wait_until
  test_route.py          #   map localization on held-out frames + the walks' decisions
  test_slots.py          #   the cell-colour reader: real belts, empty slots, cannot-read
  test_targets.py        #   targets.txt parsing + span ranking
  test_user_config.py    #   user_config.py parsing/validation

assets/                  # data, not code - edit these instead of the source
  image1.png             #   reference ("needle") image for template matching
  targets.txt            #   OCR item names + auto-collect/colour tags
  meters.json            #   HUD meter regions/colours (written by calibrate_meters.py)
  belt.json              #   the potion belt's four slots + the colour of each supply
  in_play.json/.png      #   HUD art proving the game is actually in play
  inventory.json/.png    #   the inventory panel's title bar + the region it covers
  stash.json/.png        #   the same for the stash - add a file per panel, no code needed
  save_and_exit.json/.png#   the Esc menu's Save and Exit button
  routes/                #   route maps + paths, written by tools/build_route_map.py
  zelScreenshots/        #   owner's scratch space for sharing screenshots mid-session.
                         #   GITIGNORED and cleared at will - NEVER point a test at it

docs/
  updates.txt            #   the changelog, and the source of truth for version numbers
  todolist.md            #   what to work on next, ideas, open questions, settled decisions
  Error_history.txt      #   bugs that took real effort, with root cause + fix + lesson
  design_notes.md        #   the long-form rationale behind everything in this file
  monster_detection_plan.txt

legacy/                  # earlier, incompatible prototype - reference only
requirements.txt
README.md / LICENSE / CLAUDE.md
```

**Why the layout is shaped this way, and where a new file goes.** The split is the
architecture, not filing: `core/` is the engine, `main.py` is the Diablo II integration, and
**the import direction enforces it** — `main.py` imports `core`, and `core` imports nothing
back. **If something in `core/` ever needs to import `main`, that is the design going wrong**,
not an import problem to solve.

- A new **detector** is a new module in `core/`. Core's modules do not import *each other*
  either (see Detector independence) — that is what made moving them into a package safe.
- A new **game-specific sequence** goes in `routes/`, which imports `core` the same way
  `main.py` does and **never imports `main`**. `main.py` hands a route what it needs (a frame
  grabber, `actions_allowed`, `in_play`) as plain callables, which is also what lets
  `test_route.py` drive one against saved frames with no pipeline running.
  (`quit_game`/`next_game` predate `routes/` and still live in `main.py` — moving them is the
  "Split `main.py`" item in the todo list.)
- **Tools and tests are run directly** (`python tests/test_x.py`), so each begins with a
  two-line `sys.path` bootstrap — `sys.path[0]` is the script's own folder, not the root.
- **A test never reads `assets/zelScreenshots/`.** It is gitignored scratch space cleared
  whenever it suits the owner, and a test pointed at it does not fail when a file disappears —
  it *skips*, which is indistinguishable from passing. **That has already happened twice**,
  and one of them let a refactor break `test_quit_game.py` unnoticed for long enough that
  nothing caught it until a real fixture was committed. Real frames go in `tests/fixtures/`,
  committed, with an entry in its README.
- **Fixtures are masked, not shrunk**: everything outside the regions the tests search is
  blacked out at the **same frame dimensions**, so every fraction and coordinate is unchanged
  while the files roughly halve. The rule is *preserve the numbers a test asserts on, not just
  the answers* — the first attempt masked the orb corners out of the lobby frames, every
  True/False still matched, and the in-play score fell from 0.272 to 0.038, which would have
  left the margin test measuring black pixels.

**There is exactly one `main.py`, at the repo root.** If a second entry point becomes
genuinely necessary (a GUI mode, a camera-input mode), prefer a flag on the existing `main.py`
or a distinctly-named new file — avoid ending up with two files named `main.py` again
(`legacy/` used to have one).

## Key things to watch for when editing

- Anything reading frames must handle **BGR, not RGB** — OpenCV convention throughout.
- The pipeline's threads are infinite loops with no clean shutdown beyond the quit hotkey; new
  threads follow the same **daemon-thread + bounded-queue** pattern so the app still exits.
- Match threshold (`0.60`) and `max_results` are hand-tuned — **changing the needle image or
  resize factor generally requires re-tuning both.**
- **Don't add game-specific logic into `core/`** — window titles, item images and key bindings
  belong in `main.py`, `routes/` or `assets/`.
- Watch for anything that would silently hurt FPS or add per-frame overhead (unnecessary
  `.copy()`s, growing data structures, blocking I/O in a hot loop) — **profile or reason
  through the cost before merging**, per the performance priority.
- **`None` is a third answer everywhere in this project** (see above) — preserve that split in
  anything new.

## Working with the project owner

The owner has a Computer Engineering degree but limited professional software development
experience. **Proactively flag mistakes or anti-patterns rather than quietly fixing them**,
explain *why* an approach is recommended (especially performance/threading/OS-portability
tradeoffs), and prefer straightforward, readable solutions over clever ones — **call out
clearly when a less-obvious technique is being used and why it is justified.**
