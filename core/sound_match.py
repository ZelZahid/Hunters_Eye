"""Hearing a reference sound in a live audio stream - "did THAT sound just play?"

The audio twin of template matching. It is handed a few recordings of one sound (the reference
clips) and a stream of samples, and reports each time the stream plays something that matches.
It knows nothing about any game: World of Warcraft's bobber splash is the first sound it was
pointed at (see wow.py), but the same code would hear glass breaking or a door slam for a
security robot, given different clips.

HOW IT DECIDES - three independent tests, ALL of which must pass on the same moment:

  1. SUDDEN: the sound is at least `min_rise_db` louder, on average across the frequency bands,
     than the last ~2 seconds were. This is the "onset" - something just started.
  2. LOUD FOR THIS PLACE: it is at least `min_over_recent_db` louder than the loud end (90th
     percentile) of the last ~15 seconds. Test 1 alone was measured FAILING on the reference
     video: the ambient water at 16.0s faded in over near-silence and rose 29 dB - more than
     the 15 dB rise threshold - because the 2 seconds before it were almost silent. Against the
     last 15 seconds that same fade-in scored +1.8 dB while the three splashes scored +16 to +23.
  3. SHAPED LIKE THE REFERENCE: its spectrum (how the energy spreads over ~19 frequency bands,
     with overall loudness removed) correlates with the reference's at least `min_shape`. The
     splash is broadband noise; a tonal sound such as a UI chime or a voice scored -0.4 to -0.5
     on the reference video against the splashes' 0.85-0.98. This is what makes it "only that
     sound" rather than "any loud sound".

Why not one cross-correlation of the raw waveform: a splash is noise, and no two splashes share a
waveform. Their SPECTRUM is what repeats, and a band-energy comparison is cheap enough to run
~90 times a second on one core without being noticed (design priority #2).

WHAT THE RECORDING TAUGHT (worth knowing before tuning anything):
  - The ambient sound while fishing is sparse clicks over near-digital-silence, with gaps that
    read as -120 dB. Silent frames are therefore EXCLUDED from the background rather than
    averaged in: a background full of silence makes every ordinary sound look like a huge rise.
  - All thresholds are RELATIVE (dB above this stream's own recent past), so the in-game volume
    setting does not matter. Only the reference clips' SHAPE is compared, never their loudness.

None is a third answer here too: `ready` is False until enough history exists to judge a rise,
and nothing is reported before then. "Not warmed up" is "cannot tell", not "no splash".

The matcher does no I/O and owns no threads. A caller feeds it mono float samples (see
core/audio_source.py) and acts on the hits it returns.
"""
from __future__ import annotations

import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.wav_io import read_wav

#Analysis bands, in Hz. Log-spaced because hearing and most natural sounds are; stopping at 7.5 kHz
#keeps every band inside the Nyquist limit of a 16 kHz recording, so a reference cut at one sample
#rate matches a stream at another (the tests scan a 16 kHz track with 48 kHz reference clips).
BAND_EDGES_HZ = np.geomspace(180.0, 7500.0, 20)
FRAME_SECONDS = 0.021       #analysis window; rounded up to a power of two in samples
SILENCE_DB = -120.0         #a frame quieter than this is a gap in the stream, not "quiet"
POWER_FLOOR = 1e-14


@dataclass
class SoundHit:
    """One detection. `time` is seconds of audio fed since the matcher was created."""
    time: float
    rise_db: float          #test 1: dB above the last ~2 s
    over_recent_db: float   #test 2: dB above the loud end of the last ~15 s
    shape: float            #test 3: spectral-shape correlation with the reference, -1..1


#A near miss has the same fields: for a loud sound that was NOT a match, each is the best that sound
#reached, so a log line can say which test it fell short on, and by how much.
NearMiss = SoundHit


def _frame_size(sample_rate):
    target = FRAME_SECONDS * sample_rate
    return int(2 ** np.ceil(np.log2(max(target, 64))))


