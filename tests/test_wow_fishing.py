"""Tests for wow.py's eyes (the fishing bar, "Your fish got away!") and its brain (routes/fishing.py).

The screen frames are crops of the owner's own WoW recording at its native 1920x1080 - each crop
is exactly the region wow.py captures, so the score a test sees is the score wow.py sees. The loop
is driven with a fake clock; nothing here presses a key, opens an audio device or needs the game.
"""
import sys
from pathlib import Path
#Run directly (python tests/test_x.py), so the repo root has to be on the path before any
#project import - sys.path[0] is this file's own folder, not the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import random

import cv2 as cv
import numpy as np

import wow
from core import presence, sound_match, wav_io
from routes import fishing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "wow"

failures = 0


def check(label, condition):
    global failures
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    failures += not condition


def load(name):
    path = FIXTURES / name
    image = cv.imread(str(path))
    if image is None:                    #a committed fixture that is missing FAILS, never skips
        print(f"FAIL missing committed fixture {path}")
        sys.exit(1)
    return image


bar_check = presence.load(wow.BAR_CONFIG)
got_away_check = presence.load(wow.GOT_AWAY_CONFIG)
if bar_check is None or got_away_check is None:
    print("FAIL the shipped assets/wow configs do not load")
    sys.exit(1)


def bar_score(image):
    return bar_check.check(cv.cvtColor(image, cv.COLOR_BGR2GRAY), 1920, region=(0, 0, 1, 1))


def got_away_score(image):
    return got_away_check.check(wow.yellow_mask(image), 1920, region=(0, 0, 1, 1))


print("\nV-314  The fishing bar is seen on real frames, and not on frames without it")
threshold = bar_check.references[0].threshold
on = {name: bar_score(load(name)) for name in ("bar_full_6s.png", "bar_draining_36s.png")}
off = {name: bar_score(load(name)) for name in ("no_bar_39s.png", "no_bar_nameplates_0s.png")}
for name, (found, score) in on.items():
    check(f"{name}: seen (score {score:.2f}, threshold {threshold})", found is True)
for name, (found, score) in off.items():
    check(f"{name}: not seen (score {score:.2f})", found is False)
#The nameplate frame is the one that matters: monster health bars share the cast bar's frame art,
#and a template of the bar's END scored 0.78 there. The word "Fishing" is what tells them apart.
worst_off = max(score for _, score in off.values())
best_on = min(score for _, score in on.values())
check(f"a wide margin either side of the threshold (no bar {worst_off:.2f} < {threshold} < bar {best_on:.2f})",
      worst_off < threshold - 0.2 and best_on > threshold + 0.2)
check("the template is rescaled for a smaller window, not assumed 1920 wide",
      bar_check.check(cv.cvtColor(cv.resize(load("bar_full_6s.png"), None, fx=0.75, fy=0.75),
                                  cv.COLOR_BGR2GRAY), 1440, region=(0, 0, 1, 1))[0] is True)


print("\nV-315  'Your fish got away!' is read off a real frame, and not off one without it")
found, score = got_away_score(load("got_away_17s.png"))
check(f"got_away_17s.png: seen (score {score:.2f})", found is True)
found, score = got_away_score(load("no_got_away_10s.png"))
check(f"no_got_away_10s.png: not seen (score {score:.2f})", found is False)


print("\nV-316  The water behind the text does not matter - only the yellow letters are compared")
#The message floats over moving water. Paste the real message onto other parts of the scene and
#onto plain colours: it must be found on all of them, and the bare backgrounds on none.
source = load("got_away_17s.png")
mask = wow.yellow_mask(source)
ys, xs = np.nonzero(mask)
y0, y1, x0, x1 = ys.min() - 2, ys.max() + 3, xs.min() - 2, xs.max() + 3
letters = mask[y0:y1, x0:x1] > 0
backgrounds = {"open water": load("no_got_away_10s.png"), "the beach": load("no_bar_39s.png"),
               "black": np.zeros((594, 960, 3), np.uint8), "white": np.full((594, 960, 3), 255, np.uint8)}
for name, background in backgrounds.items():
    canvas = cv.resize(background, (960, 594)) if background.shape[:2] != (594, 960) else background.copy()
    bare_found, bare_score = got_away_score(canvas)
    patch = canvas[100:100 + letters.shape[0], 300:300 + letters.shape[1]]
    patch[letters] = source[y0:y1, x0:x1][letters]
    found, score = got_away_score(canvas)
    check(f"on {name}: found ({score:.2f}), and not found without it ({bare_score:.2f})",
          found is True and bare_found is False)


