"""Locating a specific window's client area on screen.

WHY THIS EXISTS: some things the engine measures are properties of a WINDOW, not of the screen.
A detection box is screen-absolute and does not care where the game sits - it is found by looking
at pixels, so it works identically fullscreen, windowed, or moved to another monitor. A HUD meter
is the opposite: "the health orb is 21% across and 84% down" is only meaningful relative to the
window drawing it. Storing such a region as a fraction of the SCREEN silently breaks the moment
the window is not fullscreen, which is exactly what happened - meters calibrated fullscreen read
0% once the game was switched to windowed 1280x800, because the stored fractions then pointed at
bare desktop.

Anchoring to the window instead makes the same config survive the window being MOVED or RESIZED.
It does NOT survive the game changing its own resolution or aspect ratio - the game re-lays out
its HUD when that happens. Measured on Diablo II, 1920x1080 -> 1280x800 held the orbs' vertical
position and height to within 0.001 of the same window-fraction but moved them horizontally by
0.033, which is 42px against a 104px-wide orb. main.py compares the live aspect ratio against the
one recorded at calibration time and warns, since the failure mode is a plausible-but-wrong
percentage rather than an error.

PORTABILITY: this is the OS-specific seam CLAUDE.md calls out, kept in one file rather than
sprinkled through the pipeline. Everything here degrades to None on a platform without pywin32,
and every caller must treat None as "no anchor known, fall back to the full frame" rather than as
an error - the pipeline stays fully functional without it, just screen-anchored as before.

It deliberately knows no window titles. The caller supplies a substring, which comes from config
(assets/meters.json's "_anchor"), so nothing about Diablo II is baked in here.
"""
from __future__ import annotations

try:
    import win32gui
except ImportError:  # not Windows, or pywin32 missing - callers fall back to the full frame
    win32gui = None


def available():
    """Whether window lookup can work at all on this platform/install."""
    return win32gui is not None


def find_client_rect(title_substring):
    """Screen-coordinate (x, y, w, h) of the CLIENT area of a visible window whose title contains
    `title_substring` (case-insensitive). Returns None if unsupported, not found, or degenerate.

    The client area, not the window rect: the window rect includes the title bar and resize
    borders, which the game does not draw into. Anchoring a meter to the window rect would offset
    every region by the height of the title bar - about 30px, comfortably enough to miss an orb.
    """
    if win32gui is None or not title_substring:
        return None

    needle = title_substring.casefold()
    found = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return
        if not title or needle not in title.casefold():
            return
        try:
            _, _, cw, ch = win32gui.GetClientRect(hwnd)
            left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        except Exception:
            return  # window died between enumeration and query, or is not queryable
        if cw > 0 and ch > 0:
            # Rank 0 = the title is exactly what was asked for, rank 1 = merely contains it.
            exact = title.casefold().strip() == needle.strip()
            found.append((0 if exact else 1, -(cw * ch), (left, top, cw, ch)))

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None

    if not found:
        return None
    # An EXACT title match beats a substring one, and only then does size break the tie.
    # Size alone is not enough and was measured getting it wrong: searching "Diablo II" matched
    # both the game (client 1280x800) and a browser tab titled "d2r.guide - Diablo II:
    # Resurrected Cheat Sheet - Google Chrome" (client 1920x1080), and the browser won on size.
    # Anything with the game's name in a page title, a chat window or an editor tab is a
    # candidate, so preferring the exact configured title is what actually disambiguates.
    found.sort()
    return found[0][2]


def to_frame_fractions(anchor_rect, capture_rect, region):
    """Converts a region expressed as fractions OF THE ANCHOR WINDOW into fractions of a captured
    frame, so it can be handed to a detector that only knows about the frame it was given.

    anchor_rect, capture_rect: (x, y, w, h) in screen coordinates.
    region: (x, y, w, h) as fractions of the anchor.

    Returns None if the anchor is not inside the capture at all - a caller must treat that as
    "cannot read", never as a zero reading, for the same reason game_state distinguishes None
    from 0.0: a window dragged onto a second monitor that the pipeline is not capturing must not
    silently report an empty health orb.
    """
    ax, ay, aw, ah = anchor_rect
    cx, cy, cw, ch = capture_rect
    if aw <= 0 or ah <= 0 or cw <= 0 or ch <= 0:
        return None

    rx, ry, rw, rh = region
    # region -> absolute screen pixels -> fractions of the captured frame
    sx = ax + rx * aw
    sy = ay + ry * ah
    sw = rw * aw
    sh = rh * ah
    if sx + sw <= cx or sy + sh <= cy or sx >= cx + cw or sy >= cy + ch:
        return None  # entirely outside what we captured
    return ((sx - cx) / cw, (sy - cy) / ch, sw / cw, sh / ch)


def list_client_rects():
    """[(title, (x, y, w, h)), ...] for every visible titled window, in screen coordinates.

    Used by the calibrator to work out WHICH window a hand-drawn selection landed in, so the
    user never has to type a window title. Snapshot this at screenshot time, before showing any
    UI of your own - otherwise your own selection window is on top of the point being tested.
    """
    if win32gui is None:
        return []
    out = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            title = win32gui.GetWindowText(hwnd)
            if not title.strip():
                return
            _, _, cw, ch = win32gui.GetClientRect(hwnd)
            left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        except Exception:
            return
        if cw > 0 and ch > 0:
            out.append((title, (left, top, cw, ch)))

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return []
    return out


def smallest_containing(rects, x, y):
    """(title, rect) of the SMALLEST window containing screen point (x, y), or None.

    Smallest, not topmost: the desktop and any maximised window also contain the point, and the
    innermost one is the actual thing the user pointed at.
    """
    hits = [(t, r) for (t, r) in rects
            if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]]
    if not hits:
        return None
    return min(hits, key=lambda tr: tr[1][2] * tr[1][3])
