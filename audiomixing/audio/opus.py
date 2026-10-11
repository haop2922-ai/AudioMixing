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


# HWHHYN 用 DEEPSEEK 制作
def _candidate_dirs() -> list[str]:
    """所有可能藏着 opus.dll 的目录。

    三种运行方式都要能找得到，缺一个就会出现"源码好好的、打包版报
    Could not find Opus library"：

        源码运行      __file__ 就是磁盘上的真实路径 ✓
        PyInstaller   _MEIPASS 指向解压目录 ✓
        Nuitka        __file__ 指向【源码路径】（磁盘上可能已经不存在），
                      真实位置在 __compiled__.containing_dir；
                      而且它不设 sys.frozen / sys._MEIPASS，
                      所以老代码里那几个分支全都命中不了 —— 2026-10-11 踩过。
    """
    dirs: list[str] = []

    def push(path: Any) -> None:
        if not path:
            return
        p = os.path.abspath(str(path))
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)

    # 1) 本文件所在目录（源码 / onedir 布局）
    push(os.path.dirname(os.path.abspath(__file__)))

    # 2) Nuitka：编译后的模块位置
    comp = globals().get("__compiled__")
    if comp is not None:
        for attr in ("containing_dir_absolute", "containing_dir", "main_dir"):
            try:
                push(getattr(comp, attr, None))
            except Exception:
                pass

    # 3) PyInstaller：解压目录
    push(getattr(sys, "_MEIPASS", ""))

    # 4) 可执行文件所在目录（onedir / 手动把 dll 放旁边的情况）
    push(os.path.dirname(os.path.abspath(sys.executable)))
    try:
        push(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass

    # 5) 依赖包自己的目录：pyogg 自带 opus.dll，opuslib_next 自己不带
    for mod in ("pyogg", "opuslib_next"):
        try:
            m = __import__(mod)
            d = os.path.dirname(os.path.abspath(getattr(m, "__file__", "") or ""))
            push(d)
            push(os.path.join(d, mod))      # .../site-packages/pyogg
        except Exception:
            pass

    # 6) 兜底：在已知根目录的【下一层】里找带 opus*.dll 的子目录
    #    （打包器常把包数据放在 <root>/pyogg/ 这种位置）
    for root in list(dirs):
        try:
            for name in os.listdir(root):
                sub = os.path.join(root, name)
                if not os.path.isdir(sub):
                    continue
                try:
                    if any(f.lower().startswith("opus") and f.lower().endswith(".dll")
                           for f in os.listdir(sub)):
                        push(sub)
                except Exception:
                    pass
        except Exception:
            pass

    return dirs


def _load() -> Any:
    """把 Opus 库加载进来。成功返回模块，失败返回 None 并记下原因。"""
    global _opus, _load_error
    if _opus is not None:
        return _opus
    if _np is None:
        _load_error = "缺少 numpy"
        return None

    # 先让 Windows 的 DLL 搜索能看见这些目录（3.8 之后不再搜 PATH）
    dirs = _candidate_dirs()
    for folder in dirs:
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(folder)
        except Exception:
            pass

    # ★ 关键：opuslib_next 内部用 ctypes.util.find_library("opus") 找库，
    #   而 find_library 在 Windows 上【只沿 PATH 找】，add_dll_directory 对它
    #   完全无效（2026-10-11 实测：加完 add_dll_directory 仍然返回 None，
    #   把目录塞进 PATH 立刻就能返回完整路径）。
    #   所以这里必须动 PATH，否则打包版永远报
    #       Could not find Opus library. Make sure it is installed.
    try:
        path_now = os.environ.get("PATH", "")
        extra = [d for d in dirs if d and d not in path_now.split(os.pathsep)]
        if extra:
            os.environ["PATH"] = os.pathsep.join(extra + [path_now]) if path_now else os.pathsep.join(extra)
    except Exception:
        pass

    # 再把 dll 直接预加载进进程（双保险：某些环境下 find_library 会直接
    # 尝试 CDLL("opus.dll")，名字已在进程里就一定能中）
    for folder in dirs:
        for name in ("opus.dll", "libopus-0.dll", "libopus.dll"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                try:
                    ctypes.WinDLL(path)          # noqa: S606 - 明确的本地库路径
                except Exception:
                    pass
                break

    try:
        import opuslib_next as opus

        _opus = opus
        return _opus
    except Exception as exc:
        _load_error = "%s: %s" % (type(exc).__name__, exc)

    # 退路：给没有 opuslib 的环境（用 ctypes 直接找符号的场景）
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