#---------------------------------------------------------------------------------------------------
# The loop. A fake game: `bar`, `got_away` and `splash` are set by each test; time is a number.
#---------------------------------------------------------------------------------------------------

def make_loop(**kwargs):
    lines = []
    loop = fishing.FishingLoop(rng=random.Random(7), log=lines.append, **kwargs)
    loop.lines = lines
    return loop


def run_until(loop, start, end, step=0.05, **inputs):
    """Ticks the loop from start to end with fixed inputs; returns the times it pressed."""
    presses, now = [], start
    while now < end:
        if loop.decide(now, **inputs) == fishing.PRESS:
            presses.append(round(now, 2))
        now += step
    return presses


print("\nL-701  Splash while fishing: press at once, then recast after a random 1-6 s wait")
loop = make_loop()
loop.start(0.0, bar=True)
check("starts in FISHING when the bar is already up", loop.state == fishing.FISHING)
check("no press while nothing is heard", run_until(loop, 0.0, 5.0, bar=True) == [])
check("the splash presses on the SAME tick it is heard", loop.decide(5.0, bar=True, splash=True) == fishing.PRESS)
delay = loop.cast_at - 5.0
check(f"the recast is scheduled {delay:.2f} s later, inside 1-6 s", 1.0 <= delay <= 6.0)
#The bar is still on screen while it fades out after a reel-in - that must not be read as fishing.
check("no press while the old bar fades, nor before the delay is up",
      run_until(loop, 5.05, loop.cast_at - 0.01, bar=True) == [])
check("then exactly one press to cast", run_until(loop, loop.cast_at, loop.cast_at + 0.5, bar=False) == [round(loop.cast_at, 2)])
check("and the bar coming up means listening again", loop.decide(loop.cast_at + 1.0, bar=True) is None
      and loop.state == fishing.FISHING)
delays = []
for i in range(200):
    loop = make_loop()
    loop.rng = random.Random(i)
    loop.start(0.0, bar=True)
    loop.decide(1.0, bar=True, splash=True)
    delays.append(loop.cast_at - 1.0)
check(f"200 recasts: every delay in 1-6 s ({min(delays):.2f}-{max(delays):.2f}), and they vary "
      f"({len(set(round(d, 1) for d in delays))} distinct)", 1.0 <= min(delays) and max(delays) <= 6.0
      and len(set(round(d, 1) for d in delays)) > 30)
narrow = []
for i in range(50):
    loop = make_loop(recast_delay=(2.0, 3.0))
    loop.rng = random.Random(i)
    loop.start(0.0, bar=True)
    loop.decide(1.0, bar=True, splash=True)
    narrow.append(loop.cast_at - 1.0)
check(f"the range is a setting (2-3 s gives {min(narrow):.2f}-{max(narrow):.2f})",
      2.0 <= min(narrow) and max(narrow) <= 3.0)


print("\nL-702  Nothing heard for the timeout: press anyway, then restart")
loop = make_loop(no_splash_timeout=15.0)
loop.start(0.0, bar=True)
presses = run_until(loop, 0.0, 15.5, bar=True)
check(f"one press, at the 15 s timeout ({presses})", presses == [15.0])
check("counted as a miss, and a recast is scheduled", loop.misses == 1 and loop.state == fishing.CASTING)
loop = make_loop(no_splash_timeout=25.0)
loop.start(0.0, bar=True)
check("the timeout is a setting (25 s: nothing pressed by 20 s)", run_until(loop, 0.0, 20.0, bar=True) == [])


print("\nL-703  'Your fish got away!' only counts when it APPEARS while fishing")
loop = make_loop()
loop.start(0.0, bar=True)
loop.decide(1.0, bar=True, got_away=False)
check("appearing while fishing: no press now, a recast is scheduled",
      loop.decide(1.2, bar=True, got_away=True) is None and loop.state == fishing.CASTING and loop.misses == 1)
