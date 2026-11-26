"""
========================================================================================================
wow.py - a World of Warcraft fishing helper, built on Hunter's Eye's engine
========================================================================================================

A small offshoot of Hunter's Eye: it does not touch main.py or anything Diablo II. It WATCHES the
screen for the "Fishing" cast bar, WATCHES the lure, LISTENS to what the speakers are playing, and
presses the fishing key the moment a fish bites - when the splash is heard OR the lure is seen
bobbing, whichever comes first:

    1. Bite (splash heard, or lure bobbed) while the bar is up -> press F at once (reel in).
    2. Wait a random 1-6 seconds, press F again (cast), and watch and listen again.
    3. "Your fish got away!" on screen, or the bar went away -> random wait, cast again.
    4. No bite for 30 seconds -> press F anyway (restart), random wait, cast again.
    5. It does not reel in EVERY bite: once its success rate (bites reeled in / casts that ended)
       reaches 99%, it lets some go - reeling in 4-7 s late - until it is back at 90%.
       SUCCESS_RATE_PERCENT below, or --success-rate, or the GUI's Setup tab, sets the band.

Every key press is held down a random 0.2-0.6 s, usually near the short end.

    python wow.py              run it (WoW must be the window in front for any key to be pressed)
    python wow.py --dry-run    everything except the key presses - prints "would press" instead.
                               Try this first.
    python wow.py --replay FILE   offline: list every splash heard in a .wav (or a video, if ffmpeg
                               is installed). Nothing is pressed.
    python wow.py --check-audio   listen for 15 s and say what was heard - run it with WoW (or a
                               recording) playing if the splash is never heard live.
    python wow.py --list-audio    the sound devices it can listen to (see AUDIO_DEVICE below).
    python wow.py --read-chat     read the chat box once and say what it saw - checks the catch
                               counter can see your chat (the GUI's Setup tab runs this too).
    python wow_gui.py          the same, in a window: start/stop, the auto-pause countdown, what
                               was caught, the catch rate and what it is worth per hour.

It also COUNTS WHAT IS CAUGHT, by reading the green "You receive loot: [...]" lines in the chat box
(core/text_feed.py finds the new lines, OCR reads them), and AUTO-PAUSES after 30 minutes without
an F5 press, so it never fishes on for hours with nobody at the keyboard. Every F5 restarts the 30.

Every run also writes a log file to recordings/wow/ with more detail than the console: what it
heard every 10 s, every loud sound that was NOT taken for a splash and why, the lure found and lost.

Hotkeys (global, like Hunter's Eye's):  F5 pause / resume (falling tone, rising tone)   End quit
Auto-pause (no F5 for AUTO_PAUSE_MINUTES): three falling tones.

!!! READ THIS FIRST !!!
World of Warcraft is an ONLINE game, and Blizzard's terms of use forbid automating gameplay with
third-party programs. Fishing bots are a well-known target: an account caught running one can be
suspended or permanently banned. This script reads only pixels and sound (no memory reading, no
injected code), but that does not make it allowed. Use it at your own risk, on an account you are
prepared to lose.

How it is built (see docs/wow_fishing.md for the measurements behind every number):
  eyes   core/presence.py        - the same reference-image check Hunter's Eye uses for "in play";
                                   also finds the lure, by the cog icon WoW draws over it
         core/spot_watch.py      - "did something suddenly appear at this spot?" - the spray of
                                   a bite in the water under the lure
         core/frame_source.py    - the same screen capture, on two small regions of the WoW window
         core/window_region.py   - finding the WoW window, and whether it is in front
  ears   core/audio_source.py    - the speakers' output (WASAPI loopback, Windows)
         core/sound_match.py     - "did THAT sound just play?" against assets/wow/splash/*.wav
  hands  core/actions.py         - press_key(), through `keyboard` (real scancodes; see actions.py)
         core/text_feed.py       - "which lines of the chat box are new?", then
         core/text_detection.py  - OCR reads only those lines (and only the green loot ones)
  brain  routes/fishing.py       - the loop's decisions, and the auto-pause timer
         routes/fish_tally.py    - what was caught, prices, catch rate, earnings
"""
import argparse
import difflib
import json
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path

import cv2 as cv
import numpy as np

from core import (audio_source, frame_source, presence, sound_match, spot_watch, text_detection,
                  text_feed, wav_io, window_region)
from routes import fish_tally, fishing

# ======================================================================================================
# SETTINGS - edit these, then restart
# ======================================================================================================
FISH_KEY = "f"                      #the key that reels in AND casts (WoW's interact key on the bobber)
RECAST_DELAY_SECONDS = (1.0, 6.0)   #random wait after a reel-in / miss before casting again
NO_SPLASH_TIMEOUT_SECONDS = 30.0    #bar up this long with no bite -> press anyway, restart. The bar
                                    #itself lasts about 30 s; see docs/wow_fishing.md.
KEY_HOLD_SECONDS = (0.2, 0.6)       #each press is held a random time in this range...
KEY_HOLD_TYPICAL_SECONDS = 0.28     #...most often near this value (a triangular distribution)

LISTEN_FOR_SPLASH = True            #reel in when the splash is heard...
WATCH_LURE = True                   #...or when the lure is seen bobbing (whichever comes first)
AUDIO_DEVICE = None                 #None = the Windows default speakers. If WoW plays its sound
                                    #somewhere else (a headset), put part of that device's name
                                    #here, as `python wow.py --list-audio` prints it: "Headset"

AUTO_PAUSE_MINUTES = 30             #pause by itself after this long with no F5 press (or the GUI's
                                    #pause button). Every manual pause/resume restarts the count.
AUTO_PAUSE_CHOICES = (10, 15, 20, 30, 45, 60, 90, 120)   #what the GUI (and --auto-pause) offer
READ_CHAT = True                    #count catches from the chat box's "You receive loot" lines
SUCCESS_RATE_PERCENT = (90, 99)     #keep bites reeled in / casts that ended inside this band, by
                                    #letting a bite go now and then (reeled in 4-7 s late) once the
                                    #rate reaches the top, until it is back at the bottom. (100, 100)
                                    #= reel in every bite. The GUI's Setup tab, or --success-rate 85-95.

PAUSE_KEY = "f5"                    #pauses, and the same key resumes
QUIT_KEY = "end"

WINDOW_TITLE = "World of Warcraft"
SCREEN_CHECKS_PER_SECOND = 8        #the bar and the "got away" text; cheap - two small captures
LURE_CHECKS_PER_SECOND = 20         #the lure: a bite's spray lasts ~0.25 s; 2 ms per look
# ======================================================================================================

