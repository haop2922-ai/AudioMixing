"""File backend: WAV sources and WAV sinks.

Indispensable for verification: it lets the full pipeline (capture -> encode ->
UDP -> jitter buffer -> mix -> render) be tested offline and byte-compared, with
no sound card and no timing flakiness.
"""

from __future__ import annotations

import os
import threading
import time
import wave
from array import array
from typing import Any

from ..format import (
    CHANNELS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Resampler,
    as_float_array,
    channel_map,
    to_float32,
)
from ..base import AudioDevice, BackendBase, KIND_SINK, KIND_SOURCE, register


def _wav_format(wf: wave.Wave_read) -> str:
    sw = wf.getsampwidth()
    if sw == 1:
        return "u8"
    if sw == 2:
        return "s16"
    if sw == 3:
        return "s24"
    return "s32"


class WavSource:
    """Reads a WAV file in real time (or as fast as the consumer wants, if `realtime=False`)."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.path = spec.get("path") or spec.get("device") or ""
        if not os.path.isfile(self.path):
            raise FileNotFoundError("wav file not found: %s" % self.path)
        self.wf = wave.open(self.path, "rb")
        self.src_rate = self.wf.getframerate()
        self.src_channels = self.wf.getnchannels()
        self.fmt = _wav_format(self.wf)
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.name = os.path.basename(self.path)
        self.realtime = bool(spec.get("realtime", True))
        self.loop = bool(spec.get("loop", False))
        self._resampler = Resampler(self.src_rate, SAMPLE_RATE, CHANNELS)
        self._stop = threading.Event()
        self._next_ns = 0
        self.frames_emitted = 0
        self._eof = False

    def start(self) -> None:
        self._next_ns = time.monotonic_ns()

    def _read_block(self) -> array:
        raw = self.wf.readframes(FRAME_SAMPLES)
        if not raw:
            if self.loop:
                self.wf.rewind()
                raw = self.wf.readframes(FRAME_SAMPLES)
            else:
                self._eof = True
                return array("f")
        f32 = to_float32(raw, self.fmt)
        f32 = channel_map(f32, self.src_channels, CHANNELS)
        return self._resampler.process(f32)

    def read(self) -> array:
        if self._stop.is_set() or self._eof:
            return array("f")
        if self.realtime:
            now = time.monotonic_ns()
            block_ns = int(FRAME_SAMPLES * 1e9 / SAMPLE_RATE)
            if now < self._next_ns:
                time.sleep((self._next_ns - now) / 1e9)
            self._next_ns = max(self._next_ns + block_ns, time.monotonic_ns())
        out = self._read_block()
        self.frames_emitted += len(out) // CHANNELS
        return out

    @property
    def eof(self) -> bool:
        return self._eof

    def stop(self) -> None:
        self._stop.set()
        try:
            self.wf.close()
        except Exception:
            pass

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "file",
            "device": self.path,
            "kind": "file",
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "source_rate": self.src_rate,
            "source_channels": self.src_channels,
            "format": self.fmt,
            "loop": self.loop,
        }


class WavSink:
    """Writes everything it receives to a WAV file (float32), optionally in real time."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.path = spec.get("path") or spec.get("device") or "wavelink-out.wav"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.name = os.path.basename(self.path)
        self.realtime = bool(spec.get("realtime", True))
        self.limit_seconds = float(spec.get("limit_seconds", 0) or 0)
        self._wav: wave.Wave_write | None = None
        self._frames = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        w = wave.open(self.path, "wb")
        w.setnchannels(self.channels)
        w.setsampwidth(4)
        w.setframerate(self.sample_rate)
        self._wav = w

    def write(self, samples) -> int:
        if self._stop.is_set() or self._wav is None:
            return 0
        # Flatten first: len() of an (N, channels) numpy array is N, not N*channels.
        flat = as_float_array(samples)
        n = len(flat) // self.channels
        if self.limit_seconds and self._frames >= self.limit_seconds * self.sample_rate:
            return 0
        with self._lock:
            self._wav.writeframes(flat.tobytes())
            self._frames += n
        return n

    def buffered_frames(self) -> int:
        return 0

    def target_frames(self) -> int:
        return 0

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                finally:
                    self._wav = None

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "file",
            "device": self.path,
            "kind": "file",
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self._frames,
        }


def wav_info(path: str) -> dict[str, Any]:
    with wave.open(path, "rb") as wf:
        return {
            "path": path,
            "channels": wf.getnchannels(),
            "sample_width": wf.getsampwidth(),
            "sample_rate": wf.getframerate(),
            "frames": wf.getnframes(),
            "seconds": round(wf.getnframes() / max(1, wf.getframerate()), 3),
        }


def read_wav_float(path: str) -> tuple[array, int, int]:
    with wave.open(path, "rb") as wf:
        fmt = _wav_format(wf)
        ch = wf.getnchannels()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    return to_float32(raw, fmt), ch, rate


@register
class FileBackend(BackendBase):
    name = "file"
    platform = "any"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def list_devices(cls) -> list[AudioDevice]:
        return [
            AudioDevice("file:in.wav", "WAV 文件（输入）", KIND_SOURCE, "file", "file"),
            AudioDevice("file:out.wav", "WAV 文件（输出）", KIND_SINK, "file", "file"),
        ]

    @classmethod
    def open_source(cls, spec: dict[str, Any]) -> WavSource:
        return WavSource(spec)

    @classmethod
    def open_sink(cls, spec: dict[str, Any]) -> WavSink:
        return WavSink(spec)
