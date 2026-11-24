"""The fishing loop's decisions: when to press the fishing key, and why. No I/O, no clock.

wow.py owns the eyes, the ears and the hands (screen capture, audio, the keyboard). This file owns
only the DECISION, and takes everything it needs as arguments - the current time, what the screen
shows, whether a splash was just heard - so the tests can drive it through a whole fishing session
in microseconds, with no game, no audio device and no sleeping. Same pattern as
routes/pindle.py's Walker.decide().

THE LOOP (one key does everything - in WoW the interact key reels in a biting bobber and, with
nothing to interact with, casts again):

    CASTING  --press-->  WAITING_FOR_BAR  --bar seen-->  FISHING
       ^                      |  (no bar after CAST_CONFIRM_SECONDS: cast again,          |
       |                      |   and give up after MAX_FAILED_CASTS in a row)            |
       |                                                                                  |
       +-- random 1-6 s wait <-- splash heard: press at once (reel in) ------------------+
       +-- random 1-6 s wait <-- "Your fish got away!" appeared, or the bar went away ---+
       +-- random 1-6 s wait <-- nothing heard for NO_SPLASH_TIMEOUT: press (restart) ---+

THREE-VALUED INPUTS. `bar` and `got_away` are True / False / None, and None ("the screen could not
be read") is never treated as False. A FISHING state that cannot see the screen waits; it does not
decide the bar is gone and start pressing keys at a screen it cannot see.

"Your fish got away!" only counts when it APPEARS while fishing (not seen on the previous check,
seen on this one). The message stays up for a few seconds, and with a 1 s recast it is often
still on screen when the next cast's bar comes up - reacting to it then would press the key in
the middle of a perfectly good cast and pull the new bobber out.
"""
from __future__ import annotations

import random

CASTING = "casting"                    #waiting out the random delay, then press to cast
WAITING_FOR_BAR = "waiting for bar"    #pressed to cast; the fishing bar should appear
FISHING = "fishing"                    #bar is up: listening for the splash
STOPPED = "stopped"                    #gave up - see .reason

PRESS = "press"


