'''The node engine: turns a route table into real streams, threads and audio.

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
'''

from __future__ import annotations
import json
import math
import urllib.request
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import (Any, Callable, Iterable)
from . import proto
from . import __version__
from .audio import base as audio_base
from .audio.format import (CHANNELS, SAMPLE_RATE, apply_gain, as_float_array, peak as sample_peak)
from .config import Config
from .net.discovery import (BeaconService, DiscoveryRegistry, PeerEndpoints, short_id)
from .net.rtpaudio import (JitterBuffer, Receiver, Sender, SenderSession)
from .audio.capture import CaptureHub, SILENT_AFTER_S
from .audio.multiout import MultiOut
from .audio.local import LocalLoop
from .audio.tap import AudioTap
PRUNE_AFTER_S = 60.0
NO_DATA_WARN_S = 10.0
CAPTURE_FALLBACK_COOLDOWN_S = 180.0
CAPTURE_PROBE_S = 2.0
PEER_SILENCE_GRACE_S = 12.0
AUTO_LISTEN_HANG_S = 4.0
STATS_STOP_GRACE_S = 30.0
GAIN_ECHO_WINDOW_S = 10.0
AUTO_LISTEN_LABEL = '对方请求收听（自动）'
PEER_PRUNE_TTL_S = 90.0
ROUTE_STALE_AFTER_S = 60.0
PEER_VOLUME_TTL_S = 30.0
PEER_VOLUME_DETECT_MIN_S = 10.0
PEER_VOLUME_DETECT_DELTA = 0.15
PEER_VOLUME_DETECT_CONFIRM = 2
PEER_VOLUME_AUDIBLE_MIN = 0.02
PEER_TARGETED_TTL_S = 10.0
SELF_KEYWORDS = ('self', '*self*', '@self', 'me', '本机', '自己')
CHANNEL_ROUTE_ID = 'channel'
MAX_BACKLOG_BLOCKS = 6

def is_self_token(token: str, node_id: str, name: str) -> bool:
    t = (token or "").strip().lower()
    return t in SELF_KEYWORDS or t == node_id.lower() or t == short_id(node_id) or t == name.lower()

def bt_sink_phone_name(device_name: str) -> str:
    """从蓝牙采集设备名里取出手机名（"麦克风 (<某台手机> A2DP SNK)" → "<某台手机>"）。

    手机走蓝牙把声音送进来时，Windows 把它暴露成一块**采集**设备：名字形如
    `麦克风 (<手机名> A2DP SNK)`（`engine` 里"连上了但没声音"那个坑也是它）。
    只有这种名字才回手机名 —— 别的采集设备一律返回空串：
    ★ 2026-10-18 用户（图2）「「正在发送的手机自动播放」就是蓝牙的「智连」」——
      这里就是"这张蓝牙卡片开没开「智连」"的落点，认错名字等于把别的设备也自动收编。
    """
    text = str(device_name or "").strip()
    if not text:
        return ""
    low = text.lower()
    if "a2dp" not in low:
        return ""
    start, end = text.find("(") if "(" in text else text.find("（"), text.rfind(")")
    if start < 0:
        return ""
    inner = text[start + 1:end if end > start else len(text)].strip()
    for tail in ("A2DP SNK", "A2DP Sink", "A2DP"):
        idx = inner.upper().rfind(tail.upper())
        if idx >= 0:
            inner = inner[:idx].strip()
            break
    return inner.strip(" -·")

@dataclass
class SourceState:
    spec: 'dict[str, Any]'
    source: 'Any'
    device: 'str'
    started_ns: 'float' = 0.0

