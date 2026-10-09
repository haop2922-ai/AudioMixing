"""The node engine: turns a route table into real streams, threads and audio.

A running node owns:

* a **Receiver** on the audio port (one UDP socket, one jitter buffer per stream),
* zero or more **sender sessions** (one per local source -> remote sink pair),
* zero or more **sink engines**, one per local output device that is targeted by
  some route; each has its own render thread that mixes, applies gain and writes
  to the device at the device's own pace.

Route table semantics
---------------------
A route rule is ``{"id", "from": [node|"*"], "to": [node|"*"], "gain", "mute",
"codec", "source_device", "sink_device"}``.

* **Origin rule** - this node starts a sender when it matches ``from`` and some
  remote node matches ``to`` (that node's sink device must match ``sink_device``).
  ``from`` matching "self" means either the ``self`` keyword, this node's id, or
  this node's short id / name.
* **Termination rule** - this node plays audio when it matches ``to`` and some
  other node matches ``from``. One sink engine is created per matching route,
  which is what lets two people share a headset, or one person listen to two
  computers, without either side re-encoding anything.
* When ``from``/``to`` is ``"*"`` the rule applies to everyone (used for the
  "party" mode); a sender picks the most recently active node as the origin so
  that exactly one device captures each source.
"""

from __future__ import annotations

import json
import urllib.request

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import proto
from . import __version__
from .audio import base as audio_base
from .audio.format import CHANNELS, SAMPLE_RATE, apply_gain, as_float_array, peak as sample_peak
from .config import Config
from .net.discovery import BeaconService, DiscoveryRegistry, PeerEndpoints, short_id
from .net.rtpaudio import JitterBuffer, Receiver, Sender, SenderSession
from .audio.capture import CaptureHub
from .audio.local import LocalLoop
from .audio.tap import AudioTap

# How long a phone must be gone before its name leaves the source list.
PRUNE_AFTER_S = 60.0

SELF_KEYWORDS = ("self", "*self*", "@self", "me", "本机", "自己")

# One fixed route id for the whole LAN channel, so every station's audio lands in the
# same route on a listener and is mixed into a single block there instead of opening
# one sink per station.
CHANNEL_ROUTE_ID = "channel"

# How much audio a render loop may hold back when the device stops accepting it.
# Six 10 ms blocks is enough to ride out a scheduling hiccup without letting a
# dead endpoint turn into seconds of latency.
MAX_BACKLOG_BLOCKS = 6


def is_self_token(token: str, node_id: str, name: str) -> bool:
    t = (token or "").strip().lower()
    return t in SELF_KEYWORDS or t == node_id.lower() or t == short_id(node_id) or t == name.lower()


@dataclass
class SourceState:
    spec: dict[str, Any]
    source: Any
    device: str
    started_ns: float = 0.0


@dataclass

class SinkEngine:
    route_id: str
    device_selector: str
    gain: float = 1.0
    mute: bool = False
    frames_per_block: int = 480
    sink: Any = None
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    error: str = ""
    frames_written: int = 0
    underruns: int = 0
    dropped_frames: int = 0
    started_ns: float = 0.0
    peak: float = 0.0
    paused: bool = False

    def describe(self) -> dict[str, Any]:
        device = self.sink.describe() if self.sink is not None else {"device": self.device_selector}
        return {
            "route_id": self.route_id,
            "device_selector": self.device_selector,
            "gain": self.gain,
            "mute": self.mute,
            "frames_written": self.frames_written,
            "underruns": self.underruns,
            "dropped_frames": self.dropped_frames,
            "running": bool(self.thread and self.thread.is_alive()),
            "error": self.error,
            "peak": round(self.peak, 4),
            "device": device,
        }


class _SinkFeed:
    """一条路由借用别人的播放端时的占位对象。

    它不进设备、不开线程，只说明"这条路由的声音由哪个播放端负责"。
    保留这个对象是必要的：路由表、监控和测试都按 route_id 找播放端，
    对象不存在会被当成"这条路由没有输出"。
    """

    def __init__(self, route_id: str, device_selector: str, feeds_into: Any) -> None:
        self.route_id = route_id
        self.device_selector = device_selector
        self.feeds_into = feeds_into
        self.gain = 1.0
        self.mute = False
        self.peak = 0.0
        self.error = ""
        self.thread = None
        self.sink = None
        self.frames_written = 0
        self.underruns = 0
        self.dropped_frames = 0
        # 停止流程会对每个播放端 set stop_event 并 join 线程，
        # 占位对象没有线程，但要能安全地走完同一套代码。
        self.stop_event = threading.Event()
        self.started_ns = time.monotonic()

    def describe(self) -> dict[str, Any]:
        """和 SinkEngine.describe 同样的形状。

        状态接口对 self.sinks 里的每个对象都调用它。少这个方法时，
        /api/status 会直接 500 —— 现场就是"一打开只听监控页就报错"。
        """
        holder = getattr(self.feeds_into, "route_id", "")
        return {
            "route_id": self.route_id,
            "device_selector": self.device_selector,
            "gain": self.gain,
            "mute": self.mute,
            "frames_written": self.frames_written,
            "underruns": self.underruns,
            "dropped_frames": self.dropped_frames,
            "running": bool(getattr(self.feeds_into, "thread", None)
                            and self.feeds_into.thread.is_alive()),
            "error": self.error,
            "peak": round(self.peak, 4),
            "device": {"device": self.device_selector},
            "feeds_into": holder,
        }


