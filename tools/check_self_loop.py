"""What happens when the local machine is itself in the route's source list?

This is the "would I hear myself twice / get feedback?" question. The answer must
be a code-level guarantee, not a hope, so it is pinned here:

  * a route with 声音来自=本机 and 播放在=本机 must NOT create a local capture →
    local playback loop (that is an echo/feedback machine);
  * a route with 声音来自=本机 and 播放在=别的设备 must NOT capture anything either:
    the local sound is already playing here, sending it out is not what the user
    means by "listening to another device";
  * a route with 声音来自=别的设备 and 播放在=本机 must capture nothing locally and
    must open exactly one local output -- this is the normal case.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

# Verification scripts must never read or write the user's real config: they start
# real nodes, and a stray run once saved test ports into %APPDATA%\WaveLink.
# The sandbox is removed again on exit so a test run leaves nothing behind.
if "WAVELINK_HOME" not in _os.environ:
    _sandbox = _tempfile.mkdtemp(prefix="wavelink-check-")
    _os.environ["WAVELINK_HOME"] = _sandbox
    _atexit.register(lambda: _shutil.rmtree(_sandbox, ignore_errors=True))

from audiomixing.config import Config
from audiomixing.engine import WaveLinkNode

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def make_node(name: str, node_id: str) -> WaveLinkNode:
    cfg = Config()
    cfg.node_id = node_id
    cfg.name = name
    cfg.routes = []
    cfg.manual_peers = []
    return WaveLinkNode(cfg, audio_port=0, discovery_port=0, simulate=True)


def add_peer(node: WaveLinkNode, node_id: str, name: str, port: int = 47990) -> None:
    node.registry.update(
        {"wl": 1, "id": node_id, "name": name, "audio_port": port, "web_port": 1,
         "sources": [{"id": "s", "name": "扬声器 [loopback]", "detail": "loopback"}],
         "sinks": [{"id": "k", "name": "耳机", "detail": "render"}]},
        "192.168.1.50",
    )


def main() -> int:
    node = make_node("self-node", "node-self")
    node.start(announce=False)
    try:
        add_peer(node, "peer-b", "伴侣的电脑")

        # --- case 1: 本机 -> 本机 (the configuration the UI preselects today) ----
        node.set_routes([{"id": "self2self", "from": ["self"], "to": ["self"], "codec": "pcm_s16"}], persist=False)
        senders = node.desired_senders()
        sinks = node.desired_sinks()
        check("本机→本机 不建立任何发送", len(senders) == 0, "senders=%d" % len(senders))
        check("本机→本机 不建立本地播放（否则自己录自己=回声/啸叫）", len(sinks) == 0, "sinks=%d" % len(sinks))

        # --- case 2: 本机 -> 别的设备 ------------------------------------------
        node.set_routes([{"id": "self2peer", "from": ["self"], "to": ["伴侣的电脑"], "codec": "pcm_s16"}], persist=False)
        senders = node.desired_senders()
        sinks = node.desired_sinks()
        check("本机→对端 会发送", len(senders) == 1, "target=%s" % (senders[0][1].name if senders else "-"))
        check("本机→对端 不在本机播放", len(sinks) == 0, "sinks=%d" % len(sinks))

        # --- case 3: 别的设备 -> 本机 (the normal office/home case) ------------
        node.set_routes([{"id": "peer2self", "from": ["伴侣的电脑"], "to": ["self"], "codec": "pcm_s16"}], persist=False)
        senders = node.desired_senders()
        sinks = node.desired_sinks()
        check("对端→本机 不发送", len(senders) == 0, "senders=%d" % len(senders))
        check("对端→本机 建立本地播放", len(sinks) == 1, "sinks=%s" % list(sinks))

        # --- case 4: both ends in the source list (multi-select) --------------
        node.set_routes([{"id": "both", "from": ["self", "伴侣的电脑"], "to": ["self"], "codec": "pcm_s16"}], persist=False)
        sinks = node.desired_sinks()
        peers = list(sinks.values())[0]["peers"] if sinks else []
        names = [p.name for p in peers]
        check("来源里含本机时，本机不被当作本地播放的来源", "self-node" not in names,
              "peers=%s" % names)
        check("来源里的对端仍然会播放", "伴侣的电脑" in names, "peers=%s" % names)

        # --- case 5: wildcard, must not loop back into itself -----------------
        node.set_routes([{"id": "wild", "from": ["*"], "to": ["*"], "codec": "pcm_s16"}], persist=False)
        sinks = node.desired_sinks()
        peers = [p.name for entry in sinks.values() for p in entry["peers"]]
        check("通配路由不会把本机声音播回本机", "self-node" not in peers, "peers=%s" % peers)
    finally:
        node.stop()

    print()
    print("共 %d 项失败" % len(failures))
    for f in failures:
        print("  失败: %s" % f)
    print("RESULT:", "PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
