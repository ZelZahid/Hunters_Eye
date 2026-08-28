# 👁️ Hunter's Eye

**A real-time computer vision engine that watches a screen (or camera) and finds whatever you point it at.**

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-blue)
![Python](https://img.shields.io/badge/python-3.x-yellow)
![Status](https://img.shields.io/badge/status-active%20development-brightgreen)
![License](https://img.shields.io/badge/license-PolyForm%20Strict%201.0.0-lightgrey)

---

## What it is

Hunter's Eye is a lightweight, real-time object detection engine built with Python and OpenCV. Give it a reference image or a piece of text, point it at a live feed, and it locates every match on screen — drawing a bounding box and reporting the exact center point of each one, fast enough to keep up with a live video game and light enough to run without a beefy machine.

It's built on a simple idea with a lot of range: **anything that can be seen can be detected.** The engine doesn't read memory, hook into applications, or depend on any single program — it only ever looks at pixels, which means the same core can point at a video game one day and a security camera feed the next.

## Why it's interesting

- ⚡ **Real-time performance is the whole point.** Multi-threaded pipeline design keeps detection running smoothly without tanking the FPS of whatever it's watching — the entire architecture is built around staying fast and light on modest hardware.
- 🖥️ **Cross-platform by design.** Built to run on both Windows and macOS from the same codebase.
- 🧠 **Purely vision-based, purely general.** No game hooks, no memory reads, no source-specific shortcuts — if it's visible, it's fair game.
- 🧩 **Built to grow.** The detection engine is being developed as reusable infrastructure, not a one-off script — the same core is planned to power everything from gaming automation to a home robotics project.

## Where it's headed

This project is under active, ongoing development. Planned directions include:

- 🔤 Searching for on-screen **text**, not just images — done
- 🖱️ A full automation layer — react to what's detected by moving a mouse, pressing a key, or triggering a custom action. An initial version already exists (auto-click on a detected match); expanding to more action types next
- 🖼️ A user-friendly GUI so anyone can point the engine at a target without touching code
- 🤖 Porting the engine into a **robotics project** — using a live camera feed to detect and track real-world objects and people
- 📦 Packaging as a standalone cross-platform application

## Current use case

Right now, the engine is being stress-tested in a real-time PC gaming environment — a demanding, low-latency testbed for validating detection accuracy and frame rate under pressure before it's generalized further.

## Tech

Python · OpenCV · multi-threaded real-time pipeline design

---

*This repository is a portfolio project under active development — implementation details are intentionally kept high-level. Source-available for viewing under the [PolyForm Strict License 1.0.0](LICENSE); no rights are granted to copy, modify, redistribute, or commercially use this code without permission.*
