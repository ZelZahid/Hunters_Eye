"""Reading and writing 16-bit PCM WAV files as mono float arrays - standard library + numpy only.

A neutral helper, not a detector: core/sound_match.py (reference clips) and core/audio_source.py
(replaying a recording) both need it, and CLAUDE.md's rule is that two modules sharing a need put
it in a third module both import, never one reaching into the other.
"""
import wave

import numpy as np


def read_wav(path):
    """(mono float32 samples in -1..1, sample rate) from a 16-bit PCM WAV. Standard library only."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported (got {8 * width}-bit)")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def write_wav(path, samples, sample_rate):
    """Writes mono float samples (-1..1) as a 16-bit PCM WAV."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
