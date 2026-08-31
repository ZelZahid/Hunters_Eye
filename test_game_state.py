"""Synthetic verification of game_state.read_meter, runnable without the game: `python test_game_state.py`.

Every case here builds a fake orb with a KNOWN fill level and checks the measured value against
it, so the measurement can be trusted before it drives any real action. The adversarial cases
(3, 9, 10 - sensor noise, a specular highlight streak, a stray patch of the meter's own color)
are the ones that matter: they each caught a real bug during development that a clean, ideal
synthetic orb passed straight through. See Error_history.txt #19 and #20.
"""
import sys

import cv2 as cv
import numpy as np
import game_state

FRAME_W, FRAME_H = 1200, 800
ORB_BOX = (100, 500, 180, 180)          # x, y, w, h in pixels
REGION = (ORB_BOX[0] / FRAME_W, ORB_BOX[1] / FRAME_H, ORB_BOX[2] / FRAME_W, ORB_BOX[3] / FRAME_H)

RED = ((0, 70, 40), (10, 255, 255))
RED_WRAP = ((168, 70, 40), (179, 255, 255))
BLUE = ((95, 70, 40), (130, 255, 255))

health = game_state.Meter("health", REGION, (RED, RED_WRAP), "ellipse", "bottom")


def make_frame(fill, liquid_bgr=(30, 30, 200), noise=False, gloss=False):
    """A dark screen with one circular orb filled to `fill` of its HEIGHT."""
    frame = np.full((FRAME_H, FRAME_W, 3), 20, dtype=np.uint8)
    x, y, w, h = ORB_BOX

    orb = np.full((h, w, 3), (35, 30, 30), dtype=np.uint8)   # dark, desaturated empty portion
    liquid_top = int(h * (1.0 - fill))
    orb[liquid_top:, :] = liquid_bgr

    if gloss:
        # a bright highlight streak across the liquid, like a real game draws
        cv.line(orb, (0, int(h * 0.75)), (w, int(h * 0.72)), (230, 230, 255), 3)
    if noise:
        orb = cv.add(orb, np.random.randint(0, 25, orb.shape, dtype=np.uint8))

    circle = np.zeros((h, w), dtype=np.uint8)
    cv.circle(circle, (w // 2, h // 2), w // 2, 255, -1)
    frame[y:y + h, x:x + w][circle > 0] = orb[circle > 0]
    return frame


def check(label, expected, actual, tolerance=0.04):
    if actual is None:
        print(f"  FAIL {label}: got None, expected {expected:.2f}")
        return False
    ok = abs(actual - expected) <= tolerance
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: expected {expected * 100:5.1f}%  got {actual * 100:5.1f}%")
    return ok


failures = 0

print("1. Accuracy across fill levels (full resolution)")
#0.0 is deliberately absent: an orb with no liquid is indistinguishable from no orb at all, so
#it reads None rather than 0%. See section 12 and _fill_fraction's docstring.
for fill in (0.15, 0.25, 0.5, 0.75, 0.9, 1.0):
    failures += not check(f"fill {fill:.2f}", fill, game_state.read_meter(make_frame(fill), health))

print("\n2. Same, but downscaled to main.py's fast-path scale (0.3)")
for fill in (0.15, 0.4, 0.6, 0.85):
    small = cv.resize(make_frame(fill), (0, 0), fx=0.3, fy=0.3)
    failures += not check(f"fill {fill:.2f} @0.3x", fill, game_state.read_meter(small, health), tolerance=0.06)

print("\n3. Robustness: sensor noise + a gloss highlight streak over the liquid")
for fill in (0.3, 0.65):
    frame = make_frame(fill, noise=True, gloss=True)
    failures += not check(f"fill {fill:.2f} noisy+gloss", fill, game_state.read_meter(frame, health))

print("\n4. The reason for row-scanning: area fraction != height fraction on a circle")
frame = make_frame(0.25)
value = game_state.read_meter(frame, health)
_, _, mask = game_state.read_meter(frame, health, return_debug=True)
shape = game_state._shape_mask("ellipse", mask.shape[0], mask.shape[1])
area_fraction = mask.sum() / shape.sum()
print(f"  a 25%-full circle measures {value * 100:.1f}% by height (correct) "
      f"but {area_fraction * 100:.1f}% by pixel count (wrong - would under-report low health)")
failures += not (abs(value - 0.25) < 0.04 and area_fraction < 0.22)

print("\n5. Wrong color does not read as full")
mana_meter = game_state.Meter("mana", REGION, (BLUE,), "ellipse", "bottom")
value = game_state.read_meter(make_frame(0.8), mana_meter)   # red orb, blue detector
print(f"  blue detector on a red orb: {value}")
failures += not (value is None)   # None = cannot read, which is the honest answer

print("\n6. A horizontal bar filling from the left")
bar = game_state.Meter("bar", (0.1, 0.1, 0.5, 0.05), (BLUE,), "rect", "left")
frame = np.full((FRAME_H, FRAME_W, 3), 20, dtype=np.uint8)
bx, by = int(0.1 * FRAME_W), int(0.1 * FRAME_H)
bw, bh = int(0.5 * FRAME_W), int(0.05 * FRAME_H)
frame[by:by + bh, bx:bx + int(bw * 0.6)] = (200, 60, 40)
failures += not check("bar 60% from left", 0.6, game_state.read_meter(frame, bar))

print("\n7. Off-screen / degenerate region returns None, not 0.0")
offscreen = game_state.Meter("gone", (1.5, 1.5, 0.1, 0.1), (RED,), "ellipse", "bottom")
value = game_state.read_meter(make_frame(0.5), offscreen)
print(f"  {'ok  ' if value is None else 'FAIL'} off-screen region -> {value}")
failures += not (value is None)

print("\n8. Smoother rejects a single bad frame")
smoother = game_state.Smoother(window=5)
out = None
for reading in (0.80, 0.79, 0.81, 0.02, 0.80):   # 0.02 = a tooltip covering the orb for one frame
    out = smoother.update({"health": reading})["health"]
print(f"  {'ok  ' if abs(out - 0.80) < 0.02 else 'FAIL'} after a 2% glitch frame, "
      f"smoothed value is {out * 100:.0f}% (raw last was 80%)")
failures += not (abs(out - 0.80) < 0.02)

smoother2 = game_state.Smoother(window=5)
smoother2.update({"health": 0.7})
kept = smoother2.update({"health": None})["health"]
print(f"  {'ok  ' if kept == 0.7 else 'FAIL'} a failed read keeps the last good value ({kept})")
failures += not (kept == 0.7)

print("\n9. A stray patch of the meter's own color ABOVE the liquid does not inflate the reading")
frame = make_frame(0.20)
x, y, w, h = ORB_BOX
cv.circle(frame, (x + w // 2, y + int(h * 0.2)), 12, (30, 30, 200), -1)   # e.g. a red spell effect
value = game_state.read_meter(frame, health)
ok = value is not None and abs(value - 0.20) < 0.08
print(f"  {'ok  ' if ok else 'FAIL'} 20% orb with a red blob near the top reads {value * 100:.1f}%")
failures += not ok

print("\n10. A gloss streak ABOVE the liquid does not read as liquid")
frame = make_frame(0.30)
cv.line(frame, (x, y + int(h * 0.3)), (x + w, y + int(h * 0.28)), (230, 230, 255), 3)
value = game_state.read_meter(frame, health)
ok = value is not None and abs(value - 0.30) < 0.06
print(f"  {'ok  ' if ok else 'FAIL'} 30% orb with a highlight above the surface reads {value * 100:.1f}%")
failures += not ok

print("\n11. A failed read carries the last good value, but NOT indefinitely")
#Both a dropped frame and "we are not looking at the game any more" arrive as None. Carrying
#forever turns the second one into a frozen number that LOOKS live, which a consumer then acts
#on with full confidence - see Smoother's docstring.
smoother3 = game_state.Smoother(window=5, max_carried_failures=15)
for _ in range(5):
    smoother3.update({"health": 0.85})

carried = smoother3.update({"health": None})["health"]
ok = carried == 0.85
print(f"  {'ok  ' if ok else 'FAIL'} 1 failed read still reports the last good value ({carried})")
failures += not ok

for _ in range(13):      # 14 consecutive failures total - still inside the limit
    carried = smoother3.update({"health": None})["health"]
ok = carried == 0.85
print(f"  {'ok  ' if ok else 'FAIL'} 14 consecutive failures still carry ({carried})")
failures += not ok

smoother3.update({"health": None})          # 15th
gone = smoother3.update({"health": None})["health"]   # past the limit
ok = gone is None
print(f"  {'ok  ' if ok else 'FAIL'} past the limit it reports {gone!r}, not a stale number")
failures += not ok

for _ in range(200):
    gone = smoother3.update({"health": None})["health"]
ok = gone is None
print(f"  {'ok  ' if ok else 'FAIL'} and stays {gone!r} indefinitely, never resurrecting the old value")
failures += not ok

#A good frame after a gap must not be median-blended with samples from before it - whatever
#happened during a gap of unknown length is not something a median over stale values can model.
back = smoother3.update({"health": 0.10})["health"]
ok = back == 0.10
print(f"  {'ok  ' if ok else 'FAIL'} one good read after the gap reports it exactly ({back}), not blended with the pre-gap 85%")
failures += not ok

#A brief dropout must NOT reset the history - that is the case the carry exists for.
smoother4 = game_state.Smoother(window=5, max_carried_failures=15)
for _ in range(5):
    smoother4.update({"health": 0.60})
smoother4.update({"health": None})
smoother4.update({"health": None})
out = smoother4.update({"health": 0.62})["health"]
ok = abs(out - 0.60) < 0.02
print(f"  {'ok  ' if ok else 'FAIL'} a 2-frame dropout keeps smoothing normally afterwards ({out:.2f})")
failures += not ok

#Failures are counted per meter and reset by any good read, so one dead meter cannot mute a live one.
smoother5 = game_state.Smoother(window=5, max_carried_failures=3)
smoother5.update({"health": 0.5, "mana": 0.9})
for _ in range(10):
    out = smoother5.update({"health": 0.5, "mana": None})
ok = out["health"] == 0.5 and out["mana"] is None
print(f"  {'ok  ' if ok else 'FAIL'} a dead meter goes None without muting a live one (health={out['health']}, mana={out['mana']!r})")
failures += not ok

print("\n12. An UNREADABLE meter reports None, never 0% - the lobby / dimmed-menu bug")
#Reporting 0% for a meter that is simply not visible tells a consumer "you are about to die" at
#the moment nothing is wrong - with an action layer attached that empties a potion belt. Both
#cases below were seen live in Diablo II.

#(a) the meter is not on screen at all - the game lobby, where the HUD does not exist
blank = np.full((FRAME_H, FRAME_W, 3), 18, dtype=np.uint8)
value = game_state.read_meter(blank, health)
ok = value is None
print(f"  {'ok  ' if ok else 'FAIL'} no orb present at all -> {value!r}, not 0.0")
failures += not ok

#(b) the screen is dimmed, as the in-game menu does, dropping V under the hsv_range floor
for dim in (0.5, 0.35, 0.2):
    dark = (make_frame(0.9).astype(np.float32) * dim).astype(np.uint8)
    value = game_state.read_meter(dark, health)
    ok = value is None or abs(value - 0.9) < 0.06
    shown = "None" if value is None else format(value * 100, ".0f") + "%"
    print(f"  {'ok  ' if ok else 'FAIL'} 90% orb dimmed to {dim:.0%} -> {shown} (None or ~90%, never ~0%)")
    failures += not ok

#(c) stray colour that is not a meter - matching pixels with no coherent surface beneath them
speckle = np.full((FRAME_H, FRAME_W, 3), 18, dtype=np.uint8)
rng = np.random.default_rng(7)
bx, by, bw, bh = ORB_BOX
patch = speckle[by:by + bh, bx:bx + bw]
patch[rng.random((bh, bw)) < 0.30] = (30, 30, 200)
speckle[by:by + bh, bx:bx + bw] = patch
value = game_state.read_meter(speckle, health)
ok = value is None
print(f"  {'ok  ' if ok else 'FAIL'} scattered red speckle over 30% of the region -> {value!r}, not 0.0")
failures += not ok

#(d) ...but a genuinely low orb still reads a number, because that is when it matters most
for fill in (0.05, 0.10, 0.20):
    value = game_state.read_meter(make_frame(fill), health)
    ok = value is not None and abs(value - fill) < 0.04
    shown = "None" if value is None else format(value * 100, ".0f") + "%"
    print(f"  {'ok  ' if ok else 'FAIL'} a real {fill:.0%} orb still reads {shown} - low health must stay actionable")
    failures += not ok

print("\n13. A single row of stray colour is not a reading - the lobby 2% bug")
#The surface test asks "are the rows below this one filled too?", which a row at the very BOTTOM
#of the band passes vacuously: there are no rows below it, so 0 of 0 is 100%. One stray row
#therefore produced a real-looking value, and at CAPTURE_SCALE 0.3 an orb is only ~43 rows tall,
#so that value was 2% - below every potion threshold. Seen live in the Diablo II lobby, where red
#character names sit where the health orb would be: the panel read "health 2%" and the emergency
#tier fired. See game_state.MIN_SURFACE_SUPPORT_ROWS.
bx, by, bw, bh = ORB_BOX
for rows_from_bottom in (1, 2, 3):
    frame = np.full((FRAME_H, FRAME_W, 3), 18, dtype=np.uint8)
    row = by + bh - rows_from_bottom
    frame[row:row + 1, bx:bx + bw] = (30, 30, 200)      # one full-width row of the meter's colour
    value = game_state.read_meter(frame, health)
    ok = value is None
    shown = "None" if value is None else format(value * 100, ".1f") + "%"
    print(f"  {'ok  ' if ok else 'FAIL'} a single red row {rows_from_bottom} up from the bottom "
          f"-> {shown}, not a low reading")
    failures += not ok

#The floor is measured, not guessed: at MIN_SURFACE_SUPPORT_ROWS = 1 the FULL-RESOLUTION reading
#is completely unchanged, which is what makes this affordable. Raising it to 2 would start
#costing real low-health readings at the fast path's downscale for no extra protection.
for fill in (0.02, 0.03, 0.05, 0.10):
    value = game_state.read_meter(make_frame(fill), health)
    ok = value is not None and abs(value - fill) < 0.02
    shown = "None" if value is None else format(value * 100, ".1f") + "%"
    print(f"  {'ok  ' if ok else 'FAIL'} full resolution still reads a real {fill:.0%} orb as {shown}")
    failures += not ok

#At the downscale main.py actually uses, the floor is real and worth stating out loud: an orb
#below ~7% reads "unknown". That is affordable only because a meter does not teleport - health
#falls THROUGH 20%, 15% and 10% on the way down at ~50 samples a second.
small = cv.resize(make_frame(0.15), (0, 0), fx=0.3, fy=0.3)
value = game_state.read_meter(small, health)
ok = value is not None and abs(value - 0.15) < 0.05
shown = "None" if value is None else format(value * 100, ".1f") + "%"
print(f"  {'ok  ' if ok else 'FAIL'} at 0.3x a real 15% orb still reads {shown} - the thresholds "
      f"that matter stay measurable")
failures += not ok

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