#The message stays up for seconds. With a 1 s recast it is still showing when the new bar comes
#up, and reacting to it then would reel in a perfectly good cast.
loop = make_loop()
loop.start(0.0, bar=False)
loop.decide(0.5, bar=False, got_away=True)
loop.decide(1.0, bar=False, got_away=True)             #cast
loop.decide(1.5, bar=True, got_away=True)              #new bar, old message still up
check("a message left over from the last cast does not end the new one",
      run_until(loop, 1.6, 4.0, bar=True, got_away=True) == [] and loop.state == fishing.FISHING)


print("\nL-704  A screen it cannot read never makes it press on its own (None is not False)")
loop = make_loop()
loop.start(0.0, bar=True)
check("bar None for 10 s: no press, still fishing",
      run_until(loop, 0.0, 10.0, bar=None, got_away=None) == [] and loop.state == fishing.FISHING)
check("a splash with the screen unreadable still reels in - the sound is the evidence",
      loop.decide(10.0, bar=None, splash=True) == fishing.PRESS)
loop = make_loop()
loop.start(0.0, bar=True)
check("a bar missing for one tick is not 'gone' (debounced)",
      loop.decide(1.0, bar=False) is None and loop.decide(1.1, bar=True) is None and loop.state == fishing.FISHING)
run_until(loop, 2.0, 3.0, bar=False)
check("a bar missing for longer is a missed round: recast scheduled, nothing pressed now",
      loop.state == fishing.CASTING and loop.misses == 1)


print("\nL-705  Paused, or WoW not in front: nothing is pressed, and a splash then is dropped")
loop = make_loop()
loop.start(0.0, bar=True)
check("a splash while not allowed presses nothing", loop.decide(1.0, bar=True, splash=True, allowed=False) is None)
check("and is not remembered for later", run_until(loop, 1.05, 5.0, bar=True) == [])
loop = make_loop()
loop.start(0.0, bar=False)
check("a due cast waits while not allowed", run_until(loop, 0.0, 30.0, bar=False, allowed=False) == [])


print("\nL-706  A cast that never shows the bar is retried, then the loop stops and says why")
loop = make_loop(max_failed_casts=3)
loop.start(0.0, bar=False)
presses = run_until(loop, 0.0, 120.0, bar=False)
check(f"3 casts, then nothing more ({len(presses)} presses)", len(presses) == 3)
check(f"stopped, with a reason a person can act on ('{loop.reason[:60]}...')",
      loop.state == fishing.STOPPED and "fishing bar never appeared" in loop.reason)


print("\nL-707  Key holds stay in 0.2-0.6 s, and are usually short")
rng = random.Random(3)
holds = [wow.hold_time(rng) for _ in range(2000)]
check(f"every hold in range ({min(holds):.3f}-{max(holds):.3f} s)",
      min(holds) >= wow.KEY_HOLD_SECONDS[0] and max(holds) <= wow.KEY_HOLD_SECONDS[1])
short = sum(h < 0.4 for h in holds) / len(holds)
check(f"most are in the short half ({short:.0%} under 0.4 s), but not all the same "
      f"({len(set(round(h, 2) for h in holds))} distinct)", short > 0.6 and len(set(round(h, 2) for h in holds)) > 30)


print("\nL-708  The reference recording, replayed through the ears and the loop, reels in at every splash")
#End to end without the game: the real audio through the real matcher, and the bar as it really was
#on screen (measured frame by frame on the video at 60 fps: up 5.29-37.98 s and 41.24-52.94 s).
samples, rate = wav_io.read_wav(FIXTURES / "fishing_session_16k.wav")
matcher = sound_match.SoundMatcher(sound_match.load_reference_folder(wow.SPLASH_FOLDER), rate)
loop = make_loop(no_splash_timeout=60.0)   #the recording has a 20 s bite; see docs/wow_fishing.md
loop.start(0.0, bar=False)
loop.cast_at = 999.0                       #the person cast by hand in the video - do not cast here
loop.state = fishing.WAITING_FOR_BAR
loop.deadline = 999.0
presses, step = [], int(0.01 * rate)
for start in range(0, len(samples), step):
    now = start / rate
    heard = bool(matcher.feed(samples[start:start + step]))
    bar = 5.29 <= now <= 37.98 or 41.24 <= now <= 52.94
    if loop.decide(now, bar=bar, splash=heard) == fishing.PRESS:
        presses.append(round(now, 2))
reels = [p for p in presses if any(0 <= p - o <= 0.5 for o in (16.42, 37.05, 51.60))]
check(f"a reel-in press within 0.5 s of each splash ({reels})", len(reels) == 3)


print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