ASSETS = Path(__file__).resolve().parent / "assets" / "wow"
BAR_CONFIG = ASSETS / "fishing_bar.json"
GOT_AWAY_CONFIG = ASSETS / "fish_got_away.json"
BOBBER_CONFIG = ASSETS / "bobber.json"
SPLASH_FOLDER = ASSETS / "splash"
CHAT_CONFIG = ASSETS / "chat.json"
PRICES = ASSETS / "fish_prices.json"
LOG_FOLDER = Path(__file__).resolve().parent / "recordings" / "wow"   #gitignored, like all recordings
#This PC's own files, next to the logs (gitignored, so a `git pull` never touches them):
USER_CHAT_CONFIG = LOG_FOLDER / "chat.json"        #the chat box, if it is not where the default says
USER_PRICES = LOG_FOLDER / "fish_prices.json"      #new fish, and prices typed into the GUI
HISTORY = LOG_FOLDER / "catches.csv"               #every catch ever counted, one row each
#A loot line that shows up this soon after pausing is the fish reeled in just before: still counted.
PAUSE_GRACE_SECONDS = 3.0
STATUS_EVERY_SECONDS = 0.5                         #--control: how often the GUI is told the state

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


_log_file = None
_log_lock = threading.Lock()
_print_lock = threading.Lock()


def write_line(line):
    """One whole line to the console. Several threads print (the loop, the eyes, the ears, the
    command channel), and print() writes the text and the newline separately - so two threads can
    splice their lines together. Under --control the panel reads these lines, and a spliced
    "@catch" line would be a lost catch; one locked write each keeps every line whole."""
    with _print_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def open_log_file(kind):
    """Starts recordings/wow/<kind>_<date-time>.log; every log() line goes there as well."""
    global _log_file
    try:
        LOG_FOLDER.mkdir(parents=True, exist_ok=True)
        path = LOG_FOLDER / f"{kind}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"
        _log_file = open(path, "a", encoding="utf-8", buffering=1)
        return path
    except OSError as exc:
        print(f"(could not open a log file in {LOG_FOLDER}: {exc} - console only)")
        return None


def close_log_file():
    global _log_file
    with _log_lock:
        if _log_file is not None:
            _log_file.close()
            _log_file = None


def log(message, console=True):
    """One line, timestamped. console=False lines go only to the log file: detail that helps
    find out why a fish was missed, but would bury the console if printed every few seconds."""
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    if console:
        write_line(line)
    with _log_lock:
        if _log_file is not None:
            try:
                _log_file.write(line + "\n")
            except OSError:
                pass


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


def play_auto_pause_cue():
    """Three falling tones: different from F5's two, so "it paused itself" is clear by ear."""
    def play():
        try:
            import winsound
            for tone in (990, 830, 660):
                winsound.Beep(tone, 140)
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
        self.client = None        #the WoW window's client area (x, y, w, h), for the lure watcher

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
        last_bar = None
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
                    self.client = client
                if client is None:
                    self._publish(None, None, False if window_region.available() else None, None)
                else:
                    bar, got_away, scores = self._read(sources, client)
                    if bar is not None and bar != last_bar:
                        log(f"fishing bar {'seen' if bar else 'gone'} (score "
                            f"{scores[0] if scores[0] is not None else float('nan'):.2f})",
                            console=False)
                        last_bar = bar
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


class BobberEye:
    """Finds the lure by the cog WoW draws over it, and watches the water under it for a bite.

    Frame-driven and clock-free (time is passed in), so the tests can feed it a real recording.
    look() takes a BGR frame plus where that frame sits in the window, and answers (seen, bite):
        seen: True = the cog is there, False = it is not, None = cannot tell (no frame).
        bite: True on the frame the spray is seen (once per lure), False = watching and calm,
              None = not judging (no lure yet, or it only just landed - its own landing splash is
              not a bite).

    TWO SPEEDS. With no lure, the frame is the whole search area, searched at HALF size: 4 ms
    instead of about 100 at full size (measured), at a score the full-size check then confirms.
    With a lure, the frame is only tracking_region() - about 140x220 pixels holding the cog and the
    water under it - checked at full size, 2 ms a look, which is what makes 20 looks a second
    cheap. The spray lasts about a quarter of a second, so the rate matters: all 9 bites in the
    recordings were seen at 8 to 20 looks a second, but only with a single-frame trigger (see
    bobber.json); requiring two frames in a row lost 4 of 9 at 12 a second.

    The lure drifts a little, so the watched patch follows the cog every frame; a jump of more than
    `moved_px` is a different lure (a new cast) and starts over.
    """

    COARSE_SCALE = 0.5
    COARSE_THRESHOLD = 0.70   #half-size scores of the real cog: 0.84-0.92; full size confirms it

    def __init__(self, marker, watch):
        self.marker = marker
        self.offset = tuple(watch["offset"])
        self.size = tuple(watch["size"])
        self.settle_seconds = float(watch["settle_seconds"])
        self.moved_px = float(watch["moved_px"])
        self.spot = spot_watch.SpotWatcher(watch["pixel_low"], watch["pixel_high"],
                                           ratio=watch["ratio"], margin=watch["margin"],
                                           consecutive=watch["consecutive"])
        self.source_width = marker.references[0].source_width
        self.search_region = marker.references[0].search_region
        self.centre = None      #the cog's centre in WINDOW pixels, once found
        self.since = None       #when the full-size check first confirmed this lure
        self._region = None     #the tracking region, re-centred only when the lure drifts
        self._anchor = None     #the cog centre the region was built around
        self._misses = 0

    def lose(self):
        self.centre = self.since = self._region = self._anchor = None
        self._misses = 0
        self.spot.reset()

    def look(self, frame_bgr, origin, window_width, now):
        if frame_bgr is None or frame_bgr.size == 0 or not window_width:
            return None, None
        if self.centre is None:
            return self._search(frame_bgr, origin, window_width)
        scale = window_width / self.source_width
        gray = cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY)
        found, _score, box = self.marker.locate(gray, window_width, region=(0, 0, 1, 1))
        if found is None:
            return None, None
        if not found:
            #Three misses in a row, not one: a spell effect or the pointer can cover the cog for a
            #frame, and starting over would throw away the calm-water history the bite needs.
            self._misses += 1
            if self._misses >= 3:
                self.lose()
            return False, None
        self._misses = 0
        x, y, w, h = box
        centre = (origin[0] + x + w / 2.0, origin[1] + y + h / 2.0)
        if self.since is None or np.hypot(centre[0] - self.centre[0],
                                          centre[1] - self.centre[1]) > self.moved_px * scale:
            self.spot.reset()
            self.since = now
        self.centre = centre
        #Keep the capture centred on the lure as it drifts (or after the jump above), so the water
        #under it never slides out of the captured area. Measured: a lure that landed and then
        #drifted 80 px down left its water outside a region fixed at the first sighting.
        if np.hypot(centre[0] - self._anchor[0], centre[1] - self._anchor[1]) > self.moved_px * scale / 2:
            self._set_region(centre, window_width)
        if now - self.since < self.settle_seconds:
            return True, None
        pw, ph = self.size[0] * scale, self.size[1] * scale
        px = int(round(centre[0] + self.offset[0] * scale - pw / 2 - origin[0]))
        py = int(round(centre[1] + self.offset[1] * scale - ph / 2 - origin[1]))
        patch = frame_bgr[max(0, py):max(0, py + int(round(ph))),
                          max(0, px):max(0, px + int(round(pw)))]
        if patch.shape[0] < ph * 0.8 or patch.shape[1] < pw * 0.8:
            return True, None             #the lure's water is cut off by the frame edge
        return True, self.spot.feed(patch[:, :, :3])

    def _search(self, frame_bgr, origin, window_width):
        small = cv.resize(cv.cvtColor(frame_bgr, cv.COLOR_BGR2GRAY), None, fx=self.COARSE_SCALE,
                          fy=self.COARSE_SCALE, interpolation=cv.INTER_AREA)
        _found, score, box = self.marker.locate(small, window_width * self.COARSE_SCALE,
                                                region=(0, 0, 1, 1))
        if score is None:
            return None, None
        if score >= self.COARSE_THRESHOLD:
            x, y, w, h = (v / self.COARSE_SCALE for v in box)
            self.centre = (origin[0] + x + w / 2.0, origin[1] + y + h / 2.0)
            self._set_region(self.centre, window_width)
        return False, None

    def _set_region(self, centre, window_width):
        self._anchor = centre
        scale = window_width / self.source_width
        half_w = (max(abs(self.offset[0]) + self.size[0] / 2, 30) + self.moved_px) * scale
        top = (30 + self.moved_px) * scale
        bottom = (self.offset[1] + self.size[1] / 2 + self.moved_px) * scale
        self._region = (max(0, int(centre[0] - half_w)), max(0, int(centre[1] - top)),
                        int(2 * half_w), int(top + bottom))

    def capture_region(self, window_width, window_height):
        """What to capture next, in window pixels: the small tracking area while a lure is known
        (moved only when the lure has drifted half of moved_px, so the capture is not rebuilt
        for every pixel of drift), else the whole search area."""
        if self._region is not None:
            return self._region
        rx, ry, rw, rh = self.search_region
        return (int(rx * window_width), int(ry * window_height),
                int(rw * window_width), int(rh * window_height))


