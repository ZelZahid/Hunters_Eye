"""Tests for the quit_game sequence: button location, and the generic primitives under it.

WHY THIS EXISTS: this is the first scripted sequence, and next_game() will be built on top of it.
Its dangerous failure is not "it did not work" - it is clicking the WRONG menu button, because all
five share identical frame art and differ only in their words. A whole-button template scored
0.900 on the right one and 0.888 on a wrong one, a margin of 0.013, and nothing about that looks
wrong from outside: the sequence reports success and the game carries on running.

Frames come from tests/fixtures/, which is committed. NOTE: tests/fixtures/esc_menu.png does not
exist yet, so sections 3-5 do not run - see the message they print.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glob
import os
import sys
import time

import cv2 as cv
import numpy as np

from core import actions
import main

failures = 0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(REPO_ROOT, "assets")


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


print("1. The button config loads and is shaped as expected")
b = main.save_and_exit_button
check("assets/save_and_exit.json loads", b is not None)
if b is None:
    sys.exit(1)
#One reference: there is exactly one Save and Exit button to find. (The in-play check has two,
#for occlusion - see assets/in_play.json.)
check("exactly one reference", len(b.references) == 1)
ref = b.references[0]
check("threshold is set", 0.0 < ref.threshold < 1.0)
check("the search region is a menu-sized slice, not the whole frame",
      0 < ref.search_region[2] < 1.0 and 0 < ref.search_region[3] < 1.0)

print("\n2. Nothing on screen -> no position, and never a crash")
check("a None frame -> None", main._find_save_and_exit(None) is None)
check("flat black -> None", main._find_save_and_exit(np.zeros((1080, 1920, 4), np.uint8)) is None)
rng = np.random.default_rng(11)
check("random noise -> None",
      main._find_save_and_exit(rng.integers(0, 80, (1080, 1920, 4), dtype=np.uint8)) is None)
check("a 3-channel frame also works (BGR, not just BGRA)",
      main._find_save_and_exit(np.zeros((1080, 1920, 3), np.uint8)) is None)

menus = glob.glob(os.path.join(REPO_ROOT, "tests", "fixtures", "esc_menu.png"))
if not menus:
    print("\n3-5. Real Esc-menu checks - NO FIXTURE, so the button tests are NOT running.")
    print("     These cover the trap that a whole-button template scores 0.900 on the right")
    print("     button and 0.888 on a wrong one. Drop a full-screen Esc-menu screenshot at")
    print("     tests/fixtures/esc_menu.png to turn them back on.")
else:
    print("\n3. On a real Esc menu it finds the RIGHT button")
    img = cv.imread(menus[0])
    h, w = img.shape[:2]
    pos = main._find_save_and_exit(img)
    check(f"the button is found ({pos})", pos is not None)
    if pos:
        fx, fy = pos[0] / w, pos[1] / h
        #Measured centre of "Save and Exit" on this frame. The other buttons sit at y = 0.368
        #(Options), 0.499 (Return to Game), 0.580 (Loot Filter), 0.645 (Chronicle) - so a wrong
        #button is not a near miss here, it is a clearly different number.
        check(f"it is the Save and Exit row (y={fy:.3f}, want ~0.434)", abs(fy - 0.434) < 0.02)
        check(f"horizontally centred (x={fx:.3f}, want ~0.500)", abs(fx - 0.500) < 0.03)
        for name, other_y in (("Options", 0.368), ("Return to Game", 0.499),
                              ("Loot Filter", 0.580), ("Chronicle", 0.645)):
            check(f"it is NOT {name} (y={other_y})", abs(fy - other_y) > 0.03)

    print("\n4. The threshold sits between the right button and the best wrong one")
    #The number that makes this safe. If a change narrows this, the sequence starts clicking
    #whatever else is in the menu, and reports success while doing it.
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    tpl = ref.template_at(gray.shape[1] / ref.source_width)
    rx, ry, rw, rh = ref.search_region
    H, W = gray.shape
    x0, y0 = int(rx * W), int(ry * H)
    win = gray[y0:int((ry + rh) * H), x0:int((rx + rw) * W)]
    res = cv.matchTemplate(win, tpl, cv.TM_CCOEFF_NORMED)
    _, best, _, loc = cv.minMaxLoc(res)
    masked = res.copy()
    masked[max(0, loc[1] - tpl.shape[0]):loc[1] + tpl.shape[0], :] = -1
    _, wrong, _, _ = cv.minMaxLoc(masked)
    check(f"right button {best:.3f} > threshold {ref.threshold} > wrong button {wrong:.3f}",
          wrong < ref.threshold < best)
    check(f"margin {best - wrong:.3f} is comfortable (>0.2)", best - wrong > 0.2)

    print("\n5. Matching at the fast path's 0.3x would NOT be safe - why it takes its own capture")
    small = cv.cvtColor(cv.resize(img, (0, 0), fx=0.3, fy=0.3), cv.COLOR_BGR2GRAY)
    tpl_s = ref.template_at(small.shape[1] / ref.source_width)
    Hs, Ws = small.shape
    win_s = small[int(ry * Hs):int((ry + rh) * Hs), int(rx * Ws):int((rx + rw) * Ws)]
    res_s = cv.matchTemplate(win_s, tpl_s, cv.TM_CCOEFF_NORMED)
    _, best_s, _, loc_s = cv.minMaxLoc(res_s)
    m_s = res_s.copy()
    m_s[max(0, loc_s[1] - tpl_s.shape[0]):loc_s[1] + tpl_s.shape[0], :] = -1
    _, wrong_s, _, _ = cv.minMaxLoc(m_s)
    check(f"at 0.3x the margin is only {best_s - wrong_s:.3f}, vs {best - wrong:.3f} at full res",
          (best - wrong) > (best_s - wrong_s))

print("\n6. click_when_seen waits for the target, clicks once, and gives up cleanly")
#Driven with fake callbacks so nothing touches the real mouse.
clicks = []
orig_move, orig_down, orig_up = actions.pyautogui.moveTo, actions.pyautogui.mouseDown, actions.pyautogui.mouseUp
actions.pyautogui.moveTo = lambda x, y, **k: clicks.append(("move", x, y))
actions.pyautogui.mouseDown = lambda **k: clicks.append(("down",))
actions.pyautogui.mouseUp = lambda **k: clicks.append(("up",))
try:
    seen = {"n": 0}

    def appears_on_third_look():
        seen["n"] += 1
        return (500, 400) if seen["n"] >= 3 else None

    clicks.clear()
    ok = actions.click_when_seen(appears_on_third_look, timeout=2.0, poll_interval=0.01)
    check("returns True once the target appears", ok is True)
    check("it waited rather than clicking immediately", seen["n"] >= 3)
    check("clicked exactly once", [c[0] for c in clicks] == ["move", "down", "up"])
    check("clicked at the reported position", clicks[0][1:] == (500, 400))

    clicks.clear()
    started = time.monotonic()
    ok = actions.click_when_seen(lambda: None, timeout=0.3, poll_interval=0.05)
    check("returns False when it never appears", ok is False)
    check("and clicked nothing at all", clicks == [])
    check("and did not hang", time.monotonic() - started < 2.0)

    #A paused attempt must not click, and must not burn the timeout either.
    #A pause that never clears must still END. It deliberately does not consume the timeout, so
    #without a separate bound this is an infinite loop - which is how it was first written, and
    #this test is what found it. auto-collect's is_paused is "snoozed OR not safe to act", and
    #the second half stays true for as long as the game is not focused.
    clicks.clear()
    started = time.monotonic()
    ok = actions.click_when_seen(lambda: (10, 10), timeout=0.3, poll_interval=0.02,
                                 is_paused=lambda: True, pause_budget=0.2)
    took = time.monotonic() - started
    check("a permanently paused attempt gives up instead of hanging", ok is False)
    check(f"and it gave up promptly ({took:.2f}s)", took < 2.0)
    check("a paused attempt clicks nothing", clicks == [])

    #Same hazard in the older function, reachable since actions_allowed() joined its is_paused.
    started = time.monotonic()
    ok = actions.click_until_gone(lambda: (10, 10), timeout=0.3, poll_interval=0.02,
                                  is_paused=lambda: True, pause_budget=0.2)
    took = time.monotonic() - started
    check("click_until_gone also gives up rather than hanging", ok is False)
    check(f"and promptly ({took:.2f}s)", took < 2.0)

    #But a pause that DOES clear must not fail the attempt - that is the whole point of pausing.
    state = {"n": 0}
    def briefly_paused():
        state["n"] += 1
        return state["n"] < 4
    clicks.clear()
    ok = actions.click_when_seen(lambda: (7, 8), timeout=1.0, poll_interval=0.01,
                                 is_paused=briefly_paused, pause_budget=5.0)
    check("a brief pause suspends the attempt rather than failing it", ok is True)
    check("and it still clicked once afterwards", [c[0] for c in clicks] == ["move", "down", "up"])
finally:
    actions.pyautogui.moveTo, actions.pyautogui.mouseDown, actions.pyautogui.mouseUp = orig_move, orig_down, orig_up

print("\n7. wait_until sequences on a condition, not a clock")
state = {"n": 0}


def true_on_fourth():
    state["n"] += 1
    return state["n"] >= 4


check("returns True once the condition holds", actions.wait_until(true_on_fourth, timeout=1.0, poll_interval=0.01))
check("returns False on timeout", actions.wait_until(lambda: False, timeout=0.15, poll_interval=0.05) is False)
check("an already-true condition returns immediately", actions.wait_until(lambda: True, timeout=5.0))

print("\n8. quit_game refuses to act when the guards say no")
#It must not press Esc into another program, or into a lobby with a text field focused.
saved = main.actions_allowed
try:
    main.actions_allowed = lambda: False
    pressed = []
    saved_press = actions.press_key
    actions.press_key = lambda k, **kw: pressed.append(k)
    try:
        check("returns False when actions are not allowed", main.quit_game() is False)
        check("and does not press anything", pressed == [])
    finally:
        actions.press_key = saved_press
finally:
    main.actions_allowed = saved

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
