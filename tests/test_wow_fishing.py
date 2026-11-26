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

def _raises(action):
    try:
        action()
    except ValueError:
        return True
    return False


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
    loop.decide(2.0, bar=True, splash=True)
    delays.append(loop.cast_at - 2.0)
check(f"200 recasts: every delay in 1-6 s ({min(delays):.2f}-{max(delays):.2f}), and they vary "
      f"({len(set(round(d, 1) for d in delays))} distinct)", 1.0 <= min(delays) and max(delays) <= 6.0
      and len(set(round(d, 1) for d in delays)) > 30)
narrow = []
for i in range(50):
    loop = make_loop(recast_delay=(2.0, 3.0))
    loop.rng = random.Random(i)
    loop.start(0.0, bar=True)
    loop.decide(2.0, bar=True, splash=True)
    narrow.append(loop.cast_at - 2.0)
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


#---------------------------------------------------------------------------------------------------
# The lure. Fixtures are 150x230 crops of the owner's recordings at 1920x1080 holding the cog WoW
# draws over the lure and the water under it; the strips are 10 frames a second, stacked top to
# bottom, with the crop's place in the window and the first frame's time as below.
#---------------------------------------------------------------------------------------------------

LURE_STRIPS = {   #name: (origin in the 1920x1080 window, time of the first frame, bite or None)
    "lure_bite_v4.png": ((1006, 218), 24.3, 27.2),
    "lure_bite_choppy_v3.png": ((964, 241), 21.5, 24.4),
    "lure_pointer_no_bite_v4.png": ((1014, 225), 7.0, None),
}


def strip_frames(name):
    strip = load(name)
    return [strip[i:i + 230] for i in range(0, strip.shape[0], 230)]


def watch_strip(eye, name):
    """Feeds a strip to a fresh BobberEye exactly as wow.py's lure thread does; returns
    [(time, seen, bite)] for every frame."""
    origin, start, _bite = LURE_STRIPS[name]
    eye.lose()
    return [(round(start + i / 10, 1), *eye.look(frame, origin, 1920, start + i / 10))
            for i, frame in enumerate(strip_frames(name))]


eye = wow.load_bobber_eye()
if eye is None:
    print("FAIL the shipped assets/wow/bobber.json does not load")
    sys.exit(1)


print("\nV-317  The lure is found by the cog over it - on different water, light and camera - and not where there is none")
threshold = eye.marker.references[0].threshold
for name in ("lure_v1_12s.png", "lure_bite_v4.png", "lure_bite_choppy_v3.png"):
    frame = strip_frames(name)[0]
    found, score = eye.marker.check(cv.cvtColor(frame, cv.COLOR_BGR2GRAY), 1920, region=(0, 0, 1, 1))
    check(f"{name}: found (score {score:.2f}, threshold {threshold})", found is True)
found, score = eye.marker.check(cv.cvtColor(load("no_lure_v4_29s.png"), cv.COLOR_BGR2GRAY), 1920,
                                region=(0, 0, 1, 1))
check(f"no_lure_v4_29s.png (the same water once the lure is gone): not found (score {score:.2f})",
      found is False)
small = cv.resize(load("lure_v1_12s.png"), None, fx=0.5, fy=0.5, interpolation=cv.INTER_AREA)
found, score = eye.marker.check(cv.cvtColor(small, cv.COLOR_BGR2GRAY), 960, region=(0, 0, 1, 1))
check(f"rescaled for a 960-wide window, not assumed 1920 ({score:.2f})", found is True)


print("\nV-318  A real bite is seen at the moment it happens, once, and a pointer crossing the lure is not a bite")
for name, (_origin, _start, bite) in LURE_STRIPS.items():
    looks = watch_strip(eye, name)
    bites = [t for t, _seen, b in looks if b]
    judged = [t for t, _seen, b in looks if b is not None]
    if bite is not None:
        check(f"{name}: one bite, at {bite} s ({bites})", bites == [bite])
    else:
        check(f"{name}: no bite, though the pointer crosses the lure at 10.0 s ({bites})",
              bites == [] and 10.0 in judged)
    first_seen = next(t for t, seen, _b in looks if seen)
    check(f"  nothing judged in the first second after the lure appears (its own landing splash): "
          f"seen {first_seen}, first judged {judged[0]}", judged[0] - first_seen >= eye.settle_seconds)