def load_bobber_eye(path=BOBBER_CONFIG):
    """The lure watcher from its config, or None (with the reason printed) if it cannot be built."""
    import json
    marker = presence.load(path)
    if marker is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return BobberEye(marker, json.load(handle)["watch"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"WARNING: {path}: the 'watch' settings are missing or broken ({exc}) - "
              f"the lure will not be watched.")
        return None


def _primary_monitor():
    import mss
    with getattr(mss, "MSS", mss.mss)() as sct:
        mon = sct.monitors[1]
    return mon["left"], mon["top"], mon["width"], mon["height"]


class LureWatcher:
    """Runs a BobberEye on its own daemon thread, LURE_CHECKS_PER_SECOND times a second.

    It only looks while the fishing bar is up: between casts there is no lure to watch, and the
    search over the whole water (the expensive part) would be wasted. Captures only the small area
    the eye asks for, with its own mss source (mss captures belong to the thread that made them).
    """

    def __init__(self, eye, screen):
        self.eye = eye
        self.screen = screen
        self._lock = threading.Lock()
        self._bites = 0
        self._stop = threading.Event()

    def take_bites(self):
        """How many bites were seen since the last call."""
        with self._lock:
            count, self._bites = self._bites, 0
        return count

    def stop(self):
        self._stop.set()

    def run(self):
        source, source_region = None, None
        seen_before = None
        period = 1.0 / LURE_CHECKS_PER_SECOND
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                client = self.screen.client
                if client is None or self.screen.latest()[0] is not True:
                    if self.eye.centre is not None:
                        self.eye.lose()
                    seen_before = None
                    self._stop.wait(0.1)
                    continue
                cx, cy, cw, ch = client
                x, y, w, h = self.eye.capture_region(cw, ch)
                region = {"left": int(cx + x), "top": int(cy + y),
                          "width": max(1, int(w)), "height": max(1, int(h))}
                if region != source_region:
                    if source is not None:
                        source.close()
                    source = frame_source.FrameSource(region, backend=frame_source.MSS)
                    source_region = region
                try:
                    frame = source.grab()
                except Exception as exc:             # noqa: BLE001 - "cannot tell", never a crash
                    log(f"lure capture failed ({exc})", console=False)
                    frame = None
                if frame is not None:
                    searching = self.eye.centre is None
                    seen, bite = self.eye.look(np.ascontiguousarray(frame[:, :, :3]), (x, y), cw,
                                               time.monotonic())
                    if seen is not None and seen != seen_before and not (searching and not seen):
                        log("lure found - watching it" if seen else "lure lost", console=False)
                        seen_before = seen
                    if bite:
                        count, baseline, needed = self.eye.spot.last
                        log(f"lure bobbed ({count} spray pixels; calm water {baseline:.0f}, "
                            f"needed {needed:.0f})")
                        with self._lock:
                            self._bites += 1
                #Searching is 4 looks a second (the expensive part, over the whole water);
                #watching a known lure is the full rate.
                wait = 0.25 if self.eye.centre is None else period
                self._stop.wait(max(0.0, wait - (time.monotonic() - started)))
        finally:
            if source is not None:
                source.close()


# ------------------------------------------------------------------------------------------------------
# The chat box - counting catches
# ------------------------------------------------------------------------------------------------------

def load_chat_config(path=CHAT_CONFIG, user_path=USER_CHAT_CONFIG):
    """The chat settings: the repo's defaults, with this PC's own box (if one was set) on top.
    None, with the reason printed, if they cannot be used."""
    config = {}
    for source in (path, user_path):
        if source is None or not Path(source).exists():
            continue
        try:
            with open(source, encoding="utf-8") as handle:
                config.update(json.load(handle))
        except (OSError, ValueError) as exc:
            print(f"WARNING: {source}: could not read it ({exc}) - "
                  f"{'catches will not be counted' if source == path else 'using the default chat box'}.")
            if source == path:
                return None
    try:
        region = [float(v) for v in config["region"]]
        if len(region) != 4 or not all(0.0 <= v <= 1.0 for v in region) or region[2] <= 0 or region[3] <= 0:
            raise ValueError(f"region must be 4 fractions of the window, got {config['region']}")
        config["region"] = region
        for key in ("line_pitch", "source_height", "loot_min_pixels", "checks_per_second",
                    "loot_wait_seconds"):
            config[key] = float(config[key])
            if config[key] <= 0:
                raise ValueError(f"{key} must be above 0")
        config["ocr_thresholds"] = [int(v) for v in config["ocr_thresholds"]] or [110]
        config["loot_low"] = tuple(int(v) for v in config["loot_low"])
        config["loot_high"] = tuple(int(v) for v in config["loot_high"])
    except (KeyError, TypeError, ValueError) as exc:
        print(f"WARNING: the chat settings are broken ({exc}) - catches will not be counted.")
        return None
    return config


def chat_pitch(config, window_height):
    """The chat's line pitch in pixels for a window this tall (the font scales with the UI)."""
    return max(4, int(round(config["line_pitch"] * window_height / config["source_height"])))


def chat_region(config, client):
    """The chat box in screen pixels, as an mss region, for a window at `client` (x, y, w, h)."""
    x, y, w, h = client
    rx, ry, rw, rh = config["region"]
    return {"left": int(round(x + rx * w)), "top": int(round(y + ry * h)),
            "width": max(1, int(round(rw * w))), "height": max(1, int(round(rh * h)))}


def ocr_line(image, threshold):
    """One line of chat as text. The brightest channel, not grey: the loot words are pure green
    (0,160,0), which a grey conversion turns into 94 - below any threshold that keeps the background
    out (EH#48). Measured: every line of Zel's chat screenshot read exactly at 110."""
    gray = image.max(axis=2) if image.ndim == 3 else image
    return text_detection.read_line(np.ascontiguousarray(gray), threshold=threshold, whitelist=None)


def looks_like_loot(text):
    """Does OCR'd text look like a loot line that was misread (worth a warning), rather than some
    other green line (not worth one)? Forgiving: 'receive' or 'loot', give or take a letter."""
    words = re.findall(r"[a-z]+", text.lower())
    return any(difflib.SequenceMatcher(None, word, target).ratio() >= 0.75
               for word in words for target in ("receive", "loot"))


class ChatReader:
    """Finds the new "You receive loot: [...]" lines in pictures of the chat box. Frame-driven and
    clock-free (time is passed in), so the tests feed it real chat pictures.

    look(frame, now) -> [(name as read, count, closed, how)], how = "read" for a loot line seen
    arriving, "inferred" for the one case pixels cannot settle (see below).

    WHAT IS READ: core/text_feed.py says which lines are new; only those with the loot green in
    them (measured 335-379 green pixels on a loot line, 0 on any other) are OCR'd - about 0.1 s,
    once per catch. A misread is tried again at the other thresholds before giving up.

    THE PITCH IS CHECKED, NOT TRUSTED: chat.json says how far apart the lines are (18 px at 1080p
    with WoW's default chat font), but a smaller chat font makes it 16, and lines cut 2 px off
    drift further off on every line - the box then stops lining up with itself and catches are
    missed (2 of 3, on the owner's chat, 2026-11-25). So whenever the box changed (a new line, or
    "cannot tell"), the pitch is measured off the picture (text_feed.estimate_pitch, ~2 ms), and
    when PITCH_VOTES measurements in a row agree on another pitch, reading restarts at that one.
    Until one measurement has agreed with the pitch in use, every look measures (so a wrong pitch
    is fixed within half a second of starting, not at the first catch).

    THE AMBIGUOUS CASE: a chat box showing nothing but the same loot line, say eight Snappers in a
    row, looks exactly the same after a ninth arrives. text_feed flags that as `ambiguous`. So when
    the loop reels in on a bite it calls expect(now); if no loot line has arrived `loot_wait_seconds`
    later, the box looked ambiguous meanwhile, and its bottom line is a loot line, that line is
    counted once more, as "inferred". cancel() (the fish got away) drops the expectation.
    """

    PITCH_VOTES = 2            #measurements in a row that must agree before the pitch is changed

    def __init__(self, config, pitch, ocr=ocr_line, log=print, detail=None):
        self.config = config
        self.ocr = ocr
        self.log = log                                 #worth a line on the console
        self.detail = detail or (lambda message: None)  #only for the log file
        self.expect_until = None
        self._loot_since = False
        self._ambiguous_since = False
        self.lost = False          #the last look did not line up with the one before
        self._votes = []           #the last pitch measurements that disagreed with the one in use
        self._use_pitch(pitch)
        self.pitch_confirmed = False   #measured on every look until a picture agrees with it once

    def _use_pitch(self, pitch):
        self.pitch = int(pitch)
        self.feed = text_feed.TextFeed(self.pitch)
        scale = self.pitch / self.config["line_pitch"]
        self.min_green = self.config["loot_min_pixels"] * scale * scale
        self._votes = []

    def _check_pitch(self, frame):
        """Measure the line pitch off this picture; True if reading restarted at a new pitch."""
        measured = text_feed.estimate_pitch(frame)
        if measured is None:
            return False               #too few lines to tell: no vote either way
        if measured == self.pitch:
            self._votes = []
            self.pitch_confirmed = True
            return False
        self._votes = (self._votes + [measured])[-self.PITCH_VOTES:]
        if len(self._votes) < self.PITCH_VOTES or len(set(self._votes)) != 1:
            return False
        self.log(f"chat: the lines are {measured} px apart, not {self.pitch} - reading at "
                 f"{measured} from now on (a different chat font size?)")
        self._use_pitch(measured)
        self.feed.feed(frame)          #the first look at the new pitch: compared with the next one
        self.lost = True
        return True

    def expect(self, now):
        self.expect_until = now + self.config["loot_wait_seconds"]
        self._loot_since = False
        self._ambiguous_since = False

    def cancel(self):
        self.expect_until = None

    def is_loot(self, image):
        hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
        green = cv.inRange(hsv, self.config["loot_low"], self.config["loot_high"])
        return int(cv.countNonZero(green)) >= self.min_green

    def read(self, image):
        """(name, count, closed) from a loot line's picture, or None; plus the text last read."""
        text = ""
        for threshold in self.config["ocr_thresholds"]:
            text = self.ocr(image, threshold)
            parsed = fish_tally.parse_loot_line(text)
            if parsed is not None and parsed[2]:
                return parsed, text
        return fish_tally.parse_loot_line(text), text

    def look(self, frame, now):
        events = []
        new = self.feed.feed(frame)
        self.lost = new is None
        if (new is None or new or not self.pitch_confirmed) and self._check_pitch(frame):
            new = []                   #read at the wrong pitch: those "new lines" were miscut
        for image in new or []:
            if not self.is_loot(image):
                continue
            parsed, text = self.read(image)
            self.detail(f"chat: new loot line, read as {text!r}")
            if parsed is None:
                if looks_like_loot(text):
                    self.log(f"chat: a loot line arrived but could not be read (OCR: {text!r})")
                else:
                    self.detail("chat: ...green, but not a loot line - not counted")
                continue
            events.append((*parsed, "read"))
            self.expect_until = None
        if self.expect_until is not None:
            self._ambiguous_since |= self.feed.ambiguous
            if now >= self.expect_until:
                self.expect_until = None
                lines = self.feed.lines or []
                if self._ambiguous_since and lines and self.is_loot(lines[-1].image):
                    parsed, text = self.read(lines[-1].image)
                    if parsed is not None:
                        self.log(f"chat: no new line after the reel-in, but the box shows only "
                                 f"repeats of {parsed[0]!r} - counting one more of it")
                        events.append((*parsed, "inferred"))
        return events


class ChatWatcher:
    """Runs a ChatReader on its own daemon thread, `checks_per_second` times a second, on a small
    capture of the chat box (about 400x170 pixels; a look costs about 6 ms, OCR only on new loot)."""

    def __init__(self, config, screen):
        self.config = config
        self.screen = screen
        self.reader = None
        self._lock = threading.Lock()
        self._events = queue.Queue(maxsize=200)
        self._stop = threading.Event()

    def expect_loot(self):
        with self._lock:
            if self.reader is not None:
                self.reader.expect(time.monotonic())

    def cancel_loot(self):
        with self._lock:
            if self.reader is not None:
                self.reader.cancel()

    def take_catches(self):
        caught = []
        try:
            while True:
                caught.append(self._events.get_nowait())
        except queue.Empty:
            return caught

    def stop(self):
        self._stop.set()

    def run(self):
        source, source_region, source_height = None, None, None
        period = 1.0 / self.config["checks_per_second"]
        was_lost = False
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                client = self.screen.client
                if client is None:
                    self._stop.wait(0.5)
                    continue
                region = chat_region(self.config, client)
                if region != source_region:
                    if source is not None:
                        source.close()
                    source = frame_source.FrameSource(region, backend=frame_source.MSS)
                    source_region = region
                    with self._lock:
                        #A new reader (a moved window starts the comparison over), keeping any
                        #reel-in it was waiting on - and the pitch it measured, if the window is
                        #the same height (the chat font is the same size).
                        waiting = self.reader.expect_until if self.reader is not None else None
                        pitch = chat_pitch(self.config, client[3])
                        if self.reader is not None and source_height == client[3]:
                            pitch = self.reader.pitch
                        self.reader = ChatReader(self.config, pitch,
                                                 log=log, detail=lambda m: log(m, console=False))
                        self.reader.expect_until = waiting
                        source_height = client[3]
                    log(f"chat: reading the box at {region['width']}x{region['height']} px", console=False)
                try:
                    frame = source.grab()
                except Exception as exc:             # noqa: BLE001 - "cannot tell", never a crash
                    log(f"chat capture failed ({exc})", console=False)
                    frame = None
                if frame is not None:
                    with self._lock:
                        events = self.reader.look(np.ascontiguousarray(frame[:, :, :3]), started)
                        lost = self.reader.lost
                    if lost and not was_lost:
                        log("chat: the box changed all at once (scrolled, covered, or just started) "
                            "- reading from what is there now", console=False)
                    was_lost = lost
                    for event in events:
                        try:
                            self._events.put_nowait(event)
                        except queue.Full:
                            pass
                self._stop.wait(max(0.0, period - (time.monotonic() - started)))
        finally:
            if source is not None:
                source.close()


def read_chat_once():
    """--read-chat: one look at the chat box, everything it can see, line by line."""
    config = load_chat_config()
    if config is None:
        return 2
    if not text_detection.ocr_available():
        print("No OCR (Tesseract) is installed, so the chat cannot be read - see requirements.txt.")
        return 2
    found = window_region.find_window(WINDOW_TITLE)
    if found is not None:
        client = found[1]
    elif window_region.available():
        print(f"No window called '{WINDOW_TITLE}' - is WoW running?")
        return 1
    else:
        client = _primary_monitor()
    region = chat_region(config, client)
    source = frame_source.FrameSource(region, backend=frame_source.MSS)
    try:
        frame = source.grab()
    finally:
        source.close()
    if frame is None:
        print("Could not capture the screen.")
        return 1
    frame = np.ascontiguousarray(frame[:, :, :3])
    LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    picture = LOG_FOLDER / "chat_check.png"
    cv.imwrite(str(picture), frame)
    pitch = chat_pitch(config, client[3])
    reader = ChatReader(config, pitch)
    lines = text_feed.split_lines(frame, pitch)
    measured = text_feed.estimate_pitch(frame)
    print(f"Chat box: {region['width']}x{region['height']} px at ({region['left']}, {region['top']}); "
          f"{len(lines)} line(s) of text, {pitch} px apart"
          + (f" (measured: {measured})" if measured and measured != pitch else "") + ".")
    loot = 0
    for line in lines:
        if reader.is_loot(line.image):
            parsed, text = reader.read(line.image)
            if parsed:
                loot += 1
                print(f"  LOOT  {text}\n        -> {parsed[0]} x{parsed[1]}")
            else:
                print(f"  loot? {text}   (green, but not read as a loot line)")
        else:
            print(f"        {ocr_line(line.image, config['ocr_thresholds'][0])}")
    print(f"{loot} loot line(s). The picture it read is saved as {picture}")
    if not lines:
        print("No text found there. Is the chat box where wow_gui.py's Setup tab says? (Chat "
              "lines fade after a while - loot something, or type in chat, then try again.)")
    return 0


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
        #A heartbeat every 10 s in the log file: how loud the loudest moment was, or that nothing
        #arrived at all. "The splash was never heard" has two very different causes - the sound
        #never reached us (wrong device, muted) or it did and did not match - and this tells them
        #apart without guessing.
        heartbeat_at = time.monotonic() + 10.0
        loudest = None
        last_audio = time.monotonic()
        while not self._stop.is_set():
            chunk = self.source.read(timeout=0.1)
            now = time.monotonic()
            if chunk is not None and len(chunk):
                last_audio = now
                peak = float(np.max(np.abs(chunk)))
                loudest = peak if loudest is None else max(loudest, peak)
                for hit in self.matcher.feed(chunk):
                    with self._lock:
                        self._splashes += 1
                    log(f"splash heard (rise {hit.rise_db:.0f} dB, {hit.over_recent_db:+.0f} dB "
                        f"over recent, shape {hit.shape:.2f})")
                #Drained here, on the thread that feeds the matcher, so nothing else touches it.
                while self.matcher.near_misses:
                    miss = self.matcher.near_misses.popleft()
                    log("loud sound, not taken for a splash: " +
                        "; ".join(self.matcher.failed_tests(miss)), console=False)
            if now >= heartbeat_at:
                heartbeat_at = now + 10.0
                if now - last_audio >= 10.0:
                    log(f"audio: nothing at all from {self.source.device_name} for "
                        f"{now - last_audio:.0f}s - is WoW's sound on, and playing to that device? "
                        f"(python wow.py --list-audio)")
                elif loudest is not None:
                    log(f"audio: loudest moment in the last 10s {20 * np.log10(max(loudest, 1e-9)):.0f}"
                        f" dBFS", console=False)
                loudest = None


# ------------------------------------------------------------------------------------------------------
# Offline replay
# ------------------------------------------------------------------------------------------------------

def replay(path, reference):
    """Prints every bite found in a recording: splashes heard, and - for a video - the lure seen
    bobbing. Accepts .wav, or any video ffmpeg reads. Nothing is pressed."""
    import shutil
    import subprocess
    import tempfile
    path = Path(path)
    is_video = path.suffix.lower() != ".wav"
    temp = None
    if is_video:
        if shutil.which("ffmpeg") is None:
            print("Not a .wav, and ffmpeg is not installed to convert it. Either install ffmpeg "
                  "(winget install ffmpeg) or convert it yourself:\n"
                  f'  ffmpeg -i "{path}" -vn -ac 1 -ar 48000 out.wav')
            return 2
        temp = Path(tempfile.mkdtemp()) / "audio.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", "48000", "-c:a", "pcm_s16le", str(temp)], check=True)
    samples, rate = wav_io.read_wav(temp or path)
    hits = sound_match.scan(samples, rate, reference)
    print(f"{len(samples) / rate:.1f}s of audio, {len(hits)} splash(es) heard:")
    for hit in hits:
        minutes, seconds = divmod(hit.time, 60)
        print(f"  {int(minutes)}:{seconds:05.2f}  rise {hit.rise_db:4.0f} dB   over recent "
              f"{hit.over_recent_db:+4.0f} dB   shape {hit.shape:.2f}")
    if temp is not None:
        temp.unlink(missing_ok=True)
    if is_video:
        eye = load_bobber_eye()
        if eye is not None:
            bites = replay_lure(path, eye)
            print(f"the lure seen bobbing {len(bites)} time(s):")
            for when in bites:
                minutes, seconds = divmod(when, 60)
                print(f"  {int(minutes)}:{seconds:05.2f}")
    return 0