class FishingLoop:
    """One decide() call per tick. It returns PRESS when the key should go down, else None.

    `rng` is a random.Random so tests can seed it; `log` receives one line per decision worth
    printing (what happened and what comes next), so a person watching the console can follow.
    """

    def __init__(self, recast_delay=(1.0, 6.0), no_splash_timeout=15.0, cast_confirm_seconds=4.0,
                 bar_lost_seconds=0.6, max_failed_casts=3, rng=None, log=print):
        low, high = recast_delay
        if not 0 <= low <= high:
            raise ValueError(f"recast_delay must be (low, high) with 0 <= low <= high, got {recast_delay}")
        self.recast_delay = (float(low), float(high))
        self.no_splash_timeout = float(no_splash_timeout)
        self.cast_confirm_seconds = float(cast_confirm_seconds)
        self.bar_lost_seconds = float(bar_lost_seconds)
        self.max_failed_casts = int(max_failed_casts)
        self.rng = rng or random.Random()
        self.log = log

        self.state = CASTING
        self.reason = ""
        self.cast_at = None        #CASTING: when to press
        self.deadline = None       #WAITING_FOR_BAR: when to give up on this cast
        self.fishing_since = None  #FISHING: when the bar appeared
        self.bar_lost_at = None    #FISHING: when the bar was first seen missing
        self.failed_casts = 0
        self.catches = 0           #splashes reeled in (the game decides if a fish was caught)
        self.misses = 0            #rounds that ended with no splash heard, or the fish got away
        self._saw_got_away = None

    # ---- the one entry point -------------------------------------------------------------------

    def decide(self, now, bar, splash=False, got_away=None, allowed=True):
        """What to do at time `now` (seconds, any monotonic clock).

        bar: is the fishing bar on screen - True / False / None (cannot tell).
        splash: was the splash sound heard since the last call.
        got_away: is "Your fish got away!" on screen - True / False / None.
        allowed: may a key be pressed right now (not paused, game in front). When it is False
            the decision is HELD, not skipped - a splash heard while not allowed is dropped,
            because by the time pressing is allowed again it is stale.
        """
        appeared = got_away is True and self._saw_got_away is False
        if got_away is not None:
            self._saw_got_away = got_away

        if self.state == STOPPED or not allowed:
            return None
        if self.state == CASTING:
            return self._casting(now, bar)
        if self.state == WAITING_FOR_BAR:
            return self._waiting_for_bar(now, bar)
        if self.state == FISHING:
            return self._fishing(now, bar, splash, appeared)
        return None

    def start(self, now, bar):
        """Begin (or begin again after a pause) from whatever is on screen right now."""
        self.failed_casts = 0
        self._saw_got_away = None
        if bar is True:
            self._enter_fishing(now, "the fishing bar is already up - listening for the splash")
        else:
            #A short fixed wait rather than the random one: the person just pressed start (or
            #resume), so they are watching, and making them wait up to 6 s reads as "broken".
            self.state = CASTING
            self.cast_at = now + 1.0
            self.log("casting in 1.0s")

    # ---- states --------------------------------------------------------------------------------

    def _casting(self, now, bar):
        #The bar is deliberately NOT looked at here. Right after a reel-in it is still on screen
        #for most of a second while it fades out, and treating that as "a cast is in progress"
        #would go straight back to listening to a bobber that is no longer there.
        if self.cast_at is not None and now >= self.cast_at:
            self.state = WAITING_FOR_BAR
            self.deadline = now + self.cast_confirm_seconds
            self.log("cast")
            return PRESS
        return None

    def _waiting_for_bar(self, now, bar):
        if bar is True:
            self.failed_casts = 0
            self._enter_fishing(now, "the fishing bar is up - listening for the splash")
            return None
        if now < self.deadline:
            return None
        self.failed_casts += 1
        if self.failed_casts >= self.max_failed_casts:
            self.state = STOPPED
            self.reason = (f"pressed the key {self.failed_casts} times and the fishing bar never "
                           f"appeared - is the character facing water, with fishing on that key?")
            self.log(f"STOPPED: {self.reason}")
            return None
        self._schedule_cast(now, f"no fishing bar after the cast ({self.failed_casts} in a row)")
        return None

    def _fishing(self, now, bar, splash, got_away_appeared):
        if splash and bar is not False:
            #bar None (cannot read the screen this instant) still reels in: the splash itself is
            #the evidence a fish is on, and we were fishing a moment ago. Waiting to see the bar
            #again would spend the very fraction of a second the reel-in has.
            self.catches += 1
            self._schedule_cast(now, f"splash heard - reeling in (#{self.catches})")
            return PRESS
        if got_away_appeared:
            self.misses += 1
            self._schedule_cast(now, "the fish got away")
            return None
        if bar is False:
            if self.bar_lost_at is None:
                self.bar_lost_at = now
            elif now - self.bar_lost_at >= self.bar_lost_seconds:
                self.misses += 1
                self._schedule_cast(now, "the fishing bar went away with no splash heard")
                return None
        elif bar is True:
            self.bar_lost_at = None
        if now - self.fishing_since >= self.no_splash_timeout:
            #The bar is still up but nothing was heard: probably a missed splash. Pressing ends this
            #cast (reels in an empty line) so the next one can start.
            self.misses += 1
            self._schedule_cast(now, f"no splash in {self.no_splash_timeout:.0f}s - restarting")
            return PRESS
        return None

    # ---- transitions ---------------------------------------------------------------------------

    def _enter_fishing(self, now, why):
        self.state = FISHING
        self.fishing_since = now
        self.bar_lost_at = None
        self.log(why)

    def _schedule_cast(self, now, why):
        delay = self.rng.uniform(*self.recast_delay)
        self.state = CASTING
        self.cast_at = now + delay
        self.bar_lost_at = None
        self.log(f"{why}; casting again in {delay:.1f}s")
