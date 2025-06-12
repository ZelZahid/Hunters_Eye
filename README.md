# Hunters_Eye

Real-time on-screen object detection tool using OpenCV. Detect, track, and label anything across multiple monitors with minimal performance impact.

## 🧠 About

**Hunter’s Eye** is a high-performance, real-time object detection tool built using **Python**, **OpenCV**, and optionally **C++** for optimized speed. It allows users to select an image (screenshot or icon), then continuously scans their screen(s) to locate, highlight, and label instances of that object — all with minimal impact to system performance or game FPS.

This project supports multiple use cases:
- 🎮 **Gaming automation** (e.g., enemy or loot detection)
- 🛡 **Security monitoring** (e.g., detect unusual objects in feeds)
- 🧪 **UI testing** (e.g., visual regressions or button states)
- 🖥 **General visual screen tracking**

Key features:
- 🔍 Fast image detection via `matchTemplate` or ORB feature matching
- 🖼️ Visual feedback with bounding boxes and labels
- ⚙️ Multi-threaded architecture for smooth real-time performance
- 🧱 Modular design for optional C++ acceleration using `pybind11`
- 🖱️ User GUI (WIP) for selecting targets, toggling detection, and monitoring results

This project is also part of my personal portfolio to demonstrate real-world computer vision, threading, and system design skills. Future updates will include pretrained object detection models, better tracking, and packaging as a native Windows app for now.

