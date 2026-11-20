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
    """★ 2026-11-17：真机出声只在 --real-audio 时启用。

    用户反馈「调节音量有高频杂音 / 蓝牙卡顿」后自查发现：这个自检原来会在
    **用户真实的默认播放设备**（这次是蓝牙耳机）上播 14 秒 880Hz，且 B 节点
    也往真实默认设备播放。门禁必须隔离，不能碰用户设备。
    """
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
    broadcast_port_a, broadcast_port_b = 48403, 48404
    disc_port_a, disc_port_b = 48411, 48412
    web_a, web_b = 17901, 17902

    cfg_a = Config()
    cfg_a.node_id = "selftest-node-a"
    cfg_a.name = "自检-A（采集端）"
    cfg_a.audio_port = audio_port_a
    cfg_a.broadcast_port = broadcast_port_a
    cfg_a.discovery_port = disc_port_a
    cfg_a.web_port = web_a
    cfg_a.routes = []
    cfg_a.manual_peers = []

    cfg_b = Config()
    cfg_b.node_id = "selftest-node-b"
    cfg_b.name = "自检-B（播放端）"
    cfg_b.audio_port = audio_port_b
    cfg_b.broadcast_port = broadcast_port_b
    cfg_b.discovery_port = disc_port_b
    cfg_b.web_port = web_b
    cfg_b.routes = []
    cfg_b.manual_peers = []

    node_a = AudioMixingNode(cfg_a, audio_port=audio_port_a, broadcast_port=broadcast_port_a, discovery_port=disc_port_a)
    node_b = AudioMixingNode(cfg_b, audio_port=audio_port_b, broadcast_port=broadcast_port_b, discovery_port=disc_port_b)
    # ★ 2026-11-17：默认不碰真实声卡。假采集源 + 空播放端就能验证整条链路；
    #   要真实出声必须显式 --real-audio（默认门禁不带这个参数）。
    real_audio = "--real-audio" in sys.argv
    if not real_audio:
        node_a.default_source_spec = lambda: {"backend": "null", "device": "tone:880"}
        node_b.default_source_spec = lambda: {"backend": "null", "device": "tone:880"}
        node_a.default_sink_spec = lambda: {"backend": "null", "device": "null"}
        node_b.default_sink_spec = lambda: {"backend": "null", "device": "null"}
    print("音频设备: %s" % ("真实声卡（--real-audio）" if real_audio else "隔离（null，不碰真实设备）"),
          flush=True)
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
        check("广播端口独立绑定", node_a.receiver.broadcast_port == broadcast_port_a
              and node_b.receiver.broadcast_port == broadcast_port_b,
              "A=%d B=%d" % (node_a.receiver.broadcast_port, node_b.receiver.broadcast_port))
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
        tone = tone_player(14.0) if real_audio else None
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
        if tone is not None:
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

        def send_audio(ssrc: int, count: int, port: int = audio_port_b) -> None:
            """从 A 的地址发 count 个真实音频包到 B 的指定端口（点对点 / 广播）。"""
            payload = _proto.encode_pcm_s16([0.05, -0.05] * 480)   # 480 帧立体声
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                for i in range(count):
                    pkt = _proto.Packet(type=_proto.T_AUDIO, payload=payload,
                                        codec=_proto.CODEC_PCM_S16, seq=i & 0xFFFF,
                                        ssrc=ssrc, samples=480, ts_ns=_proto.now_ns())
                    s.sendto(pkt.encode(), ("127.0.0.1", port))
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
            # ★「混音只挂一路」的正确判据：同一个发送方在**同一个端口**的流表里只能有一条。
            #   ★ 2.42.2：广播口那条（info.channel=True）单独算，不混进点对点口径。
            return sum(1 for info in node_b.receiver.streams.values()
                       if (info.node_id or "") == "selftest-node-a"
                       and not bool(getattr(info, "channel", False)))

        # ★ 2.42.1（自检隔离）：这台机器上可能同时跑着真实节点（桌面上的
        #   AudioMixing exe），它们也从 127.0.0.1 发信标。接收端按 IP 反查身份时
        #   可能认到那台，导致"同一发送方只有一条流"统计成 0（20e66011 那条真实
        #   节点就抢过这个映射）。本节只验"一条流混一路"，来源固定成 A。
        node_b.receiver.resolve_source = lambda _host: "selftest-node-a"
        check("A 声明「点对点」被 B 收到", send_stats(broadcast=False))
        # ★ 2.42.2（第 2 条）：广播走独立端口 + 独立准入（只看 config.listen）。
        node_b.config.listen = True
        time.sleep(1.2)
        b0 = node_b.receiver.broadcast_packets_received
        send_audio(0xB0A5, 40, broadcast_port_b)
        b1 = node_b.receiver.broadcast_packets_received
        check("广播口：勾「听广播」后音频被接住", b1 - b0 >= 30, "Δbroadcast_rx=%d" % (b1 - b0))
        check("广播口：这条流进了流表且标记为 channel",
              wait_stream(0xB0A5, True)
              and bool(getattr(node_b.receiver.streams.get(0xB0A5), "channel", False)),
              detail())
        node_b.config.listen = False
        time.sleep(1.2)

        # ★ 2026-11-08（偶发失败定位）：等 A 的真实发送会话收干净，再做后面的点对点注入，
        #   避免真实流和注入流抢同一个 owner 的 ssrc。
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

        # ★ v2.42 门禁稳定性（两条，都是测试隔离，不改产品判据）：
        #   ① 上面"等收包静止"最多 5 秒，而发送端的模式声明只有 PEER_TARGETED_TTL_S
        #      （6 秒）新鲜度；等完声明可能刚好过期，B 会把点对点当成"认不出来源"放行。
        #      真实发送端每 ~2 秒会再报一次，这里补发一次等价于真实节奏。
        #   ② 这台机器上可能同时跑着真实节点（比如桌面上的 AudioMixing-v2.41.exe），
        #      它们也从 127.0.0.1 发信标；接收端按 IP 反查身份时可能认到那台，
        #      于是"点对点拒收"变成"来源认不出 → 放行"。这一节只验 B 的准入规则，
        #      所以临时把来源解析固定成 A，避免被同机真实节点干扰。
        original_resolve = node_b.receiver.resolve_source
        node_b.receiver.resolve_source = lambda _host: "selftest-node-a"
        try:
            # ★ v2.42：第 2 步那条路由的删除是异步推送的。这台机器上同时跑着
            #   别的真实节点时，偶尔 8 秒内没清干净（B.sinks 已关，但 B 的
            #   desired_sinks 里还留着"来自 A"的具名路由）—— 它会让下面的
            #   "未开收听必须拒收"被这条残留路由放行。这一节只验准入规则，
            #   所以先显式清掉 B 表里来自 A 的残留，并等准入真的回到"没有具名路由"。
            node_b.set_routes([r for r in node_b.routes
                               if "selftest-node-a" not in (r.get("from") or [])])
            deadline = time.time() + 5.0
            while time.time() < deadline and node_b._accepted_routes_for("selftest-node-a"):
                time.sleep(0.2)

            send_stats(broadcast=False)
            time.sleep(0.3)

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
            node_b.receiver.resolve_source = original_resolve

        # 8. ★ 2.42.2（第 2 条）：广播 / 点对点四组合端口隔离（双节点等价实测）。
        #    手机真机不在线，这里用同一套协议、两个真实 UDP 端口做等价验证：
        #    B 的 47832 只由 config.listen 决定，47831 只由具名路由决定。
        print("-" * 70)
        print("四组合：广播口 %d / 点对点口 %d" % (broadcast_port_b, audio_port_b), flush=True)
        node_b.receiver.resolve_source = lambda _host: "selftest-node-a"
        try:
            def b_rx() -> int:
                return node_b.receiver.broadcast_packets_received

            def b_den() -> int:
                return node_b.receiver.broadcast_packets_denied

            def p_rx() -> int:
                return node_b.receiver.packets_received

            def p_den() -> int:
                return node_b.receiver.packets_denied

            def clear_streams() -> None:
                with node_b.receiver._lock:
                    node_b.receiver.streams.clear()
                    node_b.receiver.buffers.clear()

            def has_named() -> bool:
                return bool(node_b._accepted_routes_for("selftest-node-a"))

            def set_named(on: bool) -> None:
                if on:
                    api(port_b, "/api/routes/add", {"route": {
                        "from": ["selftest-node-a"], "to": ["self"],
                        "codec": "pcm_s16", "gain": 1.0}})
                else:
                    for r in list(node_b.routes):
                        if "selftest-node-a" in (r.get("from") or []):
                            api(port_b, "/api/routes/delete", {"id": r.get("id")})
                deadline = time.time() + 5.0
                while time.time() < deadline and has_named() != on:
                    time.sleep(0.1)

            def set_listen(on: bool) -> None:
                node_b.config.listen = bool(on)
                time.sleep(0.9)

            def combo(name: str, listen: bool, named: bool, bssrc: int, pssrc: int) -> None:
                clear_streams()
                set_named(named)
                set_listen(listen)
                send_stats(broadcast=False)
                br0, bd0, pr0, pd0 = b_rx(), b_den(), p_rx(), p_den()
                send_audio(bssrc, 30, broadcast_port_b)
                send_audio(pssrc, 30, audio_port_b)
                brd, bdd = b_rx() - br0, b_den() - bd0
                prd, pdd = p_rx() - pr0, p_den() - pd0
                with node_b.receiver._lock:
                    ch = [s for s, i in node_b.receiver.streams.items()
                          if bool(getattr(i, "channel", False))]
                    pt = [s for s, i in node_b.receiver.streams.items()
                          if not bool(getattr(i, "channel", False))]
                evidence = ("broadcast: rx+%d denied+%d ch_streams=%s | "
                            "point: rx+%d denied+%d p2p_streams=%s"
                            % (brd, bdd, ",".join("%08x" % s for s in ch),
                               prd, pdd, ",".join("%08x" % s for s in pt)))
                print("  [%s] %s" % (name, evidence), flush=True)
                check("%s：广播口只收到该收的" % name,
                      (brd >= 25 and bssrc in ch) if listen else (brd == 0 and bssrc not in ch),
                      evidence)
                check("%s：点对点口只收到该收的" % name,
                      (prd >= 25 and pssrc in pt) if named else (prd == 0 and pssrc not in pt),
                      evidence)
                check("%s：拒收只落在自己那一路" % name,
                      (bdd >= 25 if not listen else bdd == 0)
                      and (pdd >= 25 if not named else pdd == 0), evidence)

            combo("只开广播收听", True, False, 0xB201, 0xB202)
            combo("只开点对点收听", False, True, 0xB203, 0xB204)
            combo("两者都开", True, True, 0xB205, 0xB206)
            combo("两者都关", False, False, 0xB207, 0xB208)
            set_listen(False)
            set_named(False)
        finally:
            node_b.receiver.resolve_source = original_resolve
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
