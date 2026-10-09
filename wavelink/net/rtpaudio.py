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
JITTER_TRIM_SLACK_MS = 60.0
JITTER_TRIM_STEP_MS = 20.0
# No packet for this long means the peer paused; the resulting silence is not an
# underrun and must not be allowed to inflate the target.
JITTER_IDLE_NS = 120_000_000
PING_INTERVAL_S = 1.0
STATS_INTERVAL_S = 1.0
STREAM_TIMEOUT_S = 4.0
PCM_FRAMES_PER_PACKET = 480  # 10 ms @48k

import os as _os

DEBUG_JITTER = bool(_os.environ.get("WAVELINK_DEBUG_JITTER"))


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
        self.last_packet_ns = 0
        self.started_ns = now_ns()
        self._last_adjust_ns = now_ns()
        self._peak_buffered = 0
        self.max_depth_ms = 0.0
        self._dbg_last_bucket = -1
        self._primed_wait_ns = 0
        self._last_underruns = 0

    # -- write side --------------------------------------------------------
    def write(
        self,
        samples,
        ts_ns: int,
        offset_ns: float = 0.0,
        discontinuity: bool = False,
        seq: int | None = None,
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
        if self._primed and desired + int(n * 1_000_000_000 / SAMPLE_RATE) < now:
            # The whole packet is already due (arrived very late): count it but do
            # not touch the playhead, the samples simply play as soon as possible.
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
            self.underruns += 1
        self._play_at = max(self._play_at + take * step_ns, now - target_ns)
        self.max_depth_ms = max(self.max_depth_ms, self.depth_ms())
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

        Growth is driven by **underruns only** — the real symptom of too little
        buffer. Growing on the *peak queue depth* instead (what this used to do)
        is self-reinforcing: one burst raises the target, the higher target keeps
        the queue deep, and the next peak raises it again, until the buffer parks
        at JITTER_MAX_MS for the rest of the session. Shrinking is deliberately
        slower than growing so the loop cannot oscillate.
        """
        if now - self._last_adjust_ns < 1_000_000_000:
            return
        self._last_adjust_ns = now
        self.max_depth_ms = 0.0
        target_ms = _frames_to_ms(self.target_frames)
        underruns = self.underruns - getattr(self, "_last_underruns", 0)
        self._last_underruns = self.underruns
        if underruns > 0:
            if target_ms < JITTER_MAX_MS:
                self.target_frames = min(_ms_to_frames(JITTER_MAX_MS), self.target_frames + _ms_to_frames(5.0))
                # A deeper target means the playhead starts later, so hand the extra
                # depth over immediately instead of waiting for it to accumulate.
                self._play_at += _ms_to_frames(5.0) * (1_000_000_000 // SAMPLE_RATE)
            return
        if target_ms > JITTER_MIN_MS:
            self.target_frames = max(_ms_to_frames(JITTER_MIN_MS), int(self.target_frames * 0.97))

    def stats(self) -> dict[str, Any]:
        return {
            "buffered_ms": round(self.buffered_ms, 1),
            "depth_ms": round(self.depth_ms(), 1),
            "target_ms": round(_frames_to_ms(self.target_frames), 1),
            "queued_frames": self._count,
            "written": self.samples_written,
            "played": self.samples_played,
            "underruns": self.underruns,
            "late": self.dropped_late,
            "overflow": self.dropped_overflow,
            "trimmed": self.trimmed,
            "resyncs": self.resyncs,
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
    packets: int = 0
    bytes: int = 0
    last_seq: int | None = None
    lost: int = 0
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
        bind_host: str = "0.0.0.0",
    ) -> None:
        self.node_id = node_id
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
        self.packets_received = 0
        self.packets_rejected = 0
        self.handle_errors = 0
        self.bytes_received = 0
        self.last_error = ""
        self.mix_peak = 0.0
        self.mix_rms = 0.0
        self.control_messages: list[dict[str, Any]] = []
        self._owners: dict[int, str] = {}

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
        self._thread = threading.Thread(target=self._recv_loop, name="wavelink-audio-rx", daemon=True)
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
            with self._lock:
                self.control_messages.append(body)
                if len(self.control_messages) > 200:
                    del self.control_messages[:100]
            return
        if pkt.type == proto.T_STATS:
            body = pkt.json()
            body["_from"] = "%s:%d" % addr
            body["_type"] = "STATS"
            with self._lock:
                self.control_messages.append(body)
            return
        if pkt.type == proto.T_ROUTE:
            body = pkt.json()
            body["_from"] = "%s:%d" % addr
            body["_type"] = "ROUTE"
            with self._lock:
                self.control_messages.append(body)

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
            node_id = info.node_id or ("%s:%d" % addr)
            cs = self._clock.get(node_id)
            if cs is None:
                cs = proto.ClockSync(node_id)
                self._clock[node_id] = cs
        decoder = self._decoders.get(ssrc)
        if decoder is None:
            self.packets_rejected += 1
            return
        samples = decoder(pkt.payload) if pkt.payload else []
        if not samples:
            return
        offset = cs.offset_ns if cs.ready else 0.0
        if DEBUG_JITTER and self.packets_received % 40 == 0:
            _jdbg("audio pkt node_id=%r cs=%r offset_ms=%.3f" % (info.node_id, cs.name, offset / 1e6))
        buf = self.buffers[ssrc]
        with self._lock:
            info.packets += 1
            info.bytes += len(pkt.payload)
            if info.last_seq is not None:
                delta = (pkt.seq - info.last_seq) & 0xFFFF
                if 0 < delta < 0x8000 and delta > 1:
                    info.lost += delta - 1
            info.last_seq = pkt.seq
        buf.write(samples, pkt.ts_ns, offset, bool(pkt.flags & FLAG_DISCONTINUITY), pkt.seq)

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
            return [cs.stats() for cs in self._clock.values()]

    def stats(self) -> dict[str, Any]:
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
                stream_stats.append(
                    {
                        "ssrc": ssrc,
                        "node_id": info.node_id,
                        "node_name": info.node_name,
                        "source": info.source_name,
                        "codec": CODEC_NAMES.get(info.codec, str(info.codec)),
                        "packets": info.packets,
                        "lost": info.lost,
                        "bytes": info.bytes,
                        "route_id": (",".join(sorted(buf.route_ids)) if buf is not None and buf.route_ids else info.route_id),
                        **snap,
                    }
                )
        return {
            "port": self.port,
            "packets_received": self.packets_received,
            "packets_rejected": self.packets_rejected,
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._encoder = make_encoder(codec)
        self.packets_sent = 0
        self.bytes_sent = 0
        self.frames_sent = 0
        self.sequence = 0
        self.error = ""
        self.started_ns = 0
        self.last_send_ns = 0
        self.started_at = time.time()

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
        self._thread = threading.Thread(target=self._run, name="wavelink-tx-%08x" % self.ssrc, daemon=True)
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
        frames_sent = 0
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
                self._send_chunk(chunk, frames_sent)
                frames_sent += self.frames_per_packet
                self.frames_sent += self.frames_per_packet
                next_deadline += step_ns
                if next_deadline < time.monotonic_ns() - 200_000_000:
                    # We were descheduled for a long time: resynchronise instead of
                    # trying to catch up with a burst.
                    next_deadline = time.monotonic_ns()
        # Flush whatever is left.
        if len(pending) >= CHANNELS:
            frames = len(pending) // CHANNELS
            self._send_chunk(pending[: frames * CHANNELS], frames_sent)

    def _send_chunk(self, chunk, frame_offset: int) -> None:
        if self._sock is None:
            return
        payload = self._encoder(chunk)
        if payload is None:
            return
        if self.gain != 1.0:
            scaled = array.array("f", (s * self.gain for s in chunk))
            payload = self._encoder(scaled)
        ts = now_ns()
        seq = self.sequence & 0xFFFF
        pkt = Packet(
            type=proto.T_AUDIO,
            payload=payload,
            codec=self.codec,
            seq=seq,
            ssrc=self.ssrc,
            samples=frame_offset & 0xFFFFFFFF,
            ts_ns=ts,
            gain_q14=int(self.gain * 10000),
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
        elapsed = max(1e-6, time.monotonic() - (self.started_ns / 1e9 if self.started_ns else time.monotonic()))
        kbps = self.bytes_sent * 8 / max(0.001, time.time() - self.started_at) / 1000.0
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
            "kbps": round(kbps, 1),
            "gain": self.gain,
            "running": self._thread is not None and self._thread.is_alive(),
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
