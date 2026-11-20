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

对现有链路零改动：不碰点对点、不碰广播、不碰本机广播监听。
"""

from __future__ import annotations

import threading
import time
from array import array
from typing import Any

from . import base as audio_base
from .capture import CaptureHub, MixReader
from .format import CHANNELS, SAMPLE_RATE
from .local import LocalLoop

# 每个设备的默认音量
DEFAULT_GAIN = 1.0

# ★ 2.42.1：一台输出设备最多同时接几条音源。
#   每条音源都是一次独立采集（CaptureHub 里有自己的读取器），上限是防止
#   有人把整个 App 列表全连到一台设备上把采集线程拖垮。
MAX_SOURCES_PER_DEVICE = 8


def normalize_sources(item: dict[str, Any], legacy_desktop: bool = True) -> list[dict[str, Any]]:
    """把一条设备配置归一成 ``sources`` 列表。

    ★ 2.42.1 用户语义：
      · 一个输出设备可以同时接多个 App 音源，连几个就混几个；
      · **一条线都没有时该设备不出声**，绝不能回落到桌面混音；
      · 「桌面混音」只是用户显式添加的一条音源，不再是失败兜底。

    兼容两种旧写法：
      · 显式 ``sources: []`` —— 用户就是没连音源，返回空列表（静音）；
      · 老配置只有 ``app_pid`` / 什么都没有 —— ``app_pid>0`` 归成一条 App 音源，
        否则（``legacy_desktop=True``）归成一条「桌面混音」音源，避免升级后
        老的多耳机配置突然全静音。UI 新写的配置永远带 ``sources``。
    """
    raw = item.get("sources")
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "").strip().lower()
            if kind == "desktop":
                key = ("desktop", 0)
                if key not in seen:
                    seen.add(key)
                    out.append({"kind": "desktop"})
            elif kind == "app":
                try:
                    pid = int(entry.get("pid") or 0)
                except (TypeError, ValueError):
                    pid = 0
                key = ("app", pid)
                if pid > 0 and key not in seen:
                    seen.add(key)
                    out.append({"kind": "app", "pid": pid,
                                "name": str(entry.get("name") or "")[:80]})
            if len(out) >= MAX_SOURCES_PER_DEVICE:
                break
        return out
    try:
        pid = int(item.get("app_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        return [{"kind": "app", "pid": pid,
                 "name": str(item.get("app_name") or "")[:80]}]
    return [{"kind": "desktop"}] if legacy_desktop else []


def _source_key(spec: dict[str, Any]) -> tuple[str, int]:
    if str(spec.get("kind") or "") == "app":
        return ("app", int(spec.get("pid") or 0))
    return ("desktop", 0)


class _SilenceReader:
    """没有音源时喂给播放端的静音源。

    为什么要它：设备已经打开、用户也勾了，但一条音源都没连 —— 这时必须
    **出声（静音）而不是回落桌面混音**。按 10ms 一块持续供给，播放端永远
    不会因为"读不到数据"而卡住或反复重开。
    """

    def __init__(self, name: str = "（无音源·静音）") -> None:
        self.name = name
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.started = False
        self._stop = threading.Event()
        self._deadline = 0.0

    @property
    def endpoint_name(self) -> str:
        return self.name

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self._stop.clear()
        self._deadline = time.monotonic()

    def read(self):
        if not self.started or self._stop.is_set():
            return None
        frames = int(SAMPLE_RATE * 0.01)
        self._deadline += 0.01
        delay = self._deadline - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.2:
            self._deadline = time.monotonic()
        return array("f", [0.0]) * (frames * CHANNELS)

    def stop(self) -> None:
        self._stop.set()
        self.started = False

    def describe(self) -> dict[str, Any]:
        return {"backend": "silence", "device": self.name, "name": self.name,
                "kind": "silent", "sources": []}



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

    def __init__(self, name: str, selector: str, gain: float, delay_ms: float,
                 source_specs: list[dict[str, Any]] | None = None,
                 source_labels: list[str] | None = None,
                 fallbacks: list[str] | None = None) -> None:
        self.name = name
        self.selector = selector
        self.gain = float(gain)
        self.delay_ms = max(0.0, float(delay_ms))
        self.loop: LocalLoop | None = None
        self.error = ""
        self.samples = 0
        # ★ 2.42.1：这一路自己的音源集合。空列表 = 没有音源 = 静音（不回落桌面）。
        self.source_specs = [dict(s) for s in (source_specs or [])]
        self.source_labels = list(source_labels or [])
        self.fallbacks = list(fallbacks or [])
        # ★ 2026-11-17：这一路的"结构签名"（延迟 / 音源集合）。只有它变了才需要
        #   重开这一路；单纯调音量走 set_gain，不重开、不影响其它路。
        self.signature: tuple = ()
        # 兼容旧接口/旧测试读取的字段：取第一条 App 音源。
        first_app = next((s for s in self.source_specs
                          if str(s.get("kind") or "") == "app"), {})
        self.requested_pid = int(first_app.get("pid") or 0)
        self.requested_name = str(first_app.get("name") or "")
        # 兼容旧字段名。
        self.source_spec = dict(self.source_specs[0]) if self.source_specs else {}
        self.source_label = "、".join(self.source_labels) or "无音源（静音）"
        self.fallback = "；".join(self.fallbacks)

    def set_gain(self, gain: float) -> None:
        """只改这一路的增益：播放线程里做块内平滑过渡，不重开设备。"""
        try:
            value = float(gain)
        except (TypeError, ValueError):
            value = DEFAULT_GAIN
        self.gain = max(0.0, min(4.0, value))
        loop = self.loop
        if loop is not None and hasattr(loop, "set_gain"):
            loop.set_gain(self.gain)

    def describe(self) -> dict[str, Any]:
        loop = self.loop
        # 播放端自己的错误也要带出来：它藏在 loop.error 里，不读就永远显示"无错误"，
        # 而现场表现是"这一路一个字都没写"。
        detail = ""
        frames = 0
        if loop is not None:
            detail = str(getattr(loop, "error", "") or "")
            frames = int(getattr(loop, "frames", 0) or 0)
        sources = []
        for index, spec in enumerate(self.source_specs):
            kind = str(spec.get("kind") or "desktop")
            sources.append({
                "kind": kind,
                "pid": int(spec.get("pid") or 0),
                "name": str(spec.get("name") or ""),
                "label": (self.source_labels[index]
                          if index < len(self.source_labels) else kind),
            })
        if not self.source_specs:
            source_kind = "silent"
        elif len(self.source_specs) > 1:
            source_kind = "mix"
        else:
            source_kind = str(self.source_specs[0].get("kind") or "desktop")
        return {
            "name": self.name,
            "device": self.selector,
            "gain": round(self.gain, 3),
            "delay_ms": round(self.delay_ms, 1),
            "running": bool(loop is not None and loop.is_alive()),
            "frames": frames,
            "error": detail or self.error,
            "source": self.source_label,
            # 这一路**实际**采的是什么：app = 进程回环；desktop = 桌面混音；
            # silent = 没有音源（静音）；mix = 多条音源混音。
            "source_kind": source_kind,
            "source_pid": int((self.source_specs[0] or {}).get("pid") or 0)
            if self.source_specs else 0,
            "sources": sources,
            "source_count": len(sources),
            # 兼容旧字段：第一条 App 音源（没有就是 0 / 空）。
            "app_pid": int(self.requested_pid or 0),
            "app_name": str(self.requested_name or ""),
            # 音源不可用的原因（每条一句）。空 = 全部正常。
            "fallback": "；".join(self.fallbacks),
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
        self._spec_key: tuple | None = None
        self._note = "未开启"

    # ---- 对外 ----
    def configure(self, capture_spec: dict[str, Any] | None, devices: list[dict[str, Any]]) -> None:
        """按当前配置起停各路播放。

        ★ 2026-11-17 用户：「两路同时出声时调单路音量会出高频杂音」。根因是这里
          原来把 gain 也算进整体 key：只要音量变一点，就 `_stop_all()` 把**所有**
          设备一起停掉重开 —— 被调的那一路有断档，另一路也被连带重启，听感就是
          咔哒/沙沙。现在改成**逐路 diff**：
            · 只改音量 → `set_gain()`，设备、格式、缓冲全都不动；
            · 改延迟 / 换采集 App → 只重开这一路；
            · 增删设备 → 只动增删的那一路；
            · 采集源整体变了（桌面 ↔ 麦克风）才全部重来。
          每路可以带自己的 ``app_pid``；按 App 打不开时回落到桌面混音，并把中文
          原因记在 ``fallback`` 里 —— 不静默降级。
        """
        with self._lock:
            wanted = [d for d in (devices or []) if str(d.get("device") or "").strip()]
            spec = dict(capture_spec or {})
            spec_key = (spec.get("backend"), spec.get("device"), bool(spec.get("loopback")))
            if spec_key != self._spec_key:
                # 采集源整体变了（例如桌面回环 ↔ 麦克风）：所有路的采集都得重建。
                self._stop_all()
                self._spec_key = spec_key
            names: list[str] = []
            for item in wanted:
                name = str(item.get("device") or "").strip()
                if not name or name in names:
                    continue
                names.append(name)
                sources = normalize_sources(item, legacy_desktop=True)
                signature = (float(item.get("delay_ms", 0.0) or 0.0),
                             tuple(_source_key(s) for s in sources))
                dev = self._devices.get(name)
                # ★ 2.42.1：只有"结构真的变了"才重开这一路。失败的音源不在这里
                #   每轮重试 —— 调音量会走 ?gain=1 的快速路径，如果失败一次就
                #   每次调音量都重开设备，听感会一直断。用户重新加那条音源即可再试。
                if dev is not None and dev.signature != signature:
                    self._stop_device(name)
                    dev = None
                if dev is None:
                    self._open_device(item, spec, signature, sources)
                else:
                    # 纯音量变化：就地平滑过渡，不重开、不碰另一路。
                    dev.set_gain(item.get("gain", DEFAULT_GAIN))
            for name in list(self._devices):
                if name not in names:
                    self._stop_device(name)
            opened = [n for n in names
                      if n in self._devices and self._devices[n].loop is not None]
            if not names:
                self._note = "没有选择输出设备"
            elif opened:
                self._note = "正在送往 %d 个设备：%s" % (len(opened), "、".join(opened))
            else:
                self._note = "一个设备都没打开成功"

    def _open_device(self, item: dict[str, Any], spec: dict[str, Any],
                     signature: tuple, sources: list[dict[str, Any]]) -> None:
        """只打开这一路（调用方已持有锁）。

        ★ 2.42.1：每路自己带音源集合。
          · 0 条 → 静音源（绝不回落桌面混音）；
          · 1 条 → 单路采集；
          · N 条 → MixReader 混音（连几个混几个）。
          某条 App 打不开时只摘掉那一条，其余音源照常混；全部失败也只是静音。
        """
        selector = str(item.get("device") or "").strip()
        dev = _Device(selector, selector, float(item.get("gain", DEFAULT_GAIN)),
                      float(item.get("delay_ms", 0.0)))
        dev.signature = signature
        try:
            sink = audio_base.open_sink(_sink_spec(selector))
            sink.start()
            reader, kept_specs, labels, fallbacks = self._open_reader(sources, spec)
            dev.source_specs = kept_specs
            dev.source_labels = labels
            dev.fallbacks = fallbacks
            dev.source_label = "、".join(labels) or "无音源（静音）"
            dev.fallback = "；".join(fallbacks)
            first_app = next((s for s in kept_specs
                              if str(s.get("kind") or "") == "app"), {})
            dev.requested_pid = int(first_app.get("pid") or 0)
            dev.requested_name = str(first_app.get("name") or "")
            dev.source_spec = dict(kept_specs[0]) if kept_specs else {}
            loop = LocalLoop(reader, sink, gain=dev.gain, name="多设备输出-%s" % selector)
            if dev.delay_ms > 0:
                # 延迟补偿：先喂一段静音再开始，就相当于这一路整体往后错开。
                loop.pre_roll_ms = dev.delay_ms
            loop.start()
            dev.loop = loop
        except Exception as exc:
            dev.error = str(exc)[:120]
        self._devices[selector] = dev

    def _open_reader(self, sources: list[dict[str, Any]], default_spec: dict[str, Any]
                     ) -> tuple[Any, list[dict[str, Any]], list[str], list[str]]:
        """按音源集合开读取器：逐条预检，失败只摘这一条。"""
        readers: list[Any] = []
        kept: list[dict[str, Any]] = []
        labels: list[str] = []
        fallbacks: list[str] = []
        for source in sources:
            kind = str(source.get("kind") or "desktop")
            if kind != "app":
                spec = dict(default_spec)
                try:
                    readers.append(self.hub.reader(spec))
                    kept.append({"kind": "desktop"})
                    labels.append("桌面混音")
                except Exception as exc:
                    fallbacks.append("桌面混音采集打不开：%s" % exc)
                continue
            pid = int(source.get("pid") or 0)
            if pid <= 0:
                continue
            name = str(source.get("name") or "")
            try:
                from .apps import supported

                ok, reason = supported()
                if not ok:
                    fallbacks.append("「%s」按 App 采音不可用：%s"
                                     % (name or ("PID %d" % pid), reason))
                    continue
            except Exception as exc:
                fallbacks.append("「%s」按 App 采音不可用：%s"
                                 % (name or ("PID %d" % pid), exc))
                continue
            spec = {"backend": "wasapi", "kind": "app", "pid": pid, "name": name}
            try:
                reader = self.hub.reader(spec)
            except Exception as exc:
                fallbacks.append("「%s」采集打不开：%s"
                                 % (name or ("PID %d" % pid), exc))
                continue
            ok, why = self._preflight(reader)
            if not ok:
                try:
                    reader.stop()
                except Exception:
                    pass
                fallbacks.append(why)
                continue
            readers.append(reader)
            kept.append({"kind": "app", "pid": pid, "name": name})
            labels.append("App：%s" % (name or ("PID %d" % pid)))
        if not readers:
            return _SilenceReader(), [], [], fallbacks
        if len(readers) == 1:
            return readers[0], kept, labels, fallbacks
        return (MixReader(readers, name="多设备混音"),
                kept, labels, fallbacks)

    def _preflight(self, reader: Any, timeout: float = 1.5) -> tuple[bool, str]:
        """等采集源真的开门；打不开就带着原因回落（不让用户面对一片静音）。"""
        deadline = time.monotonic() + timeout
        cap = getattr(reader, "capture", None)
        while time.monotonic() < deadline:
            error = str(getattr(cap, "error", "") or "")
            if error:
                return False, "按 App 采音失败：%s" % error
            if int(getattr(cap, "blocks_read", 0) or 0) > 0:
                return True, ""
            time.sleep(0.05)
        # 慢启动不误判：1.5 秒没报错就继续用，真失败下一轮 stats 会显示错误。
        return True, ""

    def stop(self) -> None:
        with self._lock:
            self._stop_all()
            self._spec_key = None
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
    def _stop_device(self, name: str) -> None:
        """只停这一路（调用方已持有锁）。"""
        dev = self._devices.pop(name, None)
        if dev is None or dev.loop is None:
            return
        try:
            dev.loop.stop()
        except Exception:
            pass
        dev.loop = None

    def _stop_all(self) -> None:
        for name in list(self._devices):
            self._stop_device(name)
        self._devices = {}

    def available_devices(self, exclude: list[str] | None = None,
                          devices: list | None = None) -> list[str]:
        """这台机器上可以作为输出的设备名。

        exclude 里放"已经在响、不需要再勾"的设备（通常是 Windows 的默认输出，
        以及我们自己正在采集的那块）—— 勾了它会听到原声加一份晚到的复制，
        两路叠加互相抵消，听感就是金属音。

        ``devices`` 允许调用方复用手动枚举好的设备快照，避免面板轮询时重复枚举。
        """
        skip = [str(x) for x in (exclude or []) if str(x).strip()]
        out: list[str] = []
        try:
            for item in (devices if devices is not None else audio_base.list_all_devices()):
                if getattr(item, "kind", "") != audio_base.KIND_SINK:
                    continue
                # 只列真实输出设备：文件后端和空后端也会报一个"设备名"，
                # 但它们不是耳机/音箱，列出来只会让人误选。
                if str(getattr(item, "backend", "")) in ("file", "null"):
                    continue
                name = str(getattr(item, "name", "") or "")
                if not name or name in out:
                    continue
                if any(name == s or name in s or s in name for s in skip):
                    continue
                out.append(name)
        except Exception:
            pass
        return out
