# Hunter's Eye — To-Do / Idea Log

**What this file is for:** remembering what to work on next, and why. Ideas fade fast between sessions — this is where they go, so a future you (or a future Claude session, which reads this file) can pick the thread back up without re-deriving everything.

**How to use it:**

- **NOW holds exactly one thing.** If NOW has three things in it, it isn't a "now."
- Move items between sections freely. Delete nothing — move finished work to DONE so it's obvious what's already been tried and how it turned out.
- Half-formed ideas belong in **Parking lot**. Writing down a bad idea costs nothing; losing a good one costs a lot.
- Dates are absolute (`2026-08-28`), never "yesterday" or "last week" — this file gets read months later.

See [`updates.txt`](updates.txt) for the version changelog, [`Error_history.txt`](Error_history.txt) for bugs already diagnosed, and [`CLAUDE.md`](CLAUDE.md) for architecture and design rules.

---

## Now

- [ ] **Potion drinking.** The smallest possible consumer of `game_state`, and the right first one: read health, press a belt key below a threshold. Proves the whole sensor → decision → action loop end to end with almost no new code.
  - needs: a key-press action in `actions.py` (currently mouse-click only)
  - needs: a cooldown, so it can't spam the whole belt in one second
  - watch out: drinking doesn't refill instantly, so the threshold check must wait out the heal before re-triggering
  - the `F5` panel is now the way to watch this happen live — health bar drops, potion fires, bar climbs

---

## Next

*In order — each depends on the one above it.*

- [ ] **Guard against acting while the game isn't on screen.** Nothing currently checks this. When you alt-tab, meters read whatever is behind the game (typically 0%) and `run_auto_collect` has only the `F4` snooze — no check that its clicks are landing in the game at all. A potion layer would see 0% health the moment you tab out and dump the belt; auto-collect could click into a browser. Noticed 2026-08-28 while verifying meter readings, where several samples were accidentally taken against a browser window.
  - `window_region.is_foreground(title)`, then: meters report `None` (not `0.0`), actions pause
  - open question: a *windowed* game that is visible but unfocused can still be read correctly, so focus is a proxy for visibility, not the same thing — decide whether the guard is a hard stop or a panel indicator
  - **do this before potion drinking**, since it is the difference between "drinks when low" and "drinks the whole belt when you tab out"

- [ ] **Pindle scripted run.** Waypoint → red portal → clear → collect → return.
  - sequence steps on **detection, not `sleep()`** — "wait until the portal is on screen," not "wait 3 seconds." Timing-based scripts break on any lag spike.
  - `legacy/pindle.py` is the rough prior art (hardcoded click positions)
  - new file, e.g. `routes/pindle.py` — explicitly Diablo II integration code, kept out of the core engine per CLAUDE.md's detector-independence rule
  - the portal / waypoint / UI buttons are fixed art → **template matching** is the right detector for those, not OCR and not a neural net
  - needs: an "am I in town / in the pit" state check of some kind
  - needs: an abort condition (low health, timeout, unexpected screen)

- [ ] **Monster detection.** Start with a **benchmark, not an integration**: does YOLOv8/v11-nano hit acceptable FPS on this machine?
  - ONNX Runtime, standalone script, imports nothing from the pipeline
  - measure ms/frame on CPU, and on GPU if available
  - budget: ~30–60 ms/frame CPU, ~5–10 ms GPU. CPU would roughly halve current FPS — a real tradeoff to decide deliberately, not stumble into
  - if the numbers are bad: smaller input size, run detection every Nth frame, or a cheaper classical detector for a narrower job
  - this is also the exact code path the security-robot project needs (person detection is a pretrained class — no training data required)

- [ ] **Combat target selection.** Needs monster detection working first.
  - pick a target: nearest? lowest health? most dangerous?
  - needs positional reasoning (distance from character center)
  - the hardest item on this list by a wide margin — expect several attempts and its own tuning constants

---

## Later

*Real goals, not scheduled yet.*

- [ ] **macOS overlay.** `overlay.py` is Windows-only (Tk `-transparentcolor` + Win32 `WS_EX_TRANSPARENT`). macOS needs a different implementation entirely — an `NSWindow` via `pyobjc` with `ignoresMouseEvents` and a clear background. Do **not** try to make the Tk approach cross-platform; the mechanism is inherently OS-specific. `main.py` already falls back to the debug window on non-Windows.
- [ ] **Camera frame source** (`cv.VideoCapture`). The "frame source" seam from CLAUDE.md. Once this exists, every detector should work against a webcam with no changes — that's the real test of whether the seams are genuine or imaginary.
- [ ] **Window-specific capture** instead of full-screen. `legacy/Window_Capture.py` already does this via `win32gui`/`PrintWindow`. Would cut capture cost (currently ~17–20 ms/frame, the FPS floor) by grabbing a smaller region, but costs portability — needs a graceful non-Windows path.
- [ ] **Replace module-level globals in `main.py` with a config object.** Needle image, monitor geometry, scales, and shared state are all set up at import time. Fine now; will hurt when a GUI needs to set any of it at runtime.
- [ ] **Clean shutdown.** Threads are daemons with no shutdown signal — the process just exits. Acceptable today, but a GUI or a robot can't work that way.
- [ ] **The GUI product.** Non-technical user supplies a reference image or text string plus an action (move mouse / press key), with this engine as the backend.
- [ ] **Security robot reuse.** Same engine, live camera, detecting people instead of sprites. Mostly falls out of "camera frame source" + "monster detection" being built generically.