class _BandAnalyser:
    """Turns a sample stream into one row of band levels (dB) per hop. Rate-agnostic."""

    def __init__(self, sample_rate):
        self.sample_rate = float(sample_rate)
        self.n_fft = _frame_size(sample_rate)
        self.hop = self.n_fft // 2
        self.window = np.hanning(self.n_fft).astype(np.float32)
        #Normalise so a full-scale sine reads near 0 dB whatever n_fft is - the silence threshold
        #then means the same thing at every sample rate.
        self._scale = 1.0 / float(self.window.sum()) ** 2
        freqs = np.fft.rfftfreq(self.n_fft, 1.0 / self.sample_rate)
        self.bands = []
        for lo, hi in zip(BAND_EDGES_HZ[:-1], BAND_EDGES_HZ[1:]):
            idx = np.nonzero((freqs >= lo) & (freqs < hi))[0]
            if idx.size == 0:   #very low rates leave a narrow band empty: widen it to its nearest bin
                idx = np.array([int(np.argmin(np.abs(freqs - (lo + hi) / 2)))])
            self.bands.append((int(idx[0]), int(idx[-1]) + 1))
        self._pending = np.zeros(0, dtype=np.float32)

    @property
    def frames_per_second(self):
        return self.sample_rate / self.hop

    def feed(self, samples):
        """Band levels for every complete hop in `samples` (plus any leftover from last time)."""
        data = np.concatenate([self._pending, np.asarray(samples, dtype=np.float32).ravel()])
        count = (len(data) - self.n_fft) // self.hop + 1 if len(data) >= self.n_fft else 0
        if count <= 0:
            self._pending = data
            return np.zeros((0, len(self.bands)), dtype=np.float32), np.zeros(0, dtype=np.float32)
        frames = np.lib.stride_tricks.sliding_window_view(data, self.n_fft)[::self.hop][:count]
        power = np.abs(np.fft.rfft(frames * self.window, axis=1)) ** 2 * self._scale
        bands = np.stack([power[:, a:b].sum(axis=1) for a, b in self.bands], axis=1)
        levels = 10.0 * np.log10(np.maximum(bands, POWER_FLOOR))
        total = 10.0 * np.log10(np.maximum(power.sum(axis=1), POWER_FLOOR))
        self._pending = data[count * self.hop:]
        return levels.astype(np.float32), total.astype(np.float32)


def _shape(levels):
    """Overall loudness removed, unit length: what is left is how the energy is SPREAD."""
    centred = np.asarray(levels, dtype=np.float64) - float(np.mean(levels))
    norm = float(np.linalg.norm(centred))
    return centred / norm if norm > 1e-9 else None


def fingerprint_from_clip(samples, sample_rate, onset_drop_db=12.0, seconds=0.3):
    """The reference shape of one clip: the average spectrum of the sound's first `seconds`.

    The sound starts at the first frame within `onset_drop_db` of the clip's peak, so no onset has
    to be marked by hand: a clip cut roughly around the sound (half a second either side) gives the
    same fingerprint as a trimmed one. Returns None for a clip with nothing audible in it.

    WHY THE START, NOT THE LOUDEST PART: the matcher decides within ~0.15 s of a sound starting,
    so the fingerprint has to describe that moment. The first version averaged the loudest frames
    instead, and for one splash variant those come later and sound different: its fingerprint
    then matched a spell's crackle (0.73) better than its own onset.
    """
    analyser = _BandAnalyser(sample_rate)
    levels, total = analyser.feed(samples)
    if len(total) == 0 or float(total.max()) <= SILENCE_DB:
        return None
    start = int(np.argmax(total >= float(total.max()) - onset_drop_db))
    count = max(1, int(round(seconds * analyser.frames_per_second)))
    return _shape(levels[start:start + count].mean(axis=0))


def load_reference(paths):
    """The fingerprints of several clips as a LIBRARY (one row per clip), or None.

    A library, not an average: splashes come in several distinct variants, and averaging them
    blurs each one into a shape none of them has. The matcher scores a sound against every row and
    keeps the best, so adding a clip of a new variant teaches it that variant without making it
    worse at the others.

    Never raises for a bad clip: it is skipped with a warning, because one damaged file should not
    stop the others from working. No usable clip at all returns None, and the caller must treat
    that as "this detector is unavailable", not as "nothing matched".
    """
    shapes = []
    for path in paths:
        try:
            samples, rate = read_wav(path)
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            print(f"WARNING: reference clip {path} could not be read ({exc}) - skipped.")
            continue
        shape = fingerprint_from_clip(samples, rate)
        if shape is None:
            print(f"WARNING: reference clip {path} is silent - skipped.")
            continue
        shapes.append(shape)
    if not shapes:
        return None
    return np.stack(shapes)


def load_reference_folder(folder):
    """load_reference() over every .wav in a folder, in name order."""
    folder = Path(folder)
    return load_reference(sorted(folder.glob("*.wav"))) if folder.is_dir() else None


