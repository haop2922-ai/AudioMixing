"""Audio Mixing wire protocol.

Data plane: 28-byte header + payload over UDP (unicast).
Control plane packets (HELLO/PING/ROUTE/...) reuse the same header with a JSON
payload, so a single demultiplexer handles everything.

Everything here is pure stdlib and unit-testable without any audio device.
"""

from __future__ import annotations

import json
import struct
import time
import zlib
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
# ★ 2026-11-04（B 第一阶段）：可选扩展段。
#   **包头仍是 28 字节**，只有发了这个位才在包头和负载之间多 8 字节：
#       uint32 origin_id  （原始声音主人的稳定哈希，全程不变）
#       uint8  hop        （每转发一跳 +1，超过上限直接丢）
#       3 字节保留（写 0）
#   为什么不用新版本号：老端 `decode()` 只认 version==1，改版本号会让整条音频
#   链路直接断。扩展段只发给"信标里声明 ext:1"的对端（见 build_beacon / engine），
#   老端永远收不到带扩展段的包，所以既不报错也不拒流。
FLAG_EXT = 0x08
EXT_SIZE = 8
EXT = struct.Struct("<IB3x")
MAX_HOP = 8
# 扩展段里 0 表示"没有 origin 信息"（老端 / 未协商），所以哈希结果不能是 0。
def origin_id_of(value: str) -> int:
    """node_id / device_id → 32 位稳定标识（跨重启、跨两端一致）。"""
    text = str(value or "").strip()
    return (zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF) or 1

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
# ★ 2026-11-01 用户：两端默认发现端口统一为 17830（手机 Proto.DEFAULT_DISCOVERY_PORT 同值）。
#   发现端口是**唯一**需要两端手填一致的端口；音频端口由信标协商，不需要用户管。
PORT_DISCOVERY = 17830
# 旧默认发现端口：升级时把"从没改过"的 47830 搬到 17830（见 Config.load）。
LEGACY_PORT_DISCOVERY = 47830
PORT_AUDIO = 47831
PORT_WEB = 17800
DISCOVERY_GROUP = "239.255.77.120"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
# mDNS/DNS-SD 服务类型。真正的设备互发现走 UDP 信标（见 net/discovery.py），
# 这一条只服务于可选的 mDNS 桥和 `dns-sd -B` 调试 —— 改名不影响发现功能。
SERVICE_TYPE = "_audiomixing._udp.local."


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
    # 扩展段（只有 origin_id 非 0 时才写进包里；见 FLAG_EXT 的说明）。
    origin_id: int = 0
    hop: int = 0

    # ---- encoding --------------------------------------------------------
    def encode(self) -> bytes:
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload too large for one datagram: %d" % len(self.payload))
        if not 0 <= self.ts_ns < (1 << 63):
            raise ValueError("ts_ns out of range for the wire format: %d" % self.ts_ns)
        flags = self.flags & 0xFF
        ext = b""
        if self.origin_id:
            flags |= FLAG_EXT
            ext = EXT.pack(self.origin_id & 0xFFFFFFFF, max(0, int(self.hop)) & 0xFF)
        return HEADER.pack(
            MAGIC_U16,
            VERSION,
            self.type & 0xFF,
            flags,
            self.codec & 0xFF,
            self.seq & 0xFFFF,
            self.ssrc & 0xFFFFFFFF,
            self.samples & 0xFFFFFFFF,
            self.ts_ns & MASK64,
            self.gain_q14,
            len(self.payload),
        ) + ext + self.payload

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
    """Decode one datagram; returns None when it is not a valid Audio Mixing packet."""
    if len(buf) < HEADER_SIZE:
        return None
    magic, version, ptype, flags, codec, seq, ssrc, samples, ts_ns, gain, plen = HEADER.unpack_from(buf, 0)
    if magic != MAGIC_U16 or version != VERSION:
        return None
    offset = HEADER_SIZE
    origin_id = 0
    hop = 0
    if flags & FLAG_EXT:
        if HEADER_SIZE + EXT_SIZE > len(buf):
            return None
        origin_id, hop = EXT.unpack_from(buf, HEADER_SIZE)
        offset += EXT_SIZE
    if offset + plen > len(buf):
        return None
    return Packet(
        type=ptype,
        payload=bytes(buf[offset : offset + plen]),
        codec=codec,
        flags=flags,
        seq=seq,
        ssrc=ssrc,
        samples=samples,
        ts_ns=ts_ns,
        gain_q14=gain,
        origin_id=origin_id,
        hop=hop,
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

    # ★ 2026-11-04（审计 A2）：offset 原来只在"RTT ≤ 历史最小值"的样本上更新，
    #   而 `_best_rtt` 只减不增 —— 采集过几轮之后，后续样本几乎永远不再命中，
    #   offset 就被冻死了。对端墙钟发生校时跳变（NTP / 用户改时间）时，
    #   本机仍按旧 offset 排播放头，表现为整段延迟或持续溢出，直到重启。
    #   现在：① 常规样本允许在 best_rtt 的 1.5 倍窗口内更新（仍偏好低 RTT 样本）；
    #        ② offset 跳变超过 250ms 时视为对端校时，直接重锚并重置 best_rtt。
    RTT_WINDOW = 1.5
    RESYNC_NS = 250_000_000.0
    RESYNC_RTT_GUARD = 3.0

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
        first = self.samples == 0
        jumped = (not first and abs(offset - self.offset_ns) > self.RESYNC_NS
                  and rtt <= max(self._best_rtt * self.RESYNC_RTT_GUARD, 50_000_000.0))
        if first or jumped:
            # 第一次采样，或对端墙钟刚跳了一大截：直接采用，并让 best_rtt 跟着重置，
            # 否则旧的最小 RTT 会把后面正常的样本一直挡在门外。
            self.offset_ns = offset
            self._best_rtt = rtt
        elif rtt <= self._best_rtt * self.RTT_WINDOW:
            # Prefer the sample with the smallest RTT: it has the least asymmetric error.
            alpha = 0.5 if self.samples < 4 else 0.15
            self.offset_ns = (1 - alpha) * self.offset_ns + alpha * offset
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
