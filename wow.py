"""
========================================================================================================
wow.py - a World of Warcraft fishing helper, built on Hunter's Eye's engine
========================================================================================================

A small offshoot of Hunter's Eye: it does not touch main.py or anything Diablo II. It WATCHES the
screen for the "Fishing" cast bar, LISTENS to what the speakers are playing, and presses the
fishing key the moment the bobber's splash sound plays:

    1. Splash heard while the bar is up -> press F at once (reel in).
    2. Wait a random 1-6 seconds, press F again (cast), and listen again.
    3. "Your fish got away!" on screen, or the bar went away -> random wait, cast again.
    4. Nothing heard for 30 seconds -> press F anyway (restart), random wait, cast again.

Every key press is held down a random 0.2-0.6 s, usually near the short end.

    python wow.py              run it (WoW must be the window in front for any key to be pressed)
    python wow.py --dry-run    everything except the key presses - prints "would press" instead.
                               Try this first.
    python wow.py --replay FILE   offline: list every splash heard in a .wav (or a video, if ffmpeg
                               is installed). Nothing is pressed.

Hotkeys (global, like Hunter's Eye's):  F5 pause (falling tone)   F6 resume (rising tone)   End quit

!!! READ THIS FIRST !!!
World of Warcraft is an ONLINE game, and Blizzard's terms of use forbid automating gameplay with
third-party programs. Fishing bots are a well-known target: an account caught running one can be
suspended or permanently banned. This script reads only pixels and sound (no memory reading, no
injected code), but that does not make it allowed. Use it at your own risk, on an account you are
prepared to lose.

How it is built (see docs/wow_fishing.md for the measurements behind every number):
  eyes   core/presence.py        - the same reference-image check Hunter's Eye uses for "in play"
         core/frame_source.py    - the same screen capture, on two small regions of the WoW window
         core/window_region.py   - finding the WoW window, and whether it is in front
  ears   core/audio_source.py    - the speakers' output (WASAPI loopback, Windows)
         core/sound_match.py     - "did THAT sound just play?" against assets/wow/splash/*.wav
  hands  core/actions.py         - press_key(), through `keyboard` (real scancodes; see actions.py)
  brain  routes/fishing.py       - the loop's decisions, testable without any of the above
"""
import argparse
import random
import sys
import threading
import time
from pathlib import Path

import cv2 as cv
import numpy as np

from core import audio_source, frame_source, presence, sound_match, wav_io, window_region
from routes import fishing

# ======================================================================================================
# SETTINGS - edit these, then restart
# ======================================================================================================
FISH_KEY = "f"                      #the key that reels in AND casts (WoW's interact key on the bobber)
RECAST_DELAY_SECONDS = (1.0, 6.0)   #random wait after a reel-in / miss before casting again
NO_SPLASH_TIMEOUT_SECONDS = 30.0    #bar up this long with no splash heard -> press anyway, restart.
                                    #NOTE: in the reference video one bite came 20.2 s after the bar
                                    #appeared; see docs/wow_fishing.md before changing this.
KEY_HOLD_SECONDS = (0.2, 0.6)       #each press is held a random time in this range...
KEY_HOLD_TYPICAL_SECONDS = 0.28     #...most often near this value (a triangular distribution)

PAUSE_KEY = "f5"
RESUME_KEY = "f6"
QUIT_KEY = "end"

WINDOW_TITLE = "World of Warcraft"
SCREEN_CHECKS_PER_SECOND = 8        #the bar and the "got away" text; cheap - two small captures
# ======================================================================================================

ASSETS = Path(__file__).resolve().parent / "assets" / "wow"
BAR_CONFIG = ASSETS / "fishing_bar.json"
GOT_AWAY_CONFIG = ASSETS / "fish_got_away.json"
SPLASH_FOLDER = ASSETS / "splash"

#"Your fish got away!" is plain yellow text over moving water. Matching it as a picture would match
#the water too, so both the template and the screen are reduced to "which pixels are that yellow"
#first. The template image (fish_got_away_mask.png) is already such a mask.
YELLOW_LOW = (20, 120, 150)    #HSV, OpenCV's 0-180 hue scale; the text is pure (255, 255, 0) yellow
YELLOW_HIGH = (40, 255, 255)


