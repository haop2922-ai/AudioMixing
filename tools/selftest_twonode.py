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
import socket
import sys
import tempfile
import threading
import time
import urllib.request

# ★ 2026-10-15 自查发现的真 bug：这里原来写死的是**另一台机器/另一个目录**的绝对路径
#   （旧副本那一条），而那个目录早就不存在了 —— 于是这个自检在文档里怎么写都跑不起来
#   （ModuleNotFoundError: No module named 'audiomixing'）。自查时是加
#   PYTHONPATH=. 才勉强跑通的。
#   正确做法是从本文件反推仓库根：脚本在 tools/ 下，根就是它的上一层。
#   写死绝对路径这种东西一旦留下，换台机器/换个目录就静默失效，而且报错完全指不到真凶。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# This script starts two real nodes, which write config files. Point that at a
# throwaway directory: a verification run must never touch the user's
# %APPDATA%\AudioMixing (it once saved test ports 17902/48402 into the real config,
# which then made the control panel listen somewhere the user did not expect).
# The sandbox is removed again on exit so a test run leaves nothing behind.
_SANDBOX = tempfile.mkdtemp(prefix="audiomixing-selftest-")
os.environ["AUDIOMIXING_HOME"] = _SANDBOX
atexit.register(lambda: shutil.rmtree(_SANDBOX, ignore_errors=True))

