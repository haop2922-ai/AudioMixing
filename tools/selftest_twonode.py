"""End-to-end verification on one machine: two real nodes, real control panel,
real WASAPI capture and playback.

    python tools/selftest_twonode.py

What it proves, in order:
  1. both nodes start and hear each other's discovery beacons;
  2. the control panel of node A is reachable and can add a route;
  3. that route is pushed to node B over UDP and applied there;
  4. node A captures system sound (WASAPI loopback) and streams it to node B;
  5. node B mixes it into its own output device and reports received audio.

A tone is played on the default output during the test so that the loopback capture
has something to capture (and so the test is audible proof, not just numbers).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, r"E:\AI\deep blue\wavelink")

# This script starts two real nodes, which write config files. Point that at a
# throwaway directory: a verification run must never touch the user's
# %APPDATA%\WaveLink (it once saved test ports 17902/48402 into the real config,
# which then made the control panel listen somewhere the user did not expect).
# The sandbox is removed again on exit so a test run leaves nothing behind.
_SANDBOX = tempfile.mkdtemp(prefix="wavelink-selftest-")
os.environ["WAVELINK_HOME"] = _SANDBOX
atexit.register(lambda: shutil.rmtree(_SANDBOX, ignore_errors=True))

from audiomixing.audio import base as audio_base  # noqa: E402
from audiomixing.audio.format import CHANNELS, SAMPLE_RATE, make_tone, signal_report  # noqa: E402
from audiomixing.config import Config  # noqa: E402
from audiomixing.engine import WaveLinkNode  # noqa: E402
from audiomixing.webui import WebUI  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print("[%s] %s%s" % (PASS if ok else FAIL, name, (" — " + detail) if detail else ""), flush=True)
    return ok


def api(port: int, path: str, body: dict | None = None):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if body is None:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tone_player(seconds: float, device: str = "@default") -> threading.Thread:
    def run() -> None:
        sink = audio_base.open_sink({"backend": "wasapi", "device": device})
        sink.start()
        block = make_tone(880.0, 480, 0.3, CHANNELS)
        deadline = time.monotonic()
        end = deadline + seconds
        while time.monotonic() < end:
            offset = 0
            while offset < 480:
                got = sink.write(block[offset * CHANNELS :])
                if got == 0:
                    time.sleep(0.002)
                    continue
                offset += got
            deadline += 480 / float(SAMPLE_RATE)
            sleep = deadline - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
        time.sleep(0.2)
        sink.stop()

    thread = threading.Thread(target=run, name="selftest-tone", daemon=True)
    thread.start()
    return thread


def main() -> int:
    audio_port_a, audio_port_b = 48401, 48402
    disc_port_a, disc_port_b = 48411, 48412
    web_a, web_b = 17901, 17902

    cfg_a = Config()
    cfg_a.node_id = "selftest-node-a"
    cfg_a.name = "自检-A（采集端）"
    cfg_a.audio_port = audio_port_a
    cfg_a.discovery_port = disc_port_a
    cfg_a.web_port = web_a
    cfg_a.routes = []
    cfg_a.manual_peers = []

    cfg_b = Config()
    cfg_b.node_id = "selftest-node-b"
    cfg_b.name = "自检-B（播放端）"
    cfg_b.audio_port = audio_port_b
    cfg_b.discovery_port = disc_port_b
    cfg_b.web_port = web_b
    cfg_b.routes = []
    cfg_b.manual_peers = []

    node_a = WaveLinkNode(cfg_a, audio_port=audio_port_a, discovery_port=disc_port_a)
    node_b = WaveLinkNode(cfg_b, audio_port=audio_port_b, discovery_port=disc_port_b)
    ui_a = WebUI(node_a, host="127.0.0.1", port=web_a)
    ui_b = WebUI(node_b, host="127.0.0.1", port=web_b)

    node_a.start()
    node_b.start()
    port_a = ui_a.start()
    port_b = ui_b.start()
    # Beacons are multicast/broadcast; make sure both nodes also unicast to each other
    # so the test works on machines that filter multicast.
    node_a.beacon.add_manual_target("127.0.0.1", disc_port_b)
    node_b.beacon.add_manual_target("127.0.0.1", disc_port_a)
    node_a.registry.add_manual("127.0.0.1", audio_port_b, "自检-B")
    node_b.registry.add_manual("127.0.0.1", audio_port_a, "自检-A")

    try:
        print("=" * 70)
        check("两个节点启动", node_a.receiver.port == audio_port_a and node_b.receiver.port == audio_port_b)
        check("控制台 A 可访问", bool(api(port_a, "/healthz")["ok"]), "http://127.0.0.1:%d" % port_a)
        check("控制台 B 可访问", bool(api(port_b, "/healthz")["ok"]), "http://127.0.0.1:%d" % port_b)

        # 1. discovery
        deadline = time.time() + 12
        heard_a = heard_b = 0
        while time.time() < deadline:
            heard_a = len([p for p in node_a.registry.all() if p.node_id == "selftest-node-b"])
            heard_b = len([p for p in node_b.registry.all() if p.node_id == "selftest-node-a"])
            if heard_a and heard_b:
                break
            time.sleep(0.3)
        check("A 通过发现信标听到 B", bool(heard_a), "peers=%d" % len(node_a.registry.all()))
        check("B 通过发现信标听到 A", bool(heard_b), "peers=%d" % len(node_b.registry.all()))
        peers_a = api(port_a, "/api/peers")
        names = [p["name"] for p in peers_a["peers"]]
        check("控制台列出对端设备", any("自检-B" in n for n in names), "peers=%s" % names)

        # 2. route through the control panel (this is what a user clicks)
        devices_a = api(port_a, "/api/devices")
        sources = [d["name"] for d in devices_a["sources"]]
        check("A 能列出可采集设备（含系统声音）", any("loopback" in (d.get("detail") or "") for d in devices_a["sources"]), "sources=%d" % len(devices_a["sources"]))
        devices_b = api(port_b, "/api/devices")
        check("B 能列出可播放设备", bool(devices_b["sinks"]), "sinks=%d" % len(devices_b["sinks"]))

        resp = api(port_a, "/api/routes/add", {"route": {"from": ["selftest-node-a"], "to": ["selftest-node-b"], "codec": "pcm_s16", "gain": 1.0}})
        check("通过控制台添加路由", bool(resp.get("ok")), json.dumps(resp.get("routes"), ensure_ascii=False)[:120])

        # 3. route propagation over UDP
        deadline = time.time() + 10
        applied = False
        while time.time() < deadline:
            if any(r.get("id") == resp["routes"][0]["id"] for r in node_b.routes):
                applied = True
                break
            time.sleep(0.3)
        check("路由被推送到 B 并生效", applied, "B.routes=%s" % [r["id"] for r in node_b.routes])

        # 4. audio actually flows
        # A's capture start-up is occasionally slow (WASAPI initialisation right after
        # a route appears can take a second or two), so keep the tone alive across the
        # whole window instead of playing it once and hoping the timing lines up.
        tone = tone_player(14.0)
        deadline = time.time() + 12
        packets = 0
        frames = 0
        while time.time() < deadline:
            stats = node_b.receiver.stats()
            packets = stats["packets_received"]
            streams = stats["streams"]
            frames = sum(s.get("played", 0) for s in streams)
            if packets > 200 and frames > 48000:
                break
            time.sleep(0.3)
        tone.join(timeout=20)
        check("B 收到 A 的音频包", packets > 200, "packets=%d" % packets)
        check("B 实际播放了 A 的音频", frames > 48000, "played_frames=%d (%.1f s)" % (frames, frames / float(SAMPLE_RATE)))

        streams = node_b.receiver.stats()["streams"]
        if streams:
            s = streams[0]
            print("     流详情: node=%s codec=%s 包=%d 丢=%d 缓冲=%.0fms 目标=%.0fms 下溢=%d"
                  % (s.get("node_name") or s.get("node_id") or "?", s.get("codec"), s.get("packets", 0), s.get("lost", 0),
                     s.get("buffered_ms", 0), s.get("target_ms", 0), s.get("underruns", 0)))
        gains = [x for x in node_b.sender.stats()]
        check("B 自己没有多余的发送流", len(gains) == 0, "senders=%d" % len(gains))
        check("A 正在发送", len(node_a.sender.stats()) >= 1, "senders=%s" % [x["host"] for x in node_a.sender.stats()])
        check("A 的采集扇出计数正确", all(c["consumers"] >= 1 for c in node_a.hub.stats()), str([(c["name"], c["consumers"]) for c in node_a.hub.stats()]))

        # 5. self-listen: B also plays its own... no; instead check A->B->A is not doubled
        status_b = api(port_b, "/api/status")
        check("B 状态里显示接收流", status_b["totals"]["active_streams"] >= 1, str(status_b["totals"]))

        # 6. mute through the panel
        rid = resp["routes"][0]["id"]
        api(port_a, "/api/routes/update", {"id": rid, "mute": True})
        time.sleep(3.2)
        deadline = time.time() + 8
        stopped = False
        while time.time() < deadline:
            if not node_a.sender.sessions:
                stopped = True
                break
            time.sleep(0.3)
        check("静音后 A 停止发送", stopped, "senders=%d" % len(node_a.sender.sessions))

        api(port_a, "/api/routes/delete", {"id": rid})
        time.sleep(3.2)
        check("删除路由后 B 关闭输出", len(node_b.sinks) == 0, "sinks=%d" % len(node_b.sinks))

        logs = api(port_a, "/api/logs")
        check("控制台日志可读", bool(logs.get("log")), "%d 行" % len(logs.get("log") or []))
    finally:
        ui_a.stop()
        ui_b.stop()
        node_a.stop()
        node_b.stop()

    print("=" * 70)
    failed = [r for r in results if r[1] == FAIL]
    print("共 %d 项检查，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for name, _state, detail in failed:
        print("  失败: %s %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