def replay_lure(path, eye, fps=None):
    """The times the lure was seen bobbing in a video, looked at exactly as the live watcher does
    (searching the water, then only the small area around the lure) at LURE_CHECKS_PER_SECOND."""
    import json
    import subprocess
    fps = fps or LURE_CHECKS_PER_SECOND
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", str(path)],
                           capture_output=True, text=True)
    try:
        stream = json.loads(probe.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError):
        print("could not read the video's size - the lure was not checked")
        return []
    reader = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps}",
                               "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    bites, index, size = [], 0, width * height * 3
    try:
        while True:
            data = reader.stdout.read(size)
            if len(data) < size:
                break
            frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
            now = index / fps
            index += 1
            x, y, w, h = eye.capture_region(width, height)
            _seen, bite = eye.look(frame[y:y + h, x:x + w], (x, y), width, now)
            if bite:
                bites.append(now)
    finally:
        reader.stdout.close()
        reader.wait()
    return bites


# ------------------------------------------------------------------------------------------------------
# The live loop
# ------------------------------------------------------------------------------------------------------

def list_audio():
    try:
        names, default = audio_source.list_loopback_devices()
    except audio_source.AudioSourceError as exc:
        print(f"Cannot list sound devices: {exc}")
        return 2
    print("Sound devices wow.py can listen to (what each one is PLAYING, not a microphone):")
    for name in names:
        print(f"  {'*' if default and default in name else ' '} {name}")
    print("* = the Windows default, which is what wow.py listens to unless AUDIO_DEVICE is set.\n"
          "If WoW plays its sound on another one (Game menu > Settings > Audio > Output Device),\n"
          "set AUDIO_DEVICE at the top of wow.py to part of that device's name.")
    return 0