from audiomixing.audio import base as audio_base  # noqa: E402
from audiomixing.audio.format import CHANNELS, SAMPLE_RATE, make_tone, signal_report  # noqa: E402
from audiomixing.config import Config  # noqa: E402
from audiomixing.engine import AudioMixingNode  # noqa: E402
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

    node_a = AudioMixingNode(cfg_a, audio_port=audio_port_a, discovery_port=disc_port_a)
    node_b = AudioMixingNode(cfg_b, audio_port=audio_port_b, discovery_port=disc_port_b)
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
        mute_resp = api(port_a, "/api/routes/update", {"id": rid, "mute": True})
        time.sleep(3.2)
        deadline = time.time() + 8
        stopped = False
        while time.time() < deadline:
            if not node_a.sender.sessions:
                stopped = True
                break
            time.sleep(0.3)
        check("静音后 A 停止发送", stopped, "senders=%d" % len(node_a.sender.sessions))
        if not stopped:
            print("     排错：静音接口回包=%s" % json.dumps(mute_resp, ensure_ascii=False)[:200])
            print("     A 路由表=%s" % [(r.get("id"), r.get("mute"), r.get("origin"))
                                     for r in node_a.routes])
            print("     A 仍想发送=%s" % [(x[0], x[1].name) for x in node_a.desired_senders()])
            print("     会话=%s" % list(node_a.sender.sessions))

        api(port_a, "/api/routes/delete", {"id": rid})
        # ★ 2026-11-04（审计 A10）：原来固定 sleep(3.2) 就断言。删路由要在 B 下一轮
        #   路由同步时才会生效，3.2 秒偶尔不够 —— 同一份成品两次跑会出现"19/19"和
        #   "18/19"两种结果。改成轮询到 8 秒，既保留上限，也不再靠固定睡眠赌时序。
        deadline = time.time() + 8.0
        while time.time() < deadline and len(node_b.sinks) != 0:
            time.sleep(0.3)
        check("删除路由后 B 关闭输出", len(node_b.sinks) == 0, "sinks=%d" % len(node_b.sinks))

        logs = api(port_a, "/api/logs")
        check("控制台日志可读", bool(logs.get("log")), "%d 行" % len(logs.get("log") or []))

        # 7. 接收准入（Q2）：广播→频道「只听」要接住，未点名的点对点必须拒收。
        #    这一步不靠手机：A 用真实 UDP 包扮演"广播源/点对点源"，
        #    B 用真实接收线程 + 真实准入判据处理。被拒的包只进 packets_denied，
        #    不许进 packets_received / 流表 —— 否则用户会看到"正在接收"却听不见。
        from audiomixing import proto as _proto

        def send_stats(broadcast: bool) -> bool:
            """A 以真实 T_STATS 声明"我在广播 / 我在点对点"，返回 B 是否已采纳。"""
            body = {"t": "stats", "node": "selftest-node-a", "name": "自检-A（采集端）",
                    "vol": 0.5, "send_mode": 1, "broadcast": bool(broadcast)}
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.sendto(_proto.Packet.control(_proto.T_STATS, body).encode(),
                         ("127.0.0.1", audio_port_b))
            finally:
                s.close()
            deadline = time.time() + 3
            while time.time() < deadline:
                node_b._handle_peer_requests()
                got = node_b._peer_send_mode_for("selftest-node-a")
                if got is not None and bool(got[1]) == bool(broadcast):
                    return True
                time.sleep(0.05)
            return False

        def send_audio(ssrc: int, count: int) -> None:
            """从 A 的地址发 count 个真实音频包到 B 的音频端口。"""
            payload = _proto.encode_pcm_s16([0.05, -0.05] * 480)   # 480 帧立体声
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for i in range(count):
                    pkt = _proto.Packet(type=_proto.T_AUDIO, payload=payload,
                                        codec=_proto.CODEC_PCM_S16, seq=i & 0xFFFF,
                                        ssrc=ssrc, samples=480, ts_ns=_proto.now_ns())
                    s.sendto(pkt.encode(), ("127.0.0.1", audio_port_b))
                    time.sleep(0.004)
            finally:
                s.close()
            time.sleep(0.2)

        def snapshot() -> tuple[int, int, int]:
            r = node_b.receiver
            return r.packets_denied, r.packets_received, len(r.streams)

        def wait_stream(ssrc: int, want: bool, timeout: float = 3.0) -> bool:
            """等接收线程把这条流建出来 / 确认没有被建出来（别拿单次快照赌时序）。"""
            deadline = time.time() + timeout
            while time.time() < deadline:
                if has_stream(ssrc) == want:
                    return True
                time.sleep(0.05)
            return False

        def detail() -> str:
            r = node_b.receiver
            return ("denied=%d received=%d rejected=%d echo=%d ssrcs=%s"
                    % (r.packets_denied, r.packets_received, r.packets_rejected,
                       getattr(r, "packets_echo_dropped", 0),
                       ",".join("%08x" % s for s in r.streams)))

        def has_stream(ssrc: int) -> bool:
            return ssrc in node_b.receiver.streams

        def streams_from_a() -> int:
            # ★「混音只挂一路」的正确判据：同一个发送方在流表里只能有一条。
            #   发送端换 ssrc（重启/换路由）时接收端会顶掉旧的那条（_evict_superseded），
            #   所以总数可能不变 —— 不能拿"总数 +1"当判据。
            return sum(1 for info in node_b.receiver.streams.values()
                       if (info.node_id or "") == "selftest-node-a")

        check("A 声明「广播」被 B 收到", send_stats(broadcast=True))
        wild = api(port_b, "/api/routes/add", {"route": {"from": ["*"], "to": ["self"],
                                                         "codec": "pcm_s16", "gain": 1.0}})
        check("B 挂上频道通配路由（= 勾「只听」）", bool(wild.get("ok")),
              json.dumps(wild.get("routes"), ensure_ascii=False)[:100])
        time.sleep(0.8)

        d0, r0, s0 = snapshot()
        send_audio(0xB0A5, 40)
        d1, r1, s1 = snapshot()
        check("广播→只听：音频被接住", r1 - r0 >= 30, "Δreceived=%d" % (r1 - r0))
        check("广播→只听：packets_denied 不增长", d1 == d0, "denied %d→%d" % (d0, d1))
        check("广播→只听：这条流进了流表", wait_stream(0xB0A5, True), detail())
        check("广播→只听：混音只挂一路（同一发送方只有一条流）", streams_from_a() == 1,
              "streams_from_A=%d" % streams_from_a())

        # ★ 2026-11-08（偶发失败定位）：B 加的通配路由会被 broadcast_routes 原样推给 A，
        #   A 看到 "from * → B" 就按频道发送规则**真的重新开一路采集发送**；真实流的
        #   ssrc 和下面注入的测试流属于同一个 owner，接收端按 _evict_superseded 只留
        #   一条 —— 谁后到谁留下。于是 7c 偶尔看到的是真实流 3a959cbf、注入的 0xB0A7
        #   被顶掉，报"点名收听：同一条点对点流被接住"失败（2/5 次）。
        #   这不是产品缺陷，是测试自己没隔离干净：用完通配路由立刻删掉（removed 会
        #   一并推给 A），并等 A 的真实发送会话收干净，再做后面的点对点注入。
        wild_id = str((wild.get("routes") or [{}])[-1].get("id") or "")
        if wild_id:
            api(port_b, "/api/routes/delete", {"id": wild_id})
        deadline = time.time() + 8.0
        while time.time() < deadline and node_a.sender.sessions:
            time.sleep(0.2)

        # ★ 2026-11-08（第三次复现，彻底隔离）：A 是**活着的实时节点** —— 它每 2 秒推一次
        #   自己的 broadcast 声明，会把刚才注入的「点对点」声明抢回去（RUN 1/3 实测：
        #   B 又按 broadcast 放行 → denied 0→0、0xB0A6 进了流表）；它的真实采集流还会和
        #   注入流抢同一个 owner 的 ssrc（RUN 2 实测 Δreceived=4 的尾包）。
        #   第 7 节只在验 **B 的接收准入**，A 只需要"身份"、不需要流量：停掉 A，
        #   它的身份仍由启动时的 manual peer 记录保活（不会过期）。
        node_a.stop()

        check("A 改声明「点对点」被 B 收到", send_stats(broadcast=False))
        check("B 判定 A 在点对点送声音（而不是频道广播）",
              node_b._peer_sends_point_to_point("selftest-node-a") is True)

        # ★ 2026-11-08：A 停发之后，网络上还有几十毫秒的尾包在飞；原来立刻取基准，
        #   尾包会被算进"未开收听也算已接收"的 Δreceived 里（本轮 RUN 2 实测 Δ=3）。
        #   先等 packets_received 静止再取基准，避免拿时序赌判据。
        deadline = time.time() + 5.0
        last_rx = node_b.receiver.packets_received
        stable_since = time.time()
        while time.time() < deadline:
            time.sleep(0.1)
            cur_rx = node_b.receiver.packets_received
            if cur_rx != last_rx:
                last_rx, stable_since = cur_rx, time.time()
            elif time.time() - stable_since >= 0.8:
                break

        d0, r0, s0 = snapshot()
        send_audio(0xB0A6, 40)
        d1, r1, s1 = snapshot()
        check("点对点未开「收听」：被拒收并计入 packets_denied", d1 - d0 >= 30,
              "denied %d→%d" % (d0, d1))
        check("点对点未开「收听」：不进流表", wait_stream(0xB0A6, False, 1.0), detail())
        check("点对点未开「收听」：不算作已接收", r1 - r0 <= 2, "Δreceived=%d" % (r1 - r0))

        named = api(port_b, "/api/routes/add", {"route": {"from": ["selftest-node-a"],
                                                          "to": ["self"], "codec": "pcm_s16",
                                                          "gain": 1.0}})
        check("B 为 A 点名开「收听」", bool(named.get("ok")),
              json.dumps(named.get("routes"), ensure_ascii=False)[:220])
        time.sleep(1.0)
        d0, r0, s0 = snapshot()
        send_audio(0xB0A7, 40)
        admitted = wait_stream(0xB0A7, True)
        d1, r1, s1 = snapshot()
        check("点名收听：同一条点对点流被接住", admitted, detail())
        check("点名收听：混合表里该发送方仍只有一条", streams_from_a() == 1,
              "streams_from_A=%d" % streams_from_a())
        check("点名收听：packets_denied 不增长", d1 == d0, "denied %d→%d" % (d0, d1))
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
