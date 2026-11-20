"""三档「性能预算」：流畅 / 均衡 / 高音质。

用户的原话（2026-11-18）把这个功能钉死在"性能预算"上，不是"延迟换音质"：

* **流畅**：让老机器/低配机器跑得动 —— 降采样率或编码复杂度、减缓冲与线程开销，
  宁可音质一般，也要 CPU 占用低、不容易卡。不是单纯为压延迟。
* **均衡**：默认档，等同于 2.42.2 及以前的行为，绝大多数机器用这个。
* **高音质**：主动吃性能换音质 —— 提码率、提高编码复杂度与重采样质量、加大缓冲，
  允许 CPU 占用上去。

三档本质是"同一条链路给多少性能预算"，延迟只是副作用之一。两端的档位各自可设，
不一致时**按较低那档协商生效**（谁的性能预算低就听谁的），协商结果进信标。

这里只放数据与纯函数，不碰音频设备，方便单测。

实测基准（本机 2026-11-18，48k 立体声 44.1k→48k 重采样，单核百分比；
抖动缓冲的「起点」就是稳态延迟量级）::

    档位    重采样 CPU     抖动缓冲起点   缓冲区间
    流畅    ≈ 8%          40 ms         20–90 ms
    均衡    ≈ 18%         55 ms         25–150 ms
    高音质  ≈ 28%         80 ms         40–200 ms

Opus 码率/复杂度只在 codec=opus 时生效；PCM16 是无损的，码率由采样格式决定，
所以三档在 PCM 下主要差在帧长（包数）、抖动缓冲与重采样质量。
"""

from __future__ import annotations

from typing import Any

#: 默认档。老配置里没有这个键 → 均衡（= 2.42.2 行为，不迁移也不改变听感）。
DEFAULT = "balanced"

#: 档位顺序（0 最低预算）。协商取较小值就靠这个顺序。
ORDER = ("smooth", "balanced", "high")

#: 每个档位的真实旋钮。所有值都接到现有实现上，没有"只写在面板上"的装饰参数。
PRESETS: dict[str, dict[str, Any]] = {
    # 老机器：20ms 一包（包数减半）+ 更便宜的编码/重采样 + 更浅的抖动缓冲。
    "smooth": {
        "label": "流畅",
        "rank": 0,
        "hint": "老机器/低配机器选这个：CPU 占用最低，偶尔网络抖动时宁可音质一般也不卡。",
        "frames_per_packet": 960,      # 20ms @48k，包数减半
        "jitter_start_ms": 40.0,
        "jitter_min_ms": 20.0,
        "jitter_max_ms": 90.0,
        "opus_bitrate": 64000,
        "opus_complexity": 2,
        "resample_zero_crossings": 8,  # 重采样抽头更少 = CPU 更低
    },
    # 默认：与 2.42.2 完全一致（10ms 一包、55/25/150、128k、复杂度 6、10 过零）。
    "balanced": {
        "label": "均衡",
        "rank": 1,
        "hint": "默认档，大多数机器用这个：等同 2.42.2 之前的行为。",
        "frames_per_packet": 480,
        "jitter_start_ms": 55.0,
        "jitter_min_ms": 25.0,
        "jitter_max_ms": 150.0,
        "opus_bitrate": 128000,
        "opus_complexity": 6,
        "resample_zero_crossings": 16,  # = 2.42.2 的默认值（均衡必须等同于旧行为）
    },
    # 机器够好：更高码率与编码复杂度、更贵的重采样、更深的缓冲换稳定与音质。
    "high": {
        "label": "高音质",
        "rank": 2,
        "hint": "机器够好选这个：主动吃性能换音质，CPU 占用会上去。",
        "frames_per_packet": 480,
        "jitter_start_ms": 80.0,
        "jitter_min_ms": 40.0,
        "jitter_max_ms": 200.0,
        "opus_bitrate": 192000,
        "opus_complexity": 10,
        "resample_zero_crossings": 24,
    },
}


def normalize(value: Any) -> str:
    """任何输入都收敛到一个合法档位名；认不出 = 均衡（老配置/坏配置不改变现有听感）。"""
    text = str(value or "").strip().lower()
    return text if text in PRESETS else DEFAULT


def profile(name: Any) -> dict[str, Any]:
    """取某一档的完整参数（返回副本，调用方随便改）。"""
    return dict(PRESETS[normalize(name)])


def rank(name: Any) -> int:
    return int(PRESETS[normalize(name)]["rank"])


def negotiate(local: Any, remote: Any) -> str:
    """两端协商：按**较低**那档生效。

    `remote` 为空/未知（老版本信标里没有这个键）时按"它等同于均衡"处理 ——
    老版本的听感就是均衡，这样既不会把新端无谓地降档，也不会假装对方支持高音质。
    """
    a = normalize(local)
    b = normalize(remote) if remote not in (None, "") else DEFAULT
    return a if rank(a) <= rank(b) else b


def label(name: Any) -> str:
    return str(PRESETS[normalize(name)]["label"])


def describe(name: Any, effective: str | None = None) -> str:
    """面板上的一行人话（含协商结果）。"""
    name = normalize(name)
    p = PRESETS[name]
    text = "%s（%s）" % (p["label"], p["hint"])
    if effective and normalize(effective) != name:
        text += " 本次协商生效：%s。" % label(effective)
    return text


def params(name: Any) -> dict[str, Any]:
    """给诊断/测试用的扁平参数表（数值原样，不做四舍五入）。"""
    p = profile(name)
    return {
        "preset": normalize(name),
        "label": p["label"],
        "rank": p["rank"],
        "frames_per_packet": p["frames_per_packet"],
        "jitter_start_ms": p["jitter_start_ms"],
        "jitter_min_ms": p["jitter_min_ms"],
        "jitter_max_ms": p["jitter_max_ms"],
        "opus_bitrate": p["opus_bitrate"],
        "opus_complexity": p["opus_complexity"],
        "resample_zero_crossings": p["resample_zero_crossings"],
    }
