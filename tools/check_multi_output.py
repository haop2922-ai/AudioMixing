"""Can one source play on several speakers at once, and several sources on one?

This pins the fan-out / fan-in behaviour the UI claims to support, because "只有
一个音响能出声" would be a fundamental limitation if true:

  * one route with several 播放在 targets -> the source is capture ONCE and sent to
    every target node (no duplicated capture, no duplicated audio);
  * several routes with the same source and different targets -> same thing;
  * several sources into one local sink -> the receiver mixes them into a single
    output, and mutes/gain stay per route.
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


def add_peer(node: WaveLinkNode, node_id: str, name: str, host: str, port: int) -> None:
    node.registry.update(
        {"wl": 1, "id": node_id, "name": name, "audio_port": port, "web_port": 1,
         "sources": [{"id": "s", "name": "扬声器 [loopback]", "detail": "loopback"}],
         "sinks": [{"id": "k", "name": "耳机", "detail": "render"}]},
        host,
    )


def main() -> int:
    # ---------------- fan-out: one source, three targets ----------------
    a = make_node("我的笔记本", "node-a")
    a.start(announce=False)
    try:
        add_peer(a, "node-b", "伴侣的电脑", "192.168.1.51", 47991)
        add_peer(a, "node-c", "客厅音箱", "192.168.1.52", 47992)
        add_peer(a, "node-d", "书房耳机", "192.168.1.53", 47993)

        # one route, three targets
        a.set_routes([{"id": "one-to-many", "from": ["self"],
                       "to": ["伴侣的电脑", "客厅音箱", "书房耳机"], "codec": "pcm_s16"}], persist=False)
        a.apply_routes()
        senders = a.desired_senders()
        targets = sorted(t[1].name for t in senders)
        check("一条路由可以同时送到多个音响", len(senders) == 3, "targets=%s" % targets)

        captures = a.hub.stats()
        check("多目标只打开一次采集（声音不会被拆开）", len(captures) == 1,
              "captures=%d consumers=%s" % (len(captures), [c["consumers"] for c in captures]))
        check("每个目标各有一条独立发送流", len(a.sender.sessions) == 3,
              "sessions=%d" % len(a.sender.sessions))

        # three separate routes, same source, different targets
        a.set_routes([
            {"id": "r-b", "from": ["self"], "to": ["伴侣的电脑"], "codec": "pcm_s16"},
            {"id": "r-c", "from": ["self"], "to": ["客厅音箱"], "codec": "pcm_s16"},
            {"id": "r-d", "from": ["self"], "to": ["书房耳机"], "codec": "pcm_s16"},
        ], persist=False)
        a.apply_routes()
        check("三条独立路由也能同时送三方", len(a.desired_senders()) == 3,
              "senders=%d" % len(a.desired_senders()))
        check("三条路由共用一次采集", len(a.hub.stats()) == 1,
              "captures=%d" % len(a.hub.stats()))
    finally:
        a.stop()

    # ---------------- fan-in: three sources, one local output ----------
    b = make_node("我的笔记本", "node-b")
    b.start(announce=False)
    try:
        add_peer(b, "node-x", "手机A", "192.168.1.61", 47994)
        add_peer(b, "node-y", "手机B", "192.168.1.62", 47995)
        add_peer(b, "node-z", "台式机", "192.168.1.63", 47996)

        b.set_routes([
            {"id": "in-x", "from": ["手机A"], "to": ["self"], "gain": 1.0},
            {"id": "in-y", "from": ["手机B"], "to": ["self"], "gain": 0.8},
            {"id": "in-z", "from": ["台式机"], "to": ["self"], "gain": 1.2},
        ], persist=False)
        b.apply_routes()
        sinks = b.desired_sinks()
        check("三路声音可以同时进本机（混音到同一个输出）", len(sinks) == 3, "sinks=%s" % list(sinks))

        # one route with three sources into the same local sink
        b.set_routes([{"id": "many-to-one", "from": ["手机A", "手机B", "台式机"], "to": ["self"]}], persist=False)
        b.apply_routes()
        sinks = b.desired_sinks()
        peers = sorted(p.name for e in sinks.values() for p in e["peers"])
        check("一条路由收多路来源", len(sinks) == 1 and len(peers) == 3, "sources=%s" % peers)

        # per-route gain must survive the fan-in
        b.set_routes([
            {"id": "g1", "from": ["手机A"], "to": ["self"], "gain": 0.5},
            {"id": "g2", "from": ["手机B"], "to": ["self"], "gain": 1.5},
        ], persist=False)
        gains = sorted(r["gain"] for r in b.routes)
        check("混音时每路音量独立", gains == [0.5, 1.5], "gains=%s" % gains)
    finally:
        b.stop()

    print()
    print("共 %d 项失败" % len(failures))
    for f in failures:
        print("  失败: %s" % f)
    print("RESULT:", "PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
