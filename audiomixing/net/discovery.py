"""Device discovery: self-contained UDP beacons plus optional manual peers.

Why not mDNS? A pure-stdlib mDNS responder is possible but multicast DNS is
commonly blocked or rate-limited on corporate and guest Wi-Fi, and it cannot
express "which of my devices are allowed to send me audio". A 2-second JSON
beacon on a private group gives us the same UX with none of that fragility, and
`--peer` covers AP-isolated networks where even multicast does not work.

Beacon payload (JSON, <= 1200 bytes):

    {"wl":1,"id":"<uuid>","name":"办公笔记本","platform":"Windows-11",
     "version":"0.1.0","audio_port":47831,"web_port":17800,
     "sources":[{"id":..,"name":"扬声器 (Realtek) [loopback]","detail":"loopback"}],
     "sinks":  [{"id":..,"name":"耳机","detail":"render"}],
     "bt":["小红的手机"],"busy":false,"ctl":1,"disc_port":47830}

`bt` 是我能用**蓝牙**收谁的音（只有名字，没有 id）：手机端点「送声音」时拿它判断
"我这台机器在不在它的蓝牙名单里"，在就先问一句走蓝牙还是 WiFi（2026-10-15）。
老版本读到这个键会忽略。

`ctl:1` + `disc_port` 是**控制面搬家**的协商位（2026-10-15）。这一层信标每 2 秒一发一收、
设计上<b>一直活着</b>；而音频接收端是按需启停的。控制消息（谁请谁收听、共享音量）原来
发到音频端口，对端一停接收器就永远收不到 —— 所以改发到这一层：

    控制消息的外衣 = {"wl":1,"ctl":{……原来 T_STATS 的那份 JSON 正文……}}

收到的判定很简单：`ctl` 是<b>对象</b>就是控制消息，是<b>数字</b>就是能力位。
老版本收到这份外衣会当成"没有 id 的信标"直接丢掉（不会多出一台假设备），
我们对外发包前也先看对端信标里有没有 `ctl:1` —— 没有就照旧走音频端口，
所以新旧混跑不会互相打哑。
"""

from __future__ import annotations

import json
import platform
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..proto import (
    DISCOVERY_GROUP,
    PORT_DISCOVERY,
    SERVICE_TYPE,
    now_ns,
)

BEACON_INTERVAL_S = 2.0
NODE_TIMEOUT_S = 15.0


# HWHHYN 用 DEEPSEEK 制作
def default_node_id() -> str:
    return str(uuid.uuid4())


def short_id(node_id: str) -> str:
    return node_id.replace("-", "")[:6]


def address_rank(host: str, local_ips: set[str] | None = None) -> tuple[int, int]:
    """How good a peer address is; **lower is better**.

    A machine with a VPN overlay (Radmin, Hamachi, ZeroTier, Tailscale) is heard
    on two addresses at once, and both beacons arrive every couple of seconds.
    Picking "whichever arrived last" makes the audio path alternate between a
    direct LAN hop and a tunnel, which shows up as latency that randomly doubles.

    Ranking: an address on one of our own /24s wins, then any other private /
    link-local address, and last anything else (the overlay).
    """
    if not host:
        return (9, 9)
    host = host.strip()
    same_subnet = 1
    ips = local_ips if local_ips else set()
    if host.count(".") == 3:
        prefix = host.rsplit(".", 1)[0] + "."
        if any(ip.startswith(prefix) for ip in ips if ip.count(".") == 3 and not ip.startswith("127.")):
            same_subnet = 0
    return (0 if is_lan_address(host) else 1, same_subnet)


