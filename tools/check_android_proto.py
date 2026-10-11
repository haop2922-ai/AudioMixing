"""Android(Java) 与电脑(Python) 的协议一致性校验。

两台设备要能互通，28 字节包头必须逐字节一致 —— 这种事最容易"看起来一样、
实际差一个字节"。所以不靠肉眼看代码，靠对字节：

    python tools/check_android_proto.py            # 打印 Python 端编出来的样例包
    python tools/check_android_proto.py <hex>      # 解开一段 hex，和 Python 端比对

用法（配合 Java 端的自检）：
    java -cp out com.audiomixing.app.ProtoTest     # 它会打印几个样例包的 hex
    把 hex 粘到上面的命令里，就能确认两边编出来的是同一串字节。
"""

from __future__ import annotations

import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audiomixing.proto import (  # noqa: E402
    CODEC_PCM_S16,
    HEADER,
    HEADER_SIZE,
    MAGIC_U16,
    T_AUDIO,
    T_BYE,
    T_PING,
    VERSION,
    Packet,
)


def pack(pkt: Packet) -> bytes:
    """按 Python 端的定义编一个包（和 Packet.encode 同源，这里显式写出来便于比对）。"""
    return HEADER.pack(
        MAGIC_U16, VERSION, pkt.type, pkt.flags, pkt.codec, pkt.seq, pkt.ssrc,
        pkt.samples, pkt.ts_ns, pkt.gain_q14, len(pkt.payload),
    ) + pkt.payload


def unpack(raw: bytes) -> dict:
    if len(raw) < HEADER_SIZE:
        raise ValueError("比 28 字节还短")
    fields = HEADER.unpack_from(raw, 0)
    names = [n for n, _ in Packet.__dataclass_fields__.items()] if False else None
    magic, version, ptype, flags, codec, seq, ssrc, samples, ts_ns, gain, plen = fields
    return {
        "magic": hex(magic), "version": version, "type": ptype, "flags": flags,
        "codec": codec, "seq": seq, "ssrc": ssrc, "samples": samples,
        "ts_ns": ts_ns, "gain_q14": gain, "payload_len": plen,
        "payload_first4": raw[HEADER_SIZE:HEADER_SIZE + 4].hex(),
        "total": len(raw),
    }


def samples() -> list[tuple[str, Packet]]:
    """几个有代表性的包：普通音频包 / 空负载 / seq 回绕 / PING。"""
    return [
        ("普通音频包 480 帧", Packet(type=T_AUDIO, codec=CODEC_PCM_S16, seq=1,
                                     ssrc=0x11223344, samples=480, ts_ns=1_700_000_000_000_000_000,
                                     gain_q14=16384,
                                     payload=bytes(range(128)))),
        ("空负载", Packet(type=T_AUDIO, codec=CODEC_PCM_S16, seq=2, ssrc=1,
                           samples=0, ts_ns=1234567890, gain_q14=16384, payload=b"")),
        ("seq 回绕 65535", Packet(type=T_AUDIO, codec=CODEC_PCM_S16, seq=65535, ssrc=2,
                                  samples=480, ts_ns=1, gain_q14=8192, payload=b"\x01\x02")),
        ("PING 控制包", Packet(type=T_PING, codec=CODEC_PCM_S16, seq=0, ssrc=3,
                               samples=0, ts_ns=99, gain_q14=16384, payload=b'{"t":1}')),
        ("BYE 控制包", Packet(type=T_BYE, codec=CODEC_PCM_S16, seq=0, ssrc=4,
                              samples=0, ts_ns=0, gain_q14=0, payload=b"")),
    ]


def main() -> int:
    if len(sys.argv) > 1:
        raw = bytes.fromhex(sys.argv[1].strip().replace(" ", "").replace("\n", ""))
        print("== 解开 %d 字节 ==" % len(raw))
        info = unpack(raw)
        for k, v in info.items():
            print("   %-16s %s" % (k, v))
        # 和前四个样例逐个比对，找到它是哪一个
        for label, pkt in samples():
            mine = pack(pkt)
            if mine == raw:
                print("   ★ 与 Python 端【逐字节一致】✓  对应样例：%s" % label)
                return 0
        print("   ✗ 和 Python 端的任何样例都不一致 —— 说明两边包头定义有差异")
        print("     离它最近的样例对比：")
        for label, pkt in samples():
            mine = pack(pkt)
            if len(mine) == len(raw):
                diffs = [i for i in range(len(raw)) if raw[i] != mine[i]]
                print("        %-18s 差异字节位置: %s" % (label, diffs[:8]))
        return 1

    print("== Python 端编出来的样例包（把每行 hex 拿去和 Java 端输出比对）==")
    for label, pkt in samples():
        raw = pack(pkt)
        print("   %-18s %3d 字节  %s" % (label, len(raw), raw.hex()))
    print()
    print("   包头长度: %d 字节（必须是 28）" % HEADER_SIZE)
    print("   struct 格式: %s" % HEADER.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
