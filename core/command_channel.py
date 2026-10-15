"""Taking commands from another program, one line of text at a time.

WHY THIS EXISTS: every way of driving the engine so far is a global hotkey, which is the right
tool for a person sitting at the game but the wrong one for a second PROGRAM - a control panel
(gui.py), or later a robot's supervisor - that wants to say "start the run" without faking a
keypress into whatever window happens to be in front. This is that second door. It does not
replace the hotkeys; both lead to the same functions.

THE PROTOCOL IS DELIBERATELY TINY: one command name per line on a text stream (normally the
process's stdin, written by the program that started it). No arguments, no replies - the reply
channel is the console output the caller is already reading. A name that is not in the table is
reported and ignored, never guessed at.

THIS MODULE KNOWS NOTHING ABOUT ANY GAME. It maps names to callables the caller hands in, which is
the same split as routes/ (gets callables handed in, never imports main). "next_game" means
whatever the caller says it means.

STABILITY: the reader is a daemon thread, a handler that raises is printed and survives (the next
command still works), and END OF STREAM is reported to `on_close` - for a process started by a
control panel, the panel going away is exactly when nobody is watching any more, which a caller
can treat as "stop".
"""
from __future__ import annotations

import sys
import threading
import traceback


def _dispatch(line, handlers, log):
    """Runs the handler for one line. Returns the command name, or None for a blank line."""
    name = line.strip().lower()
    if not name:
        return None
    handler = handlers.get(name)
    if handler is None:
        log(f"command: unknown command {name!r} (known: {', '.join(sorted(handlers))})")
        return name
    try:
        handler()
    except Exception:  # noqa: BLE001 - one failed command must not end the channel
        traceback.print_exc()
        log(f"command: {name!r} stopped by the error above.")
    return name


def listen(handlers, stream=None, on_close=None, log=print):
    """Starts a daemon thread that reads `stream` line by line and runs `handlers[line]`.

    handlers: {name: callable taking no arguments}. Names are matched case-insensitively.
        A handler runs ON THE READER THREAD, so anything that takes longer than a moment must
        start its own thread - otherwise the next command (often "stop") waits behind it.
    stream: a text stream; defaults to sys.stdin.
    on_close: called once when the stream ends (the writer closed it or went away).
    Returns the thread.
    """
    handlers = {k.lower(): v for k, v in handlers.items()}
    stream = sys.stdin if stream is None else stream

    def reader():
        try:
            for line in stream:
                _dispatch(line, handlers, log)
        except (OSError, ValueError) as exc:
            #A pipe torn down underneath us. Same meaning as a clean end of stream.
            log(f"command: input closed ({exc})")
        if on_close is not None:
            try:
                on_close()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    thread = threading.Thread(target=reader, name="command-channel", daemon=True)
    thread.start()
    return thread
