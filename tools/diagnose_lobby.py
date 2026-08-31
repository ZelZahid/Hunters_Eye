"""Why didn't next_game read the game name? Run this while sitting in the Diablo II lobby.

    python diagnose_lobby.py

Captures the screen through the exact path next_game() uses - same monitor, same anchor lookup,
same regions - and prints every step, then saves what it looked at as PNGs so the crops can be
inspected directly.

It exists because "it read the wrong name" has several causes that look identical from outside:
the form was located in the wrong place, the crop of the text box was off, OCR misread the
characters, or the name was read fine and the increment was wrong. Guessing between them wastes
a lot of time. Same reasoning as diagnose_ocr.py.

Nothing here clicks, types, or presses a key - it only looks.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sys

import cv2 as cv
import mss
import numpy as np

import main
from core import text_detection as td

OUT_PREFIX = "lobby_diagnostic"


def save(name, image):
    path = f"{OUT_PREFIX}_{name}.png"
    cv.imwrite(path, image)
    print(f"      saved {path}  ({image.shape[1]}x{image.shape[0]})")


def main_():
    print("Capturing the screen the way next_game() does...\n")
    with mss.mss() as sct:
        raw = sct.grab(main.monitor_area)
        frame = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
    print(f"  frame            : {frame.shape[1]}x{frame.shape[0]}")
    print(f"  capture rect     : {main.CAPTURE_RECT}")

    rect = main.anchor_rect()
    print(f"  anchor window    : {rect}")
    if rect is None:
        print("    (no anchor - regions are treated as fractions of the whole screen)")

    print(f"  in_play()        : {main.in_play()}  "
          f"(should be False in the lobby; None means the check is unavailable)")

    x0, y0, x1, y1 = main._anchor_region_pixels(main.LOBBY_FORM_REGION, frame.shape)
    print(f"\n  form search area : x {x0}..{x1}, y {y0}..{y1}")
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        print("    EMPTY - the form region does not overlap the captured frame. Nothing else "
              "below can work; check the anchor window and LOBBY_FORM_REGION.")
        return 1
    save("form", crop)

    bgr = cv.cvtColor(crop, cv.COLOR_BGRA2BGR)
    lines = td.read_lines(bgr)
    print(f"\n  every line OCR found in that area ({len(lines)}):")
    for text, (x, y, w, h) in sorted(lines, key=lambda t: t[1][1]):
        tag = ""
        if main._looks_like(text, "GAMENAME"):
            tag = "   <= used as the Game Name label"
        elif main._looks_like(text, "GAMEDESC"):
            tag = "   <= used to measure the row spacing"
        print(f"      y={y:4} x={x:4} h={h:3}  {text!r}{tag}")

    form = main._lobby_form(frame)
    if form is None:
        print("\n  FORM NOT FOUND. The 'Game Name' label was not among the lines above - so "
              "either the search area is wrong (check the region printed above against "
              f"{OUT_PREFIX}_form.png) or OCR did not read that label.")
        return 1

    print(f"\n  row spacing      : {form['row_spacing']:.1f}px  "
          f"(measured from the form's own labels, not stored)")
    print(f"  name label at    : {form['name_label']}")
    print(f"  would click name : {form['name_field']}")
    print(f"  would click pass : {form['password_field']}")

    spacing = form["row_spacing"]
    lx, ly, lh = form["name_label"]
    bx = int(lx - main.CAPTURE_RECT[0])
    by = int(ly - main.CAPTURE_RECT[1] + spacing * 0.20)
    box = frame[by:by + int(spacing * 0.62), bx:bx + int(spacing * 6)]
    print(f"\n  name box crop    : x {bx}..{bx + int(spacing * 6)}, "
          f"y {by}..{by + int(spacing * 0.62)}")
    if box.size == 0:
        print("    EMPTY - the crop falls outside the frame.")
        return 1
    save("namebox", box)
    save("namebox_big", cv.resize(box, (0, 0), fx=4, fy=4, interpolation=cv.INTER_NEAREST))

    box_bgr = cv.cvtColor(box, cv.COLOR_BGRA2BGR)
    print("\n  what OCR reads from that crop, at several thresholds:")
    print("      (the shipped value is text_detection.FIELD_TEXT_THRESHOLD = "
          f"{td.FIELD_TEXT_THRESHOLD})")
    for threshold in (90, 100, 110, 120, 130, 140, 150, 160, 170):
        raw_text = td.read_line(box_bgr, threshold=threshold)
        cleaned = main.clean_game_name(raw_text)
        marker = "  <= shipped" if threshold == td.FIELD_TEXT_THRESHOLD else ""
        print(f"      {threshold:3}  raw={raw_text!r:22} accepted={cleaned!r}{marker}")

    print("\n  what next_game() would do with the shipped threshold:")
    read_name = main._read_lobby_game_name(frame, form)
    print(f"      read       : {read_name!r}")
    preferred = main._preferred_case(read_name)
    print(f"      after case memory: {preferred!r}  (memory is {main._last_created_name!r})")
    if preferred:
        print(f"      would create : {main.next_game_name(preferred)!r}")
    else:
        print(f"      would fall back to user_config game_name: "
              f"{main.potion_config.game_name!r}")

    print("\n  name-clash dialog visible: ", main._name_clash_showing(frame))
    print("\nDone. If the read is wrong, open "
          f"{OUT_PREFIX}_namebox_big.png - if the name is not fully inside it, the crop is the "
          "problem, not OCR.")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