def check_audio(reference, seconds=15.0):
    """Listens for `seconds`, prints a level per second, saves what it heard, and runs the matcher.

    For the one question a recording cannot answer: does sound actually reach wow.py on THIS PC?
    Play WoW (or one of your fishing videos) while it runs.
    """
    try:
        source = audio_source.LoopbackSource(device=AUDIO_DEVICE)
    except audio_source.AudioSourceError as exc:
        print(f"Cannot listen: {exc}")
        return 2
    path = open_log_file("check_audio")
    log(f"listening to {source.device_name} ({source.sample_rate} Hz) for {seconds:.0f}s - "
        f"play some sound now (WoW, or one of your fishing videos)")
    chunks = []
    started = time.monotonic()
    second_end = started + 1.0
    peak = None
    try:
        while time.monotonic() - started < seconds:
            chunk = source.read(timeout=0.1)
            if chunk is not None and len(chunk):
                chunks.append(np.asarray(chunk, dtype=np.float32))
                level = float(np.max(np.abs(chunk)))
                peak = level if peak is None else max(peak, level)
            if time.monotonic() >= second_end:
                second_end += 1.0
                if peak is None:
                    log("  nothing arrived (silence, or nothing is playing on this device)")
                else:
                    db = 20 * np.log10(max(peak, 1e-9))
                    log(f"  loudest {db:6.1f} dBFS  {'#' * max(0, int((db + 60) / 2))}")
                peak = None
    finally:
        source.close()
    if not chunks:
        log("RESULT: no sound at all reached wow.py. Check that WoW's sound is on, and which "
            "device it plays to (python wow.py --list-audio).")
        close_log_file()
        return 1
    samples = np.concatenate(chunks)
    wav_path = LOG_FOLDER / f"check_audio_{time.strftime('%Y-%m-%d_%H-%M-%S')}.wav"
    wav_io.write_wav(wav_path, samples, source.sample_rate)
    matcher = sound_match.SoundMatcher(reference, source.sample_rate)
    step = max(1, int(0.02 * source.sample_rate))
    hits = []
    for i in range(0, len(samples), step):
        hits.extend(matcher.feed(samples[i:i + step]))
    log(f"RESULT: {len(samples) / source.sample_rate:.1f}s of sound reached wow.py; "
        f"{len(hits)} splash(es) heard in it, {len(matcher.near_misses)} other loud sound(s).")
    for hit in hits:
        log(f"  splash at {hit.time:.2f}s (shape {hit.shape:.2f})")
    for miss in matcher.near_misses:
        log(f"  other sound at {miss.time:.2f}s: " + "; ".join(matcher.failed_tests(miss)))
    log(f"saved what it heard to {wav_path}" + (f" and this report to {path}" if path else ""))
    close_log_file()
    return 0