def yellow_mask(bgr):
    return cv.inRange(cv.cvtColor(bgr, cv.COLOR_BGR2HSV), YELLOW_LOW, YELLOW_HIGH)


def hold_time(rng=random):
    """A key-hold duration: anywhere in KEY_HOLD_SECONDS, most often near the typical value."""
    low, high = KEY_HOLD_SECONDS
    return rng.triangular(low, high, min(max(KEY_HOLD_TYPICAL_SECONDS, low), high))


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def play_cue(rising):
    """Pause = falling two-tone, resume = rising two-tone. Never blocks the caller.

    Tonal on purpose: the splash matcher is listening to the same speakers, and a pure tone is
    about as far from a splash's broadband noise as a sound gets (card S-104 checks that these
    two cues are never heard as a splash).
    """
    tones = (660, 990) if rising else (990, 660)

    def play():
        try:
            import winsound
            for tone in tones:
                winsound.Beep(tone, 110)
        except Exception:                        # noqa: BLE001 - not Windows, or no sound device
            print("\a", end="", flush=True)

    threading.Thread(target=play, daemon=True).start()


def cue_samples(rising, sample_rate):
    """The cue as samples, for the test that the matcher never hears it as a splash."""
    tones = (660, 990) if rising else (990, 660)
    t = np.arange(int(0.11 * sample_rate)) / sample_rate
    return np.concatenate([0.5 * np.sin(2 * np.pi * f * t) for f in tones]).astype(np.float32)


# ------------------------------------------------------------------------------------------------------
# Eyes
# ------------------------------------------------------------------------------------------------------

class ScreenWatcher:
    """Reads the fishing bar and the "got away" text off two small regions of the WoW window.

    Runs on its own daemon thread (mss captures belong to the thread that made them). Publishes
    the latest reading under a lock; every answer is True / False / None, and a reading older than
    a second turns into None rather than being trusted forever.
    """

    def __init__(self, bar_check, got_away_check):
        self.bar_check = bar_check
        self.got_away_check = got_away_check
        self._lock = threading.Lock()
        self._reading = (None, None, None, None, 0.0)   #bar, got_away, foreground, scores, when
        self._stop = threading.Event()
        self.window_found = None

    def latest(self, max_age=1.0):
        with self._lock:
            bar, got_away, foreground, scores, when = self._reading
        if time.monotonic() - when > max_age:
            return None, None, foreground, scores
        return bar, got_away, foreground, scores

    def stop(self):
        self._stop.set()

    def run(self):
        sources = {}
        handle, client = None, None
        next_lookup = 0.0
        period = 1.0 / SCREEN_CHECKS_PER_SECOND
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                if started >= next_lookup:
                    #Refreshed every 2 s, never cached for the session: the window can move,
                    #resize, or be closed and reopened (CLAUDE.md, window_region.py).
                    next_lookup = started + 2.0
                    found = window_region.find_window(WINDOW_TITLE)
                    if found is not None:
                        handle, client = found
                    elif window_region.available():
                        handle, client = None, None
                    else:
                        handle, client = None, _primary_monitor()
                    self.window_found = found is not None if window_region.available() else None
                if client is None:
                    self._publish(None, None, False if window_region.available() else None, None)
                else:
                    bar, got_away, scores = self._read(sources, client)
                    self._publish(bar, got_away, window_region.is_foreground(handle), scores)
                self._stop.wait(max(0.0, period - (time.monotonic() - started)))
        finally:
            for source, _ in sources.values():
                source.close()

    def _read(self, sources, client):
        x, y, w, h = client
        bar_frame = self._grab(sources, "bar", client, self.bar_check)
        text_frame = self._grab(sources, "text", client, self.got_away_check)
        bar = got_away = None
        bar_score = text_score = None
        if bar_frame is not None:
            gray = cv.cvtColor(bar_frame, cv.COLOR_BGRA2GRAY)
            bar, bar_score = self.bar_check.check(gray, w, region=(0, 0, 1, 1))
        if text_frame is not None:
            got_away, text_score = self.got_away_check.check(
                yellow_mask(cv.cvtColor(text_frame, cv.COLOR_BGRA2BGR)), w, region=(0, 0, 1, 1))
        return bar, got_away, (bar_score, text_score)

    @staticmethod
    def _grab(sources, key, client, check):
        rx, ry, rw, rh = check.references[0].search_region
        x, y, w, h = client
        region = {"left": int(x + rx * w), "top": int(y + ry * h),
                  "width": max(1, int(rw * w)), "height": max(1, int(rh * h))}
        cached = sources.get(key)
        if cached is None or cached[1] != region:
            if cached is not None:
                cached[0].close()
            #MSS, not DXGI: these are two small regions a few times a second, and a DXGI source is
            #a per-output singleton two consumers would fight over (frame_source.py, EH#39).
            cached = (frame_source.FrameSource(region, backend=frame_source.MSS), region)
            sources[key] = cached
        try:
            return cached[0].grab()
        except Exception as exc:                 # noqa: BLE001 - a failed grab is "cannot tell"
            log(f"screen capture failed ({exc}) - treating the screen as unreadable")
            return None

    def _publish(self, bar, got_away, foreground, scores):
        with self._lock:
            self._reading = (bar, got_away, foreground, scores, time.monotonic())


