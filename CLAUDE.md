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

**Tests are numbered cards** (`V-104`: category, area, card), listed with a one-line title in
`docs/test_cards.md` — read its header before adding one. `python tests/run_all.py` runs them all;
`python tests/<file>` runs one. None needs the game. **CI** (GitHub Actions, Windows,
`.github/workflows/tests.yml`) runs `run_all.py` on every pull request and push to `main`; merge
only when it is green. A new card goes in the index in the same change — card T-301 fails CI
otherwise. A committed fixture that is missing fails; it never skips. No linter.

## Architecture — the rules, compressed

Each bullet below is a rule that cost real debugging time. The measurements and the story
behind each one are in **`docs/design_notes.md`, Part 1**, under the same module heading.

### `main.py` — the pipeline and the Diablo II integration
Daemon threads joined by bounded `queue.Queue`s (maxsize 3, drop-oldest) plus lock-protected
shared state; the Tk overlay owns the **main thread**.
- `get_screenshot()` captures at `CAPTURE_SCALE` 0.3 and **paces to `TARGET_FPS` 60** — the
  goal is the same work for less CPU, not more FPS.
- `detect_objects()` is the fast path: grayscale `matchTemplate` + `cv.dnn.NMSBoxes` (**not**
  `groupRectangles`, gone in OpenCV 5); reads meters, belt and in-play off the same frame
  (colour first, then grayscale); **never blocks on OCR**.
