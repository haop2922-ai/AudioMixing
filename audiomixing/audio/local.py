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

from array import array

from .format import CHANNELS, SAMPLE_RATE, as_float_array


# 作者：haop2922（HWHHYN）
def _apply_gain_ramp(samples, state: list, gain_from: float, gain_to: float,
                     cutoff_hz: float = 25.0):
    """直流阻断 + 逐样本线性增益过渡 + 柔和限幅，一次循环做完。

    ★ 2026-11-17：用户「两路同时出声时调单路音量会出高频杂音」。原来增益一变就
      整块乘上去（`apply_gain`），块与块之间是阶跃；拖动滑杆时每块一个台阶，
      听感就是沙沙/咔咔的高频杂音。这里在**一块之内**把增益从 gain_from 线性
      过渡到 gain_to（通常 10ms 左右），台阶消失，不需要重开设备。
      块内过渡也不会产生新的设备格式或缓冲变化，所以另一路完全不受影响。
    """
    if not samples:
        return samples
    import math

    channels = max(1, len(state))
    frames = max(1, len(samples) // channels)
    step = (gain_to - gain_from) / frames
    alpha = 1.0 - (2.0 * math.pi * max(1.0, cutoff_hz) / 48000.0)
    knee = 0.95 * 0.7
    span = max(1e-6, 0.95 - knee)
    out = array("f")
    for index, value in enumerate(samples):
        slot = index % channels
        previous_in, previous_out = state[slot]
        current = alpha * (previous_out + value - previous_in)
        state[slot] = (value, current)
        gain = gain_from + step * (index // channels)
        v = current * gain
        magnitude = v if v >= 0 else -v
        if magnitude > knee:
            over = (magnitude - knee) / span
            shaped = knee + span * (over / (1.0 + over))
            v = shaped if v >= 0 else -shaped
        out.append(v)
    return out


class LocalLoop(threading.Thread):
    """Capture -> render, both on this machine, paced by the capture device."""

    def __init__(self, source, sink, *, gain: float = 1.0, tap=None, name: str = "本机广播监听") -> None:
        super().__init__(daemon=True, name="audiomixing-local")
        self.source = source
        self.sink = sink
        self.gain = float(gain)
        # ★ 2026-11-17：滑杆只改这个目标值，绝不重开设备；播放线程每块线性靠过去。
        self.gain_target = float(gain)
        self._gain_lock = threading.Lock()
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
                if self.gain != 1.0 or self.gain_target != 1.0:
                    target = self.gain_target
                    block = _apply_gain_ramp(block, self._dc_state, self.gain, target)
                    self.gain = target
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

    def set_gain(self, gain: float) -> None:
        """只改目标增益：不 stop、不重开、不碰采集和其它输出。"""
        try:
            value = float(gain)
        except (TypeError, ValueError):
            return
        value = max(0.0, min(4.0, value))
        with self._gain_lock:
            self.gain_target = value

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
