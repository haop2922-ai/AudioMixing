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
     "bt":["小红的手机"],"busy":false,"ctl":1,"disc_port":17830}

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


def _norm_name(name: str) -> str:
    """名字归一：折叠连续空白、忽略大小写。

    2026-11-04（第 2/9 条）：同一台手机在重启/改 id 前后会用不同 node_id 上报，
    但显示名完全一样；判断"是不是同一台"时不能因为多一个尾随空格就当成两台。
    """
    return " ".join(str(name or "").split()).casefold()


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
    # ★ 「智连」协商位（2026-11-01）：对端信标里 `smart:1` = 它收到 want_listen 时，
    #   由**它自己**的「智连」开关决定要不要自动收听。只有它支持，我们才无条件把请求
    #   发过去（请求 ≠ 命令，接收方自己决定）；旧版本没这个键 → 维持旧做法：
    #   只有本机也开了「智连」才发，免得旧版本收到就替自己打开收听。
    smart: bool = False
    # 对端发现端口的实际端口（信标里的 disc_port）。0 / 缺失 = 按默认 17830 处理。
    disc_port: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    manual: bool = False
    local: bool = False
    # ★ 2026-11-04（B 第一阶段身份层）：设备身份与链路。
    #
    # 为什么不能再拿 node_id / IP / MAC 当主键：真机实测「手机一边蓝牙出、一边
    # WiFi 收」时，同一台手机的两条链路被当成两台设备，路由表里两个节点互相指向，
    # 声音绕成圈。现在由设备**自己生成、持久化、可备份**的 `device_id` 当主键，
    # 同一 deviceId 的多条链路（WiFi / 蓝牙 / 其它）合并成一个节点。
    #
    # `alias_ids` 是"以前用过的旧身份"（旧 node_id、manual:IP……），只用于解析路由，
    # **绝不自动合并** —— 合并错了比双节点更糟，所以辅助证据只做提示，最终要人点头。
    device_id: str = ""
    # 每次安装生成；用于发现"换机把旧配置搬过来导致两台真机共用同一个 deviceId"。
    # 同一个 deviceId 但 instanceId 不同 = 两条真机，必须分开显示，不能合并。
    instance_id: str = ""
    alias_ids: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    # ★ 扩展段（originId/hop）协商位：对端信标 `ext:1` = 它能安全解析多出来的 8 字节。
    #   只有它为真时我们才发扩展段；否则发老包（老端一个字节都不变）。
    ext: bool = False
    # 辅助证据（只提示、不自动合并）：对端观测到的蓝牙 MAC / AP 标识。
    observed_bt_mac: str = ""
    # ★ 2026-11-04（第 9 条）：手机自报的蓝牙名。同一台手机的 WiFi 链路与蓝牙链路
    #   会以两个 node_id 出现；蓝牙名与显示名一致时，关系图/合并不必只靠 MAC。
    observed_bt_name: str = ""
    observed_ap: str = ""
    # ★ 2026-11-06（Q2）：对端信标里自报的送声状态（`send_mode`/`broadcast`）。
    #   为什么不能只靠控制消息：控制消息可能只发给"它正在送的目标"，而点对点/广播
    #   切换的那一瞬间本机可能还拿着上一轮的声明。信标每 ~2 秒一发、面向全网，是
    #   "它现在到底在广播还是在点对点"最稳的证据；`send_state_at` 是收到它的时刻
    #   （墙钟），引擎按 PEER_TARGETED_TTL_S 判新鲜度。
    send_mode: int = 0
    broadcast: bool = False
    send_state_at: float = 0.0
    # 同一 deviceId 但 instanceId 冲突 → 疑似"换机搬配置"，面板要给人确认入口。
    identity_conflict: bool = False
    # 辅助证据命中的提示（"疑似同一台设备"）。空 = 没有提示。
    # `merge_hint_with` 是对端 node_id：面板拿它做"是否合并？"的 keep/drop。
    merge_hint: str = ""
    merge_hint_with: str = ""
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
            # 「智连」协商位：面板不显示，引擎据此决定要不要发 want_listen。
            "smart": self.smart,
            "disc_port": self.disc_port,
            # ★ 身份层（2026-11-04）：主键 device_id 与旧身份别名、链路列表、扩展能力。
            #   `aliases` 给面板做"旧 ID 已降级"的说明；路由解析用 registry.resolve。
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "alias_ids": list(self.alias_ids),
            "hosts": list(self.hosts),
            "ext": self.ext,
            "identity_conflict": self.identity_conflict,
            "merge_hint": self.merge_hint,
            "merge_hint_with": self.merge_hint_with,
            "observed_bt_mac": self.observed_bt_mac,
            "observed_bt_name": self.observed_bt_name,
            "observed_ap": self.observed_ap,
            "send_mode": self.send_mode,
            "broadcast": self.broadcast,
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
        # ★ 2026-11-03：这台机器**这台进程里见过一次**的身份（id / 名字 / 地址 / 短 id）。
        #   它和 `_peers` 是两件事：`_peers` 会被 `prune()` 清掉（界面上不再出现幽灵设备），
        #   而"见过"必须留下来 —— 否则一条指向"关机了的真设备"的路由会在清理之后
        #   被 `_route_is_orphan()` 当成"从没见过的假设备"删掉（用户要求不能误删真设备）。
        self._ever: set[str] = set()

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
        # ★ 身份层：设备自报的稳定 device_id / 每次安装的 instance_id。
        #   老端没有这两个键 → 空串，退回"按 node_id 当主键"的老路径（向后兼容）。
        did = str(payload.get("device_id") or payload.get("deviceId") or "").strip()
        iid = str(payload.get("instance_id") or payload.get("instanceId") or "").strip()
        rank = address_rank(host, self._local_ips)
        with self._lock:
            # 同一 deviceId 但 instanceId 不同 = 两台真机搬了同一份配置。
            # 绝不合并；分开放，并把 identity_conflict 标出来让人确认。
            conflict = False
            by_did = self._peers.get(did) if did else None
            if by_did is not None and by_did.instance_id and iid:
                conflict = by_did.instance_id != iid
            lookup = ("%s@%s" % (did, iid)) if conflict else (did or pid)
            peer = self._peers.get(lookup)
            if peer is None and not conflict:
                # 老记录可能是按 node_id 存的（升级前 / 老端）：并到 deviceId 主键下。
                old = self._peers.pop(pid, None) if did else None
                peer = old
                if old is not None:
                    old.device_id = did
                    if pid != old.node_id:
                        self._add_alias(old, pid)
                    self._peers[did] = old
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
                self._peers[lookup] = peer
            elif pid != peer.node_id:
                # 同一台设备的另一条链路：合并进同一个节点，旧 id 只做别名解析。
                self._add_alias(peer, pid)
            if did:
                peer.device_id = did
            if iid:
                peer.instance_id = iid
            peer.identity_conflict = conflict or peer.identity_conflict
            if conflict:
                # 冲突时旧设备也要标出来（面板两边都提示，不能只标新来的）。
                if by_did is not None:
                    by_did.identity_conflict = True
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
            # 同一台设备的每条链路都留在 hosts 里（面板显示"WiFi/其它链路 N 条"）；
            # 真正走音频的仍是排过序的 peer.host。
            self._add_host(peer, host)
            # ★ 扩展段协商位：老端没有 ext 键 → False，于是继续发老包（向后兼容）。
            try:
                peer.ext = int(payload.get("ext") or 0) == 1
            except (TypeError, ValueError):
                peer.ext = False
            # 辅助证据：只存下来做"疑似同一台设备"的提示，**绝不据此自动合并**。
            peer.observed_bt_mac = str(payload.get("bt_mac") or "").strip().lower()
            peer.observed_bt_name = str(payload.get("bt_name") or "").strip()
            peer.observed_ap = str(payload.get("ap") or payload.get("ap_bssid") or "").strip().lower()
            # ★ 2026-11-06（Q2）：只在对端**真的带了**这个键时才更新 —— 老手机不发
            #   这两个键，不能把"没声明"当成"没在广播"，否则又会退回"能听见但被拒"。
            raw_mode = payload.get("send_mode", None)
            if raw_mode is not None:
                try:
                    peer.send_mode = max(0, min(2, int(raw_mode)))
                    raw_b = payload.get("broadcast")
                    if isinstance(raw_b, bool):
                        peer.broadcast = raw_b
                    else:
                        peer.broadcast = str(raw_b or "").strip().lower() in (
                            "1", "true", "yes", "on")
                    peer.send_state_at = time.time()
                except (TypeError, ValueError):
                    pass
            # 老版本没有这个字段 → 解析成空表（不是 None），手机端照旧走 WiFi。
            peer.bt_devices = [str(x) for x in (payload.get("bt") or []) if str(x or "").strip()]
            # ★ 控制面协商位：老版本没有这个键 → False，于是照旧发音频端口（向后兼容）。
            #   只认"真的是 1"，解析不出来就当不支持（宁可多发一份到音频端口，也不能
            #   因为一个看不懂的字段就把控制消息整条丢掉）。
            try:
                peer.ctl = int(payload.get("ctl") or 0) == 1
            except (TypeError, ValueError):
                peer.ctl = False
            # ★ 老版本没有 smart 键 → False（继续按旧的保守口径发请求）。
            try:
                peer.smart = int(payload.get("smart") or 0) == 1
            except (TypeError, ValueError):
                peer.smart = False
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
            self._merge_same_host_duplicates(peer)
            self._note_merge_hints(peer)
            self._remember(peer)
            return peer

    # -- 身份层内部工具（调用方已持锁） -----------------------------------
    def _add_alias(self, peer: PeerEndpoints, token: str) -> None:
        text = str(token or "").strip()
        if text and text != peer.node_id and text not in peer.alias_ids:
            peer.alias_ids.append(text)
            # 别名不参与显示，但必须让"这身份见过"留下来，路由清理不能误删。
            self._ever.add(text.lower())

    def _add_host(self, peer: PeerEndpoints, host: str) -> None:
        text = str(host or "").strip()
        if not text:
            return
        if text not in peer.hosts:
            peer.hosts.append(text)
            del peer.hosts[:-8]

    def _merge_same_host_duplicates(self, peer: PeerEndpoints) -> None:
        """同一 IP 的重复记录并成一条。调用方已持锁。

        2026-11-04（第 2 条真机现象）：同一台手机重启后换了 node_id，旧记录还留着，
        面板就画出两张卡（一张挂着"XX 秒没有消息"的红字）。这里只用**强证据**合并：
        `host` 相同 + 名字归一后相同（platform 两边都有时也要相同）。

        第二种现场（同一台手机换身份后名字也变了，`机型A-xxxx` / `机型A-yyyy`）：
        同一 IP 上**旧的那条已经超时**，且两条名字的机型前缀相同 —— 才并。
        「旧的已超时」是必要条件：两台真机不可能同时占用同一个 IP；而 DHCP 复用
        到同型号的另一台手机时，机型前缀相同也只会在旧记录超时后才命中。

        蓝牙 MAC / 同一 AP 这类弱证据仍然只做提示，不在这里合并 —— **宁可多一张卡，
        也不能把两台真机并成一台**。manual（用户明确添加）不参与；`identity_conflict`
        （同 deviceId 不同 instanceId = 两台真机搬了配置）也不参与。
        """
        host = str(peer.host or "").strip()
        name = _norm_name(peer.name)
        if not host or not name:
            return
        platform = str(peer.platform or "").strip().lower()
        for key, other in list(self._peers.items()):
            if other is peer or other.manual or other.local != peer.local:
                continue
            if other.identity_conflict or peer.identity_conflict:
                continue
            if str(other.host or "").strip() != host:
                continue
            other_platform = str(other.platform or "").strip().lower()
            if platform and other_platform and platform != other_platform:
                continue
            same_name = _norm_name(other.name) == name
            if not same_name:
                # 名字必须"同一机型前缀"（`机型A-xxxx` / `机型A-yyyy` → `机型a`），
                # 且旧记录真的已经超时。两条都新鲜、名字又不同 → 绝不合并。
                if not self._same_name_family(peer.name, other.name):
                    continue
                if float(other.age_s) < NODE_TIMEOUT_S:
                    continue
            # 并进**刚刚更新过的**那条（它的 last_seen / host_rank 才是最新的）。
            self._add_alias(peer, other.node_id)
            if other.device_id:
                self._add_alias(peer, other.device_id)
            for alias in other.alias_ids:
                self._add_alias(peer, alias)
            for other_host in other.hosts:
                self._add_host(peer, other_host)
            if not peer.observed_bt_mac:
                peer.observed_bt_mac = other.observed_bt_mac
            if not peer.observed_bt_name:
                peer.observed_bt_name = other.observed_bt_name
            self._peers.pop(key, None)

    def _same_name_family(self, a: str, b: str) -> bool:
        """两个显示名是不是"同一机型、不同短 id"（`机型A-xxxx` vs `机型A-yyyy`）。"""
        first, second = _norm_name(a), _norm_name(b)
        if not first or not second or "-" not in first or "-" not in second:
            return False
        return first.rsplit("-", 1)[0] == second.rsplit("-", 1)[0]

    def _note_merge_hints(self, peer: PeerEndpoints) -> None:
        """辅助证据只提示、不合并。

        判据刻意很窄：两条记录都报了对端观测到的**同一个蓝牙 MAC**，并且在同一 AP。
        "同一 AP"单独出现没有意义（整间办公室都在同一个 AP），绝不能拿它合并。
        命中时只写 merge_hint，面板弹出"疑似同一台设备，是否合并？"，由人决定。
        """
        if not peer.observed_bt_mac:
            return
        for other in self._peers.values():
            if other is peer or not other.observed_bt_mac:
                continue
            if other.observed_bt_mac != peer.observed_bt_mac:
                continue
            same_ap = bool(peer.observed_ap and other.observed_ap and peer.observed_ap == other.observed_ap)
            if same_ap:
                peer.merge_hint = "同一蓝牙 MAC + 同一 AP"
                other.merge_hint = peer.merge_hint
                peer.merge_hint_with = other.node_id
                other.merge_hint_with = peer.node_id

    def _remember(self, peer: PeerEndpoints) -> None:
        """记下这台设备用过的每一个可写进路由的标识。调用方已持有 `_lock`。"""
        for token in (peer.node_id, short_id(peer.node_id), peer.name, peer.host,
                      peer.device_id, *peer.alias_ids, *peer.hosts):
            text = str(token or "").strip().lower()
            if text:
                self._ever.add(text)

    def ever_seen(self, selector: str) -> bool:
        """这台机器这台进程里有没有见过这个 id / 名字 / 地址（不管现在在不在线）。"""
        text = str(selector or "").strip().lower()
        if not text:
            return False
        with self._lock:
            return text in self._ever

    def prune(self, ttl_s: float = 60.0) -> list[str]:
        """清掉超过 `ttl_s` 没有音讯的自动发现设备，返回被清掉的 node_id 列表。

        ★ 2026-11-03 用户（图1）：`xxxxxxxx-xxxx-…` 这类"不在线 / 不可达"的
        历史节点一直留在设备图和列表里。根因是 `_peers` 只增不减：
        手机重装/改 id 之后旧身份永远在表里，界面就一直画着它。
        这里按"最后一次信标"淘汰；**手动添加的设备不删**（那是用户明确要求的），
        "见过"另存在 `_ever` 里，所以路由清理逻辑不会因此误删真设备。
        """
        now = time.time()
        removed: list[str] = []
        with self._lock:
            for pid, peer in list(self._peers.items()):
                if peer.manual:
                    continue
                if now - float(peer.last_seen or 0.0) > ttl_s:
                    self._peers.pop(pid, None)
                    removed.append(pid)
        return removed

    def get(self, node_id: str) -> PeerEndpoints | None:
        """按 node_id / device_id / 旧别名取记录。

        ★ 2026-11-04：主键改成 deviceId 之后，引擎里大量 `registry.get(node_id)`
        还会拿对端自报的 node_id 来查；直接 `_peers.get()` 会全部查空。这里先查
        主键，再按 node_id / alias_ids 扫一遍，保证老调用点一行都不用改。
        """
        if not node_id:
            return None
        with self._lock:
            peer = self._peers.get(node_id)
            if peer is not None:
                return peer
            low = str(node_id).lower()
            for candidate in self._peers.values():
                if candidate.node_id.lower() == low or candidate.device_id.lower() == low:
                    return candidate
                if any(alias.lower() == low for alias in candidate.alias_ids):
                    return candidate
        return None

    def resolve(self, selector: str) -> PeerEndpoints | None:
        """Resolve a node by full id, id prefix, short id, name, or host."""
        if not selector:
            return None
        with self._lock:
            if selector in self._peers:
                return self._peers[selector]
            low = selector.lower()
            for peer in self._peers.values():
                if (short_id(peer.node_id) == low or peer.name.lower() == low
                        or peer.device_id.lower() == low
                        or any(alias.lower() == low for alias in peer.alias_ids)):
                    return peer
            for peer in self._peers.values():
                if peer.node_id.lower().startswith(low) or peer.device_id.lower().startswith(low):
                    return peer
            for peer in self._peers.values():
                if low in peer.name.lower() or low in peer.host:
                    return peer
        return None

    def merge_identity(self, keep: str, drop: str) -> dict[str, Any]:
        """人工确认后把两条记录并成一个节点（**只有人点了才会走到这里**）。

        保留 `keep` 的主身份；`drop` 的 node_id / device_id / 旧别名 / 所有链路地址
        全部降级成 keep 的 aliasIds / hosts，然后删掉 drop 这条重复记录。
        返回合并后的快照，给面板回显"并成了哪一台、还剩几条链路"。
        """
        with self._lock:
            kept = self._peers.get(keep) or self.resolve_locked(keep)
            old = self._peers.get(drop) or self.resolve_locked(drop)
            if kept is None or old is None or kept is old:
                return {"ok": False, "reason": "找不到要合并的设备"}
            self._add_alias(kept, old.node_id)
            if old.device_id:
                self._add_alias(kept, old.device_id)
            for alias in old.alias_ids:
                self._add_alias(kept, alias)
            for host in old.hosts:
                self._add_host(kept, host)
            self._add_host(kept, old.host)
            for key, value in list(self._peers.items()):
                if value is old:
                    self._peers.pop(key, None)
            self._remember(kept)
            return {"ok": True, "peer": kept.to_json()}

    def split_identity(self, keep: str, drop_ids: list[str]) -> dict[str, Any]:
        """★ v2.27（用户第 6 条）：把人工合并过的一台设备拆开。

        只撤销"别名归并"这一层；如果被拆掉的那条真的是独立设备，它的信标下一轮就会
        重新登记成一张独立卡片。路由一条都不删。
        """
        with self._lock:
            kept = self._peers.get(keep) or self.resolve_locked(keep)
            if kept is None:
                return {"ok": False, "reason": "找不到保留的设备"}
            wanted = {str(x or '').strip().lower() for x in drop_ids if str(x or '').strip()}
            kept.alias_ids = [a for a in kept.alias_ids if a.lower() not in wanted]
            self._remember(kept)
            return {"ok": True, "peer": kept.to_json()}

    def resolve_locked(self, selector: str) -> PeerEndpoints | None:
        """`merge_identity` 内部用：已经持锁，不能再调会二次加锁的 resolve。"""
        low = str(selector or "").strip().lower()
        for peer in self._peers.values():
            if (peer.node_id.lower() == low or peer.device_id.lower() == low
                    or peer.name.lower() == low or peer.host == selector
                    or any(alias.lower() == low for alias in peer.alias_ids)):
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
            # 主键是 deviceId 时，real 只是 node_id：必须走别名查找，否则会多出一台假设备。
            peer = self._peers.get(real) or self.resolve_locked(real)
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
            self._remember(peer)
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
            self._remember(peer)
            return peer

    def remove(self, node_id: str) -> bool:
        with self._lock:
            if self._peers.pop(node_id, None) is not None:
                return True
            peer = self.resolve_locked(node_id)
            if peer is None:
                return False
            for key, value in list(self._peers.items()):
                if value is peer:
                    self._peers.pop(key, None)
            return True

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
    device_id: str = "",
    instance_id: str = "",
) -> bytes:
    payload = {
        "wl": 1,
        "id": node_id,
        # ★ 身份层（2026-11-04）：device_id 是设备自生成、持久化的主键，同一设备
        #   的多条链路（WiFi / 蓝牙 / 其它）靠它合并成一个节点；instance_id 每次
        #   安装生成，用来识破"换机把旧配置搬过来、两台真机共用一个 deviceId"。
        #   老端只会忽略这两个键，不影响它继续工作。
        "device_id": device_id or node_id,
        "instance_id": instance_id,
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
        # ★ 「智连」能力位（2026-11-01）：1 = 请求发过来，由接收方自己的「智连」决定。
        #   与手机端 Discovery.buildBeacon 同名同义；老版本忽略它。
        "smart": 1,
        # ★ 扩展段能力位（2026-11-04）：1 = 我能安全解析音频包里多出来的 8 字节
        #   originId/hop。只有这里为 1 的对端才会收到带扩展段的包；老端永远收老包。
        "ext": 1,
        # 我实际绑定的发现端口（端口被占时可能不是 17830），对端按它发包。
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
        device_id: str = "",
        instance_id: str = "",
    ) -> None:
        self.registry = registry
        self.node_id = node_id
        # ★ 身份层（2026-11-04）：信标里的稳定主键与每次安装标识。
        self.device_id = device_id or node_id
        self.instance_id = instance_id
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
            # 写死 17830 的话换过端口的那一台就再也收不到控制消息。
            self.port,
            self.device_id,
            self.instance_id,
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
            # 端口用对端自己公布的发现端口（被占时可能不是 17830）；没报就用我们的默认。
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