---

## Parking lot

*Unvetted ideas — cheap to write down, may never happen.*

- [ ] **Two-pane Zellij reading surface**: left pane = Claude working, right pane = `tail -f` on a file that gets the prose/findings only, without tool-call noise. Claude Code writes to one stdout stream so it can't be split directly, but a `Stop` hook could extract the last assistant message to a file. Partially investigated 2026-08-28, paused before implementing.
- [ ] **Re-tune `BRIGHT_TEXT_THRESHOLD`** (`text_detection.py`, currently 150) if labels get missed in bright areas — it was tuned 2026-08-28 on two dark dungeon/town frames only. A single global threshold may not hold in snow, desert or lava zones; adaptive thresholding is the fallback if it doesn't, at some speed cost.
- [ ] **Watch for more look-alikes.** `targets.txt` now lists 17 (16 low runes + small Rejuvenation Potion). Better OCR will keep surfacing items that were previously too garbled to false-match — anything sharing a suffix with a target is a candidate. `diagnose_ocr.py` shows them as `MATCH + AUTO-COLLECT` on a line you didn't expect.
- [ ] **Use Tesseract's per-word confidence score.** Already read and then discarded in `_get_words_tesserocr` / `_get_words_pytesseract`. The next lever against short-name false positives — a 2–3 letter rune name Tesseract genuinely misreads out of unrelated pixels, which no fuzzy-match tuning can catch since the string really is there.
- [ ] **Leptonica stderr noise** (`Error in pixScanForForeground: invalid box` / `Error in boxClipToRectangle: box outside rectangle`). Seen 2026-08-28 during a live run. Printed straight to fd 2 from Tesseract's C imaging library, so Python's `try/except` around the OCR call cannot see it and it does not raise — benign, one skipped layout region per occurrence. Could not reproduce: silent across a real 874-word screen grab, edge-clipped text, 1x1 through 300x1 frames, noise, and blank frames. Only worth chasing if detections are actually being missed. **Do not "fix" it by redirecting stderr around the OCR call** — fd 2 is process-wide, and four threads print to it, so that would swallow the FPS and track logging too.
- [ ] **Re-check `OCR_INTERVAL_SECONDS`** (`main.py`, currently `0.4`). `CLAUDE.md` documents `0.7` and the rule that it must sit well ABOVE a single OCR call's own cost; it is now below what a call costs on a busy real frame (~0.1–0.26s measured with `tesserocr`, and higher under load). Left alone during the v0.008 performance pass on purpose — it is a detection-latency knob, not a waste knob, so changing it would trade quality for FPS, which that pass deliberately did not do. Worth re-deriving against a real gameplay call cost with `OCR_DEBUG_TIMING = True`.
- [ ] **Cap the relocalization loop rate.** The OCR thread now only captures when it has something to do, but while items ARE tracked it still relocalizes as fast as it can (~29 Hz measured). `CLAUDE.md` documents ~19–21 Hz as the known-good rate, so there is probably headroom to cap it and hand the CPU back to the game. Not done in v0.008 because it is a genuine quality knob (a slower loop means more drift per step against `TRACK_SEARCH_MARGIN`), unlike everything else in that pass.
- [ ] **Belt potion counting** — how many potions are left in each slot. Template matching per slot, or color detection. Needed for "go restock" logic.
- [ ] **Town vs. combat detection.** Probably a template match on a UI element that only appears in one context. Would make scripted routes much more robust.
- [ ] **Chicken / panic exit** — leave the game instantly below a health threshold. Trivial once potion drinking works, and it's the difference between a survivable mistake and a dead character.
- [ ] **Log detections to a file** for later review, so a failed run can be diagnosed after the fact instead of by watching the console live.
- [ ] **Multi-template support.** `main.py` loads exactly one needle (`assets/image1.png`). The engine should take a list. Cheap to add, blocked on nothing.
- [ ] **Motion detection as a fourth detector type** — cheap, and useful for a security camera where "something changed" matters more than "what is it."

---

## Open questions

*Decisions to make, not tasks.*

- [ ] **Online vs. offline Diablo II.** Input automation on battle.net realms is bannable (Warden actively looks for it); offline / single-player has no such exposure. Worth deciding deliberately before the automation gets more capable.
- [ ] **Does `main.py` eventually split into modules?** It's the hub — capture, detect, OCR, auto-collect, overlay, and now game state all live there. Splitting would make parallel work easier and imports clearer; it would also churn a file that currently works. Not urgent.
- [ ] **Should `assets/meters.json` be committed or gitignored?** It's machine-specific (resolution + UI scale). Committing documents a working example; ignoring avoids confusion if the resolution changes. Currently neither — it doesn't exist yet.