class WaveLinkNode:
    """One WaveLink instance on one machine."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        audio_port: int | None = None,
        discovery_port: int | None = None,
        bind_host: str = "0.0.0.0",
        simulate: bool = False,
    ) -> None:
        self.config = config or Config.load()
        self.bind_host = bind_host
        self.simulate = simulate
        if audio_port:
            self.config.audio_port = audio_port
        if discovery_port:
            self.config.discovery_port = discovery_port
        self.node_id = self.config.node_id
        self.name = self.config.name
        self.registry = DiscoveryRegistry(self.node_id)
        self.sender = Sender(self.node_id, self.name)
        self.receiver = Receiver(
            self.node_id,
            self.config.audio_port,
            on_first_packet=self._on_stream_start,
            bind_host=bind_host,
        )
        self.sinks: dict[str, SinkEngine] = {}
        self.hub = CaptureHub()
        # Browsers cannot join the UDP audio plane, so every block a local sink plays
        # is copied here for HTTP/WebSocket listeners (the phone "radio" page).
        self.tap = AudioTap(name="out")
        # The single-machine path: capture -> render on this PC, with no LAN involved.
        # That is how a phone arriving over Bluetooth reaches this machine's headphones.
        self._loop: LocalLoop | None = None
        self._loop_key: tuple | None = None
        self._local_note = ""
        # 打开成功但一帧都拿不到的输出设备。蓝牙耳机断开后 Windows 有时
        # 仍然把它报成可用，打开也不报错，就是没有数据。记下来，下次换一个。
        self._silent_captures: dict[str, float] = {}
        self._local_logged = ""
        self.routes: list[dict[str, Any]] = []
        self._routes_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()
        self._shared_sinks: dict[str, Any] = {}
        self.beacon: BeaconService | None = None
        self.devices: list[audio_base.AudioDevice] = []
        self.devices_refreshed_at = 0.0
        self.started_at = time.time()
        self.log: list[str] = []
        self.last_reconcile_at = 0.0
        self.last_error = ""
        self.render_frames = 480

    # -- helpers -----------------------------------------------------------
    def note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append("%s %s" % (stamp, message))
        del self.log[:-200]

    def source_list(self) -> list[dict[str, Any]]:
        """Capture devices, grouped so the UI can show the useful ones first.

        ``kind_class`` is what the page keys off:
          * ``system``    - WASAPI loopback: what this machine is *playing* (desktop
                            audio). This is what "分享桌面音频" means.
          * ``mic``       - a real microphone/capture endpoint.
          * ``generated`` - built-in test tone, for checking the link without audio.
          * ``file``      - WAV reader, a testing/automation aid.
        """
        out: list[dict[str, Any]] = []
        for d in self.devices:
            if d.kind != audio_base.KIND_SOURCE:
                continue
            detail = (d.kind_detail or "").lower()
            if detail == "loopback":
                cls = "system"
            elif detail in ("capture", "mic"):
                cls = "mic"
            elif detail == "file":
                cls = "file"
            else:
                cls = "generated"
            out.append({"id": d.id, "name": d.name, "detail": d.kind_detail, "backend": d.backend, "kind_class": cls})
        order = {"system": 0, "mic": 1, "generated": 2, "file": 3}
        out.sort(key=lambda x: (order.get(x["kind_class"], 9), x["name"].lower()))
        return out

    def sink_list(self) -> list[dict[str, Any]]:
        """Playback devices, grouped the same way (``output`` vs test aids)."""
        out: list[dict[str, Any]] = []
        for d in self.devices:
            if d.kind != audio_base.KIND_SINK:
                continue
            detail = (d.kind_detail or "").lower()
            if detail == "render":
                cls = "output"
            elif detail == "file":
                cls = "file"
            else:
                cls = "generated"
            out.append({"id": d.id, "name": d.name, "detail": d.kind_detail, "backend": d.backend, "kind_class": cls})
        order = {"output": 0, "generated": 1, "file": 2}
        out.sort(key=lambda x: (order.get(x["kind_class"], 9), x["name"].lower()))
        return out

    def refresh_devices(self, force: bool = False) -> list[audio_base.AudioDevice]:
        if not force and time.time() - self.devices_refreshed_at < 4.0:
            return self.devices
        try:
            self.devices = audio_base.list_all_devices()
            self.devices_refreshed_at = time.time()
        except Exception as exc:  # a device API can fail transiently
            self.last_error = "device enumeration failed: %s" % exc
        return self.devices

    def default_source_spec(self) -> dict[str, Any]:
        backend = audio_base.default_backend_name()
        if self.simulate:
            return {"backend": "null", "device": "tone:440", "freq": 440.0}
        if "wasapi" not in audio_base.available_backends():
            # Silence, never a test tone. Substituting a full-scale 440 Hz tone for a real
            # capture turned a transient WASAPI failure into a loud high-pitched noise on
            # every phone that had joined as a speaker. The tone belongs to simulation,
            # where somebody asked for it.
            return {"backend": "null", "device": "silence", "freq": 440.0}
        selector = (self.config.source_device or "").strip() or "@default"
        if selector.lower() in ("default", "@default"):
            # 系统默认输出若是虚拟声卡，采它就是采一片空白（日志里那句"默认输出设备
            # 采不到数据"就是这么来的），而且它往往是回声源。换成真实的输出设备。
            picked = self._default_render_name()
            if picked:
                selector = picked
        # 返回单个规格字典（调用方要的是 dict，不是 list）。
        # 选的是麦克风就按输入打开，选的是输出设备才采它的回环。
        return {"backend": "wasapi", "device": selector,
                "loopback": self._selector_is_loopback(selector)}

    def _looks_virtual(self, name: str) -> bool:
        """是不是一块虚拟声卡。

        虚拟声卡（Steam 串流、各种虚拟音频线、远程串流）可以被设成系统默认，
        但它永远不出声：声音进了虚空。所以默认设备是它的时候要另选一块真实的。
        这些名字在任何一台装了对应软件的机器上都一样，不是针对某台机器的特判。
        """
        text = str(name or "").lower()
        for marker in ("steam", "virtual", "虚拟", "vb-audio", "voicemeeter", "cable",
                       "vac ", "loopback", "远程", "remote audio"):
            if marker in text:
                return True
        return False

    def _default_render_name(self) -> str:
        """系统默认输出设备的名字；若它是虚拟声卡，换一块真实的。

        虚拟声卡（Steam 串流、虚拟音频线、远程串流）可以被设成系统默认，但它永远
        不出声 —— 采它等于采一片空白，而且它常常是个回声源。播放和采集都走这条判断，
        这样"默认设备"在两边的含义是一致的。返回空串表示看不出来，调用方按原样处理。
        """
        try:
            render = [d for d in self.devices
                      if d.kind == audio_base.KIND_SINK
                      and (d.kind_detail or "").lower() == "render"]
        except Exception:
            render = []
        default = next((d for d in render if d.is_default), None)
        if default is not None and not self._looks_virtual(str(default.name or "")):
            return str(default.name or "")
        for item in render:
            name = str(item.name or "")
            if name and not self._looks_virtual(name):
                self._note_once("系统默认是虚拟声卡（%s），改用 %s"
                                % (str(getattr(default, "name", "") or "?"), name))
                return name
        return str(getattr(default, "name", "") or "")

    def default_sink_spec(self) -> dict[str, Any]:
        """本机默认往哪块设备放声音。

        优先用配置里明确写的设备。没写就用系统默认，但如果系统默认是虚拟声卡，
        就换一块真实的输出设备 —— 虚拟声卡永远不出声。
        """
        # 播放必须走真实声卡。旧版这里写死 wasapi，我重建时改成了
        # default_backend_name()，而那个函数返回的是"第一个可用的后端"（file 排在最前），
        # 结果两台电脑收到的声音都被写进了文件，扬声器一点声音都没有。
        if self.simulate or "wasapi" not in audio_base.available_backends():
            return {"backend": "null", "device": "null"}
        backend = "wasapi"
        configured = str(self.config.sink_device or "").strip()
        if configured and configured.lower() not in ("default", "@default"):
            # 指定的设备是什么后端就用什么后端：写 WAV 文件的测试设备不能当成声卡去
            # 打开，反之亦然。找不到这个设备时退回默认后端。
            for item in (self.devices or []):
                if (item.kind == audio_base.KIND_SINK
                        and str(item.name or "") == configured
                        and str(item.backend or "")):
                    return {"backend": str(item.backend), "device": configured}
            return {"backend": backend, "device": configured}
        # sink_list() 不带 is_default 字段（它按名字排序给界面用），
        # 默认设备要从 devices 原始列表里找，否则永远找不到默认的那一块。
        picked = self._default_render_name()
        if picked:
            return {"backend": backend, "device": picked}
        return {"backend": backend, "device": "@default"}

    def autoconnect_phones(self) -> list[str]:
        """按配置连上蓝牙设备。面板和窗口的"连接"按钮走的也是它。"""
        wanted = [str(x) for x in (self.config.autoconnect or []) if str(x).strip()]
        if not wanted:
            return []
        return self._autoconnect(wanted)

    def _capture_selectors(self) -> list[str]:
        """现在正在采集的设备选择器。

        用于"音源就是输出设备本身"这个判断：同一个设备既采又放会啸叫，
        所以这里报的是真实的采集目标，而不是配置里写了什么。
        """
        names: list[str] = []
        try:
            spec = self.default_source_spec()
            device = str(spec.get("device") or "")
            if device:
                names.append(device)
        except Exception:
            pass
        try:
            loop = getattr(self, "_loop", None)
            if loop is not None:
                info = loop.describe() or {}
                current = str(info.get("device_name") or info.get("device") or "")
                if current and current not in names:
                    names.append(current)
        except Exception:
            pass
        return names

    def _same_endpoint(self, a: str, b: str) -> bool:
        """两个设备选择器是不是指同一块设备。

        宽松比较：设备名里有括号、有别名、有前后缀，严格相等会漏判，
        而漏判的代价是把"同一块设备"当成两块，从而出现自采集或者重复打开。
        """
        na = str(a or "").strip().lower()
        nb = str(b or "").strip().lower()
        if na == nb:
            return True
        if na in ("", "default", "@default") and nb in ("", "default", "@default"):
            return True
        if not na or not nb:
            return False
        return na in nb or nb in na

    def _pick_live_capture(self) -> str:
        """挑一个能真正出数据的采集设备名。

        与 _best_loopback_selector 的区别：这个会跳过已知"打开了却没数据"的设备
        （断开的蓝牙耳机就常这样）。没有可用记录时退回真实输出设备。
        """
        silent = getattr(self, "_silent_captures", {}) or {}
        for item in (self.devices or []):
            try:
                if item.kind != audio_base.KIND_SINK:
                    continue
                name = str(item.name or "")
                if name and name not in silent and not self._looks_virtual(name):
                    return name
            except Exception:
                continue
        return self._best_loopback_selector()

    def _best_loopback_selector(self) -> str:
        """挑一块真实输出设备的名字，用来做本机回环采集。

        听广播、放点对点、播手机声音都需要从某块输出设备上采声音。
        这里返回的是设备名（不是 @default），调用方在解析不到默认设备时用它兜底。
        """
        picked = self._default_render_name()
        if picked:
            return picked
        try:
            for item in (self.devices or []):
                if item.kind == audio_base.KIND_SINK and not self._looks_virtual(str(item.name or "")):
                    return str(item.name or "")
        except Exception:
            pass
        return ""

    def _selector_is_loopback(self, selector: str) -> bool:
        """这个选择器指的是不是一块输出设备（我们采它的回环）。

        手机走蓝牙进来时，Windows 把它暴露成一个**采集**设备
        （"麦克风 (xxx A2DP SNK)"）。那种名字去要 render 端点是找不到的，
        采集端根本打不开，表现就是"连上了但没声音"。所以这里必须先判断类型。
        认不出来的名字保持老行为（当输出设备），免得误伤。
        """
        text = str(selector or "").strip().lower()
        if not text or text in ("default", "@default"):
            return True
        try:
            devices = list(self.devices or [])
        except Exception:
            devices = []
        for item in devices:
            name = str(getattr(item, "name", "") or "").strip().lower()
            if not name:
                continue
            if text == name or text in name or name in text:
                # 字段名是 kind_detail（"loopback" / "capture"），不是 path。
                detail = str(getattr(item, "kind_detail", "") or "").lower()
                return detail != "capture"
        return True

    def _autoconnect(self, wanted: list[str]) -> list[str]:
        """连上配置里写明的蓝牙设备。start 时调一次。"""
        from . import bluetooth as bt

        if not bt.available():
            self.note("自启连接：这台机器不支持（不是 Windows 或缺少 powershell.exe）")
            return []
        try:
            devices = bt.list_devices_cached()
        except Exception as exc:
            self.note("自启连接：读取蓝牙设备失败 %s" % exc)
            return []
        present = {d["name"]: d for d in devices}
        done: list[str] = []
        for name in wanted:
            device = present.get(name)
            if device is None:
                self.note("自启连接：没找到「%s」（没配对、不在范围内，或名字改了）" % name)
                continue
            if device.get("link") in ("connected", "waiting"):
                done.append(name)  # already up; reconnecting would cut the audio
                continue
            result = bt.connect(device["id"])
            self.note("自启连接「%s」-> %s" % (name, result.get("state") or result.get("error")))
            if result.get("ok"):
                done.append(name)
        return done

    def start(self, *, announce: bool = True) -> None:
        """Bring the node up: receiver, device list, discovery, background loops.

        `announce=False` keeps everything off the network, which is what the tests use.
        """
        self.receiver.start()
        self.refresh_devices()
        if announce:
            self.beacon = BeaconService(
                self.registry,
                self.node_id,
                self.name,
                self.config.audio_port,
                self.config.web_port,
                self.source_list,
                self.sink_list,
                port=self.config.discovery_port,
                version=__version__,
            )
            for peer in self.config.manual_peers:
                host = str(peer.get("host") or "")
                if host:
                    self.beacon.add_manual_target(host, int(peer.get("port") or 0) or None)
                    self.registry.add_manual(
                        host,
                        int(peer.get("port") or 0) or self.config.audio_port,
                        str(peer.get("name") or ""),
                    )
            self.beacon.start()
        self.note("engine started on node %s (%s), audio port %d"
                  % (short_id(self.node_id), self.name, self.config.audio_port))
        for target, thread_name in ((self._maintenance_loop, "wavelink-maintenance"),
                                    (self._ping_loop, "wavelink-ping")):
            worker = threading.Thread(target=target, name=thread_name, daemon=True)
            self._threads.append(worker)
            worker.start()
        if self.config.autoconnect:
            try:
                self._autoconnect(list(self.config.autoconnect))
            except Exception as exc:
                self.note("自启连接失败：%s" % exc)
        self.apply_routes()

    def stop(self) -> None:
        self._stop.set()
        self.sender.stop_all()
        for engine in list(self.sinks.values()):
            self._stop_sink(engine.route_id)
        if self.beacon is not None:
            self.beacon.stop()
        self.receiver.stop()
        self.hub.stop_all()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    # -- background loops --------------------------------------------------
    def wake(self) -> None:
        """Run the next maintenance tick right away.

        A phone that has just connected should be heard in a fraction of a second, not
        whenever the next poll happens to come round.
        """
        wake = getattr(self, "_wake", None)
        if wake is not None:
            wake.set()

    def _sweep_dead_senders(self) -> None:
        """清掉"路由已经不存在、而且一直没发出任何数据"的发送端。

        apply_routes 每轮都会停掉不该存在的发送端，正常情况下这就够了。但实测里
        出现过一种情况：会话对象留在那里、路由已经删掉、字节数一直是 0 —— 界面上
        就是一行"残留"，对端也收不到东西。这里做一次兜底清扫，不看路由从哪来，
        只看两个事实：路由不在当前表里 + 这个会话一个字节都没发出去。
        """
        try:
            live = {str(r.get("id") or "") for r in (self.routes or [])}
        except Exception:
            return
        now = time.time()
        for key, session in list((getattr(self.sender, "sessions", {}) or {}).items()):
            rid = str(key).split("|", 1)[0]
            if not rid:
                continue
            # 关键：只要"零字节"就说明它是死的。
            # 真正的静音也会照常出数据（字节数照涨），所以零字节只可能是：
            # 采集端没工作、对端早就走了、或者会话已经没人用了。
            # 以前这里还额外要求"路由不在表里"，只要表里残留一条同名记录就永远清不掉 ——
            # 现场就是界面上挂着一行 0 B 的残留，一直不消失。
            if rid.startswith("channel"):
                # 广播通道由开关控制：开关关着、且零字节，才清
                if self.config.broadcast or self.config.listen:
                    continue
            sent = 0
            try:
                stats = session.stats() if hasattr(session, "stats") else None
                if isinstance(stats, dict):
                    for field in ("bytes", "packets", "frames", "sent"):
                        sent = max(sent, int(stats.get(field) or 0))
                sent = max(sent, int(getattr(session, "bytes_sent", 0) or 0))
            except Exception:
                sent = 0
            if sent:
                continue
            # 会话自己不一定带开始时间，所以自己记一笔"第一次见到它"的时间。
            # 以前写的是 started_at or now，遇到 0 就当成刚创建，于是永远扫不掉。
            seen = getattr(self, "_session_first_seen", None)
            if seen is None:
                seen = self._session_first_seen = {}
            first_seen = seen.setdefault(key, now)
            age = now - first_seen
            started = float(getattr(session, "started_at", 0.0) or 0.0)
            if started > 0:
                age = max(age, now - started)
            if age < 8.0:
                continue
            # 会话还在，就把记录清掉，避免长期占用
            seen.pop(key, None)
            try:
                self.sender.remove(key)
                self.note("清掉残留发送端 %s（一直没有数据，判定为死会话）" % key)
            except Exception as exc:
                self._note_once("清理残留发送端失败：%s" % exc)


    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # keep the node alive whatever happens
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                self.note("maintenance error: %s" % self.last_error)
            # Half a second, not two: a phone that starts sending has to be picked up, mixed
            # and played, and every second of that cycle was a second of silence on
            # first connection.
            wake = getattr(self, "_wake", None)
            if wake is not None:
                wake.wait(0.5)
                wake.clear()
            else:
                self._stop.wait(0.5)

    def _ping_loop(self) -> None:
        while not self._stop.is_set():
            targets = []
            for peer in self.registry.online(include_local=True):
                if peer.audio_port:
                    targets.append((peer.host, peer.audio_port, peer.node_id))
            if targets:
                self.receiver.ping_loop(lambda: targets, interval=1.0)
            self._stop.wait(0.5)

    def tick(self) -> None:
        self.refresh_devices()
        self._apply_pushed_routes()
        self.apply_routes()

        # 兜底清扫放在 tick 里：tick 每半秒跑一次，是唯一稳定被执行的地方。
        # 单独包一层，任何异常都不能影响调和本身。
        try:
            self._sweep_dead_senders()
        except Exception as exc:
            self._note_once("残留清扫出错（已忽略）：%s" % exc)

    def _apply_pushed_routes(self) -> None:
        """Accept route tables pushed by another node's control panel.

        The table is absolute, not a delta, so applying the newest one is enough
        and a lost push is repaired by the next one or by the next beacon cycle.
        """
        with self.receiver._lock:  # noqa: SLF001 - receiver owns the message queue
            messages = [m for m in self.receiver.control_messages if m.get("_type") == "ROUTE"]
            self.receiver.control_messages = [m for m in self.receiver.control_messages if m.get("_type") != "ROUTE"]
        if not messages:
            return
        newest = messages[-1]
        routes = newest.get("routes")
        if not isinstance(routes, list):
            return
        origin = str(newest.get("from") or "")
        if origin == self.node_id:
            return
        # Only newer pushes win: ignore anything older than what we already applied.
        stamp = float(newest.get("ts") or 0)
        if stamp and stamp <= getattr(self, "_last_push_ts", 0.0):
            return
        self._last_push_ts = stamp or time.time()
        # Routes the pusher explicitly deleted. Honouring these is what makes
        # "delete" work from either panel: without it the owner's own copy would
        # simply be re-pushed on the next change and the route would come back.
        removed = {str(x) for x in (newest.get("removed") or [])}
        # The sender's own routes replace that sender's share; everyone else's
        # (including ours) stay, except the ids it explicitly removed. `self` inside
        # those routes means *them*, and `_normalize_route` resolves it against the
        # pusher, not against us.
        self.note("merging %d route(s) pushed by %s%s"
                  % (len(routes), (origin or "?")[:8],
                     " (removed %d)" % len(removed) if removed else ""))
        self.set_routes(routes, author_id=origin, drop=removed)

    # -- route handling ----------------------------------------------------
    def set_routes(
        self,
        routes: list[dict[str, Any]],
        persist: bool = True,
        author_id: str | None = None,
        author_name: str = "",
        drop: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Replace `author`'s share of the route table and reconcile.

        The route table is shared by every node on the LAN, but each node owns the
        routes its own user created. Keeping the *other* nodes' routes instead of
        overwriting the whole table is what lets two people each drag their own
        route on their own machine without silently deleting the other's.

        `drop` names route ids to remove no matter who owns them. It is required for
        deleting a peer's route: simply leaving it out of `routes` is not enough,
        because the merge below would restore it from the current table.
        """
        author = author_id or self.node_id
        if not author_name and author == self.node_id:
            author_name = self.name
        if not author_name:
            peer = self.registry.get(author)
            author_name = peer.name if peer is not None else ""
        incoming = [self._normalize_route(r, author, author_name) for r in routes if isinstance(r, dict)]
        dropped = {str(x) for x in drop}

        with self._routes_lock:
            incoming_ids = {r["id"] for r in incoming}
            # Drop this author's previous routes (the push is absolute, not a delta),
            # anything explicitly dropped, and any local copy of an id that just came
            # back to us, then append.
            kept = [
                r for r in self.routes
                if r.get("origin") != author and r["id"] not in incoming_ids and r["id"] not in dropped
            ]
            merged = kept + incoming
            self.routes = merged
            # Only our own routes are persisted: a peer's route belongs to that peer
            # and comes back over the wire, so storing it would resurrect it forever
            # after the other machine is gone.
            self.config.routes = [r for r in merged if r.get("origin") in (None, self.node_id)]
        if persist:
            try:
                self.config.save()
            except OSError as exc:
                self.note("could not save config: %s" % exc)
        self.apply_routes()
        return merged

    def _pull_peer_routes(self) -> None:
        """Ask peers for their route table, instead of only pushing ours.

        路由平时是推过去的。推的方向是"我们连对端的面板端口"，如果对端防火墙挡了入站
        （公司网里的笔记本很常见），这一推就永远到不了：对端不知道有这条路由，于是
        该发声音的那一方保持安静，而两边的界面都看不出问题。

        反过来问一句走的是永远通畅的方向：对端连我们是出站，通常不被挡。
        """
        try:
            peers = self.peers_json()
        except Exception:
            return
        pulled = 0
        for peer in peers or []:
            if pulled >= 2:
                break
            host = str(peer.get("host") or "")
            if not host:
                continue
            port = int(peer.get("web_port") or 17800)
            peer_id = str(peer.get("node_id") or peer.get("id") or "")
            if not peer_id:
                continue
            # 先问对端是谁。手动添加设备只能填 IP，登记表里的身份是 "manual:IP"，
            # 和路由里写的真实节点 ID 对不上，光有地址也发不出去。
            try:
                with urllib.request.urlopen("http://%s:%d/api/status" % (host, port),
                                            timeout=1.5) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                node = info.get("node") or {}
                real_id = str(node.get("id") or "")
                if real_id and real_id != peer_id:
                    self.registry.register_identity(
                        real_id, host, int(node.get("audio_port") or 0),
                        int(node.get("web_port") or port), str(node.get("name") or ""))
                    peer_id = real_id
            except Exception:
                pass
            url = "http://%s:%d/api/routes" % (host, port)
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            routes = data.get("routes") if isinstance(data, dict) else data
            if not isinstance(routes, list) or not routes:
                continue
            try:
                self.set_routes(routes, persist=False, author_id=peer_id,
                                author_name=str(peer.get("name") or ""))
                pulled += 1
            except Exception as exc:
                self._note_once("合并对端路由失败：%s" % exc)
        if pulled:
            self._note_once("已从 %d 台电脑拉取路由表" % pulled)


    def local_routes(self) -> list[dict[str, Any]]:
        """The routes this node's own user created (what the panel edits)."""
        with self._routes_lock:
            return [r for r in self.routes if r.get("origin") in (None, self.node_id)]

    def _normalize_route(self, raw: dict[str, Any], author_id: str = "", author_name: str = "") -> dict[str, Any]:
        """Canonical route dict, with every self-reference resolved to a node id.

        ``self`` / 本机 are *relative*: they mean "the node that wrote this route".
        Shipping them over the wire and letting the reader resolve them against
        itself silently turns "play A's sound on B" into "play B's sound on B",
        which no node then acts on — the audio simply never starts. Resolving
        against the author once, here, makes a route mean the same thing
        everywhere.
        """

        def as_list(value) -> list[str]:
            if value is None or value == "":
                return ["*"]
            if isinstance(value, str):
                return [value]
            return [str(v) for v in value]

        def resolve(tokens: list[str]) -> list[str]:
            if not author_id:
                return tokens
            out: list[str] = []
            for token in tokens:
                if is_self_token(token, author_id, author_name):
                    out.append(author_id)
                else:
                    out.append(token)
            return out

        # Ownership survives a round trip: when a node receives back a route it
        # authored (the peer re-broadcasts the merged table), the route must stay
        # that node's, or it could never be edited or deleted again.
        origin = str(raw.get("origin") or "") or author_id or self.node_id

        return {
            "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
            "from": resolve(as_list(raw.get("from"))),
            "to": resolve(as_list(raw.get("to"))),
            "gain": float(raw.get("gain", 1.0)),
            "mute": bool(raw.get("mute", False)),
            "codec": str(raw.get("codec") or self.config.codec),
            "source_device": raw.get("source_device") or "",
            "sink_device": raw.get("sink_device") or "",
            "label": str(raw.get("label") or ""),
            "origin": origin,
        }

    def _is_origin_for(self, rule: dict[str, Any]) -> bool:
        if rule.get("kind") == "listen":
            return False  # a listener is never a source, whatever its `from` says
        from_tokens = rule["from"]
        if any(is_self_token(t, self.node_id, self.name) for t in from_tokens):
            return True
        # "*" (or a peer listing us) makes us a candidate origin, but only one node
        # may capture a given source, or the same sound would be sent twice. The
        # rule is deterministic and needs no negotiation: this node does it if it
        # sorts first among the matching nodes (our own id always sorts first,
        # because it is compared against itself).
        if "*" in from_tokens:
            candidates = sorted(self._matching_nodes(from_tokens), key=lambda p: p.node_id)
            if not candidates:
                return True
            return candidates[0].node_id == self.node_id
        return False

    def _self_peer(self) -> PeerEndpoints:
        peer = self.registry.get(self.node_id)
        if peer is None:
            peer = PeerEndpoints(
                node_id=self.node_id,
                name=self.name,
                host="127.0.0.1",
                audio_port=self.config.audio_port,
                web_port=self.config.web_port,
                local=True,
                manual=True,
            )
        return peer

    def _matching_nodes(self, tokens: list[str]) -> list[PeerEndpoints]:
        nodes: list[PeerEndpoints] = []
        seen: set[str] = set()
        for token in tokens:
            if token in ("*", ""):
                for peer in [self._self_peer()] + self.registry.all(include_local=True):
                    if peer.online and peer.node_id not in seen:
                        seen.add(peer.node_id)
                        nodes.append(peer)
                continue
            peer = self.registry.resolve(token)
            if peer is not None and peer.node_id not in seen:
                seen.add(peer.node_id)
                nodes.append(peer)
            elif is_self_token(token, self.node_id, self.name):
                local = self._self_peer()
                if local.node_id not in seen:
                    seen.add(local.node_id)
                    nodes.append(local)
        return nodes

    def desired_senders(self) -> list[tuple[str, PeerEndpoints, dict[str, Any]]]:
        """[(session_key, target_node, rule)] this node should be sending right now.

        The session key is ``<route id>|<target node id>``, *not* just the route id:
        one route may name several 播放在 targets, and each of those needs its own
        stream. Keying only by route id made later targets silently overwrite earlier
        ones, so a multi-target route played on a single speaker.
        """
        listen_only = bool(self.config.listen) and not bool(self.config.broadcast)
        # 只听 = 不再往广播通道里送声音（这是防止通道自我循环的关键），
        # 但它不该把普通路由的发送端也关掉。以前的实现直接 return []，
        # 结果是在发送端打开「只听」会让点对点的声音凭空消失：对方什么都收不到。
        # 现场就是这样：开启点对点后，发送端再开广播或只听，音频直接消失。
        out: list[tuple[str, PeerEndpoints, dict[str, Any]]] = []
        for rule in self.effective_routes():
            if rule.get("kind") == "listen":
                continue
            if listen_only and rule.get("kind") == "broadcast":
                # 只听模式下不往广播通道发送：通道会把自己刚收到的声音再发回去，
                # 造成回环。普通点对点路由不受影响。
                continue
            if rule["mute"] or not self._is_origin_for(rule):
                continue
            for target in self._matching_nodes(rule["to"]):
                if target.node_id == self.node_id:
                    continue
                if not target.online or not target.audio_port:
                    continue
                if rule["sink_device"] and target.sinks:
                    if not any(rule["sink_device"].lower() in (s.get("name") or "").lower() or rule["sink_device"] == s.get("id") for s in target.sinks):
                        continue
                out.append(("%s|%s" % (rule["id"], target.node_id), target, rule))
        return out

    def desired_sinks(self) -> dict[str, dict[str, Any]]:
        """{route_id: {"rule":.., "peers":[..]}} for streams to play locally."""
        out: dict[str, dict[str, Any]] = {}
        for rule in self.effective_routes():
            if not any(is_self_token(t, self.node_id, self.name) for t in rule["to"]) and "*" not in rule["to"]:
                continue
            peers = [p for p in self._matching_nodes(rule["from"]) if p.node_id != self.node_id]
            if not peers:
                continue
            # A route may name the output it wants, but that name was typed on some
            # machine's panel and this table is shared with every node on the LAN. It
            # is a hint, not a requirement: if this machine does not have that device,
            # play on this machine's own default. Requiring a match silenced whole
            # routes on the receiving side whenever the name belonged to the sender.
            local_rule = rule
            wanted = (rule.get("sink_device") or "").strip()
            if wanted and self.devices and not self._has_local_sink(wanted):
                local_rule = dict(rule)
                local_rule["sink_device"] = ""
            entry = out.setdefault(rule["id"], {"rule": local_rule, "peers": []})
            entry["peers"].extend(peers)
        return out

    def _has_local_sink(self, selector: str) -> bool:
        """True when `selector` names an output this machine actually has."""
        low = (selector or "").strip().lower()
        if not low:
            return False
        return any(
            low in (d.name or "").lower() or low == (d.id or "").lower()
            for d in self.devices
            if d.kind == audio_base.KIND_SINK
        )

    def _default_loopback_name(self) -> str:
        """Name of the playback endpoint Windows currently calls the default."""
        for d in self.devices:
            if d.kind == audio_base.KIND_SOURCE and (d.kind_detail or "") == "loopback" and d.is_default:
                return d.name or ""
        return ""

    def _resolved_source_key(self, spec: dict[str, Any]) -> tuple[str, str]:
        """Identity of the device a capture spec will actually open.

        A literal selector is already an identity. "@default" is not: it means
        "whatever Windows currently uses", and that changes while the program runs.
        Folding the resolved name into the key lets the reconciler notice the change
        and rebuild the stream instead of capturing a device nobody plays to.
        """
        key = (str(spec.get("backend")), str(spec.get("device")))
        sources = [str(s).strip() for s in (spec.get("sources") or []) if str(s).strip()]
        if sources:
            # Any change to the mixed inputs must rebuild the stream.
            return (key[0], " + ".join(self._resolved_source_key(self._single_source_spec(s))[1] for s in sources))
        if key[1].strip().lower() in ("", "@default", "default"):
            return (key[0], "@default:" + (self._default_loopback_name() or "?"))
        return key

    def _sink_device_still_valid(self, engine: SinkEngine, rule: dict[str, Any]) -> bool:
        """May a running sink stay on the endpoint it already has?

        The wanted device is recomputed on every reconcile, and a device list refresh
        can reorder two otherwise equal candidates (Windows flips `is_default` when a
        Bluetooth device sleeps). Re-opening the sink each time is heard as constant
        dropouts, so a running sink keeps its endpoint while that endpoint is still a
        sensible place to play.
        """
        selector = (engine.device_selector or "").strip()
        if not selector:
            return False
        wanted = (rule.get("sink_device") or "").strip()
        if wanted and not self._same_endpoint(selector, wanted):
            return False  # the route now asks for a specific device
        outputs = [d for d in self.devices if d.kind == audio_base.KIND_SINK]
        if outputs and not any(
            self._same_endpoint(selector, d.name) or selector.lower() == (d.id or "").lower() for d in outputs
        ):
            return False  # the device is gone (unplugged, disabled)
        return True

    def channel_routes(self) -> list[dict[str, Any]]:
        """The LAN "radio channel": one station transmits, anyone can tune in.

        `broadcast` sends this machine's sound to every node on the network.
        `listen` plays everything that arrives on the channel, and a listening node
        sends nothing at all - that is what makes the channel loop-free by
        construction, exactly like a radio receiver never feeding back into the
        transmitter. Both together is a conference, and is allowed; it echoes only if
        the same sound device is captured and played, which is the operator's call.
        """
        out: list[dict[str, Any]] = []
        if self.config.listen:
            out.append({
                "id": CHANNEL_ROUTE_ID, "from": ["*"], "to": [self.node_id],
                "gain": float(self.config.channel_gain), "mute": False,
                "codec": self.config.codec, "source_device": "", "sink_device": "",
                "label": "频道收听", "origin": self.node_id, "kind": "listen",
            })
        if self.config.broadcast:
            out.append({
                "id": CHANNEL_ROUTE_ID, "from": [self.node_id], "to": ["*"],
                "gain": 1.0, "mute": False,
                "codec": self.config.codec, "source_device": "", "sink_device": "",
                "label": "频道广播", "origin": self.node_id, "kind": "broadcast",
            })
        return out

    def effective_routes(self) -> list[dict[str, Any]]:
        """The table plus the synthesised channel rules (never persisted)."""
        with self._routes_lock:
            table = list(self.routes)
        return self.channel_routes() + table

    def channel_state(self) -> dict[str, Any]:
        return {
            "broadcast": bool(self.config.broadcast),
            "listen": bool(self.config.listen),
            "gain": round(float(self.config.channel_gain), 3),
            "id": CHANNEL_ROUTE_ID,
        }

    def apply_routes(self) -> None:
        self.last_reconcile_at = time.time()
        # 1. Senders ---------------------------------------------------------
        wanted = self.desired_senders()
        wanted_ids = {rid for rid, _t, _r in wanted}
        for rid in list(self.sender.sessions.keys()):
            if rid not in wanted_ids:
                self.sender.remove(rid)
                self.note("stopped sender %s" % rid)
        for rid, target, rule in wanted:
            spec = self._source_spec_for(rule)
            source_key = (str(spec.get("backend")), str(spec.get("device")))
            resolved_key = self._resolved_source_key(spec)
            session = self.sender.get(rid)
            same_target = session is not None and session.host == target.host and session.port == target.audio_port
            # Reusing the session is only correct while its capture device is still the
            # one the settings ask for. `resolved_key` covers the case that matters most
            # in practice: "@default" is not a fixed endpoint, and Windows switches the
            # default output whenever a headset is plugged in or a Bluetooth device
            # connects. A capture bound to the old endpoint then sits on a device that
            # plays nothing, so the peer hears silence - which is exactly what "the
            # other side went quiet after I changed my output" looks like.
            if same_target and getattr(session, "source_resolved", None) == resolved_key:
                if session.gain != rule["gain"]:
                    session.gain = rule["gain"]
                continue
            if session is not None:
                # The route is unchanged but its destination moved (peer re-resolved):
                # replace the session so the old target stops receiving.
                self.sender.remove(rid)
            try:
                source = self._open_source(spec)
            except Exception as exc:
                fallback = self._best_loopback_selector() if str(spec.get("device", "")).lower() in ("@default", "") else ""
                if fallback:
                    # The default endpoint may be gone (unplugged headset, disabled
                    # device). Capture a real one rather than dropping the route.
                    try:
                        retry = dict(spec)
                        retry["device"] = fallback
                        source = self._open_source(retry)
                        spec = retry
                        self.note("default capture failed (%s); using %s" % (exc, fallback))
                    except Exception as exc2:
                        self.last_error = "cannot open source %s: %s" % (spec, exc2)
                        self.note(self.last_error)
                        continue
                else:
                    self.last_error = "cannot open source %s: %s" % (spec, exc)
                    self.note(self.last_error)
                    continue
            codec = proto.CODEC_BY_NAME.get(rule["codec"], proto.CODEC_PCM_S16)
            session = SenderSession(
                source,
                target.host,
                target.audio_port,
                codec=codec,
                gain=rule["gain"],
                node_id=self.node_id,
                node_name=self.name,
                source_name=source.name if hasattr(source, "name") else spec.get("device", ""),
                route_id=rid,
            )
            session.source_key = source_key
            session.source_resolved = resolved_key
            if not session.supported:
                self.note("codec %s unavailable, falling back to pcm_s16" % rule["codec"])
                session.codec = proto.CODEC_PCM_S16
                session._encoder = __import__("wavelink.proto", fromlist=["x"]).encode_pcm_s16
            try:
                self.sender.add(rid, session)
                self.note("sending %s -> %s:%d (%s)" % (rid, target.host, target.audio_port, target.name))
            except Exception as exc:
                self.last_error = "sender %s failed: %s" % (rid, exc)
                self.note(self.last_error)

        # 2. Sinks ------------------------------------------------------------
        desired = self.desired_sinks()
        for rid in list(self.sinks.keys()):
            if rid not in desired:
                engine = self.sinks.get(rid)
                feeds = getattr(engine, "feeds_into", None)
                if feeds is not None:
                    extra = getattr(feeds, "extra_routes", None)
                    if extra and rid in extra:
                        extra.remove(rid)
                    self.sinks.pop(rid, None)
                    self.note("sink %s 不再复用 %s 的设备" % (rid, feeds.route_id))
                    continue
                self._stop_sink(rid)
                self.note("closed sink %s" % rid)
        for rid, entry in desired.items():
            rule = entry["rule"]
            spec = self._sink_spec_for(rule)
            wanted_selector = str(spec.get("device") or "")
            engine = self.sinks.get(rid)
            if engine is not None and engine.device_selector != wanted_selector:
                # The chosen output device changed (settings edit, the default
                # endpoint moved). Follow it when the choice was explicit, but do not
                # chase a preference that merely re-ordered itself.
                explicit = bool((rule.get("sink_device") or "").strip()) or (
                    (self.config.sink_device or "").strip().lower() not in ("", "default", "@default")
                )
                if not explicit and self._sink_device_still_valid(engine, rule):
                    wanted_selector = engine.device_selector
                else:
                    self._stop_sink(rid)
                    self.note("sink %s moved to %s" % (rid, wanted_selector or "?"))
                    engine = None
            if engine is None:
                # 同一块设备只让一个播放端真正持有。别的路由把自己的混音交给它，
                # 而不是再打开一次设备。
                shared = None
                for other in list(self.sinks.values()):
                    if other is engine or getattr(other, "feeds_into", None) is not None:
                        continue
                    if wanted_selector:
                        if self._same_endpoint(other.device_selector, wanted_selector):
                            shared = other
                            break
                    elif not other.device_selector:
                        shared = other
                        break
                if shared is not None:
                    extra = getattr(shared, "extra_routes", None)
                    if extra is None:
                        extra = []
                        shared.extra_routes = extra
                    if rid not in extra:
                        extra.append(rid)
                    self.sinks[rid] = _SinkFeed(rid, wanted_selector, shared)
                    self.note("sink %s 复用 %s 的设备 %s"
                              % (rid, shared.route_id, wanted_selector or "默认"))
                    continue
                if wanted_selector and any(self._same_endpoint(wanted_selector, s) for s in self._capture_selectors()):
                    self.note("note: %s is captured and played at once - the other side will hear itself; "
                              "pick a different output to avoid the echo" % wanted_selector)
                engine = self._start_sink(rid, rule)
                if engine is None:
                    continue
            engine.gain = rule["gain"]
            engine.mute = bool(rule["mute"])
        # Streams that arrived before their sink existed must be attached now that
        # the table is settled, or they would never be heard.
        self._reattach_stream_routes()
        # Release capture devices whose last consumer has gone. Without this, every
        # device change (or a headset being plugged in) left a capture thread and a
        # WASAPI client running for the rest of the session.
        try:
            from .audio.backends import netmic as _netmic
            _netmic.set_jitter_ms(getattr(self.config, "netmic_jitter_ms", 150))
        except Exception:
            pass
        self._auto_play_sources()
        self.hub.reap()
        for name in self.hub.health():
            self._note_once("采集端卡死，已重建：%s" % name)
        self._apply_local_loop()
        self._sample_quality()
        # The real Bluetooth listing takes seconds, so it happens here, in the background
        # thread, and never on a request the panel makes.
        try:
            from . import bluetooth as _bt_refresh

            # 刷新频率取决于我们有没有握着连接：
            #   握着连接时列设备很危险，可能把用户正用的那条顶掉，所以慢刷（十分钟）。
            #   没握连接时列设备无害，而缓存的"已连接"会过期（手机会自己关蓝牙），
            #   所以勤刷（二十秒），免得面板一直报着一个已经不存在的连接。
            _bt_refresh.list_devices_cached(
                max_age=600.0 if _bt_refresh.held_count() else 20.0)
            # 每隔一会儿也去问对端要一次路由表，顺着"出站"这个永远通畅的方向。
            # 看门狗：采集开着却一帧都拿不到，说明这个设备是"死的"
            # （断开后 Windows 仍报可用的蓝牙设备就是这样）。记下来，换设备。
            # 注意：真正的静音也会出数据（帧数照涨），所以"帧数为零"等价于"设备没在工作"。
            try:
                loop = getattr(self, "_loop", None)
                if loop is not None:
                    info = loop.describe() or {}
                    started = float(getattr(loop, "started_at", 0.0) or 0.0)
                    frames = int(info.get("frames") or 0)
                    device = str(info.get("device_name") or info.get("device") or "")
                    if started and frames == 0 and time.time() - started > 15.0 and device:
                        silent = getattr(self, "_silent_captures", None)
                        if silent is not None and device not in silent:
                            silent[device] = time.time()
                            self._note_once("采集设备 %s 打开后一直没数据，已标记并换用其它设备"
                                            % device)
            except Exception:
                pass
            now_mono = time.monotonic()
            if now_mono - getattr(self, "_last_route_pull", 0.0) >= 6.0:
                self._last_route_pull = now_mono
                self._pull_peer_routes()
        except Exception as exc:
            self._note_once("蓝牙列表刷新失败：%s: %s" % (type(exc).__name__, exc))

    def _auto_play_sources(self) -> None:
        """Adopt a phone the moment it starts sending.

        On a LAN the operator knows what is on the network, and the click that used to
        be required was exactly the step where the audio went missing. Muted phones are
        left alone, so the mute button means something.
        """
        if not getattr(self.config, "auto_play", True):
            return
        from .audio.backends import netmic as netmic_backend
        try:
            live = [m for m in netmic_backend.active() if not m.muted]
        except Exception:
            return
        if not live:
            return
        # A name whose phone has been gone for a while is dropped from the sources: it
        # would otherwise accumulate forever (renamed phones, one-off tests), and every
        # stale name is one more thing that can keep the mix from opening. Auto-play
        # re-adopts the phone the moment it comes back, so nothing is lost.
        try:
            gone = [m.name for m in netmic_backend.MICS.values()
                    if m.bytes_in > 0 and (time.monotonic() - m.last_push) > PRUNE_AFTER_S]
        except Exception:
            gone = []
        if gone:
            kept = [str(s).strip() for s in self.config.channel_sources if str(s).strip() not in gone]
            if len(kept) != len([s for s in self.config.channel_sources if str(s).strip()]):
                self.config.channel_sources = kept
                self._note_once("音源里这些手机已不在，已移出：%s" % "、".join(gone))
                try:
                    self.config.save()
                except Exception:
                    pass
        current = [str(s).strip() for s in self.config.channel_sources]
        added = [m.name for m in live if m.name not in current]
        if not added:
            return
        self.config.channel_sources = current + added
        if not self.config.local:
            self.config.local = True
        self._note_once("自动收编正在发送的手机：%s" % "、".join(added))
        try:
            self.config.save()
        except Exception:
            pass

    def _live_channel_sources(self) -> tuple[list[str], list[str]]:
        """Split the configured sources into (usable now, not available now).

        A name that cannot be resolved would make the whole mix fail to open, and then
        nothing plays at all - which is a very confusing way to react to a phone that
        happens to be away, or to a stale name left behind when a phone was renamed.
        """
        usable: list[str] = []
        missing: list[str] = []
        # Exact *name* against a freshly listed source device. Not find_device(): that
        # also matches by id, and the Windows backend numbers its endpoints - so a phone
        # that had stopped sending still "resolved", to whichever endpoint happened to
        # own that number. The mix was then built from the headphones, the feedback guard
        # refused it as a howl, and the machine went silent over a phone that was simply
        # offline.
        names = {d.name.strip().lower() for d in self.devices
                 if d.kind == audio_base.KIND_SOURCE and d.name}
        try:
            # Plus a fresh listing, which is where a phone that just started sending
            # shows up before the periodic refresh has caught up.
            names |= {d.name.strip().lower() for d in audio_base.list_all_devices()
                      if d.kind == audio_base.KIND_SOURCE and d.name}
        except Exception:
            pass
        for selector in [str(s).strip() for s in self.config.channel_sources if str(s).strip()]:
            if selector.lower() in names:
                usable.append(selector)
            else:
                missing.append(selector)
        return usable, missing

    def _local_loop_spec(self) -> tuple[Any, dict[str, Any], tuple]:
        """(source, sink spec, change key) for the local capture->render path."""
        channels, missing = self._live_channel_sources()
        if missing:
            self._note_once("音源里这些现在打不开，已跳过：%s" % "、".join(missing))
        spec = dict(self.default_source_spec())
        if len(channels) > 1:
            spec["sources"] = channels
            spec["device"] = channels[0]
        elif len(channels) == 1:
            spec = self._single_source_spec(channels[0])
        sink_spec = self.default_sink_spec()
        key = (tuple(channels), str(sink_spec.get("device")), str(sink_spec.get("backend")))
        return spec, sink_spec, key

    @staticmethod
    def _looks_like_same_device(a: str, b: str) -> bool:
        """Loose device-name comparison, for the local pass-through feedback guard.

        Deliberately loose: a false positive only costs a refused pass-through with an
        explanation, while a false negative costs a howl in someone's ears.
        """
        strip = " \t()（）[]【】{}<>"
        na = "".join(c for c in str(a or "").lower() if c not in strip)
        nb = "".join(c for c in str(b or "").lower() if c not in strip)
        if not na or not nb:
            return False
        return na == nb or na in nb or nb in na

    def _note_once(self, message: str) -> None:
        """Report a standing condition only when it changes.

        `apply_routes` runs every couple of seconds, so a guard that logs each time it
        refuses buried the log in duplicate lines within a minute.
        """
        if message != self._local_logged:
            self._local_logged = message
            self.note(message)

    def _loop_needs_restart(self) -> bool:
        """Should the local pass-through be rebuilt?

        Not merely because it is not ready yet: `apply_routes` runs every couple of
        seconds, and treating "not ready" as "start again" rebuilt the loop forever -
        which kept re-opening the capture and resetting its cursor, so audio could never
        settle. A device that has just started gets ten seconds; after that its error is
        reported once and it is retried on a slow timer instead.
        """
        loop = self._loop
        if loop is None:
            return False
        # Generous enough not to thrash a device that is still opening, short enough that
        # a transient failure is not heard as ten seconds of silence.
        if loop.error:
            return (time.time() - loop.started_at) > 3.0
        if not loop.ready:
            return (time.time() - loop.started_at) > 2.0
        return False

    def _record_input_history(self, inputs: list[dict[str, Any]]) -> None:
        """One history point per input, so every row can draw its own curve.

        The metric has to fit the path: an arrival interval means nothing for Bluetooth,
        where Windows exposes no timing at all - there the honest curve is the link state
        itself, which at least shows when it drops.
        """
        if not hasattr(self, "_input_hist"):
            self._input_hist = {}
        seen = set()
        for item in inputs:
            key = (item.get("path"), item.get("name"))
            seen.add(key)
            value = 0.0
            if item.get("path") == "wifi":
                value = float(item.get("median_ms") or 0.0)
            elif item.get("path") == "bluetooth":
                value = 100.0 if str(item.get("state") or "").startswith("正在接收") else 40.0
            elif item.get("path") == "p2p":
                value = float(item.get("depth_ms") or 0.0)
            history = self._input_hist.setdefault(key, [])
            history.append(round(value, 1))
            if len(history) > 60:
                del history[:-60]
            item["history"] = list(history)
        for key in list(self._input_hist):
            if key not in seen:
                del self._input_hist[key]


    def _inputs_with_history(self) -> list[dict[str, Any]]:
        inputs = self.inputs_state()
        try:
            self._record_input_history(inputs)
        except Exception as exc:
            self._note_once("输入历史记录失败：%s: %s" % (type(exc).__name__, exc))
        return inputs

    def inputs_state(self) -> list[dict[str, Any]]:
        """Every sound arriving at this machine, with the path it came in on.

        The panel used to show only the Wi-Fi phones, so a phone feeding audio over
        Bluetooth was invisible there even while the Bluetooth card showed it playing -
        and the Wi-Fi phone never said how it got here. One list answers both.
        """
        out: list[dict[str, Any]] = []
        # Wi-Fi phones
        try:
            from .audio.backends import netmic as netmic_backend
            for mic in netmic_backend.active():
                info = mic.describe()
                penalty = min(100.0, (info.get("gaps_over_100ms") or 0) * 8.0
                              + (info.get("underruns") or 0) * 1.0)
                out.append({
                    "path": "wifi",
                    "path_label": "Wi-Fi",
                    "name": info.get("name") or "",
                    "state": "正在传入" if info.get("live") else "已停止",
                    "score": int(max(0.0, 100.0 - penalty)),
                    "detail": "间隔中位 %.1fms · 最大 %.0fms · 断档 %d 次"
                              % (info.get("median_gap_ms") or 0.0, info.get("max_gap_ms") or 0.0,
                                 info.get("gaps_over_100ms") or 0),
                    "muted": bool(info.get("muted")),
                    "gain": info.get("gain") or 1.0,
                    "metric": "到达间隔 (ms)",
                    "ideal": 20.0,
                    "median_ms": float(info.get("median_gap_ms") or 0.0),
                })
        except Exception:
            pass
        # Phones arriving over Bluetooth (Windows renders those straight to a device)
        try:
            from . import bluetooth as _bt

            for dev in _bt.cached_devices():
                link = str(dev.get("link") or "off")
                if link not in ("connected", "hfp"):
                    continue
                out.append({
                    "path": "bluetooth",
                    "path_label": "蓝牙" + ("（通话通路）" if link == "hfp" else ""),
                    "name": str(dev.get("name") or ""),
                    "state": "正在接收手机声音" if link == "connected" else "通话通路（音质低）",
                    "score": None,
                    "detail": "蓝牙链路的丢包/重传 Windows 不提供计数，只能听感判断",
                    "muted": False,
                    "gain": 1.0,
                    "metric": "链路状态 (满格=正在接收)",
                    "ideal": 100.0,
                })
        except Exception as exc:
            self._note_once("读取蓝牙设备失败：%s: %s" % (type(exc).__name__, exc))
        # Streams from other computers
        try:
            recv = self.receiver.stats() or {}
            for stream in (recv.get("streams") or []):
                lost = int(stream.get("lost") or 0)
                late = int(stream.get("late") or 0)
                out.append({
                    "path": "p2p",
                    "path_label": "点对点/广播",
                    "name": str(stream.get("node_name") or stream.get("source") or ""),
                    "state": "正在接收",
                    "score": int(max(0.0, 100.0 - min(100.0, lost * 5.0 + late * 2.0))),
                    "detail": "丢包 %d · 迟到 %d · 缓冲 %.0fms"
                              % (lost, late, float(stream.get("depth_ms") or 0.0)),
                    "muted": False,
                    "gain": 1.0,
                    "metric": "缓冲深度 (ms)",
                    "ideal": 60.0,
                    "depth_ms": float(stream.get("depth_ms") or 0.0),
                })
        except Exception:
            pass
        return out


    def quality_state(self) -> dict[str, Any]:
        """Per-path history for the panel's curves.

        A volume meter only says "something is playing". These say *which link* is
        misbehaving and by how much, which is the question that actually comes up.
        """
        if not hasattr(self, "quality_history"):
            self.quality_history = {}
        # Sample on demand as well as on the maintenance tick: the panel asks for status
        # every couple of seconds, so the curves fill from ordinary use even if the
        # periodic sampler is idle or its caller has stopped.
        last = getattr(self, "_quality_at", 0.0)
        if time.time() - last > 1.5:
            self._quality_at = time.time()
            try:
                self._sample_quality()
            except Exception as exc:
                self.note("质量采样出错：%s: %s" % (type(exc).__name__, exc))
        out: dict[str, Any] = {}
        for key in ("wifi", "bt", "p2p"):
            history = self.quality_history.get(key) or []
            out[key] = {
                "latest": (history[-1][1] if history else None),
                "history": [entry for _at, entry in history],
            }
        return out

    def _sample_quality(self) -> None:
        """One point per path, per maintenance tick (about every two seconds)."""
        try:
            from .audio.backends import netmic as netmic_backend
            mics = [m.describe() for m in netmic_backend.active()]
        except Exception:
            mics = []
        point: dict[str, Any] = {"wifi": None, "bt": None, "p2p": None}
        if mics:
            # Downstream rate and the full list of senders, so the panel can show what is
            # actually arriving instead of naming one phone and calling it the source.
            total_bytes = sum(int(d.get("bytes_in") or 0) for d in mics)
            now_mono = time.monotonic()
            prev = getattr(self, "_wifi_bytes", None)
            prev_at = getattr(self, "_wifi_at", 0.0)
            kbps = 0.0
            if prev is not None and now_mono - prev_at > 0.3:
                kbps = (total_bytes - prev) * 8 / 1000.0 / (now_mono - prev_at)
            self._wifi_bytes = total_bytes
            self._wifi_at = now_mono
            worst = max(mics, key=lambda d: d.get("median_gap_ms") or 0.0)
            penalty = min(100.0, (worst.get("gaps_over_100ms") or 0) * 8.0
                          + (worst.get("underruns") or 0) * 1.0)
            point["wifi"] = {
                "v": round(worst.get("median_gap_ms") or 0.0, 1),
                "score": int(max(0.0, 100.0 - penalty)),
                "label": " + ".join(d.get("name") or "" for d in mics),
                "count": len(mics),
                "kbps": round(max(0.0, kbps), 1),
                "unit": "ms",
                "ideal": 20.0,
                "detail": "块间隔 %.1fms · 最大 %.0fms · 断档 %d 次"
                          % (worst.get("median_gap_ms") or 0.0, worst.get("max_gap_ms") or 0.0,
                             worst.get("gaps_over_100ms") or 0),
            }
        try:
            local = self.local_state() or {}
        except Exception:
            local = {}
        # Named for what it measures: how the audio is fed to whatever the output
        # device happens to be. Calling it "Bluetooth" made the card look alive with
        # no headset connected - as misleading as a silent meter, and it also depended
        # on a name match against the paired-phone list, which never contained the
        # headset at all.
        if local.get("ready"):
            penalty = min(100.0, float(local.get("short_writes") or 0) * 5.0
                          + float(local.get("gaps") or 0) * 1.0)
            point["bt"] = {
                "v": round(float(local.get("max_gap_ms") or 0.0), 1),
                "score": int(max(0.0, 100.0 - penalty)),
                "label": "输出：" + str(local.get("sink") or ""),
                "unit": "ms",
                "ideal": 11.0,
                "detail": "喂声卡间隔最大 %.1fms · 短写 %d · 写入最大 %.1fms"
                          % (float(local.get("max_gap_ms") or 0.0),
                             int(local.get("short_writes") or 0),
                             float(local.get("slow_write_ms") or 0.0)),
            }
        try:
            recv = self.receiver.stats() or {}
            streams = recv.get("streams") or []
        except Exception:
            streams = []
        if streams:
            lost = sum(int(s.get("lost") or 0) for s in streams)
            late = sum(int(s.get("late") or 0) for s in streams)
            depth = max([float(s.get("depth_ms") or 0.0) for s in streams] or [0.0])
            penalty = min(100.0, lost * 5.0 + late * 2.0)
            point["p2p"] = {
                "v": round(depth, 1),
                "score": int(max(0.0, 100.0 - penalty)),
                "label": ", ".join(sorted({str(s.get("node_name") or "") for s in streams}))[:40],
                "unit": "ms",
                "ideal": 60.0,
                "detail": "丢包 %d · 迟到 %d · 缓冲 %.0fms" % (lost, late, depth),
            }
        if not hasattr(self, "quality_history"):
            self.quality_history = {}
        now = time.time()
        for key, entry in point.items():
            history = self.quality_history.setdefault(key, [])
            history.append((now, entry))
            if len(history) > 60:
                del history[:-60]

    def _system_sound_specs(self) -> list[dict[str, Any]]:
        """One loopback spec per real playback endpoint.

        "系统声音" is documented as everything this machine plays, so it has to be every
        output device, not just the endpoint Windows calls the default. Capturing only
        the default made audio sent to a second headset invisible - and turned a general
        option into one that had to be hand-tuned per machine.
        """
        # One device: whatever Windows is actually playing to, virtual or not. Summing
        # every render endpoint looked thorough and was wrong - idle virtual devices
        # (Steam's speakers, remote-desktop shims) hand back DC and sub-audio rumble, and a
        # stream that is 95% energy below 200 Hz is heard as a buzzing rattle on a phone
        # speaker. A virtual device is still correct to capture when it *is* the default:
        # that is where the sound genuinely goes.
        return [{"backend": "wasapi", "device": self._pick_live_capture(),
                 "loopback": True}]


    def _capture_names_for(self, spec: dict[str, Any]) -> list[str]:
        """The real device names a spec would capture (resolving @default as needed)."""
        names = [str(s).strip() for s in (spec.get("sources") or []) if str(s).strip()]
        if not names:
            names = [str(spec.get("device") or "@default")]
        out: list[str] = []
        for selector in names:
            if selector.strip().lower() in ("", "@default", "default"):
                out.append(self._default_loopback_name() or "")
                continue
            device = audio_base.find_device(selector)
            out.append(device.name if device is not None else selector)
        return [n for n in out if n]

    def _apply_local_loop(self) -> None:
        """Keep the local pass-through in step with the channel settings.

        Restarting on every change would click, so the loop is only rebuilt when the
        inputs or the output actually differ - and it is stopped outright when it is
        switched off, rather than left holding two devices open.
        """
        self._local_note = ""
        self._local_debug = {}
        if not self.config.local:
            if self._loop is not None:
                self._loop.stop()
                self._loop = None
                self._loop_key = None
            return
        spec, sink_spec, key = self._local_loop_spec()
        self._local_debug = {"spec": dict(spec), "sink": dict(sink_spec),
                             "captures": self._capture_names_for(spec)}
        # Refuse to build a feedback loop. Capturing the very endpoint we are playing to
        # is a howl within a second, and the channel's source list can easily still name
        # the output device - which is exactly what happened to a user, who heard "a lot
        # of noise" the moment the local pass-through was switched on.
        sink_name = str(sink_spec.get("device") or "")
        # Drop only the offending input. Refusing the whole mix meant that one stale
        # source pointed at the output device silenced every other source with it - and
        # clicking anything that rebuilt the routes was needed to get sound back.
        keep = [name for name in self._capture_names_for(spec)
                if not self._looks_like_same_device(name, sink_name)]
        dropped = [name for name in self._capture_names_for(spec)
                   if self._looks_like_same_device(name, sink_name)]
        if dropped and keep:
            self._note_once("音源 %s 就是输出设备本身，已从混音里去掉（避免啸叫）" % "、".join(dropped))
            spec = dict(spec)
            spec.pop("sources", None)
            kept = [str(s).strip() for s in self.config.channel_sources
                    if str(s).strip() and str(s).strip() not in dropped]
            if len(kept) > 1:
                spec["sources"] = kept
                spec["device"] = kept[0]
            elif kept:
                spec = self._single_source_spec(kept[0])
        if not keep:
            # Capturing the very device we are playing to would howl - but that input is
            # usually the machine's own sound, which is exactly what "broadcast my sound"
            # means. So the playback is what gets dropped, not the capture: read it into a
            # silent sink and keep publishing, and the phones still hear the PC while the
            # speakers do not howl.
            # 音源就是正在放的这块设备本身。这种情况不该再开一路采集去空转：
            # 手机走蓝牙进来时，声音由 Windows 直接送到耳机，本来就在响；
            # 我们再开一路回环采集并静音播放，只会多占一份设备和 CPU，
            # 听感就是开始还行、越听越卡。
            self._local_note = ("音源 %s 就是输出设备本身，声音由系统直接播放，"
                                "这里不再重复处理。" % "、".join(dropped))
            if self._loop is not None:
                self._loop.stop()
                self._loop = None
                self._loop_key = None
            return
        if self._loop is not None and self._loop_key == key and not self._loop_needs_restart():
            self._loop.gain = float(self.config.channel_gain)
            # The pass-through is the one thing that is always running when audio should
            # reach anyone - locally or over the LAN - so it always publishes. Making this
            # conditional produced a state where nobody published at all: the phones got
            # silence while the panel showed everything as healthy.
            self._loop.tap = self.tap
            return
        if self._loop is not None:
            self._loop.stop()
            self._loop = None
        try:
            source = self._open_source(spec)
            sink = audio_base.open_sink(sink_spec)
        except Exception as exc:
            self._local_note = "打不开设备: %s" % exc
            self._note_once("本机直通打不开设备: %s" % exc)
            return
        loop = LocalLoop(source, sink, gain=float(self.config.channel_gain), tap=self.tap)
        loop.start()
        self._loop = loop
        self._loop_key = key
        self.note("本机直通: %s -> %s" % (loop.describe()["source"], sink_spec.get("device")))

    def local_state(self) -> dict[str, Any]:
        state = self._loop.describe() if self._loop is not None else {}
        state["enabled"] = bool(self.config.local)
        if self._local_note:
            state["note"] = self._local_note
        return state

    def _source_spec_for(self, rule: dict[str, Any]) -> dict[str, Any]:
        if rule.get("kind") == "broadcast" and self.config.channel_sources:
            # The channel may mix several inputs - several phones, each on its own
            # Bluetooth adapter, or a phone plus this machine's own sound.
            excluded = {str(s).strip() for s in getattr(self.config, "broadcast_excluded", [])}
            sources = [str(s).strip() for s in self.config.channel_sources
                       if str(s).strip() and str(s).strip() not in excluded]
            if sources:
                spec = dict(self.default_source_spec())
                spec["sources"] = sources
                spec["device"] = sources[0]
                return spec
        spec = dict(self.default_source_spec())
        selector = (rule.get("source_device") or "").strip()
        if selector:
            spec = audio_base.spec_for_selector(spec, selector)
            # A route may name any input: a loopback of a playback device, a
            # microphone, or a phone that arrived over Bluetooth A2DP.
            spec["loopback"] = self._selector_is_loopback(selector)
        return spec

    def _open_source(self, spec: dict[str, Any]):
        """Get a fan-out reader for a capture device.

        Two routes that target two different sinks must send *identical* samples,
        so the hub opens the device once and every session reads its own cursor.
        A spec carrying several `sources` (the LAN channel) is mixed here, on the
        sending side, so the wire format and the receiver stay unchanged.
        """
        from .audio.backends import netmic as netmic_backend  # local: this is the only
        # place that needs it, and it must not depend on some other function's import.

        # "System sound" resolves to every playback endpoint here, so it really is
        # everything this machine plays.
        if (str(spec.get("backend")) == "wasapi" and spec.get("loopback")
                and str(spec.get("device") or "").lower() in ("", "@default", "default")):
            specs = self._system_sound_specs()
            if len(specs) > 1:
                self._note_once("系统声音：正在采集 %d 块输出设备" % len(specs))
                return self.hub.mixer(specs, name="系统声音")
        sources = [str(s).strip() for s in (spec.get("sources") or []) if str(s).strip()]
        if len(sources) > 1:
            specs = [self._single_source_spec(s) for s in sources]
            if all(str(sp.get("backend")) == "netmic" for sp in specs):
                # Phones mix here, not in the hub: their read is paced and blocking, and
                # the hub's writer parked on it, which is what silenced multi-phone setups.
                mics = []
                for sp in specs:
                    try:
                        mics.append(audio_base.open_source(sp))
                    except Exception as exc:
                        self._note_once("混音里这台打不开，已跳过：%s（%s）" % (sp.get("device"), exc))
                if not mics:
                    raise RuntimeError("没有可用的手机音源")
                if len(mics) == 1:
                    return mics[0]
                self._note_once("多台手机混音：%s" % "、".join(m.name for m in mics))
                return netmic_backend.NetMicMixer(mics)
            return self.hub.mixer(specs, name="频道混音")
        if len(sources) == 1:
            spec = self._single_source_spec(sources[0])
        if str(spec.get("backend")) == "netmic":
            # Straight from the phone, without the capture hub. The hub exists to fan one
            # hardware device out to several readers with independent cursors, and its
            # writer thread kept parking forever on this source (blocks_read stayed 0, no
            # error, nothing closed) - which is silence that no amount of rebuilding
            # fixes. A NetMic already has its own queue and its own pacing, so the extra
            # layer bought nothing and cost the audio.
            return audio_base.open_source(spec)
        key = (str(spec.get("backend")), str(spec.get("device")))
        return self.hub.reader(spec, name=key[1])

    def _single_source_spec(self, selector: str) -> dict[str, Any]:
        """A full spec for one selector, with the right capture direction."""
        spec = audio_base.spec_for_selector(dict(self.default_source_spec()), selector)
        spec["loopback"] = self._selector_is_loopback(selector)
        return spec

    def _sink_spec_for(self, rule: dict[str, Any]) -> dict[str, Any]:
        spec = dict(self.default_sink_spec())
        if rule.get("sink_device"):
            spec["device"] = rule["sink_device"]
        return spec

    def _start_sink(self, route_id: str, rule: dict[str, Any]) -> SinkEngine | None:
        spec = self._sink_spec_for(rule)
        try:
            sink = audio_base.open_sink(spec)
            sink.start()
        except Exception as exc:
            self.last_error = "cannot open sink %s: %s" % (spec, exc)
            self.note(self.last_error)
            return None
        engine = SinkEngine(
            route_id=route_id,
            device_selector=str(spec.get("device")),
            gain=float(rule.get("gain", 1.0)),
            mute=bool(rule.get("mute", False)),
            frames_per_block=self.render_frames,
            sink=sink,
        )
        engine.started_ns = time.monotonic()
        engine.thread = threading.Thread(target=self._render_loop, args=(engine,), name="wavelink-sink-%s" % route_id, daemon=True)
        self.sinks[route_id] = engine
        engine.thread.start()
        self.note("opened sink %s on %s" % (route_id, engine.device_selector))
        return engine

    def _stop_sink(self, route_id: str) -> None:
        engine = self.sinks.pop(route_id, None)
        if engine is None:
            return
        engine.stop_event.set()
        if getattr(engine, "thread", None) is not None:
            getattr(engine, "thread", None).join(timeout=2.0)
        if engine.sink is not None:
            try:
                engine.sink.stop()
            except Exception:
                pass

    def _render_loop(self, engine: SinkEngine) -> None:
        frames = engine.frames_per_block
        chunk = frames * CHANNELS
        # A render endpoint accepts only as much as fits in its buffer, so a block
        # often has to be handed over in pieces. Anything the device did not take
        # is *kept* for the next pass: dropping it (what this loop used to do) eats
        # a slice out of the audio several times a second, which is heard as a
        # click or a rapid ticking on a Bluetooth headset.
        backlog_cap = chunk * MAX_BACKLOG_BLOCKS
        prefill_target = None
        try:
            prefill_target = engine.sink.target_frames()
        except Exception:
            prefill_target = 0
        # Prime the device buffer so playback starts without an initial underrun.
        if prefill_target:
            try:
                silence = as_float_array([0.0] * chunk)
                filled = 0
                while filled < prefill_target and not engine.stop_event.is_set():
                    got = engine.sink.write(silence)
                    if got == 0:
                        break
                    filled += got
            except Exception as exc:
                engine.error = str(exc)
        next_deadline = time.monotonic()
        pending = as_float_array([])
        offset = 0  # frames of `pending` already handed to the device
        while not engine.stop_event.is_set():
            try:
                if offset >= len(pending) // CHANNELS:
                    # 一块声卡只能有一条写入路径。广播通道和点对点如果各开一个
                    # 播放端、各写各的混音，就会互相踩：广播那头没人发时写的是静音，
                    # 会把点对点整个淹没（听起来"完全没声音"），两边都有内容时
                    # 混叠失真（听起来"音质极差"）。所以这里把同一块设备上的
                    # 多条路由的混音相加，只写这一次。
                    routes = [engine.route_id] + list(getattr(engine, "extra_routes", ()) or ())
                    blocks = self.receiver.mix_by_route(frames, routes=routes)
                    parts = [blocks.get(r) for r in routes if blocks.get(r) is not None]
                    if not parts:
                        block = [0.0] * chunk
                    elif len(parts) == 1:
                        block = parts[0]
                    else:
                        block = sum(parts[1:], parts[0])
                    pending = as_float_array(block)
                    offset = 0
                    if engine.mute:
                        pending = apply_gain(pending, 0.0)
                    elif engine.gain != 1.0:
                        pending = apply_gain(pending, float(engine.gain))
                    engine.peak = max(sample_peak(pending), engine.peak * 0.85)
                    # Deliberately NOT published here. The pass-through already publishes
                    # this same mix, and two copies arriving a few milliseconds apart sum
                    # into comb filtering - which is heard as a metallic, robotic edge on
                    # every phone that joined as a speaker.
                    if len(pending) > backlog_cap:
                        # The device has stopped draining (unplugged endpoint, stalled
                        # link). Holding all of it would add unbounded latency, so
                        # skip to the newest audio instead.
                        drop = len(pending) - backlog_cap
                        drop -= drop % CHANNELS
                        pending = pending[drop:]
                        engine.dropped_frames += drop // CHANNELS
                total = len(pending) // CHANNELS
                first_pass = offset == 0
                while offset < total:
                    got = engine.sink.write(pending[offset * CHANNELS :])
                    if got <= 0:
                        break
                    offset += got
                    engine.frames_written += got
                if first_pass and offset == 0:
                    engine.underruns += 1  # the device had no room at all
            except Exception as exc:
                engine.error = "%s: %s" % (type(exc).__name__, exc)
                pending = as_float_array([])
                offset = 0
                time.sleep(0.05)
            next_deadline += frames / float(SAMPLE_RATE)
            sleep_for = next_deadline - time.monotonic()
            if offset < len(pending) // CHANNELS:
                # Still holding audio back: catch up rather than sleep the full block.
                if sleep_for > 0:
                    time.sleep(min(sleep_for, 0.002))
            elif sleep_for > 0:
                time.sleep(sleep_for)
            elif sleep_for < -0.05:
                next_deadline = time.monotonic()

    # -- status ------------------------------------------------------------
    def routes_for_sender(self, node_id: str) -> set[str]:
        """The local route ids that want audio from `node_id`.

        This is the only correct answer to "which of my sinks does this incoming
        stream belong to"; the receiver cannot know it on its own, because the
        audio packet carries no route id.
        """
        if not node_id:
            return set()
        out: set[str] = set()
        for route_id, entry in self.desired_sinks().items():
            if any(peer.node_id == node_id for peer in entry["peers"]):
                out.add(route_id)
        return out

    def _attach_stream_routes(self, info, addr) -> set[str]:
        """Work out a stream's routes, with a last-resort single-sink fallback."""
        routes = self.routes_for_sender(info.node_id)
        if not routes and len(self.sinks) == 1:
            # The sender could not be resolved to a node (a manual peer whose
            # beacon never arrived, say). With exactly one sink there is no
            # ambiguity about where its audio goes.
            routes = set(self.sinks)
        return routes

    def _reattach_stream_routes(self) -> None:
        """Re-resolve every live stream's routes after the table changed.

        A stream that arrived before its sink existed would otherwise stay
        attached to nothing for the rest of the session and never be heard.
        """
        try:
            with self.receiver._lock:  # noqa: SLF001 - receiver owns its stream table
                streams = list(self.receiver.streams.items())
            for ssrc, info in streams:
                routes = self.routes_for_sender(info.node_id)
                if not routes and len(self.sinks) == 1:
                    routes = set(self.sinks)
                if routes:
                    self.receiver.assign_routes(ssrc, routes)
        except Exception as exc:  # never let bookkeeping break reconciliation
            self.last_error = "route attach failed: %s" % exc

    def _on_stream_start(self, info, addr) -> None:
        peer = self.registry.resolve(addr[0]) if addr and addr[0] else None
        if peer is not None:
            info.node_id = peer.node_id
            info.node_name = peer.name
            # The receiver may already have created a placeholder clock keyed by
            # "ip:port"; re-key it to the node so probes and audio share one clock.
            with self.receiver._lock:  # noqa: SLF001 - receiver owns its clock table
                placeholder = self.receiver._clock.pop("%s:%d" % (addr[0], addr[1]), None)
                if placeholder is not None and peer.node_id not in self.receiver._clock:
                    placeholder.name = peer.node_id
                    self.receiver._clock[peer.node_id] = placeholder
        routes = self._attach_stream_routes(info, addr)
        if routes:
            self.receiver.assign_routes(info.ssrc, routes)
            self.note("stream %08x from %s -> route(s) %s" % (info.ssrc, addr[0] if addr else "?", ",".join(sorted(routes))))
        else:
            self.note("stream %08x from %s has no local sink yet" % (info.ssrc, addr[0] if addr else "?"))

    def peers_json(self) -> list[dict[str, Any]]:
        return self.registry.snapshot()

    def status(self) -> dict[str, Any]:
        recv = self.receiver.stats()
        return {
            "debug": getattr(self, "_local_debug", {}),
            "quality": self.quality_state(),
            "inputs": self._inputs_with_history(),
            "node": {
                "id": self.node_id,
                "short_id": short_id(self.node_id),
                "name": self.name,
                "audio_port": self.config.audio_port,
                "web_port": self.config.web_port,
                "discovery_port": self.config.discovery_port,
                "uptime_s": round(time.time() - self.started_at, 1),
                "simulate": self.simulate,
            },
            "backends": audio_base.available_backends(),
            "opus": _opus_state(),
            "devices": {
                "sources": self.source_list(),
                "sinks": self.sink_list(),
            },
            "peers": self.registry.snapshot(),
            "routes": self.routes,
            "channel": self.channel_state(),
            "local": self.local_state(),
            "senders": self.sender.stats(),
            "sinks": [engine.describe() for engine in self.sinks.values()],
            "receiver": recv,
            "totals": {
                "sent_kbps": self.sender.total_kbps(),
                "active_senders": len(self.sender.sessions),
                "active_sinks": len(self.sinks),
                "active_streams": recv.get("active_streams", 0),
            },
            "discovery": self.beacon.stats() if self.beacon is not None else {},
            "tap": self.tap.stats(),
            "log": self.log[-40:],
            "last_error": self.last_error,
            "captures": self.hub.stats(),
        }
def _opus_state() -> dict[str, Any]:
    """Opus 能不能用。打包后靠这个确认库有没有被带进去。"""
    try:
        from .audio import opus as _opus

        return {"available": bool(_opus.available()), "error": _opus.load_error()}
    except Exception as exc:
        return {"available": False, "error": "%s: %s" % (type(exc).__name__, exc)}

