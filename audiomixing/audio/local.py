"""One local capture device played straight into one local output device.

This is the single-machine case, and it is the common one: a phone arriving over
Bluetooth is a *capture* device on this PC, and the headphones are a render device on
the same PC, so the audio never has to touch the LAN at all.

It could be routed through the normal sender/receiver machinery, but that would add a
jitter buffer and a network round trip to something that is already local.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .format import CHANNELS, SAMPLE_RATE, apply_gain, as_float_array


# HWHHYN 用 DEEPSEEK 制作
def _dc_block(samples, state: list, cutoff_hz: float = 25.0):
    """One-pole high-pass, one state per channel.

    Loopback captures can carry a DC offset or sub-audio rumble. It is inaudible on a big
    speaker and turns into a loud rattle on a phone, which is what made a perfectly
    ordinary capture sound like noise.
    """
    if not samples:
        return samples
    import math

    channels = max(1, len(state))
    # y[n] = a*(y[n-1] + x[n] - x[n-1]) with a just below 1 gives a gentle high-pass
    # near the cutoff. Writing it as 1/(1+fs/2*pi*fc) inverts the coefficient and moves the
    # corner up to several kHz, which deletes the audio instead of the rumble - the loop
    # test caught exactly that.
    alpha = 1.0 - (2.0 * math.pi * max(1.0, cutoff_hz) / 48000.0)
    out = []
    for index, value in enumerate(samples):
        slot = index % channels
        previous_in, previous_out = state[slot]
        current = alpha * (previous_out + value - previous_in)
        state[slot] = (value, current)
        out.append(current)
    return out


def _soft_limit(samples, ceiling: float = 0.95):
    """Keep peaks under the ceiling without the crunch of hard clipping.

    The channel gain is a user setting and 1.4 is a normal thing to type, but multiplying
    past full scale then clamping produces the harsh, "broken speaker" distortion that a
    listener hears as terrible quality. Above the knee the signal is compressed smoothly
    instead, so a loud source stays loud without turning into square waves.
    """
    if not samples:
        return samples
    out = []
    knee = ceiling * 0.7
    span = max(1e-6, ceiling - knee)
    for value in samples:
        magnitude = value if value >= 0 else -value
        if magnitude <= knee:
            out.append(value)
        else:
            over = (magnitude - knee) / span
            shaped = knee + span * (over / (1.0 + over))
            out.append(shaped if value >= 0 else -shaped)
    return out


class LocalLoop(threading.Thread):
    """Capture -> render, both on this machine, paced by the capture device."""

    def __init__(self, source, sink, *, gain: float = 1.0, tap=None, name: str = "本机直通") -> None:
        super().__init__(daemon=True, name="wavelink-local")
        self.source = source
        self.sink = sink
        self.gain = float(gain)
        self._dc_state = [(0.0, 0.0), (0.0, 0.0)]
        self.short_writes = 0
        self.slow_writes = 0
        self.slow_write_ms = 0.0
        self.max_gap_ms = 0.0
        self.gaps = 0
        self._last_write_at = 0.0
        self.tap = tap
        self.label = name
        self._stop = threading.Event()
        self.frames = 0
        self.blocks = 0
        self.peak = 0.0
        self.ready = False
        # 延迟补偿：先把这么多毫秒的静音喂给这个设备，
        # 这一路就整体往后错开同样的时间（用来对齐蓝牙和有线）。
        self.pre_roll_ms = 0.0
        self.error = ""
        self.started_at = time.time()

    def run(self) -> None:
        try:
            self.source.start()
            self.sink.start()
        except Exception as exc:  # a device that will not open must not kill the node
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return
        self.ready = True
        try:
            if self.pre_roll_ms > 0:
                # 先补一段静音，让这一路晚一点出声。
                # 必须和真实数据块同一种类型（float 数组），否则播放端会直接抛异常，
                # 而异常被这里吞掉后表现为"这一路一个字都没写"。
                frames = int(SAMPLE_RATE * self.pre_roll_ms / 1000.0)
                chunk = 480
                silence = as_float_array([0.0] * (chunk * CHANNELS))
                while frames > 0 and not self._stop.is_set():
                    take = min(chunk, frames)
                    self.sink.write(silence[: take * CHANNELS])
                    frames -= take
            while not self._stop.is_set():
                block = self.source.read()
                if block is None:
                    # The capture was closed underneath us (a wedged source being
                    # rebuilt, or the device vanishing). Stop and let the engine rebuild
                    # the loop, instead of spinning on a reader that will never return.
                    self.error = "采集端已关闭"
                    break
                if not block:
                    continue
                if self.gain != 1.0:
                    block = _soft_limit(apply_gain(_dc_block(block, self._dc_state), self.gain))
                # Instrumented, because "accepted 100% of frames" does not prove the
                # device was fed *evenly*: writing in bursts starves the endpoint's own
                # small buffer, and that is heard as a stutter while every counter here
                # still looks perfect.
                started = time.monotonic()
                wrote = self.sink.write(block) or 0
                took_ms = (time.monotonic() - started) * 1000.0
                self.frames += wrote
                self.blocks += 1
                if wrote != len(block) // CHANNELS:
                    self.short_writes += 1
                if took_ms > self.slow_write_ms:
                    self.slow_write_ms = took_ms
                if took_ms > 20.0:
                    self.slow_writes += 1
                gap_ms = (started - self._last_write_at) * 1000.0 if self._last_write_at else 0.0
                if gap_ms > self.max_gap_ms:
                    self.max_gap_ms = gap_ms
                if gap_ms > 25.0:
                    self.gaps += 1
                self._last_write_at = started
                try:
                    self.peak = max(max(abs(s) for s in block), self.peak * 0.85)
                except (TypeError, ValueError):
                    pass
                if self.tap is not None:
                    # A phone played here is also what the phone listener page should
                    # hear, exactly like a network route.
                    self.tap.publish(block)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            for closer in (self.sink.stop, self.source.stop):
                try:
                    closer()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()

    def describe(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "frames": self.frames,
            "blocks": self.blocks,
            "peak": round(float(self.peak), 4),
            "gain": round(self.gain, 3),
            "short_writes": self.short_writes,
            "slow_writes": self.slow_writes,
            "slow_write_ms": round(self.slow_write_ms, 1),
            "max_gap_ms": round(self.max_gap_ms, 1),
            "gaps": self.gaps,
            "ready": self.ready,
            "error": self.error,
            "uptime_s": round(time.time() - self.started_at, 1),
            "source": getattr(self.source, "endpoint_name", getattr(self.source, "name", "")),
            "sink": getattr(self.sink, "name", "") or getattr(self.sink, "describe", lambda: {})().get("device", ""),
        }