---

## Known limitations

*True today — don't rediscover these.*

- `game_state.py` has **never been validated against a real screen.** Synthetic tests only. See [Now](#now).
- The overlay is **Windows-only.** macOS falls back to a plain debug window.
- Only **one template image** is supported (`assets/image1.png`), and its match threshold (`0.60`) is hand-tuned to it.
- OCR viewport margins (`VIEWPORT_TOP_MARGIN` / `VIEWPORT_BOTTOM_MARGIN` in `main.py`) assume item labels never render in the top 8% or bottom 25% of the screen. An item very close to a screen edge could go undetected.
- Capture costs **~17–20 ms/frame regardless of downscale** — the FPS floor for the current `mss` full-screen approach, roughly a 50–58 FPS ceiling.
- **No clean shutdown**; threads are daemons killed at process exit.
- Only **one test file** exists (`test_game_state.py`). Nothing else is covered.

---

## Done

- [x] **2026-08-29 — overlay flicker follow-up.** Raising FPS silently raised the overlay's repaint rate with it (34→51/s), because it redraws once per detection frame — a fullscreen always-on-top *layered* window, so every repaint costs the compositor and the bill lands on the game, invisible in our own FPS number (`Error_history.txt` #38). Fixed by capping repaint rates independently of detection (`BOX_REDRAW_HZ` 30 / `PANEL_REDRAW_HZ` 10), dropping the panel's now-redundant 9x text outline, and ignoring sub-4px box jitter. Worst case 73.3 → 31.5 ms/s of repaint work; a stationary item with jitter went ~25 repaints/s → 0.3/s. **Answered a parked question along the way:** `PANEL_TEXT_OUTLINE` existed to fight *magenta* fringing and stopped earning its keep the moment `TRANSPARENT_COLOR` became near-black — measured no contrast difference with 8 / 4 / 0 offsets. Also measured and rejected: in-place `coords()` canvas updates (no faster than delete-and-recreate). User confirmed the click delay is gone and `F4` made no difference to it, which rules out auto-collect as its cause.
- [x] **2026-08-29 — v0.008 — performance pass. FPS ~34 → ~51 idle / ~43 with three items tracked**, measured A/B against v0.007 on the same machine. Nothing traded away: every change is either bit-identical output or measured equally/more accurate. Almost all of it was work the pipeline did and threw away — a colour matcher that was already colour-blind (5x for nothing), an OCR thread capturing full-resolution frames it never looked at ~45x/sec, and `np.array()` duplicating 8MB per frame on two threads. See `Error_history.txt` #35–#37 for the three traps, and `updates.txt` v0.008 for what was measured and rejected.
- [x] **2026-08-28 — `game_state.py` validated against the real game.** Health and mana both calibrated and tracking live. Full-res vs fast-path agreement: health 1.4 pts, mana 0.5 pts — both inside the ~2 pt bar, so `main.py` can safely keep reading meters off the downscaled detection frame. Took four bugs to get there: the calibrator captured all three monitors while `main.py` captured one (`Error_history.txt` #27), its windows opened behind the game (#26), and the hand-drawn box included animated scenery, making the reading noise rather than health (#28). `game_state.py`'s own measurement code needed no changes — it was right all along.
- [x] **2026-08-28 — v0.006** — `game_state.py`: read health/mana orbs as a 0.0–1.0 value. Generic meter reading (region + color + fill direction), config in `assets/meters.json`, `calibrate_meters.py` for setup and live validation, `test_game_state.py` for verification. Two bugs caught before going live — see `Error_history.txt` #19 and #20.
- [x] **2026-08-28 — v0.005** — per-item detection box colors via `targets.txt` `[color]` tags.
- [x] **2026-08-28 — v0.004** — auto-collect action layer (`actions.py`) + click-accuracy fixes.
- [x] **2026-08-27 — v0.003** — `tesserocr` OCR backend (~4x faster) + windowed FPS logging.
- [x] **2026-08-27 — v0.002** — fixed OCR item tracking during real movement.
- [x] **2026-08-27 — v0.001** — first tagged checkpoint; docs, cleanup, OCR detection added.

### Decisions already made

*So they don't get re-litigated.*

- **Detectors stay independent**, and none is ever removed for being out of focus. See CLAUDE.md → "Detector independence."
- **Templates for fixed UI art** (portals, buttons, icons); **a neural detector for monsters** (they animate, rotate, recolor, overlap — templates fail there). Neural cost is constant in class count; template cost is linear.
- **Tesla-style multi-camera BEV fusion / occupancy networks are out of scope.** The useful half is just "one net, many objects per frame" = YOLO. The rest is for driving a car with eight cameras through 3D space.
- **One Claude session at a time, not parallel agents.** The bottleneck on this project is live in-game validation, which is inherently serial and human-only.
