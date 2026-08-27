# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hunter's Eye is a real-time on-screen object detection tool: it grabs the screen, runs template matching to find a target image ("needle") on it, and draws bounding boxes around matches. It's a personal portfolio project (Python + OpenCV), currently a prototype/proof-of-concept rather than a packaged application.

**This is intentionally more than a Diablo II tool.** The end goal is a cross-platform (Windows + macOS), purely pixels-in computer-vision engine: point it at a screen or camera feed, tell it what to look for (a reference image, or a piece of on-screen text), and it reports every match as a bounding box plus the match's center point (so a caller can e.g. move a mouse there or aim a servo there). It never reads a game's memory, process internals, or network traffic — detection is strictly "look at pixels," which is exactly what keeps it usable against any game (or any camera) instead of being tied to one title.

Diablo II: Resurrected is the current test bed for validating detection accuracy and driving simple mouse/keyboard automation — it is not the product. Two other consumers are already planned and should shape design decisions now, even though neither is built yet:
- A future GUI product where a non-technical end user supplies a reference image or text string plus an action (move mouse here / press this key), with this repo's detection engine as the backend.
- A robotics project (a patrolling security robot) that reuses the same detection engine against a live camera feed to spot people instead of game sprites.

Treat anything Diablo-II-specific (item/potion reference images, cascade classifiers, `pindle.py`-style scripted click sequences) as example/integration code, not something to design the core engine's abstractions around.

## Design priorities

In order, because they can conflict and the tradeoff should always be made deliberately, not by accident:

1. **Stability** — no crashes, no silent hangs. This is meant to run unattended alongside a game (or eventually on a robot with no one watching the console), so it can't be flaky.
2. **Performance (FPS)** — this must never noticeably slow down whatever it's watching, and must run acceptably on modest, non-"beefy" hardware. This is the top practical constraint the project owner cares about — a correct detector that tanks game FPS is a failed detector. If a change trades accuracy for speed (or vice versa), call that tradeoff out explicitly rather than making it silently.
3. **Portability** — Windows and macOS both need to work from the same codebase. OS-specific code (e.g. `pywin32`/`win32gui` window capture) must stay isolated behind something that can degrade gracefully on platforms that don't have it, not be sprinkled through core logic.
4. **Flexibility for future targets** — nothing in the core detection path should assume "this is Diablo II," because the same code needs to make sense pointed at a webcam later. Expect these seams to matter even before they're formally built out:
   - **frame source** — full-screen grab (`mss`, current), a specific window (`win32gui`, `legacy`), or eventually a camera (`cv.VideoCapture`).
   - **detector** — template matching (current); OCR for on-screen text is an explicitly planned near-term addition; a trained model (Haar cascade, or a small object/person detector) for the robotics use case later.
   - **match result** — bounding box + center point. Keep this contract stable; both future automation and the future GUI depend on it.
   - **action** — move mouse / press key / click. Not built in the current root pipeline yet; `legacy/pindle.py` is a rough, hardcoded preview of what this becomes.

   None of this needs formal plugin interfaces today, but avoid choices that would be painful to walk back later — e.g. don't assume `win32gui` is always importable, and don't hardcode game-specific window titles, item images, or key bindings into files outside of an obviously example/integration area.

## Versioning

`updates.txt` is the changelog and the source of truth for version numbers — read it before assuming what "the current version" means. Versioning starts at `0.001` and increments by `0.001` per significant update, working toward `1.000` ("Hunter's Eye complete," which is intentionally far off). Each version has a matching git tag (`v0.001`, `v0.002`, ...) on the exact commit for that state — **when asked to roll back to a known-working version, check out that tag; don't guess from raw commit history.** When cutting a new version: append an entry to `updates.txt` (date + a short description of what changed, in the same style as existing entries), commit, then create an annotated tag (`git tag -a v0.XXX -m "..."`) on that commit.

