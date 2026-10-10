"""WaveLink command line interface.

    audiomixing devices                 list capture/playback devices
    audiomixing serve                   run the node + web control panel
    audiomixing status                  print the state of a running node
    audiomixing route add|list|rm|set   manage the route table (pushes to the LAN)
    audiomixing send / play             direct one-shot streaming without the daemon
    audiomixing peer add|list           manual peers for AP-isolated networks
    wavelink doctor                  end-to-end self test on this machine
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from typing import Any

from . import __version__, proto
from .audio import base as audio_base
from .config import Config, config_path
from .engine import WaveLinkNode
from .net import discovery as disc
from .net.rtpaudio import JitterBuffer, Receiver, SenderSession


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _short(text: str, width: int = 46) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _existing_instance(port: int) -> dict[str, Any] | None:
    """Talk to an already-running WaveLink control panel on `port`, if any.

    Double-clicking the exe twice is the single most confusing thing a user can do
    here: the second process cannot bind the ports, looks alive in Task Manager, and
    serves nothing. So we detect the first one and act on it instead.
    """
    if not port:
        return None
    import json as _json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=1.5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) and "node" in data else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_in_use(port: int, kind: str = "tcp") -> bool:
    """True when something is already listening on `port` on this machine."""
    if not port:
        return False
    import socket as _socket

    if kind == "udp":
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True
        finally:
            s.close()
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _isolate_config() -> str | None:
    """Point config storage at a throwaway directory for this process.

    Diagnostic commands (`doctor`, `netcheck`) must never read or write the user's
    real config: doing so once saved test ports (57168/58842...) and a test route into
    the user's %APPDATA%, which then made the control panel listen on a port nobody
    expected. Returns the previous value so the caller can restore it.
    """
    import tempfile

    previous = os.environ.get("WAVELINK_HOME")
    os.environ["WAVELINK_HOME"] = tempfile.mkdtemp(prefix="wavelink-diag-")
    return previous


def _restore_config(previous: str | None) -> None:
    import shutil

    current = os.environ.get("WAVELINK_HOME")
    if current and "wavelink-diag-" in current:
        shutil.rmtree(current, ignore_errors=True)
    if previous is None:
        os.environ.pop("WAVELINK_HOME", None)
    else:
        os.environ["WAVELINK_HOME"] = previous


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

def cmd_net(args: argparse.Namespace) -> int:
    """Show this machine's LAN interfaces and what WaveLink can reach."""
    report = disc.lan_report()
    cfg = Config.load()
    if args.json:
        _print(report)
        return 0
    local = [i for i in report["interfaces"] if i["kind"] == "private"]
    public = [i for i in report["interfaces"] if i["kind"] != "private"]
    print("WaveLink 只在局域网内工作（没有服务器、没有穿透、没有中继）")
    print("=" * 62)
    if not local:
        print("没有找到局域网接口：请确认已连接 Wi-Fi / 网线。")
        return 1
    print("  %-16s %-18s %-16s %s" % ("本机局域网地址", "所在网段", "广播地址", "主机名"))
    for iface in local:
        print("  %-16s %-18s %-16s %s" % (iface["address"], iface["subnet"], iface["broadcast"], iface["hostname"]))
    print()
    for iface in local:
        print("  同一局域网内的其它设备可访问控制台: http://%s:%d" % (iface["address"], cfg.web_port))
    if public:
        print()
        print("  注意：以下地址看起来不在局域网内（可能是 VPN / 虚拟网卡 / 公网），")
        print("        WaveLink 不会向它们投射，也不会被它们投射：")
        for iface in public:
            print("       %s (%s)" % (iface["address"], iface["subnet"] or "未知网段"))
    print()
    print("  判定规则：对端地址必须是私有/链路本地地址（如 192.168.x.x、10.x.x.x），")
    print("  跨网段或走公网的地址一律拒绝——这是设计如此，不是配置问题。")
    return 0


def cmd_netcheck(args: argparse.Namespace) -> int:
    """Delegate to the standalone network diagnostic module (read-only w.r.t. config)."""
    from .netcheck import main as netcheck_main

    argv: list[str] = []
    if args.web_port:
        argv += ["--web-port", str(args.web_port)]
    return netcheck_main(argv)