def _boost_current_thread() -> None:
    """把当前线程登记成 Windows MMCSS 的「Audio」任务；不行就提高线程优先级。

    ★ 2026-10-26：渲染线程每 10ms 必须喂一次声卡，晚于声卡缓冲时长就会断音。
      普通优先级线程在编译、杀软扫描、浏览器抢 CPU 时抢不过，听感就是"一卡一卡"
      （项目里 v1.39 已经因为编译吃满 CPU 出现过同样的"咔"）。MMCSS 是 Windows
      给实时音频线程的正规通道，和设备类型无关；线程退出时系统会自动归还。
      失败时退回 THREAD_PRIORITY_HIGHEST，仍然比默认强。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        avrt = ctypes.WinDLL("avrt.dll")
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        index = wintypes.DWORD()
        if avrt.AvSetMmThreadCharacteristicsW("Audio", ctypes.byref(index)):
            return
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass

@dataclass
class SinkEngine:
    route_id: 'str'
    device_selector: 'str'
    gain: 'float' = 1.0
    route_gain: 'float' = 1.0
    media_vol: 'float' = 1.0
    media_vol_source: 'str' = ''
    volume_factor: 'float' = 1.0
    volume_note: 'str' = ''
    mute: 'bool' = False
    frames_per_block: 'int' = 480
    sink: 'Any' = None
    thread: 'threading.Thread | None' = None
    stop_event: 'threading.Event' = field(default_factory=threading.Event)
    error: 'str' = ''
    frames_written: 'int' = 0
    underruns: 'int' = 0
    dropped_frames: 'int' = 0
    started_ns: 'float' = 0.0
    peak: 'float' = 0.0
    paused: 'bool' = False
    blocks: 'int' = 0
    late_blocks: 'int' = 0
    max_block_gap_ms: 'float' = 0.0
    device_buffer_ms: 'float' = 0.0
    last_progress_ns: 'float' = 0.0
    progress_frames: 'int' = -1
    reopen_requested: 'bool' = False
    recoveries: 'int' = 0
    last_reopen_reason: 'str' = ''
    def describe(self) -> dict[str, Any]:
        device = self.sink.describe() if self.sink is not None else {'device': self.device_selector}
        return {'route_id': self.route_id, 'device_selector': self.device_selector, 'gain': self.gain, 'route_gain': self.route_gain, 'media_vol': self.media_vol, 'media_vol_source': self.media_vol_source, 'volume_factor': self.volume_factor, 'volume_note': self.volume_note, 'mute': self.mute, 'frames_written': self.frames_written, 'underruns': self.underruns, 'dropped_frames': self.dropped_frames, 'running': bool(self.thread and self.thread.is_alive()), 'error': self.error, 'peak': round(self.peak, 4), 'blocks': self.blocks, 'late_blocks': self.late_blocks, 'max_block_gap_ms': round(self.max_block_gap_ms, 1), 'device_buffer_ms': round(self.device_buffer_ms, 1), 'recoveries': self.recoveries, 'last_reopen_reason': self.last_reopen_reason, 'device': device}


class _SinkFeed:
    '''一条路由借用别人的播放端时的占位对象。

    它不进设备、不开线程，只说明"这条路由的声音由哪个播放端负责"。
    保留这个对象是必要的：路由表、监控和测试都按 route_id 找播放端，
    对象不存在会被当成"这条路由没有输出"。
    '''

    def __init__(self, route_id: str, device_selector: str, feeds_into: Any) -> None:
        self.route_id = route_id
        self.device_selector = device_selector
        self.feeds_into = feeds_into
        self.gain = 1.0
        self.route_gain = 1.0
        self.media_vol = 1.0
        self.media_vol_source = ''
        self.volume_factor = 1.0
        self.volume_note = ''
        self.mute = False
        self.peak = 0.0
        self.error = ''
        self.thread = None
        self.sink = None
        self.frames_written = 0
        self.underruns = 0
        self.dropped_frames = 0
        self.stop_event = threading.Event()
        self.started_ns = time.monotonic()

    def describe(self) -> dict[str, Any]:
        '''和 SinkEngine.describe 同样的形状。

        状态接口对 self.sinks 里的每个对象都调用它。少这个方法时，
        /api/status 会直接 500 —— 现场就是"一打开只听监控页就报错"。
        '''

        holder = getattr(self.feeds_into, 'route_id', '')
        return {'route_id': self.route_id, 'device_selector': self.device_selector, 'gain': self.gain, 'route_gain': self.route_gain, 'media_vol': self.media_vol, 'media_vol_source': self.media_vol_source, 'volume_factor': self.volume_factor, 'volume_note': self.volume_note, 'mute': self.mute, 'frames_written': self.frames_written, 'underruns': self.underruns, 'dropped_frames': self.dropped_frames, 'running': bool(getattr(self.feeds_into, 'thread', None) and self.feeds_into.thread.is_alive()), 'error': self.error, 'peak': round(self.peak, 4), 'blocks': 0, 'late_blocks': 0, 'max_block_gap_ms': 0.0, 'recoveries': 0, 'last_reopen_reason': '', 'device_buffer_ms': 0.0, 'device': {'device': self.device_selector}, 'feeds_into': holder}


def _rtt_score(rtt_ms: float) -> int:
    """往返时延折算成分：≤2ms 满分，之后每毫秒扣 0.5 分（100ms → 50，200ms → 0）。

    LAN 上正常是 1~5ms，所以健康的链路看起来基本是满的 —— 这是**对的**，
    不要为了让条子"看起来有变化"把满分线压低。
    """
    return max(0, min(100, int(round(100.0 - min(100.0, max(0.0, rtt_ms) * 0.5)))))

def _heartbeat_score(age_s: float, jitter_ms: float | None) -> int:
    """只有信标心跳可用时的分（手机不回 PING/PONG，走的就是这一条）。

    心跳本该每 ~2 秒一次：刚听到（≤2.5 秒）算满，越久没听到扣得越多
    （5 秒 → 80 分，10 秒 → 40 分，15 秒 → 0 分）；间隔忽长忽短再扣一点。
    """
    penalty = max(0.0, age_s - 2.5) * 8.0
    if jitter_ms:
        penalty += min(30.0, float(jitter_ms) * 0.5)
    return max(0, min(100, int(round(100.0 - min(100.0, penalty)))))

def _loss_score(stats: dict[str, Any]) -> int | None:
    """正在收声音那一路的丢包/缓冲分；一个包都没统计到返回 None（**不是 0**）。"""
    received = int(stats.get("packets") or 0)
    lost = int(stats.get("lost") or 0)
    if received + lost <= 0:
        return None
    penalty = (lost / float(received + lost)) * 400.0
    buffered = float(stats.get("buffered_ms") or 0.0)
    target = float(stats.get("target_ms") or 0.0)
    if target and buffered > target * 1.5:
        penalty += min(30.0, (buffered - target) * 0.5)
    return max(0, min(100, int(round(100.0 - min(100.0, penalty)))))

class _OutputLevelMeter(threading.Thread):
    '''后台直读「当前输出设备」的峰值表（见 ``wasapi.WasapiMeter``）。

    为什么是"直读"而不是再开一路回环采集：真机反馈过"同一块设备多开一路采集会越听越卡"
    （特别是蓝牙耳机），而手机走蓝牙进来时声音是 **Windows 直接播放**的，本工具那一路
    根本没有数据 —— 面板上的"本机播放电平"于是永远是 0。``IAudioMeterInformation`` 是
    音频引擎自带的峰值表，读一次 float 就有数，不占设备、不打扰正在播放的声音。

    带一点衰减记忆：峰值表的窗口是 100ms，读到的值在 0 和峰值之间跳得厉害，直接显示会
    变成一堆看不清的数字。
    '''

    def __init__(self, spec: dict[str, Any], *, interval: float = 0.1) -> None:
        super().__init__(daemon=True, name="audiomixing-outmeter")
        self.spec = dict(spec)
        self.interval = float(interval)
        self.peak = 0.0
        self.device = str(self.spec.get("device") or "@default")
        self.error = ""
        self._stop = threading.Event()
        self._meter: Any = None

    def run(self) -> None:
        from .audio.backends.wasapi import WasapiMeter

        self._meter = WasapiMeter(self.spec)
        if self._meter.open():
            self.device = self._meter.device()
        else:
            self.error = self._meter.error
        misses = 0
        while not self._stop.is_set():
            value = self._meter.peak()
            if value is None:
                # 接口失效（设备换了一块/被独占/驱动重启）：隔三秒重开一次，
                # 而不是每 100ms 都去撞一次失败的 COM 调用。
                misses += 1
                if misses >= 30:
                    misses = 0
                    if self._meter.open():
                        self.device = self._meter.device()
                        self.error = ""
                    else:
                        self.error = self._meter.error
            else:
                misses = 0
                self.peak = max(float(value), self.peak * 0.82)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._meter is not None:
            try:
                self._meter.close()
            except Exception:
                pass


class AudioMixingNode:
    '''One AudioMixing instance on one machine.'''

    ''
    def __init__(self, config: Config | None=None, *, audio_port: int | None=None, discovery_port: int | None=None, bind_host: str='0.0.0.0', simulate: bool=False) -> None:
        self.config = config or Config.load()
        self.bind_host = bind_host
        self.simulate = simulate
        if audio_port:
            self.config.audio_port = audio_port
        if discovery_port:
            self.config.discovery_port = discovery_port
        self.node_id = self.config.node_id
        self.device_id = str(getattr(self.config, 'device_id', '') or self.config.node_id)
        self.instance_id = str(getattr(self.config, 'instance_id', '') or '')
        self.origin_id = proto.origin_id_of(self.device_id)
        self.name = self.config.name
        self.registry = DiscoveryRegistry(self.node_id)
        self.sender = Sender(self.node_id, self.name)
        self.receiver = Receiver(self.node_id, self.config.audio_port, on_first_packet=self._on_stream_start, accept_source=self._stream_is_accepted, resolve_source=self._node_id_for_host, bind_host=bind_host, self_origin_id=self.origin_id)
        self.sinks = {}
        self.hub = CaptureHub()
        self.tap = AudioTap(name='out')
        self._loop = None
        self._loop_key = None
        self._local_note = ''
        self._out_meter = None
        self._silent_captures = {}
        self._local_logged = ''
        self._last_capture_switch = None
        self._capture_health = {}
        self._probe_name = None
        self._probe_started = 0.0
        self._probe_result = None
        self._last_fallback_at = 0.0
        self._dead_since = {}
        self._peer_seen = {}
        self._senders_blocked = {}
        self._auto_listen_granted = {}
        self._auto_listen_hangs = {}
        self._peer_listen_ack = {}
        self._listen_req_seq = 0
        self._listen_req_state = {}
        self._auto_stop_requested = {}
        self._pending_auto_listen = {}
        self._pending_auto_lock = threading.Lock()
        self._gain_local_at = {}
        self._handled_stats_at = 0.0
        self._last_peer_stats = 0.0
        self._stats_last_keys = set()
        self._stats_seen = {}
        self._stats_grace = {}
        self._bt_requests = {}
        self._bt_request_at = {}
        self._gain_ts = {}
        self._gain_published = {}
        self.routes = []
        self._routes_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._wake = threading.Event()
        self.multi_out = MultiOut(self.hub)
        self._dropped_ids = {}
        self._route_first_seen = {}
        self._last_orphan_sweep = 0.0
        self._last_auto_sweep = 0.0
        self._last_peer_prune = 0.0
        self._unknown_route_since = {}
        self._stale_route_ids = set()
        self._stale_route_missing_s = {}
        # ★ 2026-11-04（第 1 条）：每条路由的三态 —— ok / no_data / offline / missing。
        #   只有 missing（对端**确实不存在**）才画虚线、才进「清理失效路由」。
        self._route_states = {}
        self._peer_volume = {}
        self._peer_vol_last = {}
        # ★ 2026-11-06（第 2 条）：每台对端最近 12 次 `vol` 的原始数值序列。
        #   用户实测"手机视频 App 调音量，声音纹丝不动"，光看一个瞬时值分辨不出
        #   "手机没上报变化"还是"电脑没跟着变"。把序列留在状态接口里，点一下「诊断」
        #   就能看到 vol 到底动没动 —— 这是数值证据，不是推断。
        self._peer_vol_hist = {}
        self._identity_rewrites = []
        self._peer_send_mode = {}
        self._peer_vol_trace = {}
        self._peer_vol_verdict = {}
        self._peer_vol_pending = {}
        # ★ v2.27（Bug-1）：手机自己声明"采集点已经在音量之后"。声明优先于自动判定，
        #   因为 auto 在 vol 长期不变时没有样本，会退回"叠加"，把中间档压成平方衰减。
        self._peer_vol_declared = {}
        # ★ 2026-11-08：手机声明的"音量乘数由档位决定"。
        #   vol_declared = 手机已经处理过这一级（外放档乘系统音量 / 静音档不叠加），PC 恒不叠乘；
        #   vol_source   = external / silent（只用于把中文说明写准确）；
        #   vol_policy   = 静音档乘数策略（internal_only / last_nonzero）。
        self._peer_vol_source = {}
        self._peer_vol_policy = {}
        # 电脑切换"静音档乘数策略"时的请求号（单调递增；手机按它只处理一次）。
        self._silent_policy_seq = 0
        self._shared_sinks = {}
        self.beacon = None
        self.devices = []
        self.devices_refreshed_at = 0.0
        self.started_at = time.time()
        self.log = []
        self.last_reconcile_at = 0.0
        self.last_error = ''
        self.render_frames = 480

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
        elif not self._selector_still_exists(selector):
            # 配置里记着的那块设备现在可能已经不在了（拔掉、断开、改名、驱动重装），
            # 这与设备类型无关 —— 蓝牙、USB、板载、虚拟声卡都会发生。
            # 以前这里会拿着这个不存在的名字反复去开：采集端每次都 start failed，
            # 一个字节都产不出来，对端永远收不到声音，而界面上只看到"没有音频数据"。
            # 现在退回"当前真实可用的输出"，并在日志里说清发生了什么、怎么固定回去。
            picked = self._default_render_name() or self._best_loopback_selector()
            if picked and not self._same_endpoint(picked, selector):
                self._note_once(
                    "音源「%s」现在在这台电脑上找不到了，已临时改用「%s」。"
                    "要让程序固定用它，请把该设备接回来；否则请在控制台的音源清单里"
                    "改成你现在正在使用的那一块。" % (selector, picked))
                selector = picked
        # 返回单个规格字典（调用方要的是 dict，不是 list）。
        # 选的是麦克风就按输入打开，选的是输出设备才采它的回环。
        return {"backend": "wasapi", "device": selector,
                "loopback": self._selector_is_loopback(selector)}

    def _selector_still_exists(self, selector: str) -> bool:
        """这个名字现在还能在这台电脑上找到吗（输入或输出都算）。

        只做"存不存在"的事实判断，不看设备类型。
        """
        name = str(selector or "").strip()
        if not name:
            return True
        try:
            for item in self.devices:
                if self._same_endpoint(str(getattr(item, "name", "") or ""), name):
                    return True
        except Exception:
            # 设备枚举失败时不要因此把用户的设置换掉
            return True
        return False

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

    def _bt_names(self) -> list[str]:
        """我用蓝牙能收谁的音（名字列表）—— 交给发现信标，手机端据此决定要不要问用户。

        用户原话（2026-10-15）：「手机界面点击送声音后，应当判断手机是否在电脑的蓝牙
        设备列表里。如果是就让用户判断到底用蓝牙还是 WiFi。」

        这里**只读缓存**：列设备要跑 3 秒 PowerShell，而信标每 2 秒发一次
        （见 bluetooth.cached_names 的说明）。取不到就返回空表 —— 手机端照旧走 WiFi。
        """
        try:
            from . import bluetooth as bt

            return bt.cached_names()
        except Exception:
            return []

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

    def start(self, *, announce: bool=True) -> None:
        '''Bring the node up: receiver, device list, discovery, background loops.

        `announce=False` keeps everything off the network, which is what the tests use.
        '''

        self.receiver.start()
        self.refresh_devices()
        if self.config.routes:
            try:
                pass
            except Exception as exc:
                self.note('恢复上次的路由失败（已跳过）：%s' % exc)
            try:
                self.set_routes([dict(r) for r in self.config.routes if isinstance(r, dict)], persist=False)
            except Exception as exc:
                self.note('恢复上次的路由失败（已跳过）：%s' % exc)
        if announce:
            self.beacon = BeaconService(self.registry, self.node_id, self.name, self.config.audio_port, self.config.web_port, self.source_list, self.sink_list, port=self.config.discovery_port, version=__version__, bt_provider=self._bt_names, device_id=self.device_id, instance_id=self.instance_id)
            for peer in self.config.manual_peers:
                host = str(peer.get('host') or '')
                if not host:
                    continue
                self.beacon.add_manual_target(host, int(peer.get('port') or 0) or None)
                self.registry.add_manual(host, int(peer.get('port') or 0) or self.config.audio_port, str(peer.get('name') or ''))
            self.beacon.start()
        self.note('engine started on node %s (%s), audio port %d' % (short_id(self.node_id), self.name, self.config.audio_port))
        for target, thread_name in (self._maintenance_loop, 'audiomixing-maintenance'), (self._ping_loop, 'audiomixing-ping'):
            worker = threading.Thread(target=target, name=thread_name, daemon=True)
            self._threads.append(worker)
            worker.start()
        if self.config.autoconnect:
            try:
                self._autoconnect(list(self.config.autoconnect))
            except Exception as exc:
                self.note('自启连接失败：%s' % exc)
        self.apply_routes()

    def stop(self) -> None:
        self._stop.set()
        if self._out_meter is not None:
            self._out_meter.stop()
            self._out_meter = None
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
        self._apply_multi_out()
        try:
            self._sweep_dead_senders()
        except Exception as exc:
            self._note_once('残留清扫出错（已忽略）：%s' % exc)
        if time.monotonic() - self._last_orphan_sweep >= self.ORPHAN_SWEEP_INTERVAL_S:
            self._last_orphan_sweep = time.monotonic()
            try:
                self._sweep_orphan_routes()
            except Exception as exc:
                self._note_once('孤儿路由清扫出错（已忽略）：%s' % exc)
        if time.monotonic() - self._last_auto_sweep >= self.AUTO_ROUTE_SWEEP_INTERVAL_S:
            self._last_auto_sweep = time.monotonic()
            try:
                self._sweep_unwanted_auto_routes()
            except Exception as exc:
                self._note_once('自动路由清扫出错（已忽略）：%s' % exc)
        if time.monotonic() - self._last_peer_prune >= 5.0:
            self._last_peer_prune = time.monotonic()
            try:
                removed = self.registry.prune(PEER_PRUNE_TTL_S)
                if removed:
                    self.note('清掉 %d 台 90s 没有音讯的历史设备（只是不再显示，路由保留）' % len(removed))
                # ★ v2.26 第 1 条：先按 identity 归一改写旧 id 的路由，再判三态。
                self.normalize_route_identities()
                self._refresh_route_staleness()
            except Exception as exc:
                self._note_once('设备/路由清理出错（已忽略）：%s' % exc)

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

    DROPPED_TTL = 120.0
    ORPHAN_GRACE_S = 180.0
    ORPHAN_SWEEP_INTERVAL_S = 30.0
    AUTO_ROUTE_SWEEP_INTERVAL_S = 2.0
    def _sweep_orphan_routes(self) -> None:
        """清掉"整条路由都是在说一台这台电脑从来没见过的设备"的路由。

        ★ 2026-10-19（用户连着两次反馈）：他在面板上看到一堆 `preview-…`「不在线 / 不可达」
          的节点和红线 —— 那是我为了改功能在同一台电脑上起的**临时测试节点**留下的路由。
          路由表是全网共享的（任何面板都能改，见 set_routes），被合进某台机器的内存表之后
          就**再没人清**：它们的作者早就不在了，而两台机器还会互相把这张表推来推去，
          于是删掉几秒后又回来（实测：本机删掉 8 条，另一台电脑还留着 7 条；只删一边没用）。

        判据刻意选得**保守**，正常路由（对方只是关机了）不受任何影响：
          · 注册表里的条目在本进程内**不会被删**（掉线只是把 online 置成 False），所以
            "注册表里查不到"就等于"这台电脑从头到尾没见过这台设备"；
          · 只要作者、或任意一端是本机自己 / 广播星号 / 注册表里认识的设备，就不算孤儿；
          · 还有 ORPHAN_GRACE_S 宽限。
        清掉时带上 drop 名单，对端在 TTL 内再推同样的 id 也会被挡住（不会来回打架）。
        """
        try:
            alive = {str(r.get("id") or "") for r in self.routes if str(r.get("id") or "")}
            for rid in list(self._route_first_seen):
                if rid not in alive:
                    self._route_first_seen.pop(rid, None)
            now = time.monotonic()
            orphans: list[str] = []
            for rule in self.routes:
                rid = str(rule.get("id") or "")
                if not rid:
                    continue
                first = self._route_first_seen.setdefault(rid, now)
                if now - first < self.ORPHAN_GRACE_S:
                    continue
                if self._route_is_orphan(rule):
                    orphans.append(rid)
            if not orphans:
                return
            gone = set(orphans)
            self.set_routes([r for r in self.routes if str(r.get("id") or "") not in gone],
                            drop=orphans)
            self._note_once("清掉了 %d 条「整条路由都是没见过的设备」的残留路由"
                            "（多半是别人测试留下的）" % len(orphans))
        except Exception as exc:
            self._note_once("孤儿路由清扫出错（已忽略）：%s" % exc)

    def _route_is_orphan(self, rule: dict[str, Any]) -> bool:
        '''这条路由说的设备（作者 + 两端），这台电脑是不是**一个都没见过**？'''

        tokens = [str(t) for t in rule.get('from') or []] + [str(t) for t in rule.get('to') or []]
        try:
            for token in tokens:
                if not token or token == '*':
                    continue
                if is_self_token(token, self.node_id, self.name):
                    return False
                if self.registry.get(token) is not None:
                    return False
                if self.registry.ever_seen(token):
                    return False
        except Exception:
            return False
        return True

    def _route_has_traffic(self, rid: str) -> bool:
        '''这条路由现在有没有音频在流（判断 no_data 用）。

        "静音"也算有流：只要收/发两端还在出包，`last_packet` 就一直在刷新，
        和"用户把音量拖到 0"无关 —— 这正是为什么不能用峰值判断。
        '''
        rid = str(rid or '')
        if not rid:
            return False
        try:
            for item in self.receiver.stats().get('streams') or []:
                ids = {p for p in str(item.get('route_id') or '').split(',') if p}
                if rid in ids and float(item.get('age_s') or 999.0) < NO_DATA_WARN_S:
                    return True
            for item in self.sender.stats() or []:
                if str(item.get('route_id') or '') == rid and bool(item.get('sending')):
                    return True
        except Exception:
            return False
        return False

    def _canonical_peer_id(self, peer: Any) -> str:
        '''对端的"归一后 identity"：优先 device_id，老端没有就退回 node_id。

        ★ 2026-11-04（v2.26 第 1 条）：路由里可能写着手机**旧身份**（b1fbc20c / 5e65addb），
          而列表里那台手机只报 e9b8cc51。只要注册表能按旧 id 精确命中（aliasIds），
          就说明它是同一台设备的旧身份，可以安全改写。
        '''
        if peer is None:
            return ''
        return str(getattr(peer, 'device_id', '') or getattr(peer, 'node_id', '') or '')

    def normalize_route_identities(self) -> dict[str, Any]:
        '''把路由里能**精确解析**到的旧 id 改写成对端归一后的 device_id。

        ★ 2026-11-04（v2.26 第 1 条）用户诊断包：
          id=68c622a3 / id=4eecefd4 两条路由仍指向 b1fbc20c、5e65addb —— 这两条是
          同一台手机（e9b8cc51，host 192.168.0.100）的旧身份。关系图上一轮已经不再画
          它们，但监控页/路由表还在，并且两条都只会空转。根因是**路由 token 没跟随
          identity 合并改写**，解析虽然靠 aliasIds 命中，但界面仍拿旧 id 显示。

        这里只在**强证据**下改写：`registry.get(token)` 是精确命中（node_id / device_id /
        aliasIds），不是名称模糊匹配。命中后：
          · 把 token 改成对端的 device_id（老端没有 device_id 就用 node_id）；
          · 改写后如果两条本地路由变得完全一样（from/to/增益/采集/输出都相同），
            只保留第一条，其余记进 `identity_rewrites` —— 否则同一台手机会收到两份声音；
          · 改写前备份 config.json，改写只落盘**本机自己的**路由，不碰对端推送的。

        无强证据（解析不到）的 token 原样保留，继续由 `_refresh_route_staleness`
        标 missing，交给用户在监控页一键清理。
        '''
        changes: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        with self._routes_lock:
            for rule in list(self.routes):
                # 只改**本机自己的**路由。对端推送的路由每一轮都可能被重新下发，
                # 在这里改它只会打日志、下一轮又变回去；显示层用 identity 归一即可。
                if str(rule.get('origin') or '') not in ('', self.node_id):
                    continue
                for key in ('from', 'to'):
                    tokens = rule.get(key)
                    if not isinstance(tokens, (list, tuple)):
                        continue
                    new_tokens = list(tokens)
                    for index, raw in enumerate(list(tokens)):
                        token = str(raw or '').strip()
                        if not token or token == '*' or is_self_token(token, self.node_id, self.name):
                            continue
                        peer = self.registry.get(token)
                        canonical = self._canonical_peer_id(peer)
                        if not canonical or canonical.lower() == token.lower():
                            continue
                        new_tokens[index] = canonical
                        changes.append({
                            'route_id': str(rule.get('id') or ''),
                            'field': key,
                            'old': token,
                            'new': canonical,
                            'peer': str(getattr(peer, 'node_id', '') or ''),
                            'name': str(getattr(peer, 'name', '') or ''),
                            'host': str(getattr(peer, 'host', '') or ''),
                        })
                    if new_tokens != list(tokens):
                        rule[key] = new_tokens
            if not changes:
                self._identity_rewrites = list(getattr(self, '_identity_rewrites', []))[-32:]
                return {'ok': True, 'changed': 0, 'changes': []}
            # 改写后可能产生"同一对端、同一源/输出"的重复路由。只对**本机自己的**路由去重，
            # 对端推送的路由不删（下一次推送会自己修正）。
            seen: dict[tuple[Any, ...], str] = {}
            for rule in list(self.routes):
                if str(rule.get('origin') or '') not in ('', self.node_id):
                    continue
                signature = self._route_identity_signature(rule)
                rid = str(rule.get('id') or '')
                if signature not in seen:
                    seen[signature] = rid
                    continue
                self.routes.remove(rule)
                merged.append({'route_id': rid, 'kept': seen[signature], 'reason': '旧身份改写后与已有路由完全相同'})
        self.config.routes = [r for r in self.routes
                              if str(r.get('origin') or '') in ('', self.node_id)]
        self._persist_identity_rewrites()
        log = list(getattr(self, '_identity_rewrites', []))[-32:] + changes + merged
        self._identity_rewrites = log[-64:]
        self.note('路由身份归一：改写 %d 处旧 id，合并 %d 条重复路由（已备份 config.json）'
                  % (len(changes), len(merged)))
        return {'ok': True, 'changed': len(changes), 'merged': len(merged),
                'changes': changes, 'merged_routes': merged}

    def repair_route_identity(self, route_id: str, target_id: str = '') -> dict[str, Any]:
        '''一键把一条失效路由的旧 id 改成当前对端的 device_id。

        自动改写只在**精确命中**（aliasIds / device_id）时发生；如果旧 id 在本次进程里
        已经解析不到（例如手机重装后换了 node_id，旧 alias 没有持久化），监控页会给
        一个"一键修复"按钮。点它 = 用户确认"就是这台"，属于人工强证据，允许改写。
        没传 target_id 时只在"当前恰好只有一台在线对端"时猜它，绝不批量乱改。
        '''
        with self._routes_lock:
            rule = next((r for r in self.routes
                         if str(r.get('id') or '') == str(route_id or '')), None)
            if rule is None:
                return {'ok': False, 'error': '找不到这条路由（可能已被清理）'}
            peer = self.registry.get(target_id) if target_id else None
            if peer is None:
                candidates = [p for p in self.registry.all()
                              if getattr(p, 'online', False) and not getattr(p, 'manual', False)]
                unique = {self._canonical_peer_id(p): p for p in candidates}
                if len(unique) != 1:
                    return {'ok': False,
                            'error': '当前有 %d 台在线对端，无法确定改指向哪一台；请先连上目标设备' % len(unique)}
                peer = next(iter(unique.values()))
            canonical = self._canonical_peer_id(peer)
            if not canonical:
                return {'ok': False, 'error': '目标设备没有可用的 identity'}
            changed = 0
            for key in ('from', 'to'):
                tokens = list(rule.get(key) or [])
                for index, raw in enumerate(tokens):
                    token = str(raw or '').strip()
                    if not token or token == '*' or is_self_token(token, self.node_id, self.name):
                        continue
                    if self.registry.get(token) is not None:
                        continue
                    tokens[index] = canonical
                    changed += 1
                if tokens:
                    rule[key] = tokens
            if not changed:
                return {'ok': False, 'error': '这条路由没有需要修复的旧 id'}
        self._identity_rewrites = list(getattr(self, '_identity_rewrites', []))[-63:] + [{
            'route_id': str(route_id), 'field': 'manual-repair',
            'old': '旧身份', 'new': canonical,
            'name': str(getattr(peer, 'name', '') or ''),
            'host': str(getattr(peer, 'host', '') or ''),
        }]
        self._persist_identity_rewrites()
        # 改写后可能又出现重复路由，交给同一套归一去重（不会产生新的自动改写，除非还能命中）。
        self.normalize_route_identities()
        self.note('已按你的确认把路由 %s 改指向「%s」（%s），配置已备份'
                  % (str(route_id)[:8], getattr(peer, 'name', '') or '?', canonical))
        return {'ok': True, 'target': canonical, 'name': str(getattr(peer, 'name', '') or ''), 'changed': changed}

    def _route_identity_signature(self, rule: dict[str, Any]) -> tuple[Any, ...]:
        '''判断两条路由是不是"同一条"（忽略 id / 归属 / 创建时间等身份字段）。'''

        def items(value: Any) -> tuple[str, ...]:
            if isinstance(value, (list, tuple)):
                return tuple(str(x or '') for x in value)
            return (str(value or ''),)

        return (items(rule.get('from')), items(rule.get('to')),
                round(float(rule.get('gain', 1.0) or 0.0), 3),
                bool(rule.get('mute', False)),
                str(rule.get('source_device') or ''),
                str(rule.get('sink_device') or ''),
                str(rule.get('codec') or ''))

    def _persist_identity_rewrites(self) -> None:
        '''改写路由前先备份 config.json，再只保存本机自己的路由。'''

        try:
            from .config import _backup_before_migration, config_path

            path = config_path()
            if path.is_file():
                _backup_before_migration(path)
        except Exception as exc:
            # 不吞异常：备份失败就不落盘（内存里的改写仍然有效，下次启动旧 id 还会再来）。
            self.note('身份归一：改写前备份 config.json 失败，本次不改写配置文件：%s' % exc)
            return
        try:
            self.config.routes = [r for r in self.routes
                                  if str(r.get('origin') or '') in ('', self.node_id)]
            self.config.save()
        except OSError as exc:
            self.note('身份归一：保存 config.json 失败（路由已在内存中生效）：%s' % exc)

    def _refresh_route_staleness(self) -> None:
        '''给每条路由判三态（只影响界面，不删配置）。

        ★ 2026-11-04（第 1 条回退）：上一版把"端点当前一个都不在线"一律当 stale，
          结果把**真设备只是关机/换网/暂时没数据**的活路由也画成了虚线 ——
          用户现场看到的"虚线节点反而更多"就是这个判定过宽。
        现在分三种，含义严格区分：
          · `ok`       —— 两端在线，而且这条路由现在有音频在流；
          · `no_data`  —— 两端在线，但这条流暂时没数据：**保持实线**，只标注；
          · `offline`  —— 对端是**见过面的真设备**，只是现在不在线：实线 + 离线标注，
                          **不算失效**（它随时会回来，删了就是丢用户配置）；
          · `missing`  —— 对端身份**从来没在这台电脑上出现过**（旧 id / 脏配置 / 重装换了 id），
                          连续 60 秒都解析不到 → 才画虚线，才进「清理失效路由」。
        判定只看"身份是否真的不存在"，不看"现在有没有声音"——后者是 `no_data`。
        '''

        now = time.monotonic()
        stale = set()
        missing_s = {}
        states = {}
        try:
            for rule in list(self.routes):
                rid = str(rule.get('id') or '')
                if not rid:
                    continue
                tokens = [str(t) for t in rule.get('from') or []] + [str(t) for t in rule.get('to') or []]
                unknown = False
                offline = False
                remote = 0
                for token in tokens:
                    if not token or token == '*':
                        continue
                    if is_self_token(token, self.node_id, self.name):
                        # 本机不是"对端"：它永远在线，不能拿来掩盖对端掉线这件事。
                        continue
                    if self._is_manual_peer(token):
                        # 用户手写的地址：本机配置的一部分，不能因为"没看到信标"就当不存在。
                        remote += 1
                        continue
                    remote += 1
                    peer = self.registry.resolve(token)
                    if peer is not None:
                        if not (peer.online or peer.local):
                            offline = True
                        continue
                    if self.registry.ever_seen(token):
                        offline = True
                    else:
                        unknown = True
                if unknown:
                    since = self._unknown_route_since.setdefault(rid, now)
                    age = now - since
                    if age >= ROUTE_STALE_AFTER_S:
                        stale.add(rid)
                        missing_s[rid] = round(age, 1)
                        states[rid] = 'missing'
                    else:
                        # 刚消失不到 60 秒：先按"等待对端"处理，**不**画虚线。
                        states[rid] = 'offline'
                    continue
                self._unknown_route_since.pop(rid, None)
                if offline:
                    states[rid] = 'offline'
                elif not remote:
                    # 只有本机 / 占位符的路由：没有对端可判，按"暂时没数据"呈现。
                    states[rid] = 'no_data'
                else:
                    states[rid] = 'ok' if self._route_has_traffic(rid) else 'no_data'
        except Exception:
            return
        live_ids = {str(r.get('id') or '') for r in self.routes}
        for rid in list(self._unknown_route_since):
            if rid not in live_ids:
                self._unknown_route_since.pop(rid, None)
        self._stale_route_ids = stale
        self._stale_route_missing_s = missing_s
        self._route_states = states

    def purge_stale_routes(self) -> dict[str, Any]:
        '''一键清掉**已经标失效**的路由（界面「清理失效路由」按钮）。

        为什么只删 stale：真实设备只是关机/换网时，路由先标失效（图上消失、配置仍在）；
        用户确认"这些设备我确实不再用了"才点这个按钮，删掉的就是这些条目，
        并记进删除名单挡住对端再推回来。**不会**碰任何当前在线的路由。
        '''

        stale = sorted(self._stale_route_ids)
        if not stale:
            return {'ok': True, 'removed': [], 'count': 0}
        gone = set(stale)
        self.set_routes([r for r in self.routes if str(r.get('id') or '') not in gone], drop=stale)
        self._stale_route_ids = set()
        self._stale_route_missing_s = {}
        self.note('已清理 %d 条失效路由（端点长时间不存在）' % len(stale))
        return {'ok': True, 'removed': stale, 'count': len(stale)}

    def _is_manual_peer(self, token: str) -> bool:
        text = str(token or '').strip().lower()
        if not text:
            return False
        for item in getattr(self.config, 'manual_peers', None) or []:
            if isinstance(item, dict):
                keys = item.get('node_id'), item.get('name'), item.get('host')
                if any((str(k or '').strip().lower() == text for k in keys)):
                    return True
        return False

    def _purge_dropped(self) -> None:
        """把过期的"已删除路由"记录清掉，让同名路由有机会重新进来。"""
        try:
            now = time.time()
            for key, stamp in list(self._dropped_ids.items()):
                if now - float(stamp or 0.0) > self.DROPPED_TTL:
                    self._dropped_ids.pop(key, None)
        except Exception:
            pass

    def _canonical_route_token(self, token: str) -> str:
        """把一个端点 token 归一到"真实身份"再比较：别名/旧 id/IP 和真 id 等价。"""
        text = str(token or "").strip()
        if not text or text == "*":
            return text
        if is_self_token(text, self.node_id, self.name):
            return self.node_id
        try:
            peer = self.registry.get(text)
            if peer is not None and str(getattr(peer, "node_id", "")):
                return str(peer.node_id)
        except Exception:
            pass
        return text

    def _route_equivalence_key(self, rule: dict[str, Any]) -> tuple:
        """同一对端点 + 同一采集/输出设备 = 同一条路由（重复的那条只会把声音播两遍）。"""
        frm = frozenset(self._canonical_route_token(t) for t in (rule.get("from") or []))
        to = frozenset(self._canonical_route_token(t) for t in (rule.get("to") or []))
        return (frm, to, str(rule.get("sink_device") or ""), str(rule.get("source_device") or ""))

    def _dedupe_routes(self, table: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """★ v2.27（Bug-3）把语义重复的路由收敛成一条。

        "收听它"要点两次才取消、开「只听」后声音变嘈杂，根因都是同一对端点上有
        两条路由（旧别名一条 + 当前 id 一条，或自动收听一条 + 手工一条）：两条都挂
        同一个流就会叠着播两遍。这里只按"端点 + 设备"判重，保留手工路由，不动增益。
        """
        out: list[dict[str, Any]] = []
        index: dict[tuple, int] = {}
        for rule in table:
            if not isinstance(rule, dict):
                continue
            key = self._route_equivalence_key(rule)
            slot = index.get(key)
            if slot is None:
                index[key] = len(out)
                out.append(rule)
                continue
            old = out[slot]
            # 手工建的优先于自动收听/自动加入；同为手工时保留先到的那条（id 稳定）。
            if bool(old.get("auto")) and not bool(rule.get("auto")):
                out[slot] = rule
        return out

    def set_routes(
        self,
        routes: list[dict[str, Any]],
        persist: bool = True,
        author_id: str | None = None,
        author_name: str = "",
        drop: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        _drop_now = time.time()
        for _x in drop:
            self._dropped_ids[str(_x)] = _drop_now
        self._purge_dropped()
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
        # ★ 保留已存在路由的"归属"。
        # 集合里的归属决定"谁的推送有资格替换它"：下面 kept 会把 origin == 推送者
        # 的记录全部丢掉、用推送内容替代。而面板（update_route / set_routes）
        # 提交的是【整张表】，_normalize_route 会把表里每一条的归属都写成提交者 ——
        # 结果：甲机建的路由，被乙机的某次保存改成"归属 乙机"，
        # 而乙机推送自己表时并不包含它 → 这条路被当成"作者撤回了"而删除 ✗
        # → 发送会话还在、字节 0、界面显示"残留"（2026-10-11 定位）。
        # 所以：id 已经在表里的路由，归属一律沿用旧值，不因为谁提交而改变。
        # （这与具体机器无关：任何两台机器互相保存路由时都会遇到。）
        try:
            origin_by_id = {str(r.get("id") or ""): r.get("origin") for r in (self.routes or [])}
        except Exception:
            origin_by_id = {}
        if origin_by_id:
            for r in incoming:
                old = origin_by_id.get(str(r.get("id") or ""))
                if old:
                    r["origin"] = old
        dropped = {str(x) for x in drop}
        # 刚被显式删掉的路由，短时间内在【所有入口】都不许复活。
        # 以前只在"从对端拉取"那条路上挡，结果面板（浏览器里那份旧表）下一次
        # 自动保存就把删掉的路由又 PUT 回来了 —— 两边来回删/加，界面显示"残留"，
        # 音频链路反复重建，现场表现就是"一直不响"（2026-10-11 定位）。
        self._purge_dropped()
        if self._dropped_ids:
            blocked = set(self._dropped_ids)
            dropped = dropped | blocked
            incoming = [r for r in incoming if str(r.get("id") or "") not in blocked]
        # ★ v2.27（Bug-3）：删一条 = 删掉"语义相同的全部副本"。
        #   以前旧别名路由和当前 id 路由各占一条，面板点一次只删中一条 → 要点两次。
        if dropped:
            keys = {self._route_equivalence_key(r) for r in (self.routes or [])
                    if str(r.get("id") or "") in dropped}
            if keys:
                for r in (self.routes or []):
                    if str(r.get("id") or "") in dropped:
                        continue
                    if self._route_equivalence_key(r) in keys:
                        dropped = dropped | {str(r.get("id") or "")}

        with self._routes_lock:
            incoming_ids = {r["id"] for r in incoming}
            # Drop this author's previous routes (the push is absolute, not a delta),
            # anything explicitly dropped, and any local copy of an id that just came
            # back to us, then append.
            kept = [
                r for r in self.routes
                if r.get("origin") != author and r["id"] not in incoming_ids and r["id"] not in dropped
            ]
            merged = self._dedupe_routes(kept + incoming)
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
        # ★ 对端要**立刻**知道本机刚改了路由表 / 音量（见 push_peer_stats_now 的说明：
        #   用户反馈的"调音量同步很慢""跨端自动启停很慢"就是这里等 2 秒造成的）。
        self.push_peer_stats_now()
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
                # 能问到它的状态页 = 它确实还活着。这条证据比信标更硬。
                self._note_peer_seen(host, real_id or peer_id)
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
            # 已经被显式删掉的路由短时间内不要再拉回来（压住来回打架的推送），
            # 但【不能永久】：过了有效期就允许重新拉取，否则对端重建同名路由
            # 也永远回不来，现场表现就是"对端一直在发、本机永远不响"。
            self._purge_dropped()
            kept = [r for r in routes
                    if str(r.get("id") or "") not in self._dropped_ids]
            if not kept:
                continue
            routes = kept
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

    def _link_routes(self) -> list[dict[str, Any]]:
        """Every row that can describe one of *my* links, including rows a peer authored.

        `local_routes()` answers "which rows may this panel edit" (origin == me).
        A route's `origin` is **who wrote it**, not **who it connects**. Smart-link
        auto-listen rows are written with `origin = peer node id`, so using
        `local_routes()` for gain sync / stats targets silently skipped the very
        link the user was dragging: PC showed 1.60 while the phone showed 0.40
        (2026-11-02). Link facts must be read from the whole table.
        """
        with self._routes_lock:
            return [dict(r) for r in self.routes]

    def _normalize_route(self, raw: dict[str, Any], author_id: str='', author_name: str='') -> dict[str, Any]:
        '''Canonical route dict, with every self-reference resolved to a node id.

        ``self`` / 本机 are *relative*: they mean "the node that wrote this route".
        Shipping them over the wire and letting the reader resolve them against
        itself silently turns "play A's sound on B" into "play B's sound on B",
        which no node then acts on — the audio simply never starts. Resolving
        against the author once, here, makes a route mean the same thing
        everywhere.
        '''

        def as_list(value) -> list[str]:
            if value is None or value == '':
                return ['*']
            if isinstance(value, str):
                return [value]
            return [str(v) for v in value]

        def resolve(tokens: list[str]) -> list[str]:
            if not author_id:
                return tokens
            out = []
            for token in tokens:
                if is_self_token(token, author_id, author_name):
                    out.append(author_id)
                else:
                    out.append(token)
            return out

        def as_gain(value) -> float:
            try:
                gain = float(value)
            except (TypeError, ValueError):
                return 1.0
            if not math.isfinite(gain):
                return 1.0
            return max(0.0, min(gain, 8.0))

        def as_text(value) -> str:
            if value not in (None, ''):
                return str(value)
            return ''

        origin = str(raw.get('origin') or '') or author_id or self.node_id
        return {'id': str(raw.get('id') or uuid.uuid4().hex[:8]), 'from': resolve(as_list(raw.get('from'))), 'to': resolve(as_list(raw.get('to'))), 'gain': as_gain(raw.get('gain', 1.0)), 'mute': bool(raw.get('mute', False)), 'codec': str(raw.get('codec') or self.config.codec), 'source_device': as_text(raw.get('source_device')), 'sink_device': as_text(raw.get('sink_device')), 'label': str(raw.get('label') or ''), 'origin': origin, 'auto': bool(raw.get('auto', False))}

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

    def _live_alias(self, peer: PeerEndpoints) -> PeerEndpoints | None:
        '''同 IP 上还有另一条在线的记录吗？（重复身份时优先用它，第 2 条）'''

        try:
            peers = self.registry.all(include_local=False)
        except Exception:
            return
        for other in peers:
            if not other.online or other.local:
                continue
            if not peer.host:
                continue
            if not other.host == peer.host:
                continue
            return other

    def _matching_nodes(self, tokens: list[str]) -> list[PeerEndpoints]:
        nodes = []
        seen = set()
        for token in tokens:
            if token in ('*', ''):
                for peer in [self._self_peer()] + self.registry.all(include_local=True):
                    if not peer.online:
                        continue
                    if peer.node_id not in seen:
                        seen.add(peer.node_id)
                        nodes.append(peer)
                continue
            peer = self.registry.resolve(token)
            if peer is not None and not peer.online and not peer.manual:
                peer = self._live_alias(peer) or peer
            if peer is not None:
                if peer.node_id not in seen:
                    seen.add(peer.node_id)
                    nodes.append(peer)
                    continue
            if not is_self_token(token, self.node_id, self.name):
                continue
            local = self._self_peer()
            if local.node_id in seen:
                continue
            seen.add(local.node_id)
            nodes.append(local)
            continue
        return nodes

    def desired_senders(self) -> list[tuple[str, PeerEndpoints, dict[str, Any]]]:
        '''[(session_key, target_node, rule)] this node should be sending right now.

        The session key is ``<route id>|<target node id>``, *not* just the route id:
        one route may name several 播放在 targets, and each of those needs its own
        stream. Keying only by route id made later targets silently overwrite earlier
        ones, so a multi-target route played on a single speaker.
        '''

        listen_only = bool(self.config.listen) and not bool(self.config.broadcast)
        out = []
        self._senders_blocked = {}
        for rule in self.effective_routes():
            if rule.get('kind') == 'listen':
                continue
            if listen_only and rule.get('kind') == 'broadcast':
                continue
            if rule['mute'] or not self._is_origin_for(rule):
                continue
            # ★ v2.28：同一台设备可能同时以"手动 IP"和"信标发现"两条记录存在，
            #   它们 host/audio_port 完全相同。以前按 node_id 建会话，同一路声音会向
            #   同一个端口发两遍 —— 手机端两条流各自播放，听感就是"嘈杂/回声"。
            #   这里按 (host, audio_port) 去重，真机身份（非 manual）优先。
            seen_endpoints: set[tuple[str, int]] = set()
            for target in sorted(self._matching_nodes(rule['to']),
                                 key=lambda p: (1 if p.manual else 0, str(p.node_id))):
                if target.node_id == self.node_id:
                    continue
                if not target.audio_port:
                    continue
                endpoint = (str(target.host), int(target.audio_port))
                if endpoint in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint)
                if self._is_auto_route(rule):
                    if not self.smart_link_allows(target.node_id, target.name):
                        key = f'{rule['id']!s}|{target.node_id!s}'
                        self._senders_blocked[key] = {'session_key': key, 'route_id': rule['id'], 'node_id': target.node_id, 'name': target.name, 'host': target.host, 'smart_blocked': True, 'text': '「%s」想听本机的声音，但它没开「智连」→ 已拦下、不给声音（要放行就在它的卡片上点「智连」）。' % target.name}
                        continue
                silent = self._peer_silent_for(target)
                if silent > PEER_SILENCE_GRACE_S:
                    key = f'{rule['id']!s}|{target.node_id!s}'
                    self._senders_blocked[key] = {'session_key': key, 'route_id': rule['id'], 'node_id': target.node_id, 'name': target.name, 'host': target.host, 'silent_for_s': round(silent, 1), 'text': '已暂停向「%s」发送：这个节点 ID 已经 %.0f 秒没有任何消息。如果这台设备还开着（只是换了 ID 或网络），在设备列表里把它合并/重新选择即可恢复。' % (target.name, silent)}
                    continue
                if rule['sink_device'] and target.sinks:
                    if not any((rule['sink_device'].lower() in (s.get('name') or '').lower() or rule['sink_device'] == s.get('id') for s in target.sinks)):
                        continue
                out.append((f'{rule['id']!s}|{target.node_id!s}', target, rule))
        return out

    def desired_sinks(self) -> dict[str, dict[str, Any]]:
        """{route_id: {"rule":.., "peers":[..]}} for streams to play locally."""
        out: dict[str, dict[str, Any]] = {}
        for rule in self.effective_routes():
            if not any(is_self_token(t, self.node_id, self.name) for t in rule["to"]) and "*" not in rule["to"]:
                continue
            peers = [p for p in self._matching_nodes(rule["from"]) if p.node_id != self.node_id]
            # ★ 2026-10-16 用户真机反馈第 6 条：「广播模式下。电脑开只听没有声音。」
            #   根因就在这里：这条规则原来是"一个在线的发送方都没有 → 干脆不建播放端"。
            #   频道（from:["*"]）是**广播式**的：收听方根本不需要先认识广播方 ——
            #   谁在发就听谁。原来一旦对方还没被登记成在线设备（手动填的 IP、信标丢了、
            #   路由器做了 AP 隔离），播放端就一个都不建，于是"开着只听、什么都没有"，
            #   而面板上开关是亮着的，用户完全看不出为什么（探针复现：对方标离线后
            #   desired_sinks 变空 → sinks 空 → 到的包只能写 "has no local sink yet"）。
            #   所以只有**具名**规则（from 里没有通配）才要求"至少有一个在线对端"。
            if not peers and "*" not in (rule.get("from") or []):
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
        return self.channel_routes() + self._dedupe_routes(table)

    def channel_state(self) -> dict[str, Any]:
        """频道的实时状态，含"每个音源现在到底能不能用、广播会不会真的发出声音"。

        以前这里只有四个布尔值/数字，面板上"广播开着、上行 0 kbps"是看不出原因的；
        用户把手机上那台设备选成音源时，唯一的反馈就是"没有声音"。判定结论由
        `channel_sources_report()` 统一给出，面板和引擎日志用的是同一套口径。
        """
        report = self.channel_sources_report()
        return {
            "broadcast": bool(self.config.broadcast),
            "listen": bool(self.config.listen),
            "gain": round(float(self.config.channel_gain), 3),
            "id": CHANNEL_ROUTE_ID,
            # 配置里每个音源的判定（能不能用、为什么不能、现在有没有数据）
            "sources": report["sources"],
            "usable": report["usable"],
            "missing": report["missing"],
            "excluded": report["excluded"],
            # 广播真正会去采的设备，以及其中现在真的在出音频的
            "effective": report["effective"],
            "live": report["live"],
            # 广播现在会不会真的发出音频（含"一个字节都不会发"的人话警告）
            "will_send": report["will_send"],
            "receivers": report["receivers"],
            "warnings": report["warnings"],
            # 最近一次"自动换了一台设备"的记录。不能静默：用户手动选过设备时更要写清楚。
            "switch": (dict(self._last_capture_switch,
                            age_s=round(time.time() - float(self._last_capture_switch.get("at") or 0), 1))
                       if self._last_capture_switch else None),
        }

    def apply_routes(self) -> None:
        """把"现在该收谁、该送谁"对齐到路由表上。

        ★ 2026-10-16 真机崩溃后的**同线程重入保护**（WER 记录：Audio Mixing v1.31.exe /
        audiomixing_launcher.dll / 0xC00000FD = 栈溢出，崩在用户拖音量那几分钟里）。
        这条路原来是一条**同步递归回路**：
            set_routes() → apply_routes() → _handle_peer_requests()
              → _grant_listen_request() / _apply_shared_gain() / _revoke_auto_listen()
              → set_routes() → …
        手机每 2 秒一条 T_STATS（带 want_listen + gain/gain_rx），只要有一条"新"消息，
        内层就把同一批消息**再处理一遍**、又改一次路由表、再进一层；探针实测嵌套到
        **198 层**。源码运行时 Python 只是慢（每条控制消息白跑几百遍完整 reconcile ——
        这正是用户说的"调音量反应很久"），Nuitka 打包版每帧占的 C 栈更大，
        1MB 线程栈当场撑爆 → 应用直接消失，用户看到的就是"调音量时电脑端应用重启"。

        只挡**同一条线程**里的重入（别的线程——HTTP 面板那条——照旧能进来）：
        外层这一轮马上会带着新的路由表把收发两端 reconcile 完（_handle_peer_requests
        在 desired_senders() 之前执行），所以被挡掉的内层调用不会漏掉任何事。
        """
        tl = getattr(self, "_apply_tls", None)
        if tl is None:
            tl = self._apply_tls = threading.local()
        if getattr(tl, "on", False):
            self._apply_reentered = int(getattr(self, "_apply_reentered", 0)) + 1
            return
        tl.on = True
        started = time.monotonic()
        try:
            self._apply_routes_once()
        finally:
            tl.on = False
            self._apply_ms = (time.monotonic() - started) * 1000.0

    def recover_audio(self) -> dict[str, Any]:
        """重开所有本机播放端（等同在 Windows 声音里切换一次输出）。

        ★ 2026-10-26 用户：其他终端（蓝牙/WiFi）连上后没声音，去 Windows 声音里
          切换几下输出就恢复，至少 7 个版本如此。自动看门狗会处理“打开着但不再
          消费”的端点；这个按钮/接口是兜底：用户不用再进 Windows 设置，点一下
          就把每一路播放端、本机直通全部按当前默认输出重新打开。
        """
        stopped: list[str] = []
        for rid in list(self.sinks.keys()):
            engine = self.sinks.get(rid)
            if engine is None:
                continue
            if getattr(engine, "feeds_into", None) is not None:
                self.sinks.pop(rid, None)
                stopped.append(rid)
                continue
            self._stop_sink(rid)
            stopped.append(rid)
        loop_stopped = False
        if self._loop is not None:
            try:
                self._loop.stop()
            except Exception:
                pass
            self._loop = None
            self._loop_key = None
            loop_stopped = True
        self.refresh_devices(force=True)
        self.apply_routes()
        active = sum(1 for e in self.sinks.values()
                     if getattr(e, "feeds_into", None) is None)
        self.note("手动恢复声音：重开 %d 路播放端（已停 %d 路）%s"
                  % (active, len(stopped), "，并重建本机直通" if loop_stopped else ""))
        return {
            "ok": True,
            "stopped": stopped,
            "restarted": active,
            "loop_stopped": loop_stopped,
            "loop_restarted": self._loop is not None,
        }

    def _apply_routes_once(self) -> None:
        self.last_reconcile_at = time.time()
        self._track_peer_liveness()
        self._refresh_stream_identities()
        self._handle_peer_requests()
        self._apply_pending_auto_listen()
        self._sweep_auto_listen_hangs()
        self._apply_identity_merges()
        wanted = self.desired_senders()
        wanted_ids = {rid for rid, _t, _r in wanted}
        for rid in list(self.sender.sessions.keys()):
            if rid not in wanted_ids:
                self.sender.remove(rid)
                entry = self._senders_blocked.get(rid)
                if isinstance(entry, dict) and entry.get('source_error'):
                    self._senders_blocked.pop(rid, None)
                self.note('stopped sender %s' % rid)
        for rid, target, rule in wanted:
            spec = self._source_spec_for(rule)
            source_key = str(spec.get('backend')), str(spec.get('device'))
            resolved_key = self._resolved_source_key(spec)
            session = self.sender.get(rid)
            same_target = session is not None and session.host == target.host and session.port == target.audio_port
            if same_target and getattr(session, 'source_resolved', None) == resolved_key:
                if session.gain != rule['gain']:
                    session.gain = rule['gain']
                continue
            if session is not None:
                self.sender.remove(rid)
            source = None
            try:
                source = self._open_source(spec)
            except Exception as exc:
                # ★ 2026-11-06（第 3 条）：音源打不开时**不能**继续往下走。
                #   原来失败后 `source` 没赋值，下一行 `SenderSession(source…)` 会抛
                #   UnboundLocalError，整个 reconcile 停摆，界面只看到"没声音"。
                fallback = self._best_loopback_selector()
                if fallback and str(fallback) != str(spec.get('device', '')):
                    try:
                        retry = dict(spec)
                        retry['device'] = fallback
                        source = self._open_source(retry)
                        spec = retry
                        source_key = str(spec.get('backend')), str(spec.get('device'))
                        resolved_key = self._resolved_source_key(spec)
                        self.last_error = ''
                        self.note('音源「%s」打不开（%s），已临时改用「%s」' % (
                            str(rule.get('source_device') or '@default'), str(exc), str(fallback)))
                    except Exception as exc2:
                        self.last_error = f'cannot open source {spec!s}: {exc2!s}'
                else:
                    self.last_error = f'cannot open source {spec!s}: {exc!s}'
                if source is None:
                    self.note(self.last_error)
                    text = ('送声失败：音源「%s」打不开（%s）。'
                            '请确认这块输出设备还连着、并且不是被独占；或换一块正在出声的输出设备。'
                            % (str(rule.get('source_device') or spec.get('device') or '@default'), self.last_error))
                    self._senders_blocked[rid] = {
                        'session_key': rid, 'route_id': rid, 'node_id': target.node_id,
                        'name': target.name, 'host': target.host, 'source_error': True,
                        'source_device': str(rule.get('source_device') or spec.get('device') or ''),
                        'text': text,
                    }
                    continue
            codec = proto.CODEC_BY_NAME.get(rule['codec'], proto.CODEC_PCM_S16)
            session = SenderSession(source, target.host, target.audio_port, codec=codec, gain=rule['gain'], node_id=self.node_id, node_name=self.name, source_name=source.name if hasattr(source, 'name') else spec.get('device', ''), route_id=rid, origin_id=self.origin_id if bool(getattr(target, 'ext', False)) else 0, hop=0)
            session.source_key = source_key
            session.source_resolved = resolved_key
            if not session.supported:
                self.note('codec %s unavailable, falling back to pcm_s16' % rule['codec'])
                session.codec = proto.CODEC_PCM_S16
                session._encoder = __import__('audiomixing.proto', fromlist=['x']).encode_pcm_s16
            try:
                self.sender.add(rid, session)
                # 送声恢复后只清掉"音源打不开"这一类提示；
                # 「对端消失/smart_blocked」的原因要保留给面板解释为什么没在送。
                entry = self._senders_blocked.get(rid)
                if isinstance(entry, dict) and entry.get('source_error'):
                    self._senders_blocked.pop(rid, None)
                self.note('sending %s -> %s:%d (%s)' % (rid, target.host, target.audio_port, target.name))
                # ★ 2026-11-04（第 11 条）：一开始送就**立刻**把 listen_req 推给对端，
                #   不再等 `_send_peer_stats` 那 2 秒一次的节流 —— 手机开智连后应当
                #   在第一个音频包前后就开始收，而不是二十秒后。
                self.push_peer_stats_now()
            except Exception as exc:
                self.last_error = f'sender {rid!s} failed: {exc!s}'
                self.note(self.last_error)
            continue
        desired = self.desired_sinks()
        for rid in list(self.sinks.keys()):
            if rid not in desired:
                engine = self.sinks.get(rid)
                feeds = getattr(engine, 'feeds_into', None)
                if feeds is not None:
                    extra = getattr(feeds, 'extra_routes', None)
                    if extra and rid in extra:
                        extra.remove(rid)
                    self.sinks.pop(rid, None)
                    self.note(f'sink {rid!s} 不再复用 {feeds.route_id!s} 的设备')
                    continue
                self._stop_sink(rid)
                self.note('closed sink %s' % rid)
        for rid, entry in desired.items():
            rule = entry['rule']
            spec = self._sink_spec_for(rule)
            wanted_selector = str(spec.get('device') or '')
            engine = self.sinks.get(rid)
            if engine is not None and engine.device_selector != wanted_selector:
                explicit = bool((rule.get('sink_device') or '').strip()) or (self.config.sink_device or '').strip().lower() not in ('', 'default', '@default')
                if not explicit and not wanted_selector:
                    wanted_selector = engine.device_selector
                elif not explicit and self._same_endpoint(engine.device_selector, wanted_selector):
                    wanted_selector = engine.device_selector
                else:
                    self._stop_sink(rid)
                    self.note(f'sink {rid!s} moved to {wanted_selector or '?'!s}')
                    engine = None
            if engine is not None and not getattr(engine, 'reopen_requested', False) and engine.thread is not None and not engine.thread.is_alive() and not engine.stop_event.is_set() and time.monotonic() - engine.started_ns > 1.0:
                engine.reopen_requested = True
                engine.last_reopen_reason = '渲染线程已退出'
            if engine is not None and getattr(engine, 'reopen_requested', False):
                reason = engine.last_reopen_reason or '端点停止消费'
                count = engine.recoveries
                self._stop_sink(rid)
                engine = self._start_sink(rid, rule)
                if engine is not None:
                    engine.recoveries = count + 1
                    engine.last_reopen_reason = reason
                    self._note_once('播放端卡住（%s），已自动重开设备' % reason)
                    self.note('sink %s auto-reopened (%s) #%d' % (rid, reason, engine.recoveries))
            if engine is None:
                shared = None
                for other in list(self.sinks.values()):
                    if other is engine or getattr(other, 'feeds_into', None) is not None:
                        continue
                    if wanted_selector:
                        if self._same_endpoint(other.device_selector, wanted_selector):
                            shared = other
                            break
                        continue
                    if other.device_selector:
                        continue
                    shared = other
                    break
                if shared is not None:
                    extra = getattr(shared, 'extra_routes', None)
                    if extra is None:
                        extra = []
                        shared.extra_routes = extra
                    if rid not in extra:
                        extra.append(rid)
                    self.sinks[rid] = _SinkFeed(rid, wanted_selector, shared)
                    self.note(f'sink {rid!s} 复用 {shared.route_id!s} 的设备 {wanted_selector or '默认'!s}')
                    continue
                if wanted_selector:
                    if any((self._same_endpoint(wanted_selector, s) for s in self._capture_selectors())):
                        # ★ v2.26 第 4 条：这句原来是英文，用户看不懂也不知道该做什么。
                        #   改成中文 + 两个明确处置：换输出设备；或者"当前没开采集"就忽略。
                        self.note("提示：输出设备「%s」同时也是本机默认的采集设备。"
                                  "如果你正开着「广播我的声音」或本机直通（采集在跑），"
                                  "对方会听到自己的回音 —— 请换一个输出设备；"
                                  "当前没有开采集的话，这条提示可以忽略。" % wanted_selector)
                engine = self._start_sink(rid, rule)
                if engine is None:
                    continue
            self._apply_sink_gain(engine, rule)
            continue
        self._apply_stream_volume()
        self._reattach_stream_routes()
        try:
            from .audio.backends import netmic as _netmic
            _netmic.set_jitter_ms(getattr(self.config, 'netmic_jitter_ms', 150))
        except Exception:
            pass
        self._auto_play_sources()
        self.hub.reap()
        for name in self.hub.health():
            self._note_once('音源「%s」一直没有产出音频数据（已自动重建采集）。常见原因：这块设备当前没有在播放声音、被其它程序独占、已经被拔出或断开、或者它是虚拟设备而没有在推流。如果持续如此，请在控制台的音源清单里确认选中的就是你正在使用的那一块。' % name)
        self._apply_local_loop()
        self._send_peer_stats()
        self._track_dead_captures()
        self._recover_stalled_capture()
        self._sample_quality()
        try:
            from . import bluetooth as _bt_refresh
            _bt_refresh.list_devices_cached(max_age=600.0 if _bt_refresh.held_count() else 20.0)
            try:
                loop = getattr(self, '_loop', None)
                if loop is not None:
                    info = loop.describe() or {}
                    started = float(getattr(loop, 'started_at', 0.0) or 0.0)
                    frames = int(info.get('frames') or 0)
                    device = str(info.get('device_name') or info.get('device') or '')
                    if started and frames == 0 and time.time() - started > 15.0 and device:
                        silent = getattr(self, '_silent_captures', None)
                        if silent is not None and device not in silent:
                            silent[device] = time.time()
                            self._note_once('采集设备 %s 打开后一直没数据，已标记并换用其它设备' % device)
            except Exception:
                pass
            else:
                now_mono = time.monotonic()
                if now_mono - getattr(self, '_last_route_pull', 0.0) >= 6.0:
                    self._last_route_pull = now_mono
                    self._pull_peer_routes()
        except Exception as exc:
            self._note_once(f'蓝牙列表刷新失败：{type(exc).__name__!s}: {exc!s}')

    def _note_peer_seen(self, *keys: str) -> None:
        """记下"刚刚听到这台对端"（信标 / 统计包 / 它的音频流 / HTTP 探测成功都算）。"""
        now = time.monotonic()
        for key in keys:
            text = str(key or "").strip().lower()
            if text:
                self._peer_seen[text] = now

    def _peer_silent_for(self, peer) -> float:
        """这台对端已经多久没有任何消息了（秒）。越小越新鲜。"""
        # 信标是最常见的一种"它还在"：age_s 就是"上次听到它的信标是几秒前"。
        try:
            age = float(getattr(peer, "age_s", 999.0))
        except Exception:
            age = 999.0
        best = 0.0
        for key in (str(getattr(peer, "node_id", "") or ""), str(getattr(peer, "host", "") or "")):
            seen = self._peer_seen.get(key.strip().lower())
            if seen and seen > best:
                best = seen
        # 取"最硬的那条证据"：信标年龄 vs 最近一次统计包/音频/HTTP 探测。
        if best:
            return min(age, time.monotonic() - best)
        return age

    def _track_peer_liveness(self) -> None:
        '''把"这台对端最近还在"的证据都收进来（引擎每个循环跑一次）。

        为什么不能只看"能不能连上"：UDP 没有握手，手机端也不回 PING/PONG，
        连通性根本问不出来。能问出来的只有"最近有没有它的任何消息"。
        '''

        try:
            for item in self.receiver.stats().get('streams') or []:
                node_id = str(item.get('node_id') or '')
                self._note_peer_seen(node_id)
                if not node_id:
                    continue
                if node_id not in self._auto_listen_granted:
                    continue
                try:
                    try:
                        age = max(0.0, float(item.get('age_s') or 0.0))
                    except (TypeError, ValueError):
                        age = 0.0
                except Exception:
                    break
                self._auto_listen_granted[node_id]['last_audio'] = time.monotonic() - age
        except Exception:
            pass
        try:
            cutoff = time.monotonic() - PEER_SILENCE_GRACE_S
            for msg in self._control_messages():
                if float(msg.get('_at') or 0.0) <= cutoff:
                    continue
                src = str(msg.get('_from') or '')
                self._note_peer_seen(src.split(':')[0], str(msg.get('node') or msg.get('node_id') or ''))
        except Exception:
            pass
        try:
            for peer in self.registry.all(include_local=True):
                silent = self._peer_silent_for(peer)
                if not PEER_SILENCE_GRACE_S * 0.5 <= silent <= PEER_SILENCE_GRACE_S * 4:
                    continue
                host = str(getattr(peer, 'host', '') or '')
                if not host:
                    continue
                port = int(getattr(peer, 'web_port', 0) or 0) or 17800
                try:
                    with urllib.request.urlopen('http://%s:%d/api/status' % (host, port), timeout=0.8) as resp:
                        json.loads(resp.read().decode('utf-8'))
                except Exception:
                    continue
                self._note_peer_seen(peer.node_id, host)
        except Exception:
            return

    def blocked_senders(self) -> list[dict[str, Any]]:
        """这一轮因为"对端已经没了"而停发的那些（面板据此说明原因）。"""
        return list(self._senders_blocked.values())

    @staticmethod
    def _as_ms(value: Any) -> int:
        """把对端的时间戳按【整数】毫秒解析，绝不经过浮点。

        手机端踩过这个坑：借道 float 会把 13 位毫秒时间戳截断成 1.7e8，
        "谁后改谁赢"直接失效。这里按字符串取整数部分。
        """
        try:
            text = str(value).strip()
            if not text:
                return 0
            if "." in text:
                text = text.split(".", 1)[0]
            if text.lower().startswith("e"):
                return 0
            return int(text)
        except (TypeError, ValueError):
            return 0

    def _route_between(self, peer_id: str, direction: str) -> dict[str, Any] | None:
        """direction='out' → 我→它 那一条（我的「我送它的」）；'in' → 它→我 那一条。"""
        for rule in self._link_routes():
            frm = [str(t) for t in rule.get("from") or []]
            to = [str(t) for t in rule.get("to") or []]
            mine_from = any(is_self_token(t, self.node_id, self.name) for t in frm)
            mine_to = any(is_self_token(t, self.node_id, self.name) for t in to)
            if direction == "out" and mine_from and peer_id in to:
                return rule
            if direction == "in" and mine_to and peer_id in frm:
                return rule
        return None

    def _link_gains(self, peer_id: str) -> dict[str, Any]:
        """我和这台对端两条链路的当前增益 + "最后一次改动时刻"（整数毫秒）。

        ★ 2026-10-16 真机反馈（"有时候听/送两条音量条都自己刷回 1"）：
        这里原来**不管有没有那条路由**都报 `gain=1.0` —— "没有这条路由"和"音量就是 1.0"
        在对面看起来一模一样，于是对面会把一个凭空来的 1.00 采纳过去（音量条当场跳回 1）。
        现在：**没有那条路由就一个字都不报**（不报 = 对面什么都不做，
        手机端的 `hasGain()` 本来就把缺字段当"老版本/没有值"处理）。有路由才报值。
        """
        out = self._route_between(peer_id, "out")
        inn = self._route_between(peer_id, "in")
        gains: dict[str, Any] = {}
        now = time.monotonic()
        if out is not None:
            rid = str(out.get("id") or "")
            value = round(float(out.get("gain", 1.0)), 3)
            gains["gain"] = value
            gains["gain_ts"] = int(self._gain_ts.get(rid, 0))
            self._gain_published[rid] = (value, now)
        if inn is not None:
            rid = str(inn.get("id") or "")
            value = round(float(inn.get("gain", 1.0)), 3)
            gains["gain_rx"] = value
            gains["gain_rx_ts"] = int(self._gain_ts.get(rid, 0))
            self._gain_published[rid] = (value, now)
        return gains

    def _control_messages(self) -> list[dict[str, Any]]:
        """两处控制消息的合集：音频层（老对端）＋ 发现层（新对端）。

        为什么必须两处都读：老版本对端只会把 T_STATS 发到音频端口（它不知道发现层这条
        路），新对端发到发现层（那一层一直活着，不受音频接收器启停影响）。只读一处就会
        漏掉一整类对端 —— 那正是"混跑时一台好一台坏"的来源，也是最难查的一种状态。
        """
        out: list[dict[str, Any]] = []
        try:
            out.extend(list(self.receiver.control_messages))
        except Exception:
            pass
        beacon = self.beacon
        if beacon is not None:
            try:
                out.extend(list(beacon.control_messages))
            except Exception:
                pass
        return out

    def _ctl_target(self, node_id: str, host: str) -> tuple[str, int] | None:
        """这台对端能不能收"走发现层"的控制消息？能就给出 (地址, 发现端口)，否则 None。

        判据是它信标里的 `ctl:1`（见 net/discovery.py 开头）。**只有对端支持才改路**：
        老版本对端不认这层外衣，改过去就等于把控制消息全部打哑（它的接收器只在按需
        启停时才活着，又收不到发现层的包）。
        """
        peer = None
        try:
            if node_id:
                peer = self.registry.get(node_id)
            if peer is None and host:
                peer = self.registry.resolve(host)
        except Exception:
            peer = None
        if peer is None or not bool(getattr(peer, "ctl", False)):
            return None
        port = int(getattr(peer, "disc_port", 0) or 0)
        if port <= 0:
            # 对端报了 ctl 但没报端口（理论上不会有）：按默认发现端口兜底，
            # 而不是干脆不发 —— 少一条消息比整条链路哑掉难查得多。
            port = int(self.config.discovery_port or proto.PORT_DISCOVERY)
        return (str(getattr(peer, "host", "") or host), port)

    def _send_ctl(self, body: dict[str, Any], node_id: str, host: str, port: int) -> None:
        """把一条控制消息发出去：优先走发现层，对端不支持就退回音频端口（老格式）。"""
        target = self._ctl_target(node_id, host) if self.beacon is not None else None
        delivered = False
        if target is not None:
            try:
                delivered = bool(self.beacon.send_control(body, target[0], target[1]))
            except Exception:
                delivered = False
        if delivered:
            return
        try:
            self.receiver.send_control(proto.T_STATS, body, (str(host), int(port)))
        except Exception:
            pass

    def _send_peer_stats(self, force: bool=False) -> None:
        '''给"正在给它发声音"的对端发统计 + 收听请求 + 两条链路的共享增益。

用户要的两件事都搭在这个每 2 秒一次的统计包上：
  · 我点「送给它」→ 对方自动开始收听（want_listen，幂等）；
  · 两端音量条是同一个增益，谁调都以最新为准（gain/gain_rx + 时间戳）。

2026-10-15 起这条路分两条：对端信标里有 `ctl:1` 就发到它的<b>发现端口</b>
（那一层一直活着），否则照旧发<b>音频端口</b>（老版本只有这条路能收到）。
见 _ctl_target 与 net/discovery.py 开头的说明。

★ 2026-10-15（用户反馈第 3 条「电脑停止送音后手机依旧收音不停止」）：
"我停了"这件事也必须送达 —— 停掉之后还要再报 {@link STATS_STOP_GRACE_S} 秒
`want_listen=false`。原来这个循环只遍历"还在送的会话"，我这边一停会话就没了，
一个包都发不出去，对端（手机）永远不知道我停了，它替自动打开的那条收听就一直开着。
（手机端早就为同一个原因加了 30 秒宽限，电脑端一直漏着。）
'''

        now = time.monotonic()
        if getattr(self, '_ctl_push_pending', False):
            self._ctl_push_pending = False
            force = True
        if not force:
            if now - getattr(self, '_last_peer_stats', 0.0) < 2.0:
                return
        self._last_peer_stats = now
        try:
            sessions = list(self.sender.sessions.values())
        except Exception:
            return
        targets = {}
        for session in sessions:
            # ★ 2026-11-04（第 11 条）：强制推送（刚建好发送会话 / 刚改路由）时，
            #   哪怕第一个音频包还没发出去也要送 listen_req —— 用户要的是"第一个包
            #   就能接上"，不是等统计攒够再通知。周期性统计仍只报已经出过包的对端。
            if int(getattr(session, 'packets_sent', 0) or 0) <= 0 and not force:
                continue
            peer_id = str(getattr(session, 'node_id', '') or '')
            key = peer_id or str(getattr(session, 'host', '') or '')
            if not key:
                continue
            broadcast_line = str(getattr(session, 'route_id', '') or '').split('|', 1)[0] == CHANNEL_ROUTE_ID
            targets[key] = {'node_id': peer_id, 'host': str(session.host), 'port': int(session.port), 'sending': True, 'sent': int(getattr(session, 'packets_sent', 0) or 0), 'bytes': int(getattr(session, 'bytes_sent', 0) or 0), 'want_listen': not broadcast_line, 'listen_req': None if broadcast_line else 'start'}
            continue
        for key, info in self._listening_peer_targets().items():
            targets.setdefault(key, info)
        for key, info in self._route_peer_targets().items():
            targets.setdefault(key, info)
        for key in list(getattr(self, '_stats_last_keys', ())):
            if key in targets or key in self._stats_grace:
                continue
            known = dict(self._stats_seen.get(key) or {})
            if known.get('host'):
                self._stats_grace[key] = dict(known, until=now + STATS_STOP_GRACE_S)
        for key, info in list(self._stats_grace.items()):
            if now > float(info.get('until') or 0.0):
                self._stats_grace.pop(key, None)
                continue
            targets.setdefault(key, dict(info, sending=False, want_listen=False, sent=0, bytes=0, listen_req='stop'))
        self._stats_last_keys = {k for k, t in targets.items() if t.get('sending')}
        self._stats_seen = {k: dict(t) for k, t in targets.items()}
        rx_by_peer = self._peer_recv_stats()
        for key in targets:
            info = targets[key]
            peer_id = str(info.get('node_id') or '')
            host = str(info.get('host') or '')
            if not host:
                continue
            gains = self._link_gains(peer_id) if peer_id else {}
            rx = rx_by_peer.get(peer_id) or {}
            body = {'t': 'stats', 'node': self.node_id, 'name': self.name, 'sending': bool(info.get('sending')), 'sent': int(info.get('sent') or 0), 'bytes': int(info.get('bytes') or 0), 'recv': int(rx.get('recv') or 0), 'lost': int(rx.get('lost') or 0), 'buf_ms': int(rx.get('buf_ms') or 0), 'streams': int(rx.get('streams') or 0), 'want_listen': bool(info.get('want_listen')) and (self._peer_supports_smart(peer_id) or self._smart_wants_listen(peer_id))}
            req = info.get('listen_req')
            if req:
                key = peer_id or host
                if self._listen_req_state.get(key) != req:
                    self._listen_req_seq += 1
                    self._listen_req_state[key] = str(req)
                body['listen_req'] = str(req)
                body['listen_req_id'] = self._listen_req_seq
            body.update(gains)
            # ★ 2026-11-08（用户方案）：把"静音档乘数策略"随统计包幂等下发。
            #   手机端只认更大的请求号，所以同一句话重复发不会把本地选择反复覆盖。
            policy = str(getattr(self.config, 'phone_silent_vol_policy', 'internal_only')
                         or 'internal_only').strip().lower()
            if policy in ('internal_only', 'last_nonzero'):
                body['silent_vol_policy'] = policy
                body['silent_vol_policy_req'] = int(getattr(self, '_silent_policy_seq', 0) or 0)
            self._send_ctl(body, peer_id, host, int(info.get('port') or 0))
            continue

    def set_phone_silent_vol_policy(self, policy: str) -> str:
        '''切换手机静音档乘数策略（internal_only / last_nonzero），并立即下发一次。

★ 2026-11-08（用户方案）：默认 `internal_only`（乘 1.0）—— 静音档里 STREAM_MUSIC
  恒为 0，用旧值会造成"调了没反应"；`last_nonzero` 只是给"外放档压到 25% 的信息
  不想在切静音档时丢掉"的用户留的选择。
  设置页/API 都走这里：改本地配置 + 递增请求号 + 立刻单播给所有已发现节点。
        '''

        p = str(policy or '').strip().lower()
        p = 'last_nonzero' if p == 'last_nonzero' else 'internal_only'
        self.config.phone_silent_vol_policy = p
        self._silent_policy_seq = int(getattr(self, '_silent_policy_seq', 0) or 0) + 1
        try:
            peers = list(self.registry.snapshot() or [])
        except Exception:
            peers = []
        for peer in peers:
            body = {
                't': 'stats', 'node': self.node_id, 'name': self.name,
                'silent_vol_policy': p,
                'silent_vol_policy_req': self._silent_policy_seq,
                'silent_vol_policy_only': True,   # 纯设置包：手机端不拿它做收听/增益判定
            }
            try:
                self._send_ctl(body, str(getattr(peer, 'node_id', '') or ''),
                               str(getattr(peer, 'host', '') or ''),
                               int(getattr(peer, 'audio_port', 0) or 0))
            except Exception:
                continue
        return p

    def _peer_recv_stats(self) -> dict[str, dict[str, int]]:
        """{对端 node_id: 我从它那儿收到的包/丢包/缓冲/流数} —— 回给对端自己看。

        为什么按 node_id 聚合而不是按地址：接收端的流表就是按 node_id 记的
        （见 rtpaudio.StreamInfo.node_id），一台对端可能有不止一条流（多条路由），
        对端真正想知道的是"我发出去的总共到了多少"，所以这里做的是求和。

        解析不出来就当空表：这只是"给对方看的一行数字"，绝不能因为它让统计包发不出去。
        """
        out: dict[str, dict[str, int]] = {}
        try:
            streams = self.receiver.stats().get("streams") or []
        except Exception:
            return out
        for item in streams:
            pid = str(item.get("node_id") or "")
            if not pid:
                continue
            acc = out.setdefault(pid, {"recv": 0, "lost": 0, "buf_ms": 0, "streams": 0})
            acc["recv"] += int(item.get("packets") or 0)
            acc["lost"] += int(item.get("lost") or 0)
            try:
                buffered = int(round(float(item.get("buffered_ms") or 0)))
            except (TypeError, ValueError):
                buffered = 0
            acc["buf_ms"] = max(acc["buf_ms"], buffered)
            acc["streams"] += 1
        return out

    def _listening_peer_targets(self) -> dict[str, dict[str, Any]]:
        """我正从哪些设备收声音（`它 → 本机` 的路由）→ 这一轮也要给它们回统计。

        只认"我在收"这一侧：频道收听（`from:["*"]`）没有具体的一台设备，跳过。
        键用 node_id，和发送端的键一致，`setdefault` 保证同一条链路只报一次。
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            rules = self._link_routes()
        except Exception:
            return out
        for rule in rules:
            if str(rule.get("kind") or "") == "listen":
                continue
            frm = [str(t) for t in (rule.get("from") or [])]
            to = [str(t) for t in (rule.get("to") or [])]
            if not any(is_self_token(t, self.node_id, self.name) for t in to):
                continue
            for token in frm:
                token = token.strip()
                if not token or token == "*" or is_self_token(token, self.node_id, self.name):
                    continue
                try:
                    peer = self.registry.get(token) or self.registry.resolve(token)
                except Exception:
                    peer = None
                if peer is None:
                    continue
                node_id = str(getattr(peer, "node_id", "") or token)
                if node_id in out:
                    continue
                host = str(getattr(peer, "host", "") or "")
                port = int(getattr(peer, "audio_port", 0) or 0)
                if not host or port <= 0:
                    continue
                out[node_id] = {
                    "node_id": node_id, "host": host, "port": port,
                    "sending": False, "sent": 0, "bytes": 0,
                    "want_listen": False, "listening": True,
                }
        return out

    def _route_peer_targets(self) -> dict[str, dict[str, Any]]:
        """本机有路由**指向**的对端 → 也要给它们报统计（哪怕现在还没发出音频）。

        为什么（2026-11-01 用户「电脑端修改音量手机不同步」）：
        `_send_peer_stats` 原来的收件人只来自 `sender.sessions`，并且要求
        `packets_sent > 0`。用户完全可能**先调音量、后开声音**，或者采集刚好空转 ——
        那时这条**唯一**携带 `gain / gain_rx` 的消息一个都不发，对面永远停在旧值。
        路由本身已经说明"我和这台之间有一条链路"，按它补上收件人即可。

        范围刻意只补"具体一台"的路由：`to: ["*"]`（广播）没有"某一台的共享音量"
        这回事；`from` 里没有本机的那些是"我在听它"，已经由
        :meth:`_listening_peer_targets` 覆盖。
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            rules = self._link_routes()
        except Exception:
            return out
        for rule in rules:
            frm = [str(t) for t in (rule.get("from") or [])]
            to = [str(t) for t in (rule.get("to") or [])]
            if not any(is_self_token(t, self.node_id, self.name) for t in frm):
                continue
            for token in to:
                token = token.strip()
                if not token or token == "*" or is_self_token(token, self.node_id, self.name):
                    continue
                try:
                    peer = self.registry.get(token) or self.registry.resolve(token)
                except Exception:
                    peer = None
                if peer is None:
                    continue
                node_id = str(getattr(peer, "node_id", "") or token)
                if node_id in out or node_id == self.node_id:
                    continue
                host = str(getattr(peer, "host", "") or "")
                port = int(getattr(peer, "audio_port", 0) or 0)
                if not host or port <= 0:
                    continue
                out[node_id] = {
                    "node_id": node_id, "host": host, "port": port,
                    "sending": False, "sent": 0, "bytes": 0,
                    "want_listen": False,
                }
        return out

    def _peer_identity_keys(self, node_id: str = '', host: str = '') -> list[str]:
        '''一台对端所有可能被用来存/查媒体音量的键。

        ★ 2026-11-04（v2.26 第 2 条）：手机 2.14 明明在发 vol，PC 却显示"未上报"。
          根因不是手机没发，而是**键对不上**：identity 合并后，statistics 里的 node_id
          和对端记录的 node_id / 路由里的旧 alias 可能是三个不同字符串。旧实现只按
          node_id 存、只按 node_id + host 查，漏了 device_id / alias_ids / hosts。
          这里把一座对端的全部身份键集中列出，存和查都用同一份。
        '''
        keys: list[str] = []

        def add(value: Any) -> None:
            text = str(value or '').strip()
            if text and text.lower() not in {k.lower() for k in keys}:
                keys.append(text)

        add(node_id)
        add(host)
        peer = None
        try:
            peer = self.registry.get(node_id) if node_id else None
            if peer is None and host:
                peer = self.registry.resolve(host)
        except Exception:
            peer = None
        if peer is not None:
            add(getattr(peer, 'node_id', ''))
            add(getattr(peer, 'device_id', ''))
            add(getattr(peer, 'host', ''))
            for alias in getattr(peer, 'alias_ids', []) or []:
                add(alias)
            for other_host in getattr(peer, 'hosts', []) or []:
                add(other_host)
        try:
            add(self._node_id_for_host(host))
        except Exception:
            pass
        return keys

    def _handle_peer_requests(self) -> None:
        '''处理对端发来的 T_STATS：① 它请我听它 → 自动建路由（可撤销）；② 共享增益。'''

        now = time.monotonic()
        messages = self._control_messages()
        for msg in messages:
            if str(msg.get('_type') or '') != 'STATS':
                continue
            at = float(msg.get('_at') or 0.0)
            if at <= getattr(self, '_handled_stats_at', 0.0):
                continue
            src = str(msg.get('_from') or '')
            host = src.split(':')[0]
            node_id = str(msg.get('node') or msg.get('node_id') or msg.get('listen_from') or '')
            if not node_id:
                node_id = self._node_id_for_host(host)
            name = str(msg.get('name') or msg.get('listen_name') or host or '对端')
            ack_id = msg.get('listen_ack_id')
            if node_id and ack_id not in (None, '', 0):
                self._peer_listen_ack[node_id] = {'node_id': node_id, 'req_id': ack_id, 'action': str(msg.get('listen_ack_action') or ''), 'reason': str(msg.get('listen_ack_reason') or ''), 'at': time.time()}
            if bool(msg.get('want_listen')):
                self._grant_listen_request(node_id, name, host)
            elif node_id:
                self._auto_stop_requested[node_id] = now
            if node_id:
                raw_mode = msg.get('send_mode')
                raw_broadcast = msg.get('broadcast')
                if raw_mode is not None or raw_broadcast is not None:
                    try:
                        mode = int(raw_mode) if raw_mode is not None else 1 if bool(msg.get('sending')) else 0
                    except (TypeError, ValueError):
                        mode = 1 if bool(msg.get('sending')) else 0
                    mode = max(0, min(2, mode))
                    if isinstance(raw_broadcast, bool):
                        broadcast = raw_broadcast
                    else:
                        broadcast = str(raw_broadcast or '').strip().lower() in (
                            '1', 'true', 'yes', 'on')
                    # ★ 2026-11-06（Q2）：和 vol 一样存进**这台对端的每一个身份键**。
                    #   现场 identity 合并后 statistics 报的 id 与音频包解析出的 id
                    #   可能只在一个别名上相等；旧实现只按一个键存/查，广播声明就会
                    #   "明明发了却查不到"，退回"按点对点拒绝"。
                    for key in self._peer_identity_keys(node_id, host):
                        self._peer_send_mode[key] = mode, broadcast, now
                else:
                    for key in self._peer_identity_keys(node_id, host):
                        self._peer_send_mode.pop(key, None)
            if bool(msg.get('want_bt')):
                self._request_bluetooth(node_id, name)
            if msg.get('gain') is not None and node_id:
                self._apply_shared_gain(node_id, 'in', float(msg['gain']), self._as_ms(msg.get('gain_ts')))
            if msg.get('gain_rx') is not None and node_id:
                self._apply_shared_gain(node_id, 'out', float(msg['gain_rx']), self._as_ms(msg.get('gain_rx_ts')))
            if msg.get('vol') is None:
                continue
            if not node_id:
                continue
            try:
                value = float(msg['vol'])
            except (TypeError, ValueError):
                value = -1.0
            if not 0.0 <= value <= 1.0:
                continue
            # ★ v2.26 第 2 条：把 vol 存进**这台对端的每一个身份键**（node_id / device_id /
            #   aliasIds / host / 历史地址）。旧实现只存 node_id，identity 改名后路由按
            #   旧 alias 查，永远命中不了 —— 这就是"手机 2.14 明明在发 vol 却显示未上报"。
            keys = self._peer_identity_keys(node_id, host)
            for key in keys:
                self._peer_volume[key] = value, now
            phone: dict[str, Any] = {}
            for key in ('vol_q14', 'gain_q14', 'total_q14', 'raw_peak', 'post_peak'):
                if msg.get(key) is not None:
                    try:
                        phone[key] = round(float(msg[key]), 4)
                    except (TypeError, ValueError):
                        pass
            # ★ 2026-11-08：诊断页要中文显示"手机当前档位与乘数来源"，
            #   所以把 vol_source/vol_policy 一并留在最近一次上报里（老手机为空）。
            raw_source = str(msg.get('vol_source') or '').strip().lower()
            raw_policy = str(msg.get('vol_policy') or '').strip().lower()
            self._peer_vol_last = {
                'node': node_id, 'host': host, 'vol': round(float(value), 3),
                'keys': keys, 'at': round(time.time(), 1), 'phone': phone,
                'vol_source': raw_source if raw_source in ('external', 'silent') else '',
                'vol_policy': raw_policy if raw_policy in ('internal_only', 'last_nonzero') else '',
            }
            self._record_peer_vol_history(node_id, host, value)
            if not node_id:
                continue
            try:
                peer = self.registry.get(node_id)
                canonical = str(getattr(peer, 'node_id', '') or node_id)
            except Exception:
                canonical = node_id
            # ★ 2026-11-08（用户方案）：手机上报"音量乘数由送声档位决定"。
            #   vol_source = external（外放档，手机乘了 STREAM_MUSIC）
            #              / silent（静音档，手机不再叠加）；
            #   vol_in_capture 按档位真实取值（外放 false / 静音 true）；
            #   vol_policy = 静音档乘数策略（internal_only / last_nonzero）。
            #
            #   关键：**两种档位都已经由手机处理过一次**，所以电脑端系数恒为 1.0 ——
            #   这就是"两端合计只乘一次"的落点。不能再用 vol_in_capture=false 去叠加
            #   （外放档那会二次衰减）。
            for key in keys:
                if raw_source in ('external', 'silent'):
                    self._peer_vol_source[key] = raw_source
                if raw_policy in ('internal_only', 'last_nonzero'):
                    self._peer_vol_policy[key] = raw_policy
            if raw_source in ('external', 'silent'):
                self._peer_vol_source[canonical] = raw_source
            if raw_policy in ('internal_only', 'last_nonzero'):
                self._peer_vol_policy[canonical] = raw_policy
            if raw_source in ('external', 'silent'):
                for key in keys:
                    self._peer_vol_declared[key] = True
                self._peer_vol_declared[canonical] = True
                self._peer_vol_verdict[canonical] = True
            elif bool(msg.get('vol_in_capture')):
                # 老手机（2.15+ / 2.19）：明确声明 PCM 里已经包含音量，PC 不再叠加。
                for key in keys:
                    self._peer_vol_declared[key] = True
                self._peer_vol_declared[canonical] = True
                self._peer_vol_verdict[canonical] = True
            elif msg.get('vol_in_capture') is False:
                # 老约定下的 false（没有 vol_source）：按"采集在音量之前"处理，允许 PC 叠加。
                for key in keys:
                    self._peer_vol_declared.pop(key, None)
                self._peer_vol_declared.pop(canonical, None)
            self._observe_peer_volume(canonical, value, now)
            continue
        self._handled_stats_at = now

    def _peer_media_volume(self, rule: dict[str, Any]) -> float:
        '''这条路由的源设备当前媒体音量（0..1）；没收到过或过期就按 1.0（行为不变）。'''

        return self._peer_media_volume_source(rule)[0]

    def _peer_media_volume_source(self, rule: dict[str, Any]) -> tuple[float, str]:
        '''同 `_peer_media_volume`，另外返回"这个值是从哪个键查到的"。

★ 2026-11-04（Q1）：键对齐不是走过场 —— 上一轮 `_clock_for` 就是栽在
  `node_id` / IP 两个键上。这里除了 token 本身，还会补上
  `resolve(token).node_id`、`resolve(token).host`，以及把 token 当 IP 反查出的
  node_id；任何一条命中都算。返回值里的键写进诊断，用户看到 `vol 键=…`
  就能判断是"没上报"还是"上报了但没匹配上"。
'''

        now = time.monotonic()
        best = None
        for raw in rule.get('from') or []:
            token = str(raw or '').strip()
            if token:
                if token == '*' or is_self_token(token, self.node_id, self.name):
                    continue
                # ★ v2.26 第 2 条：token 本身 + 对端的全部身份键（device_id / aliasIds / hosts）。
                try:
                    peer = self.registry.resolve(token)
                except Exception:
                    peer = None
                keys = [token]
                if peer is not None:
                    keys = self._peer_identity_keys(str(getattr(peer, 'node_id', '') or ''), str(getattr(peer, 'host', '') or ''))
                    keys.insert(0, token)
                for key in keys:
                    if not key:
                        continue
                    item = self._peer_volume.get(key)
                    if item is None:
                        continue
                    value, at = item
                    if now - at > PEER_VOLUME_TTL_S:
                        continue
                    value = max(0.0, min(1.0, float(value)))
                    if best is None or value < best[0]:
                        best = value, key
                    break
            continue
        if best is not None:
            return best
        return (1.0, '')

    def _peer_media_volume_for_node(self, node_id: str) -> float:
        '''按流的**发送方 node_id**查手机音量（通配路由只能这么查）。'''

        nid = str(node_id or '')
        if not nid:
            return 1.0
        now = time.monotonic()
        try:
            peer = self.registry.get(nid)
        except Exception:
            peer = None
        host = str(getattr(peer, 'host', '') or '')
        for key in self._peer_identity_keys(nid, host):
            item = self._peer_volume.get(key)
            if item is not None and now - item[1] <= PEER_VOLUME_TTL_S:
                return max(0.0, min(1.0, float(item[0])))
        return 1.0

    def _received_peak_for(self, node_id: str) -> float:
        '''这台发送方的流**实际收到的峰值**（解码后、乘任何增益之前）。

        `JitterBuffer.peak` 记的是接收端对原始 PCM 的滑动最大值，与 `vol` 无关 ——
        正好拿来和手机上报的 vol 做同比例比较。多路流取最大（同一台设备的多路音源
        都是它自己的采集，峰值口径一致）。
        '''

        nid = str(node_id or '')
        if not nid:
            return 0.0
        try:
            items = list(self.receiver.streams.copy().items())
        except Exception:
            return 0.0
        # ★ v2.26 第 2 条：流里的 node_id 可能是旧 alias，和路由/统计用的 id 不同。
        aliases = {nid.lower()}
        try:
            peer = self.registry.get(nid)
        except Exception:
            peer = None
        if peer is not None:
            for token in (getattr(peer, 'node_id', ''), getattr(peer, 'device_id', '')):
                if token:
                    aliases.add(str(token).lower())
            for token in getattr(peer, 'alias_ids', []) or []:
                aliases.add(str(token).lower())
        best = 0.0
        for ssrc, info in items:
            if str(getattr(info, 'node_id', '') or '').lower() not in aliases:
                continue
            buf = self.receiver.buffers.get(ssrc)
            if buf is not None:
                try:
                    value = float(getattr(buf, 'peak', 0.0))
                except (TypeError, ValueError):
                    continue
                if value > best:
                    best = value
        return best

    def _record_peer_vol_history(self, node_id: str, host: str, vol: float) -> None:
        '''记下一条 `vol` 样本，只留最近 12 条（约 24 秒，手机每 2 秒一条）。

        为什么留 12 条而不是只留最后一个：用户调 App 音量时，我们要能回答
        "vol 数值到底有没有跟着动"。连续 12 条都一样，就说明乘法拿到的乘数
        本身没变（问题在采集点），而不是电脑侧没乘 —— 这两种情况的修法完全不同。
        '''
        nid = str(node_id or host or '')
        if not nid:
            return
        hist = self._peer_vol_hist.get(nid)
        if not isinstance(hist, list):
            hist = []
            self._peer_vol_hist[nid] = hist
        hist.append({'t': round(time.time(), 1), 'vol': round(float(vol), 3),
                     'host': str(host or '')})
        del hist[:-12]

    def _peer_vol_series(self) -> dict[str, Any]:
        '''把每台对端的 vol 序列整理成"变没变"的结论，给诊断和状态接口用。

        额外算出 `min/max/changed`：changed=True 才说明大脑收到的乘数在动；
        changed=False 且样本≥6 时，界面可以直接说"这台手机报的媒体音量一直没变"。
        '''
        out: dict[str, Any] = {}
        for nid, items in list(self._peer_vol_hist.items()):
            vals = [float(i.get('vol') or 0.0) for i in items if isinstance(i, dict)]
            if not vals:
                continue
            out[nid] = {
                'host': items[-1].get('host', '') if items else '',
                'count': len(vals),
                'min': round(min(vals), 3),
                'max': round(max(vals), 3),
                'changed': (max(vals) - min(vals)) >= 0.02,
                'samples': list(items[-12:]),
            }
        return out

    def _observe_peer_volume(self, node_id: str, vol: float, now: float) -> None:
        '''把一轮 `(vol, 收到峰值)` 喂进判定窗口；样本够了才提交结论。

判定：峰值比 ≈ vol 比 → 采集端已衰减（PC 不叠加）；
      峰值比 ≈ 1      → 采集在音量之前（PC 仍叠加）。
两者都不像（声音内容自己变了）或音频太小 → 这一轮作废，把基准挪到当下等下一轮。
'''

        nid = str(node_id or '')
        if not nid:
            return
        if self._peer_vol_declared.get(nid):
            return
        peak = self._received_peak_for(nid)
        prev = self._peer_vol_trace.get(nid)
        if prev is None:
            self._peer_vol_trace[nid] = now, vol, peak, 0.0
            return
        at0, vol0, peak0, window_max = prev
        window_max = max(window_max, peak)
        if now - at0 < PEER_VOLUME_DETECT_MIN_S:
            self._peer_vol_trace[nid] = at0, vol0, peak0, window_max
            return
        if peak0 < PEER_VOLUME_AUDIBLE_MIN or window_max < PEER_VOLUME_AUDIBLE_MIN:
            self._peer_vol_trace[nid] = now, vol, peak, 0.0
            return
        if abs(vol - vol0) < PEER_VOLUME_DETECT_DELTA:
            self._peer_vol_trace[nid] = now, vol, window_max, 0.0
            return
        vol_ratio = vol / vol0 if vol0 > 1e-6 else 1.0
        peak_ratio = window_max / peak0
        same_ratio = abs(peak_ratio - vol_ratio) <= max(0.15, abs(vol_ratio) * 0.25)
        flat = abs(peak_ratio - 1.0) <= 0.15
        if same_ratio and not flat:
            verdict = True
        elif flat and not same_ratio:
            verdict = False
        else:
            self._peer_vol_trace[nid] = now, vol, window_max, 0.0
            return
        pending = self._peer_vol_pending.get(nid)
        current = self._peer_vol_verdict.get(nid)
        if current is None or current == verdict:
            self._peer_vol_verdict[nid] = verdict
            self._peer_vol_pending.pop(nid, None)
        else:
            count = pending[1] + 1 if pending is not None and pending[0] == verdict else 1
            if count >= PEER_VOLUME_DETECT_CONFIRM:
                self._peer_vol_verdict[nid] = verdict
                self._peer_vol_pending.pop(nid, None)
            else:
                self._peer_vol_pending[nid] = verdict, count
        self._peer_vol_trace[nid] = now, vol, window_max, 0.0

    def _volume_verdict_for(self, keys: Iterable[str]) -> bool | None:
        '''按若干可能的键（node_id / IP）查判定结论；没有样本返回 None。'''

        for raw in keys:
            key = str(raw or '')
            if not key:
                continue
            if key in self._peer_vol_declared:
                return True
            if key in self._peer_vol_verdict:
                return self._peer_vol_verdict[key]
            nid = self._node_id_for_host(key) if '.' in key else ''
            if not nid:
                continue
            if nid in self._peer_vol_verdict:
                return self._peer_vol_verdict[nid]

    def _peer_vol_source_for(self, keys: list[str]) -> str:
        '''从这台对端的任一身份键里取档位声明（external/silent）；没有返回空串。'''

        for key in keys or []:
            src = self._peer_vol_source.get(str(key or ''))
            if src in ('external', 'silent'):
                return src
        return ''

    def _peer_vol_policy_for(self, keys: list[str]) -> str:
        '''从这台对端的任一身份键里取静音档乘数策略；没有返回空串。'''

        for key in keys or []:
            policy = self._peer_vol_policy.get(str(key or ''))
            if policy in ('internal_only', 'last_nonzero'):
                return policy
        return ''

    def _volume_factor_for(self, vol: float, verdict: bool | None, source: str='') -> tuple[float, str]:
        '''返回 (最终乘数, 判定文本)。`phone_volume_mode` 的强制档在这里优先。

★ 2026-11-08（用户方案）不变式：**两端合计只乘一次**。
  手机 2.20+ 按送声档位决定 media_q14：
    · 外放档 vol_source=external → 手机乘了 STREAM_MUSIC（vol_in_capture=false）；
    · 静音档 vol_source=silent   → 手机不再叠加（vol_in_capture=true）。
  两种档位都已由手机处理过，电脑端系数必须是 1.0，否则就是二次衰减。
  只有老配置的 on/off 才延续"电脑自己乘"的旧语义。
'''

        mode = str(getattr(self.config, 'phone_volume_mode', 'auto') or 'auto').strip().lower()
        if mode == 'off':
            return (1.0, '强制关（不叠加）')
        if mode == 'on':
            return vol, '强制开（叠加）'
        src = str(source or '').strip().lower()
        if src == 'external':
            return (1.0, '外放档：手机已乘系统音量（不叠加）')
        if src == 'silent':
            return (1.0, '静音档：手机不再叠加（不叠加）')
        if verdict is None:
            return vol, '自动·样本不足（按叠加）'
        if verdict:
            return (1.0, '采集端已衰减（不叠加）')
        return vol, '采集在音量之前（叠加）'

    def _volume_decision_text(self, vol: float, peak: float, verdict: bool | None, factor: float, note: str) -> str:
        '''诊断一行：`vol=x.xx  收到峰值=y.yy  判定=…`（用户要"一眼判死"）。'''

        return 'vol=%.2f  收到峰值=%.2f  判定=%s（系数 %.2f）' % (max(0.0, min(1.0, float(vol))), max(0.0, float(peak)), note, float(factor))

    def _peer_send_mode_for(self, node_id: str) -> tuple[int, bool] | None:
        '''对端声明的 (档位, 是否广播)；没报或已过期返回 None。

        ★ 2026-11-06（Q2）：取"最新的一份声明"，来源有两处 ——
          ① STATS 控制消息里显式的 send_mode/broadcast（存进这台对端的每个身份键）；
          ② 发现层信标里同名字段（每 ~2 秒一封、面向全网，切换瞬间也不会漏）。
        两处都按 PEER_TARGETED_TTL_S 判新鲜度，取时间更近的那份。旧实现只看
        控制消息、且只认一个 node_id 键，身份归一/别名不一致时就会查不到声明，
        广播被误判成点对点而整条拒收。
        '''

        nid = str(node_id or '')
        if not nid:
            return None
        mono = time.monotonic()
        wall = time.time()
        best: tuple[float, int, bool, str] | None = None
        for key in self._peer_identity_keys(nid):
            item = self._peer_send_mode.get(key)
            if item is None:
                continue
            mode, broadcast, at = item
            at = float(at)
            if mono - at > PEER_TARGETED_TTL_S:
                continue
            if best is None or at > best[0]:
                best = (at, int(mode), bool(broadcast), 'ctl')
        peer = None
        try:
            peer = self.registry.get(nid)
            if peer is None:
                peer = self.registry.resolve(nid)
        except Exception:
            peer = None
        declared_at = float(getattr(peer, 'send_state_at', 0.0) or 0.0)
        if peer is not None and declared_at > 0:
            age = wall - declared_at
            if 0.0 <= age <= PEER_TARGETED_TTL_S:
                at_mono = mono - age
                if best is None or at_mono >= best[0]:
                    best = (at_mono, int(getattr(peer, 'send_mode', 0) or 0),
                            bool(getattr(peer, 'broadcast', False)), 'beacon')
        if best is None:
            return None
        return best[1], best[2]

    def _peer_sends_point_to_point(self, node_id: str) -> bool:
        '''对端是不是在对我"点对点送声音"（而不是在走频道广播）。

★ 2026-11-04（Q1 回退修复）：只认**发送端显式声明的模式**，不再用
  `want_listen` 推断。广播（`broadcast=true`）永远不算点对点，哪怕它因为
  残留的 sendIds 同时报了 want_listen。拿不到模式的老版本一律返回 False
  （= 放行，保证"能听"），不允许因为缺字段就静默失败。
'''

        item = self._peer_send_mode_for(node_id)
        if item is None:
            return False
        mode, broadcast = item
        return mode >= 1 and not broadcast

    def _stream_is_accepted(self, node_id: str) -> bool:
        '''接收准入：这条流能不能进本机混音（Q2 的闸门）。

        规则（与文档里的铁律一致：点对点路由 与 广播→只听 互斥）：
        * 认不出来源的流 → 放行（老行为：手动 IP / 信标丢了，只能走频道那条）；
        * 有这台设备的**具名已接受路由** → 放行；
        * 没有具名路由、但它声明了在对我点对点送 → 拒绝（不进流表、不计接收统计）；
        * 其余（没声明点对点，也就是频道广播）→ 放行，由通配"只听"接住。
        '''

        nid = str(node_id or '')
        if not nid:
            return True
        # ★ 2026-11-06（第 1 条，回归到 BUG-20261104-04）：勾「只听」不等于放行点对点。
        #   v2.28 为了修"只听下彻底没声"把这里改成一律放行，结果没开「收听」的手机
        #   也能把点对点流灌进频道通配路由 —— 声音是进来了，但走的是"频道"那条
        #   为广播准备的混音路径，现场表现就是"能听见、卡得很厉害"。
        #   正确语义（用户本轮确认 + 文档铁律）：只听只收**频道广播**；点对点必须
        #   在设备卡片上开「收听」才收。广播仍按 `_peer_sends_point_to_point` 放行。
        if self._accepted_routes_for(nid):
            return True
        return not self._peer_sends_point_to_point(nid)

    def _accepted_routes_for(self, node_id: str) -> set[str]:
        '''只为这台设备建过、且真的指向本机的路由（不含通配频道那条）。'''

        nid = str(node_id or '')
        if not nid:
            return set()
        specific = set()
        for route_id, entry in self.desired_sinks().items():
            if '*' in (entry['rule'].get('from') or []):
                continue
            if any((peer.node_id == nid for peer in entry['peers'])):
                specific.add(route_id)
        return specific

    def _volume_verdict_for_rule(self, rule: dict[str, Any]) -> bool | None:
        '''这条具名路由的来源设备有没有已判定的结论（键可能是 node_id 或 IP）。'''

        keys = []
        for raw in rule.get('from') or []:
            token = str(raw or '').strip()
            if token:
                if token == '*' or is_self_token(token, self.node_id, self.name):
                    continue
                keys.append(token)
                try:
                    peer = self.registry.resolve(token)
                except Exception:
                    peer = None
                if peer is not None:
                    keys.extend(self._peer_identity_keys(
                        str(getattr(peer, 'node_id', '') or ''),
                        str(getattr(peer, 'host', '') or '')))
                else:
                    nid = self._node_id_for_host(token)
                    if nid:
                        keys.append(nid)
            continue
        return self._volume_verdict_for(keys)

    def _volume_verdict_for_node(self, node_id: str) -> bool | None:
        '''通配路由只能按流的发送方 node_id（外加它的 IP）查结论。'''

        nid = str(node_id or '')
        if not nid:
            return
        try:
            peer = self.registry.get(nid)
        except Exception:
            peer = None
        host = str(getattr(peer, 'host', '') or '')
        return self._volume_verdict_for(self._peer_identity_keys(nid, host))

    def _apply_sink_gain(self, engine: Any, rule: dict[str, Any]) -> None:
        '''播放端最终增益 = 路由增益 × 来源设备的音量系数。

★ 2026-11-04（Q1）：这是用户要的那条式子，单测钉住它；三个分量也写进
  `describe()`，面板/诊断看到 "0.80 × 0.50 → 0.40" 就能判断乘算有没有发生。
★ 2026-11-04（Q2）：中间那个系数不再无条件等于 `vol` —— 判定出"采集端已衰减"
  时它是 1.0（`_volume_factor_for`），避免把手机音量乘成平方。
'''

        route_gain = float(rule.get('gain', 1.0))
        vol, vol_key = self._peer_media_volume_source(rule)
        verdict = self._volume_verdict_for([vol_key]) if vol_key else None
        if verdict is None:
            verdict = self._volume_verdict_for_rule(rule)
        factor, note = self._volume_factor_for(vol, verdict,
                                               self._peer_vol_source_for([vol_key]) if vol_key else '')
        engine.gain = route_gain * factor
        engine.route_gain = route_gain
        engine.media_vol = vol
        engine.media_vol_source = vol_key
        engine.volume_factor = factor
        engine.volume_note = note
        engine.mute = bool(rule.get('mute'))

    def _apply_stream_volume(self) -> None:
        '''把"来源音量"写到**每条流**上（只有通配路由需要，见 `mix_by_route`）。

        具名路由的播放端增益已经在 `_apply_routes_once` 里乘过，这里的流必须保持
        1.0；否则一路声音会被乘两次（用户提醒过的"平方衰减"）。
        '''

        try:
            items = list(self.receiver.streams.copy().items())
        except Exception:
            return
        try:
            rules = {rid: entry['rule'] for rid, entry in self.desired_sinks().items()}
        except Exception:
            return
        for ssrc, info in items:
            buf = self.receiver.buffers.get(ssrc)
            if buf is not None:
                wildcard = any(('*' in (rules.get(rid, {}).get('from') or []) for rid in buf.route_ids or ()))
                if wildcard:
                    vol = self._peer_media_volume_for_node(info.node_id)
                    verdict = self._volume_verdict_for_node(info.node_id)
                    factor, _note = self._volume_factor_for(
                        vol, verdict, self._peer_vol_source_for(self._peer_identity_keys(str(info.node_id or ''), str(getattr(info, 'host', '') or ''))))
                    buf.source_gain = factor
                else:
                    buf.source_gain = 1.0

    def _peers_with_media_volume(self) -> list[dict[str, Any]]:
        '''`registry.snapshot()` + 每台设备最近上报的媒体音量（Q2 验收用）。

        只读快照，不改发现表。`media_vol=None` 的含义很明确：这台设备没发过 vol
        （老版本手机 / 还没装 2.10），PC 端按 1.0 处理，行为与升级前完全一致。
        '''

        now = time.monotonic()
        peers = self.registry.snapshot()
        for peer in peers:
            value = None
            age = None
            hit_key = ''
            nid = str(peer.get('node_id') or '')
            host = str(peer.get('host') or '')
            keys = self._peer_identity_keys(nid, host)
            # ★ v2.26 第 2 条：诊断必须说清"查了哪些键、哪个命中"。
            #   `media_vol_key` 为空 = 这台设备一条 vol 都没有对上（不是"旧版不发"就一定是键的问题）。
            for key in keys:
                item = self._peer_volume.get(key)
                if item is None:
                    continue
                raw_value, at = item
                if not now - at <= PEER_VOLUME_TTL_S:
                    continue
                value = float(raw_value)
                age = now - at
                hit_key = key
                break
            peer['media_vol'] = round(max(0.0, min(1.0, value)), 2) if value is not None else None
            peer['media_vol_age_s'] = round(age, 1) if age is not None else None
            peer['media_vol_key'] = hit_key
            peer['media_vol_keys'] = keys
            verdict = self._volume_verdict_for(keys)
            source = self._peer_vol_source_for(keys)
            peak = self._received_peak_for(nid) if nid else 0.0
            factor, note = self._volume_factor_for(value if value is not None else 1.0, verdict, source)
            peer['vol_peak'] = round(peak, 2)
            peer['vol_verdict'] = verdict
            # ★ 2026-11-08：电脑端"读到手机当前实际模式"的三个字段（界面中文显示用）。
            peer['vol_mode'] = source or None
            peer['vol_source'] = source or None
            peer['vol_policy'] = self._peer_vol_policy_for(keys) or None
            peer['vol_in_capture'] = True if source == 'silent' else (False if source == 'external' else None)
            peer['volume_factor'] = round(factor, 2)
            peer['volume_note'] = (
                'vol=未上报（已按 node_id/device_id/旧别名/IP 查过 %s，未命中；'
                '手机 2.14 应发 vol，若手机在送声请把诊断里的 vol 键发给我们）'
                % ('、'.join(keys) or '无可用键')
                if value is None else self._volume_decision_text(value, peak, verdict, factor, note))
            declared = self._peer_send_mode_for(nid)
            peer['send_mode'] = declared[0] if declared is not None else None
            peer['broadcast'] = declared[1] if declared is not None else None
            continue
        return peers

    def _request_bluetooth(self, node_id: str, name: str) -> None:
        """对端（手机）点「走蓝牙」：由**电脑这边**去连它的蓝牙。

        用户原话（2026-10-15）：「手机确认走蓝牙后应当由电脑发送蓝牙请求并配对互相连接」。

        为什么必须节流（30 秒一台只试一次）：这条请求会跟着每 2 秒一次的统计包
        幂等地重复到达，而一次连接要跑几秒 PowerShell 起一个 helper —— 不节流就会
        反复去连，把用户正握着的音频链路顶掉。
        """
        key = str(node_id or name or "")
        if not key:
            return
        now = time.monotonic()
        if now - float(self._bt_request_at.get(key, 0.0)) < 30.0:
            return
        self._bt_request_at[key] = now
        self._bt_requests[key] = {"name": name, "at": time.time(), "state": "connecting",
                                 "ok": False}
        self.note("对端「%s」请求走蓝牙，正在连它…" % name)

        def run() -> None:
            try:
                from . import bluetooth as bt

                result = dict(bt.connect_by_name(name) or {})
            except Exception as exc:
                result = {"ok": False, "state": "error", "error": str(exc)}
            result.update({"name": name, "at": time.time()})
            self._bt_requests[key] = result
            self.note("蓝牙连接「%s」：%s" % (name, result.get("error") or result.get("state") or "完成"))

        threading.Thread(target=run, name="audiomixing-bt-request", daemon=True).start()

    def bluetooth_requests(self) -> list[dict[str, Any]]:
        """对端请求过蓝牙的记录（面板 ⑦ 和诊断报告要看）。"""
        return list(self._bt_requests.values())

    def _node_id_for_host(self, host: str) -> str:
        '''按 IP 从发现表反查对端的**真实** node id；查不到返回空串。

为什么要它：T_STATS 的负载里可能没有 node 字段（手机端就是如此），而
"给谁自动开门""跟谁同步音量"都必须认 id。发现表里的对端记录一直带着 id，
按来源 IP 就能对上 —— 也不依赖对端升级。查不到就退回原来的行为（不认领）。

★ 2026-11-04（审计 B1，第 2 轮）：只认真实身份，**不许**把 `manual:<IP>`
  占位当成身份。手动添加设备时 `add_manual()` 会先写一条 `manual:IP`，
  而真实身份（信标 / HTTP 探测）是另一条记录、同一个 host。`resolve(host)`
  按字典顺序可能先命中占位 —— 于是流的 `node_id` 被写成 `manual:IP`，
  而路由里写的是真实 id，永远匹配不上；`_refresh_stream_identities()`
  又只补空身份，这条流于是整场不出声（用户看到的就是"手动加的设备没声音"）。
  这里改成遍历同 host 的记录、只取真实 id（占位一律跳过）；同时按
  `last_seen` 取最新的那台，避免同一 IP 上多条记录时取到旧的。
'''

        if not host:
            return ''
        try:
            peer = self.registry.resolve(host)
        except Exception:
            return ''
        node_id = str(getattr(peer, 'node_id', '') or '')
        if node_id and not node_id.lower().startswith('manual:'):
            if str(getattr(peer, 'host', '') or '') == str(host):
                return node_id
        if not node_id:
            return ''
        try:
            peers = self.registry.all()
        except Exception:
            return ''
        best = None
        for other in peers:
            oid = str(getattr(other, 'node_id', '') or '')
            if not oid or oid == self.node_id or oid.lower().startswith('manual:'):
                continue
            if str(getattr(other, 'host', '') or '') != str(host):
                continue
            if best is None or float(getattr(other, 'last_seen', 0.0) or 0.0) > float(getattr(best, 'last_seen', 0.0) or 0.0):
                best = other
        if best is not None:
            return str(getattr(best, 'node_id', '') or '')
        return ''

    def smart_link_keys(self) -> set[str]:
        """开了「智连」的设备（node_id / 名字，小写）。

        ★ 2026-10-18 用户第 6 条：这张名单取代了原来的两个旧口径
          （全局总开关 auto_accept_listen + "关过一次就永不再自动"的黑名单 listen_optout）。
          旧口径正是他报的第 1 条那个 bug 的根因：手机端关过一次收听，之后电脑再怎么点
          「送给它」，手机都永远不再自动打开收音。现在只有一件事说了算 ——
          **这台设备你有没有点开「智连」**：开了就一定自动，没开就一定不自动，不存在中间态。
        """
        return {str(x).strip().lower()
                for x in (getattr(self.config, "smart_link", None) or []) if str(x).strip()}

    def smart_link_allows(self, node_id: str, name: str='') -> bool:
        '''这台设备有没有开「智连」（node_id 或名字命中都算）。

        ★ 2026-11-04（A 硬规则）：广播模式**不允许**和智连组合。广播是"所有人一起听"，
        智连是"具名点对点自动接"，两者语义冲突；本机广播开着时这里一律返回 False，
        面板上的智连按钮同时置灰并给出原因（见 status 的 smart_available / smart_reason）。
        '''

        if bool(getattr(self.config, 'broadcast', False)):
            return False
        keys = self.smart_link_keys()
        if not keys:
            return False
        return any((str(v).strip().lower() in keys for v in (node_id, name) if str(v).strip()))

    def set_smart_link(self, node_id: str, name: str='', on: bool=True) -> dict[str, Any]:
        '''把一台设备加进/移出「智连」名单（就是设备卡片上那颗按钮）。'''

        if on and bool(getattr(self.config, 'broadcast', False)):
            return {'ok': False, 'on': False, 'reason': 'broadcast', 'error': '广播模式不能和智连组合：智连只服务具名点对点。请先关闭广播。'}
        keys = [str(x).strip() for x in getattr(self.config, 'smart_link', None) or [] if str(x).strip()]
        targets = {k.lower() for k in (node_id, name) if str(k).strip()}
        kept = [k for k in keys if k.lower() not in targets]
        if on:
            for value in node_id, name:
                value = str(value).strip()
                if not value:
                    continue
                if value not in kept:
                    kept.append(value)
        self.config.smart_link = kept[:16]
        try:
            self.config.save()
        except Exception:
            pass
        self.note(f'「智连」{'已开' if on else '已关'!s}：{name or node_id!s}')
        try:
            self.apply_smart_link_now()
        except Exception:
            pass
        return {'ok': True, 'smart_link': kept, 'on': bool(on)}

    def note_manual_listen_off(self, node_id: str, name: str='') -> None:
        '''★ 2026-11-04（A4）手动优先：用户手动关掉那条**自动打开的**收听 →
        这台设备的「智连」按钮随之关闭，直到用户下次人工再打开。

        为什么必须在删除路由的**同一个请求里**做：如果只删路由、智连还开着，
        下一包音频到达 `_queue_auto_listen` 会立刻把它又自动打开 ——
        现场就是用户说的"你越关它越开"。这里一次把名单里的 id/名字都摘掉。
        '''

        caller = str(node_id or '').strip()
        if not caller:
            return
        keys = [str(x).strip() for x in getattr(self.config, 'smart_link', None) or [] if str(x).strip()]
        targets = {caller.lower()}
        for value in name, self._peer_name_of(caller):
            text = str(value or '').strip()
            if text:
                targets.add(text.lower())
        kept = [k for k in keys if k.lower() not in targets]
        if len(kept) == len(keys):
            return
        self.config.smart_link = kept
        try:
            self.config.save()
        except Exception as exc:
            self.note('手动关闭收听时保存智连名单失败：%s' % exc)
        self.note('用户手动关闭自动收听 → 已同时关闭「%s」的智连（下次人工打开才恢复）' % (name or caller))

    def apply_smart_link_now(self) -> dict[str, Any]:
        """「智连」名单刚变 → **立刻**按新名单建/撤自动收听路由，不等下一包统计。

        ★ 2026-11-01 用户：「电脑的智连不是真实反馈，功能不完整」。
          旧实现只把名单写进配置：打开后要等对端下一次送声音（或下一轮统计包）
          才会真的自动收听；关掉后也要等清扫线程下一轮才撤。中间这段时间卡片上的
          按钮已经变色，实际什么也没发生 —— 这就是"不是真实反馈"。
          这里把两件事都做成同步的：关掉的立刻撤（`_sweep_unwanted_auto_routes`），
          打开并且**对端正在送声音**的立刻建收听路由（没在送就不建，避免平白占用
          输出设备，只显示"等它送声音"）。
        """
        # 1) 关掉的那些：把替它自动建的收听路由立刻收回
        try:
            self._sweep_unwanted_auto_routes()
        except Exception as exc:
            self.note("「智连」关闭后的清理失败：%s" % exc)
        # 2) 打开的那些：只对"现在真的有流进来"的设备立刻建路由
        live_ids: set[str] = set()
        try:
            for item in (self.receiver.stats().get("streams") or []):
                node_id = str(item.get("node_id") or "")
                host = str(item.get("host") or "")
                if node_id:
                    live_ids.add(node_id.lower())
                elif host:
                    mapped = self._node_id_for_host(host)
                    if mapped:
                        live_ids.add(mapped.lower())
        except Exception:
            pass
        granted: list[str] = []
        try:
            peers = list(self.registry.snapshot() or [])
        except Exception:
            peers = []
        for item in peers:
            node_id = str(item.get("node_id") or "")
            name = str(item.get("name") or "")
            host = str(item.get("host") or "")
            if not node_id or node_id == self.node_id:
                continue
            if not self.smart_link_allows(node_id, name):
                continue
            if node_id.lower() not in live_ids:
                continue
            try:
                self._grant_listen_request(node_id, name, host)
            except Exception as exc:
                self.note("「智连」立刻收听失败：%s" % exc)
                continue
            if node_id in self._auto_listen_granted:
                granted.append(name or node_id)
        if granted:
            self.note("「智连」已生效：%s 正在送声音，已立刻打开收听" % "、".join(granted))
        return {"granted": granted, "smart": sorted(self.smart_link_keys())}

    def _bt_phone_endpoint(self, phone: str) -> str:
        """这台手机的 A2DP SNK 采集端点名；当前没到就返回空串。

        注意不能把"手机名"直接写进 channel_sources：音源解析认的是完整设备名
        （`麦克风 (<手机> A2DP SNK)`）。2026-10-18 那版自动收编写的是短名，
        所以在 `_live_channel_sources()` 里永远解析不到 —— 看起来"开了智连却没反应"。
        """
        want = str(phone or "").strip()
        if not want:
            return ""
        for dev in (getattr(self, "devices", None) or []):
            if getattr(dev, "kind", "") != audio_base.KIND_SOURCE:
                continue
            if bt_sink_phone_name(getattr(dev, "name", "") or "") == want:
                return str(getattr(dev, "name", "") or "")
        # ★ 2026-10-26：WASAPI 的 ACTIVE 枚举里没有这块 A2DP SNK，但注册表里是 active。
        #   名字仍然要返回；采集端会用注册表里的 MMDevice id 直接 GetDevice。
        try:
            from . import bluetooth as _bt

            for entry in _bt.mmdevice_endpoints(want):
                if entry.get("active") and entry.get("name"):
                    return str(entry["name"])
        except Exception:
            pass
        for selector in (self.config.channel_sources or []):
            if bt_sink_phone_name(str(selector)) == want:
                return str(selector)
        return ""

    def _revoke_auto_listen(self, node_id: str) -> None:
        '''对端停了送声音 → 收回本机替它自动打开的那条收听路由。

        用户真机反馈（2026-10-15）：「手机 WiFi 送声音关闭后，电脑听的选项不会自动关」。
        只认"我们建的"（记录里 created=True）：如果那条路由本来就是用户自己建的，
        当初只是记了一笔，这时绝不能替他删掉 —— 那就变成"程序动了用户的设置"。
        '''

        record = self._auto_listen_granted.pop(node_id, None)
        if not record or not record.get('created'):
            return
        route_id = str(record.get('route_id') or '')
        if not route_id:
            return
        left = [r for r in self.routes if str(r.get('id') or '') != route_id]
        if len(left) == len(self.routes):
            return
        try:
            self.set_routes(left)
            self.note('对方（%s）停了送声音，已收回自动打开的收听' % (record.get('name') or node_id))
        except Exception as exc:
            self.note('收回自动收听失败：%s' % exc)

    def _peer_name_of(self, node_id: str) -> str:
        """node_id → 显示名（注册表里的记录就带着名字）；查不到就回空串。

        为什么不用 webui 里那个 `_name_of`：那是界面层的局部函数，引擎里没有它 —— 直接写
        `self._name_of` 会被 AttributeError 吞掉（except 里返回 False），表现出来就是
        "智连开着也不生效"。这个坑 2026-10-19 就是它造成的，测试当场抓到。
        """
        try:
            peer = self.registry.get(node_id)
            return str(getattr(peer, "name", "") or "")
        except Exception:
            return ""

    def _smart_wants_listen(self, node_id: str) -> bool:
        """要不要请求对端"自动收听本机" —— 只看这张设备的「智连」（默认关）。

        ★ 2026-10-19 用户实测：「无论是否开启智连，设备都自动开启「听」「送」」。
          根因之一是这条请求（want_listen）原来无条件发出去 —— 对端（旧版本尤其）收到就替
          它自己开了收听。智连没开就不发这个请求，「自动开听」从源头上就没有了。
        """
        try:
            return self.smart_link_allows(node_id, self._peer_name_of(node_id))
        except Exception:
            return False

    def _peer_supports_smart(self, node_id: str) -> bool:
        """对端信标里有没有 `smart:1`（它收到 want_listen 后由**它自己**决定）。

        为什么不能只信本机的「智连」：请求的接受方是它，不是我们。旧版本没有这个键，
        只能按老口径保守发（本机开了智连才发），免得旧版本收到就替自己打开收听。
        """
        if not node_id:
            return False
        try:
            peer = self.registry.get(node_id)
            return bool(getattr(peer, "smart", False))
        except Exception:
            return False

    def _is_auto_route(self, rule: dict[str, Any]) -> bool:
        """这条路由是不是"自动建的"。

        ★ 2026-10-19：判据必须**跨版本**成立 —— 对端可能还是旧版本（v1.34 / 没重装的手机），
          它建的那条自动收听路由里**没有 `auto` 字段**（这个字段是 v1.35 才有的），只看 `auto`
          会让旧版本推来的自动路由被当成"用户手动建的"，照样收发、按钮照样自动亮（用户实测
          看到的就是这个）。所以三个特征任一命中就算自动：`auto` 字段 / id 前缀 `auto-listen-` /
          label 文本（这三样里 id 前缀自 v1.28 就没变过）。
        """
        if bool(rule.get("auto")):
            return True
        if str(rule.get("id") or "").startswith("auto-listen-"):
            return True
        return str(rule.get("label") or "").strip() == AUTO_LISTEN_LABEL

    def _auto_route_unwanted(self, rule: dict[str, Any]) -> bool:
        """这条自动路由的"对面那台设备"没有开「智连」吗？（两个方向都按这个判）"""
        if not self._is_auto_route(rule):
            return False
        try:
            for token in list(rule.get("from") or []) + list(rule.get("to") or []):
                token = str(token or "")
                if not token or token == "*":
                    continue
                if is_self_token(token, self.node_id, self.name):
                    continue
                if not self.smart_link_allows(token, self._peer_name_of(token)):
                    return True
            return False
        except Exception:
            return False

    def _sweep_unwanted_auto_routes(self) -> None:
        """清掉"本机没开智连、却被自动建出来"的路由 —— **听、送两个方向都清**。

        ★ 2026-10-19 用户实测：「无论是否开启智连，设备都自动开启「听」「送」」。
          这类路由留着，卡片上那两颗按钮就会自己亮起来（用户看到的就是这个），
          所以直接清掉、并记进"已删除"名单挡一段时间（挡住对端把同样的 id 再推回来），
          让面板如实显示"关着"。等用户真的点了「智连」，对端再请求时才会重新建（那是应该的）。
        两个来源都在这里收口：
          · 本机自己曾经替对端建的（现在请求已经不发，这些是历史残留）；
          · 对端（旧版本）建好推过来的（判据见 _is_auto_route，跨版本认得出来）。
        """
        unwanted = [str(r.get("id") or "") for r in self.routes
                    if r.get("id") and self._auto_route_unwanted(r)]
        if not unwanted:
            return
        gone = set(unwanted)
        self.set_routes([r for r in self.routes if str(r.get("id") or "") not in gone],
                        drop=unwanted)
        self._note_once("清掉了 %d 条「智连没开、却被自动建出来」的路由（听/送两个方向都清）"
                        % len(unwanted))

    def _queue_auto_listen(self, node_id: str, name: str, host: str) -> None:
        """收到一路没有路由的流：如果本机给这台设备开了「智连」，排队自动收听。

        只入队，不改路由表 —— 这个函数在接收线程上被调用，那里正握着 receiver 的锁，
        最多只能记一笔，真正的 set_routes 要回维护线程做。
        """
        # ★ 2026-11-01：手机端的音频流往往**没有 node_id**（它只发 T_AUDIO，不发 HELLO），
        #   这里原来直接 return，正是"手机开了智连、电脑却不自动收"的另一半成因。
        #   按来源 IP 从发现表反查一次即可（不依赖对端升级）。
        if not node_id:
            node_id = self._node_id_for_host(host)
        if not node_id:
            return
        if not name:
            name = self._peer_name_of(node_id)
        try:
            if not self.smart_link_allows(node_id, name):
                return
        except Exception:
            return
        # ★ 2026-11-04（第 8 条）：新的音频包到了 → 上一次的"已挂断"记录立刻作废。
        #   否则卡片会一直写"已挂断 · 待命"，而实际声音已经在流 —— 状态与时间对不上。
        self._auto_listen_hangs.pop(node_id, None)
        with self._pending_auto_lock:
            self._pending_auto_listen[node_id] = {"name": name, "host": host}

    def _apply_pending_auto_listen(self) -> None:
        """维护线程：把排队中的「智连」自动收听落成路由。"""
        with self._pending_auto_lock:
            pending = list(self._pending_auto_listen.items())
            self._pending_auto_listen.clear()
        for node_id, info in pending:
            name = str(info.get("name") or "")
            if not self.smart_link_allows(node_id, name):
                continue
            try:
                self._grant_listen_request(node_id, name, str(info.get("host") or ""))
            except Exception as exc:
                self.note("「智连」自动收听失败：%s" % exc)
                continue
            record = self._auto_listen_granted.get(node_id) or {}
            if record.get("created"):
                self.note("「智连」：%s 正在送声音过来 → 已自动打开收听" % (name or node_id))

    def _grant_listen_request(self, node_id: str, name: str, host: str) -> None:
        '''对端说"我在给你发声音，请收听我" → 自动建一条 从它到本机 的路由。

★ 2026-10-18 用户第 6 条：判据只剩一条 —— **这台设备有没有开「智连」**。
  开了就一定自动打开收听；没开就什么都不做（也不再记"被拒"的账：
  用户明确说那种自动打开的提示/警告不要出现在界面上）。
  顺带修掉用户第 1 条「电脑主动发送声音手机不会主动接收」——
  旧口径里那个"关过一次就永不再自动"的黑名单，在手机端就是同一个病根，两端一起拆了。
'''

        if not node_id or node_id == self.node_id:
            return
        if bool(getattr(self.config, 'broadcast', False)):
            return
        info = self._peer_send_mode.get(node_id)
        if info and bool(info[1]):
            return
        if not self.smart_link_allows(node_id, name):
            return
        self._auto_stop_requested.pop(node_id, None)
        existing = None
        for rule in self.routes:
            frm = [str(t) for t in rule.get('from') or []]
            to = [str(t) for t in rule.get('to') or []]
            if not node_id in frm:
                continue
            if not any((is_self_token(t, self.node_id, self.name) for t in to)):
                continue
            existing = rule
            break
        if existing is not None:
            self._auto_listen_granted[node_id] = {'node_id': node_id, 'name': name, 'host': host, 'route_id': str(existing.get('id') or ''), 'created': False, 'at': time.time(), 'last_audio': time.monotonic()}
            return
        rule = {'id': 'auto-listen-%s' % short_id(node_id), 'from': [node_id], 'to': [self.node_id], 'gain': 1.0, 'mute': False, 'codec': self.config.codec, 'source_device': '', 'sink_device': '', 'label': AUTO_LISTEN_LABEL, 'origin': node_id, 'auto': True}
        try:
            self._dropped_ids.pop(rule['id'], None)
        except Exception:
            pass
        try:
            self.set_routes(list(self.routes) + [rule])
        except Exception as exc:
            self.note('自动收听失败：%s' % exc)
        self._auto_listen_granted[node_id] = {'node_id': node_id, 'name': name, 'host': host, 'route_id': rule['id'], 'created': True, 'at': time.time(), 'last_audio': time.monotonic()}
        self.note('「%s」请求收听本机 → 已自动加了一条路由（可在面板上一键撤销）' % name)

    def _revoke_auto_listen(self, node_id: str) -> None:
        """只撤掉【自动建的】那一条，用户自己建的路由不动。

        ★ 2026-10-18：这里**不再**顺手把设备写进"永不再自动"名单了。
          那个动作是单向的、界面看不见的，而它造成的症状正是用户报的第 1 条
          （对方一旦被记进去，之后对方再怎么送声音都不会再自动打开）。
          现在"要不要自动"只由设备卡片上的「智连」决定，收回一条路由不改变那个选择。
        """
        if not node_id:
            return
        auto_ids = [str(r.get("id")) for r in self.routes
                    if r.get("auto") and node_id in [str(t) for t in r.get("from") or []]]
        if auto_ids:
            kept = [r for r in self.routes if str(r.get("id")) not in auto_ids]
            try:
                self.set_routes(kept, drop=auto_ids)
            except Exception:
                pass
        self._auto_listen_granted.pop(node_id, None)

    def _sweep_auto_listen_hangs(self) -> None:
        '''★ 2026-11-04（A3）智连挂断：双判据都"没了"才收回自动收听。

判据（用户口述，原样实现）：
  ① 音频包静默 ≥ AUTO_LISTEN_HANG_S（**不是**"现在没声音"：静音音频仍有包，
     `last_audio` 会一直刷新，所以静音永远不会挂）；
  ② 发现端口心跳也没了（信标 age ≥ 阈值）；或者对端显式宣告"停止送"
     （这是"数据流结束"的权威证据，可以替代心跳那一条）。
两条同时成立 → 收回**只撤自动建的那条**路由，并写下可观测字段
`audio_silence_s / heartbeat_s / threshold_s`，诊断页直接显示。
'''

        now = time.monotonic()
        for node_id, info in list(self._auto_listen_granted.items()):
            audio_age = max(0.0, now - float(info.get('last_audio') or now))
            peer = self.registry.get(node_id)
            heartbeat_age = float(peer.age_s) if peer is not None else float('inf')
            requested = node_id in self._auto_stop_requested
            if audio_age < AUTO_LISTEN_HANG_S:
                self._auto_listen_hangs.pop(node_id, None)
                continue
            if heartbeat_age < AUTO_LISTEN_HANG_S:
                if not requested:
                    continue
            created = bool(info.get('created'))
            self._auto_listen_hangs[node_id] = {'node_id': node_id, 'name': str(info.get('name') or ''), 'audio_silence_s': round(audio_age, 2), 'heartbeat_s': None if heartbeat_age == float('inf') else round(heartbeat_age, 2), 'threshold_s': AUTO_LISTEN_HANG_S, 'route_kept': not created, 'reason': 'stop_requested' if requested and heartbeat_age < AUTO_LISTEN_HANG_S else 'dual_silence', 'at': time.time()}
            self._auto_stop_requested.pop(node_id, None)
            self._auto_listen_granted.pop(node_id, None)
            if created:
                self._revoke_auto_listen(node_id)
                self.note('「%s」音频包静默 %.1fs 且心跳消失 → 已收回自动收听' % (info.get('name') or node_id, audio_age))
                continue
            self.note('「%s」音频包静默 %.1fs 且心跳消失 → 已挂断（路由保留，状态回到待命）' % (info.get('name') or node_id, audio_age))
            continue

    def _apply_shared_gain(self, peer_id: str, direction: str, value: float, ts: int) -> None:
        """共享增益：对端调了音量条 → 本机对应那条路由跟着变（谁最后动以谁为准）。

        两条链路的语义（与手机端一致）：
          direction='in'  → 它→我（我的「我听它的」），对端用 gain/gain_ts 报过来；
          direction='out' → 我→它（我的「我送它的」），对端用 gain_rx/gain_rx_ts 报过来。
        判新旧的规则（★ 2026-10-16 按真机反馈重写）：
          · 值一样就只对齐时间戳、绝不回推（否则两端会互相弹）；
          · **本机用户** 1.2 秒内刚动过这条 → 不抢（用户正在拖）；
          · 其余情况**最后到达的为准**（远端一次拖动会连发多个中间值，
            必须让最后一个落地；见本函数末尾 2026-11-01 的说明）。
        ★ 为什么不再拿时间戳判新旧：两台机器的墙钟本来就对不齐。原来那条
          「本机 5 秒没动过才采纳对端的值」在真机上的表现正是用户报的两件事 ——
          拖完音量**对端要等约 5 秒才跟上**，而 5 秒一到又会把一个默认值采纳进来、
          **两条音量条自己刷回 1.00**。延迟和回弹是同一个原因。
          时间戳现在只当"我上次改动的时刻"写回去（报给对方看），不参与该不该采纳的决定。
        """
        rule = self._route_between(peer_id, direction)
        if rule is None:
            return
        rid = str(rule.get("id") or "")
        value = max(0.0, min(2.0, float(value)))
        my_ts = int(self._gain_ts.get(rid, 0))
        if abs(float(rule.get("gain", 1.0)) - value) < 0.005:
            self._gain_ts[rid] = max(my_ts, ts)
            return
        # ★ 2026-11-01：这是**镜像回声**，不是对端用户改的音量。
        #   本机刚把 1.30 报过去，对端把它记成自己那个键，下一包又原样报回来；
        #   如果按"最后到达的为准"采纳，本机刚改的值会被自己上一次的旧值覆盖 ——
        #   现场就是"电脑拖完，手机没变，电脑自己又弹回去"。
        published = self._gain_published.get(rid)
        if (published is not None
                and abs(float(published[0]) - value) < 0.005
                and time.monotonic() - float(published[1]) < GAIN_ECHO_WINDOW_S):
            self._gain_ts[rid] = max(my_ts, ts)
            return
        idle = time.monotonic() - self._gain_local_at.get(rid, 0.0)
        if idle < 1.2:
            return
        updated = [dict(r) if str(r.get("id")) == rid else r for r in self.routes]
        for item in updated:
            if str(item.get("id")) == rid:
                item["gain"] = value
        try:
            self.set_routes(updated)
        except Exception:
            return
        # 采纳对端的时间戳：这样我们下次报出去的还是"这个值最后一次被改的时刻"，
        # 不会用自己的 now 把对端的更新顶掉（那就是回环）。
        self._gain_ts[rid] = max(my_ts, ts)
        # ★ 2026-11-01 真机自测（USB 调试、快速拖动手机滑杆）：
        #   这里原来还写了 `_gain_local_at[rid] = time.monotonic()`，于是**采纳一次对端值**
        #   也被当成"本机用户刚动过"，接下来 1.2 秒内的所有远端更新全被挡掉。
        #   一次拖动会发出很多中间值（1.56 → 1.45 → … → 0.51），第一个到达的 1.45 被采纳，
        #   后面的 0.51 全被这条 hold 挡在门外；两端最后就停在 1.45，松手值反而丢了。
        #   这条 hold 的本意只是"别抢本机用户正在拖的滑杆"—— 只有
        #   `note_local_gain_change()`（本机 UI 真的动了）才该写它。远端连续更新必须按顺序落地。
        self.note("对端把「%s」的音量调成了 %.2f，本机同步过去了"
                  % ("我听它的" if direction == "in" else "我送它的", value))

    def note_local_gain_change(self, route_id: str) -> None:
        """用户在本机动了某条路由的音量条 → 记下时刻（判"谁最后动"用）。"""
        rid = str(route_id or "")
        if not rid:
            return
        self._gain_local_at[rid] = time.monotonic()
        self._gain_ts[rid] = int(time.time() * 1000)

    def push_peer_stats_now(self) -> None:
        """本机刚改了路由表 / 音量 → **立刻**把控制消息推给对端，不等那 2 秒一次的周期。

        用户真机反馈（2026-10-16）：
          · 「两边的音量调同步太慢了，要反应很久」—— 拖完滑块，对端最多要等 2 秒才看见；
          · 「跨端自动启停同样慢」—— 电脑开始/停止送音，手机那一端要等 2 秒才开/关收音。
        这两件事都由 `_send_peer_stats` 承载，而它是 2 秒一次的节流循环。
        这里只做两件事：置一个"这一次别等"的标记，然后叫醒维护循环（它本来就有 wake()）——
        UDP 仍然由维护线程去发，HTTP 请求线程不会为了一个包去等网络。
        """
        self._ctl_push_pending = True
        self.wake()

    def auto_listen_state(self) -> dict[str, Any]:
        '''「智连」当前的实况：哪几台设备开了、以及引擎替它们自动建了哪几条收听路由。

★ 2026-10-18 改口径：以前这里还回一份"被拒请求 + 原因"（denied），因为那时
  自动打开是**默认行为**，被挡下来必须让用户看见。现在默认全关、由每台设备的
  「智连」决定（用户第 6 条），所以没有"被拒"这个概念了 —— 字段一起去掉，
  省得留一份没人看、又在 /api/status 里撑着旧逻辑的数据。
'''

        items = []
        live_now = time.monotonic()
        for info in self._auto_listen_granted.values():
            rule_id = str(info.get('route_id') or '')
            alive = any((str(r.get('id')) == rule_id for r in self.routes))
            peer = self.registry.get(str(info.get('node_id') or ''))
            items.append(dict(info, active=alive, age_s=round(time.time() - float(info.get('at') or 0), 1), audio_silence_s=round(max(0.0, live_now - float(info.get('last_audio') or live_now)), 2), heartbeat_s=None if peer is None else round(float(peer.age_s), 2)))
            continue
        broadcast_on = bool(getattr(self.config, 'broadcast', False))
        # ★ 2026-11-04（第 8 条）：挂断记录里的秒数必须是**现在**的，不是判定那一刻的快照。
        #   卡片、诊断、实际 audio/heartbeat 三个数字要对齐：截图 30 秒后就该是 30 秒左右，
        #   不能一直停在"音频静默 4.2s"而状态却还写收听中。
        now_wall = time.time()
        hangs = []
        for item in self._auto_listen_hangs.values():
            live = dict(item)
            recorded = float(item.get('audio_silence_s') or 0.0)
            live['audio_silence_s'] = round(max(0.0, recorded + (now_wall - float(item.get('at') or now_wall))), 2)
            peer = self.registry.get(str(item.get('node_id') or ''))
            beat = None if peer is None else round(float(peer.age_s), 2)
            if beat is None:
                beat = item.get('heartbeat_s')
            else:
                beat = max(float(beat), float(item.get('heartbeat_s') or 0.0))
            live['heartbeat_s'] = beat
            hangs.append(live)
        hangs.sort(key=(lambda item: float(item.get('at') or 0)), reverse=True)
        hangs = hangs[:8]
        return {'items': items, 'label': AUTO_LISTEN_LABEL, 'smart': sorted(getattr(self.config, 'smart_link', None) or []), 'available': not broadcast_on, 'reason': '广播模式不能和智连组合：智连只服务具名点对点。' if broadcast_on else '', 'hang_threshold_s': AUTO_LISTEN_HANG_S, 'hangs': hangs}

    def smart_link_accepts(self, name: str) -> bool:
        """这台设备的声音要不要"自动收下并播放" —— 只看它有没有开「智连」。

        ★ 2026-10-18 用户（图2）：「「正在发送的手机自动播放」就是蓝牙的「智连」」——
          原来那个面板上的全局勾（`config.auto_play`）就是这个判断，现在整块删掉：
          局域网设备看 `smart_link`（卡片上的「智连」），蓝牙设备看 `autoconnect`
          （同一张蓝牙卡片上的「智连」）。默认两张名单都是空的 = 一台都不自动。
        """
        want = str(name or "").strip().lower()
        if not want:
            return False
        try:
            if self.smart_link_allows("", want):
                return True
        except Exception:
            pass
        try:
            return want in {str(x).strip().lower() for x in (self.config.autoconnect or []) if str(x).strip()}
        except Exception:
            return False

    def _auto_play_sources(self) -> None:
        """把**已经开了「智连」的那台设备**正在送进来的声音收编成音源（= 自动播放）。

        ★ 2026-10-18 用户（图2）：这条原来由一个全局开关（面板上「正在发送的手机自动播放」）
          无条件打开 —— 现在改成**逐台设备**判断，判据就是那张卡片上的「智连」，默认关。
          少了这个开关不等于少了能力：没开「智连」的设备仍然可以手动加进「音源」
          （频道那一页的「加入并开直通」，或者手机上自己点「收听」），只是不替用户做主。
        """
        from .audio.backends import netmic as netmic_backend
        try:
            names = [m.name for m in netmic_backend.active()
                     if not m.muted and self.smart_link_accepts(m.name)]
        except Exception:
            return
        # ★ 2026-10-26 用户重新钉死「智连」语义：
        #   蓝牙智连 = 打开软件时自动尝试连接一次配对过的蓝牙设备；
        #   连上后手机媒体音频由 **Windows 直接播放**（用户真机：手机蓝牙在播放，
        #   电脑也能听到），不再把 A2DP SNK 采集端点自动塞进「音源」——
        #   那会让本工具再采一遍、再放一遍，和系统播放叠在一起（回声/断续）。
        #   需要把手机声音转发到别的设备/频道时，仍然可以手动在「音源」里选它。
        if not names:
            return
        # A name whose phone has been gone for a while is dropped from the sources: it
        # would otherwise accumulate forever (renamed phones, one-off tests), and every
        # stale name is one more thing that can keep the mix from opening. Auto-play
        # re-adopts the phone the moment it comes back (while its 「智连」 is on), so
        # nothing is lost.
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
        added = [n for n in names if n not in current]
        if not added:
            return
        self.config.channel_sources = current + added
        if not self.config.local:
            self.config.local = True
        self._note_once("「智连」自动收下正在发送的设备：%s" % "、".join(added))
        try:
            self.config.save()
        except Exception:
            pass

    def _local_capture_names(self) -> set[str]:
        """这台电脑现在真的能采到的设备名（小写）。

        判定口径只有一条：名字出现在本机采集设备清单里。刻意【不】用
        `find_device()`：它还会按 id 匹配，而 Windows 后端给端点编号，
        于是一台早就不在送声音的手机也能"解析"到恰好占了那个编号的随便哪块设备
        （这正是以前把耳机当成音源、还被反馈保护判成啸叫的原因）。
        """
        names = {d.name.strip().lower() for d in self.devices
                 if d.kind == audio_base.KIND_SOURCE and d.name}
        try:
            # 外加一份现列的清单：手机刚开始送声音时，先出现在这里，
            # 比周期性刷新更快一步（周期刷新最多 4 秒一次）。
            names |= {d.name.strip().lower() for d in audio_base.list_all_devices()
                      if d.kind == audio_base.KIND_SOURCE and d.name}
        except Exception:
            pass
        try:
            # ★ 2026-10-26：部分 Intel/Realtek 蓝牙驱动把 A2DP SNK 端点标成 active，
            #   但 WASAPI 的 ACTIVE 枚举里没有它。名字要认，采集端再按注册表 id 打开。
            from . import bluetooth as _bt

            names |= {str(e.get("name") or "").strip().lower()
                      for e in _bt.mmdevice_endpoints("") if e.get("active")}
        except Exception:
            pass
        return names

    def _live_channel_sources(self) -> tuple[list[str], list[str]]:
        """Split the configured sources into (usable now, not available now).

        A name that cannot be resolved would make the whole mix fail to open, and then
        nothing plays at all - which is a very confusing way to react to a phone that
        happens to be away, or to a stale name left behind when a phone was renamed.

        用不了的名单不再"静默丢掉"：`_explain_selectors()` / `channel_sources_report()`
        会把每个名字为什么用不了写成人话，面板直接显示。
        """
        usable: list[str] = []
        missing: list[str] = []
        names = self._local_capture_names()
        for selector in [str(s).strip() for s in self.config.channel_sources if str(s).strip()]:
            if selector.lower() in names:
                usable.append(selector)
            else:
                missing.append(selector)
        return usable, missing

    def _phone_names(self) -> set[str]:
        """之前通过网络送过声音的手机名（小写）。用来区分"拔掉的设备"和"没在送的手机"。"""
        try:
            from .audio.backends import netmic as netmic_backend

            return {str(name).strip().lower() for name in list(netmic_backend.MICS.keys())
                    if str(name).strip()}
        except Exception:
            return set()

    def _peer_names(self) -> dict[str, str]:
        """{局域网设备名(小写): node_id} —— 现在能被发现的其它节点。"""
        out: dict[str, str] = {}
        try:
            for peer in self.registry.all(include_local=False):
                name = str(getattr(peer, "name", "") or "").strip()
                if name:
                    out.setdefault(name.lower(), str(peer.node_id))
        except Exception:
            pass
        return out

    def _classify_missing_source(self, name: str, peers: dict[str, str] | None = None,
                                 phones: set[str] | None = None) -> tuple[str, str]:
        """(类别, 人话原因) —— 这个名字为什么不是本机可用的音源。

        这是缺陷 1 的核心：用户很自然地把"要广播给谁"理解成了"音源是谁"，
        于是把手机的名字选了进来。程序必须说清楚这不是本机的音源，
        而不是收下、然后一个字节都不发。三类原因分开报，处置办法完全不同。
        """
        low = (name or "").strip().lower()
        peers = self._peer_names() if peers is None else peers
        phones = self._phone_names() if phones is None else phones
        if low in peers:
            return "remote_node", ("%s 是网络上的另一台设备，不是本机的音源 —— "
                                   "它必须自己打开「广播我的声音」或「送声音」。" % name)
        if low in phones:
            return "phone_offline", ("%s 这台手机之前送过声音，现在没有在送 —— "
                                     "要它自己打开「送声音」或「广播我的声音」。" % name)
        return "gone", ("%s 现在不是这台电脑上的采集设备：它可能已经被拔掉、断开或改了名；"
                        "如果它其实是另一台手机/电脑，那它必须自己打开「广播我的声音」或「送声音」。"
                        % name)

    def _explain_selectors(self, names: list[str]) -> str:
        """把一批"用不了"的音源名连原因一起写成人话（引擎日志和面板共用）。"""
        peers = self._peer_names()
        phones = self._phone_names()
        out = []
        for name in names:
            _kind, reason = self._classify_missing_source(name, peers, phones)
            out.append(reason)
        return "；".join(out)

    @staticmethod
    def _device_name_key(name: str) -> str:
        """设备名归一化：去掉采集端自己加的 [loopback]/[capture] 后缀，大小写无关。

        同一个端点在这两个地方写法不同：设备清单里是「扬声器 (Realtek(R) Audio)」，
        采集对象的名字会多一个「 [loopback]」。不归一化就会"面板说在采、实际采的是另一块"。
        """
        text = str(name or "").strip().lower()
        for suffix in ("[loopback]", "[capture]"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
        return text

    def _dead_age_for(self, name: str) -> float | None:
        """这块设备"一直没数据"持续了多久（跨采集重建累计）。没记录就是 None。"""
        since = self._dead_since.get(self._device_name_key(name))
        if since is None:
            return None
        return max(0.0, time.monotonic() - since)

    def _track_dead_captures(self) -> None:
        """记录每块采集设备"从什么时候开始一直没有数据"。

        为什么必须跨采集重建来记：一块坏设备（网易 UU 的虚拟声卡就是这样）会被
        看门狗每 6 秒判一次"卡住"并重建，而重建会把 alive_for_s 清零 ——
        只看 alive_for_s 就永远等不到"连续 10 秒没有数据"，自动回退也就永远不会触发。
        这里只按"有没有产出音频"记，与设备类型无关；一旦某块设备真的出数据了就立刻清掉。
        """
        now = time.monotonic()
        try:
            stats = self.hub.stats()
        except Exception:
            stats = []
        for item in stats:
            key = self._device_name_key(item.get("name"))
            if not key:
                continue
            blocks = int(item.get("blocks_read") or 0)
            age = item.get("last_data_age_s")
            fresh = blocks > 0 and (age is None or float(age) <= NO_DATA_WARN_S)
            if fresh:
                self._dead_since.pop(key, None)
            else:
                self._dead_since.setdefault(key, now)

    def _capture_stat_for(self, name: str) -> dict[str, Any]:
        """这块音源在采集端的实时计数（现在没在采它就是空字典）。"""
        low = self._device_name_key(name)
        if not low:
            return {}
        try:
            items = self.hub.stats()
        except Exception:
            return {}
        for item in items:
            cap_name = self._device_name_key(item.get("name"))
            spec_device = self._device_name_key((item.get("spec") or {}).get("device"))
            if low == cap_name or (spec_device and low == spec_device):
                return item
        return {}

    def _capture_data_state(self, stat: dict[str, Any]) -> tuple[str, str, bool]:
        """(状态, 人话原因, 是否"确定采不到") —— 一块采集源现在出不出音频。

        "确定采不到"用来决定要不要对用户说"一个字节都不会发"、要不要自动换设备：
        设备打开后一直没数据、或者已经超过 NO_DATA_WARN_S 没有新音频，都是确定采不到；
        而"还没开始采"只是暂时的，不该吓人。
        """
        if not stat:
            return "not_running", "设备在，但现在没有在采集它", False
        blocks = int(stat.get("blocks_read") or 0)
        age = stat.get("last_data_age_s")
        alive = float(stat.get("alive_for_s") or 0.0)
        if blocks <= 0:
            # 跨重建累计的"一直没数据"时长优先：坏设备每 6 秒被重建一次，alive_for_s 永远很小。
            dead_for = self._dead_age_for(str(stat.get("name") or ""))
            if dead_for is None:
                dead_for = alive
            definite = dead_for > NO_DATA_WARN_S
            reason = ("设备存在但当前没有数据：已经 %.0f 秒没有新音频" % dead_for) if definite \
                else "设备存在但当前没有数据：打开到现在一块音频都没收到"
            return "no_data", reason, definite
        if age is not None and float(age) > NO_DATA_WARN_S:
            return "stalled", ("设备存在但当前没有数据：已经 %.0f 秒没有新音频" % float(age)), True
        # ★ 2026-11-06（第 3 条）：块在走、但一直是 0 —— 蓝牙端点"连上了但在送静音"
        #   就是这个样子：包数健康、面板显示正常，用户听到的却是没声。
        #   这不是"确定采不到"（用户可能只是暂停了播放），所以 definite=False，
        #   只把原因如实说出来，不触发自动换设备。
        silent_for = float(stat.get("silent_for_s") or 0.0)
        if stat.get("silent") and silent_for > SILENT_AFTER_S:
            return "silent", ("设备在出数据，但已经 %.0f 秒没有电平（这块设备当前没有声音在播，"
                              "或蓝牙链路没把音频送过来）" % silent_for), False
        return "ok", "", False

    def channel_sources_report(self) -> dict[str, Any]:
        """「广播我的声音」现在采的是什么、发不发得出去、每个名字为什么不能用。

        为什么要有这个：解析不出来的音源以前被静默丢掉，面板上只有一个 0 B。
        他的真实配置就是：音源清单里只写了【手机的名字】，而这台还被
        `broadcast_excluded` 排除了 —— 广播永远没有音源、一个字节都不发，
        而界面上没有任何地方告诉他这件事。这里把结论写成人话，让面板直接显示。
        """
        configured = [str(s).strip() for s in self.config.channel_sources if str(s).strip()]
        excluded = {str(s).strip() for s in getattr(self.config, "broadcast_excluded", [])}
        usable, _missing = self._live_channel_sources()
        usable_low = {s.lower() for s in usable}
        peers = self._peer_names()
        phones = self._phone_names()

        entries: list[dict[str, Any]] = []
        for name in configured:
            low = name.lower()
            entry: dict[str, Any] = {
                "name": name,
                "excluded": name in excluded,
                "is_local": low in usable_low,
                "state": "ok",
                "reason": "",
            }
            if entry["is_local"]:
                stat = self._capture_stat_for(name)
                state, reason, _definite = self._capture_data_state(stat)
                entry.update({
                    "state": state,
                    "reason": reason,
                    "capturing": bool(stat),
                    "blocks_read": int(stat.get("blocks_read") or 0),
                    "last_data_age_s": stat.get("last_data_age_s"),
                    "peak": stat.get("peak"),
                    "silent_for_s": stat.get("silent_for_s"),
                })
                if name in excluded:
                    entry["state"] = "excluded"
                    entry["reason"] = "已被排除：这台设备自己把「广播」关掉了，广播时会跳过它"
            else:
                kind, reason = self._classify_missing_source(name, peers, phones)
                entry.update({"state": kind, "reason": reason})
                if name in excluded:
                    entry["reason"] += "（它同时还在「已排除」名单里。）"
            entries.append(entry)

        # 广播实际会去采哪几块设备：配置里有能用的就用它们，否则退回「跟随采集设备」
        # （default_source_spec）。面板必须能看到这个"实际"，否则用户以为在采 A、
        # 程序其实在采 B，永远对不上。
        effective: list[str] = []
        try:
            rule = {"kind": "broadcast", "id": CHANNEL_ROUTE_ID, "source_device": ""}
            effective = self._capture_names_for(self._source_spec_for(rule))
        except Exception:
            effective = []

        by_low = {e["name"].lower(): e for e in entries}
        live: list[str] = []
        blocked: list[str] = []
        stuck_ones: list[tuple[str, str, str]] = []   # (名字, 状态, 原因)
        definite = False
        for name in effective:
            entry = by_low.get(name.lower())
            stat = self._capture_stat_for(name)
            state, data_reason, is_definite = self._capture_data_state(stat)
            if state == "ok":
                # 真的在采、而且确实有数据。这里不看"排除了没有"：全部被排除时引擎会
                # 回落到「跟随采集设备」，此刻采的就是默认输出设备，排除名单不参与。
                live.append(name)
                continue
            # 原因里如果已经带了名字（"<名字> 是网络上的另一台设备…"），
            # 外面又写了「名字」，就不必再说两遍。
            reason = (entry or {}).get("reason") or data_reason or "现在不可用"
            if reason.startswith(name):
                reason = reason[len(name):].lstrip(" 　：:，,")
            if entry is not None and not entry["is_local"]:
                # 配置里那个名字根本不是本机设备：它一定开不起来，广播发不出去。
                blocked.append("「%s」%s" % (name, reason))
                definite = True
            else:
                blocked.append("「%s」%s" % (name, data_reason or reason))
                definite = definite or is_definite
            if state in ("stalled", "no_data", "silent"):
                stuck_ones.append((name, state, data_reason))
            elif entry is not None and entry["state"] in ("stalled", "no_data", "silent"):
                stuck_ones.append((name, entry["state"], entry["reason"]))
            elif entry is not None and entry["excluded"]:
                stuck_ones.append((name, "excluded", entry["reason"]))

        # 面板上的警告。scope=capture 的进「音源状态」区域，scope=channel 的进⑥频道卡片。
        warnings: list[dict[str, Any]] = []
        for name, state, reason in stuck_ones:
            if state == "silent":
                advice = ("这块设备有数据但一直是静音：先在那台设备上放一点声音，再看这里；"
                          "如果它是蓝牙（A2DP SNK），确认音频确实在往这台电脑送。")
            else:
                advice = ("这块设备当前没有声音在播（虚拟声卡空转时就是采不到，"
                          "比如网易 UU 远程的虚拟声卡）；换成你真正的输出设备试试 —— "
                          "「频道」的「音源」里改，或「设置」。")
            warnings.append({
                "scope": "capture",
                "level": "warn",
                "source": name,
                "text": "音源「%s」%s。" % (name, reason),
                "advice": advice,
            })

        receivers = 0
        try:
            receivers = len([p for p in self._matching_nodes(["*"])
                             if p.node_id != self.node_id and p.online and p.audio_port])
        except Exception:
            receivers = 0

        # 配置里"现在用不了"的那些条目：不管广播此刻有没有别的音源可用，都要说出来。
        # 缺陷 1 就是这样被藏起来的 —— 用户选了手机的名字，程序收下了，然后什么都不说。
        broken = [e for e in entries if not e["is_local"] or e["excluded"]]
        if self.config.broadcast and broken:
            parts = []
            for e in broken:
                reason = e["reason"] or "现在不可用"
                # 单条展示时原因是以名字开头的完整句子（"<手机名> 是网络上的另一台设备…"）；
                # 汇总成一条时名字已经在「」里了，就不必再说两遍。
                if reason.startswith(e["name"]):
                    reason = reason[len(e["name"]):].lstrip(" 　：:，,")
                parts.append("「%s」%s" % (e["name"], reason))
            warnings.append({
                "scope": "channel",
                "level": "warn" if live else "bad",
                "text": "广播的音源清单里有 %d 条现在用不了：%s" % (len(broken), "；".join(parts)),
                "advice": ("点下面的「移除用不了的音源」一键清掉，再选本机的采集设备 —— "
                           "一般是「扬声器 (...) [loopback]」，也就是这台电脑正在播放的声音。"
                           "网络上的另一台手机/电脑必须自己打开「广播我的声音」或「送声音」。"),
            })

        if self.config.broadcast and not live and definite:
            warnings.append({
                "scope": "channel",
                "level": "bad",
                "text": "广播没有音源，一个字节都不会发。" + "；".join(blocked),
                "advice": ("请在「频道」的「音源」里选一块本机设备 —— 一般是「扬声器 (...) [loopback]」，"
                           "也就是这台电脑正在播放的声音。网络上的另一台手机/电脑不是本机的音源："
                           "它必须自己打开「广播我的声音」或「送声音」。"),
            })
        if self.config.broadcast and receivers == 0:
            warnings.append({
                "scope": "channel",
                "level": "warn",
                "text": ("广播已打开，但局域网上现在没有任何其它设备在线 —— "
                         "就算有音源也没有可发的对象（0 台）。"),
                "advice": ("确认对端设备开着、连在同一个 Wi-Fi 下；手机端还要点「听广播」或「收听」，"
                           "只打开网页不点是不会收的。"),
            })

        return {
            "configured": configured,
            "sources": entries,
            "usable": [e["name"] for e in entries if e["is_local"]],
            "missing": [e["name"] for e in entries if not e["is_local"]],
            "excluded": [e["name"] for e in entries if e["excluded"]],
            "effective": effective,
            "live": live,
            # 广播开着、而且现在确实有一块采集源在出音频，才叫"发得出去"。
            "will_send": bool(self.config.broadcast) and bool(live),
            "receivers": receivers,
            "warnings": warnings,
        }

    def stalled_effective_sources(self) -> list[dict[str, Any]]:
        """现在"确定采不到声音"的那几块音源（广播真正在采的范围内）。"""
        out: list[dict[str, Any]] = []
        try:
            report = self.channel_sources_report()
        except Exception:
            return out
        for name in report.get("effective") or []:
            stat = self._capture_stat_for(name)
            state, reason, definite = self._capture_data_state(stat)
            if definite and state in ("stalled", "no_data"):
                out.append({"name": name, "state": state, "reason": reason,
                            "last_data_age_s": (stat or {}).get("last_data_age_s"),
                            "blocks_read": int((stat or {}).get("blocks_read") or 0)})
        return out

    def capture_candidates(self, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
        """能当"系统声音"音源的候选设备：本机输出设备的回环，真实声卡优先。

        为什么只列输出设备：用户要的是"把电脑正在放的声音送出去"。
        麦克风是另一个用途，不该在"这块设备没有声音"的修复清单里出现。
        """
        skip = {self._device_name_key(x) for x in exclude if str(x or "").strip()}
        default_name = self._default_render_name()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for device in self.devices:
            if device.kind != audio_base.KIND_SINK or not device.name:
                continue
            if str(device.backend or "") in ("file", "null"):
                continue          # 测试音源/写文件的后端不是"能让手机听到声音的设备"
            key = self._device_name_key(device.name)
            if not key or key in skip or key in seen:
                continue
            seen.add(key)
            health = self._capture_health.get(key) or {}
            out.append({
                "device": device.name,
                "virtual": bool(self._looks_virtual(device.name)),
                "is_default": bool(self._same_endpoint(device.name, default_name))
                if default_name else bool(getattr(device, "is_default", False)),
                # alive=True 表示"最近真的试采过并且收到了音频"；None = 还没验过。
                "alive": health.get("alive"),
                "checked_s": health.get("checked_s"),
                "note": health.get("note") or "",
            })
        # 排序：验过有数据的 > 没验过的 > 验过没数据的；同档里默认设备优先、真实声卡优先。
        out.sort(key=lambda c: (
            0 if c["alive"] is True else (1 if c["alive"] is None else 2),
            0 if c["is_default"] else 1,
            1 if c["virtual"] else 0,
            c["device"].lower(),
        ))
        return out

    def probe_candidates_async(self, limit: int = 4) -> dict[str, Any]:
        """把还没验过的候选设备逐个试采一遍（面板上"先验一下候选设备"）。

        试采同一时间只允许一块（`probe_capture_async` 自己会拒绝重复），
        所以这里排队等前一块结束再开下一块；整个过程在后台线程里，
        请求立刻返回，面板过几秒刷新就能看到"有数据 ✓ / 没数据 ✗"。
        """
        pending = [c["device"] for c in self.capture_candidates() if c.get("alive") is None][:max(1, limit)]
        if not pending:
            return {"started": False, "devices": [], "note": "候选设备都已经验过了"}

        def run() -> None:
            for name in pending:
                deadline = time.time() + 8.0
                while self._probe_name and time.time() < deadline:
                    time.sleep(0.2)
                if self._probe_name or not self.probe_capture_async(name):
                    continue
                until = time.time() + CAPTURE_PROBE_S + 4.0
                while self._probe_name and time.time() < until:
                    time.sleep(0.2)

        threading.Thread(target=run, name="audiomixing-probe-all", daemon=True).start()
        return {"started": True, "devices": pending,
                "note": "正在试采 %d 块候选设备（每块最多 %.0f 秒），大约 %d 秒后点「重新检查」。"
                        % (len(pending), CAPTURE_PROBE_S, int((CAPTURE_PROBE_S + 2) * len(pending)))}

    def capture_health(self) -> dict[str, dict[str, Any]]:
        """最近一次试采的结论（面板据此写"有数据 ✓ / 没数据 ✗ / 未验证"）。"""
        return dict(self._capture_health)

    def probe_capture_async(self, name: str) -> bool:
        """在后台试采一块设备：真的收到音频才算"有数据"。已经在试就别重复开。"""
        name = str(name or "").strip()
        if not name or self._probe_name:
            return False
        self._probe_name = name
        self._probe_started = time.monotonic()
        self._probe_result = None

        def run() -> None:
            self._probe_result = (name, self._probe_capture(name))

        threading.Thread(target=run, name="audiomixing-probe", daemon=True).start()
        return True

    def _probe_capture(self, name: str, timeout: float = CAPTURE_PROBE_S) -> dict[str, Any]:
        """真开一次这块设备读几块音频。必须真开：

        设备清单里"存在"和"有数据"是两件事 —— 网易 UU 的虚拟声卡一直在列表里、
        也一直能被打开，但一块音频都收不到（用户机器的真实数据：blocks_read 停在 9512，
        145 秒没有新数据）。不试采就没法说"换上它就有声音"。
        """
        result: dict[str, Any] = {"alive": False, "blocks": 0, "frames": 0,
                                  "peak": 0.0, "error": "", "seconds": 0.0}
        started = time.monotonic()
        try:
            spec = self._single_source_spec(name)
            source = audio_base.open_source(spec)
        except Exception as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            return result
        try:
            source.start()
            deadline = started + timeout
            while time.monotonic() < deadline:
                block = source.read()
                if block:
                    result["blocks"] += 1
                    result["frames"] += len(block) // CHANNELS
                    result["peak"] = max(result["peak"], float(sample_peak(block)))
                    if result["blocks"] >= 3:
                        # 三块（约 30 毫秒）就足够证明"它在出数据"了，不必等满 2 秒。
                        break
            result["alive"] = result["blocks"] > 0
        except Exception as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                source.stop()
            except Exception:
                pass
        result["seconds"] = round(time.monotonic() - started, 2)
        return result

    def _capture_probe_tick(self) -> None:
        """推进"试采 → 决定要不要换"这个状态机（在引擎自己的循环里跑）。"""
        # 1) 收结果
        if self._probe_result is not None:
            name, res = self._probe_result
            self._probe_result = None
            self._probe_name = None
            key = self._device_name_key(name)
            self._capture_health[key] = {
                "alive": bool(res.get("alive")),
                "blocks": int(res.get("blocks") or 0),
                "peak": round(float(res.get("peak") or 0.0), 4),
                "error": str(res.get("error") or ""),
                "checked_s": round(time.monotonic(), 1),
                "note": ("试采到 %d 块音频（电平 %.3f）" % (res.get("blocks") or 0, res.get("peak") or 0.0)
                         if res.get("alive") else
                         ("打不开：%s" % res.get("error")) if res.get("error") else
                         "试采 %.1f 秒，一块音频都没收到" % float(res.get("seconds") or 0)),
            }
        # 2) 试采超时（read() 可能永远不返回）：当作"这块也没数据"，换下一块
        if self._probe_name and (time.monotonic() - self._probe_started) > (CAPTURE_PROBE_S + 3.0):
            key = self._device_name_key(self._probe_name)
            self._capture_health[key] = {"alive": False, "blocks": 0, "peak": 0.0,
                                         "error": "读取一直没有返回",
                                         "checked_s": round(time.monotonic(), 1),
                                         "note": "试采超时：打开后读不到任何数据"}
            self._note_once("试采「%s」超时，当作它也没有数据" % self._probe_name)
            self._probe_name = None

    def _recover_stalled_capture(self) -> None:
        """采集源空转 → 自动换到一块**真的收到过音频**的设备；没有就只告警。

        为什么要有：用户的电脑一直在采「扬声器 (UU远程虚拟音频设备)」——
        一块空转的虚拟声卡。v1.8 只把这件事报出来，用户还是得自己找到该换成哪一块。
        这里只做一件事：**确凿地**换到一块试采成功的设备；否则什么都不动。
        """
        self._capture_probe_tick()
        if self._probe_name:
            return                                   # 正在试采，等结果
        if time.monotonic() - self._last_fallback_at < CAPTURE_FALLBACK_COOLDOWN_S:
            return
        # 便宜的预检：先看采集端自己报的计数里有没有"确定采不到"的。
        # 没有就直接返回 —— 下面那步要重新枚举设备，不能每个循环都做。
        try:
            stats = self.hub.stats()
        except Exception:
            stats = []
        if not any(self._capture_data_state(item)[2] for item in stats):
            return
        stalled = self.stalled_effective_sources()
        if not stalled:
            return
        worst = max(stalled, key=lambda s: float(s.get("last_data_age_s") or 999.0))
        candidates = self.capture_candidates(exclude=[s["name"] for s in stalled])
        if not candidates:
            self._note_once("采集源「%s」一直没有数据，但这台电脑上没有别的输出设备可以换"
                            % worst["name"])
            return
        # 先在已经验过的里面找"有数据"的；没有就挑一块最像真的声卡的试采。
        proven = [c for c in candidates if c.get("alive") is True]
        if proven:
            self._apply_capture_switch(proven[0]["device"], worst, auto=True)
            return
        fresh = [c for c in candidates if c.get("alive") is None and not c.get("virtual")]
        if not fresh:
            fresh = [c for c in candidates if c.get("alive") is None]
        if not fresh:
            self._note_once("采集源「%s」一直没有数据，试过的其它输出设备也都没有数据 —— "
                            "没有自动切换（换过去也一样是静音）。" % worst["name"])
            self._last_fallback_at = time.monotonic()
            return
        self.probe_capture_async(fresh[0]["device"])

    def _apply_capture_switch(self, device: str, stalled: dict[str, Any], auto: bool,
                              note: str = "") -> dict[str, Any]:
        """把频道音源换成 device，并把这次切换明确记下来（日志 + 面板 + 配置）。"""
        stalled_name = str(stalled.get("name") or "")
        age = stalled.get("last_data_age_s")
        why = note or ("「%s」%s" % (stalled_name, stalled.get("reason") or "现在采不到声音") +
                       ("（%.0f 秒没有音频）" % float(age) if age is not None else ""))
        before = [str(s).strip() for s in self.config.channel_sources if str(s).strip()]
        bad = {self._device_name_key(s["name"]) for s in self.stalled_effective_sources()}
        bad |= {self._device_name_key(e["name"]) for e in self.channel_sources_report()["sources"]
                if not e["is_local"] or e["excluded"]}
        kept: list[str] = []
        for name in before:
            if self._device_name_key(name) in bad:
                continue
            kept.append(name)
        if not any(self._device_name_key(name) == self._device_name_key(device) for name in kept):
            kept.append(device)
        changed: list[str] = []
        if kept != before:
            self.config.channel_sources = kept
            changed.append("channel_sources")
        old_device = str(self.config.source_device or "")
        if not self._same_endpoint(old_device, device):
            # 也把"默认采集设备"指过去：否则广播一旦回落到「跟随采集设备」，
            # 又会回到那块坏设备上。
            self.config.source_device = device
            changed.append("source_device")
        if changed:
            try:
                self.config.save()
            except Exception as exc:
                self.note("切换后保存配置失败：%s" % exc)
        self._last_capture_switch = {
            "from": stalled_name or (before[0] if before else ""),
            "to": device,
            "why": why,
            "auto": bool(auto),
            "at": time.time(),
            "changed": changed,
        }
        self._last_fallback_at = time.monotonic()
        self.note("已自动从「%s」切到「%s」—— %s" % (stalled_name or "?", device, why)
                  if auto else "已把音源换成「%s」—— %s" % (device, why))
        # 注意：这里【不】调用 apply_routes()。自动回退是在 apply_routes 内部被调用的，
        # 再进去一次就会重入；下一个循环（约 2 秒后）自然会把新的采集源用上。
        # 面板点的"换成这块设备"走 switch_capture()，那里会立刻生效。
        return self._last_capture_switch

    def switch_capture(self, device: str, why: str = "", auto: bool = False) -> dict[str, Any]:
        """公开入口：把频道音源换成 device（面板的"换成有数据的设备"和自动回退共用）。"""
        device = str(device or "").strip()
        if not device:
            return {"ok": False, "error": "没有指定设备"}
        stalled = self.stalled_effective_sources()
        target = stalled[0] if stalled else {"name": "", "reason": why or "手动换设备"}
        result = self._apply_capture_switch(device, target, auto=auto, note=why)
        self.apply_routes()          # 面板点的：立刻生效，不等下一个循环
        return {"ok": True, "switch": result, "sources": list(self.config.channel_sources),
                "source_device": self.config.source_device}

    def auto_fix_channel(self) -> dict[str, Any]:
        """一键修广播：清掉用不了的音源，选一块真的有数据的设备，勾上广播。

        对应向导第 2 步的【一键修】。判定全部复用 channel_sources_report()，
        不另写一套口径；修完把"改了什么"原样回报给面板写文案。
        """
        before_sources = [str(s).strip() for s in self.config.channel_sources if str(s).strip()]
        report = self.channel_sources_report()
        dropped = [e["name"] for e in report["sources"]
                   if not e["is_local"] or e["excluded"] or e["state"] in ("stalled", "no_data")]
        usable = [e["name"] for e in report["sources"]
                  if e["is_local"] and not e["excluded"] and e["state"] == "ok"]
        picked = ""
        if not usable:
            # 没有一块能用的：从"真的收到过音频"的候选里挑（试采过的优先）。
            candidates = self.capture_candidates(exclude=dropped)
            proven = [c for c in candidates if c.get("alive") is True]
            if proven:
                picked = proven[0]["device"]
            else:
                live = [s.get("name") for s in (self.hub.stats() or []) if self._capture_is_fresh(s)]
                picked = (live[0] if live else "")
                if not picked:
                    # 最后才用"Windows 当前的默认输出"，它是这台电脑正在出声的地方。
                    picked = self._default_render_name() or self._default_loopback_name()
        changed: list[str] = []
        if picked:
            kept = [n for n in before_sources if n not in dropped
                    and self._device_name_key(n) != self._device_name_key(picked)]
            kept.append(picked)
            if kept != before_sources:
                self.config.channel_sources = kept
                changed.append("channel_sources")
            if not self._same_endpoint(str(self.config.source_device or ""), picked):
                self.config.source_device = picked
                changed.append("source_device")
        elif dropped:
            kept = [n for n in before_sources if n not in dropped]
            if kept != before_sources:
                self.config.channel_sources = kept
                changed.append("channel_sources")
        if not self.config.broadcast:
            self.config.broadcast = True
            changed.append("broadcast")
        if not self.config.local:
            self.config.local = True
            changed.append("local")
        if changed:
            try:
                self.config.save()
            except Exception as exc:
                self.note("一键修广播后保存配置失败：%s" % exc)
        note = "已清掉用不了的音源" if dropped else ""
        if picked:
            note = (note + "；" if note else "") + "音源改成「%s」" % picked
        if "broadcast" in changed:
            note = (note + "；" if note else "") + "打开了广播"
        if not note:
            note = "已经是好的，没有改动"
        self.note("一键修广播：" + note)
        if changed:
            self.apply_routes()
        return {"ok": True, "changed": changed, "dropped": dropped, "picked": picked,
                "note": note, "sources": list(self.config.channel_sources)}

    def _capture_is_fresh(self, stat: dict[str, Any]) -> bool:
        """这块采集源现在是不是真的在出数据（面板/向导共用的唯一判据）。"""
        state, _reason, _definite = self._capture_data_state(stat)
        return state == "ok"

    def capture_state(self) -> dict[str, Any]:
        """向导第 1 步要的东西：现在在采什么、有没有数据、能换成什么。"""
        sources = []
        try:
            stats = self.hub.stats()
        except Exception:
            stats = []
        for item in stats:
            state, reason, _definite = self._capture_data_state(item)
            sources.append({
                "name": item.get("name") or "",
                "state": state,
                "reason": reason,
                "blocks_read": int(item.get("blocks_read") or 0),
                "last_data_age_s": item.get("last_data_age_s"),
                "consumers": int(item.get("consumers") or 0),
            })
        effective = []
        try:
            effective = self.channel_sources_report()["effective"]
        except Exception:
            pass
        stalled = self.stalled_effective_sources()
        # 采集对象可能正好卡在"重建的空档"里（坏设备每 6 秒被判一次卡住并重建，
        # 那一瞬间 hub.stats() 里什么都没有）—— 用跨重建的"一直没数据"记录补上，
        # 否则用户点「重新检查」正好赶上空档，看到的会是"采集设备还没起来"。
        if not stalled:
            for name in effective:
                age = self._dead_age_for(name)
                if age is not None and age > NO_DATA_WARN_S:
                    stalled.append({"name": name, "state": "no_data",
                                    "reason": "设备存在但当前没有数据：已经 %.0f 秒没有新音频" % age,
                                    "last_data_age_s": round(age, 1), "blocks_read": 0})
        last = self._last_capture_switch
        return {
            "sources": sources,
            "effective": effective,
            "stalled": stalled,
            "healthy": [s["name"] for s in sources if s["state"] == "ok"],
            "candidates": self.capture_candidates(exclude=[s["name"] for s in stalled]),
            "switch": (dict(last, age_s=round(time.time() - float(last.get("at") or 0), 1))
                       if last else None),
        }

    def _tap_wanted(self) -> bool:
        """现在有没有人需要这份音频：广播开着，或者有听众连着网页。"""
        try:
            if self.config.broadcast:
                return True
        except Exception:
            pass
        try:
            stats = self.tap.stats() or {}
            if int(stats.get("listeners") or 0) > 0:
                return True
        except Exception:
            pass
        return False

    def set_multi_out(self, devices: list[dict[str, Any]]) -> None:
        """设置"本机多设备输出"的设备清单，并立刻生效。"""
        clean: list[dict[str, Any]] = []
        for item in devices or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("device") or "").strip()
            if not name:
                continue
            clean.append({"device": name,
                          "gain": float(item.get("gain", 1.0) or 1.0),
                          "delay_ms": float(item.get("delay_ms", 0.0) or 0.0)})
        self.config.multi_out_devices = clean
        self._apply_multi_out()
        try:
            self.save_config()
        except Exception:
            pass

    def _apply_multi_out(self) -> None:
        """按配置起停"多设备输出"。配置没变时 MultiOut 内部直接返回，不会反复开关设备。"""
        try:
            wanted = list(getattr(self.config, "multi_out_devices", None) or [])
            if not wanted:
                if self.multi_out.running():
                    self.multi_out.stop()
                return
            spec = self.default_source_spec()
            self.multi_out.configure(spec, wanted)
        except Exception as exc:
            self.note("多设备输出出错: %s" % exc)

    def _local_loop_spec(self) -> tuple[Any, dict[str, Any], tuple]:
        """(source, sink spec, change key) for the local capture->render path."""
        channels, missing = self._live_channel_sources()
        if missing:
            # 不再只说"打不开"：说清是网络上的另一台设备、是没在送的手机，还是真被拔了。
            # 用户看到的就是这句话，含糊等于没说。
            self._note_once("音源里这些现在用不了，已跳过：%s" % self._explain_selectors(missing))
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
                    # ★ 2026-10-20：按真实归属标（具名路由=点对点，落在频道那条=广播），
                    #   不要再写"点对点/广播"这种让人以为混淆了两种路的东西。
                    "path_label": ("点对点" if [x for x in str(stream.get("route_id") or "").split(",")
                                                if x and not x.startswith("channel")] else "广播"),
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
            # 音源就是正在放的这块设备本身（例如直接选耳机回环）时，声音本来就在响。
            # 这种情况不该再开一路采集去空转：
            # 我们再开一路回环采集并静音播放，只会多占一份设备和 CPU，
            # 听感就是开始还行、越听越卡。
            if self._tap_wanted():
                # 有人在听：按原设计保留采集，但输出指向 null。
                # 手机/网页听众听得到，本机不会重复播放，所以既不啸叫也不叠音。
                sink_spec = {"backend": "null", "device": "null"}
                key = key + ("tap-only",)
                self._local_note = ("音源 %s 就是输出设备本身：本机不再重复播放，"
                                    "正在采集一份送给广播听众" % "、".join(dropped))
                self._note_once("广播开着（或有人在听），开始把本机声音送给听众")
            else:
                # 没人听：真的停掉，省一份设备和 CPU，也不打扰蓝牙那条路
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

    def enable_output_meter(self) -> bool:
        """打开「当前输出设备电平直读」。返回是否真的开起来了。

        为什么做成显式开关、而不是 start() 里自动开：这是给**桌面应用**看的显示功能。
        单元测试（不少用例是 ``simulate=False``）不该因为跑一次测试就去碰真实声卡；
        真正的服务进程（audiomixing_launcher / ``python -m audiomixing serve``）各调一次。
        """
        if self._out_meter is not None:
            return True
        if self.simulate or "wasapi" not in audio_base.available_backends():
            return False
        meter = _OutputLevelMeter(self._output_meter_spec())
        meter.start()
        self._out_meter = meter
        return True

    def _output_meter_spec(self) -> dict[str, Any]:
        """要直读哪块输出设备的峰值表：优先按配置的播放设备，否则系统默认。"""
        sink = self.default_sink_spec()
        device = str(sink.get("device") or "")
        if str(sink.get("backend")) != "wasapi" or device.lower() in ("", "null", "default", "@default"):
            device = "@default"
        return {"backend": "wasapi", "device": device}

    def output_level(self) -> dict[str, Any]:
        """本机当前"在出声多大声"，0..1 —— 面板上「声音大小」的唯一口径。

        三个来源取最大，因为它们各自只看得到声音的一部分：
          * ``meter`` —— 当前输出设备的峰值表：**Windows 直接播放**的那种声音（手机走蓝牙
            进来就是这一类）只有这里看得到，本工具那条路根本没有数据；
          * ``local`` —— 本机直通那一路实际写进设备的数据；
          * ``sink``  —— 我们自己开的播放端实际渲染的数据。
        三者都沉默时才是真的 0（本机确实没在出声），不会再像以前那样"明明在响却恒为 0"。
        """
        candidates: list[tuple[str, float]] = []
        meter = self._out_meter
        if meter is not None:
            candidates.append(("meter", float(meter.peak or 0.0)))
        if self._loop is not None:
            candidates.append(("local", float(getattr(self._loop, "peak", 0.0) or 0.0)))
        for engine in self.sinks.values():
            candidates.append(("sink", float(getattr(engine, "peak", 0.0) or 0.0)))
        source, peak = ("none", 0.0)
        for name, value in candidates:
            if value > peak:
                source, peak = name, value
        out: dict[str, Any] = {
            "peak": round(min(1.0, max(0.0, peak)), 4),
            "source": source,
            "measured": bool(candidates),
            "meter_on": meter is not None,
        }
        if meter is not None:
            out["device"] = meter.device
            if meter.error:
                out["error"] = meter.error
            # 测不到的时候必须说清是"真的没在出声"还是"这台机器根本没有可直读的设备"，
            # 面板据此写"测不到"而不是拿一个 0 糊弄用户。
            out["meter_ok"] = not meter.error
        return out

    def signals_state(self) -> dict[str, dict[str, Any]]:
        """每台设备的链路信号。**和有没有在传声音没关系**。

        用户原话（2026-10-16）：「虽然二者没有收发声音，但是设备间信号应当存在」——
        原来这一格只在"正在收它的音频流"时才有分数，两台设备明明互相看得见，
        卡片上却写着"还没有可测的链路"。那不是一个显示 bug，是这一格的**判据选错了**。

        现在按三种真实证据取值，从最贴近"声音那条路"到最粗糙，取**最差**的那一个
        （哪条路有毛病都要看得见）：
          1) ``stream``  —— 正在收它的声音：用那一路的丢包/缓冲算；
          2) ``rtt``     —— 有 PING/PONG 样本（电脑对电脑）：用往返时延折算；
          3) ``heartbeat`` —— 只有信标心跳（**手机不回 PING/PONG**，只能走这条）：
                            用心跳新鲜度 + 间隔抖动折算。
        三种证据都没有（例如手动填了个 IP、对方从没回过话）才写"没有依据" ——
        那种情况下界面写"不可测"，绝不拿一个 0 冒充"链路很差"。
        """
        signals: dict[str, dict[str, Any]] = {}
        # 1) 正在收的声音
        try:
            for st in (self.receiver.stats().get("streams") or []):
                node = str(st.get("node_id") or "")
                if not node:
                    continue
                score = _loss_score(st)
                if score is None:
                    continue
                entry = signals.setdefault(node, {})
                entry["stream_score"] = score
                entry["packets"] = int(st.get("packets") or 0)
                entry["lost"] = int(st.get("lost") or 0)
        except Exception:
            pass
        # 2) PING/PONG 往返时延（每秒一轮，和有没有传声音无关）
        try:
            for clock in self.receiver.clock_stats():
                node = str(clock.get("peer") or "")
                if not node or int(clock.get("samples") or 0) <= 0:
                    continue
                rtt = float(clock.get("rtt_ms") or 0.0)
                entry = signals.setdefault(node, {})
                entry["rtt_ms"] = round(rtt, 3)
                entry["rtt_score"] = _rtt_score(rtt)
        except Exception:
            pass
        # 3) 信标心跳（手机也走这一条）
        try:
            for peer in self.registry.all():
                if peer.node_id == self.node_id:
                    continue
                jitter = peer.beat_jitter_ms
                entry = signals.setdefault(peer.node_id, {})
                entry["age_s"] = round(peer.age_s, 1)
                entry["online"] = bool(peer.online)
                if peer.beat_ms is not None:
                    entry["beat_ms"] = round(peer.beat_ms, 1)
                if jitter is not None:
                    entry["beat_jitter_ms"] = jitter
                if peer.online:
                    entry["heartbeat_score"] = _heartbeat_score(peer.age_s, jitter)
        except Exception:
            pass
        for entry in signals.values():
            picked = [(name, entry[key]) for name, key in
                      (("stream", "stream_score"), ("rtt", "rtt_score"),
                       ("heartbeat", "heartbeat_score")) if key in entry]
            if not picked:
                entry["known"] = False
                continue
            source, score = min(picked, key=lambda kv: kv[1])
            entry["known"] = True
            entry["source"] = source
            entry["score"] = int(score)
        return signals

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
        engine.last_progress_ns = engine.started_ns
        engine.thread = threading.Thread(target=self._render_loop, args=(engine,), name="audiomixing-sink-%s" % route_id, daemon=True)
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
        _boost_current_thread()
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
        # ★ 2026-10-26：这块声卡真正的缓冲时长（共享模式由音频引擎决定，不一定是
        #   我们请求的 20ms）。两次喂块之间的间隔一旦超过它，声卡中间就会断音；
        #   以前报告里没有任何字段能看出这件事，只能靠猜"是网络还是声卡"。
        try:
            desc = engine.sink.describe() or {}
            capacity = desc.get("capacity_ms")
            if capacity is None:
                buf_frames = int(desc.get("buffer_frames") or 0)
                capacity = (buf_frames * 1000.0 / SAMPLE_RATE) if buf_frames else desc.get("buffer_ms")
            engine.device_buffer_ms = float(capacity or 0.0)
        except Exception:
            engine.device_buffer_ms = 0.0
        last_tick = time.monotonic()
        engine.last_progress_ns = last_tick
        next_health_check = last_tick + 1.0
        while not engine.stop_event.is_set():
            tick = time.monotonic()
            if engine.blocks:
                gap_ms = (tick - last_tick) * 1000.0
                engine.max_block_gap_ms = max(engine.max_block_gap_ms, gap_ms)
                if engine.device_buffer_ms > 0 and gap_ms > engine.device_buffer_ms:
                    engine.late_blocks += 1
            last_tick = tick
            engine.blocks += 1
            # ★ 2026-10-26 老问题看门狗：端点打开着、写也不报错，但设备从某一刻起
            #   再也不把缓冲里的音频放出去（padding 恒满，write 一直收 0 帧）。
            #   Windows 里手动切换输出就是在重建这个 audio client —— 我们照做。
            #   判据只认"设备真的收下过音频"的时刻，不看路由有没有声音：
            #   空闲时渲染线程也在写静音，帧数本来就应该一直涨。
            if tick >= next_health_check:
                next_health_check = tick + 1.0
                # ★ 不能看 engine.frames_written：WasapiSink.write() 返回的是“输入帧已
                #   接受”，设备一帧没放时它也会把这些帧存进 _out 并返回满值。真正证明
                #   设备还在消费的是它自己的 frames_submitted（实际写进缓冲的帧数）。
                #   非 WASAPI 后端没有这个字段，才退回 frames_written。
                submitted = None
                try:
                    desc = engine.sink.describe() or {}
                    if "frames_submitted" in desc:
                        submitted = int(desc.get("frames_submitted") or 0)
                except Exception:
                    submitted = None
                progress = submitted if submitted is not None else engine.frames_written
                if progress != engine.progress_frames:
                    engine.progress_frames = progress
                    engine.last_progress_ns = tick
                stalled_s = tick - (engine.last_progress_ns or engine.started_ns)
                if engine.blocks > 25 and stalled_s > 3.0:
                    engine.reopen_requested = True
                    engine.last_reopen_reason = "设备 %.1f 秒没有收下任何音频" % stalled_s
                    break
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

    def routes_for_sender(self, node_id: str) -> set[str]:
        '''The local route ids that want audio from `node_id`.

        This is the only correct answer to "which of my sinks does this incoming
        stream belong to"; the receiver cannot know it on its own, because the
        audio packet carries no route id.

        ★ 2026-10-16（用户第 6 条「电脑开只听没有声音」，以及他后来钉死的广播语义）：
        频道收听那条规则的 from 是**通配** `["*"]` —— 它要的是"谁在发就听谁"。
        所以这里不能只按"认得出身份的发送方"去匹配：解析不出身份的流（手动填的 IP、
        信标丢了、路由器做了 AP 隔离）也必须落进那条通配规则里，否则包到了也只能在日志里
        写一句 "has no local sink yet"，用户听到的是静音。

        ★ 但**具名路由优先**：我既开着「听广播」又点名收这一台时，两条规则都能命中同一个
        发送方 —— 同一声声音会被两个播放端各放一遍（音量直接翻倍）。所以先给具名命中的那条，
        一条都没有（不认识这个发送方）才落通配的那条。手机端也写着同一句话
        （「同一台设备的声音如果从两条路都进来，只会播一路、不叠加」）。
        '''

        if not node_id:
            node_id = ''
        specific = set()
        wildcard = set()
        for route_id, entry in self.desired_sinks().items():
            if '*' in (entry['rule'].get('from') or []):
                wildcard.add(route_id)
            elif any((peer.node_id == node_id for peer in entry['peers'])):
                specific.add(route_id)
        # ★ v2.27（Bug-3.2）：对端明确声明"我在广播"时，只走频道「只听」这一条。
        #   以前具名路由优先：手机切到广播后，之前智能收听留下的具名路由仍然命中，
        #   于是电脑没开「只听」也能听到；再打开「只听」就变成两条路由同时挂同一个流，
        #   听感就是"声音变嘈杂"（同一段 PCM 叠着播两遍）。广播语义必须是"只听专属"。
        mode = self._peer_send_mode_for(node_id) if node_id else None
        if mode is not None and bool(mode[1]):
            return wildcard
        if specific:
            return specific
        # ★ 2026-11-06（第 1 条）：v2.28 这里的 `if self.config.listen: return wildcard`
        #   让"没开收听"的点对点流也挂进频道通配路由 → 声音能听到但走频道混音路径，卡。
        #   恢复铁律：点对点发送方必须有点名接受的具名路由；没有就返回空集（不放行）。
        if node_id and self._peer_sends_point_to_point(node_id):
            return set()
        return wildcard

    def _sender_is_known(self, node_id: str) -> bool:
        """这个发送方是不是本机认得出的设备（注册表里有它）？"""
        nid = str(node_id or "")
        if not nid:
            return False
        try:
            return self.registry.get(nid) is not None
        except Exception:
            return False

    def _attach_stream_routes(self, info, addr) -> set[str]:
        """Work out a stream's routes, with a last-resort single-sink fallback."""

        routes = self.routes_for_sender(info.node_id)
        host = addr[0] if addr else ''
        if not routes:
            if len(self.sinks) == 1 and not self._sender_is_known(info.node_id) and self._is_manual_peer(host):
                routes = set(self.sinks)
        return routes

    def _refresh_stream_identities(self) -> None:
        '''★ 2026-11-04（审计 A1 的回归）：给"开播时还没认出来"的流补上身份。

        为什么必须补：`_on_stream_start` 只在第一个包到达时按来源 IP 查一次发现表。
        信标比音频晚到（同机自检、Wi-Fi 刚连上、对方刚启动）时 `node_id` 是空串，
        而空串匹配不上任何具名路由 —— 于是这条流既挂不上路由、也不会建播放端，
        **声音整场都不出来**，而且不会自愈（`node_id` 再不更新）。
        每半秒在调和前补一次：只填空的，不覆盖已有的，也不认领自己。
        '''

        try:
            streams = list(self.receiver.streams.copy().items())
        except Exception:
            return
        for _ssrc, info in streams:
            try:
                if getattr(info, 'node_id', ''):
                    continue
            except Exception:
                continue
            try:
                node_id = self._node_id_for_host(getattr(info, 'host', ''))
                if node_id:
                    if node_id != self.node_id:
                        info.node_id = node_id
            except Exception:
                continue

    def _reattach_stream_routes(self) -> None:
        """Re-resolve every live stream's routes after the table changed.

        A stream that arrived before its sink existed would otherwise stay
        attached to nothing for the rest of the session and never be heard.
        """

        try:
            with self.receiver._lock:
                streams = list(self.receiver.streams.items())
            for ssrc, info in streams:
                routes = self.routes_for_sender(info.node_id)
                if not routes and len(self.sinks) == 1 and not self._sender_is_known(info.node_id) and self._is_manual_peer(getattr(info, 'host', '')):
                    routes = set(self.sinks)
                if not routes and info.node_id:
                    if self.smart_link_allows(info.node_id, getattr(info, 'node_name', '') or ''):
                        try:
                            self._grant_listen_request(info.node_id, getattr(info, 'node_name', '') or '', '')
                        except Exception:
                            pass
                        routes = self.routes_for_sender(info.node_id)
                if not routes:
                    continue
                self.receiver.assign_routes(ssrc, routes)
                continue
        except Exception as exc:
            self.last_error = 'route attach failed: %s' % exc

    def _on_stream_start(self, info, addr) -> None:
        host = addr[0] if addr else ''
        info.host = host
        node_id = self._node_id_for_host(host)
        peer = self.registry.get(node_id) if node_id else None
        if peer is not None:
            info.node_id = node_id
            info.node_name = peer.name
            with self.receiver._lock:
                placeholder = self.receiver._clock.pop('%s:%d' % (addr[0], addr[1]), None)
                if placeholder is not None and peer.node_id not in self.receiver._clock:
                    placeholder.name = peer.node_id
                    self.receiver._clock[peer.node_id] = placeholder
        routes = self._attach_stream_routes(info, addr)
        if routes:
            self.receiver.assign_routes(info.ssrc, routes)
            self.note('stream %08x from %s -> route(s) %s' % (info.ssrc, addr[0] if addr else '?', ','.join(sorted(routes))))
            return
        self._queue_auto_listen(str(getattr(info, 'node_id', '') or ''), str(getattr(info, 'node_name', '') or ''), addr[0] if addr else '')
        self.note('stream %08x from %s has no local sink yet' % (info.ssrc, addr[0] if addr else '?'))

    def peers_json(self) -> list[dict[str, Any]]:
        return self.registry.snapshot()

    def routes_json(self) -> list[dict[str, Any]]:
        '''给界面用的路由表副本：带三态 `state`（不落盘、不删配置）。

        `stale` 只在 `missing`（对端确实不存在）时为真 —— 这是第 1 条回退的落点：
        以前"端点不在线"就算 stale，把活路由也画成虚线；现在 `no_data` / `offline`
        保持实线，只有真 missing 才虚线并进入「清理失效路由」。
        '''

        stale = set(self._stale_route_ids)
        missing = dict(getattr(self, '_stale_route_missing_s', {}) or {})
        states = dict(getattr(self, '_route_states', {}) or {})
        out = []
        for rule in self._dedupe_routes(list(self.routes)):
            item = dict(rule)
            rid = str(item.get('id') or '')
            state = str(states.get(rid) or ('missing' if rid in stale else 'ok'))
            item['state'] = state
            item['stale'] = state == 'missing'
            item['no_data'] = state == 'no_data'
            item['offline'] = state == 'offline'
            if rid in missing:
                item['missing_s'] = missing[rid]
            # ★ v2.26 第 1 条：对外再给一份"identity 归一后的 id"，监控页/诊断统一用它，
            #   不再把 b1fbc20c 这类旧身份当设备名展示。
            item['from_identity'] = [self._display_identity_id(t) for t in (item.get('from') or [])]
            item['to_identity'] = [self._display_identity_id(t) for t in (item.get('to') or [])]
            out.append(item)
        return out

    def _display_identity_id(self, token: Any) -> str:
        '''把路由 token 归一成对端当前的 device_id；解析不到就原样返回（供界面标"旧身份"）。'''
        text = str(token or '').strip()
        if not text or text == '*' or is_self_token(text, self.node_id, self.name):
            return text
        try:
            peer = self.registry.get(text)
        except Exception:
            peer = None
        return self._canonical_peer_id(peer) or text

    def _apply_identity_merges(self) -> None:
        '''把 config.identity_merges 里人工确认过的关系重新套到当前注册表。

        只处理"两条记录都在"的情况；合并结果只影响注册表/别名解析，**不改路由表**，
        所以即使用户的旧路由指向被合并掉的那个 id，解析仍然能命中保留的那条。
        '''

        applied = getattr(self, '_identity_merges_applied', None)
        if applied is None:
            applied = self._identity_merges_applied = set()
        for record in list(getattr(self.config, 'identity_merges', None) or []):
            key = str(record.get('keep') or ''), str(record.get('drop') or '')
            if not key[0] or key in applied:
                continue
            kept = self.registry.get(key[0])
            old = self.registry.get(key[1])
            if kept is None or old is None or kept is old:
                continue
            result = self.registry.merge_identity(key[0], key[1])
            if result.get('ok'):
                applied.add(key)

    def identity_merge(self, keep: str, drop: str) -> dict[str, Any]:
        '''★ 2026-11-04（B 第一阶段）人工确认"这两条是同一台设备"后合并。

        **绝不自动调用**：自动合并错了比双节点更糟（用户原话）。只有面板上人点了
        "疑似同一台设备，是否合并？"才走这里。合并结果写进 config.identity_merges，
        下次启动仍然生效（但每次启动都只是按别名解析，不会凭空删路由）。
        '''

        result = self.registry.merge_identity(keep, drop)
        if not result.get('ok'):
            return result
        merges = list(getattr(self.config, 'identity_merges', None) or [])
        old = self.registry.get(drop)
        drop_ids = [drop]
        if old is not None:
            drop_ids.extend([old.node_id, old.device_id] + list(old.alias_ids))
        record = {'keep': keep, 'drop': drop, 'drop_ids': [x for x in dict.fromkeys(drop_ids) if x],
                  'at': time.time(), 'reason': '人工确认（面板）'}
        merges.append(record)
        self.config.identity_merges = merges[-64:]
        try:
            self.config.save()
        except Exception as exc:
            self.note('身份合并保存失败：%s' % exc)
        self.note(f'身份合并：{keep!s} ← {drop!s}（旧 ID 降级为别名，路由一条都没删）')
        return dict(result, merge=record)

    @staticmethod
    def _bt_key_name(key: str) -> str:
        text = str(key or '').strip()
        return text[3:] if text.lower().startswith('bt:') else text

    @staticmethod
    def _graph_key_id(key: str) -> str:
        """关系图节点键 → 注册表 node_id。

        ★ v2.28（用户第 3 条「蓝牙设备不被认为是一个节点，软件不让我合并」）：
          关系图发给后端的是**图节点键**（`lan:<node_id>` / `bt:<名字>`），
          而 `registry.get()` 要的是裸 node_id。v2.27 直接把 `lan:xxx` 拿去查表，
          永远查不到 → 一律回「找不到要合并的局域网设备」，所以怎么拖都合并不上。
        """
        text = str(key or '').strip()
        low = text.lower()
        for prefix in ('lan:', 'peer:'):
            if low.startswith(prefix):
                return text[len(prefix):]
        return text

    def relation_merge(self, a: str, b: str, a_name: str = '', b_name: str = '') -> dict[str, Any]:
        """★ v2.27（用户第 6 条）：关系图里把一个蓝牙气泡拖到一台局域网设备上合并。

        两条都认识（注册表里有）→ 走 identity_merge（真合并身份）；
        一条是蓝牙快照（没有 node_id）→ 只记 config.relation_merges，纯面板合并，
        随时可以在卡片上「拆开」。
        """
        ka, kb = self._graph_key_id(a), self._graph_key_id(b)
        if not ka or not kb or ka == kb:
            return {'ok': False, 'reason': '需要拖动两个不同的设备气泡'}
        if any(is_self_token(x, self.node_id, self.name) for x in (ka, kb)):
            return {'ok': False, 'reason': '本机不能和别的设备合并'}
        is_bt_a = ka.lower().startswith('bt:')
        is_bt_b = kb.lower().startswith('bt:')
        if is_bt_a and is_bt_b:
            return {'ok': False, 'reason': '两个蓝牙卡片不能合并（请拖到对应的 Wi-Fi 设备上）'}
        pa = None if is_bt_a else self.registry.get(ka)
        pb = None if is_bt_b else self.registry.get(kb)
        if pa is not None and pb is not None:
            out = self.identity_merge(pa.node_id, pb.node_id)
            out['kind'] = 'identity'
            return out
        bt_name = self._bt_key_name(ka if is_bt_a else kb)
        peer = pb if is_bt_a else pa
        if not bt_name or peer is None:
            return {'ok': False, 'reason': '找不到要合并的局域网设备（请等它上线后再拖）'}
        merges = list(getattr(self.config, 'relation_merges', None) or [])
        merges = [m for m in merges
                  if not (str(m.get('bt') or '').lower() == bt_name.lower()
                          and str(m.get('peer') or '') == peer.node_id)]
        record = {'bt': bt_name, 'peer': peer.node_id,
                  'peer_name': peer.name or a_name or b_name, 'at': time.time()}
        merges.append(record)
        self.config.relation_merges = merges[-64:]
        try:
            self.config.save()
        except Exception as exc:
            self.note('关系图合并保存失败：%s' % exc)
        self.note(f'关系图合并：蓝牙「{bt_name}」↔ 设备「{peer.name}」')
        return {'ok': True, 'kind': 'relation', 'merge': record}

    def relation_split(self, key: str, name: str = '') -> dict[str, Any]:
        """★ v2.27：拆开一个合并（身份合并或关系图合并都支持）。"""
        text = self._graph_key_id(key)
        if not text:
            return {'ok': False, 'reason': '没有指定要拆开的设备'}
        changed = False
        kept_merges = []
        for record in list(getattr(self.config, 'relation_merges', None) or []):
            if (text.lower().startswith('bt:')
                    and str(record.get('bt') or '').lower() == self._bt_key_name(text).lower()) \
                    or str(record.get('peer') or '') == text:
                changed = True
            else:
                kept_merges.append(record)
        self.config.relation_merges = kept_merges
        kept_identity = []
        for record in list(getattr(self.config, 'identity_merges', None) or []):
            ids = [str(record.get('keep') or ''), str(record.get('drop') or '')] + list(record.get('drop_ids') or [])
            if text in ids:
                keep = str(record.get('keep') or '')
                drop_ids = list(record.get('drop_ids') or [record.get('drop') or ''])
                self.registry.split_identity(keep, [x for x in drop_ids if x])
                changed = True
            else:
                kept_identity.append(record)
        self.config.identity_merges = kept_identity
        if changed:
            try:
                self.config.save()
            except Exception as exc:
                self.note('拆分保存失败：%s' % exc)
            self.note(f'已拆开：{text}')
            return {'ok': True}
        return {'ok': False, 'reason': '没有找到这条合并记录'}

    def status(self) -> dict[str, Any]:
        recv = self.receiver.stats()
        return {'debug': getattr(self, '_local_debug', {}), 'quality': self.quality_state(), 'inputs': self._inputs_with_history(), 'node': {'id': self.node_id, 'short_id': short_id(self.node_id), 'name': self.name, 'audio_port': self.config.audio_port, 'web_port': self.config.web_port, 'discovery_port': self.config.discovery_port, 'uptime_s': round(time.time() - self.started_at, 1), 'simulate': self.simulate}, 'backends': audio_base.available_backends(), 'opus': _opus_state(), 'devices': {'sources': self.source_list(), 'sinks': self.sink_list()}, 'peers': self._peers_with_media_volume(), 'routes': self.routes_json(), 'channel': self.channel_state(), 'local': self.local_state(), 'output': self.output_level(), 'signals': self.signals_state(), 'senders': self.sender.stats(), 'senders_blocked': self.blocked_senders(), 'auto_listen': self.auto_listen_state(), 'identity': {'device_id': self.device_id, 'instance_id': self.instance_id, 'node_id': self.node_id, 'alias_ids': list(getattr(self.config, 'alias_ids', []) or []), 'merges': list(getattr(self.config, 'identity_merges', []) or []), 'relation_merges': list(getattr(self.config, 'relation_merges', []) or []), 'conflicts': [dict(p.to_json()) for p in self.registry.all() if getattr(p, 'identity_conflict', False)], 'hints': [{'node_id': p.node_id, 'name': p.name, 'reason': p.merge_hint, 'with': p.merge_hint_with, 'device_id': p.device_id} for p in self.registry.all() if getattr(p, 'merge_hint', '')], 'route_rewrites': list(getattr(self, '_identity_rewrites', []) or [])[-32:]}, 'peer_volume_debug': {'ttl_s': PEER_VOLUME_TTL_S, 'keys': sorted(self._peer_volume.keys()), 'last': dict(getattr(self, '_peer_vol_last', {}) or {}), 'series': self._peer_vol_series()}, 'loop_guard': {'origin_id': self.origin_id, 'echo_dropped': int(recv.get('echo_dropped') or 0), 'hop_dropped': int(recv.get('hop_dropped') or 0), 'max_hop': int(recv.get('max_hop') or 0)}, 'listen_ack': dict(self._peer_listen_ack), 'peer_silence_grace_s': PEER_SILENCE_GRACE_S, 'sinks': [engine.describe() for engine in self.sinks.values()], 'multi_out': self.multi_out.stats(), 'sources': self.hub.stats(), 'receiver': recv, 'totals': {'sent_kbps': self.sender.total_kbps(), 'active_senders': len(self.sender.sessions), 'active_sinks': len(self.sinks), 'active_streams': recv.get('active_streams', 0)}, 'discovery': self.beacon.stats() if self.beacon is not None else {}, 'tap': self.tap.stats(), 'log': self.log[-40:], 'last_error': self.last_error, 'captures': self.hub.stats()}


def _opus_state() -> dict[str, Any]:
    """Opus 能不能用。打包后靠这个确认库有没有被带进去。"""
    try:
        from .audio import opus as _opus

        return {"available": bool(_opus.available()), "error": _opus.load_error()}
    except Exception as exc:
        return {"available": False, "error": "%s: %s" % (type(exc).__name__, exc)}

# WARNING: Decompyle incomplete