- `detect_text()` is the slow path with its **own full-resolution `mss` capture**, grabbing
  only when the frame will be used (EH#36). Full OCR at most every `OCR_INTERVAL_SECONDS`
  (0.7, must stay well above one call's cost), relocalizing tracks in between. **Consumers only
  see `CAPTURE_SCALE` coordinates.** Sharing one frame between different-shaped detectors is a
  trap.
- Timing: schedule periodic work from when it **starts**; refresh a relocalized patch on every
  match; OCR positions are stale by the call's duration, so relocalize once more before
  publishing.
- Auto-collect: action per item is data (`[click]`/`[key:e]`), keys validated once at
  startup; items on an open (opaque) UI panel are filtered **positionally** and only while it
  is open; teleporting happens *before* `act_until_gone()`, and the re-find must wait for a
  position that has **moved** (a lost track reads as success). `_should_teleport()` stays pure.
- Potions: rules from `user_config.txt`, first match wins, **`None` fires nothing**, per-rule
  cooldowns plus a global `POTION_MIN_GAP_SECONDS` floor, readings below `ignore_below` are
  unreadable, multi-key rules alternate, unsatisfiable rules fall through (that *is* the
  backup-rule mechanism), nothing spends a rejuvenation on mana. `_potion_due()` stays pure.
- `quit_game()` / `next_game()` (F3): **every step waits on detection, never on a clock**, and
  confirms its effect. The lobby name is read with known-name resolution (OCR can't tell case
  or `0`/`O`); button templates are the **text only**. The shared Tesseract engine is **not
  thread-safe** — always behind its lock (EH#42).

### `assets/targets.txt` — OCR vocabulary
One name per line; `-` = ignore (the look-alike mechanism — **load-bearing, don't delete**),
`*` = collect, `[colour]`, `[click]`/`[key:x]`, `[exact]`. **Line order is priority, lower
wins** — regrouping silently re-ranks. `[exact]` also needs no row neighbour and no extra words
(EH#51). Duplicates warn. Delete a look-alike once it is unnecessary.

### `core/frame_source.py`
DXGI (`bettercam`, Windows, optional) with `mss` fallback — at startup *and* mid-session.
- A DXGI source is a **per-output singleton**; a second consumer must pass `backend=MSS` (EH#39).
- Use polled `grab()`, **never** `get_latest_frame()` (untimed wait = hang).
- `grab()` returns a **view** valid until the next grab; `None` on a static screen is normal.

### `core/actions.py`
Game-agnostic. `act_until_gone(get_position, act)` separates *what* from *how long*.
- Click = move, settle, press, hold, release, all `_pause=False`. Re-check and re-click are
  **different cadences** (EH#18).
- Keys go through **`keyboard`, never pyautogui** (pyautogui sends scancode 0, which games ignore
  silently).
- `AIM_SETTLE_SECONDS` > `MOVE_SETTLE_SECONDS` by design; every settle is a **hold** that
  re-asserts the cursor; both are `user_config.txt` settings.
- Retry early when the target hasn't moved (`retry_if_still=None` for fixed-camera games).
- Paused time is **bounded** (`PAUSE_BUDGET_SECONDS`, EH#41) — "doesn't count against the
  timeout" means "unbounded".

### `core/overlay.py` (Windows-only; macOS falls back to `run_debug_window()`)
- **Re-verify see-through AND click-through** after any change — different Win32 state
  (re-assert `SetLayeredWindowAttributes`; walk to the outer frame via `GetParent()`;
  `SWP_FRAMECHANGED`). Test click-through with a click-counting button.
- Repaints are capped (`BOX_REDRAW_HZ` 30, `PANEL_REDRAW_HZ` 10) and deadbanded (4px) — the
  cost lands on the **game's** FPS, not ours (EH#38). A skipped repaint must not count as drawn.
- `TRANSPARENT_COLOR` is `#010203` on purpose; it is coupled to the empty
  `PANEL_TEXT_OUTLINE_OFFSETS`. Keep the stippled panel plate, sized by measured font metrics.
- **The overlay is in our own captures**: card V-405 asserts no panel string matches
  a target (EH#49) — extend it when wording changes.

### `core/text_detection.py` — OCR
- OCR beats templates above ~15-20 names (constant vs linear cost). **One OCR call per frame.**
- **Binarize first, on the strongest colour channel** (EH#48); `BRIGHT_TEXT_THRESHOLD` 180 for
  labels over terrain, `DIM_UI_TEXT_THRESHOLD` for flat UI.
- Matching: word-runs within a line; **per-word cutoffs**, never one whole-string ratio;
  stricter `MERGED_WORD_CUTOFF` for differing word counts; shared words get one extra miss only
  at ≥4 chars, never below `SHARED_WORD_FLOOR`, never when all words are shared (EH#49);
  short words compare **by position**; spans rank **longest-match first**; **a tie reports
  nothing**.
- Fuzzy match always returns the closest *listed* name, so the fix for false positives is a
  **complete vocabulary**, not a cutoff. Loose cutoffs and listed look-alikes are a pair.
- Backends: `tesserocr` (fast, needs a matching prebuilt wheel, EH#15) then `pytesseract`.
  Missing OCR disables matching with a warning; `TesseractError` skips a scan.
- Diagnose with `tools/diagnose_ocr.py` (never saw / misread / didn't match) or `MATCH_DEBUG`.

### `core/game_state.py` — meters
- **`None` ≠ `0.0`** (EH#40); failed reads carry the last value for at most
  `MAX_CARRIED_FAILURES`; median of 5.
- Fill by **height, not area** (EH#19); surface by global consistency test (EH#20) with
  `MIN_SURFACE_SUPPORT_ROWS`; low value floor (EH#50); `hsv_ranges` is a list because meters
  change colour (poison).
- Regions are window fractions, one `Profile` per window shape; no match selects nothing.

### `core/slots.py` — the potion belt
Colour, not templates. Per-cell `None` = empty slot; whole-row `None` = can't see the belt —
**never collapse them**. Policy lives in `main.py`; an unreadable belt falls back to configured
keys.

### `core/presence.py` — in-play check
References contain **only pixels that cannot change** (EH#44); two references on opposite
corners, either suffices, failing independently; misses debounced, hits not; restricted search
regions; size-check before `cv.resize`. `actions_allowed()` needs both focus and in-play to not
say no.

### `core/window_region.py`
Client rect, not window rect; exact title beats substring. `is_foreground()` is **three-valued**
(`None` must behave as before the guard). Refresh the handle on every lookup. Anchor missing →
meters read `None`, and the panel says why (`window not found`, `RECALIBRATE`).

### `core/localize.py` + `routes/pindle.py`
Map matching assumes **pure translation** (checked on every map build). Local contrast
normalisation plus a margin over the next-best place (EH#45); screen-fixed UI is masked when
stitching (EH#46). Walks replay a recorded path from the located position; waypoint keep-out;
the portal is clicked only after its label is read; arrival is confirmed by looking; stop and
say why rather than carry on blind. `Walker.decide()` and `fight()` take their dependencies as
arguments so tests can drive them. Maps are valid only at their recorded aspect ratio.

### `routes/shuttle.py` + `tools/teleport_bot.py` — bot test mode
Teleports out and back over one spot with nobody at the keyboard, so `measure_ocr_latency.py`
has view jumps to time. Same guards as `main.py`'s actions (warmed up before the first press),
F4/End stop, a health stop and a mana wait, and a hard time limit. **Every teleport is checked by
how far the scene slid** (phase correlation): a confident zero is a miss and four in a row stop the
run; a frame pair it cannot compare is "cannot tell", never a miss. Aims correct for short
landings from the measured position, but a landing within `ON_TARGET_PX` counts as on target —
adding up every leg's measurement noise is a random walk. Run it **instead of** `main.py`, not
with it (auto-collect would fight it for the mouse).

### `core/monster_detection.py`
**One multi-class model**, never one per monster. CPU by default (`prefer_gpu` opt-in).
**Full-frame inference** (it was trained that way). Class-aware NMS with score threshold
**`0.0`**; boxes clipped to the frame. `Detection` unpacks like every other match. Rate and
threading are the caller's job. Full record: `docs/monster_detection_plan.txt`.

### `tools/calibrate_meters.py`
Windows must be created topmost or they never appear. The live preview is the point. Capture
`main.py`'s `monitor_area` by import (EH#27).

### `legacy/` — reference only, not runnable.

## Performance facts that steer decisions
Directional, measured on dev hardware.
- Per-frame matching runs on **one channel** (free 5x; `TM_CCOEFF_NORMED` is near colour-blind
  anyway — use an HSV pre-mask when colour truly matters).
- **Capture is the dominant cost**, not matching. Wrap `mss` with `np.frombuffer`, never
  `np.array`.
- Shrink before convert; identity resizes/converts are real copies.
- Real OCR on busy gameplay is ~0.9s uncropped — measure against the real thing.
- Rejected, don't retry: per-track sub-region grabs; capping OpenCV threads.

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
  shuttle.py             #   bot test mode: teleport out and back, each jump checked by looking

tools/                   # run by hand, never by the pipeline
  build_route_map.py     #   stitch route screenshots into assets/routes/<name>.png/.json
  calibrate_meters.py    #   one-time setup: define + live-validate the HUD meters
  label_monsters.py      #   draw the boxes the monster detector trains on; --import frames
  prelabel.py            #   the trained model drafts boxes on new frames, for review
  train_monster.py       #   labelled frames -> assets/monsters/monsters.onnx
  diagnose_monsters.py   #   "why wasn't this monster detected?" - and model vs. your labels
  diagnose_ocr.py        #   "why wasn't this item detected?" - what Tesseract actually read
  diagnose_quit.py       #   "why didn't F3 click Save and Exit?" - the guards, live
  diagnose_lobby.py      #   "why was the game name misread?" - dumps the crop it OCR'd
  teleport_bot.py        #   bot test mode: unattended teleports for timing tests (--measure)

tests/                   # numbered test cards - none of them need the game running
  run_all.py             #   runs every file, reports failing cards by ID (what CI runs)
  fixtures/              #   REAL frames the tests measure against - committed, see its README
  test_auto_collect.py   #   teleport decision, item ranking, retry cadences, UI panel filter
  test_card_index.py     #   docs/test_cards.md lists exactly the cards the tests run
  test_detector_contract.py # every detector: no crash on junk frames, None = cannot tell,
                         #   works on a document page (image search, OCR)
  test_frame_source.py   #   capture: DXGI -> mss fallback, re-served frames, window geometry
  test_game_state.py     #   meter fill measurement + Smoother
  test_image_search.py   #   the reference-image search, held to its pre-shortcut output
  test_label_monsters.py #   the YOLO label file: round trip, empty-vs-missing, stable ids
  test_monster_detection.py # letterbox mapping, class-aware NMS, graceful degradation
  test_next_game.py      #   name increment/validation, lobby form location, OCR field read
  test_overlay.py        #   panel layout (needs a display; skips otherwise)
  test_pindle_fight.py   #   the monster plate, motion spots, and the fight loop
  test_potions.py        #   the potion decision, and the belt-driven key resolution
  test_presence.py       #   presence.py + the in-play gate
  test_quit_game.py      #   the quit_game sequence + click_when_seen/wait_until
  test_route.py          #   map localization on held-out frames + the walks' decisions
  test_shuttle.py        #   bot test mode: scene shift on real frames, the loop in a fake game
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
  test_cards.md          #   every test card: ID + one-line title, and when tests run
  Error_history.txt      #   bugs that took real effort, with root cause + fix + lesson
  design_notes.md        #   the long-form rationale behind everything in this file
  diagrams/              #   system_map.svg (README-ready) + system_map.html - the whole system on one page
  monster_detection_plan.txt

legacy/                  # earlier, incompatible prototype - reference only
requirements.txt
README.md / LICENSE / CLAUDE.md
```

**Layout rules.** The import direction *is* the architecture: `main.py` imports `core`, and
**`core` never imports `main`**; core modules don't import each other.
- New detector → new module in `core/`. New game-specific sequence → `routes/` (never imports
  `main`; gets callables handed in). `quit_game`/`next_game` still live in `main.py` pending
  the "Split `main.py`" todo.
- Tools and tests run directly, so each starts with a two-line `sys.path` bootstrap.
- **Tests never read `assets/zelScreenshots/`** (a missing file skips, which looks like
  passing). Real frames go in `tests/fixtures/` with a README entry, **masked, not shrunk**, so
  every asserted number is preserved.
- Exactly one `main.py`; a new mode is a flag or a distinctly named file.

## Key things to watch for when editing
- Frames are **BGR**.
- New threads: daemon + bounded queue.
- Match threshold (`0.60`) and `max_results` are tuned to the needle and scale — retune both if
  either changes.
- No game-specific logic in `core/`.
- No silent per-frame cost (extra `.copy()`s, growing structures, blocking I/O) — profile or
  reason about it first.
- Preserve `None` as the third answer.

## Working with the project owner
The owner has a Computer Engineering degree but limited professional software experience.
**Flag mistakes and anti-patterns rather than quietly fixing them**, explain *why* (especially
performance, threading and portability tradeoffs), prefer readable solutions, and say so when
a less obvious technique is justified.
