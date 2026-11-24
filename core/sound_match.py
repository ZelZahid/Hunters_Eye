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


def fingerprint_from_clip(samples, sample_rate, loud_window_db=6.0):
    """The reference shape of one clip: the average spectrum of its loudest moments.

    "Loudest moments" = every frame within `loud_window_db` of the clip's peak. This needs no
    hand-marked onset, so a clip cut roughly around the sound (half a second either side is
    plenty) gives the same fingerprint as a precisely trimmed one. Returns None for a clip with
    nothing audible in it.
    """
    analyser = _BandAnalyser(sample_rate)
    levels, total = analyser.feed(samples)
    if len(total) == 0 or float(total.max()) <= SILENCE_DB:
        return None
    loud = total >= float(total.max()) - loud_window_db
    return _shape(levels[loud].mean(axis=0))


def load_reference(paths):
    """Averages the fingerprints of several clips into one reference shape, or None.

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
    return _shape(np.mean(shapes, axis=0))


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
                 min_shape=0.7, evidence_seconds=0.15, background_seconds=2.0, gap_seconds=0.3,
                 recent_seconds=15.0, refractory_seconds=3.0):
        if reference_shape is None:
            raise ValueError("SoundMatcher needs a reference shape (load_reference returned None)")
        self.reference = np.asarray(reference_shape, dtype=np.float64)
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
        self._recent_level = None
        self._last_hit = None
        self._quiet_until = None
        self.last_scores = None

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
            self._delay.append(None if silent or matched else row)
            if len(self._delay) == self._delay.maxlen:
                aged = self._delay[0]
                if aged is not None:
                    self._ring[self._ring_pos] = aged
                    self._ring_pos = (self._ring_pos + 1) % self._recent_n
                    self._ring_count = min(self._ring_count + 1, self._recent_n)
            self._frames_since_recent += 1
            if self._frames_since_recent >= self._recent_every and self._ring_count >= fps:
                self._frames_since_recent = 0
                self._recent_level = np.percentile(self._ring[:self._ring_count], 90, axis=0)

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
        shape = float(shape_vec @ self.reference) if shape_vec is not None else -1.0
        self.last_scores = (rise, over_recent, shape)
        if rise < self.min_rise_db or over_recent < self.min_over_recent_db or shape < self.min_shape:
            return None
        if self._last_hit is not None and now - self._last_hit < self.refractory_seconds:
            return None
        self._last_hit = now
        #A sound that MATCHED must not become part of the background it is judged against. Without
        #this, a second splash a few seconds after the first is compared with the first one's
        #loud tail, reads as "not louder than recently", and is missed - and with a 1-6 s recast
        #a bite 5-10 s after the last one is the normal case, not an edge case. The frames still
        #in the gap buffer are the start of this very sound, so they go too.
        self._quiet_until = now + self.refractory_seconds
        for i in range(len(self._delay)):
            self._delay[i] = None
        return SoundHit(now, rise, over_recent, shape)


def scan(samples, sample_rate, reference_shape, chunk_seconds=0.02, **settings):
    """Every hit in a whole recording, fed in small chunks exactly as a live stream would be."""
    matcher = SoundMatcher(reference_shape, sample_rate, **settings)
    step = max(1, int(chunk_seconds * sample_rate))
    hits = []
    for start in range(0, len(samples), step):
        hits.extend(matcher.feed(samples[start:start + step]))
    return hits
