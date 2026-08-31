"""
Action layer: turning a decision into physical mouse and keyboard input.

This is the "action" seam described in CLAUDE.md (move mouse / press key / click) -
deliberately generic. It knows nothing about Diablo II, item names, potions or OCR;
it moves the mouse and clicks based on positions a caller feeds it, presses keys a
caller names, and reports back what happened. Callers (see main.py's
run_auto_collect() and run_potion_drinking()) own all of the game-specific meaning.

A THEME RUNS THROUGH BOTH FUNCTIONS HERE, and it is the main thing to know before
adding a third: the obvious convenience call from an input library is repeatedly the
wrong one, and it fails SILENTLY - it sends something, raises nothing, and the game
ignores it. pyautogui.click() collapses press and release into a single zero-duration
event a per-frame poll can miss; pyautogui's key press hardcodes the scancode to 0,
which a game reading raw input sees as empty. Both were established by reading the
library source and then measuring the delivered Win32 event, not by assuming. Expect
to do the same for whatever gets added next.
"""
import time

import keyboard
import pyautogui


KEY_HOLD_SECONDS = 0.05 #how long a key stays down before releasing - same reason as CLICK_HOLD_SECONDS
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


def press_key(key, hold=KEY_HOLD_SECONDS):
    """Presses and releases one key, holding it down for `hold` seconds.

    Generic on purpose - it knows nothing about potions, belts or Diablo II. A caller decides
    what the key means and when to send it (see main.py's run_potion_drinking()).

    USES THE `keyboard` PACKAGE, NOT pyautogui, AND THAT IS NOT INTERCHANGEABLE HERE. Both end up
    at Windows' keybd_event, but with a decisive difference, confirmed by reading each library's
    source rather than assuming:

        pyautogui  keybd_event(vkCode, 0,    KEYEVENTF_KEYDOWN, 0)   <- scancode hardcoded to 0
        keyboard   keybd_event(vk,     code, event_type,        0)   <- real scancode

    A game reading the keyboard through DirectInput or Raw Input looks at the SCANCODE, not the
    virtual key code, so pyautogui's zero means the keypress arrives empty and the game does
    nothing - with no error anywhere to say why. This is the keyboard-side twin of the mouse
    finding documented on click_until_gone() above: the convenience call looks right, sends
    something, and is silently ignored by the one program it was aimed at.

    HOLDING IT DOWN FOR A BEAT IS ALSO DELIBERATE, for exactly the reason CLICK_HOLD_SECONDS
    exists: a press and release landing in the same instant can fall between two of a game's
    per-frame input polls and be missed entirely. `hold` is long enough that no reasonable poll
    rate can straddle it.
    """
    keyboard.press(key)
    time.sleep(hold)
    keyboard.release(key)