def cmd_devices(args: argparse.Namespace) -> int:
    devices = audio_base.list_all_devices()
    if args.json:
        _print([d.to_json() for d in devices])
        return 0
    if not devices:
        print("没有找到任何音频设备。")
        return 1
    groups = [
        (audio_base.KIND_SOURCE, "可采集的声音（source）"),
        (audio_base.KIND_SINK, "可播放的目标（sink）"),
    ]
    for kind, title in groups:
        subset = [d for d in devices if d.kind == kind]
        if not subset:
            continue
        print("\n%s" % title)
        print("  %-8s %-9s %-5s %8s  %s" % ("backend", "detail", "ch", "rate", "name"))
        for d in subset:
            mark = "*" if d.is_default else " "
            print(
                "  %-8s %-9s %-5d %7dHz %s%s"
                % (d.backend, d.kind_detail or "-", d.channels, d.sample_rate, mark, _short(d.name))
            )
    print("\n提示：name 前缀 backend: 可精确指定，例如 --sink 'wasapi:耳机'。")
    return 0


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.name:
        cfg.name = args.name
    if args.codec:
        cfg.codec = args.codec
    if args.source_device:
        cfg.source_device = args.source_device
    if args.sink_device:
        cfg.sink_device = args.sink_device
    if args.web_port:
        cfg.web_port = args.web_port
    cfg.save()

    node = WaveLinkNode(cfg, audio_port=args.audio_port or cfg.audio_port, discovery_port=args.discovery_port or cfg.discovery_port,
                        simulate=args.simulate)
    from .webui import WebUI

    # Detect a port collision *before* starting: the previous behaviour was to walk
    # the web port up by one (findable, but the URL changed silently) and to let the
    # audio socket fail, leaving a live-looking process that served nothing.
    requested_web = cfg.web_port
    requested_audio = cfg.audio_port

    running = _existing_instance(requested_web)
    if running is not None:
        node_info = running.get("node", {})
        print("WaveLink 已经在这台电脑上运行了 —— 不需要再启动一个。")
        print("  节点名称 : %s" % node_info.get("name"))
        print("  已运行   : %.0f 秒" % float(node_info.get("uptime_s") or 0))
        print("  发送/播放: %s / %s" % (running.get("totals", {}).get("active_senders", 0),
                                        running.get("totals", {}).get("active_sinks", 0)))
        print()
        print("  请直接用这个网址（本机或手机/别的电脑都可以）：")
        for ip in [i["address"] for i in disc.lan_report()["interfaces"] if i["kind"] == "private"]:
            print("      http://%s:%d" % (ip, requested_web))
        print("      http://127.0.0.1:%d  （仅本机）" % requested_web)
        print()
        print("  这个新窗口会退出。要重启服务，请先关闭原来那个黑窗口。")
        if getattr(args, "open_browser", False):
            urls = ["http://%s:%d" % (i["address"], requested_web)
                    for i in disc.lan_report()["interfaces"] if i["kind"] == "private"]
            try:
                import webbrowser

                webbrowser.open(urls[0] if urls else "http://127.0.0.1:%d" % requested_web)
            except Exception:
                pass
        return 0

    if _port_in_use(requested_web, "tcp"):
        print("⚠ 端口 %d 被其它程序占用（不是 WaveLink）。改用其它端口启动。" % requested_web)
        print()
    if _port_in_use(requested_audio, "udp"):
        print("⚠ 音频端口 udp/%d 被其它程序占用 —— 这台机器将无法接收音频。" % requested_audio)
        print("  请关闭占用它的程序后重启 WaveLink。")
        print()

    ui = WebUI(node, host=args.host, port=cfg.web_port)
    node.start()
    port = ui.start()
    node.config.web_port = port
    # Persist the port actually in use so `status` and the next launch agree.
    try:
        node.config.save()
    except OSError:
        pass
    if port != requested_web:
        print("⚠ 原定端口 %d 被占用，控制台实际运行在 %d。" % (requested_web, port))
    # Only advertise addresses that are actually on a LAN. A VPN adapter or a public
    # address would look like a valid choice to the user and then fail on the other
    # machine -- exactly the kind of dead end this printout exists to prevent.
    lan_report = disc.lan_report()
    lan_ips = [i["address"] for i in lan_report["interfaces"] if i["kind"] == "private"]
    skipped = [i["address"] for i in lan_report["interfaces"] if i["kind"] != "private"]
    local_url = "http://127.0.0.1:%d" % port
    lan_urls = ["http://%s:%d" % (ip, port) for ip in lan_ips]
    print("=" * 70)
    print(" WaveLink %s 已启动" % __version__)
    print("=" * 70)
    if lan_urls:
        print("  给『别的电脑/手机』用这个网址（复制到它的浏览器里打开）:")
        for u in lan_urls:
            print("      %s" % u)
        print()
    else:
        print("  ⚠ 没有检测到局域网地址：请先连上 Wi-Fi 或网线，否则别的设备连不进来。")
        print()
    print("  这台电脑自己用这个网址:")
    print("      %s" % local_url)
    print()
    if skipped:
        print("  (已忽略这些非局域网地址: %s)" % "、".join(skipped))
        print()
    print("  节点名称   : %s (%s)" % (node.name, disc.short_id(node.node_id)))
    print("  音频端口   : udp/%d    发现端口: udp/%d" % (node.config.audio_port, node.config.discovery_port))
    print("  配置文件   : %s" % config_path())
    if args.simulate:
        print("  [模拟模式] 不打开真实声卡，使用内置测试音。")
    print()
    print("  下一步: 在页面上「② 路由」里选择'声音来自'和'播放在'，点添加路由。")
    print("  关闭本窗口即停止服务（或按 Ctrl+C）。")
    print()
    _flush()

    if getattr(args, "open_browser", False):
        # Open the LAN address, not the loopback one: that is the URL the user has to
        # type on the *other* machines, and seeing it in the address bar removes the
        # "is this only local?" confusion. Fall back to loopback if none was found.
        url_to_open = lan_urls[0] if lan_urls else local_url
        try:
            import webbrowser

            webbrowser.open(url_to_open)
        except Exception:
            pass

    # A double-clicked .exe has no console to print into on some systems, so also
    # write a short breadcrumb next to the config for troubleshooting.
    try:
        log_path = config_path().with_name("last-run.txt")
        log_path.write_text(
            "WaveLink %s\n启动时间: %s\n给别的设备用: %s\n本机用: %s\n"
            % (
                __version__,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                ", ".join(lan_urls) or "无（没有检测到局域网地址）",
                local_url,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        ui.stop()
        node.stop()
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    port = args.web_port or cfg.web_port
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("无法连接本机控制台 (端口 %d): %s" % (port, exc))
        print("先运行: audiomixing serve")
        return 1
    if args.json:
        _print(data)
        return 0
    node = data["node"]
    totals = data.get("totals", {})
    print("节点 %s (%s)  运行 %.0f 秒" % (node["name"], node["short_id"], node["uptime_s"]))
    print("  音频端口 %d · 网页 %d" % (node["audio_port"], node["web_port"]))
    print("  发送 %d 路 (%s kbps) · 播放 %d 路 · 接收 %d 路"
          % (totals.get("active_senders", 0), totals.get("sent_kbps", 0), totals.get("active_sinks", 0), totals.get("active_streams", 0)))
    peers = data.get("peers", [])
    print("  设备 %d 台:" % len(peers))
    for p in peers:
        mark = "本机" if p.get("local") else ("在线" if p.get("online") else "离线")
        print("    - %-24s %-15s %s" % (_short(p["name"], 24), p.get("host", ""), mark))
    routes = data.get("routes", [])
    print("  路由 %d 条:" % len(routes))
    for r in routes:
        print("    - %s -> %s  音量 %s %s" % (",".join(r["from"]), ",".join(r["to"]), r["gain"], "(静音)" if r["mute"] else ""))
    if data.get("last_error"):
        print("  最近错误: %s" % data["last_error"])
    return 0


# --------------------------------------------------------------------------
# route
# --------------------------------------------------------------------------

def _post(port: int, path: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(port: int, path: str) -> dict:
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_route(args: argparse.Namespace) -> int:
    cfg = Config.load()
    port = args.web_port or cfg.web_port
    try:
        if args.action == "list":
            data = _get(port, "/api/routes")
            if args.json:
                _print(data)
            else:
                for r in data["routes"]:
                    print("%-10s %-28s -> %-28s gain=%.2f %s %s"
                          % (r["id"], ",".join(r["from"]), ",".join(r["to"]), r["gain"], r["codec"], "(muted)" if r["mute"] else ""))
                if not data["routes"]:
                    print("（没有路由）")
            return 0
        if args.action == "add":
            if not args.to:
                print("需要 --to（目标设备，可重复）")
                return 2
            body = {
                "route": {
                    "from": args.from_ or ["self"],
                    "to": args.to,
                    "gain": args.gain,
                    "codec": args.codec or cfg.codec,
                    "sink_device": args.sink_device or "",
                    "mute": False,
                }
            }
            data = _post(port, "/api/routes/add", body)
            _print(data)
            return 0
        if args.action == "rm":
            if not args.id:
                print("需要 --id")
                return 2
            _print(_post(port, "/api/routes/delete", {"id": args.id}))
            return 0
        if args.action == "mute":
            _print(_post(port, "/api/routes/update", {"id": args.id, "mute": True}))
            return 0
        if args.action == "unmute":
            _print(_post(port, "/api/routes/update", {"id": args.id, "mute": False}))
            return 0
        if args.action == "gain":
            _print(_post(port, "/api/routes/update", {"id": args.id, "gain": args.gain}))
            return 0
        if args.action == "clear":
            _print(_post(port, "/api/routes", {"routes": []}))
            return 0
    except Exception as exc:
        print("操作失败（控制台在 %d 端口运行吗？）: %s" % (port, exc))
        return 1
    print("未知操作")
    return 2


# --------------------------------------------------------------------------
# peer
# --------------------------------------------------------------------------

def cmd_peer(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.action == "list":
        if args.json:
            _print(cfg.manual_peers)
            return 0
        for p in cfg.manual_peers:
            local = disc.is_lan_address(str(p.get("host") or ""))
            print("%-16s discovery=%s audio=%s %s%s" % (p.get("host"), p.get("port", cfg.discovery_port),
                                                        p.get("audio_port", cfg.audio_port), p.get("name", ""),
                                                        "" if local else "   [非局域网地址，会被忽略]"))
        if not cfg.manual_peers:
            print("（没有手动设备）")
        return 0
    if args.action == "add":
        if not args.host:
            print("需要 host")
            return 2
        if not disc.is_lan_address(args.host):
            print("拒绝添加 %s：不是局域网地址（WaveLink 不做跨网穿透）。" % args.host)
            return 2
        cfg.manual_peers = [p for p in cfg.manual_peers if p.get("host") != args.host]
        cfg.manual_peers.append({"host": args.host, "port": cfg.discovery_port, "audio_port": cfg.audio_port, "name": args.name or ""})
        cfg.save()
        print("已添加 %s（重启 serve 后生效；也可在网页里立即添加）" % args.host)
        return 0
    if args.action == "rm":
        cfg.manual_peers = [p for p in cfg.manual_peers if p.get("host") != args.host]
        cfg.save()
        print("已删除 %s" % args.host)
        return 0
    return 2


# --------------------------------------------------------------------------
# send / play (one-shot, no daemon)
# --------------------------------------------------------------------------

def cmd_send(args: argparse.Namespace) -> int:
    """Capture this machine's audio and stream it to a remote WaveLink node."""
    if not disc.is_lan_address(args.host) and not args.allow_wan:
        print("拒绝发送到 %s：这不是局域网地址。" % args.host)
        print("WaveLink 是局域网内工具（不做穿透/中继）。想强制发送请加 --allow-wan，")
        print("但请确认这是你自己的网络，跨公网传输未加密。")
        return 2
    spec: dict[str, Any] = {"backend": args.backend or audio_base.default_backend_name()}
    if args.file:
        spec = {"backend": "file", "path": args.file, "loop": args.loop}
    else:
        spec["device"] = args.source or "@default"
        spec["loopback"] = not args.microphone
    codec = proto.CODEC_BY_NAME.get(args.codec, proto.CODEC_PCM_S16)
    source = audio_base.open_source(spec)
    session = SenderSession(
        source,
        args.host,
        args.port,
        codec=codec,
        gain=args.gain,
        node_id=Config.load().node_id,
        source_name=source.name,
    )
    if not session.supported:
        print("该机器不支持 %s 编码，改用 pcm_s16" % args.codec)
        session.codec = proto.CODEC_PCM_S16
        from .net.rtpaudio import make_encoder

        session._encoder = make_encoder(proto.CODEC_PCM_S16)
    session.start()
    print("正在发送 %s -> %s:%d (%s)，Ctrl+C 停止" % (source.describe().get("device"), args.host, args.port, proto.CODEC_NAMES[session.codec]))
    try:
        while True:
            time.sleep(1.0)
            st = session.stats()
            print("  已发送 %d 包 / %.1f kbps%s" % (st["packets"], st["kbps"], (" 错误: " + st["error"]) if st["error"] else ""), end="\r")
    except KeyboardInterrupt:
        print("\n停止发送")
    finally:
        session.stop()
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    """Receive WaveLink audio on this machine and play it to a chosen device."""
    import numpy as np  # noqa: F401  (only to fail fast if numpy is missing)

    from .audio.format import CHANNELS, SAMPLE_RATE
    from .net.rtpaudio import Receiver

    node_id = args.node_id or Config.load().node_id
    receiver = Receiver(node_id, args.port)
    spec: dict[str, Any] = {"backend": args.backend or audio_base.default_backend_name()}
    if args.file:
        spec = {"backend": "file", "path": args.file}
    else:
        spec["device"] = args.sink or "@default"
    sink = audio_base.open_sink(spec)
    sink.start()
    receiver.start()
    print("正在接收 udp/%d 并播放到 %s，Ctrl+C 停止" % (args.port, sink.describe().get("device")))
    frames = 480
    prefill = sink.target_frames()
    silence = [0.0] * (frames * CHANNELS)
    filled = 0
    while filled < prefill:
        n = sink.write(silence)
        if n == 0:
            break
        filled += n
    deadline = time.monotonic()
    try:
        while True:
            blocks = receiver.mix_by_route(frames)
            block = blocks.get("")
            if block is None:
                block = silence
            sink.write(list(block))
            deadline += frames / float(SAMPLE_RATE)
            sleep = deadline - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -0.05:
                deadline = time.monotonic()
    except KeyboardInterrupt:
        print("\n停止接收")
    finally:
        receiver.stop()
        sink.stop()
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    """Full self-test: capture, encode, UDP loopback, jitter buffer, decode, render.

    Runs against a throwaway config directory so it can never leave test ports or
    test routes in the user's %APPDATA% (that happened during development and made
    the control panel listen somewhere unexpected).
    """
    previous_home = _isolate_config()
    try:
        return _doctor_body(args)
    finally:
        _restore_config(previous_home)


def _doctor_body(args: argparse.Namespace) -> int:
    import numpy as np

    from .audio.format import CHANNELS, SAMPLE_RATE, make_tone, measure_frequency, signal_report
    from .net.rtpaudio import Receiver, SenderSession

    failures: list[str] = []
    notes: list[str] = []
    print("WaveLink %s 自检" % __version__)
    print("=" * 62)

    # 1. backends ---------------------------------------------------------
    avail = audio_base.available_backends()
    print("1. 音频后端: %s" % ", ".join("%s=%s" % (k, "可用" if v else "不可用") for k, v in avail.items()))
    if not any(v for k, v in avail.items() if k not in ("null", "file")):
        notes.append("没有可用的真实音频后端，只测试数据通路")

    # 2. devices ----------------------------------------------------------
    devices = audio_base.list_all_devices()
    sources = [d for d in devices if d.kind == audio_base.KIND_SOURCE]
    sinks = [d for d in devices if d.kind == audio_base.KIND_SINK]
    print("2. 设备: 可采集 %d 个, 可播放 %d 个" % (len(sources), len(sinks)))
    for d in sources[:6]:
        print("     src  %-9s %s" % (d.kind_detail or d.backend, _short(d.name)))
    for d in sinks[:6]:
        print("     sink %-9s %s" % (d.kind_detail or d.backend, _short(d.name)))

    # 3. protocol ---------------------------------------------------------
    pkt = proto.Packet(type=proto.T_AUDIO, seq=1234, ssrc=0xDEADBEEF & 0x7FFFFFFF, samples=480, ts_ns=proto.now_ns(), payload=b"\x01\x02\x03")
    decoded = proto.decode(pkt.encode())
    if decoded is None or decoded.seq != 1234 or decoded.ssrc != (0xDEADBEEF & 0x7FFFFFFF) or decoded.payload != b"\x01\x02\x03":
        failures.append("报文编解码不一致")
    print("3. 报文: 头 %d 字节, 往返 %s" % (proto.HEADER_SIZE, "OK" if decoded else "失败"))

    # 4. UDP path on the loopback interface --------------------------------
    port = args.port or 47999
    receiver = Receiver("doctor", port)
    receiver.start()
    received_frames = 0

    from .audio.format import CHANNELS, SAMPLE_RATE, Chirp, best_lag, make_tone, signal_report

    class ToneSource:
        """Phase-continuous chirp source; it also remembers how many frames it made
        so the test can predict what the receiver *should* measure."""

        name = "test-chirp"
        sample_rate = SAMPLE_RATE
        channels = CHANNELS

        def __init__(self, blocks: int) -> None:
            self.chirp = Chirp(300.0, 3000.0, seconds=max(0.5, blocks * 0.01))
            self.blocks = blocks
            self.i = 0
            self.eof = False
            self.frames = 0
            self.first_block = None   # reference copy for the latency measurement

        def start(self) -> None:
            pass

        def read(self):
            if self.i >= self.blocks:
                self.eof = True
                return []
            self.i += 1
            block = self.chirp.read(480, CHANNELS)
            if self.first_block is None:
                self.first_block = list(block)
            self.frames += 480
            return block

        def stop(self) -> None:
            pass

    source = ToneSource(150)
    session = SenderSession(
        source,
        "127.0.0.1",
        port,
        codec=proto.CODEC_PCM_S16,
        node_id="doctor-src",
        source_name="chirp",
    )
    session.start()
    # Consume exactly 10 ms of audio every 10 ms, the same way a real sink render
    # thread does; anything else would measure our own test pacing, not the link.
    blocks = []
    frames = 480
    deadline = time.monotonic()
    end = deadline + 1.5
    mix_calls = 0
    slow_calls = 0
    while time.monotonic() < end:
        t_call = time.perf_counter()
        blocks.append(receiver.mix(frames))
        mix_calls += 1
        if time.perf_counter() - t_call > 0.02:
            slow_calls += 1
        deadline += frames / float(SAMPLE_RATE)
        sleep = deadline - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        elif sleep < -0.05:
            deadline = time.monotonic()
    if args.debug:
        print("   [debug] mix calls=%d slow(>20ms)=%d expected=150" % (mix_calls, slow_calls))
    session.stop()
    time.sleep(0.1)
    stack = np.concatenate(blocks, axis=0).reshape(-1)
    received_frames = len(stack) // CHANNELS
    report = signal_report(list(stack))
    # The chirp sweeps 300 -> 3000 Hz over `source.frames` frames of audio. A clean
    # path reproduces exactly that shape, so compare the measured rate over the
    # middle 60% against what the sweep should be doing there. The receiver buffers
    # a pre-roll before it starts playing, which shows up as leading silence; the
    # expected value must therefore be taken from where the audio really starts.
    total_audio_s = source.frames / float(SAMPLE_RATE)
    mono_lead = 0
    peak = report["peak"]
    if peak > 0:
        ch = CHANNELS
        flat = np.asarray(stack, dtype=np.float32).reshape(-1, ch)[:, 0]
        idx = np.nonzero(np.abs(flat) > max(1e-4, peak * 0.15))[0]
        if len(idx):
            mono_lead = int(idx[0])
    mid_lo = int(received_frames * 0.2)
    mid_hi = int(received_frames * 0.8)
    avg_s = (mid_lo + mid_hi) / 2.0 / float(SAMPLE_RATE) - mono_lead / float(SAMPLE_RATE)
    expected_mid = 300.0 + (3000.0 - 300.0) * min(1.0, max(0.0, avg_s / max(1e-6, total_audio_s)))
    measured_mid = signal_report(list(stack[mid_lo * CHANNELS : mid_hi * CHANNELS]))["frequency"]

    # End-to-end latency: the receiver pads a pre-roll of silence before playback
    # starts, so the sent block is NOT at offset 0 of the returned stream. Slide the
    # sent reference over the received stream and take the best match; the offset is
    # the real capture-to-playback latency through the whole chain.
    latency_ms = None
    latency_score = 0.0
    if peak > 0 and source.first_block:
        sent_ref = np.asarray(source.first_block, dtype=np.float64)[::CHANNELS]
        recv_mono = np.asarray(stack, dtype=np.float64).reshape(-1, CHANNELS)[:, 0]
        window = recv_mono[: min(len(recv_mono), SAMPLE_RATE)]  # search the first second
        if len(window) > len(sent_ref) + 16 and float(np.sum(sent_ref * sent_ref)) > 0:
            corr = np.correlate(window, sent_ref, mode="valid")
            power = np.convolve(window * window, np.ones(len(sent_ref)), mode="valid")
            denom = np.sqrt(power) * np.sqrt(float(np.sum(sent_ref * sent_ref)))
            with np.errstate(divide="ignore", invalid="ignore"):
                ncc = np.where(denom > 0, corr / denom, 0.0)
            if ncc.size:
                best = int(np.argmax(ncc))
                latency_score = float(ncc[best])
                if latency_score > 0.5:
                    latency_ms = best / float(SAMPLE_RATE) * 1000.0
    if args.debug and latency_ms is None:
        print("   [debug] 延迟相关失败: lead=%s score=%.2f" % (mono_lead, latency_score))

    print(
        "4. UDP 数据通路: 收到 %d 帧, peak=%.3f, 中段 %.0f Hz (应为 %.0f Hz), 全程 %.0f Hz%s"
        % (
            received_frames,
            report["peak"],
            measured_mid,
            expected_mid,
            report["frequency"],
            "" if latency_ms is None else ", 端到端延迟 %.0f ms" % latency_ms,
        )
    )
    if args.debug:
        win = SAMPLE_RATE // 10 * CHANNELS
        env = [float(np.sqrt(np.mean(stack[w : w + win] ** 2))) for w in range(0, max(0, len(stack) - win), win)]
        print("   [debug] per-100ms rms: %s" % " ".join("%.2f" % v for v in env[:20]))
        print("   [debug] receiver buffers: %s" % {"%08x" % k: v.stats() for k, v in receiver.buffers.items()})
        print("   [debug] mix calls=%d slow(>20ms)=%d expected=150" % (mix_calls, slow_calls))
    if received_frames < 24000:
        failures.append("UDP 通路收包不足（%d 帧）" % received_frames)
    if report["peak"] < 0.05:
        failures.append("UDP 通路没有可听音频（peak=%.3f）" % report["peak"])
    elif abs(measured_mid - expected_mid) > 0.06 * expected_mid:
        failures.append("UDP 通路频率不符: %.1f Hz (期望 %.1f Hz)" % (measured_mid, expected_mid))
    receiver.stop()

    # 5. jitter buffer ----------------------------------------------------
    buf = JitterBuffer()
    t0 = proto.now_ns()
    for i in range(10):
        buf.write(make_tone(440.0, 480, 0.3, CHANNELS), t0 + i * 10_000_000, 0.0)
    stats = buf.stats()
    print("5. 抖动缓冲: 写入 %d 帧, 目标 %.0f ms, 深度 %.0f ms" % (stats["written"], stats["target_ms"], stats["depth_ms"]))
    if stats["written"] != 4800:
        failures.append("抖动缓冲写入量错误: %d" % stats["written"])

    # 6. real capture -> real playback (only if hardware exists) -----------
    if avail.get("wasapi") and sources and sinks:
        from .audio.capture import CaptureHub

        hub = CaptureHub()
        reader = hub.reader({"backend": "wasapi", "device": "@default", "loopback": True})
        sink_spec = {"backend": "wasapi", "device": "@default"}
        sink = audio_base.open_sink(sink_spec)
        sink.start()
        collected = []
        t_end = time.monotonic() + 1.0
        # Play a tone while recording loopback, so we prove the capture path.
        tone = make_tone(880.0, 480, 0.3, CHANNELS)
        while time.monotonic() < t_end:
            block = reader.read()
            if block:
                sink.write(tone)
                collected.extend(block)
        reader.stop()
        hub.stop_all()
        sink.stop()
        rep = signal_report(collected)
        print("6. 真机回路测试: 采集 %d 帧, peak=%.3f, rms=%.4f" % (rep["frames"], rep["peak"], rep["rms"]))
        if rep["rms"] < 0.005:
            notes.append("系统声音采集到的是静音（这台机器当前没有在播放声音？）")
    else:
        print("6. 真机回路测试: 跳过（没有真实音频后端）")

    print("=" * 62)
    for n in notes:
        print("注意: %s" % n)
    if failures:
        for f in failures:
            print("失败: %s" % f)
        return 1
    print("自检通过。")
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audiomixing", description="把多台设备的声音投射到指定的音响设备（局域网）")
    p.add_argument("--version", action="version", version="wavelink %s" % __version__)
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("devices", help="列出本机可采集/可播放的音频设备")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_devices)

    net = sub.add_parser("net", help="查看本机的局域网接口（确认哪些地址可以互投）")
    net.add_argument("--json", action="store_true")
    net.set_defaults(func=cmd_net)

    s = sub.add_parser("serve", help="启动节点与控制台（推荐）")
    s.add_argument("--name", help="设备显示名")
    s.add_argument("--host", default="0.0.0.0", help="控制台监听地址")
    s.add_argument("--web-port", type=int, default=0)
    s.add_argument("--audio-port", type=int, default=0)
    s.add_argument("--discovery-port", type=int, default=0)
    s.add_argument("--source-device", default="", help="默认采集设备（含 loopback 的系统声音）")
    s.add_argument("--sink-device", default="", help="默认播放设备")
    s.add_argument("--codec", choices=["pcm_s16", "pcm_f32", "opus"], default="")
    s.add_argument("--simulate", action="store_true", help="不打开真实声卡，用内置测试音")
    s.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    s.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="查看正在运行的节点")
    st.add_argument("--web-port", type=int, default=0)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("route", help="管理路由表")
    r.add_argument("action", choices=["list", "add", "rm", "mute", "unmute", "gain", "clear"])
    r.add_argument("--from", dest="from_", action="append", help="源设备（可重复，默认 self）")
    r.add_argument("--to", action="append", help="目标设备（可重复）")
    r.add_argument("--id", help="路由 id")
    r.add_argument("--gain", type=float, default=1.0)
    r.add_argument("--sink-device", default="")
    r.add_argument("--codec", default="")
    r.add_argument("--web-port", type=int, default=0)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_route)

    pe = sub.add_parser("peer", help="手动管理设备（组播不可用时）")
    pe.add_argument("action", choices=["list", "add", "rm"])
    pe.add_argument("host", nargs="?", default="")
    pe.add_argument("--name", default="")
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=cmd_peer)

    se = sub.add_parser("send", help="把本机声音直接发给某台设备（不用守护进程）")
    se.add_argument("host")
    se.add_argument("--port", type=int, default=proto.PORT_AUDIO)
    se.add_argument("--source", default="@default")
    se.add_argument("--microphone", action="store_true", help="采集麦克风而不是系统声音")
    se.add_argument("--file", help="改为发送 WAV 文件")
    se.add_argument("--loop", action="store_true")
    se.add_argument("--codec", default="pcm_s16", choices=list(proto.CODEC_BY_NAME))
    se.add_argument("--gain", type=float, default=1.0)
    se.add_argument("--backend", default="")
    se.add_argument("--allow-wan", action="store_true", help="允许发送到非局域网地址（默认拒绝）")
    se.set_defaults(func=cmd_send)

    pl = sub.add_parser("play", help="接收音频并播放到指定设备")
    pl.add_argument("--port", type=int, default=proto.PORT_AUDIO)
    pl.add_argument("--sink", default="@default")
    pl.add_argument("--file", help="改为写入 WAV 文件")
    pl.add_argument("--node-id", default="")
    pl.add_argument("--backend", default="")
    pl.set_defaults(func=cmd_play)

    doc = sub.add_parser("doctor", help="端到端自检")
    doc.add_argument("--port", type=int, default=0)
    doc.add_argument("--debug", action="store_true", help="打印每一步的原始测量数据")
    doc.set_defaults(func=cmd_doctor)

    nc = sub.add_parser("netcheck", help="诊断手机/别的电脑连不上的原因（输出可复制粘贴）")
    nc.add_argument("--web-port", type=int, default=0)
    nc.set_defaults(func=cmd_netcheck)
    return p


