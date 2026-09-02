# 👁️ Hunter's Eye

**A real-time computer vision engine that watches a screen (or camera) and finds whatever you point it at.**

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue)
![Python](https://img.shields.io/badge/python-3.x-yellow)
![Version](https://img.shields.io/badge/version-0.013-informational)
![Status](https://img.shields.io/badge/status-active%20development-brightgreen)
![License](https://img.shields.io/badge/license-PolyForm%20Strict%201.0.0-lightgrey)

---

## What it is

Hunter's Eye is a lightweight, real-time detection engine built with Python and OpenCV. Give it a reference image, a piece of text, or a region of a HUD, point it at a live feed, and it tells you what's there — a bounding box and center point for anything it finds, a `0.0`–`1.0` reading for anything it measures — fast enough to keep up with a live video game and light enough to run without a beefy machine.

It's built on a simple idea with a lot of range: **anything that can be seen can be detected.** The engine never reads memory, hooks into a process, or inspects network traffic — it only ever looks at pixels. That's the constraint that keeps the same core usable against any program, and eventually against a camera pointed at the real world.

## What it does today

The engine is organized around four seams, each independently replaceable. All four are built and running.

**👁️ See — where the pixels come from**
Full-screen capture with two interchangeable backends: GPU-side Desktop Duplication (DXGI) where available, and a portable fallback everywhere else, including an automatic mid-session downgrade if the fast path is lost. A camera source drops into the same seam.

**🔍 Find — four independent detectors, four different questions**

| Detector | Answers | Built on |
|---|---|---|
| Template matching | *Where is this picture?* | Normalized cross-correlation + non-max suppression |
| Text detection | *Where is this word?* | OCR, fuzzy-matched against a plain-text watch list |
| Meter reading | *How full is this?* | HSV masking + liquid-surface detection |
| Presence check | *Is this here at all?* | Region-restricted template match → a boolean |

They are deliberately **self-contained and know nothing about each other** — each is the right tool in a different situation, and the project needs all of them available at once. Adding a watched name is a line in a text file, not a code change or another per-frame template pass.

**🧠 Judge — turning readings into decisions**
Measured values feed a rules layer that's driven entirely by a user-editable config file: *"when this named meter drops below this level, do this, then wait this long."* Every value is validated, and every bad one falls back to a named default with a warning, because a config file must never be able to stop a program that runs unattended.

**🖱️ Act — doing something about it**
A generic mouse and keyboard layer: click until a target is gone, wait for something to appear and click it once, wait until a condition holds. Actions are gated behind two independent safety guards — *is the target application actually focused*, and *is it actually showing what I think it's showing* — because the same keystroke that heals you in one context types into a text box in another.

**🖼️ Show — a transparent, click-through overlay**
Detections are drawn directly over the live application in a borderless always-on-top window that input passes straight through, so whatever is underneath stays fully controllable. An optional debug panel shows live sensor readings, and says so loudly when a reading is stale, unreadable, or uncalibrated.

## Why it's interesting

- ⚡ **Real-time performance is the whole point.** A multi-threaded producer/consumer pipeline keeps a slow detector from ever stalling a fast one. Measured on dev hardware: the capture path is deliberately paced rather than run flat out, holding its target frame rate on **~0.42 CPU cores instead of 3.56** — the same work for a fraction of the cost, because a detector that tanks the frame rate of whatever it's watching is a failed detector.
- 📏 **Decisions are measured, not guessed.** Nearly every tuning constant in the project traces back to a number someone actually recorded — running detection on one channel instead of three turned out to be ~5x cheaper with *no* measurable accuracy loss; running OCR in-process instead of shelling out was ~4x faster; a candidate optimization that profiled badly was written down as rejected so it doesn't get retried.
- 🛡️ **Failure modes are designed, not discovered.** A sensor that can't read reports *"cannot read"*, never a plausible-looking number — the distinction between "unknown" and "zero" is enforced end to end, because a consumer that confuses the two acts on maximum urgency at exactly the wrong moment.
- 🧩 **Built to grow.** The engine knows nothing about any particular game. Game-specific knowledge lives in data files and integration code, never in a detector — the litmus test for every change is *"could this still work pointed at a security camera, with only a different config file?"*
- 📓 **Documented like a real project.** A versioned changelog, a working to-do log, and a bug history recording root cause and lesson for every problem that took real effort to track down.

## Current test bed

The engine is being stress-tested against **Diablo II: Resurrected** — a demanding, low-latency environment for validating detection accuracy and frame rate under real pressure. It currently runs a full sensor → decision → action loop live: reading HUD meters, watching for named items on the ground, collecting them, drinking potions when a meter drops, and running scripted UI sequences that verify each step's effect before taking the next one.

The game is the *test*, not the product. Everything it exercises — anchoring regions to a window, reading a level off a gauge, confirming you're looking at what you think you're looking at — is exactly what a robot pointed at a room will need.

## Where it's headed

- 🔤 Searching for on-screen **text**, not just images — ✅ done
- 🖱️ A full automation layer — ✅ done: mouse, keyboard, condition-driven sequences, and safety guards
- 📊 Reading **values** off a screen, not just positions — ✅ done
- 🎯 **Object detection via a trained model** (person/entity detection) — benchmark first, integrate second
- 📷 A **camera frame source**, the real test of whether these seams are genuine or imaginary
- 🍎 A native **macOS overlay** — the one genuinely OS-specific piece left
- 🖼️ A user-friendly **GUI** so anyone can point the engine at a target without touching code
- 🤖 Porting the engine into a **robotics project** — a patrolling security robot reusing the same detectors against a live camera feed
- 📦 Packaging as a standalone cross-platform application

## Tech

Python · OpenCV · NumPy · Tesseract OCR · DXGI / GDI screen capture · Win32 layered windows · multi-threaded real-time pipeline design

## Repo map

```
main.py       the entry point, and the game-specific integration
core/         the engine — capture, detectors, actions, overlay, config
tools/        run by hand: calibration and diagnostics
tests/        eight suites, none of which need the game running
assets/       data, not code — edit these instead of the source
docs/         changelog, to-do log, and bug history
legacy/       an earlier prototype, kept for reference
```

---

*This repository is a portfolio project under active development — implementation details are intentionally kept high-level. Source-available for viewing under the [PolyForm Strict License 1.0.0](LICENSE); no rights are granted to copy, modify, redistribute, or commercially use this code without permission.*
