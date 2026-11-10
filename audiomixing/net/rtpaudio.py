"""The audio data plane: send sessions, receive sessions, jitter buffers, mixing.

Design notes
------------
* One **sender session** per (source, sink) pair. The source samples the local
  audio, the session packetises it and unicasts it to the sink's audio port.
* One **receiver** per node (a single UDP socket on the audio port). Every packet
  is routed to a per-stream jitter buffer keyed by ``ssrc``.
* Timing: the sender stamps each packet with its own wall clock. The receiver
  learns ``offset = remote_clock - local_clock`` from PING/PONG exchanges and then
  schedules samples with ``play_at = packet_ts - offset + jitter_target``, which
  puts every stream on one shared local timeline, whatever machine it came from.
* Mixing happens in the caller's render loop: ``Receiver.mix(into, frames)``
  pulls the next block from every active stream and saturates the sum.

The receiver keeps its samples in a numpy ring (fast mixing) but accepts plain
``array('f')`` from any audio source.
"""

from __future__ import annotations

import array
import json
import socket
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is bundled, but stay safe
    _np = None

from .. import proto
from ..proto import (
    CHANNELS,
    CODEC_OPUS,
    CODEC_PCM_F32,
    CODEC_PCM_S16,
    CODEC_NAMES,
    FLAG_DISCONTINUITY,
    Packet,
    SAMPLE_RATE,
    StreamStats,
    now_ns,
)

JITTER_MIN_MS = 25.0
JITTER_MAX_MS = 150.0
JITTER_START_MS = 55.0
# Latency trimming: above `target + slack`, the oldest frames are skipped until
# the delay is back where it belongs. A read only ever consumes one block, so
# without this a single burst of arrivals leaves the stream permanently deep.
#
# ★ 2026-11-03 用户（图3）：「缓冲 60.0 毫秒，目标 25.0 毫秒」两者互相矛盾。
#   自适应把 target 压到下限 25ms 后，trim 的阈值却是 target + 60 = 85ms ——
#   60ms 的实际深度**永远低于阈值**，于是 target 一路显示 25ms，缓冲却永远停在 60ms。
#   slack 收紧到 20ms、单次步长 10ms：阈值降到 45ms，实际深度会跟着目标收敛，
#   同时仍留 20ms 余量吸收 Wi-Fi 抖动（不会为了对齐显示而制造欠载/爆音）。
JITTER_TRIM_SLACK_MS = 20.0
JITTER_TRIM_STEP_MS = 10.0
# ★ 2026-10-19 用户：「音频质量出现了下降，存在短小杂音」。
#   一个块只差**几个采样**（网络抖动让这一包晚到几毫秒）时，原来直接补静音 ——
#   波形被硬切到 0 再跳回来，那就是一声"咔"。缺口不超过这个宽度就改成**保持最后一个采样**
#   （≤2 毫秒的直流保持，听不出来），更大的缺口才是真的断流、保持静音。
JITTER_HOLD_MS = 2.0
# No packet for this long means the peer paused; the resulting silence is not an
# underrun and must not be allowed to inflate the target.
JITTER_IDLE_NS = 120_000_000
PING_INTERVAL_S = 1.0
STATS_INTERVAL_S = 1.0
STREAM_TIMEOUT_S = 4.0
PCM_FRAMES_PER_PACKET = 480  # 10 ms @48k

import os as _os


def _chunk_peak(samples) -> float:
    """一块音频的峰值（0..1）。

    面板上每台设备的「送 / 收」现在是**音量显示条**，不是码率条：码率跟"声音大小"完全
    没关系（静音也照样在按 1536kbps 出数据），所以亮度必须从这里来。
    """
    if samples is None:
        return 0.0
    try:
        if _np is not None:
            # 不能写 `if not samples`：这里传进来的可能是 numpy 数组，
            # 对多元素数组取真假会直接抛"truth value is ambiguous"。
            block = _np.asarray(samples, dtype=_np.float32)
            return 0.0 if block.size == 0 else float(_np.max(_np.abs(block)))
        if len(samples) == 0:
            return 0.0
        return max(abs(float(s)) for s in samples)
    except (TypeError, ValueError):
        return 0.0

DEBUG_JITTER = bool(_os.environ.get("AUDIOMIXING_DEBUG_JITTER") or _os.environ.get("WAVELINK_DEBUG_JITTER"))


# HWHHYN 用 DEEPSEEK 制作
def _jdbg(*args) -> None:
    if DEBUG_JITTER:
        print("[jitter]", *args, file=__import__("sys").stderr)


def _frames_to_ms(frames: int) -> float:
    return frames * 1000.0 / SAMPLE_RATE


def _ms_to_frames(ms: float) -> int:
    return int(round(ms * SAMPLE_RATE / 1000.0))


# --------------------------------------------------------------------------
# Jitter buffer
# --------------------------------------------------------------------------