def _use_utf8_output() -> None:
    """Make Chinese output safe regardless of the console code page.

    A packaged exe inherits whatever code page the user's console has (often GBK on
    Chinese Windows, sometimes cp437). Reconfiguring the std streams to UTF-8 with
    replacement avoids both mojibake and the classic UnicodeEncodeError crash when a
    log line contains a character the code page cannot represent.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _flush() -> None:
    """Flush stdout so a double-clicked exe shows its banner immediately.

    When stdout is redirected (or when the exe is launched by Explorer), Python
    switches to block buffering: without this the user stares at an empty window and
    assumes the program is broken.
    """
    try:
        sys.stdout.flush()
    except (ValueError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Double-clicked executable with no arguments: behave like a normal desktop app.
    # A console window opens, the node starts, and the control panel appears in the
    # browser. This is the entry point a non-technical user should ever need.
    if not argv and getattr(sys, "frozen", False):
        argv = ["serve", "--open-browser"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # a packaged exe must not vanish without a trace
        print("\n[WaveLink] 出错了: %s: %s" % (type(exc).__name__, exc))
        import traceback

        traceback.print_exc()
        if getattr(sys, "frozen", False):
            print("\n这个窗口不会自动关闭，方便你看清错误。按回车退出。")
            try:
                input()
            except EOFError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