`Error_history.txt` logs significant bugs hit during development with root cause + fix + a one-line lesson each — **check it before debugging something that feels familiar** (e.g. anything touching `overlay.py`'s Win32 layered-window code, OCR/Tesseract error handling, or shared capture-resolution constants); it may already be documented there with the fix ready to reapply. Append a new entry there (same format as existing ones) any time a bug takes real effort to track down, not just quick typo fixes.

## Running

No build step (no `setup.py`/`pyproject.toml`). Dependencies are listed in `requirements.txt`; install with:

```
python -m pip install -r requirements.txt
```

`pywin32` is Windows-only and gated behind a `sys_platform == "win32"` marker in `requirements.txt`, so the same file installs cleanly on macOS/Linux too (the `legacy/Window_Capture.py` module that needs it just won't import there). **When you add a new import to any script in this repo, add the matching package to `requirements.txt` in the same change.**

Run the current (root-level) version:

```
python main.py
```

Press `q` in the OpenCV display window to stop; final average FPS is printed on exit.

There are no tests, linter configs, or CI in this repo.

## Architecture

### Root `main.py` — current version, producer/consumer pipeline

Three background daemon threads plus a main-thread output loop, connected by two bounded `queue.Queue`s (maxsize 3, drop-oldest-when-full) plus a lock-protected shared variable:

1. `get_screenshot()` — grabs the full virtual screen via `mss`, converts BGRA→BGR, downscales by `CAPTURE_SCALE`, pushes to `screenshot_queue`.
2. `detect_objects()` — the fast path. Pulls from `screenshot_queue`, runs `cv.matchTemplate` against a single hardcoded needle image (`assets/image1.png`), collapses overlapping matches via `cv.dnn.NMSBoxes` (**not** `cv.groupRectangles` — OpenCV 5 removed that from its Python bindings), reads whatever `detect_text()` last published from `shared_text_tracks` (behind `text_tracks_lock`, never blocks waiting on it), merges both rectangle sets, and pushes `(frame, rectangles)` to `detection_queue`. This thread's speed is governed only by capture + `matchTemplate` cost — OCR never touches it.
3. `detect_text()` — the slow path, fully decoupled from thread 2 on purpose (see "Why OCR has its own thread" below). Runs its **own independent `mss` capture loop** at `OCR_CAPTURE_SCALE` (not `CAPTURE_SCALE` — see "Why OCR needs its own capture resolution" below), runs a full OCR pass via `text_detection.find_text_matches()` at most every `OCR_INTERVAL_SECONDS`, and in between re-localizes each already-found match every loop iteration via `_relocalize_track()` (a small local `cv.matchTemplate` search around the item's last position) so the box tracks the item smoothly instead of jumping only when OCR refreshes. Converts match coordinates from `OCR_CAPTURE_SCALE` space to `CAPTURE_SCALE` space before publishing to `shared_text_tracks` (consumers only ever deal in `CAPTURE_SCALE` coordinates). New matches are logged to the console (`Found '<name>' at center (x, y)`).
4. `run_overlay()` — **runs on the main thread, not a spawned one** (Tk's event loop must own whichever thread created its window). Pulls from `detection_queue`, rescales rectangles from `CAPTURE_SCALE` coordinates back to real screen pixels (`scale_to_native = 1 / CAPTURE_SCALE`), and draws them via `overlay.Overlay` — see below. `main()` calls this and falls back to `run_debug_window()` (the old plain OpenCV window, kept for platforms the overlay doesn't support) if `Overlay` raises `NotImplementedError`.

The needle image, monitor geometry, target item list, and queues/shared state are module-level globals set up at import time — there's no class/config object here yet.

**Why OCR has its own thread**: it used to run inline inside `detect_objects()`, throttled to every Nth frame. That still periodically stalled the fast image-matching path for the OCR call's full ~120-150ms cost (pytesseract launches a new `tesseract.exe` subprocess per call — see `text_detection.py`'s docstring), capping overall FPS well below what image matching alone could do. Moving it to its own thread that merely *publishes* results asynchronously means `detect_objects()`'s throughput is no longer coupled to OCR timing at all — measured on dev hardware, this took the pipeline from ~25 FPS to ~44-46 FPS with OCR active either way.

**A single real OCR call against actual gameplay costs far more than a synthetic benchmark suggests — measure against the real thing before tuning around it.** Earlier profiling (against a mostly-blank synthetic test image) suggested ~150-300ms per Tesseract call; measured against actual busy gameplay it was **~0.9s**. Tesseract's sparse-text segmentation cost scales with visual complexity, not just pixel count, so a cluttered real game screen is far more expensive than a quiet test image. `VIEWPORT_TOP_MARGIN`/`VIEWPORT_BOTTOM_MARGIN` in `main.py` crop OCR calls to the gameplay viewport, excluding the HUD (health/mana, inventory, minimap, clock) at the top/bottom edges — item labels never render there, and that chrome is disproportionately expensive per pixel (small icons/numbers) for zero benefit. Measured effect: ~0.9s → ~0.42s per call, over 2x, for the cost of a slice (no extra processing). This is a general HUD-vs-viewport assumption applicable beyond Diablo II, not a hardcoded game-specific hack — but it IS an assumption: an item rendering very close to the top/bottom edge could go undetected; narrow the margins if that happens. A cheaper-input pre-filter (MSER, tried first) was measured and rejected: it cost ~240ms on its own against real gameplay, and the combined bounding box of all its candidates covered 99% of the frame (unrelated UI text scattered at the top and bottom forced it wide) — not worth its own overhead once measured, which is why the simpler fixed-margin crop is what shipped instead.

**`OCR_INTERVAL_SECONDS` must be well above a single OCR call's own cost, not near or below it.** Set too low (0.3s was tried and measured, back when a call cost ~0.9s pre-viewport-crop), the "wait between calls" throttle does nothing in practice — by the time one OCR call finishes, the interval has usually already elapsed, so OCR ends up running back-to-back on nearly every loop iteration. That starves `_relocalize_track()` (the cheap, fast tracking step that's supposed to keep the box glued to a moving/panning target) of any chance to run — confirmed via a loop-rate diagnostic print in `detect_text()` (`[detect_text loop rate: ...]`), which dropped to ~6-9 Hz at 0.3s vs ~19-21 Hz at `1.5`. Symptom when this is wrong: the box tracks fine while standing still (position isn't changing anyway) but loses or lags badly behind the target the moment the camera pans. Current value (`0.7`) is tuned against the post-viewport-crop call cost (~0.42-0.6s) — if the crop margins or OCR call cost change again, re-check this against the new real cost, don't just leave it.

**Time a periodic action's "next run" from when it *starts*, not when it *finishes*.** `last_ocr_time` used to be stamped after the OCR call completed — since the interval check is `elapsed_since(last_ocr_time) >= OCR_INTERVAL_SECONDS`, this meant the real gap between successive scans was `interval + the call's own duration` (~1.2s with `interval=0.7` and a ~0.5s call), not just the interval as intended, silently doubling worst-case discovery latency. Both versions look correct on casual inspection. Caught with a live diagnostic that measures the actual real-world gap between occurrences (`[scan-to-scan gap: ...]` in `detect_text()`), not just logging each occurrence — confirmed ~1.2s before the fix, ~0.7s (matching the interval exactly) after. Fixed by stamping `last_ocr_time` at the start of the call, so the interval can elapse *while* the call runs.

**`_relocalize_track()` must refresh its own reference patch on every successful match, not keep reusing the original OCR-time snapshot.** An earlier version always matched against the patch captured at the last full OCR refresh, however long ago that was. That's fine while nothing nearby changes, but real gameplay isn't static (flickering torches, animated water/grass, other moving characters) — the longer a reference patch goes unrefreshed, the more the live scene has drifted from it, degrading match confidence. Now each successful relocalization re-crops the patch from the *current* frame at the newly-found position, so the reference is always at most one loop iteration (a few ms) stale instead of up to `OCR_INTERVAL_SECONDS` stale.

**The position OCR returns is already stale by the time you get it — by the OCR call's own duration, not just "since the last loop tick."** The two fixes above weren't enough on their own; live logging (per-track `TRACK LOST` prints) showed relocalization failing almost immediately after nearly every OCR refresh, at very low confidence (0.08-0.30 — a genuine miss, not a near-threshold one). Cause: a single OCR call takes ~150-300ms, so the coordinates it returns describe where the item was *before* that call started — the first relocalization attempt has to bridge that entire call duration on top of a normal loop iteration, which is a much bigger gap than "one iteration's worth of movement." Fix in `detect_text()`: immediately grab one more frame right after OCR completes and relocalize against *that* before publishing, instead of leaving the OCR-time position to go stale until the next loop iteration. Also raised `TRACK_SEARCH_MARGIN` to `250` for headroom on the remaining steady-state gap. Re-verified with real gameplay logging: lost-track events dropped from nearly every refresh cycle to a small handful across a full test with heavy movement and direction changes. General lesson: any time a slow periodic call's result gets used as if it were "current," account for the call's own duration explicitly — don't assume the next scheduled step naturally catches up.

**Why OCR needs its own capture resolution**: `CAPTURE_SCALE` was dropped to 0.3 purely to speed up `matchTemplate` for icon matching. `detect_text()` briefly shared that same downscaled frame for OCR too — and broke text recognition entirely, because shrinking a 1280x800 screen to 0.3x leaves in-game text only a few pixels tall, well below what Tesseract can read. Template matching and OCR have fundamentally different resolution needs; sharing one frame between different-shaped detectors is a trap worth remembering when adding a third one later. Since OCR only runs a couple of times a second (`OCR_INTERVAL_SECONDS`), it can affordably run its own separate, much higher-resolution capture (`OCR_CAPTURE_SCALE`, currently `1.0`) without touching the fast path at all.

**Measured performance facts** (profiled on dev hardware — treat as directional, re-profile on real target hardware before trusting exact numbers):
- `cv.matchTemplate` cost scales with frame area, not template size: ~43ms/call at 0.5x downscale (960x540), ~22ms at 0.35x, ~12ms at 0.25x. This is the single biggest lever over image-matching FPS.
- `mss` screen capture (grab + BGRA→BGR + resize) costs ~17-20ms/frame on this hardware regardless of downscale target — this is close to a hard floor for this capture method, capping image-matching alone around ~50-58 FPS even with `matchTemplate` cost driven to near-zero. Going faster than that would mean capturing a smaller region, or a different (likely OS-specific) capture backend — not something to reach for without a concrete reason, since it costs portability.
- `CAPTURE_SCALE` (currently `0.3`) is the tuning knob for the image-matching accuracy/speed trade-off — lower is faster but shrinks small on-screen items toward unrecognizable.

### `overlay.py` — transparent, click-through output layer (Windows-only for now)

Replaces showing a separate mirrored debug window: `Overlay` is a borderless, always-on-top Tk window using two Windows-specific tricks layered together — Tk's `-transparentcolor` attribute (a real Windows Tk feature: makes one chosen color both invisible and click-through) for the background, plus a Win32 `WS_EX_TRANSPARENT` style bit (set via `pywin32` after the window exists) so that even the drawn green box outlines never intercept a click — input always reaches the game underneath. Neither trick exists on macOS; `Overlay.__init__` raises `NotImplementedError` there, and `main()` catches that to fall back to `run_debug_window()`. A real macOS overlay would need a different implementation (e.g. an `NSWindow` via `pyobjc` with `ignoresMouseEvents` and a clear background) — nobody should try to make the Tk approach itself cross-platform, the mechanism is inherently OS-specific.

This mechanism has bitten twice already; both fixes are live in `Overlay.__init__` now, but re-verify both properties (see-through **and** click-through) if this code is touched again — testing only one can pass while the other silently regresses, since they're governed by different pieces of Win32 state:
- **Renders solid black instead of see-through**: calling `win32gui.SetWindowLong` to add `WS_EX_TRANSPARENT` after Tk has already set up `-transparentcolor` resets the layered window's transparency data, because Tk has no idea the window was touched behind its back. Fix: immediately re-assert it via `win32gui.SetLayeredWindowAttributes(hwnd, colorkey, 0, win32con.LWA_COLORKEY)` right after the `SetWindowLong` call.
- **Looks transparent but doesn't click through** (mouse input hits the overlay instead of the game/window underneath): two compounding causes. (1) `winfo_id()` can return an inner Tk drawing window rather than the actual outer OS-level frame that governs hit-testing — styling the wrong (child) window silently does nothing, so walk up via `win32gui.GetParent()` first. (2) Even on the right HWND, `SetWindowLong` alone doesn't reliably make Windows re-evaluate hit-testing for the new style — force it with `win32gui.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)` right after. Verified with an isolated test harness (a plain Tk window with a click-counting button, overlaid and clicked via `pyautogui` at a known screen coordinate on a monitor away from any real game window) — visual screenshot inspection alone can't catch this class of bug, since the overlay looks identical whether clicks pass through or not.

Because the overlay is click-through by design, it can never hold keyboard focus — a normal Tk key-binding for quitting silently wouldn't fire. Quitting instead uses a **global hotkey** via the `keyboard` package (`main.py`'s `run_overlay()`, bound to `'q'`), the same pattern `legacy/pindle.py` already used for its own hotkey.

### `text_detection.py` — OCR-based detector

This is the first concrete instance of the "detector" seam called out in Design Priorities above — a second way to find things on screen, independent of and unknown to the image-matching code. It exists because image templates don't scale to "watch for hundreds of item names": each new template needs its own `matchTemplate` pass, so cost grows **linearly** with vocabulary size, while OCR's cost is **constant** regardless of vocabulary size (read the text once, compare the result against a list — nearly free). This was measured directly, not assumed: a single `matchTemplate` call at OCR's native-resolution viewport costs ~21ms, so 20 templates/frame (~424ms) already roughly equals OCR's cost, and 80 templates/frame (realistic for ~40 items with a couple of color-rarity variants each) costs ~1.7s — over 3x slower than OCR. **Template matching only wins below roughly 15-20 total templates; above that, and especially at "hundreds of items" scale, OCR is the correct architecture** — this is why OCR call *speed* (below) was worth investing in rather than switching detectors when more raw speed was needed.

`find_text_matches()` makes **exactly one** OCR call per invocation, over the whole frame, using the same word/line-grouping approach regardless of backend (`_group_words_into_lines()`) — deliberately not one call per candidate region. An earlier version cropped candidate regions (found via OpenCV's MSER detector) and OCR'd each separately; since each OCR call pays a fixed overhead regardless of image size, calling it per-region (up to a dozen times a frame) multiplied that overhead into a 10-20x FPS hit. Recognized text is cleaned and fuzzy-matched (`difflib.get_close_matches`) against `assets/targets.txt` (one item name per line — this is the file to edit when adding new items to watch for, no code changes needed).

**Two OCR backends, tried in order at import time**, both producing the same normalized `(text, left, top, width, height, block_num, par_num, line_num)` word list before a shared `_group_words_into_lines()`:
1. **`tesserocr`** (fast path) — talks to the Tesseract engine in-process via compiled bindings, reusing one long-lived `PyTessBaseAPI` instance (`_tesserocr_api`, module-level) across every call. Measured **~3.5-4.5x faster** against real gameplay (~0.1-0.26s vs ~0.42-0.6s per call) — this isn't just subprocess-launch overhead disappearing, it's also skipping the temp-file I/O round-trip pytesseract's subprocess model requires and, most importantly, not re-initializing the whole Tesseract engine from scratch on every single call the way a fresh subprocess would. `GetTSVText(0)` returns the exact same column layout as `pytesseract.image_to_data`'s DICT output (level/block/par/line/word numbers, left, top, width, height, confidence, text) — level `5` rows are actual words; other levels are page/block/paragraph/line placeholder rows with no text.
2. **`pytesseract`** (portable fallback) — used if `tesserocr` isn't installed (no matching prebuilt wheel for the current Python version/platform — see `requirements.txt`) or its `PyTessBaseAPI` init fails for any reason. Slower, but works anywhere the Tesseract binary itself is installed, regardless of platform or Python version.

`tesserocr` needs a **prebuilt wheel matching the exact Python version and platform** — there's no PyPI wheel for Windows (only a source distribution requiring a full C++ build toolchain + Tesseract dev libraries, which failed here — see `Error_history.txt` #15). The prebuilt wheels at `github.com/simonflueckiger/tesserocr-windows_build` cover current Python versions including very new ones (this project used the Python 3.14 wheel the same day 3.14 was current). It also needs `_find_tessdata_dir()` to locate the `tessdata` folder (currently only auto-detects the default Windows Tesseract install path) — separate from `pytesseract.pytesseract.tesseract_cmd`, which points at the `.exe`, not the data folder.

`_group_words_into_lines()` pads each line's bounding box by `LABEL_PAD_X`/`LABEL_PAD_Y` before returning it — Tesseract's word boxes cover only the text glyphs, but D2R (and most games) render a semi-transparent background panel behind item labels that's visibly larger than the text itself, so an unpadded box looks noticeably too small next to it. This is a fixed pixel margin, not automatic panel-edge detection (e.g. via color/contour analysis around the text) — deliberately, since D2R's panel is often similar in darkness to the surrounding terrain (dungeons, caves), making "find the edge of the dark rectangle" unreliable across different scenes. Values are estimates from visual testing against real gameplay; re-tune if the box still doesn't match the panel.

Tesseract itself is a separate program, not a pip package (see `requirements.txt` for per-OS install instructions) — `pytesseract` just shells out to it. Two layers of defense against that external dependency being unreliable: `text_detection.py` checks once at import time whether Tesseract is installed at all and disables text matching (one-time warning) rather than crashing; and each individual OCR call is wrapped in a `try/except TesseractError`, because the installed Windows build has been observed to intermittently throw a spurious `TesseractError` (an "ObjectCache leak" warning during its own internal cleanup, unrelated to whether OCR actually succeeded) — that's treated as "skip this scan," not a fatal error.

Call-site throttling (how often `find_text_matches()` gets called at all) is `main.py`'s job via `OCR_INTERVAL_SECONDS`, not this module's — see `detect_text()` above.

### `legacy/` — earlier, more modular prototype (kept for reference, not run by default)

This is a different, incompatible architecture built for automating a specific game (Diablo II: Resurrected) rather than generic screen detection:

- `Window_Capture.py` — `WindowCapture` class: finds a game window by title via `win32gui`, computes crop offsets for borders/titlebar, and captures that window specifically (via `PrintWindow`) instead of the whole screen. Also maps in-window coordinates back to screen coordinates for mouse actions.
- `vision.py` — `Vision` class: reusable template-matching wrapper (load a needle image, `find()` matches/groups rectangles, `get_click_points()`, `draw_rectangles()`/`draw_crosshairs()`), plus an interactive HSV-trackbar GUI (`init_control_gui`/`apply_hsv_filter`) for tuning color-based masks.
- `hsvwindow.py` — plain data holder (`HsvWindow`) for the HSV min/max + add/sub trackbar values used by `Vision`'s HSV filter.
- `pindle.py` — game-specific automation script (`pindle_script`, `nihlathak_portal_search`) that uses `WindowCapture` + `Vision` + `pyautogui` to click through a sequence of screen positions.
- `oldmain.py` — the integration point tying the above together (loads a Haar cascade classifier and several `Vision` instances for health/mana potions, binds `F9` via the `keyboard` module to trigger `pindle_script`, runs a single-threaded detect/draw/act loop keyed on a `health` variable).

`legacy` code references assets that aren't in this repo (`cascade_classifier/cascade/cascade.xml`, `items/potions/*.jpg`, `items/misc/NithalakPortal.jpg`) and won't run as-is — treat it as prior-art/reference when porting ideas (e.g. the `Vision` class abstraction, HSV tooling, or window-specific capture) into the current `main.py` pipeline, not as runnable code. `legacy/assets/` holds a few leftover test images (`img_1.jpg`, `zombie_1.jpg`, `zombie_1_processed.jpg`) — none of them are loaded by any script; they're just artifacts from earlier testing.

## Project layout

```
main.py              # the one entry point — real-time detection pipeline
text_detection.py    # OCR-based detector (see above)
overlay.py           # transparent click-through output layer, Windows-only (see above)
assets/               # reference ("needle") images + targets.txt (OCR target item names)
legacy/               # earlier, incompatible prototype (see above) — reference only
  legacy/assets/       # unused leftover test images from that prototype
requirements.txt
README.md / LICENSE / CLAUDE.md
```

There is exactly one `main.py`, at the repo root. `legacy/` used to be `version1/` and also had its own `main.py`; that duplicate was deleted since it was a strict subset of the root pipeline's screen-capture thread with no detection logic of its own. If a second entry point becomes genuinely necessary later (e.g. a GUI mode, a camera-input mode), prefer a flag/mode on the existing `main.py` or a distinctly-named new file — avoid ending up with two files named `main.py` again.

## Key things to watch for when editing

- Anything reading frames must handle BGR (not RGB) — OpenCV convention throughout.
- The root pipeline's threads are infinite loops with no clean shutdown signal besides the `q` keypress in `draw_and_output`; if you add new threads, follow the same daemon-thread + bounded-queue pattern so the app still exits on `q`.
- Match threshold (`0.60` in root `main.py`) and `max_results` are tuned by hand — changing the needle image or resize factor generally requires re-tuning both.
- Don't add game-specific logic (Diablo II window titles, item images, key bindings) into the core capture/detect/draw pipeline — keep it in clearly-separated example/integration code so the engine stays reusable for other games and, eventually, non-game camera input.
- Watch for anything that would silently hurt FPS or add per-frame overhead (unnecessary `.copy()`s, growing data structures, blocking I/O in a hot loop) — profile or reason through the cost before merging, per the performance priority above.

## Working with the project owner

The project owner has a Computer Engineering degree but limited professional software development experience. When helping on this repo: proactively flag mistakes or anti-patterns rather than quietly fixing them, explain *why* an approach is recommended (especially performance/threading/OS-portability tradeoffs), and prefer straightforward, readable solutions over clever ones — call out clearly when a less-obvious technique is being used and why it's justified.
