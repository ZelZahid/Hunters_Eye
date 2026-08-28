"""
Action layer: turns a detected match's position into a physical mouse action.

This is the "action" seam described in CLAUDE.md (move mouse / press key / click) -
deliberately generic. It knows nothing about Diablo II, item names, or OCR; it just
moves the mouse and clicks based on positions a caller feeds it, and reports back
whether the target visually disappeared or the attempt timed out. Callers (see
main.py's run_auto_collect()) own all of the game-specific meaning.
"""
import time

import pyautogui


MOVE_SETTLE_SECONDS = 0.05 #pause between the cursor arriving and the button going down - see below
CLICK_HOLD_SECONDS = 0.05 #how long the mouse button stays down before releasing - see the comment
                           #on the mouseDown/mouseUp call below for why this can't be 0


def click_until_gone(get_position, timeout=5.0, click_interval=0.8, poll_interval=0.15, is_paused=None):
    """Clicks whatever get_position() reports is the target's current center, repeating until
    either get_position() returns None (target gone - success) or `timeout` seconds elapse with
    it still present (gave up).

    Checking "is it gone yet" (poll_interval) and actually issuing a new click (click_interval)
    are deliberately different cadences. Many games (Diablo II included) treat a click on an
    item as "walk over there and pick it up on arrival", not an instant action - re-clicking
    every 150ms while that walk is still in progress keeps re-issuing the move command, which
    can retarget the character before it ever reaches the item. Polling stays fast so success is
    detected quickly and get_position()'s own position tracking stays fresh, but a new click is
    only fired every click_interval, giving a walk-to-item time to actually complete.

    get_position: callable -> (x, y) center in real screen pixels, or None if the target
        is no longer visible.
    is_paused: optional callable -> bool. While it returns True, this pauses (no move/click,
        and the pause doesn't count against `timeout`) instead of stopping outright - lets a
        caller temporarily suppress clicking (e.g. a snooze hotkey) without losing progress
        on the current attempt.
    Returns True on success (target disappeared), False on timeout.
    """
    elapsed_active = 0.0
    time_since_click = click_interval #click immediately on the first iteration
    while elapsed_active < timeout:
        if is_paused is not None and is_paused():
            time.sleep(poll_interval)
            continue

        pos = get_position()
        if pos is None:
            return True

        if time_since_click >= click_interval:
            # Three separate steps, each with its own real gap - not one mouseDown(x, y) call:
            #
            # 1. Move first, then wait MOVE_SETTLE_SECONDS before pressing. mouseDown(x, y)
            #    repositions the cursor AND presses the button in one call, with nothing
            #    separating them - the game can receive "cursor moved" and "button down" in
            #    the same input instant and resolve the click against where the cursor WAS,
            #    not where it just arrived. Moving first and pausing gives the game a chance
            #    to actually process the new cursor position before any click event referring
            #    to it shows up.
            # 2. Then press and hold for CLICK_HOLD_SECONDS before releasing. On Windows,
            #    pyautogui.click() sends MOUSEEVENTF_LEFTDOWN and MOUSEEVENTF_LEFTUP combined
            #    into ONE mouse_event() call (verified by reading pyautogui's
            #    _pyautogui_win.py: MOUSEEVENTF_LEFTCLICK is literally
            #    MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP) - down and up in the same instant,
            #    zero time held. A game polling input once per frame can miss a press/release
            #    that lands in the same instant entirely, even with the cursor in the right
            #    place. Holding it down for a beat is long enough that no reasonable per-frame
            #    poll rate can miss it.
            #
            # _pause=False on all three skips pyautogui's own post-call sleep (default 0.1s) -
            # MOVE_SETTLE_SECONDS/CLICK_HOLD_SECONDS are the only intended delays here, not an
            # incidental side effect of the wrong default.
            pyautogui.moveTo(pos[0], pos[1], _pause=False)
            time.sleep(MOVE_SETTLE_SECONDS)
            pyautogui.mouseDown(_pause=False)
            time.sleep(CLICK_HOLD_SECONDS)
            pyautogui.mouseUp(_pause=False)
            time_since_click = 0.0

        time.sleep(poll_interval)
        elapsed_active += poll_interval
        time_since_click += poll_interval

    return get_position() is None
