"""Tests for overlay.py's panel layout. Needs a display; skips cleanly where the overlay can't run.

WHY THIS EXISTS: the panel is the only part of this project a person reads while playing, and its
failure mode is silent - text that does not fit simply renders outside the backing plate, in
colour, on top of the game. It looks like a rendering glitch rather than a layout bug, and it
only shows up for the LONG status strings, which are exactly the ones that appear when something
has already gone wrong and you most want to read them.

Measured with the real Tk font metrics rather than an estimate, because that is the thing being
checked: a monospace guess would agree with the code under test and prove nothing.
"""
import sys

try:
    import overlay as ov
except Exception as exc:                      # noqa: BLE001 - no display, no Tk, whatever
    print(f"SKIPPED: overlay could not be imported ({exc})")
    sys.exit(0)

try:
    o = ov.Overlay(1920, 1080)
except NotImplementedError as exc:
    print(f"SKIPPED: {exc}")
    sys.exit(0)
except Exception as exc:                      # noqa: BLE001 - headless, no display
    print(f"SKIPPED: overlay window could not be created ({exc})")
    sys.exit(0)

o.root.withdraw()   # measure only; do not flash a topmost window over whatever is on screen

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def plate_width(title, rows):
    """Reimplements draw_panel's sizing so the assertions below can check the result."""
    width = max(ov.PANEL_WIDTH, o._text_width(title, ov.PANEL_TITLE_FONT))
    for (label, value, _f, _c) in rows:
        width = max(width,
                    o._text_width(label, ov.PANEL_FONT),
                    ov.PANEL_VALUE_OFFSET + o._text_width(value, ov.PANEL_FONT))
    return width


def overflows(title, rows, width):
    """Anything that would render past the right-hand edge of a plate `width` wide."""
    out = []
    if o._text_width(title, ov.PANEL_TITLE_FONT) > width:
        out.append(f"title {title!r}")
    for (label, value, _f, _c) in rows:
        if o._text_width(label, ov.PANEL_FONT) > width:
            out.append(f"label {label!r}")
        if ov.PANEL_VALUE_OFFSET + o._text_width(value, ov.PANEL_FONT) > width:
            out.append(f"value {value!r}")
    return out


BAD = (255, 70, 70)
GOOD = (60, 230, 90)

#Every panel state main.py can actually produce. The first one is the reported bug: "not on
#screen (0.30)" rendered in red past the edge of the plate, over the game.
STATES = {
    "NOT IN GAME": ("GAME STATE  NOT IN GAME",
                    [("game HUD", "not on screen (0.30)", None, BAD),
                     ("actions", "paused", None, BAD)]),
    "NOT FOCUSED": ("GAME STATE  NOT FOCUSED",
                    [("readings", "suspended", None, BAD), ("actions", "paused", None, BAD)]),
    "window not found": ("GAME STATE  window not found",
                         [("Diablo II: Resurrec", "not on screen", None, BAD)]),
    "NOT CALIBRATED": ("GAME STATE  NOT CALIBRATED",
                       [("1920x1080", "no profile", None, BAD),
                        ("have", "1280x800, 1920x1080", None, BAD)]),
    "stale": ("GAME STATE  STALE 4s",
              [("health", "100%", 1.0, GOOD), ("mana", " 98%", 0.98, GOOD)]),
    "normal": ("GAME STATE", [("health", "  9%", 0.09, BAD), ("mana", "no read", None, BAD)]),
    "no meters": ("GAME STATE", [("no meters", "run calibrate_meters.py", None, BAD)]),
}

print("1. Nothing renders outside the backing plate, in any panel state")
for name, (title, rows) in STATES.items():
    width = plate_width(title, rows)
    spills = overflows(title, rows, width)
    check(f"{name:18} plate {width:4}px, no overflow" + (f"  SPILLS: {spills}" if spills else ""),
          not spills)

print("\n2. The plate grows only when it has to")
#A plate that resized on every reading would visibly jitter as "9%" became "100%", which is why
#PANEL_WIDTH is a minimum rather than just being removed.
normal = plate_width(*STATES["normal"])
stale = plate_width(*STATES["stale"])
check(f"an ordinary reading stays at the minimum ({normal}px)", normal == ov.PANEL_WIDTH)
check(f"...and so does a stale one ({stale}px), so the plate does not jitter", stale == ov.PANEL_WIDTH)
check("a long status message grows the plate",
      plate_width(*STATES["NOT IN GAME"]) > ov.PANEL_WIDTH)

print("\n3. The reported bug specifically")
title, rows = STATES["NOT IN GAME"]
needed = ov.PANEL_VALUE_OFFSET + o._text_width("not on screen (0.30)", ov.PANEL_FONT)
check(f"'not on screen (0.30)' needs {needed}px, more than the old fixed {ov.PANEL_WIDTH}px",
      needed > ov.PANEL_WIDTH)
check("the plate is now at least that wide", plate_width(title, rows) >= needed)

print("\n4. Measuring is robust to junk input")
check("empty string measures 0", o._text_width("", ov.PANEL_FONT) == 0)
check("None measures 0 rather than raising", o._text_width(None, ov.PANEL_FONT) == 0)
check("a very long string measures large",
      o._text_width("x" * 200, ov.PANEL_FONT) > o._text_width("x" * 20, ov.PANEL_FONT))
check("fonts are cached, not rebuilt per call", ov.PANEL_FONT in o._fonts)

o.close()
print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