class Bookkeeper:
    """What was caught this run: names resolved against the price list, counted, written to the
    history file, and (under --control) told to the GUI. Lives on the main thread only."""

    def __init__(self, catalog, session, control):
        self.catalog = catalog
        self.session = session
        self.control = control
        self.tally = fish_tally.Tally()

    def caught(self, raw, count, closed, how, counted):
        name = self.catalog.resolve(raw, closed)
        if name is None:
            log(f"loot line not understood ({raw!r}) - not counted")
            return
        if self.catalog.add(name):
            log(f"a new item, not seen before: {name} (no price yet - set one in wow_gui.py)")
        if not counted:
            log(f"looted {name} while paused - not counted as fishing")
            return
        self.tally.add(name, count, time.monotonic(), how)
        try:
            fish_tally.append_history(HISTORY, time.strftime("%Y-%m-%d %H:%M:%S"), self.session,
                                      name, count, how)
        except OSError as exc:
            log(f"could not write {HISTORY.name} ({exc}) - the catch still counts for this run")
        price = self.catalog.price(name)
        log(f"caught: {name}{f' x{count}' if count > 1 else ''} "
            f"({fish_tally.format_money(price, 'price not known')})"
            f"{' - inferred, see docs/wow_fishing.md' if how == 'inferred' else ''}")
        if self.control:
            emit("catch", {"fish": name, "count": count, "how": how,
                           "vendor": price, "time": time.strftime("%H:%M:%S")})

    def summary(self, fishing_seconds):
        copper, unpriced = self.tally.value(self.catalog)
        hourly = fish_tally.per_hour(copper, fishing_seconds)
        rate = fish_tally.per_hour(self.tally.total, fishing_seconds)
        parts = [f"{self.tally.total} caught in {fish_tally.format_duration(fishing_seconds)} of fishing"]
        if rate is not None:
            parts.append(f"{rate:.0f} an hour")
        parts.append(f"worth {fish_tally.format_money(copper)} to a vendor"
                     + (f" (+{unpriced} with no price yet)" if unpriced else ""))
        if hourly is not None:
            parts.append(f"{fish_tally.format_money(hourly)} an hour")
        return ", ".join(parts)


