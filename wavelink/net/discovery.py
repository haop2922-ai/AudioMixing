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
     "sinks":  [{"id":..,"name":"耳机 (WH-1000XM5)","detail":"render"}],
     "busy":false}
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
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    manual: bool = False
    local: bool = False
    # Quality of the address currently in `host`. Kept out of to_json(): it is an
    # internal tie-breaker, not something the control panel needs to render.
    host_rank: tuple[int, int] | None = None

    @property
    def age_s(self) -> float:
        return time.time() - self.last_seen

    @property
    def online(self) -> bool:
        return self.manual or self.age_s < NODE_TIMEOUT_S

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
            "age_s": round(self.age_s, 1),
            "online": self.online,
            "manual": self.manual,
            "local": self.local,
        }


class DiscoveryRegistry:
    """Thread-safe view of every WaveLink node heard on the LAN."""

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
            if local:
                peer.local = True
            if manual:
                peer.manual = True
            peer.last_seen = time.time()
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

    WaveLink deliberately has no relay, no hole punching and no cloud rendezvous:
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
    """What this machine can see on its local network — used by `wavelink net`
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
    ) -> None:
        self.registry = registry
        self.node_id = node_id
        self.name = name
        self.audio_port = audio_port
        self.web_port = web_port
        self.source_provider = source_provider
        self.sink_provider = sink_provider
        self.port = port
        self.group = group
        self.version = version
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        self._local_ips = set(local_ip_addresses())
        self.beacons_sent = 0
        self.beacons_received = 0
        self.last_error: str = ""
        self._manual_targets: list[str] = []

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
            threading.Thread(target=self._announce_loop, name="wavelink-beacon-tx", daemon=True),
            threading.Thread(target=self._listen_loop, name="wavelink-beacon-rx", daemon=True),
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
        return build_beacon(
            self.node_id,
            self.name,
            self.audio_port,
            self.web_port,
            self.source_provider(),
            self.sink_provider(),
            self.version,
        )

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
            sent = 0
            for target in targets:
                try:
                    sock.sendto(data, target)
                    sent += 1
                except OSError:
                    continue
            if sent:
                self.beacons_sent += 1
            self._stop.wait(BEACON_INTERVAL_S)

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
            self.beacons_received += 1
            local = addr[0] in self._local_ips
            self.registry.update(payload, addr[0], local=local)

    # -- mDNS compatibility -------------------------------------------------
    def dns_sd_records(self) -> dict[str, Any]:
        """The records a DNS-SD responder would publish for this node.

        Exposed for the (optional) mDNS bridge and for debugging with
        `dns-sd -B _wavelink._udp`; the beacon above is the primary mechanism.
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
            "peers": len(self.registry.all()),
            "error": self.last_error,
        }
