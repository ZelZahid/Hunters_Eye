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
   - **frame source** — full-screen grab (`mss`, current), a specific window (`win32gui`, `version1`), or eventually a camera (`cv.VideoCapture`).
   - **detector** — template matching (current); OCR for on-screen text is an explicitly planned near-term addition; a trained model (Haar cascade, or a small object/person detector) for the robotics use case later.
   - **match result** — bounding box + center point. Keep this contract stable; both future automation and the future GUI depend on it.
   - **action** — move mouse / press key / click. Not built in the current root pipeline yet; `version1/pindle.py` is a rough, hardcoded preview of what this becomes.

   None of this needs formal plugin interfaces today, but avoid choices that would be painful to walk back later — e.g. don't assume `win32gui` is always importable, and don't hardcode game-specific window titles, item images, or key bindings into files outside of an obviously example/integration area.

## Running

No build step (no `setup.py`/`pyproject.toml`). Dependencies are listed in `requirements.txt`; install with:

```
python -m pip install -r requirements.txt
```

`pywin32` is Windows-only and gated behind a `sys_platform == "win32"` marker in `requirements.txt`, so the same file installs cleanly on macOS/Linux too (the `version1/Window_Capture.py` module that needs it just won't import there). **When you add a new import to any script in this repo, add the matching package to `requirements.txt` in the same change.**

Run the current (root-level) version:

```
python main.py
```

Press `q` in the OpenCV display window to stop; final average FPS is printed on exit.

There are no tests, linter configs, or CI in this repo.

## Architecture

### Root `main.py` — current version, producer/consumer pipeline

Three daemon threads connected by two bounded `queue.Queue`s (maxsize 3), so a slow detection stage drops old frames instead of backing up the pipeline:

1. `get_screenshot()` — grabs the full virtual screen via `mss`, converts BGRA→BGR, downscales 0.5x, pushes to `screenshot_queue` (drops oldest frame if full).
2. `detect_objects()` — pulls from `screenshot_queue`, runs `cv.matchTemplate` against a single hardcoded needle image (`image1.png`, loaded once at import time and also downscaled 0.5x), thresholds/groups results with `cv.groupRectangles`, pushes `(frame, rectangles)` to `detection_queue`.
3. `draw_and_output()` — pulls from `detection_queue`, draws boxes, shows the OpenCV window, tracks/prints FPS, and is the loop that owns the `q`-to-quit exit condition (`main()` joins on this thread only).

The needle image, monitor geometry, and queues are module-level globals set up at import time — there's no class/config object here yet.

### `version1/` — earlier, more modular prototype (kept for reference, not run by default)

This is a different, incompatible architecture built for automating a specific game (Diablo II: Resurrected) rather than generic screen detection:

- `Window_Capture.py` — `WindowCapture` class: finds a game window by title via `win32gui`, computes crop offsets for borders/titlebar, and captures that window specifically (via `PrintWindow`) instead of the whole screen. Also maps in-window coordinates back to screen coordinates for mouse actions.
- `vision.py` — `Vision` class: reusable template-matching wrapper (load a needle image, `find()` matches/groups rectangles, `get_click_points()`, `draw_rectangles()`/`draw_crosshairs()`), plus an interactive HSV-trackbar GUI (`init_control_gui`/`apply_hsv_filter`) for tuning color-based masks.
- `hsvwindow.py` — plain data holder (`HsvWindow`) for the HSV min/max + add/sub trackbar values used by `Vision`'s HSV filter.
- `pindle.py` — game-specific automation script (`pindle_script`, `nihlathak_portal_search`) that uses `WindowCapture` + `Vision` + `pyautogui` to click through a sequence of screen positions.
- `oldmain.py` — the integration point tying the above together (loads a Haar cascade classifier and several `Vision` instances for health/mana potions, binds `F9` via the `keyboard` module to trigger `pindle_script`, runs a single-threaded detect/draw/act loop keyed on a `health` variable).

`version1` code references assets that aren't in this repo (`cascade_classifier/cascade/cascade.xml`, `items/potions/*.jpg`, `items/misc/NithalakPortal.jpg`) and won't run as-is — treat it as prior-art/reference when porting ideas (e.g. the `Vision` class abstraction, HSV tooling, or window-specific capture) into the current `main.py` pipeline, not as runnable code.

## Key things to watch for when editing

- Anything reading frames must handle BGR (not RGB) — OpenCV convention throughout.
- The root pipeline's threads are infinite loops with no clean shutdown signal besides the `q` keypress in `draw_and_output`; if you add new threads, follow the same daemon-thread + bounded-queue pattern so the app still exits on `q`.
- Match threshold (`0.60` in root `main.py`) and `max_results` are tuned by hand — changing the needle image or resize factor generally requires re-tuning both.
- Don't add game-specific logic (Diablo II window titles, item images, key bindings) into the core capture/detect/draw pipeline — keep it in clearly-separated example/integration code so the engine stays reusable for other games and, eventually, non-game camera input.
- Watch for anything that would silently hurt FPS or add per-frame overhead (unnecessary `.copy()`s, growing data structures, blocking I/O in a hot loop) — profile or reason through the cost before merging, per the performance priority above.

## Working with the project owner

The project owner has a Computer Engineering degree but limited professional software development experience. When helping on this repo: proactively flag mistakes or anti-patterns rather than quietly fixing them, explain *why* an approach is recommended (especially performance/threading/OS-portability tradeoffs), and prefer straightforward, readable solutions over clever ones — call out clearly when a less-obvious technique is being used and why it's justified.
