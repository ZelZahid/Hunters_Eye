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


TYPE_INTERVAL_SECONDS = 0.02 #gap between characters when typing into a field - see type_text()
KEY_HOLD_SECONDS = 0.05 #how long a key stays down before releasing - same reason as CLICK_HOLD_SECONDS
#HOW LONG A PAUSED ATTEMPT MAY STAY PAUSED BEFORE IT GIVES UP.
#
#Pausing deliberately does NOT consume an attempt's timeout - a brief snooze should suspend an
#attempt, not fail it. Taken alone that is an unbounded wait, and it became a real hang the moment
#an is_paused callback existed that can stay true forever: auto-collect's pause is
#"snoozed OR not safe to act", and the safety half stays true for as long as the game is not
#focused. Alt-tab in the middle of a pickup and never come back, and click_until_gone() never
#returns - the thread is not spinning (it sleeps each poll) so nothing looks wrong, it just
#silently stops collecting for the rest of the session. A snooze alone could never do this,
#because a snooze always expires; the guard added later is what made it reachable.
#
#So paused time is bounded separately from active time: an attempt may be suspended for this long
#in total, after which it gives up and reports failure. Callers re-attempt on their own schedule,
#which is the right way to resume - not by holding one attempt open indefinitely.
PAUSE_BUDGET_SECONDS = 30.0

MOVE_SETTLE_SECONDS = 0.05 #pause between the cursor arriving and the button going down - see below
CLICK_HOLD_SECONDS = 0.05 #how long the mouse button stays down before releasing - see the comment
                           #on the mouseDown/mouseUp call below for why this can't be 0


def click_until_gone(get_position, timeout=5.0, click_interval=0.8, poll_interval=0.15,
                     is_paused=None, pause_budget=PAUSE_BUDGET_SECONDS):
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
        on the current attempt. Bounded by `pause_budget` - see PAUSE_BUDGET_SECONDS.
    Returns True on success (target disappeared), False on timeout or on staying paused too long.
    """
    elapsed_active = 0.0
    elapsed_paused = 0.0
    time_since_click = click_interval #click immediately on the first iteration
    while elapsed_active < timeout:
        if is_paused is not None and is_paused():
            if elapsed_paused >= pause_budget:
                return False  #suspended too long to still be one attempt - see PAUSE_BUDGET_SECONDS
            time.sleep(poll_interval)
            elapsed_paused += poll_interval
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


def click_when_seen(find_position, timeout=8.0, poll_interval=0.1, is_paused=None,
                    pause_budget=PAUSE_BUDGET_SECONDS):
    """Waits for find_position() to report a target, then clicks it ONCE. Returns True if clicked.

    The counterpart to click_until_gone(): that one clicks something already on screen until it
    goes away, this one waits for something to appear and clicks it a single time. Between them
    they cover the two halves of driving a UI - "act on what is here" and "act when this shows up".

    WAITING ON DETECTION, NOT ON A CLOCK, IS THE POINT. The obvious version of "open a menu and
    click a button" is press-key, sleep, click-fixed-coordinates, and it breaks on the first
    frame-rate dip, loading hitch or animation that runs long - and it breaks by clicking whatever
    happens to be under those coordinates instead, which is worse than doing nothing. Polling for
    the thing itself takes exactly as long as it takes and does nothing at all if it never shows.

    ONCE, not repeatedly: a menu button is not a walk-over-and-pick-it-up (which is what
    click_until_gone's repeat cadence exists for). Clicking a button a second time lands on
    whatever replaced it - a different menu, a confirmation dialog, the game world.

    find_position: callable -> (x, y) in real screen pixels, or None if not visible yet.
    is_paused: optional callable -> bool. While true, this waits without clicking and without
        counting the time against `timeout`, matching click_until_gone's behaviour so a snooze or
        a safety guard suspends an attempt rather than failing it. Bounded by `pause_budget`,
        because "does not count against the timeout" is otherwise an unbounded wait - see
        PAUSE_BUDGET_SECONDS for the hang that produced.
    """
    waited = 0.0
    paused = 0.0
    while waited < timeout:
        if is_paused is not None and is_paused():
            if paused >= pause_budget:
                return False
            time.sleep(poll_interval)
            paused += poll_interval
            continue

        position = find_position()
        if position is not None:
            #Same three-step click as click_until_gone, and for the same measured reasons - see
            #the long comment there on why move/press/release are separated by real delays.
            pyautogui.moveTo(position[0], position[1], _pause=False)
            time.sleep(MOVE_SETTLE_SECONDS)
            pyautogui.mouseDown(_pause=False)
            time.sleep(CLICK_HOLD_SECONDS)
            pyautogui.mouseUp(_pause=False)
            return True

        time.sleep(poll_interval)
        waited += poll_interval

    return False


def wait_until(condition, timeout=5.0, poll_interval=0.1):
    """Blocks until condition() is true, or `timeout` passes. Returns whether it came true.

    The other half of sequencing on detection rather than on a clock: click_when_seen() waits for
    something to APPEAR, this waits for any condition a caller can express - most usefully "did
    that actually work?". A step that fires and never checks its own effect is how a scripted
    sequence carries on into a screen it was not written for.
    """
    waited = 0.0
    while waited < timeout:
        if condition():
            return True
        time.sleep(poll_interval)
        waited += poll_interval
    return bool(condition())


def move_to(x, y):
    """Moves the cursor without clicking.

    Separate from click_at() because "put the pointer somewhere useful" is not an action on the
    thing under it - parking the mouse where a player will want it next must never risk a click.
    """
    pyautogui.moveTo(x, y, _pause=False)


def click_at(x, y):
    """Clicks once at a screen position. Same three separated steps as everywhere else here -
    move, settle, press, hold, release - see click_until_gone() for why none of them collapse."""
    pyautogui.moveTo(x, y, _pause=False)
    time.sleep(MOVE_SETTLE_SECONDS)
    pyautogui.mouseDown(_pause=False)
    time.sleep(CLICK_HOLD_SECONDS)
    pyautogui.mouseUp(_pause=False)


def press_combo(combo):
    """Presses a key combination written the way a person would say it: 'ctrl+a', 'alt+f4'.

    Separate from press_key() because the `keyboard` package handles the whole chord itself -
    pressing and releasing the modifiers in the right order - and doing that by hand with
    press/release pairs is how modifier keys get left stuck down.
    """
    keyboard.send(combo)


def type_text(text, interval=TYPE_INTERVAL_SECONDS):
    """Types `text` one character at a time.

    ONE CHARACTER AT A TIME, WITH A GAP, for the same reason every other input call in this file
    is spaced out: a game is not a text editor. It polls input once a frame and reads a keyboard
    state rather than draining an OS queue, so characters delivered faster than its poll rate can
    be dropped - and a dropped character in a game NAME is not a visible failure, it is a
    slightly wrong name that then gets incremented from forever after.

    `keyboard` rather than pyautogui, for the scancode reason documented on press_key().
    """
    for character in text:
        keyboard.write(character)
        time.sleep(interval)


def clear_field():
    """Empties the focused text field: select-all, then delete.

    Select-all-then-delete rather than a run of backspaces, because backspacing depends on
    knowing how many characters are there - and the whole reason to clear a field is usually
    that you do NOT know what is in it.
    """
    press_combo("ctrl+a")
    time.sleep(KEY_HOLD_SECONDS)
    press_key("backspace")