looks = watch_strip(eye, "lure_bite_v4.png")
check("a lure that is no longer there is 'not seen', not an error",
      eye.look(load("no_lure_v4_29s.png"), (1006, 218), 1920, 29.0)[0] is False)
check("no frame at all is 'cannot tell': (None, None)", eye.look(None, (0, 0), 1920, 30.0) == (None, None))
#How many looks a second it needs is measured on the full videos, not here (the strips are too
#short to build up history at a lower rate): all 9 bites at 8, 12 and 20 looks a second, 0 false
#alarms - see docs/wow_fishing.md.


print("\nL-709  A splash right after the bar appears is the lure landing, not a bite: ignored, and said so")
loop = make_loop()
loop.start(0.0, bar=False)
run_until(loop, 0.0, 1.05, bar=False)                  #the cast
loop.decide(1.5, bar=True)                             #the bar appears: fishing since 1.5
check("a splash 0.3 s later presses nothing (measured: the landing plop came 0.3 s after the bar)",
      loop.decide(1.8, bar=True, splash=True) is None and loop.state == fishing.FISHING)
check(f"and the log says why ('{loop.lines[-1][:60]}...')", "landing" in loop.lines[-1])
check("a lure 'bob' that early is ignored the same way", loop.decide(2.0, bar=True, bobbed=True) is None)
check("a splash 1.6 s after the bar is a bite: reel in", loop.decide(3.1, bar=True, splash=True) == fishing.PRESS)


print("\nL-710  Either signal reels in - the splash heard, or the lure seen bobbing - and the tally says which")
loop = make_loop()
loop.start(0.0, bar=True)
check("the lure bobbing alone reels in", loop.decide(5.0, bar=True, bobbed=True) == fishing.PRESS)
loop.start(20.0, bar=True)
check("the splash alone reels in", loop.decide(25.0, bar=True, splash=True) == fishing.PRESS)
loop.start(40.0, bar=True)
check("both on the same tick: ONE press, not two", loop.decide(45.0, bar=True, splash=True, bobbed=True)
      == fishing.PRESS and loop.decide(45.01, bar=True, splash=True, bobbed=True) is None)
check(f"tallied by signal {loop.caught_by}", loop.caught_by == {"sound": 1, "sight": 1, "both": 1})
loop = make_loop()
loop.start(0.0, bar=False)
loop.decide(0.5, bar=False, bobbed=True)
check(f"a bite while not fishing is logged as not acted on ('{loop.lines[-1][:50]}...')",
      "not acted on" in loop.lines[-1] and "lure bobbed" in loop.lines[-1])


print("\nL-711  Pause and resume are ONE key, and each state still has its own sound")
check("there is no separate resume key any more", not hasattr(wow, "RESUME_KEY"))
check(f"the pause key is {wow.PAUSE_KEY.upper()}", wow.PAUSE_KEY == "f5")
check("the two cues differ (falling to pause, rising to resume)",
      not np.array_equal(wow.cue_samples(False, 16000), wow.cue_samples(True, 16000)))
check(f"the no-bite restart is {wow.NO_SPLASH_TIMEOUT_SECONDS:.0f} s, and the loop's default agrees",
      wow.NO_SPLASH_TIMEOUT_SECONDS == 30.0 and fishing.FishingLoop().no_splash_timeout == 30.0)

print("\nL-718  The success rate is kept in its band: bites are let go only near the top, never two in a row, reeled in late")
Keeper = fishing.SuccessRateKeeper
check("the rate is None before anything was tried - not 0%, not 100%", Keeper.rate(0, 0) is None)
check("4 reeled of 5 ended is 80%", Keeper.rate(4, 1) == 0.8)
keeper = Keeper(0.90, 0.99, rng=random.Random(1))
check("under 10 casts nothing is let go, even at 100%",
      not any(keeper.let_go(n, 0) for n in range(10)))
keeper = Keeper(0.90, 0.99, rng=random.Random(1))
check("below the top of the band nothing is let go (95%, 50 times)",
      not any(keeper.let_go(95, 5) for _ in range(50)))
check("bands that cannot be: refused", all(_raises(lambda b=b: Keeper(*b)) for b in
                                           ((0.0, 0.9), (0.95, 0.90), (0.9, 1.01))))
keeper = Keeper(0.90, 0.99)
keeper.set_band(0.90, 0.95)
check("the band can be changed while fishing; a band that is not one is refused and the old kept",
      (keeper.low, keeper.high) == (0.90, 0.95) and _raises(lambda: keeper.set_band(0.97, 0.95))
      and (keeper.low, keeper.high) == (0.90, 0.95))