def emit(kind, payload):
    """One machine-readable line for wow_gui.py: "@<kind> <json>". Console only, never the log
    file - it is the GUI's feed, not something a person reads."""
    write_line(f"@{kind} {json.dumps(payload, separators=(',', ':'))}")


def parse_success_band(text):
    """'90-99' -> (90, 99) percent; 'off' -> (100, 100). ValueError if it is not a usable band."""
    text = str(text).strip().lower().replace("%", "")
    if text in ("off", "none", "100"):
        return 100, 100
    low, _, high = text.partition("-")
    low, high = int(low), int(high or low)
    if not 0 < low <= high <= 100:
        raise ValueError(f"a band is LOW-HIGH percent with 0 < LOW <= HIGH <= 100, got {text!r}")
    return low, high


def run(dry_run, control=False, auto_pause_minutes=None, success_band=None):
    try:
        import keyboard
    except ImportError:
        print("The `keyboard` package is missing - run: python -m pip install -r requirements.txt")
        return 2
    from core import actions, command_channel

    bar_check = presence.load(BAR_CONFIG)
    got_away_check = presence.load(GOT_AWAY_CONFIG)
    if bar_check is None or got_away_check is None:
        print(f"Missing or broken reference files in {ASSETS} - see the warnings above.")
        return 2
    reference = sound_match.load_reference_folder(SPLASH_FOLDER) if LISTEN_FOR_SPLASH else None
    eye = load_bobber_eye() if WATCH_LURE else None
    if LISTEN_FOR_SPLASH and reference is None:
        print(f"No usable splash clips in {SPLASH_FOLDER} - the splash will not be listened for.")
    if WATCH_LURE and eye is None:
        print("The lure will not be watched - see the warning above.")
    source = None
    if reference is not None:
        try:
            source = audio_source.LoopbackSource(device=AUDIO_DEVICE)
        except audio_source.AudioSourceError as exc:
            print(f"Cannot listen to the game's sound: {exc}")
    if source is None and eye is None:
        print("Neither the splash nor the lure can be used, so a bite could never be noticed.")
        return 2
    chat_config = load_chat_config() if READ_CHAT else None
    if READ_CHAT and chat_config is not None and not text_detection.ocr_available():
        print("No OCR (Tesseract) installed - catches will not be counted (see requirements.txt).")
        chat_config = None

    log_path = open_log_file("wow")
    if log_path:
        log(f"log file: {log_path}")
    if source is not None:
        log(f"listening to: {source.device_name} ({source.sample_rate} Hz)")
    log(f"watching the lure: {'yes' if eye is not None else 'no'}")
    log(f"counting catches from the chat: {'yes' if chat_config is not None else 'no'}")
    catalog = fish_tally.Catalog(PRICES, USER_PRICES)
    for problem in catalog.problems:
        log(f"WARNING: fish prices - {problem}")
    books = Bookkeeper(catalog, time.strftime("%Y-%m-%d_%H-%M-%S"), control)

    screen = ScreenWatcher(bar_check, got_away_check)
    ears = SplashListener(source, reference) if source is not None else None
    lure = LureWatcher(eye, screen) if eye is not None else None
    chat = ChatWatcher(chat_config, screen) if chat_config is not None else None
    low, high = success_band or SUCCESS_RATE_PERCENT
    keeper = fishing.SuccessRateKeeper(low / 100, high / 100)
    log(f"success rate kept in: {keeper.band_text()}"
        + ("" if keeper.off else " (a bite is let go now and then, once it reaches the top)"))
    loop = fishing.FishingLoop(recast_delay=RECAST_DELAY_SECONDS,
                               no_splash_timeout=NO_SPLASH_TIMEOUT_SECONDS, log=log, keeper=keeper)
    threading.Thread(target=screen.run, daemon=True, name="screen").start()
    if ears is not None:
        threading.Thread(target=ears.run, daemon=True, name="audio").start()
    if lure is not None:
        threading.Thread(target=lure.run, daemon=True, name="lure").start()
    if chat is not None:
        threading.Thread(target=chat.run, daemon=True, name="chat").start()

    quit_requested = threading.Event()
    paused = threading.Event()
    restart = threading.Event()
    auto_pause = fishing.AutoPause(auto_pause_minutes or AUTO_PAUSE_MINUTES, time.monotonic())
    pause_state = {"reason": None, "at": None}   #why paused ("manual" / "auto") and since when
    state_lock = threading.Lock()

    def toggle_pause(by=PAUSE_KEY.upper()):
        #One key, two states (Zel asked for F5 to do both). Each state still has its own sound,
        #so it is clear which one you are in without looking at the console. Either way it is a
        #person at the keyboard, so the auto-pause countdown starts over.
        with state_lock:
            auto_pause.restart(time.monotonic())
            if paused.is_set():
                paused.clear()
                pause_state["reason"] = None
                restart.set()
                play_cue(rising=True)
                log(f"resumed ({by}) - auto-pause in {auto_pause.minutes:.0f} min unless "
                    f"{PAUSE_KEY.upper()} is pressed")
            else:
                paused.set()
                pause_state.update(reason="manual", at=time.monotonic())
                play_cue(rising=False)
                log(f"PAUSED ({by}) - {PAUSE_KEY.upper()} again to resume")

    def set_auto_pause(minutes):
        with state_lock:
            auto_pause.set_minutes(minutes, time.monotonic())
        log(f"auto-pause set to {minutes} min (counting from now)")

    keyboard.add_hotkey(PAUSE_KEY, toggle_pause)
    keyboard.add_hotkey(QUIT_KEY, quit_requested.set)
    if control:
        handlers = {"toggle": lambda: toggle_pause("control panel"),
                    "pause": lambda: paused.is_set() or toggle_pause("control panel"),
                    "resume": lambda: paused.is_set() and toggle_pause("control panel"),
                    "quit": quit_requested.set}
        for minutes in AUTO_PAUSE_CHOICES:
            handlers[f"auto_pause_{minutes}"] = lambda m=minutes: set_auto_pause(m)
        #End of input = the control panel went away: nobody is watching, so stop.
        command_channel.listen(handlers, on_close=quit_requested.set, log=log)
    log(f"{'DRY RUN - no key will be pressed. ' if dry_run else ''}"
        f"{PAUSE_KEY.upper()} pause/resume, {QUIT_KEY.title()} quit. "
        f"Auto-pause after {auto_pause.minutes:.0f} min without {PAUSE_KEY.upper()}.")
    play_cue(rising=True)

    def press():
        hold = hold_time()
        if dry_run:
            log(f"(dry run) would press {FISH_KEY.upper()} for {hold:.2f}s")
            return
        actions.press_key(FISH_KEY, hold=hold)

    def take_bites():
        heard = ears.take_splashes() > 0 if ears is not None else False
        seen = lure.take_bites() > 0 if lure is not None else False
        return heard, seen

    started_at = time.monotonic()
    fishing_seconds = 0.0       #time actually fishing: not paused, WoW in front, not stopped
    last_tick = started_at
    next_status = 0.0
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
            with state_lock:
                if auto_pause.due(now, paused.is_set()):
                    paused.set()
                    pause_state.update(reason="auto", at=now)
                    play_auto_pause_cue()
                    log(f"AUTO-PAUSED: {auto_pause.minutes:.0f} min without {PAUSE_KEY.upper()} - "
                        f"press {PAUSE_KEY.upper()} to carry on")
            if restart.is_set():
                restart.clear()
                take_bites()                   #a bite from before the pause is stale
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
            if reason is None:
                fishing_seconds += now - last_tick
            last_tick = now
            heard, seen = take_bites()
            if (heard or seen) and reason is not None:
                log(f"{fishing.signal_name(heard, seen)} but not acted on: {reason}")
            reeled, missed = loop.catches, loop.misses
            if loop.decide(now, bar, splash=heard, got_away=got_away, allowed=reason is None,
                           bobbed=seen) == fishing.PRESS:
                press()
            if chat is not None:
                if loop.catches > reeled:
                    chat.expect_loot()         #a reeled-in bite: a loot line should follow
                elif loop.misses > missed:
                    chat.cancel_loot()
                for raw, count, closed, how in chat.take_catches():
                    since_pause = (now - pause_state["at"]) if paused.is_set() and pause_state["at"] else 0.0
                    books.caught(raw, count, closed, how,
                                 counted=not paused.is_set() or since_pause <= PAUSE_GRACE_SECONDS)
            if control and now >= next_status:
                next_status = now + STATUS_EVERY_SECONDS
                emit("status", {
                    "state": loop.state, "paused": paused.is_set(), "pause_reason": pause_state["reason"],
                    "blocked": reason if reason != "paused" else None,
                    "auto_pause_left": round(auto_pause.left(now, paused.is_set()), 1),
                    "auto_pause_minutes": auto_pause.minutes,
                    "fishing_seconds": round(fishing_seconds, 1),
                    "reeled": loop.catches, "missed": loop.misses, "by": loop.caught_by,
                    "let_go": loop.let_go, "success_band": [round(keeper.low * 100), round(keeper.high * 100)],
                    "caught": books.tally.total,
                    "chat": "reading" if chat is not None else "off", "dry_run": dry_run})
            time.sleep(0.01)                   #100 Hz: at most 10 ms between a bite and the press
    except KeyboardInterrupt:
        pass
    finally:
        screen.stop()
        if ears is not None:
            ears.stop()
        if lure is not None:
            lure.stop()
        if chat is not None:
            chat.stop()
        if source is not None:
            source.close()
        keyboard.unhook_all_hotkeys()
    minutes = (time.monotonic() - started_at) / 60
    if loop.state == fishing.STOPPED:
        log(f"stopped: {loop.reason}")
        if control:
            emit("stopped", {"reason": loop.reason})
    by = loop.caught_by
    log(f"done after {minutes:.1f} min: {loop.catches} reeled in (heard {by['sound']}, seen "
        f"{by['sight']}, both {by['both']}), {loop.misses} missed"
        f"{f' ({loop.let_go} let go on purpose)' if loop.let_go else ''}"
        f"{f', {source.dropped} audio chunks dropped' if source is not None and source.dropped else ''}")
    if chat is not None:
        log(books.summary(fishing_seconds))
    close_log_file()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="WoW fishing helper (see the top of wow.py).")
    parser.add_argument("--dry-run", action="store_true",
                        help="watch and listen, but print instead of pressing any key")
    parser.add_argument("--replay", metavar="FILE",
                        help="list every splash heard in a recording, then exit")
    parser.add_argument("--check-audio", action="store_true",
                        help="listen for 15 s and report what reached wow.py, then exit")
    parser.add_argument("--list-audio", action="store_true",
                        help="list the sound devices wow.py can listen to, then exit")
    parser.add_argument("--read-chat", action="store_true",
                        help="read the chat box once and print what it saw, then exit")
    parser.add_argument("--auto-pause", type=int, choices=AUTO_PAUSE_CHOICES, metavar="MINUTES",
                        help=f"pause after this many minutes without {PAUSE_KEY.upper()} "
                             f"(default {AUTO_PAUSE_MINUTES}; one of {AUTO_PAUSE_CHOICES})")
    parser.add_argument("--success-rate", type=parse_success_band, metavar="LOW-HIGH",
                        help=f"keep the success rate in this band, in percent, by letting a bite "
                             f"go now and then (default {SUCCESS_RATE_PERCENT[0]}-"
                             f"{SUCCESS_RATE_PERCENT[1]}; 'off' reels in every bite)")
    parser.add_argument("--control", action="store_true",
                        help="run under wow_gui.py: commands on stdin, state as @lines on stdout")
    args = parser.parse_args(argv)
    if args.list_audio:
        return list_audio()
    if args.read_chat:
        return read_chat_once()
    if args.replay or args.check_audio:
        reference = sound_match.load_reference_folder(SPLASH_FOLDER)
        if reference is None:
            print(f"No usable reference clips in {SPLASH_FOLDER}.")
            return 2
        return replay(args.replay, reference) if args.replay else check_audio(reference)
    return run(args.dry_run, control=args.control, auto_pause_minutes=args.auto_pause,
               success_band=args.success_rate)


if __name__ == "__main__":
    sys.exit(main())