@dataclass
class PeerEndpoints:
    node_id: str
    name: str
    host: str
    audio_port: int
    web_port: int
    platform: str = ""
    version: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    sinks: list[dict[str, Any]] = field(default_factory=list)
    # 这台设备能通过**蓝牙**接收哪些设备的声音（名字列表，见 build_beacon 的 bt）。
    # 手机端"点送声音时先问蓝牙还是 WiFi"就靠这一条：自己的名字在名单里 = 它跟这台
    # 电脑已经配过对，蓝牙那条路是通的（2026-10-15 用户要求）。
    bt_devices: list[str] = field(default_factory=list)
    # ★ 控制面协商位（2026-10-15）：对端信标里 `ctl:1` = 它能收"控制消息走发现层"。
    #   只有它支持，我们才把 T_STATS 改发到它的发现端口；否则照旧发音频端口（向后兼容）。
    ctl: bool = False
    # 对端发现端口的实际端口（信标里的 disc_port）。0 / 缺失 = 按默认 47830 处理。
    disc_port: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    manual: bool = False
    local: bool = False
    # Quality of the address currently in `host`. Kept out of to_json(): it is an
    # internal tie-breaker, not something the control panel needs to render.
    host_rank: tuple[int, int] | None = None
    # ★ 心跳记录（2026-10-16 用户：「虽然二者没有收发声音，但是设备间信号应当存在」）：
    #   每收到一次它的信标就记一个间隔。为什么需要它 —— 手机端**不回 PING/PONG**
    #   （面板上"测连通"那句提示就是这么写的），所以对手机拿不到往返时延；能反映
    #   "这条链路好不好"的只剩信标节奏（本该每 ~2 秒一次，越拖越不稳就越差）。
    #   只留最近 8 个间隔，够算中位数又不至于把很久以前的抖动一直挂在身上。
    _beats: list[float] = field(default_factory=list)

    @property
    def age_s(self) -> float:
        return time.time() - self.last_seen

    @property
    def online(self) -> bool:
        return self.manual or self.age_s < NODE_TIMEOUT_S

    @property
    def beat_ms(self) -> float | None:
        """信标心跳的中位间隔（毫秒）；样本少于 3 次返回 None（不够说明什么）。"""
        if len(self._beats) < 3:
            return None
        ordered = sorted(self._beats)
        return ordered[len(ordered) // 2] * 1000.0

    @property
    def beat_jitter_ms(self) -> float | None:
        """心跳间隔相对中位数的最大偏离（毫秒）：越接近 0 越稳。样本不足返回 None。"""
        if len(self._beats) < 3:
            return None
        ordered = sorted(self._beats)
        return round((ordered[-1] - ordered[len(ordered) // 2]) * 1000.0, 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "short_id": short_id(self.node_id),
            "name": self.name,
            "host": self.host,
            "audio_port": self.audio_port,
            "web_port": self.web_port,
            "platform": self.platform,
            "version": self.version,
            "sources": self.sources,
            "sinks": self.sinks,
            "bt": self.bt_devices,
            # 控制面能力：面板/诊断据此说明"控制消息走的是哪条路"。
            "ctl": self.ctl,
            "disc_port": self.disc_port,
            "age_s": round(self.age_s, 1),
            # 心跳节奏：「设备间信号」在没有音频流时就是靠它算的（见 engine.signals_state）。
            "beat_ms": (round(self.beat_ms, 1) if self.beat_ms is not None else None),
            "beat_jitter_ms": self.beat_jitter_ms,
            "online": self.online,
            "manual": self.manual,
            "local": self.local,
        }


class DiscoveryRegistry:
    """Thread-safe view of every Audio Mixing node heard on the LAN."""

    def __init__(self, node_id: str, local_ips: set[str] | None = None) -> None:
        self.node_id = node_id
        self._peers: dict[str, PeerEndpoints] = {}
        self._lock = threading.Lock()
        self._local_ips: set[str] = set(local_ips or ())

    def set_local_ips(self, local_ips: set[str]) -> None:
        """Tell the registry which addresses belong to this machine.

        Used to rank peer addresses: an address on one of our own subnets is a
        direct LAN path and always beats one that only arrives over a VPN.
        """
        self._local_ips = set(local_ips or ())

    def update(self, payload: dict[str, Any], host: str, *, local: bool = False, manual: bool = False) -> PeerEndpoints | None:
        pid = str(payload.get("id") or "")
        if not pid or pid == self.node_id:
            return None
        rank = address_rank(host, self._local_ips)
        with self._lock:
            peer = self._peers.get(pid)
            if peer is None:
                peer = PeerEndpoints(
                    node_id=pid,
                    name=str(payload.get("name") or short_id(pid)),
                    host=host,
                    audio_port=int(payload.get("audio_port") or 0),
                    web_port=int(payload.get("web_port") or 0),
                    platform=str(payload.get("platform") or ""),
                    version=str(payload.get("version") or ""),
                    manual=manual,
                    local=local,
                    host_rank=rank,
                )
                self._peers[pid] = peer
            # Keep the best address we have heard for this node. A machine on a VPN
            # overlay beacons over *both* the physical LAN and the overlay, so a
            # plain "last packet wins" makes the audio path flap between them; the
            # overlay is slower and jitterier, so it must not win.
            if peer.host_rank is None or rank <= peer.host_rank:
                peer.host = host or peer.host
                peer.host_rank = rank
            peer.name = str(payload.get("name") or peer.name)
            peer.audio_port = int(payload.get("audio_port") or peer.audio_port)
            peer.web_port = int(payload.get("web_port") or peer.web_port)
            peer.platform = str(payload.get("platform") or peer.platform)
            peer.version = str(payload.get("version") or peer.version)
            peer.sources = list(payload.get("sources") or [])
            peer.sinks = list(payload.get("sinks") or [])
            # 老版本没有这个字段 → 解析成空表（不是 None），手机端照旧走 WiFi。
            peer.bt_devices = [str(x) for x in (payload.get("bt") or []) if str(x or "").strip()]
            # ★ 控制面协商位：老版本没有这个键 → False，于是照旧发音频端口（向后兼容）。
            #   只认"真的是 1"，解析不出来就当不支持（宁可多发一份到音频端口，也不能
            #   因为一个看不懂的字段就把控制消息整条丢掉）。
            try:
                peer.ctl = int(payload.get("ctl") or 0) == 1
            except (TypeError, ValueError):
                peer.ctl = False
            try:
                disc_port = int(payload.get("disc_port") or 0)
            except (TypeError, ValueError):
                disc_port = 0
            if disc_port > 0:
                peer.disc_port = disc_port
            if local:
                peer.local = True
            if manual:
                peer.manual = True
            # ★ 记录信标间隔（"设备间信号"在没有音频流时靠它）：太短（同一个节点从
            #   LAN 和 VPN 两次地址各来一份）和太久（中间断了一大截）都不算有效样本，
            #   否则中位数会被这两种情况带歪。
            now = time.time()
            gap = now - peer.last_seen if peer.last_seen else 0.0
            if 0.05 < gap < NODE_TIMEOUT_S:
                peer._beats.append(gap)
                del peer._beats[:-8]
            peer.last_seen = now
            return peer

    def get(self, node_id: str) -> PeerEndpoints | None:
        with self._lock:
            return self._peers.get(node_id)

    def resolve(self, selector: str) -> PeerEndpoints | None:
        """Resolve a node by full id, id prefix, short id, name, or host."""
        if not selector:
            return None
        with self._lock:
            if selector in self._peers:
                return self._peers[selector]
            low = selector.lower()
            for peer in self._peers.values():
                if short_id(peer.node_id) == low or peer.name.lower() == low:
                    return peer
            for peer in self._peers.values():
                if peer.node_id.lower().startswith(low):
                    return peer
            for peer in self._peers.values():
                if low in peer.name.lower() or low in peer.host:
                    return peer
        return None

    def all(self, include_local: bool = True) -> list[PeerEndpoints]:
        with self._lock:
            peers = list(self._peers.values())
        if not include_local:
            peers = [p for p in peers if not p.local]
        peers.sort(key=lambda p: (not p.online, p.name.lower()))
        return peers

    def online(self, include_local: bool = False) -> list[PeerEndpoints]:
        return [p for p in self.all(include_local=include_local) if p.online]

    def register_identity(self, node_id: str, host: str, audio_port: int = 0,
                          web_port: int = 0, name: str = "") -> PeerEndpoints | None:
        """用对端自己报的身份登记它。

        手动添加设备只能填 IP，registry 里那条记录的 node_id 是 "manual:IP"，
        和路由里写的真实节点 ID 对不上，于是即使知道地址也发不出去。
        这里用对端 /api/status 里报的真实 ID 覆盖掉，写清楚主机和端口，
        对端就能被路由正确匹配到。

        这条路完全走"我们主动连对端"，不需要对端开放任何入站，所以
        防火墙挡入站的那台机器也能用它加入。
        """
        host = str(host or "").strip()
        if not host:
            return None
        real = str(node_id or "").strip() or ("manual:" + host)
        with self._lock:
            peer = self._peers.get(real)
            if peer is None:
                peer = PeerEndpoints(
                    node_id=real,
                    name=str(name or host),
                    host=host,
                    audio_port=int(audio_port or 0) or 47831,
                    web_port=int(web_port or 0),
                    manual=True,
                    host_rank=(-1, -1),
                )
                self._peers[real] = peer
            else:
                peer.host = host
                if audio_port:
                    peer.audio_port = int(audio_port)
                if web_port:
                    peer.web_port = int(web_port)
                if name:
                    peer.name = str(name)
                peer.manual = True
            return peer



    def add_manual(self, host: str, audio_port: int = 0, name: str = "") -> PeerEndpoints:
        """Register a node we cannot hear beacons from (AP isolation, routing)."""
        pid = "manual:" + host
        with self._lock:
            peer = self._peers.get(pid)
            if peer is None:
                peer = PeerEndpoints(
                    node_id=pid,
                    name=name or host,
                    host=host,
                    audio_port=audio_port or 47831,
                    web_port=0,
                    manual=True,
                    host_rank=(-1, -1),  # the user asked for this one explicitly
                )
                self._peers[pid] = peer
            peer.last_seen = time.time()
            return peer

    def remove(self, node_id: str) -> bool:
        with self._lock:
            return self._peers.pop(node_id, None) is not None

    def snapshot(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self.all()]


def local_ip_addresses() -> list[str]:
    """Best-effort list of this host's IPv4 addresses (for same-machine detection)."""
    addrs: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    # The routing trick: the local address used to reach the internet.
    for probe in (("8.8.8.8", 80), ("1.1.1.1", 80)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(probe)
            addrs.add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    addrs.add("127.0.0.1")
    return sorted(a for a in addrs if a)


def is_lan_address(host: str) -> bool:
    """True when `host` plausibly belongs to the local network.

    Audio Mixing deliberately has no relay, no hole punching and no cloud rendezvous:
    it only ever sends to addresses on the same LAN. This helper is what the CLI
    and the control panel use to refuse a target that would need the internet.
    """
    import ipaddress

    if not host:
        return False
    h = host.strip().strip("[]")
    if h in ("localhost", "::1"):
        return True
    if h.endswith((".local", ".lan", ".home", ".localdomain")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # A bare hostname with no dot is a NetBIOS/mDNS name -> local by definition.
        # A dotted name would need DNS, which may leave the LAN, so we refuse it.
        return "." not in h
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def lan_report() -> dict[str, Any]:
    """What this machine can see on its local network — used by `audiomixing net`
    and by the control panel, which shows the user where to point other devices."""
    import ipaddress

    interfaces: list[dict[str, Any]] = []
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    addrs = local_ip_addresses()
    reports: list[dict[str, Any]] = []
    for addr in addrs:
        if addr.startswith("127."):
            continue
        subnet = ""
        broadcast = ""
        try:
            iface = ipaddress.ip_interface("%s/24" % addr)
            subnet = str(iface.network)
            broadcast = str(iface.network.broadcast_address)
        except ValueError:
            pass
        reports.append(
            {
                "address": addr,
                "subnet": subnet,
                "broadcast": broadcast,
                "kind": "private" if is_lan_address(addr) else "public",
                "hostname": hostname,
            }
        )
    if not reports:
        return {"interfaces": [], "lan_only": True}
    # Interface with the primary address first (the `ipaddress` import is used
    # above; keep the ordering stable so the UI does not flicker).
    return {"interfaces": reports, "lan_only": True}


def build_beacon(
    node_id: str,
    name: str,
    audio_port: int,
    web_port: int,
    sources: list[dict[str, Any]],
    sinks: list[dict[str, Any]],
    version: str = "0.1.0",
    bt_devices: list[str] | None = None,
    discovery_port: int = 0,
) -> bytes:
    payload = {
        "wl": 1,
        "id": node_id,
        "name": name,
        "platform": "%s-%s" % (platform.system() or "?", platform.release() or "?"),
        "version": version,
        "audio_port": audio_port,
        "web_port": web_port,
        "sources": sources,
        "sinks": sinks,
        # 我能用蓝牙收谁的音（名字列表，只报名字、不报 id）：手机端拿它判断
        # "我这台机器在不在它的蓝牙名单里"，从而决定要不要先问用户走蓝牙还是 WiFi。
        # 老版本读到这个字段会直接忽略（它只认自己认识的那几个键）。
        "bt": list(bt_devices or []),
        # ★ 控制面能力位（见模块开头那段说明）：1 = "请把控制消息发到我的发现端口"。
        #   恒为 1：这一层的收发本来就是每台节点一直在做的事，没有"有时支持"的中间态。
        "ctl": 1,
        # 我实际绑定的发现端口（端口被占时可能不是 47830），对端按它发包。
        "disc_port": int(discovery_port or 0),
        "scope": "lan",  # this protocol never leaves the local network
        "ts": int(time.time()),
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class BeaconService:
    """Announces this node and listens for other nodes' beacons."""

    def __init__(
        self,
        registry: DiscoveryRegistry,
        node_id: str,
        name: str,
        audio_port: int,
        web_port: int,
        source_provider: Callable[[], list[dict[str, Any]]],
        sink_provider: Callable[[], list[dict[str, Any]]],
        port: int = PORT_DISCOVERY,
        group: str = DISCOVERY_GROUP,
        version: str = "0.1.0",
        bt_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self.registry = registry
        self.node_id = node_id
        self.name = name
        self.audio_port = audio_port
        self.web_port = web_port
        self.source_provider = source_provider
        self.sink_provider = sink_provider
        # 可选的第三个提供者：我用蓝牙能收谁的音。默认没有（手机端不需要，电脑端由
        # engine 传进来）。**必须只读缓存**：列蓝牙设备要跑 3 秒 PowerShell，
        # 放在每 2 秒一次的信标里会把广播卡住（见 bluetooth.cached_devices）。
        self.bt_provider = bt_provider
        self.port = port
        self.group = group
        self.version = version
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        self._local_ips = set(local_ip_addresses())
        self.beacons_sent = 0
        self.beacons_received = 0
        # ★ 单播信标（发给"已经见过面的对端"）：手机上「丢设备」的根因就是缺这一条，
        #   所以这两个数要能看见（诊断报告里要写"单播给了几台"）—— 见 _unicast_targets。
        self.beacons_unicast = 0
        self.unicast_targets = 0
        self.last_error: str = ""
        self._manual_targets: list[str] = []

        # ── ★ 控制面（2026-10-15）：控制消息借这一层的 socket 收发 ──────────────
        #
        # 为什么搬到这里：这一层的收发<b>设计上一直活着</b>（每 2 秒一轮，见 _announce_loop
        # 与 _listen_loop）；而音频接收端是按需启停的 —— 用户没在收听、没在听广播、没在
        # 送声音时它是停的。控制消息（"我在把声音送给你，请自动打开收听"、共享音量、
        # "请电脑去连我的蓝牙"）原来发到音频端口，接收器一停对方就永远收不到，
        # 现场表现就是"电脑送声音打开了，手机的收听不会自己打开"。
        #
        # 收到的每一条都攒进这个列表，引擎按 _at 判新旧后再处理（和音频层那个
        # control_messages 的约定完全一样，两处的消费者是同一段代码）。
        self.control_messages: list[dict[str, Any]] = []
        self._ctl_lock = threading.Lock()
        self.ctrl_sent = 0
        self.ctrl_received = 0
        self.ctrl_last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def add_manual_target(self, host: str, port: int | None = None) -> None:
        self._manual_targets.append("%s:%d" % (host, port or self.port))
        # Send a unicast hello immediately so the peer answers without waiting.
        if self._sock is not None:
            try:
                self._send_to(host, port or self.port)
            except OSError:
                pass

    def start(self) -> None:
        self._local_ips = set(local_ip_addresses())
        self.registry.set_local_ips(self._local_ips)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        sock.bind(("0.0.0.0", self.port))
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            self.last_error = "multicast join failed: %s" % exc
        sock.settimeout(1.0)
        self._sock = sock
        self._threads = [
            threading.Thread(target=self._announce_loop, name="audiomixing-beacon-tx", daemon=True),
            threading.Thread(target=self._listen_loop, name="audiomixing-beacon-rx", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    # -- internals ---------------------------------------------------------
    def _beacon_bytes(self) -> bytes:
        try:
            bt = list(self.bt_provider() or []) if self.bt_provider is not None else []
        except Exception:
            # 蓝牙那一路出问题不能连累发现层：没名单就是"没名单"，手机端照旧走 WiFi。
            bt = []
        return build_beacon(
            self.node_id,
            self.name,
            self.audio_port,
            self.web_port,
            self.source_provider(),
            self.sink_provider(),
            self.version,
            bt,
            # 我实际的发现端口（端口被占时 Discovery 会换一个）—— 对端按它发控制消息，
            # 写死 47830 的话换过端口的那一台就再也收不到控制消息。
            self.port,
        )

    def send_control(self, body: dict[str, Any], host: str, port: int) -> bool:
        """把一条控制消息发给某台对端（走的是发现层这个 socket，不是音频端口）。

        外衣是 `{"wl":1,"ctl":{...正文...}}`（见模块开头）。正文<b>原样</b>是原来 T_STATS
        那份 JSON —— 字段名一个都不改，这样收端的解析代码（引擎 / 手机端 Diagnosis.parse）
        完全不用分叉。

        返回是否真的发出去了：调用方据此知道"这条路不通"，而不是以为送达了。
        """
        sock = self._sock
        if sock is None or not host:
            return False
        try:
            data = json.dumps({"wl": 1, "ctl": dict(body)}, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.ctrl_last_error = "控制消息组装失败：%s" % exc
            return False
        try:
            sock.sendto(data, (host, int(port or PORT_DISCOVERY)))
        except OSError as exc:
            self.ctrl_last_error = "%s:%s 发不出去：%s" % (host, port, exc)
            return False
        self.ctrl_sent += 1
        return True

    def _queue_control(self, body: dict[str, Any], addr: tuple[str, int], at: float) -> None:
        """收下一条控制消息：补上和音频层那套一样的 `_from` / `_type` / `_at` 三个字段。

        为什么要补齐：引擎处理这两处消息走的是<b>同一段代码</b>（_handle_peer_requests），
        它只认这三个下划线字段。两边字段不一致就会出现"从发现层收到的消息不生效"这种
        最难查的一类问题。
        """
        msg = dict(body)
        msg["_from"] = "%s:%d" % addr
        kind = str(msg.get("t") or "").strip().lower()
        msg["_type"] = "STATS" if kind == "stats" else (kind.upper() or "CTL")
        msg["_at"] = at
        with self._ctl_lock:
            self.control_messages.append(msg)
            # 和音频层同样的上限：攒着没被消费的消息没有意义（引擎每轮都读最新那条），
            # 但不能无限涨 —— 防的是有人往发现端口乱刷。
            if len(self.control_messages) > 200:
                del self.control_messages[:100]

    def _send_to(self, host: str, port: int) -> None:
        if self._sock is None:
            return
        self._sock.sendto(self._beacon_bytes(), (host, port))

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            data = self._beacon_bytes()
            sock = self._sock
            if sock is None:
                return
            targets = [(self.group, self.port), ("255.255.255.255", self.port)]
            # Also hit each /24 broadcast range we know about, plus manual peers.
            # Only for real LAN addresses: a VPN adapter's "broadcast" (Radmin hands
            # out 26.x.x.x with a /8 mask) is not a /24 and the beacon would be sent
            # to a made-up address instead of the tunnel's actual broadcast.
            for ip in self._local_ips:
                if ip.count(".") == 3 and not ip.startswith("127.") and is_lan_address(ip):
                    targets.append(("%s.255" % ip.rsplit(".", 1)[0], self.port))
            for host_port in self._manual_targets:
                host, _, port = host_port.partition(":")
                targets.append((host, int(port or self.port)))
            # ★ 单播给"已经见过面的对端"（2026-10-15，手机端真机反馈：「手机端经常丢失电脑端」）。
            #
            # 为什么必须有这一条：手机端早就这么做了（见 Discovery.knownPeerHosts 那段注释），
            # 电脑端却只发组播/广播 —— Wi-Fi 省电时安卓会把组播/广播帧成批丢掉（真机实测：
            # 74 秒里 ~37 轮只收到 8 个）。结果是一个很难解释的**不对称**：
            #   · 电脑一直看得见手机（手机在单播给电脑）；
            #   · 手机 15 秒收不到信标就把电脑从列表里摘掉 → 手机"丢设备"、
            #     顺带把"正在送给它"也一起停掉（对端判死的逻辑），于是电脑端看起来像
            #     "收到声音信号却不自动打开收听"。
            # 单播帧不会被 AP 的组播过滤丢掉，只要互相见过一次面，这条路就一直通。
            unicast = self._unicast_targets()
            targets.extend(unicast)
            sent = 0
            for target in targets:
                try:
                    sock.sendto(data, target)
                    sent += 1
                except OSError:
                    continue
            if sent:
                self.beacons_sent += 1
            self.beacons_unicast += len(unicast)
            self.unicast_targets = len(unicast)
            self._stop.wait(BEACON_INTERVAL_S)

    def _unicast_targets(self) -> list[tuple[str, int]]:
        """单播信标要发给谁：最近还在、而且我们知道地址的那些对端。

        上限 16 台（局域网里不会有这么多）—— 防的是被人往发现端口乱刷、把这张表撑爆。
        只发给"最近 15 秒内还有消息"的（与 NODE_TIMEOUT_S 同一个口径）：对端真走了就别再
        一直往一个死地址发。
        """
        out: list[tuple[str, int]] = []
        try:
            peers = self.registry.all(include_local=False)
        except Exception:
            return out
        for peer in peers:
            try:
                if float(getattr(peer, "age_s", 999.0)) > NODE_TIMEOUT_S:
                    continue
            except Exception:
                continue
            host = str(getattr(peer, "host", "") or "")
            if not host or host in {h for h, _p in out}:
                continue
            # 端口用对端自己公布的发现端口（被占时可能不是 47830）；没报就用我们的默认。
            port = int(getattr(peer, "disc_port", 0) or 0) or self.port
            out.append((host, port))
            if len(out) >= 16:
                break
        return out

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("wl") != 1:
                continue
            # ★ 同一个 socket 上跑两种包，靠 ctl 的类型区分（见模块开头）：
            #   ctl 是【对象】→ 控制消息，交给引擎，绝不进设备列表（它没有 id，
            #   万一漏进去就会变成一台"名字是空"的假设备）；
            #   ctl 是【数字】/ 没有 → 信标，照旧更新设备列表。
            ctl = payload.get("ctl")
            if isinstance(ctl, dict):
                self.ctrl_received += 1
                self._queue_control(ctl, addr, time.monotonic())
                continue
            self.beacons_received += 1
            local = addr[0] in self._local_ips
            self.registry.update(payload, addr[0], local=local)

    # -- mDNS compatibility -------------------------------------------------
    def dns_sd_records(self) -> dict[str, Any]:
        """The records a DNS-SD responder would publish for this node.

        Exposed for the (optional) mDNS bridge and for debugging with
        `dns-sd -B _audiomixing._udp`; the beacon above is the primary mechanism.
        """
        return {
            "service": SERVICE_TYPE,
            "instance": "%s.%s" % (self.name, SERVICE_TYPE),
            "port": self.audio_port,
            "txt": {
                "id": self.node_id,
                "name": self.name,
                "web": str(self.web_port),
                "version": self.version,
            },
        }

    def stats(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "group": self.group,
            "sent": self.beacons_sent,
            "received": self.beacons_received,
            # 单播信标：总共发出去多少份、这一轮发给了几台（0 = 还没见过任何对端）
            "unicast_sent": self.beacons_unicast,
            "unicast_targets": self.unicast_targets,
            "peers": len(self.registry.all()),
            "error": self.last_error,
            # ★ 控制面：这一层收发了多少条控制消息（诊断报告据此说明"控制消息真的走了
            #   这条路"，不用再靠猜）。
            "ctl_sent": self.ctrl_sent,
            "ctl_received": self.ctrl_received,
            "ctl_error": self.ctrl_last_error,
        }