class SoundMatcher:
    """Streaming matcher: feed() samples in, get SoundHits out. See the module docstring.

    Defaults were set from the reference video (three splashes, 57 s of fishing audio) with a
    margin on both sides - see docs/wow_fishing.md for the measured numbers behind each one.
    """

    def __init__(self, reference_shape, sample_rate, min_rise_db=15.0, min_over_recent_db=8.0,
                 min_shape=0.78, evidence_seconds=0.15, background_seconds=2.0, gap_seconds=0.3,
                 recent_seconds=15.0, refractory_seconds=3.0):
        if reference_shape is None:
            raise ValueError("SoundMatcher needs a reference shape (load_reference returned None)")
        #One row per reference clip; a single 1-D shape is a library of one.
        self.reference = np.atleast_2d(np.asarray(reference_shape, dtype=np.float64))
        self.min_rise_db = float(min_rise_db)
        self.min_over_recent_db = float(min_over_recent_db)
        self.min_shape = float(min_shape)
        self.refractory_seconds = float(refractory_seconds)

        self._analyser = _BandAnalyser(sample_rate)
        fps = self._analyser.frames_per_second
        self._evidence_n = max(1, int(round(evidence_seconds * fps)))
        self._gap_n = max(0, int(round(gap_seconds * fps)))
        self._background_n = max(4, int(round(background_seconds * fps)))
        self._recent_n = max(self._background_n, int(round(recent_seconds * fps)))
        #The background and recent-loudness windows sit BEHIND a short gap, so the start of the
        #sound being judged is never part of its own background.
        self._evidence = deque(maxlen=self._evidence_n)
        self._delay = deque(maxlen=self._gap_n + 1)
        #The last `recent_seconds` of audible, aged frames, as a ring buffer rather than a deque:
        #turning a 1400-row deque into an array on every frame was measured at half the matcher's
        #whole cost, and this is the same data with no conversion.
        self._ring = np.zeros((self._recent_n, len(BAND_EDGES_HZ) - 1), dtype=np.float32)
        #Which ring rows belong to a sound that MATCHED. They count toward the 2 s background (so a
        #long sound stops being "sudden" once it has gone on for a while) but not toward the recent
        #loud level (so one splash does not make the next one look ordinary). See _judge().
        self._ring_matched = np.zeros(self._recent_n, dtype=bool)
        self._ring_count = 0
        self._ring_pos = 0
        self._background_offsets = np.arange(1, self._background_n + 1)
        #The 15 s percentile is the one non-trivial cost per frame, and it moves slowly, so it is
        #recomputed ten times a second rather than ninety.
        self._recent_every = max(1, int(round(fps / 10)))
        self._recent_level = None
        self._frames_since_recent = 0
        self._frame_index = 0
        self._last_hit = None
        self._quiet_until = None  #frames before this time stay out of the history - see _judge()
        self.last_scores = None   #(rise, over_recent, shape) of the latest frame, for debugging
        #Every sudden loud sound that was NOT a match, one entry per sound - so when a real splash is
        #missed live, the log says which test it failed instead of leaving nothing to go on.
        self.near_misses = deque(maxlen=100)
        self._event = None        #[start, best rise, best over_recent, best shape, last loud time]

    @property
    def sample_rate(self):
        return self._analyser.sample_rate

    @property
    def ready(self):
        """Whether enough audible history exists to judge a rise at all."""
        return self._ring_count >= self._background_n // 2 and self._recent_level is not None

    def reset(self):
        """Forget the past - for after a pause, when the old background no longer describes now."""
        self._evidence.clear()
        self._delay.clear()
        self._ring_count = 0
        self._ring_pos = 0
        self._ring_matched[:] = False
        self._recent_level = None
        self._last_hit = None
        self._quiet_until = None
        self.last_scores = None
        self._event = None

    def feed(self, samples):
        """Processes samples (mono float, -1..1) and returns the list of hits they contained."""
        levels, totals = self._analyser.feed(samples)
        hits = []
        fps = self._analyser.frames_per_second
        for row, total in zip(levels, totals):
            self._frame_index += 1
            now = (self._frame_index * self._analyser.hop + self._analyser.n_fft / 2) / self.sample_rate
            silent = total <= SILENCE_DB
            self._evidence.append(None if silent else row)

            #A frame joins the history only after it has aged past the gap, and only if audible.
            matched = self._quiet_until is not None and now < self._quiet_until
            self._delay.append(None if silent else (row, matched))
            if len(self._delay) == self._delay.maxlen:
                aged = self._delay[0]
                if aged is not None:
                    self._ring[self._ring_pos] = aged[0]
                    self._ring_matched[self._ring_pos] = aged[1]
                    self._ring_pos = (self._ring_pos + 1) % self._recent_n
                    self._ring_count = min(self._ring_count + 1, self._recent_n)
            self._frames_since_recent += 1
            if self._frames_since_recent >= self._recent_every and self._ring_count >= fps:
                self._frames_since_recent = 0
                ordinary = ~self._ring_matched[:self._ring_count]
                if ordinary.sum() >= fps:
                    self._recent_level = np.percentile(self._ring[:self._ring_count][ordinary], 90, axis=0)

            hit = self._judge(now)
            if hit is not None:
                hits.append(hit)
        return hits

    def _judge(self, now):
        if not self.ready:
            return None
        audible = [row for row in self._evidence if row is not None]
        if len(audible) < 0.7 * self._evidence_n:
            return None   #mostly gap: cannot tell, which is not a match
        evidence = np.mean(audible, axis=0)
        count = min(self._background_n, self._ring_count)
        newest = (self._ring_pos - self._background_offsets[:count]) % self._recent_n
        background = np.median(self._ring[newest], axis=0)
        rise = float(np.mean(evidence - background))
        over_recent = float(np.mean(evidence - self._recent_level))
        shape_vec = _shape(evidence)
        shape = float(np.max(self.reference @ shape_vec)) if shape_vec is not None else -1.0
        self.last_scores = (rise, over_recent, shape)
        if self._last_hit is not None and now - self._last_hit < self.refractory_seconds:
            return None
        if rise < self.min_rise_db or over_recent < self.min_over_recent_db or shape < self.min_shape:
            self._track_near_miss(now, rise, over_recent, shape)
            return None
        self._last_hit = now
        self._event = None        #it became a match after all: not a near miss
        #A sound that MATCHED must not raise the recent loud level the next one is judged against.
        #Without this, a second splash a few seconds after the first is compared with the first
        #one's loud tail, reads as "not louder than recently", and is missed - and with a 1-6 s
        #recast a bite 5-10 s after the last one is ordinary. It DOES still count toward the 2 s
        #background: when it once didn't, a long spell sound that matched once stayed "sudden"
        #forever and matched again every 3 s (measured, 2026-11-24 recording, 77-84 s). The frames
        #still in the gap buffer are the start of this very sound, so they are marked too.
        self._quiet_until = now + self.refractory_seconds
        for i, entry in enumerate(self._delay):
            if entry is not None:
                self._delay[i] = (entry[0], True)
        return SoundHit(now, rise, over_recent, shape)

    def _track_near_miss(self, now, rise, over_recent, shape):
        """Accumulates one loud non-matching sound; files it once it has been quiet for 0.3 s."""
        event = self._event
        if rise >= self.min_rise_db:
            if event is None:
                self._event = [now, rise, over_recent, shape, now]
            else:
                event[1] = max(event[1], rise)
                event[2] = max(event[2], over_recent)
                event[3] = max(event[3], shape)
                event[4] = now
        elif event is not None and now - event[4] > 0.3:
            self.near_misses.append(NearMiss(event[0], event[1], event[2], event[3]))
            self._event = None

    def failed_tests(self, miss):
        """Plain words for which tests a near miss fell short on."""
        failed = []
        if miss.over_recent_db < self.min_over_recent_db:
            failed.append(f"only {miss.over_recent_db:+.1f} dB over the recent loud level "
                          f"(needs {self.min_over_recent_db:+.1f})")
        if miss.shape < self.min_shape:
            failed.append(f"shape {miss.shape:.2f} (needs {self.min_shape:.2f})")
        return failed or ["each test passed, but never all at the same moment"]


def scan(samples, sample_rate, reference_shape, chunk_seconds=0.02, **settings):
    """Every hit in a whole recording, fed in small chunks exactly as a live stream would be."""
    matcher = SoundMatcher(reference_shape, sample_rate, **settings)
    step = max(1, int(chunk_seconds * sample_rate))
    hits = []
    for start in range(0, len(samples), step):
        hits.extend(matcher.feed(samples[start:start + step]))
    return hits