def _primary_monitor():
    import mss
    with getattr(mss, "MSS", mss.mss)() as sct:
        mon = sct.monitors[1]
    return mon["left"], mon["top"], mon["width"], mon["height"]


# ------------------------------------------------------------------------------------------------------
# Ears
# ------------------------------------------------------------------------------------------------------

class SplashListener:
    """Feeds the speakers' output to the matcher on a daemon thread; records when a splash played."""

    def __init__(self, source, reference):
        self.source = source
        self.matcher = sound_match.SoundMatcher(reference, source.sample_rate)
        self._lock = threading.Lock()
        self._splashes = 0
        self._stop = threading.Event()

    def take_splashes(self):
        """How many splashes were heard since the last call."""
        with self._lock:
            count, self._splashes = self._splashes, 0
        return count

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            chunk = self.source.read(timeout=0.1)
            if chunk is None:
                continue
            for hit in self.matcher.feed(chunk):
                with self._lock:
                    self._splashes += 1
                log(f"splash heard (rise {hit.rise_db:.0f} dB, {hit.over_recent_db:+.0f} dB over "
                    f"recent, shape {hit.shape:.2f})")


# ------------------------------------------------------------------------------------------------------
# Offline replay
# ------------------------------------------------------------------------------------------------------

def replay(path, reference):
    """Prints every splash the matcher hears in a recording. Accepts .wav, or any video ffmpeg reads."""
    path = Path(path)
    temp = None
    if path.suffix.lower() != ".wav":
        import shutil
        import subprocess
        import tempfile
        if shutil.which("ffmpeg") is None:
            print("Not a .wav, and ffmpeg is not installed to convert it. Either install ffmpeg "
                  "(winget install ffmpeg) or convert it yourself:\n"
                  f'  ffmpeg -i "{path}" -vn -ac 1 -ar 48000 out.wav')
            return 2
        temp = Path(tempfile.mkdtemp()) / "audio.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", "48000", "-c:a", "pcm_s16le", str(temp)], check=True)
        path = temp
    samples, rate = wav_io.read_wav(path)
    hits = sound_match.scan(samples, rate, reference)
    print(f"{len(samples) / rate:.1f}s of audio, {len(hits)} splash(es) heard:")
    for hit in hits:
        minutes, seconds = divmod(hit.time, 60)
        print(f"  {int(minutes)}:{seconds:05.2f}  rise {hit.rise_db:4.0f} dB   over recent "
              f"{hit.over_recent_db:+4.0f} dB   shape {hit.shape:.2f}")
    if temp is not None:
        temp.unlink(missing_ok=True)
    return 0


# ------------------------------------------------------------------------------------------------------
# The live loop
# ------------------------------------------------------------------------------------------------------

