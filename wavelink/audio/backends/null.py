"""Null backend: no hardware at all.

Used for tests, for `wavelink doctor`, and as a safe fallback when no audio API
is available. It also provides the `tone` source (a 440 Hz sine) that lets the
whole network path be exercised without touching a sound card.
"""

from __future__ import annotations

import threading
import time
from array import array
from typing import Any

from ..format import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE, as_float_array, make_tone
from ..base import AudioDevice, BackendBase, KIND_SINK, KIND_SOURCE, register


class NullSource:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.name = spec.get("device") or "tone"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self._freq = float(spec.get("freq", 440.0))
        self._gain = float(spec.get("gain", 0.15))
        self._silent = self.name == "silence" or bool(spec.get("silent"))
        self._stop = threading.Event()
        self._i = 0
        self._next_ns = 0
        self.frames_emitted = 0

    def start(self) -> None:
        self._next_ns = time.monotonic_ns()

    def read(self) -> array:
        if self._stop.is_set():
            return array("f")
        now = time.monotonic_ns()
        block_ns = int(FRAME_SAMPLES * 1e9 / SAMPLE_RATE)
        if now < self._next_ns:
            time.sleep((self._next_ns - now) / 1e9)
        self._next_ns = max(self._next_ns + block_ns, time.monotonic_ns())
        n = FRAME_SAMPLES
        if self._silent:
            out = array("f", bytes(4 * n * CHANNELS))
        else:
            out = make_tone(self._freq, n, self._gain, CHANNELS)
        self._i += n
        self.frames_emitted += n
        return out

    def stop(self) -> None:
        self._stop.set()

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "null",
            "device": self.name,
            "kind": "null",
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "freq": self._freq,
            "silent": self._silent,
        }


class NullSink:
    """Consumes samples at wall-clock pace; records what it got for verification."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.name = spec.get("device") or "null"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self._stop = threading.Event()
        self._start_ns = 0
        self._frames = 0
        self.capture: array | None = array("f") if spec.get("capture") else None
        self.peak = 0.0
        self.silent_frames = 0

    def start(self) -> None:
        self._start_ns = time.monotonic_ns()

    def write(self, samples) -> int:
        if self._stop.is_set():
            return 0
        flat = as_float_array(samples)
        n = len(flat) // self.channels
        if self.capture is not None:
            self.capture.extend(flat)
        for s in flat:
            a = s if s >= 0 else -s
            if a > self.peak:
                self.peak = a
        self._frames += n
        # Pace to real time so the engine loop behaves like a device would.
        target_ns = self._start_ns + int(self._frames * 1e9 / SAMPLE_RATE)
        now = time.monotonic_ns()
        if target_ns > now:
            time.sleep((target_ns - now) / 1e9)
        return n

    def buffered_frames(self) -> int:
        return 0

    def target_frames(self) -> int:
        return 0

    def stop(self) -> None:
        self._stop.set()

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "null",
            "device": self.name,
            "kind": "null",
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self._frames,
            "peak": round(self.peak, 5),
        }


@register
class NullBackend(BackendBase):
    name = "null"
    platform = "any"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def list_devices(cls) -> list[AudioDevice]:
        return [
            AudioDevice("tone:440", "内置测试音 440Hz", KIND_SOURCE, "null", "generator", is_default=False),
            AudioDevice("tone:1000", "内置测试音 1kHz", KIND_SOURCE, "null", "generator"),
            AudioDevice("silence", "静音源", KIND_SOURCE, "null", "generator"),
            AudioDevice("null", "空输出（仅统计）", KIND_SINK, "null", "null", is_default=False),
        ]

    @classmethod
    def open_source(cls, spec: dict[str, Any]) -> NullSource:
        return NullSource(spec)

    @classmethod
    def open_sink(cls, spec: dict[str, Any]) -> NullSink:
        return NullSink(spec)
