"""一台电脑同时把声音送到多个耳机/音箱。

为什么这么做
    需求是"两个人戴两个耳机打双人游戏，两边都要有声音"。Windows 一次只能选
    一个默认输出，系统自带的"侦听此设备"能凑合，但要手动配、延迟大、三个以上
    设备很难办。

结构（复用现成的两块）
    CaptureHub  一份采集，给每个调用者一个独立读取器 —— 一块声卡只被打开一次，
                所以不会出现"两个程序抢声卡"的问题
    LocalLoop   一路采集到一个播放端，自带增益、限幅、去直流

    于是：一份采集 → N 个读取器 → N 个播放端 ✓ 每个设备独立音量 ✓
    将来还能给每个设备加延迟补偿（蓝牙比有线慢几十到几百毫秒，双人游戏要的是同步）。

对现有链路零改动：不碰点对点、不碰广播、不碰本机直通。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import base as audio_base
from .capture import CaptureHub
from .local import LocalLoop

# 每个设备的默认音量
DEFAULT_GAIN = 1.0



def _sink_spec(selector: str) -> dict[str, Any]:
    """按设备名挑后端。

    真实输出设备走 wasapi；以 .wav/.raw/.pcm 结尾的当成文件（测试和录音用）。
    写死 wasapi 的后果是：拿文件当播放端时一路都不出声，而且不报错。
    """
    low = selector.strip().lower()
    if low.endswith((".wav", ".raw", ".pcm")):
        return {"backend": "file", "device": selector}
    return {"backend": "wasapi", "device": selector}

class _Device:
    """一个输出设备对应的一路播放。"""

    def __init__(self, name: str, selector: str, gain: float, delay_ms: float) -> None:
        self.name = name
        self.selector = selector
        self.gain = float(gain)
        self.delay_ms = max(0.0, float(delay_ms))
        self.loop: LocalLoop | None = None
        self.error = ""
        self.samples = 0

    def describe(self) -> dict[str, Any]:
        loop = self.loop
        # 播放端自己的错误也要带出来：它藏在 loop.error 里，不读就永远显示"无错误"，
        # 而现场表现是"这一路一个字都没写"。
        detail = ""
        frames = 0
        if loop is not None:
            detail = str(getattr(loop, "error", "") or "")
            frames = int(getattr(loop, "frames", 0) or 0)
        return {
            "name": self.name,
            "device": self.selector,
            "gain": round(self.gain, 3),
            "delay_ms": round(self.delay_ms, 1),
            "running": bool(loop is not None and loop.is_alive()),
            "frames": frames,
            "error": detail or self.error,
        }


class MultiOut:
    """把本机声音同时送到多个输出设备。

    用法：
        out = MultiOut(hub)                    # hub 是引擎里的 CaptureHub
        out.configure(capture_spec, devices)   # 设备列表见下
        ...
        out.stop()

    devices 每项形如 {"device": "<输出设备名>", "gain": 1.0, "delay_ms": 80.0}
    device 可以填设备名，也可以填 "@default"。
    """

    def __init__(self, hub: CaptureHub) -> None:
        self.hub = hub
        self._lock = threading.RLock()
        self._devices: dict[str, _Device] = {}
        self._key: tuple | None = None
        self._note = "未开启"

    # ---- 对外 ----
    def configure(self, capture_spec: dict[str, Any] | None, devices: list[dict[str, Any]]) -> None:
        """按当前配置起停各路播放。设备和音源没变就不动，避免反复开关设备。"""
        with self._lock:
            wanted = [d for d in (devices or []) if str(d.get("device") or "").strip()]
            spec = dict(capture_spec or {})
            key = (repr(sorted((str(d.get("device")), float(d.get("gain", 1.0)),
                                float(d.get("delay_ms", 0.0))) for d in wanted)),
                   spec.get("backend"), spec.get("device"), spec.get("loopback"))
            if key == self._key:
                return
            self._key = key
            self._stop_all()
            if not wanted:
                self._note = "没有选择输出设备"
                return
            rate = 48000
            opened: list[str] = []
            for item in wanted:
                selector = str(item.get("device") or "").strip()
                dev = _Device(selector, selector, float(item.get("gain", DEFAULT_GAIN)),
                              float(item.get("delay_ms", 0.0)))
                try:
                    sink = audio_base.open_sink(_sink_spec(selector))
                    sink.start()
                    reader = self.hub.reader(spec)
                    loop = LocalLoop(reader, sink, gain=dev.gain, name="多设备输出-%s" % selector)
                    if dev.delay_ms > 0:
                        # 延迟补偿：先喂一段静音再开始，就相当于这一路整体往后错开。
                        loop.pre_roll_ms = dev.delay_ms
                    loop.start()
                    dev.loop = loop
                    self._devices[selector] = dev
                    opened.append(selector)
                except Exception as exc:
                    dev.error = str(exc)[:120]
                    self._devices[selector] = dev
            if opened:
                self._note = "正在送往 %d 个设备：%s" % (len(opened), "、".join(opened))
            else:
                self._note = "一个设备都没打开成功"

    def stop(self) -> None:
        with self._lock:
            self._stop_all()
            self._key = None
            self._note = "已停止"

    def running(self) -> bool:
        with self._lock:
            return any(d.loop is not None and d.loop.is_alive() for d in self._devices.values())

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.running(),
                "note": self._note,
                "devices": [d.describe() for d in self._devices.values()],
            }

    # ---- 内部 ----
    def _stop_all(self) -> None:
        for dev in self._devices.values():
            if dev.loop is not None:
                try:
                    dev.loop.stop()
                except Exception:
                    pass
                dev.loop = None
        self._devices = {}

    def available_devices(self) -> list[str]:
        """这台机器上可以作为输出的设备名（跳过虚拟声卡）。"""
        out: list[str] = []
        try:
            for item in audio_base.list_all_devices():
                if getattr(item, "kind", "") != audio_base.KIND_SINK:
                    continue
                # 只列真实输出设备：文件后端和空后端也会报一个"设备名"，
                # 但它们不是耳机/音箱，列出来只会让人误选。
                if str(getattr(item, "backend", "")) in ("file", "null"):
                    continue
                name = str(getattr(item, "name", "") or "")
                if name and name not in out:
                    out.append(name)
        except Exception:
            pass
        return out
