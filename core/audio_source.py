"""Where sound comes from - the audio twin of frame_source.py.

A source hands back mono float32 chunks (-1..1) and knows nothing about what listens to them.

  LoopbackSource  - "what the speakers are playing", via WASAPI loopback (Windows only, through the
                    optional `pyaudiowpatch` package). This is how a program hears a game's sound
                    without a microphone and without touching the game: it is the sound card's
                    output, exactly as the person hears it.
  WavFileSource   - a recording, played back in real time (or as fast as possible), so everything
                    downstream can be exercised offline - the tests and wow.py --replay use it.

DEGRADES, NEVER CRASHES: on macOS/Linux, or without pyaudiowpatch, LoopbackSource raises
AudioSourceError at construction with the reason, and the caller decides what to do (wow.py says
why and exits before pressing any key). macOS has no system loopback at all; a virtual device such
as BlackHole would be the way in, and is a todo, not a silent gap.

WASAPI LOOPBACK DELIVERS NOTHING WHILE NOTHING PLAYS. That is the driver, not a bug: during a silent
stretch read() returns None (timeout), the same way frame_source's grab() returns None on a static
screen. "No chunk" is "cannot tell", not "silence was heard".

THREADING: pyaudiowpatch calls our callback on its own audio thread. The callback does the minimum
(downmix to mono, put_nowait into a bounded queue, drop the oldest when full) and returns at once -
an audio callback that blocks makes the driver drop audio, and a growing queue is a memory leak
with a delay attached. Same rule as main.py's capture queues.
"""
from __future__ import annotations

import queue
import sys
import time

import numpy as np

from core.wav_io import read_wav


class AudioSourceError(RuntimeError):
    """The source could not be opened, with a human-readable reason."""


def _to_mono(data, channels):
    if channels > 1:
        return data.reshape(-1, channels).mean(axis=1)
    return data


class LoopbackSource:
    """The default speakers' output, as mono float32 chunks. Windows only."""

    def __init__(self, chunk_seconds=0.01, max_queued_chunks=200):
        if sys.platform != "win32":
            raise AudioSourceError("listening to the speakers (WASAPI loopback) is Windows-only; "
                                   "on macOS it needs a loopback device such as BlackHole (not "
                                   "supported yet)")
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:
            raise AudioSourceError("pyaudiowpatch is not installed - run: "
                                   "python -m pip install -r requirements.txt") from exc
        self._pyaudio = pyaudio
        self._queue = queue.Queue(maxsize=max_queued_chunks)
        self.dropped = 0
        self._audio = pyaudio.PyAudio()
        try:
            device = self._find_loopback(pyaudio)
            self.device_name = device["name"]
            self.sample_rate = int(device["defaultSampleRate"])
            self.channels = max(1, int(device["maxInputChannels"]))
            self._stream = self._audio.open(
                format=pyaudio.paFloat32, channels=self.channels, rate=self.sample_rate,
                input=True, input_device_index=device["index"],
                frames_per_buffer=max(64, int(self.sample_rate * chunk_seconds)),
                stream_callback=self._callback)
        except Exception as exc:
            self._audio.terminate()
            if isinstance(exc, AudioSourceError):
                raise
            raise AudioSourceError(f"could not open the speakers' loopback: {exc}") from exc

    def _find_loopback(self, pyaudio):
        try:
            wasapi = self._audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as exc:
            raise AudioSourceError("WASAPI is not available on this system") from exc
        speakers = self._audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            return speakers
        #The loopback twin of an output device carries the output's name plus " [Loopback]".
        for loopback in self._audio.get_loopback_device_info_generator():
            if speakers["name"] in loopback["name"]:
                return loopback
        raise AudioSourceError(f"no loopback device found for the default speakers "
                               f"({speakers['name']})")

    def _callback(self, in_data, frame_count, time_info, status):
        chunk = _to_mono(np.frombuffer(in_data, dtype=np.float32), self.channels)
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()   #drop the OLDEST: the newest audio is what matters
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass
        return (None, self._pyaudio.paContinue)

    def read(self, timeout=0.1):
        """The next chunk, or None if nothing arrived within `timeout` (nothing is playing)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._audio.terminate()


class WavFileSource:
    """A WAV recording served in chunks, optionally at real-time speed. Ends with None forever."""

    def __init__(self, path, chunk_seconds=0.01, realtime=True):
        self.samples, self.sample_rate = read_wav(path)
        self.device_name = str(path)
        self.dropped = 0          #same attribute as LoopbackSource; a file never drops anything
        self._step = max(1, int(chunk_seconds * self.sample_rate))
        self._position = 0
        self._realtime = realtime
        self._started = None

    @property
    def finished(self):
        return self._position >= len(self.samples)

    def read(self, timeout=0.1):
        if self.finished:
            time.sleep(min(timeout, 0.05))
            return None
        if self._realtime:
            if self._started is None:
                self._started = time.monotonic()
            due = self._started + self._position / self.sample_rate
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, timeout))
                if wait > timeout:
                    return None
        chunk = self.samples[self._position:self._position + self._step]
        self._position += self._step
        return chunk

    def close(self):
        pass