check("a band ending at 100% is off: every bite reeled in",
      Keeper(0.9, 1.0).off and not any(Keeper(0.9, 1.0).let_go(500, 0) for _ in range(20)))


def simulate(low, high, bites, seed, natural_miss=0.02):
    """A long session with the keeper in charge: (rate after each bite, longest run of let-gos)."""
    rng = random.Random(seed)
    keeper = Keeper(low, high, rng=random.Random(seed + 1000))
    reeled = missed = run = longest = 0
    rates = []
    for _ in range(bites):
        if keeper.let_go(reeled, missed):
            missed += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
            if rng.random() < natural_miss:      #a bite the eyes and ears really missed
                missed += 1
            else:
                reeled += 1
        rates.append(reeled / (reeled + missed))
    return rates, longest


for seed in (3, 4, 5):
    rates, longest = simulate(0.90, 0.99, 600, seed)
    settled = rates[100:]
    check(f"600 bites (seed {seed}): after the first 100 the rate stays in 89-99.5% "
          f"({min(settled):.1%}-{max(settled):.1%}), and never two let go in a row (longest {longest})",
          0.89 <= min(settled) and max(settled) <= 0.995 and longest == 1)
rates, _ = simulate(0.80, 0.85, 600, 6)
check(f"the band is a setting: 80-85% keeps it at {min(rates[200:]):.1%}-{max(rates[200:]):.1%}",
      0.79 <= min(rates[200:]) and max(rates[200:]) <= 0.86)


class AlwaysLetGo(Keeper):
    """A keeper that lets every bite go - to drive the loop's let-go path on demand."""

    def let_go(self, reeled, missed):
        return True


loop = make_loop(keeper=AlwaysLetGo())
loop.start(0.0, bar=True)
check("a bite let go: no press on the bite", loop.decide(5.0, bar=True, splash=True) is None)
check(f"...and it is said so ('{loop.lines[-1][:60]}...')", "on purpose" in loop.lines[-1])
late = loop.late_press_at - 5.0
check(f"...the reel-in comes {late:.1f} s late, inside 4-7 s", 4.0 <= late <= 7.0)
check("later splashes from the same bite are ignored, and nothing is pressed before then",
      loop.decide(5.5, bar=True, splash=True) is None
      and run_until(loop, 5.6, loop.late_press_at - 0.01, bar=True, bobbed=True) == [])
check("then ONE late press, counted as a miss, not a catch",
      loop.decide(loop.late_press_at, bar=True) == fishing.PRESS
      and (loop.catches, loop.misses, loop.let_go) == (0, 1, 1) and loop.state == fishing.CASTING)
loop = make_loop(keeper=AlwaysLetGo())
loop.start(0.0, bar=True)
loop.decide(5.0, bar=True, splash=True)
check("if the fish gets away before the late press, nothing is pressed at all",
      loop.decide(6.0, bar=True, got_away=False) is None
      and loop.decide(6.1, bar=True, got_away=True) is None and loop.state == fishing.CASTING
      and loop.misses == 1)
loop = make_loop(keeper=AlwaysLetGo())
loop.start(0.0, bar=True)
loop.decide(5.0, bar=True, splash=True)
check("...or if the bar goes away (debounced, as when fishing)",
      run_until(loop, 5.1, 6.0, bar=False) == [] and loop.state == fishing.CASTING and loop.misses == 1)
loop = make_loop()
loop.start(0.0, bar=True)
check("with no keeper, every bite is reeled in at once, as before",
      loop.decide(5.0, bar=True, splash=True) == fishing.PRESS and loop.let_go == 0)
check(f"wow.py's default band is 90-95%, the band Zel asked to start with ({wow.SUCCESS_RATE_PERCENT})",
      wow.SUCCESS_RATE_PERCENT == (90, 95))
check("--success-rate reads '85-95', '85-95%', 'off'; refuses '95-85' and '0-90'",
      wow.parse_success_band("85-95") == (85, 95) and wow.parse_success_band("85-95%") == (85, 95)
      and wow.parse_success_band("off") == (100, 100)
      and _raises(lambda: wow.parse_success_band("95-85")) and _raises(lambda: wow.parse_success_band("0-90")))

print(f"\n{'ALL CHECKS PASSED' if failures == 0 else str(failures) + ' CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