def run(dry_run):
    try:
        import keyboard
    except ImportError:
        print("The `keyboard` package is missing - run: python -m pip install -r requirements.txt")
        return 2
    from core import actions

    bar_check = presence.load(BAR_CONFIG)
    got_away_check = presence.load(GOT_AWAY_CONFIG)
    reference = sound_match.load_reference_folder(SPLASH_FOLDER)
    if bar_check is None or got_away_check is None or reference is None:
        print(f"Missing or broken reference files in {ASSETS} - see the warnings above.")
        return 2
    try:
        source = audio_source.LoopbackSource()
    except audio_source.AudioSourceError as exc:
        print(f"Cannot listen to the game's sound: {exc}")
        return 2
    log(f"listening to: {source.device_name} ({source.sample_rate} Hz)")

    screen = ScreenWatcher(bar_check, got_away_check)
    ears = SplashListener(source, reference)
    loop = fishing.FishingLoop(recast_delay=RECAST_DELAY_SECONDS,
                               no_splash_timeout=NO_SPLASH_TIMEOUT_SECONDS, log=log)
    threading.Thread(target=screen.run, daemon=True, name="screen").start()
    threading.Thread(target=ears.run, daemon=True, name="audio").start()

    quit_requested = threading.Event()
    paused = threading.Event()
    restart = threading.Event()

    def pause():
        if not paused.is_set():
            paused.set()
            play_cue(rising=False)
            log(f"PAUSED - {RESUME_KEY.upper()} to resume")

    def resume():
        if paused.is_set():
            paused.clear()
            restart.set()
            play_cue(rising=True)
            log("resumed")

    keyboard.add_hotkey(PAUSE_KEY, pause)
    keyboard.add_hotkey(RESUME_KEY, resume)
    keyboard.add_hotkey(QUIT_KEY, quit_requested.set)
    log(f"{'DRY RUN - no key will be pressed. ' if dry_run else ''}"
        f"{PAUSE_KEY.upper()} pause, {RESUME_KEY.upper()} resume, {QUIT_KEY.title()} quit.")
    play_cue(rising=True)

    def press():
        hold = hold_time()
        if dry_run:
            log(f"(dry run) would press {FISH_KEY.upper()} for {hold:.2f}s")
            return
        actions.press_key(FISH_KEY, hold=hold)

    started_at = time.monotonic()
    #Wait (briefly) for a first screen reading so the loop starts from what is really there.
    deadline = time.monotonic() + 2.0
    while screen.latest()[0] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    loop.start(time.monotonic(), screen.latest()[0])
    blocked_reason = None
    try:
        while not quit_requested.is_set() and loop.state != fishing.STOPPED:
            now = time.monotonic()
            bar, got_away, foreground, _scores = screen.latest()
            if restart.is_set():
                restart.clear()
                ears.take_splashes()           #a splash from before the pause is stale
                loop.start(now, bar)
            reason = ("paused" if paused.is_set() else
                      "WoW window not found" if screen.window_found is False else
                      "WoW is not the window in front" if foreground is False else None)
            if reason != blocked_reason:
                if reason and reason != "paused":
                    log(f"waiting: {reason} - no keys will be pressed")
                elif blocked_reason and blocked_reason != "paused" and not reason:
                    log("WoW is in front again")
                    loop.start(now, bar)       #the old timers are meaningless after a gap
                blocked_reason = reason
            splash = ears.take_splashes() > 0
            if loop.decide(now, bar, splash=splash, got_away=got_away,
                           allowed=reason is None) == fishing.PRESS:
                press()
            time.sleep(0.01)                   #100 Hz: at most 10 ms between a splash and the press
    except KeyboardInterrupt:
        pass
    finally:
        screen.stop()
        ears.stop()
        source.close()
        keyboard.unhook_all_hotkeys()
    minutes = (time.monotonic() - started_at) / 60
    if loop.state == fishing.STOPPED:
        log(f"stopped: {loop.reason}")
    log(f"done after {minutes:.1f} min: {loop.catches} reeled in, {loop.misses} missed"
        f"{f', {source.dropped} audio chunks dropped' if source.dropped else ''}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="WoW fishing helper (see the top of wow.py).")
    parser.add_argument("--dry-run", action="store_true",
                        help="watch and listen, but print instead of pressing any key")
    parser.add_argument("--replay", metavar="FILE",
                        help="list every splash heard in a recording, then exit")
    args = parser.parse_args(argv)
    if args.replay:
        reference = sound_match.load_reference_folder(SPLASH_FOLDER)
        if reference is None:
            print(f"No usable reference clips in {SPLASH_FOLDER}.")
            return 2
        return replay(args.replay, reference)
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
