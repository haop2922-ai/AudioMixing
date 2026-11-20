"""按进程（按 App）回环采集 —— Windows 10 2004 起支持。

为什么要单独一路：桌面回环拿到的是**所有 App 混在一起**的声音，用户想把
BILIBILI 的声音只送到某个耳机、其它耳机继续听系统声音时，混音里拆不出来。
WASAPI 提供了 process loopback：指定一个 PID（含它的子进程树），只录这个
进程正在播放的声音。

这一层只负责"采"，不负责混音；每路输出用哪一路采集，由 MultiOut 决定。
打不开时抛 OSError，文字直接可以给用户看（MultiOut 会据此回落到桌面混音）。
"""

from __future__ import annotations

import ctypes
import threading
import time
from array import array
from typing import Any

from ..format import CHANNELS, SAMPLE_RATE, Resampler
from . import wasapi_com as wc
from .wasapi import (
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
    AUDCLNT_BUFFERFLAGS_SILENT,
    AUDCLNT_SHAREMODE_SHARED,
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
    AUDCLNT_STREAMFLAGS_LOOPBACK,
    AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
    _decode_device_bytes,
    _ensure_com,
)


_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
_K32.CreateEventW.restype = ctypes.c_void_p
_K32.CloseHandle.argtypes = [ctypes.c_void_p]


class ProcessLoopbackSource:
    """只录指定进程（及其子进程树）正在播放的声音。"""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.pid = int(spec.get("pid") or 0)
        self.app_name = str(spec.get("name") or "").strip()
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.name = self.app_name or ("App #%d" % self.pid)
        self._stop = threading.Event()
        self._client: wc.IAudioClient | None = None
        self._capture: wc.IAudioCaptureClient | None = None
        self._event = None
        self._info: dict = {}
        self._resampler: Resampler | None = None
        self.frames_captured = 0
        self.glitches = 0

    def start(self) -> None:
        if self.pid <= 0:
            raise OSError("没有指定要采集的 App")
        ok, reason = wc.process_loopback_available()
        if not ok:
            raise OSError(reason)
        _ensure_com()
        try:
            self._client = wc.activate_process_loopback(self.pid, include_tree=True)
        except OSError as exc:
            raise OSError("按 App 采音打不开（这个 App 可能受保护或正在用独占模式）：%s" % exc)
        # 官方示例：事件回调 + 自动转换 PCM + 普通 WAVEFORMATEX，缓冲区时长为 0。
        # 少了 AUTOCONVERTPCM 也能 Initialize，但读出来会一直是静音。
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                 | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        self._event = _K32.CreateEventW(None, 0, 0, None)
        if not self._event:
            raise OSError("按 App 采音：创建采集事件失败")
        tried: list[str] = []
        for rate in (SAMPLE_RATE, 44100):
            raw_fmt, info = wc.make_waveformatex(rate, CHANNELS, 16)
            fmt_buf = ctypes.create_string_buffer(raw_fmt, len(raw_fmt))
            try:
                self._client.initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    flags,
                    0,
                    0,
                    ctypes.addressof(fmt_buf),
                )
                self._info = info
                if rate != SAMPLE_RATE:
                    self._resampler = Resampler(rate, SAMPLE_RATE, CHANNELS)
                break
            except OSError as exc:
                tried.append(str(exc))
        else:
            self.stop()
            raise OSError("按 App 采音初始化失败：%s" % ("；".join(tried) or "格式不被支持"))
        try:
            self._client.set_event_handle(self._event)
            self._capture = wc.IAudioCaptureClient.from_ptr(
                self._client.get_service(wc.IID_IAudioCaptureClient)
            )
            self._client.start()
        except OSError as exc:
            self.stop()
            raise OSError("按 App 采音启动失败：%s" % exc)

    def read(self):
        """把当前能拿到的数据都取出来，返回 48k 双声道 float32。"""
        if self._stop.is_set() or self._capture is None or self._client is None:
            return array("f")
        out = array("f")
        idle = 0
        while not self._stop.is_set():
            try:
                available = self._capture.get_next_packet_size()
            except OSError:
                break
            if available == 0:
                if out:
                    break
                idle += 1
                if idle > 4:
                    break
                time.sleep(0.002)
                continue
            idle = 0
            try:
                data_ptr, frames, flags, _dev_pos, _qpc = self._capture.get_buffer()
            except OSError:
                break
            try:
                if frames:
                    if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                        self.glitches += 1
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr:
                        block = array("f", bytes(4 * frames * CHANNELS))
                    else:
                        size = frames * self._info.get("channels", CHANNELS) * \
                            max(1, self._info.get("bits", 32) // 8)
                        raw = ctypes.string_at(data_ptr, size)
                        block = _decode_device_bytes(raw, frames, self._info)
                    if self._resampler is not None:
                        block = self._resampler.process(block)
                    out.extend(block)
                    self.frames_captured += len(block) // CHANNELS
            finally:
                try:
                    self._capture.release_buffer(frames)
                except OSError:
                    pass
        return out

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                pass
            self._capture = None
        if self._client is not None:
            try:
                self._client.release()
            except Exception:
                pass
            self._client = None
        if self._event:
            try:
                _K32.CloseHandle(self._event)
            except Exception:
                pass
            self._event = None

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "wasapi",
            "device": "app:%d" % self.pid,
            "name": self.name,
            "kind": "app-loopback",
            "app_pid": self.pid,
            "app_name": self.app_name,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames_captured": self.frames_captured,
            "glitches": self.glitches,
        }