class JitterBuffer:
    """Per-stream sample queue on a shared local timeline.

    Stores interleaved stereo frames in a numpy ring, tracks the local clock value
    at which each stored frame should be played, and adapts its target depth to
    the observed network delay.
    """

    def __init__(
        self,
        channels: int = CHANNELS,
        capacity_ms: float = 400.0,
        name: str = "",
        route_id: str = "",
        route_ids: Iterable[str] | None = None,
    ) -> None:
        self.channels = channels
        self.name = name
        # One stream can be wanted by several local sinks - the same sound played
        # on two devices at once - so membership is a set of route ids, not one id.
        self.route_ids: set[str] = {r for r in (route_ids or ()) if r}
        if route_id:
            self.route_ids.add(route_id)
        self.capacity = _ms_to_frames(capacity_ms)
        if _np is not None:
            self._buf = _np.zeros((self.capacity, channels), dtype=_np.float32)
        else:  # pragma: no cover
            self._buf = array.array("f", bytes(4 * self.capacity * channels)).tolist()
        self._head = 0  # next frame to play
        self._count = 0  # frames stored
        self._play_at = 0  # local ns of the next frame to play
        self._have_timeline = False
        self._primed = False
        self._warmup_hint_ns = 0
        self.target_frames = _ms_to_frames(JITTER_START_MS)
        self.samples_written = 0
        self.samples_played = 0
        self.dropped_late = 0
        self.dropped_overflow = 0
        self.trimmed = 0
        self.underruns = 0
        self.resyncs = 0
        # ★ 2026-10-19：块里只差几个采样时"保持最后一个采样"（而不是补静音）的次数 ——
        #   它和 underruns 分开计数：这些缺口是听不出来的，不该被算成"断流"（也就不会去撑大缓冲）。
        self.held_gaps = 0
        self.last_packet_ns = 0
        self.started_ns = now_ns()
        self._last_adjust_ns = now_ns()
        # 这一路**收到**的声音有多大声（面板「收」那根音量条用它）。
        self.peak = 0.0
        self._peak_buffered = 0
        self.max_depth_ms = 0.0
        self._dbg_last_bucket = -1
        self._primed_wait_ns = 0
        self._last_underruns = 0
        # ★ 2026-11-03（Q2）：自适应要看的每秒增量。`_last_late/_last_overflow` 是上一秒
        #   的累计值，差值才是"这一秒又晚到/溢出多少"；只看累计值会让一次旧事件永远
        #   把目标往上推。
        self._last_late = 0
        self._last_overflow = 0
        self._last_written = 0
        # ★ 2026-11-04（Q1）：自适应不能再用"这一秒只要出现过一次欠载就涨"。
        #   用户现场的 5982 秒里欠载 2945 次 ≈ 0.5%/块，但按"每秒 > 0"判，
        #   几乎每秒都涨 5ms，目标被钉死在 150ms 上限 —— 欠载并没有因此消失。
        #   这里改为算**欠载率**（欠载 / 本秒读块数），并给收缩加"连续干净"条件，
        #   避免目标在 25~150 之间来回震荡。
        self._reads = 0
        self._last_reads = 0
        self._clean_s = 0
        # ★ 2026-11-04（Q1）：这条流播出来时还要乘多少（0..1）。
        #   为什么不能只乘在播放端的 `gain` 上：频道收听那条路由的 `from` 是通配 `["*"]`，
        #   一个播放端里可能同时混着**多台来源**，一台手机的音量不能去改整条路由的增益。
        #   所以"来源音量"按流来乘（由引擎在每次 apply_routes 里写），播放端只负责路由增益。
        self.source_gain = 1.0

    # -- write side --------------------------------------------------------
    def write(
        self,
        samples,
        ts_ns: int,
        offset_ns: float = 0.0,
        discontinuity: bool = False,
        seq: int | None = None,
        count_late: bool = True,
    ) -> None:
        """Queue `samples` (interleaved float32) captured at remote clock `ts_ns`.

        `offset_ns` is ``remote_clock - local_clock`` for that peer, so
        ``ts_ns - offset_ns`` is when the packet was captured on *our* clock. The
        frames belong at that instant, therefore we want to be playing them
        ``target`` milliseconds later; that is the depth the playout buffer keeps.

        The playhead is sample-driven, not timestamp-driven: once anchored, it
        advances by ``samples_played``. Packet timestamps only steer the anchor, so
        ordinary network jitter (or a late packet) can never push the playhead into
        the future and manufacture phantom latency.
        """
        n = len(samples) // self.channels
        if n <= 0:
            return
        target_ns = self.target_frames * 1_000_000_000 // SAMPLE_RATE
        capture_local_ns = int(ts_ns - offset_ns)
        desired = capture_local_ns + target_ns
        now = now_ns()
        self.last_packet_ns = now
        _jdbg(
            "write n=%d depth=%.0f primed=%s play_at-now=%.1fms" % (n, self.depth_ms(), self._primed, (self._play_at - now) / 1e6)
        ) if DEBUG_JITTER and self.samples_written % (n * 25) == 0 else None
        if not self._have_timeline:
            self._have_timeline = True
            self._play_at = max(desired, now - target_ns)
        elif not self._primed:
            # Pre-roll: keep the anchor tied to the newest packet so that the
            # queued depth at the moment playback starts is exactly the target.
            self._play_at = desired
        else:
            drift_ns = desired - self._play_at
            # Forward jumps mean the peer restarted (or its clock was corrected) and
            # a different anchor is now correct. Backward jumps only mean this packet
            # is stale, so they are ignored unless the peer explicitly says so.
            if discontinuity and drift_ns > 20_000_000:
                self._play_at = desired
                self.resyncs += 1
            elif drift_ns > 200_000_000:
                self._play_at = desired
                self.resyncs += 1
        if count_late and self._primed and desired + int(n * 1_000_000_000 / SAMPLE_RATE) < now:
            # The whole packet is already due (arrived very late): count it but do
            # not touch the playhead, the samples simply play as soon as possible.
            #
            # ★ 2026-11-04（Q1）：只有在**时钟已同步**时才允许计"晚到"。offset 为 0 时
            #   `desired` 根本不在本机时间轴上（手机墙钟与电脑差 1.17 秒，现场
            #   597844 / 597850 包全被判晚到），这是计数口径错，不是链路差。
            self.dropped_late += 1
        # Drop the oldest frames if this packet would overflow the ring.
        if self._count + n > self.capacity:
            drop = self._count + n - self.capacity
            self._head = (self._head + drop) % self.capacity
            self._count -= drop
            self.dropped_overflow += drop
        idx = (self._head + self._count) % self.capacity
        if _np is not None:
            arr = _np.frombuffer(memoryview(samples), dtype=_np.float32) if isinstance(samples, array.array) else _np.asarray(samples, dtype=_np.float32)
            arr = arr.reshape(n, self.channels)
            first = min(n, self.capacity - idx)
            self._buf[idx : idx + first] = arr[:first]
            if first < n:
                self._buf[: n - first] = arr[first:]
        else:  # pragma: no cover
            for i in range(n):
                base = i * self.channels
                self._buf[((idx + i) % self.capacity) * self.channels : ((idx + i) % self.capacity) * self.channels + self.channels] = list(
                    samples[base : base + self.channels]
                )
        self._count += n
        self.samples_written += n
        # 收到多大声：这里拿到的是**解码后**的采样，正好是"对面送过来多大声"。
        self.peak = max(_chunk_peak(samples), self.peak * 0.8)
        self._peak_buffered = max(self._peak_buffered, self._count)

    # -- read side ---------------------------------------------------------
    def read(self, frames: int):
        """Return exactly `frames` frames for the current local time, padding with
        silence until the stream has accumulated `target_frames` of pre-roll.

        Playhead rules (``_play_at`` is the local time of the frame at ``_head``):

        * warm-up: `_play_at` tracks the newest packet, nothing is consumed, so the
          queued depth when priming completes is exactly the target;
        * playing: `_play_at` advances by the frames actually played. A short block
          pads with silence but does **not** add fake latency;
        * resync: if the playhead drifts outside a sane window (the peer restarted,
          the machine slept, a long gap in the middle of a stream), it is re-anchored
          to ``now - target``, which makes the queued data play immediately without
          counting as a data drop.
        """
        out = _np.zeros((frames, self.channels), dtype=_np.float32) if _np is not None else [0.0] * (frames * self.channels)
        now = now_ns()
        step_ns = 1_000_000_000 // SAMPLE_RATE
        target_ns = self.target_frames * step_ns
        if not self._primed:
            # One-way latch: once playback has started it never re-arms, otherwise a
            # single shallow moment could stop the stream for good. Priming only
            # requires *some* audio (200 us) plus the target depth when we can afford
            # to wait for it.
            if not self._have_timeline or self._count == 0:
                self.underruns += 1
                return out
            if self.depth_ms() < _frames_to_ms(self.target_frames) and self._primed_wait_ns < 500_000_000:
                self._primed_wait_ns += frames * step_ns
                self.underruns += 1
                return out
            self._primed = True
            self.underruns = 0
            # ★ 2026-11-03（Q2）：预滚阶段收进来的包还没有真正开始播，这期间的
            #   溢出/晚到只是"还没开播"的副作用，不能永久挂在面板上误导判断。
            #   真正开始播放的这一刻清零一次，之后再累计就都是播放期间的真实事件。
            self.dropped_late = 0
            self.dropped_overflow = 0
            self._last_late = 0
            self._last_overflow = 0
        if not self._have_timeline:
            self.underruns += 1
            return out
        # Re-anchor if the playhead is not where a healthy stream would be.
        if self._play_at > now + target_ns + 5_000_000 or self._play_at < now - target_ns - 100_000_000:
            self._play_at = now - target_ns
            self.resyncs += 1
        # Latency trim: if the ring holds much more than the target, skip the oldest
        # frames (a block-sized step per read) so the delay converges back down.
        trim_above = self.target_frames + _ms_to_frames(JITTER_TRIM_SLACK_MS)
        if self._count > trim_above:
            drop = min(self._count - self.target_frames, _ms_to_frames(JITTER_TRIM_STEP_MS))
            drop = max(drop, 0)
            if drop:
                self._head = (self._head + drop) % self.capacity
                self._count -= drop
                self.trimmed += drop
        # Everything in the ring that is due by the time this block finishes playing,
        # capped at one block. Taking the deficit into account here is what makes the
        # loop self-correcting: a short fall behind is made up on the next block
        # instead of accumulating until the ring overflows.
        horizon = now + frames * step_ns
        take = 0
        if self._count > 0:
            due_ns = horizon - self._play_at
            if due_ns > 0:
                take = min(frames, self._count, int(due_ns // step_ns) + 1)
        if take > 0:
            if _np is not None:
                first = min(take, self.capacity - self._head)
                out[:first] = self._buf[self._head : self._head + first]
                if first < take:
                    out[first:take] = self._buf[: take - first]
            else:  # pragma: no cover
                for i in range(take):
                    src = ((self._head + i) % self.capacity) * self.channels
                    out[i * self.channels : (i + 1) * self.channels] = self._buf[src : src + self.channels]
            self._head = (self._head + take) % self.capacity
            self._count -= take
            self.samples_played += take
        if take < frames and now - self.last_packet_ns < JITTER_IDLE_NS:
            # ★ 2026-10-19：小缺口保持最后一个采样（不补静音）——见文件上面 JITTER_HOLD_MS 那段。
            #   只有"这一包真的到了、只是晚了几毫秒"（last_packet_ns 在 idle 窗口内）才这么做；
            #   真断流（take==0 或者缺口很大）仍然保持静音，那时本来就没有内容。
            hold = frames - take
            if take > 0 and hold <= _ms_to_frames(JITTER_HOLD_MS):
                out[take:] = out[take - 1]
                self.held_gaps += 1
            else:
                self.underruns += 1
        self._play_at = max(self._play_at + take * step_ns, now - target_ns)
        self.max_depth_ms = max(self.max_depth_ms, self.depth_ms())
        # 自适应按"块"算比率：欠载次数 / 本秒读块数，和晚到包的比率同一量纲。
        self._reads += 1
        self._adapt(now)
        if DEBUG_JITTER and (self.samples_played // (frames * 20) != self._dbg_last_bucket):
            self._dbg_last_bucket = self.samples_played // (frames * 20)
            _jdbg(
                "read t=%.2fs played=%d depth=%.0f play_at-now=%.1fms take=%d/%d count=%d target=%.0f"
                % (
                    (now - self.started_ns) / 1e9,
                    self.samples_played,
                    self.depth_ms(),
                    (self._play_at - now) / 1e6,
                    take,
                    frames,
                    self._count,
                    _frames_to_ms(self.target_frames),
                )
            )
        return out

    def available_frames(self, now_ns_value: float | None = None) -> int:
        now = now_ns() if now_ns_value is None else now_ns_value
        if not self._have_timeline:
            return 0
        # Frames whose scheduled play time is <= now.
        head_play_ns = self._play_at
        if head_play_ns > now:
            return 0
        due_ns = now - head_play_ns
        due = int(due_ns // (1_000_000_000 // SAMPLE_RATE))
        return max(0, min(self._count, due + 1))

    @property
    def buffered_frames(self) -> int:
        return self._count

    @property
    def buffered_ms(self) -> float:
        return _frames_to_ms(self._count)

    def depth_ms(self, now: float | None = None) -> float:
        """How much audio is queued for this stream, in milliseconds of sound.

        This is the quantity the adaptive controller cares about: samples sitting in
        the ring that have not been played yet, independent of the playhead.
        """
        return _frames_to_ms(self._count)

    def _adapt(self, now: float) -> None:
        """Tune the pre-roll depth from what the link is actually doing.

        Growth is driven by **underruns** (the real symptom of too little buffer)
        and by **late packets** (★ 2026-11-03 Q2: 迟到比例 > 10% 说明 25ms 下限根本
        不够用 —— 包到了但已经"过期"，样本照播、时间线错位，听感上是间隙/跳动).
        Growing on the *peak queue depth* instead (what this used to do) is
        self-reinforcing: one burst raises the target, the higher target keeps the
        queue deep, and the next peak raises it again, until the buffer parks at
        JITTER_MAX_MS for the rest of the session. Shrinking is deliberately slower
        than growing so the loop cannot oscillate.

        **Overflow never grows the target**: it means the ring is already too deep
        (or playback stalled), so growing would make it worse. The per-second delta
        is consumed (baseline reset) and reported through the counters; at most we
        fall through to the slow shrink below.
        """
        if now - self._last_adjust_ns < 1_000_000_000:
            return
        self._last_adjust_ns = now
        self.max_depth_ms = 0.0
        target_ms = _frames_to_ms(self.target_frames)
        underruns = self.underruns - getattr(self, "_last_underruns", 0)
        self._last_underruns = self.underruns
        late = self.dropped_late - self._last_late
        self._last_late = self.dropped_late
        overflow = self.dropped_overflow - self._last_overflow
        self._last_overflow = self.dropped_overflow
        written = self.samples_written - self._last_written
        self._last_written = self.samples_written
        reads = self._reads - self._last_reads
        self._last_reads = self._reads
        # 每秒写了多少"包"：按标准 10ms 包长折算。迟到的分子也是"包数"，
        # 两者同单位，比例才有意义。样本不足一包时按 0 处理，避免除零。
        packets = written / float(PCM_FRAMES_PER_PACKET)
        late_ratio = (late / packets) if packets >= 1.0 else 0.0
        # 欠载率：模糊的单次欠载很常见（用户现场 0.5%/块），不该把目标一路推到上限。
        underrun_ratio = (underruns / float(reads)) if reads >= 1 else 0.0
        if overflow > 0:
            # 溢出说明缓冲已经太深/播放端停顿，再涨目标只会更糟；不涨。
            grow_needed = False
        else:
            # 涨：真的缺缓冲才涨。欠载 > 2% 或晚到 > 10% 才算"缺"；
            # 更严重的（欠载 > 5% / 晚到 > 25%）一次涨 10ms，否则 5ms。
            grow_needed = underrun_ratio > 0.02 or late_ratio > 0.10
        if grow_needed and target_ms < JITTER_MAX_MS:
            step_ms = 10.0 if (underrun_ratio > 0.05 or late_ratio > 0.25) else 5.0
            grow = min(_ms_to_frames(step_ms),
                       _ms_to_frames(JITTER_MAX_MS) - self.target_frames)
            if grow > 0:
                self.target_frames += grow
                # A deeper target means the playhead starts later, so hand the extra
                # depth over immediately instead of waiting for it to accumulate.
                self._play_at += grow * (1_000_000_000 // SAMPLE_RATE)
            self._clean_s = 0
            return
        # 收要慢：连续 5 秒完全没有欠载/晚到/溢出才允许收缩，且每秒只收 2%。
        # 这样偶发抖动不会触发收缩，收缩后也不会立刻反弹 → 不会在 25~150 之间震荡。
        if underruns == 0 and late == 0 and overflow == 0:
            self._clean_s += 1
        else:
            self._clean_s = 0
        if self._clean_s >= 5 and target_ms > JITTER_MIN_MS:
            self.target_frames = max(_ms_to_frames(JITTER_MIN_MS), int(self.target_frames * 0.98))

    def stats(self) -> dict[str, Any]:
        age_s = max(0.0, (now_ns() - float(self.last_packet_ns or 0.0)) / 1e9)
        return {
            # 「收」音量条：这一路收到多大声（0..1）。
            "peak": round(float(self.peak), 4),
            # ★ 2026-11-03：这一路"现在还在不在收"。前端条子和文字都只看这个，
            #   不再用"峰值有没有衰减"这种间接推断（断流后 peak 会冻在旧值上）。
            "receiving": bool(self.samples_written and age_s < 3.0),
            "age_s": round(age_s, 2),
            # 统计口径：除 age_s 外，下面所有计数器都是"这一路从开始到现在"的累计值。
            "seconds": round(max(0.0, (now_ns() - float(self.started_ns or 0.0)) / 1e9), 1),
            "buffered_ms": round(self.buffered_ms, 1),
            "depth_ms": round(self.depth_ms(), 1),
            "target_ms": round(_frames_to_ms(self.target_frames), 1),
            "queued_frames": self._count,
            "written": self.samples_written,
            "played": self.samples_played,
            "underruns": self.underruns,
            # ★ 2026-10-19：块里只差几个采样、用"保持上一个采样"补上的次数（听不出来，不算断流）。
            "held_gaps": self.held_gaps,
            "late": self.dropped_late,
            "overflow": self.dropped_overflow,
            "trimmed": self.trimmed,
            "resyncs": self.resyncs,
            # ★ 2026-11-04（Q1）：给面板/诊断一个"比率"，用户不必自己拿 597844 去除
            #   5960 秒。分母分别是累计包数和累计读块数，与分子同量纲。
            "late_pct": (round(100.0 * self.dropped_late / (self.samples_written / float(PCM_FRAMES_PER_PACKET)), 2)
                         if self.samples_written >= PCM_FRAMES_PER_PACKET else 0.0),
            "underrun_pct": (round(100.0 * self.underruns / float(self._reads), 2)
                             if self._reads else 0.0),
        }


# --------------------------------------------------------------------------
# Decoder (raw PCM, optionally Opus)
# --------------------------------------------------------------------------

def make_decoder(codec: int, channels: int = CHANNELS):
    """Return callable(payload) -> interleaved float32 samples, or None if unsupported."""
    if codec == CODEC_PCM_S16:
        return lambda payload: proto.decode_pcm_s16(payload)
    if codec == CODEC_PCM_F32:
        return lambda payload: proto.decode_pcm_f32(payload)
    if codec == CODEC_OPUS:
        try:
            from ..audio.opus import OpusDecoder
        except ImportError:
            return None
        dec = OpusDecoder(channels=channels)
        return dec.decode
    return None


def make_encoder(codec: int, channels: int = CHANNELS):
    if codec == CODEC_PCM_S16:
        return proto.encode_pcm_s16
    if codec == CODEC_PCM_F32:
        return proto.encode_pcm_f32
    if codec == CODEC_OPUS:
        try:
            from ..audio.opus import OpusEncoder
        except ImportError:
            return None
        enc = OpusEncoder(channels=channels)
        return enc.encode
    return None


# --------------------------------------------------------------------------
# Receiver
# --------------------------------------------------------------------------

@dataclass
class StreamInfo:
    ssrc: int
    node_id: str = ""
    node_name: str = ""
    source_name: str = ""
    codec: int = CODEC_PCM_S16
    started_ns: int = field(default_factory=now_ns)
    # ★ 2026-11-04（审计 A1）：发送方的 IP。准入兜底只认"用户手动添加的对端"时要用它，
    #   不能只看 node_id（信标丢了就只剩地址这一个身份）。
    host: str = ""
    packets: int = 0
    bytes: int = 0
    last_seq: int | None = None
    lost: int = 0
    # ★ 2026-11-04（审计 A3）：重复包（同一 seq 又来一遍）单独计数，且不再写进缓冲。
    duplicates: int = 0
    route_id: str = ""


class Receiver:
    """Owns the audio UDP port; demultiplexes and mixes every incoming stream."""

    def __init__(
        self,
        node_id: str,
        port: int,
        *,
        on_first_packet: Callable[[StreamInfo, tuple[str, int]], None] | None = None,
        accept_source: Callable[[str], bool] | None = None,
        resolve_source: Callable[[str], str] | None = None,
        bind_host: str = "0.0.0.0",
        self_origin_id: int = 0,
        max_hop: int = proto.MAX_HOP,
    ) -> None:
        self.node_id = node_id
        # ★ 2026-11-04（B 第一阶段）：防自环。`self_origin_id` 是本机 device_id 的稳定哈希；
        #   收到 origin_id == 自己的帧，说明这台机器自己的声音绕了一圈又回来了 —— 立刻丢，
        #   并且单独计数（诊断页要显示"自回声丢弃 N"）。`max_hop` 是转发上限。
        #   老端没有扩展段（origin_id == 0），这两个判据都不触发，走老路径。
        self.self_origin_id = int(self_origin_id or 0)
        self.max_hop = int(max_hop or proto.MAX_HOP)
        self.packets_echo_dropped = 0
        self.packets_hop_dropped = 0
        self.port = port
        self.bind_host = bind_host
        self.streams: dict[int, StreamInfo] = {}
        self.buffers: dict[int, JitterBuffer] = {}
        self._decoders: dict[int, Callable[[bytes], Any] | None] = {}
        self._clock: dict[str, proto.ClockSync] = {}
        self._pending_pings: dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.on_first_packet = on_first_packet
        self.accept_source = accept_source
        self.resolve_source = resolve_source
        self.packets_received = 0
        self.packets_rejected = 0
        # ★ 2026-11-04（Q2）：被准入规则挡下的包（未经接受的点对点单播）。
        #   单独计数，且**不计入** packets_received / bytes_received —— 否则用户会在
        #   监控页看到"正在接收"和一条根本不让他听的流，统计也就失去意义了。
        self.packets_denied = 0
        self.handle_errors = 0
        self.bytes_received = 0
        self.last_error = ""
        self.mix_peak = 0.0
        self.mix_rms = 0.0
        self.control_messages: list[dict[str, Any]] = []
        self._owners: dict[int, str] = {}
        # ★ 2026-11-03 用户（图3）：路由行「数据量 9.2 MB，速率 0 KB/s」。
        #   根因是 Receiver.stats 从来没算过速率（SenderSession 有 kbps、接收流没有），
        #   前端 `stream.get("kbps")` 永远是 0。这里按真实字节增量算一个每秒窗口。
        self._rate_prev: dict[int, tuple[float, int, float]] = {}

    # HWHHYN 用 DEEPSEEK 制作
    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.bind((self.bind_host, self.port))
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(target=self._recv_loop, name="audiomixing-audio-rx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- receive -----------------------------------------------------------
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                self._housekeeping()
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            pkt = proto.decode(data)
            if pkt is None:
                self.packets_rejected += 1
                continue
            # ★ 2026-11-04（Q2）：接收准入。通配"只听"以前会接住所有进来的流，
            #   包括**本机没有接受的**点对点单播。这里在进入任何流表/计数器之前先判：
            #   认得出来源、且本机不接受它 → 直接丢包（packets_denied 单独计）。
            if not self._admitted(pkt, addr[0]):
                self.packets_denied += 1
                continue
            self.packets_received += 1
            self.bytes_received += len(data)
            try:
                self._handle(pkt, addr)
                self._housekeeping()
            except Exception as exc:  # a bad packet must never kill the receiver
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                self.handle_errors += 1
                if DEBUG_JITTER:
                    import traceback

                    traceback.print_exc()

    def _admitted(self, pkt: Packet, host: str) -> bool:
        """这条包能不能进流表（Q2 的接收准入）。

        只拦**音频包**，而且只在认得出来源时才拦 —— 认不出来的（手动 IP、信标丢了）
        仍然按频道处理，保持老行为；没有注入回调时也一律放行。
        """
        if pkt.type != proto.T_AUDIO:
            return True
        # ★ 防自环（B 第一阶段）：先于任何路由/频道判据执行。
        #   ① 原始声音是我自己 → 这是绕回来的自回声，绝不能播放（否则声音会一直绕圈）。
        #   ② hop 超过上限 → 这条帧已经在网里转了太多跳，继续传只会放大环。
        #   两个计数都要能在诊断里看到；老端 origin_id=0 不会命中。
        if (getattr(pkt, "origin_id", 0) and self.self_origin_id
                and pkt.origin_id == self.self_origin_id):
            self.packets_echo_dropped += 1
            return False
        if getattr(pkt, "hop", 0) > self.max_hop:
            self.packets_hop_dropped += 1
            return False
        if self.accept_source is None or self.resolve_source is None:
            return True
        node_id = self.resolve_source(host)
        if not node_id:
            return True
        return bool(self.accept_source(node_id))

    def _handle(self, pkt: Packet, addr: tuple[str, int]) -> None:
        if pkt.type == proto.T_AUDIO:
            self._handle_audio(pkt, addr)
            return
        if pkt.type == proto.T_PING:
            # Answer immediately so the peer can lock its clock.
            self._send_control(proto.T_PONG, proto.pong_body(pkt, now_ns(), now_ns()), addr)
            return
        if pkt.type == proto.T_PONG:
            body = pkt.json()
            node_id = str(body.get("node") or addr[0])
            with self._lock:
                cs = self._clock.get(node_id)
                if cs is None:
                    # Answer arrived before we had a record (or from a node we only
                    # know by address): keep the sample anyway.
                    cs = proto.ClockSync(node_id)
                    self._clock[node_id] = cs
            cs.update(pkt, now_ns())
            return
        if pkt.type in (proto.T_HELLO, proto.T_HELLO_ACK, proto.T_BYE):
            body = pkt.json()
            body["_from"] = "%s:%d" % addr
            body["_type"] = proto.TYPE_NAMES.get(pkt.type, str(pkt.type))
            # 到达时刻：引擎判"这台对端还在不在"要用它，否则一条旧消息会被反复当成新证据。
            body["_at"] = time.monotonic()
            with self._lock:
                self.control_messages.append(body)
                if len(self.control_messages) > 200:
                    del self.control_messages[:100]
            return
        if pkt.type == proto.T_STATS:
            body = pkt.json()
            body["_from"] = "%s:%d" % addr
            body["_type"] = "STATS"
            body["_at"] = time.monotonic()
            with self._lock:
                self.control_messages.append(body)
                # ★ 2026-11-02：统计包每 2 秒一条、永不停止；原来只给 HELLO 限长，
                #   这里无上限地长跑会让列表越积越大，引擎每 0.5s 还要全量遍历一次。
                if len(self.control_messages) > 200:
                    del self.control_messages[:100]
            return
        if pkt.type == proto.T_ROUTE:
            body = pkt.json()
            body["_from"] = "%s:%d" % addr
            body["_type"] = "ROUTE"
            with self._lock:
                self.control_messages.append(body)
                if len(self.control_messages) > 200:
                    del self.control_messages[:100]

    def _handle_audio(self, pkt: Packet, addr: tuple[str, int]) -> None:
        ssrc = pkt.ssrc
        with self._lock:
            info = self.streams.get(ssrc)
            if info is None:
                info = StreamInfo(ssrc=ssrc, codec=pkt.codec)
                self.streams[ssrc] = info
                self.buffers[ssrc] = JitterBuffer(name="ssrc-%08x" % ssrc)
                self._decoders[ssrc] = make_decoder(pkt.codec)
                if self.on_first_packet is not None:
                    # The callback resolves the sender, and the engine then attaches
                    # the stream to the local routes that actually want it.
                    self.on_first_packet(info, addr)
                self._owners[ssrc] = info.node_id or ("%s:%d" % addr)
                self._evict_superseded(ssrc)
            elif info.codec != pkt.codec:
                # ★ 2026-11-04（审计 B2，第 3 轮）：同一个 ssrc 换了编码（面板改编码，
                #   或发送端用同一条路由重启并换了编码 —— ssrc 由 node_id|route_id 派生，
                #   重启后不变）时，解码器必须跟着换。改前 `_decoders[ssrc]` 只在首包建
                #   一次，之后拿旧解码器解新负载：整条流变成噪声/无声，`info.codec` 还
                #   一直显示旧值，直到这条流被回收。
                info.codec = pkt.codec
                self._decoders[ssrc] = make_decoder(pkt.codec)
            node_id = info.node_id or ("%s:%d" % addr)
            cs = self._clock_for(node_id, addr)
        decoder = self._decoders.get(ssrc)
        if decoder is None:
            self.packets_rejected += 1
            return
        samples = decoder(pkt.payload) if pkt.payload else []
        # 用 len 判空，不用真值判断：numpy 数组的真值判断会抛异常，
        # 那样每个包都会被当成坏包丢掉，症状是"这条编码完全没声音"。
        if samples is None or len(samples) == 0:
            return
        offset = cs.offset_ns if cs.ready else 0.0
        if DEBUG_JITTER and self.packets_received % 40 == 0:
            _jdbg("audio pkt node_id=%r cs=%r offset_ms=%.3f" % (info.node_id, cs.name, offset / 1e6))
        buf = self.buffers[ssrc]
        duplicate = False
        with self._lock:
            info.packets += 1
            info.bytes += len(pkt.payload)
            if info.last_seq is not None:
                delta = (pkt.seq - info.last_seq) & 0xFFFF
                if delta == 0:
                    # ★ 2026-11-04（审计 A3）：同一 seq 的重复包。原来照样写进抖动缓冲，
                    #   于是这 10ms 音频被播第二遍、缓冲深度也凭空涨 10ms。
                    info.duplicates += 1
                    duplicate = True
                elif delta > 0x8000:
                    # ★ 2026-11-04（审计 A3）：乱序旧包。音频照收（晚到也能用），
                    #   **但不许把 last_seq 退回去** —— 原来无条件覆盖，下一个正常包
                    #   就会被算成"丢了几十个"，诊断页的丢包数据此虚高。
                    pass
                else:
                    if delta > 1:
                        info.lost += delta - 1
                    info.last_seq = pkt.seq
            else:
                info.last_seq = pkt.seq
        if duplicate:
            return
        # ★ 2026-11-04（Q1）：时钟没锁定时 offset=0，包时间戳不在本机时间轴上，
        #   此时"晚到"没有意义（现场因此把 99.99% 的包都算成晚到）。只在 ready 时计数。
        buf.write(samples, pkt.ts_ns, offset, bool(pkt.flags & FLAG_DISCONTINUITY),
                  pkt.seq, count_late=cs.ready)

    # ★ 2026-11-04（Q1）：时钟同步样本被记在「地址」名下，而音频流按「node_id」找它。
    #   用户现场的 `clocks` 就是两条：node_id 那条 samples=0，IP 那条 samples=8752、
    #   offset=-1173.839ms。音频流按 node_id 查 → 拿到 0 样本的时钟 → offset=0 →
    #   597844 / 597850 包（99.99%）全被判"晚到"。这里按 node_id → IP 顺序回退，
    #   并把 IP 的时钟登记到 node_id 名下（后面 stats 也就能看见真实偏移）。
    def _clock_for(self, node_id: str, addr: tuple[str, int] | None) -> proto.ClockSync:
        with self._lock:
            cs = self._clock.get(node_id)
            if cs is not None and cs.ready:
                return cs
            host = addr[0] if addr else ""
            alt = self._clock.get(host)
            if alt is not None and alt.ready:
                if node_id:
                    self._clock[node_id] = alt
                return alt
            if cs is not None:
                return cs
            cs = proto.ClockSync(node_id)
            if node_id:
                self._clock[node_id] = cs
            return cs

    def assign_routes(self, ssrc: int, route_ids: Iterable[str]) -> None:
        """Tell a stream which local sinks want it.

        An audio packet carries no route id, so the receiver cannot work this out
        by itself. It used to tag *every* incoming stream with whatever the first
        local sink happened to be, which meant a second sink received nothing and
        a stream that arrived before any sink existed stayed attached to nothing
        for the rest of the session. The engine derives the real answer from the
        route table and reports it here.
        """
        ids = {r for r in route_ids if r}
        with self._lock:
            buf = self.buffers.get(ssrc)
            info = self.streams.get(ssrc)
            if buf is not None:
                buf.route_ids = ids
            if info is not None and ids:
                info.route_id = ",".join(sorted(ids))
            self._owners.setdefault(ssrc, info.node_id if info is not None else "")

    def _evict_superseded(self, ssrc: int) -> None:
        """Drop older buffers that are really this sender's same route, restarted.

        A sender rebuilds its session - with a fresh SSRC - whenever the route
        table changes or the peer's address moves. Keeping both buffers means the
        same audio is mixed twice a few milliseconds apart, which is heard as a
        hollow, doubled sound. Streams from a different node, or for a route this
        one does not cover, are deliberately left alone.
        """
        owner = self._owners.get(ssrc)
        buf = self.buffers.get(ssrc)
        if buf is None or not owner:
            return
        mine = set(buf.route_ids)
        for other in [s for s in self.buffers if s != ssrc]:
            if self._owners.get(other) != owner:
                continue
            other_buf = self.buffers.get(other)
            if other_buf is None:
                continue
            if mine and other_buf.route_ids and not (mine & other_buf.route_ids):
                continue  # a different route from the same sender: both are wanted
            self.streams.pop(other, None)
            self.buffers.pop(other, None)
            self._decoders.pop(other, None)
            self._owners.pop(other, None)

    def _housekeeping(self) -> None:
        now = now_ns()
        with self._lock:
            dead = []
            for ssrc, info in self.streams.items():
                buf = self.buffers.get(ssrc)
                last = (buf.last_packet_ns if buf is not None else 0) or info.started_ns
                if (now - info.started_ns) / 1e9 > STREAM_TIMEOUT_S and (now - last) / 1e9 > STREAM_TIMEOUT_S:
                    dead.append(ssrc)
            for s in dead:
                self.streams.pop(s, None)
                self.buffers.pop(s, None)
                self._decoders.pop(s, None)
                self._owners.pop(s, None)

    def send_control(self, ptype: int, body: dict[str, Any], addr: tuple[str, int]) -> None:
        """公开入口：往某个地址发一个控制包（PONG 内部走的就是同一个函数）。

        引擎用它把「请求对方收听」「这条路由的增益」这些信息带出去 ——
        复用的是既有的 T_STATS（JSON 负载），旧版本收到会当未知字段忽略，不会崩。
        """
        self._send_control(ptype, body, addr)

    def _send_control(self, ptype: int, body: dict[str, Any], addr: tuple[str, int]) -> None:
        sock = self._sock
        if sock is None:
            return
        body = dict(body)
        body["node"] = self.node_id
        pkt = Packet.control(ptype, body)
        try:
            sock.sendto(pkt.encode(), addr)
        except OSError:
            pass

    # -- ping/pong clock sync ---------------------------------------------
    def ping_loop(self, targets: Callable[[], list[tuple[str, int, str]]], interval: float = PING_INTERVAL_S) -> None:
        while not self._stop.is_set():
            for host, port, node_id in targets():
                t0 = now_ns()
                with self._lock:
                    cs = self._clock.get(node_id)
                    if cs is None:
                        cs = proto.ClockSync(node_id)
                        self._clock[node_id] = cs
                pkt = Packet.control(proto.T_PING, {"t0": t0, "node": self.node_id})
                sock = self._sock
                if sock is None:
                    return
                try:
                    sock.sendto(pkt.encode(), (host, port))
                except OSError:
                    continue
            self._stop.wait(interval)

    # -- mixing ------------------------------------------------------------
    def mix(self, frames: int):
        """Return the mix of every active stream for the next `frames` frames."""
        return self.mix_by_route(frames).get("", _np.zeros((frames, CHANNELS), dtype=_np.float32))

    def mix_by_route(self, frames: int, routes: list[str] | None = None) -> dict[str, Any]:
        """Mix into one block per route id (a local sink).

        A packet may belong to several routes at once (two local listeners), in
        which case it is written into every matching buffer on arrival and simply
        ends up in several output blocks here.
        """
        if _np is None:  # pragma: no cover
            raise RuntimeError("numpy is required for mixing")
        with self._lock:
            items = list(self.buffers.items())
        wanted = set(routes) if routes is not None else None
        out: dict[str, Any] = {}
        for _ssrc, buf in items:
            targets = buf.route_ids & wanted if wanted is not None else (buf.route_ids or {""})
            if not targets:
                continue
            if not buf.samples_written:
                continue
            block = buf.read(frames)
            # ★ 2026-11-04（Q1）：来源音量按流乘一次，且只在这里乘一次。
            #   具名路由的播放端增益里已经乘过 vol（用户要的 `路由gain × vol`），
            #   所以那种流的 `source_gain` 由引擎写成 1.0；只有通配路由
            #   （`from=["*"]`，一个播放端混多台）才把 vol 乘在流上，避免平方衰减。
            gain = getattr(buf, "source_gain", 1.0)
            gain = 1.0 if gain is None else float(gain)
            if gain != 1.0:
                block = block * gain
            # One read feeds every route this stream belongs to; a copy is needed
            # when several sinks share it, because each output block is clipped
            # in place and summed into independently.
            for route in targets:
                acc = out.get(route)
                if acc is None:
                    out[route] = block.copy() if len(targets) > 1 else block
                else:
                    acc += block
        for acc in out.values():
            _np.clip(acc, -1.0, 1.0, out=acc)
        if out:
            self.mix_peak = float(max(_np.max(_np.abs(a)) for a in out.values()))
            self.mix_rms = float(max(_np.sqrt(_np.mean(a * a)) for a in out.values()))
        return out

    def active_stream_count(self) -> int:
        with self._lock:
            return sum(1 for s, b in self.buffers.items() if b.samples_written and (now_ns() - b.last_packet_ns) / 1e9 < STREAM_TIMEOUT_S)

    def clock_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            # node_id 与 IP 可能指向同一个 ClockSync（见 `_clock_for`），这里按对象去重，
            # 否则面板会把同一台设备显示成两条时钟。
            seen: set[int] = set()
            out: list[dict[str, Any]] = []
            for cs in self._clock.values():
                if id(cs) in seen:
                    continue
                seen.add(id(cs))
                out.append(cs.stats())
            return out

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            stream_stats = []
            for ssrc, info in self.streams.items():
                buf = self.buffers.get(ssrc)
                st = StreamStats(
                    ssrc=ssrc,
                    packets=info.packets,
                    lost=info.lost,
                )
                snap = buf.stats() if buf is not None else {}
                # ★ 2026-11-03：接收速率 = 两次 status 之间真实收到的字节 / 时间。
                #   窗口太短（同一秒内被轮询两次）就沿用上一次的速率，避免 0.1ms 的
                #   dt 把结果放大成天文数字。
                clock = self._clock.get(info.node_id) if info.node_id else None
                prev = self._rate_prev.get(ssrc)
                rate = float(prev[2]) if prev else 0.0
                if prev is not None:
                    dt = now - prev[0]
                    if dt >= 0.2:
                        rate = max(0.0, (float(info.bytes) - float(prev[1])) / 1000.0 / dt)
                self._rate_prev[ssrc] = (now, int(info.bytes), rate)
                stream_stats.append(
                    {
                        "ssrc": ssrc,
                        "node_id": info.node_id,
                        "node_name": info.node_name,
                        "source": info.source_name,
                        "codec": CODEC_NAMES.get(info.codec, str(info.codec)),
                        "packets": info.packets,
                        "lost": info.lost,
                        # ★ 2026-11-04（审计 A3）：重复包单独一列，和丢包分开看。
                        "duplicates": info.duplicates,
                        # 累计字节（整个这一路生命周期），和下面 kbps 的"每秒窗口"分开标注。
                        "bytes": info.bytes,
                        "kbps": round(rate, 1),
                        "route_id": (",".join(sorted(buf.route_ids)) if buf is not None and buf.route_ids else info.route_id),
                        **snap,
                        # ★ 2026-11-04（Q1）：这一路的包时间戳到底有没有被换算到本机时间。
                        #   `clock_ready=False` 时"晚到"一律不计（见 `_handle_audio`），
                        #   面板要写明原因，否则用户只看到一个永远 0 的计数器。
                        "clock_ready": bool(clock.ready) if clock is not None else False,
                        "clock_ms": (round(float(clock.offset_ns) / 1e6, 1)
                                     if clock is not None and clock.ready else None),
                        "clock_samples": int(clock.samples) if clock is not None else 0,
                        # 用**这个流的真实包数**做分母（Opus 是 20ms 包，不能用 480 帧估算）。
                        "late_pct": (round(100.0 * float(snap.get("late", 0)) / float(info.packets), 2)
                                     if info.packets else 0.0),
                        # ★ 2026-11-04（Q1）：这一路在混音时被乘上的来源音量。
                        #   频道路由（from=["*"]）只能按流乘，播放端那行的
                        #   "手机音量"查不到具体来源，用户要从这里看。
                        "media_vol": (round(float(getattr(buf, "source_gain", 1.0)), 3)
                                      if buf is not None else 1.0),
                    }
                )
            for ssrc in list(self._rate_prev):
                if ssrc not in self.streams:
                    self._rate_prev.pop(ssrc, None)
        return {
            "port": self.port,
            "packets_received": self.packets_received,
            "packets_rejected": self.packets_rejected,
            "packets_denied": self.packets_denied,
            # ★ 2026-11-04（B 第一阶段）防自环的两个独立计数（诊断页/报告要用）。
            "echo_dropped": self.packets_echo_dropped,
            "hop_dropped": self.packets_hop_dropped,
            "max_hop": self.max_hop,
            "self_origin_id": self.self_origin_id,
            "bytes_received": self.bytes_received,
            "active_streams": self.active_stream_count(),
            "streams": stream_stats,
            "clocks": self.clock_stats(),
            "mix_peak": round(self.mix_peak, 4),
            "mix_rms": round(self.mix_rms, 4),
        }


# --------------------------------------------------------------------------
# Sender
# --------------------------------------------------------------------------

def stable_ssrc(node_id: str, route_id: str) -> int:
    """A stream id that stays the same for one (sender, route) pair.

    It must be stable across restarts *and* differ between senders: the receiver
    identifies a stream by SSRC alone, so a fresh random id every time a session
    is rebuilt leaves it holding several half-dead buffers for the same audio.
    """
    value = zlib.crc32(("%s|%s" % (node_id, route_id)).encode("utf-8")) & 0x7FFFFFFF
    return value or 1


class SenderSession:
    """One source -> one sink stream."""

    def __init__(
        self,
        source,
        host: str,
        port: int,
        *,
        codec: int = CODEC_PCM_S16,
        gain: float = 1.0,
        ssrc: int | None = None,
        node_id: str = "",
        node_name: str = "",
        source_name: str = "",
        route_id: str = "",
        frames_per_packet: int = PCM_FRAMES_PER_PACKET,
        on_stats: Callable[[dict[str, Any]], None] | None = None,
        loopback_echo: bool = False,
        origin_id: int = 0,
        hop: int = 0,
    ) -> None:
        self.source = source
        self.host = host
        self.port = port
        self.codec = codec
        self.gain = gain
        self.node_id = node_id
        self.node_name = node_name
        self.source_name = source_name
        self.route_id = route_id
        self.ssrc = ssrc if ssrc is not None else stable_ssrc(node_id, route_id)
        self.frames_per_packet = frames_per_packet
        self.on_stats = on_stats
        self.loopback_echo = loopback_echo
        # ★ 2026-11-04（B 第一阶段）：原始声音主人 + 已转跳数。只有对端声明 ext:1
        #   时才由引擎传非 0 的 origin_id；老端保持 0 = 不发扩展段（向后兼容）。
        self.origin_id = int(origin_id or 0)
        self.hop = int(hop or 0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._encoder = make_encoder(codec)
        self.packets_sent = 0
        self.bytes_sent = 0
        self.frames_sent = 0
        self.sequence = 0
        # 这一路**送出去**的声音有多大声（面板「送」那根音量条用它）。
        self.peak = 0.0
        self.error = ""
        self.started_ns = 0
        self.last_send_ns = 0
        self.started_at = time.time()
        # ★ 2026-11-03（图2：「送」条亮着却写"没有在送"）：速率/在线状态要按
        #   "最近一秒有没有真的发出去"算，不能拿整个会话的平均值冒充实时值。
        self._rate_at = 0.0
        self._rate_bytes = 0
        self._rate_kbps = 0.0

    @property
    def supported(self) -> bool:
        return self._encoder is not None

    def start(self) -> None:
        if self._encoder is None:
            raise RuntimeError("codec %s is not available on this machine" % CODEC_NAMES.get(self.codec, self.codec))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self._sock.connect((self.host, self.port))
        self.source.start()
        self.started_ns = time.monotonic_ns()
        self._thread = threading.Thread(target=self._run, name="audiomixing-tx-%08x" % self.ssrc, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        try:
            self.source.stop()
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _run(self) -> None:
        """Read the source and emit 10 ms packets, pacing ourselves in real time.

        A sender must never rely on its source being paced: a file reader or a
        generator can produce an hour of audio in a second, which would flood the
        socket and the receiver's jitter buffer. Pacing here means every source is
        remote-controllable and the link always carries real-time audio.
        """
        pending = array.array("f")
        step_ns = 1_000_000_000 * self.frames_per_packet // SAMPLE_RATE
        next_deadline = time.monotonic_ns()
        while not self._stop.is_set():
            try:
                block = self.source.read()
            except Exception as exc:  # a device can disappear (headset unplugged)
                self.error = "%s: %s" % (type(exc).__name__, exc)
                break
            if block is None:
                # The capture ended (device removed, or the hub shut down).
                break
            if not block:
                if getattr(self.source, "eof", False):
                    break
                time.sleep(0.002)
                continue
            pending.extend(block)
            while len(pending) >= self.frames_per_packet * CHANNELS:
                take = self.frames_per_packet * CHANNELS
                chunk = pending[:take]
                del pending[:take]
                now = time.monotonic_ns()
                if next_deadline > now:
                    time.sleep((next_deadline - now) / 1e9)
                self._send_chunk(chunk)
                self.frames_sent += self.frames_per_packet
                next_deadline += step_ns
                if next_deadline < time.monotonic_ns() - 200_000_000:
                    # We were descheduled for a long time: resynchronise instead of
                    # trying to catch up with a burst.
                    next_deadline = time.monotonic_ns()
        # Flush whatever is left.
        if len(pending) >= CHANNELS:
            frames = len(pending) // CHANNELS
            self._send_chunk(pending[: frames * CHANNELS])

    def _send_chunk(self, chunk) -> None:
        """Send one audio packet.

        ★ samples 字段填的是【本包帧数】，不是累计帧偏移 ★
        这一条曾经填错（`samples=frame_offset`，即 480 × seq）：Python 接收端压根不读
        `pkt.samples`（它按解码后的负载长度算帧数），所以电脑↔电脑之间完全没有症状；
        而手机端把 samples 当准入判据（`samples > 每包帧数` 直接判坏包），于是电脑发出的
        **每一个音频包都被手机拒收** —— 用户"手机听不到电脑的声音"的根因就在这里。
        时间信息由 ts_ns 承担，累计偏移没有任何接收方需要。
        """
        if self._sock is None:
            return
        payload = self._encoder(chunk)
        if payload is None:
            return
        # ★ 2026-11-02：这条链路的共享增益**只在接收端乘一次**。
        #   以前发送端也按 route.gain 缩放（session.gain），接收端的 sink 又按同一个
        #   route.gain 再缩一次：两端显示 0.40 时，真正听到的只有 0.16，滑杆像"没效果"。
        #   发送端仍把 gain_q14 写进包头（如实描述这条流的音量），但不再改样本。
        level_source = chunk
        # 「送」电平仍按"这条链路最终会听到的大小"显示：样本不在发送端缩放，
        # 但电平平移了接收端要乘的那个共享增益（与原来 0.3×2.0=0.6 的口径一致）。
        self.peak = max(min(1.0, _chunk_peak(level_source) * self.gain), self.peak * 0.85)
        ts = now_ns()
        seq = self.sequence & 0xFFFF
        frames_here = len(chunk) // CHANNELS
        pkt = Packet(
            type=proto.T_AUDIO,
            payload=payload,
            codec=self.codec,
            seq=seq,
            ssrc=self.ssrc,
            samples=frames_here & 0xFFFFFFFF,
            ts_ns=ts,
            gain_q14=int(self.gain * 10000),
            origin_id=self.origin_id,
            hop=self.hop,
        )
        try:
            self._sock.send(pkt.encode())
        except OSError as exc:
            self.error = str(exc)
            return
        self.sequence += 1
        self.packets_sent += 1
        self.bytes_sent += len(payload)
        self.last_send_ns = time.monotonic_ns()

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        running = self._thread is not None and self._thread.is_alive()
        age_s = (now - self.last_send_ns / 1e9) if self.last_send_ns else float("inf")
        # 1.5 秒 ≈ 150 个音频包；丢几个包不至于把"在送"判成"没在送"。
        # 还没开始发的会话（last_send_ns==0）不按"断流"处理，否则测试/启动瞬间
        # 的峰值会被清成 0；只有"发过、又停了"才清零。
        sending = bool((running or self._thread is None)
                       and (age_s < 1.5 or (not self.last_send_ns and self.peak > 0)))
        if self._rate_at:
            dt = now - self._rate_at
            if dt >= 0.2:
                self._rate_kbps = max(0.0, (self.bytes_sent - self._rate_bytes) / 1000.0 / dt)
                self._rate_at = now
                self._rate_bytes = self.bytes_sent
        else:
            self._rate_at = now
            self._rate_bytes = self.bytes_sent
        return {
            "route_id": self.route_id,
            "ssrc": self.ssrc,
            "codec": CODEC_NAMES.get(self.codec, str(self.codec)),
            "host": self.host,
            "port": self.port,
            "source": self.source_name or getattr(self.source, "name", ""),
            "packets": self.packets_sent,
            "bytes": self.bytes_sent,
            "frames": self.frames_sent,
            # 「送」音量条：只有真的还在送才给电平；断流后 peak 会冻在旧值上，
            # 那就成了"条子亮着、文字写没有在送"的矛盾（用户图2）。
            "peak": round(float(self.peak) if sending else 0.0, 4),
            "kbps": round(self._rate_kbps, 1),
            "sending": sending,
            "gain": self.gain,
            "running": running,
            "error": self.error,
            "seconds": round(time.time() - self.started_at, 1),
        }


class Sender:
    """Manages all outgoing sessions of this node."""

    def __init__(self, node_id: str, node_name: str) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self.sessions: dict[str, SenderSession] = {}
        self._lock = threading.RLock()

    def add(self, route_id: str, session: SenderSession) -> None:
        self.remove(route_id)
        with self._lock:
            self.sessions[route_id] = session
        session.start()

    def remove(self, route_id: str) -> bool:
        with self._lock:
            session = self.sessions.pop(route_id, None)
        if session is not None:
            session.stop()
            return True
        return False

    def get(self, route_id: str) -> SenderSession | None:
        with self._lock:
            return self.sessions.get(route_id)

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for s in sessions:
            s.stop()

    def stats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.stats() for s in self.sessions.values()]

    def total_kbps(self) -> float:
        with self._lock:
            return round(sum(s.stats()["kbps"] for s in self.sessions.values()), 1)
