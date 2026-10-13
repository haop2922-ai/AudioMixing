"""WaveLink wire protocol.

Data plane: 28-byte header + payload over UDP (unicast).
Control plane packets (HELLO/PING/ROUTE/...) reuse the same header with a JSON
payload, so a single demultiplexer handles everything.

Everything here is pure stdlib and unit-testable without any audio device.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any

MAGIC = b"WL"
VERSION = 1

# --- packet types ---------------------------------------------------------
T_AUDIO = 1
T_HELLO = 2
T_HELLO_ACK = 3
T_PING = 4
T_PONG = 5
T_BYE = 6
T_STATS = 7
T_ROUTE = 8
T_KEEPALIVE = 9

TYPE_NAMES = {
    T_AUDIO: "AUDIO",
    T_HELLO: "HELLO",
    T_HELLO_ACK: "HELLO_ACK",
    T_PING: "PING",
    T_PONG: "PONG",
    T_BYE: "BYE",
    T_STATS: "STATS",
    T_ROUTE: "ROUTE",
    T_KEEPALIVE: "KEEPALIVE",
}

# --- codecs ---------------------------------------------------------------
CODEC_PCM_F32 = 1
CODEC_PCM_S16 = 2
CODEC_OPUS = 3
CODEC_NAMES = {
    CODEC_PCM_F32: "pcm_f32",
    CODEC_PCM_S16: "pcm_s16",
    CODEC_OPUS: "opus",
}
CODEC_BY_NAME = {v: k for k, v in CODEC_NAMES.items()}

FLAG_DISCONTINUITY = 0x01
FLAG_LAST = 0x02
FLAG_SILENCE = 0x04

HEADER_FIELDS = (
    ("magic", "H"),  # 0x574C == "WL"
    ("version", "B"),
    ("ptype", "B"),
    ("flags", "B"),
    ("codec", "B"),
    ("seq", "H"),
    ("ssrc", "I"),
    # ★ samples ＝【本包帧数】（每帧 2 声道），不是"从开始发送算起的累计帧数" ★
    # 语音/音乐的接收端要按它做准入与对齐（手机端就是：samples > 每包帧数 直接判坏包）。
    # 时间信息由 ts_ns 承担；累计偏移没有任何接收方需要它。
    # 2026-10-14 的教训：这里曾经填成累计偏移（480 × seq），Python 接收端不读这个字段，
    # 所以电脑↔电脑完全没有症状；手机端严格校验它，于是电脑发出的每一个音频包都被拒收。
    ("samples", "I"),
    ("ts_ns", "Q"),
    ("gain_q14", "h"),
    ("payload_len", "H"),
)
HEADER = struct.Struct("<" + "".join(fmt for _name, fmt in HEADER_FIELDS))
HEADER_SIZE = HEADER.size  # 28
MAGIC_U16 = 0x574C
MASK64 = 0xFFFFFFFFFFFFFFFF
# The header layout is data that must never drift silently, so check it here.
assert HEADER_SIZE == 28, HEADER_SIZE
assert HEADER.format == "<HBBBBHIIQhH", HEADER.format

SAMPLE_RATE = 48000
CHANNELS = 2

# Default ports.
PORT_DISCOVERY = 47830
PORT_AUDIO = 47831
PORT_WEB = 17800
DISCOVERY_GROUP = "239.255.77.120"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_TYPE = "_wavelink._udp.local."


def now_ns() -> int:
    """Wall-clock nanoseconds since the Unix epoch (used for cross-node sync)."""
    return time.time_ns()


def mono_ns() -> int:
    """Monotonic nanoseconds, for local scheduling only."""
    return time.monotonic_ns()


@dataclass
class Packet:
    type: int
    payload: bytes = b""
    codec: int = CODEC_PCM_F32
    flags: int = 0
    seq: int = 0
    ssrc: int = 0
    samples: int = 0
    ts_ns: int = 0
    gain_q14: int = 10000

    # ---- encoding --------------------------------------------------------
    def encode(self) -> bytes:
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload too large for one datagram: %d" % len(self.payload))
        if not 0 <= self.ts_ns < (1 << 63):
            raise ValueError("ts_ns out of range for the wire format: %d" % self.ts_ns)
        return HEADER.pack(
            MAGIC_U16,
            VERSION,
            self.type & 0xFF,
            self.flags & 0xFF,
            self.codec & 0xFF,
            self.seq & 0xFFFF,
            self.ssrc & 0xFFFFFFFF,
            self.samples & 0xFFFFFFFF,
            self.ts_ns & MASK64,
            self.gain_q14,
            len(self.payload),
        ) + self.payload

    # ---- JSON helpers ----------------------------------------------------
    def json(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        try:
            obj = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def control(
        ptype: int,
        body: dict[str, Any] | None = None,
        *,
        ts_ns: int | None = None,
        seq: int = 0,
        ssrc: int = 0,
    ) -> "Packet":
        raw = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return Packet(
            type=ptype,
            payload=raw,
            codec=0,
            ts_ns=now_ns() if ts_ns is None else ts_ns,
            seq=seq,
            ssrc=ssrc,
        )

    @property
    def gain(self) -> float:
        return self.gain_q14 / 10000.0


def decode(buf: bytes) -> Packet | None:
    """Decode one datagram; returns None when it is not a valid WaveLink packet."""
    if len(buf) < HEADER_SIZE:
        return None
    magic, version, ptype, flags, codec, seq, ssrc, samples, ts_ns, gain, plen = HEADER.unpack_from(buf, 0)
    if magic != MAGIC_U16 or version != VERSION:
        return None
    if HEADER_SIZE + plen > len(buf):
        return None
    return Packet(
        type=ptype,
        payload=bytes(buf[HEADER_SIZE : HEADER_SIZE + plen]),
        codec=codec,
        flags=flags,
        seq=seq,
        ssrc=ssrc,
        samples=samples,
        ts_ns=ts_ns,
        gain_q14=gain,
    )


# --------------------------------------------------------------------------
# Payload codecs (PCM only; Opus lives in audio/opus.py to keep stdlib purity)
# --------------------------------------------------------------------------

def encode_pcm_f32(samples) -> bytes:
    return struct.pack("<%df" % len(samples), *samples)


def decode_pcm_f32(payload: bytes):
    n = len(payload) // 4
    return list(struct.unpack_from("<%df" % n, payload, 0))


def encode_pcm_s16(samples) -> bytes:
    import array

    a = array.array("h", (max(-32768, min(32767, int(round(s * 32767.0)))) for s in samples))
    return a.tobytes()


def decode_pcm_s16(payload: bytes):
    import array

    a = array.array("h")
    a.frombytes(payload[: len(payload) // 2 * 2])
    return [v / 32768.0 for v in a]


def encode_payload(codec: int, samples) -> bytes:
    if codec == CODEC_PCM_S16:
        return encode_pcm_s16(samples)
    if codec == CODEC_PCM_F32:
        return encode_pcm_f32(samples)
    raise ValueError("encode_payload does not handle codec %r" % (codec,))


def decode_payload(codec: int, payload: bytes, channels: int = CHANNELS):
    if codec == CODEC_PCM_S16:
        return decode_pcm_s16(payload)
    if codec == CODEC_PCM_F32:
        return decode_pcm_f32(payload)
    raise ValueError("decode_payload does not handle codec %r" % (codec,))


# --------------------------------------------------------------------------
# Clock synchronisation (NTP-style four-timestamp exchange)
# --------------------------------------------------------------------------

def ping_body(t0: int) -> dict[str, int]:
    return {"t0": t0}


def pong_body(ping: Packet, recv_ns: int, send_ns: int) -> dict[str, int]:
    body = ping.json()
    return {"t0": int(body.get("t0", 0)), "t1": recv_ns, "t2": send_ns}


def clock_sample(pong: Packet, t3: int) -> tuple[float, float, float] | None:
    """Return (offset_ns, rtt_ns, quality) from a PONG.

    offset = ((t1 - t0) + (t2 - t3)) / 2  -> add to a remote timestamp to get the
    local clock. rtt = (t3 - t0) - (t2 - t1). Positive offset means the remote
    clock is ahead of ours.
    """
    b = pong.json()
    try:
        t0 = int(b["t0"])
        t1 = int(b["t1"])
        t2 = int(b["t2"])
    except (KeyError, TypeError, ValueError):
        return None
    if t0 <= 0 or t1 <= 0 or t2 <= 0 or t3 < t0:
        return None
    rtt = (t3 - t0) - (t2 - t1)
    if rtt < 0:
        rtt = 0
    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    return offset, float(rtt), 1.0 / (1.0 + rtt / 1e6)


class ClockSync:
    """Keeps a best-effort estimate of `remote_clock - local_clock`."""

    def __init__(self, name: str = "peer") -> None:
        self.name = name
        self.offset_ns: float = 0.0
        self.rtt_ns: float = 0.0
        self.samples: int = 0
        self._best_rtt: float = float("inf")

    @property
    def ready(self) -> bool:
        return self.samples > 0

    def update(self, pong: Packet, t3: int) -> bool:
        s = clock_sample(pong, t3)
        if s is None:
            return False
        offset, rtt, _ = s
        # Prefer the sample with the smallest RTT: it has the least asymmetric error.
        if rtt <= self._best_rtt or self.samples == 0:
            alpha = 0.5 if self.samples < 4 else 0.15
            self.offset_ns = offset if self.samples == 0 else (1 - alpha) * self.offset_ns + alpha * offset
            self._best_rtt = min(self._best_rtt, rtt)
        self.rtt_ns = rtt
        self.samples += 1
        return True

    def to_local_ns(self, remote_ts_ns: int) -> int:
        """Map a timestamp produced by the remote node onto our clock."""
        return int(remote_ts_ns - self.offset_ns)

    def stats(self) -> dict[str, Any]:
        return {
            "peer": self.name,
            "offset_ms": round(self.offset_ns / 1e6, 3),
            "rtt_ms": round(self.rtt_ns / 1e6, 3),
            "samples": self.samples,
        }


@dataclass
class StreamStats:
    """Per-stream reception counters, surfaced in the web UI."""

    ssrc: int = 0
    packets: int = 0
    lost: int = 0
    late: int = 0
    decoded_samples: int = 0
    buffer_ms: float = 0.0
    target_ms: float = 0.0
    last_seq: int | None = None
    source_node: str = ""
    source_name: str = ""
    codec: str = ""
    started_ns: int = field(default_factory=mono_ns)

    def observe_seq(self, seq: int) -> int:
        """Update loss counters; returns the number of packets considered lost."""
        lost = 0
        if self.last_seq is not None:
            delta = (seq - self.last_seq) & 0xFFFF
            if delta == 0:
                return 0
            if delta > 0x8000:  # reordered/old packet
                return 0
            if delta > 1:
                lost = delta - 1
                self.lost += lost
        self.last_seq = seq
        self.packets += 1
        return lost

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(1e-9, (mono_ns() - self.started_ns) / 1e9)
        return {
            "ssrc": self.ssrc,
            "source_node": self.source_node,
            "source_name": self.source_name,
            "codec": self.codec,
            "packets": self.packets,
            "lost": self.lost,
            "late": self.late,
            "kbps": round(self.decoded_samples * CHANNELS * 4 * 8 / elapsed / 1000.0, 1),
            "buffer_ms": round(self.buffer_ms, 1),
            "target_ms": round(self.target_ms, 1),
        }
