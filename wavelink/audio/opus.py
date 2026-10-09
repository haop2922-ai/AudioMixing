"""Opus 编解码。把每路 1536 KB/s 的未压缩音频压到几十 KB/s。

为什么需要它：未压缩立体声 48 kHz 是 1536 KB/s，两台设备就是 3 MB/s，
三台就吃满 Wi-Fi，多一台设备音质就明显变差。Opus 在同等工作点只要 20 KB/s
上下，压缩比接近 10 倍以上，而且音质比降采样好得多。

依赖：libopus 的本地库。构建时随包带上（pyogg 自带 opus.dll），
这里负责把它找出来；找不到就让上层退回未压缩，绝不因此起不来。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

_np: Any = None
try:
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# 48 kHz 立体声是这套程序的统一格式
SAMPLE_RATE = 48000
CHANNELS = 2
# Opus 只接受固定长度的帧：2.5 / 5 / 10 / 20 / 40 / 60 毫秒
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000      # 960
_BITRATE = 128_000                                  # 128 kbps，语音和视频都够

_opus: Any = None
_load_error = ""


def _candidate_dirs() -> list[str]:
    dirs: list[str] = []
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.append(meipass)
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    # pyogg 自带 opus.dll，构建时会被一起打进来
    try:
        import pyogg

        dirs.append(os.path.dirname(os.path.abspath(pyogg.__file__)))
    except Exception:
        pass
    # 源码运行时，顺便找一下 site-packages 下的副本
    for base in sys.path:
        if base and base not in dirs:
            dirs.append(base)
    return [d for d in dirs if d and os.path.isdir(d)]


def _load() -> Any:
    """把 Opus 库加载进来。成功返回模块，失败返回 None 并记下原因。"""
    global _opus, _load_error
    if _opus is not None:
        return _opus
    if _np is None:
        _load_error = "缺少 numpy"
        return None

    # 先让 Windows 的 DLL 搜索能看见这些目录（3.8 之后不再搜 PATH）
    for folder in _candidate_dirs():
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(folder)
        except Exception:
            pass

    try:
        import opuslib_next as opus

        _opus = opus
        return _opus
    except Exception as exc:
        _load_error = "%s: %s" % (type(exc).__name__, exc)

    # 退路：直接把 opus.dll 塞进进程，再用 ctypes 找符号（给没有 opuslib 的环境）
    for folder in _candidate_dirs():
        for name in ("opus.dll", "libopus-0.dll", "libopus.dll"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                try:
                    ctypes.WinDLL(path)          # noqa: S606 - 明确的本地库路径
                    import opuslib_next as opus

                    _opus = opus
                    return _opus
                except Exception as exc:
                    _load_error = "%s: %s" % (type(exc).__name__, exc)
    return None


def available() -> bool:
    """Opus 是否可用。上层据此决定用不用压缩。"""
    return _load() is not None


def load_error() -> str:
    return _load_error


class OpusEncoder:
    """把 float32 交错样本压成 Opus 包。

    帧长必须是 Opus 允许的值。引擎按 10 毫秒或 20 毫秒的块调用，
    两种都支持；其它长度按 20 毫秒补零，保证不会因为长度不对而报错。
    """

    def __init__(self, channels: int = CHANNELS, bitrate: int = _BITRATE) -> None:
        opus = _load()
        if opus is None:
            raise RuntimeError("Opus 不可用：%s" % _load_error)
        self.channels = max(1, min(2, int(channels)))
        self._enc = opus.Encoder(SAMPLE_RATE, self.channels, "audio")
        try:
            self._enc.bitrate = int(bitrate)
        except Exception:
            pass
        self.frames_sent = 0

    def encode(self, samples: Any) -> bytes:
        """samples 是 float32 交错数组，返回一个 Opus 包。"""
        if _np is None:
            raise RuntimeError("缺少 numpy")
        data = _np.asarray(samples, dtype=_np.float32).reshape(-1)
        if self.channels == 2:
            frames = len(data) // 2
        else:
            frames = len(data)
        # 只接受 Opus 的合法帧长；不合适就补零到最近的合法值
        for allowed in (120, 240, 480, 960, 1920, 2880):
            if frames <= allowed:
                frames = allowed
                break
        else:
            frames = 960
        need = frames * self.channels
        if len(data) < need:
            data = _np.concatenate([data, _np.zeros(need - len(data), dtype=_np.float32)])
        elif len(data) > need:
            data = data[:need]
        pcm16 = _np.clip(data, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2").tobytes()
        self.frames_sent += 1
        return self._enc.encode(pcm16, frames)


class OpusDecoder:
    """把 Opus 包还原成 float32 交错样本。"""

    def __init__(self, channels: int = CHANNELS) -> None:
        opus = _load()
        if opus is None:
            raise RuntimeError("Opus 不可用：%s" % _load_error)
        self.channels = max(1, min(2, int(channels)))
        self._dec = opus.Decoder(SAMPLE_RATE, self.channels)
        self.packets = 0

    def decode(self, payload: bytes) -> Any:
        if _np is None or not payload:
            return []
        try:
            pcm = self._dec.decode(bytes(payload), FRAME_SAMPLES)
        except Exception:
            # 坏包不该让整条流断掉，返回空让上层丢弃这一包
            return []
        self.packets += 1
        data = _np.frombuffer(pcm, dtype="<i2").astype(_np.float32) / 32768.0
        # 必须返回普通列表，不能返回 numpy 数组：接收端用 `if not samples` 判空，
        # 而对数组做真值判断会抛 ValueError，整包被当成坏包丢掉 —— 表现就是
        # "用 Opus 时完全没声音"，而单元测试里直接调解码器是看不出这个问题的。
        return data.tolist()
