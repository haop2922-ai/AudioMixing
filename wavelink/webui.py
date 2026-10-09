"""The control panel: a dependency-free HTTP server plus a single-page UI.

Runs inside the WaveLink node process, so one command gives you both the audio
engine and the thing you click on. The page itself lives in ``wavelink/ui/index.html``
(plain HTML/CSS/JS — no build step, no node_modules, no CDN, so it works on an
isolated LAN); this module only serves it and exposes the JSON API it talks to.

    http://<this-machine>:17800
"""

from __future__ import annotations

import html as _html

import base64
import hashlib
from datetime import datetime
import json
import os
import select
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .net import discovery as disc
from .net import rtpaudio
from . import bluetooth

MAX_BODY = 256 * 1024
_LAST_FORCED_BT = 0.0


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class RawAudioServer(threading.Thread):
    """Phone audio over one plain TCP connection.

    The HTTP endpoint needs a request *and* a response per 20 ms block, so a slow reply is
    a slow microphone - which is why this sounded worse than Bluetooth or WO Mic, both of
    which simply stream. Here: one connection, a one line header naming the phone, then raw
    PCM16 until the connection drops. Nothing on the send path ever waits for anything.
    """

    def __init__(self, node, port: int) -> None:
        super().__init__(daemon=True, name="wavelink-rawmic")
        self.node = node
        self.port = port
        self.stop_event = threading.Event()
        self.error = ""
        self.clients = 0
        self.bytes_in = 0

    def run(self) -> None:
        import socket

        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", self.port))
            server.listen(16)
            server.settimeout(0.5)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self._note("音频长连接通道无法监听 %d：%s" % (self.port, exc))
            return
        self._note("手机音频长连接通道已监听 %d" % self.port)
        while not self.stop_event.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                self.error = str(exc)
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        try:
            server.close()
        except OSError:
            pass

    def _note(self, message: str) -> None:
        try:
            self.node.note(message)
        except Exception:
            pass

    def _serve(self, conn) -> None:
        import socket

        from .audio.backends import netmic as netmic_backend

        name = "手机"
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # No read timeout: a phone that is simply not playing anything goes quiet for
            # minutes, and timing it out dropped the connection - after which the app fell
            # back to the slow HTTP route and never came back. TCP keepalive still notices
            # a phone that actually disappeared.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(None)
            stream = conn.makefile("rb")
            header = stream.readline(200).decode("utf-8", "replace").strip()
            parts = header.split()
            name = parts[1] if len(parts) > 1 else "手机"
            rate = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 48000
            mic = netmic_backend.open_mic(name, rate)
            self.clients += 1
            self.node.wake()
            self._note("手机 %s 用长连接开始发送（%d Hz）" % (name, rate))
            carry = b""
            while not self.stop_event.is_set():
                # Read one 20 ms unit at a time, not a big block: a buffered read of
                # 8 kB holds four blocks back and adds 80 ms of delay for nothing.
                try:
                    chunk = stream.read(1920)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data = carry + chunk
                if len(data) % 2:
                    carry = data[-1:]
                    data = data[:-1]
                else:
                    carry = b""
                if data:
                    mic.push(data)
                    self.bytes_in += len(data)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self._note("手机 %s 的长连接出错：%s" % (name, exc))
        finally:
            self.clients = max(0, self.clients - 1)
            self._note("手机 %s 的长连接已断开" % name)
            try:
                conn.close()
            except OSError:
                pass

    def stats(self) -> dict:
        return {"port": self.port, "error": self.error, "clients": self.clients,
                "bytes_in": self.bytes_in}

    def stop(self) -> None:
        self.stop_event.set()


class WaveLinkApi:
    """Thin logic layer; the HTTP handler only parses and serialises."""

    def __init__(self, node, *, web=None, on_routes_changed: Callable[..., None] | None = None) -> None:
        self.node = node
        self.web = web
        self.on_routes_changed = on_routes_changed

    # -- GET ---------------------------------------------------------------
    def page_rev(self) -> str:
        """A fingerprint of the page being served right now.

        The panel is long-lived: a tab left open keeps running the JavaScript it loaded,
        so a fix could be invisible for hours with nothing on screen to say why. Each
        status response carries this, and the page reloads itself when it changes.
        """
        try:
            return hashlib.sha1(index_html().encode("utf-8")).hexdigest()[:10]
        except Exception:
            return ""

    def status(self) -> dict[str, Any]:
        data = self.node.status()
        data["page_rev"] = self.page_rev()
        web = getattr(self, "web", None)
        raw = getattr(web, "raw", None) if web is not None else None
        data["raw_audio"] = raw.stats() if raw is not None else {
            "port": 0, "error": "未启动", "clients": 0, "bytes_in": 0}
        return data

    def peers(self) -> dict[str, Any]:
        node = self.node
        report = disc.lan_report()
        primary = (report["interfaces"] or [{"address": "127.0.0.1"}])[0]
        return {
            "self": {
                "id": node.node_id,
                "short_id": disc.short_id(node.node_id),
                "name": node.name,
                "host": primary["address"],
                "audio_port": node.config.audio_port,
                "web_port": node.config.web_port,
                "sources": node.source_list(),
                "sinks": node.sink_list(),
            },
            "peers": node.registry.snapshot(),
            "network": report,
        }

    def network(self) -> dict[str, Any]:
        return disc.lan_report()

    def routes(self) -> dict[str, Any]:
        return {"routes": self.node.routes}

    def devices(self) -> dict[str, Any]:
        self.node.refresh_devices(force=True)
        return {
            "sources": self.node.source_list(),
            "sinks": self.node.sink_list(),
            "all": [d.to_json() for d in self.node.devices],
        }

    def netcheck(self) -> dict[str, Any]:
        """把和"连不通"有关的本机情况一次说清楚。

        为什么要这个东西：连不通的现象有好几种（防火墙挡入站、网络被判成公用、
        程序路径变了导致旧规则失效、没有放行 UDP 于是收不到发现广播），
        在故障的那台机器上跑一次这个，界面直接显示结论，不用去命令行敲。
        """
        import subprocess as sp

        out: dict[str, Any] = {"checks": [], "node": str(getattr(self.node, "name", ""))}

        def add(name: str, ok: bool | None, detail: str) -> None:
            out["checks"].append({"name": name, "ok": ok, "detail": detail})

        if sys.platform != "win32":
            add("防火墙", None, "只有 Windows 需要检查这一项")
            return out

        flags = getattr(sp, "CREATE_NO_WINDOW", 0)

        def ps(script: str) -> str:
            try:
                return (sp.run(["powershell", "-NoProfile", "-Command", script],
                               creationflags=flags, capture_output=True, text=True,
                               timeout=25).stdout or "").strip()
            except Exception as exc:
                return "读取失败: %s" % exc

        # 网络位置：公用网络下入站更容易被挡
        prof = ps("Get-NetConnectionProfile | ForEach-Object { $_.Name + '|' + "
                  "$_.NetworkCategory }")
        public = [line for line in prof.splitlines() if line.strip().endswith("Public")]
        # "公用网络"本身不是错误：只要放行规则覆盖了公用，入站照样能通。
        # 以前这里一律标红，害得人以为必须去改网络位置。
        detail = "；".join(line.strip() for line in prof.splitlines() if line.strip()) or "读不到"
        if public:
            detail += "　（有公用网络；只要下面的入站规则覆盖了公用，就不影响使用）"
        add("网络位置", True, detail)

        # 防火墙总开关
        fw = ps("Get-NetFirewallProfile | ForEach-Object { $_.Name + '=' + $_.Enabled }")
        add("防火墙状态", True, "；".join(x.strip() for x in fw.splitlines() if x.strip()) or "读不到")

        # 我们程序的入站规则：路径对不对、协议全不全
        rules = ps(
            "Get-NetFirewallApplicationFilter | "
            "Where-Object { $_.Program -like '*avelink*' } | ForEach-Object { "
            "$r = $_ | Get-NetFirewallRule; "
            "$p = $r | Get-NetFirewallPortFilter; "
            "if ($r.Direction -eq 'Inbound') { "
            "$_.Program + '|' + $r.Action + '|' + $r.Profile + '|' + $p.Protocol } }")
        lines = [x.strip() for x in rules.splitlines() if x.strip()]
        tcp = any("|TCP" in x.upper() and "|Allow|" in x for x in lines)
        udp = any("|UDP" in x.upper() and "|Allow|" in x for x in lines)
        howto = ("右键右下角托盘里的 WaveLink 图标，点「放行防火墙（需管理员）」，"
                 "在弹窗里点「是」，然后重新检查")
        add("入站 TCP 放行", tcp,
            "；".join(lines) if tcp else
            "没有任何和本程序有关的入站规则，别人连不进来。" + howto)
        add("入站 UDP 放行", udp, "已放行" if udp else
            "没放行 UDP，收不到对方的发现广播，对端不知道你在哪。" + howto)

        # 正在跑的 exe 路径：规则是绑路径的，换了目录旧规则就失效
        exe = ps("(Get-Process -Name WaveLink -ErrorAction SilentlyContinue | "
                 "Select-Object -First 1 -ExpandProperty Path)")
        add("当前程序路径", bool(exe), exe or "读不到")
        if exe:
            matched = any(x.lower().startswith(exe.lower()) for x in lines)
            add("规则是否覆盖当前路径", matched,
                "覆盖到了" if matched else
                "没有覆盖。防火墙规则是绑程序路径的，这个路径从来没被放行过，"
                "所以别人连不进来。" + howto +
                "。另外提醒：以后把程序换个目录，规则就失效了，要在这里重新放行一次")

        out["ok"] = all(c["ok"] for c in out["checks"] if c["ok"] is not None)
        return out


    def diagnose(self) -> dict[str, Any]:
        """把所有和"传不了声音"有关的状态汇成一份纯文本报告。

        为什么做这个：排查要在出问题的那台机器上看，而那台机器上可能什么都没有
        （没 Python、没命令行习惯）。用户只要在监控窗点一下，报告就能复制出来发人。
        报告只读，不修改任何东西。
        """
        node = self.node
        lines: list[str] = []

        def head(title: str) -> None:
            lines.append("")
            lines.append("===== %s =====" % title)

        lines.append("WaveLink 诊断报告")
        lines.append("生成时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import platform

            lines.append("系统 %s" % platform.platform())
        except Exception:
            pass
        lines.append("程序 %s" % (sys.executable if getattr(sys, "frozen", False) else "源码运行"))

        head("本机")
        st = self.status()
        n = st.get("node") or {}
        lines.append("名称 %s" % n.get("name"))
        lines.append("节点 ID %s" % n.get("id"))
        lines.append("端口 网页=%s 音频=%s 发现=%s" % (
            n.get("web_port"), n.get("audio_port"), n.get("discovery_port")))
        lines.append("已运行 %.0f 秒" % (n.get("uptime_s") or 0))

        head("对端")
        peers = st.get("peers") or []
        if not peers:
            lines.append("一个都没有。收不到对方的发现广播时，请手动添加对方 IP。")
        for p in peers:
            lines.append("%-14s host=%-16s 在线=%s 手动添加=%s 音频端口=%s id=%s" % (
                p.get("name"), p.get("host"), p.get("online"), p.get("manual"),
                p.get("audio_port"), p.get("node_id")))

        head("路由")
        routes = node.routes or []
        if not routes:
            lines.append("没有任何路由。没有路由就不会传声音，这是最常见的原因。")
        my_id = str(n.get("id") or "")
        for r in routes:
            frm = [str(x) for x in (r.get("from") or [])]
            to = [str(x) for x in (r.get("to") or [])]
            mine_from = my_id in frm or "self" in frm
            mine_to = my_id in to or "self" in to
            role = []
            if mine_from:
                role.append("本机要把声音发出去")
            if mine_to:
                role.append("本机要收下来播放")
            if not role:
                role.append("和本机无关")
            lines.append("id=%s 从=%s 到=%s 音量=%s 静音=%s 输出设备=%r 设备来源=%r 归属=%s" % (
                str(r.get("id"))[:8], r.get("from"), r.get("to"), r.get("gain"),
                r.get("mute"), r.get("sink_device"), r.get("source_device"),
                str(r.get("origin"))[:8]))
            lines.append("      本机角色：%s%s" % (
                "、".join(role),
                "　【这条是静音的，不会出声】" if r.get("mute") else ""))

        head("发送端（本机往外送）")
        senders = st.get("senders") or []
        # senders 是列表，不是字典。以前这里按字典读，于是"正在广播"的时候
        # 报告里写"没有"，让人以为没在发。
        if isinstance(senders, dict):
            entries = list(senders.items())
        else:
            entries = [(str(x.get("route_id") or ""), x) for x in senders
                       if isinstance(x, dict)]
        if not entries:
            lines.append("没有。若你的角色是把声音送出去，说明路由的「从」没有指向本机。")
        for rid, info in entries:
            is_channel = str(rid).startswith("channel")
            kind = "广播通道" if is_channel else "点对点"
            lines.append("[%s] %s → %s:%s  速率=%s KB/s 包=%s 字节=%s" % (
                kind, info.get("source"), info.get("host"), info.get("port"),
                info.get("kbps"), info.get("packets"), info.get("bytes")))
            if is_channel:
                lines.append("      广播通道不依赖路由：它由「广播我的声音」开关和音源清单决定。"
                             "对端只有在打开「只听」时才会播放它。")

        head("接收端（本机收进来的流）")
        recv = (self.streams().get("receiver") or {}).get("streams") or []
        if not recv:
            lines.append("没有。对端没在发，或者路由的「到」没有指向本机。")
        for s in recv:
            lines.append("来自=%s 包=%s 字节=%s" % (
                s.get("node_name"), s.get("packets"), s.get("bytes")))

        head("播放端（本机往设备写）")
        sinks = st.get("sinks") or []
        if not sinks:
            lines.append("没有。收到数据但没有播放端，通常是路由对不上本机。")
        for item in sinks:
            lines.append(json.dumps(item, ensure_ascii=False)[:240])

        head("本机声音来源与直通")
        for item in (st.get("inputs") or []):
            lines.append("[%s] %-14s %s %s" % (
                item.get("path_label"), item.get("name"), item.get("state"),
                str(item.get("detail") or "")[:60]))
        lines.append("直通 " + json.dumps(st.get("local") or {}, ensure_ascii=False)[:200])

        head("网络自检")
        for c in (self.netcheck().get("checks") or []):
            mark = "?" if c.get("ok") is None else ("通过" if c.get("ok") else "不通过")
            lines.append("[%s] %s：%s" % (mark, c.get("name"), c.get("detail")))

        head("发送端判定依据（引擎自报）")
        debug = st.get("debug") or {}
        if not debug:
            lines.append("引擎没有给出调试信息")
        for key, value in debug.items():
            lines.append("%s = %s" % (key, json.dumps(value, ensure_ascii=False)[:300]))

        head("最近日志")
        for line in (st.get("log") or [])[-25:]:
            lines.append(str(line))

        text = "\n".join(lines)
        path = ""
        try:
            base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                                "WaveLink")
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, "诊断报告.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            path = ""
        return {"text": text, "path": path}


    def monitor(self) -> dict[str, Any]:
        """一条一条的流向：谁给谁，走哪个通道，多少数据，延迟多少。

        这里是给监控窗用的，只报能测到的东西。测不到的宁可写清楚，不编数字：
        蓝牙那一路的声音由 Windows 直接送进耳机，本工具不参与，也就不计量。
        """
        node = self.node
        status = self.status()
        flows: list[dict[str, Any]] = []

        # 手机经 Wi-Fi 送进来，播放到本机。
        # 数据量来自网络音源自己的计数，速率用它和上一次采样的差值算。
        mics: dict = {}
        mic_error = ""
        try:
            from .audio.backends import netmic as netmic_backend

            # active() 返回的是 NetMic 对象，不是字典，所以按属性取。
            for item in netmic_backend.active():
                name = str(getattr(item, "name", "") or "")
                if name:
                    mics[name] = item
        except Exception as exc:
            # 不吞异常。之前这里静默变成空字典，监控里数据量恒为 0，
            # 而日志和界面都不说为什么，只能靠猜。
            mic_error = "%s: %s" % (type(exc).__name__, exc)
        now = time.time()
        previous = getattr(self, "_mon_prev", {})
        current_prev = {}
        for item in status.get("inputs") or []:
            if item.get("path") != "wifi":
                continue
            name = str(item.get("name") or "")
            mic = mics.get(name) or {}
            total = int(getattr(mic, "bytes_in", 0) or 0)
            rate = 0.0
            last = previous.get(name)
            if last and now > last[1] and total >= last[0]:
                rate = (total - last[0]) / 1024.0 / (now - last[1])
            current_prev[name] = (total, now)
            flows.append({
                "from": name,
                "to": "本机耳机",
                "channel": "Wi-Fi",
                "state": str(item.get("state") or ""),
                "bytes": total,
                "rate_kbps": round(rate, 1),
                "latency_ms": round(float(item.get("median_ms") or 0.0), 1),
                "detail": str(item.get("detail") or ""),
            })
        self._mon_prev = current_prev

        # 手机经蓝牙进来，由系统直接播放
        try:
            from . import bluetooth as bt

            for device in bt.cached_devices():
                if str(device.get("link") or "") != "connected":
                    continue
                flows.append({
                    "from": str(device.get("name") or ""),
                    "to": "本机耳机",
                    "channel": "蓝牙",
                    "state": "正在接收手机声音",
                    "bytes": None,
                    "rate_kbps": None,
                    "latency_ms": None,
                    "detail": "系统直接播放，本工具不参与，不计数据量",
                })
        except Exception:
            pass

        # 本机送给别的电脑。注意 senders 是**列表**，不是字典：
        # 以前这里按字典读，一条都匹配不上，于是"声音在传但监控什么都不显示"。
        try:
            senders = (node.sender.stats() or {})
        except Exception:
            senders = {}
        if isinstance(senders, dict):
            entries = list(senders.items())
        elif isinstance(senders, list):
            entries = [(str(item.get("route_id") or ""), item)
                       for item in senders if isinstance(item, dict)]
        else:
            entries = []
        live_ids = set()
        try:
            for rule in (node.routes or []):
                live_ids.add(str(rule.get("id") or ""))
        except Exception:
            pass
        for rid, info in entries:
            if not isinstance(info, dict):
                continue
            # 广播的发送端在引擎里也是"发送端"，标识以 channel 开头
            # （例如 channel|<节点ID>）。以前一律写成点对点，于是监控页
            # 把广播显示成点对点，而真正的广播流一条都不显示。
            is_channel = str(rid).startswith("channel") or str(
                info.get("route_id") or "").startswith("channel")
            raw_state = str(info.get("state") or "")
            target = str(info.get("target") or info.get("peer") or "")
            if not target:
                host = str(info.get("host") or "")
                port = info.get("port")
                target = (host + (":" + str(port) if port else "")) or rid
            amount = int(info.get("bytes") or info.get("sent_bytes") or 0)
            rate = round(float(info.get("kbps") or 0.0), 1)
            device = str(info.get("source") or "")
            # "从谁"这一列写的是**谁在发**，也就是这台机器的节点名（配置里那个名字），
            # 不是采集设备的名字。设备名放进说明里，免得让人以为声音来自耳机。
            node_name = str(getattr(node, "name", "") or "本机")
            source_label = node_name
            where = ("采自 " + device) if device else ""
            stale = bool(live_ids) and not is_channel and str(rid) not in live_ids
            if is_channel:
                flows.append({
                    "from": source_label,
                    "to": "全网设备（广播）",
                    "channel": "广播",
                    "state": "正在广播",
                    "bytes": amount,
                    "rate_kbps": rate,
                    "latency_ms": None,
                    "detail": ("手机和电脑里的听众都能收到" + (("，" + where) if where else "")),
                })
                continue
            flows.append({
                "from": source_label,
                "to": target,
                "channel": "点对点",
                "state": "残留（这条路由已经删了，但它还在发）" if stale else "正在发送",
                "bytes": amount,
                "rate_kbps": rate,
                "latency_ms": None,
                "detail": ("残留：这条路由已经删了但它还在发" if stale
                           else (where or str(info.get("error") or ""))),
            })

        # 广播的听众：谁在听这条广播
        try:
            channel = node.channel_state() or {}
        except Exception:
            channel = {}
        for listener in (channel.get("listeners") or channel.get("clients") or []):
            name = str(listener if not isinstance(listener, dict)
                       else listener.get("name") or listener.get("node_name") or "")
            if not name:
                continue
            flows.append({
                "from": "本机声音",
                "to": name,
                "channel": "广播",
                "state": "正在收听",
                "bytes": None,
                "rate_kbps": None,
                "latency_ms": None,
                "detail": "通过网页或手机收听这条广播",
            })

        # 别的电脑送给本机
        try:
            streams = (node.receiver.stats() or {}).get("streams") or []
        except Exception:
            streams = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            flows.append({
                "from": str(stream.get("node_name") or stream.get("source") or "其它电脑"),
                "to": "本机耳机",
                "channel": "点对点",
                "state": "正在接收",
                "bytes": int(stream.get("bytes") or 0),
                "rate_kbps": round(float(stream.get("kbps") or 0.0), 1),
                "latency_ms": round(float(stream.get("depth_ms") or 0.0), 1),
                "detail": "丢包 %s 迟到 %s" % (stream.get("lost", 0), stream.get("late", 0)),
            })

        local = status.get("local") or {}
        return {
            "flows": flows,
            "mic_error": mic_error,
            "mic_count": len(mics),
            "updated": round(time.time(), 1),
            "uptime_s": round(time.time() - getattr(node, "started_at", time.time()), 1),
            "local_peak": local.get("peak"),
            "node": str(getattr(node, "name", "")),
        }


    def streams(self) -> dict[str, Any]:
        stats = self.node.receiver.stats()
        captures = self.node.hub.stats()
        return {"receiver": stats, "captures": captures, "senders": self.node.sender.stats()}

    def logs(self) -> dict[str, Any]:
        node = self.node
        extra = getattr(self, "broadcast_log", [])
        return {"log": node.log[-200:], "last_error": node.last_error, "broadcast": extra[-50:]}

    # -- POST --------------------------------------------------------------
    def set_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
        routes = payload.get("routes")
        if routes is None and isinstance(payload.get("route"), dict):
            routes = [payload["route"]]
        if not isinstance(routes, list):
            raise ApiError("body must contain a 'routes' list")
        if len(routes) > 200:
            raise ApiError("too many routes (max 200)")
        applied = self.node.set_routes(routes)
        if self.on_routes_changed is not None:
            self.on_routes_changed(applied)
        return {"ok": True, "routes": applied}

    def add_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        route = payload.get("route") or payload
        if not isinstance(route, dict):
            raise ApiError("expected a route object")
        # Edit only *our* routes. The rest of the table belongs to other nodes and
        # is re-merged by the engine, so adding a route here can never delete a
        # route somebody else created on their own machine.
        routes = self.node.local_routes() + [route]
        applied = self.node.set_routes(routes)
        if self.on_routes_changed is not None:
            self.on_routes_changed(applied)
        return {"ok": True, "routes": applied}

    def update_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        route_id = str(payload.get("id") or "")
        if not route_id:
            raise ApiError("missing route id")
        routes = []
        found = False
        # Any panel may adjust any route on the LAN - the volume slider is the
        # everyday case, and the node doing the playing is often not the node that
        # created the route. Ownership is preserved, so the author can still edit it.
        for existing in self.node.routes:
            if existing["id"] == route_id:
                merged = dict(existing)
                merged.update({k: v for k, v in payload.items() if k != "id"})
                routes.append(merged)
                found = True
            else:
                routes.append(existing)
        if not found:
            raise ApiError("no such route: %s" % route_id, 404)
        applied = self.node.set_routes(routes)
        if self.on_routes_changed is not None:
            self.on_routes_changed(applied)
        return {"ok": True, "routes": applied}

    def delete_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        route_id = str(payload.get("id") or "")
        if not route_id:
            raise ApiError("missing route id")
        # Deleting a route another node owns needs two things: the id must leave our
        # own table (`drop`, because the merge would otherwise restore it), and it
        # must travel in the push so the owner drops its copy too - otherwise its
        # next push puts the route straight back.
        routes = [r for r in self.node.routes if r["id"] != route_id]
        applied = self.node.set_routes(routes, drop=[route_id])
        if self.on_routes_changed is not None:
            self.on_routes_changed(applied, [route_id])
        return {"ok": True, "routes": applied}

    def add_peer(self, payload: dict[str, Any]) -> dict[str, Any]:
        host = str(payload.get("host") or "").strip()
        if not host:
            raise ApiError("missing host")
        if not disc.is_lan_address(host):
            raise ApiError(
                "%s 不是局域网地址。WaveLink 只在同一局域网内工作（不做穿透/中继）。" % host,
                400,
            )
        port = int(payload.get("port") or self.node.config.discovery_port)
        audio_port = int(payload.get("audio_port") or self.node.config.audio_port)
        peer = self.node.registry.add_manual(host, audio_port, str(payload.get("name") or ""))
        if self.node.beacon is not None:
            self.node.beacon.add_manual_target(host, port)
        manual = [p for p in self.node.config.manual_peers if p.get("host") != host]
        manual.append({"host": host, "port": port, "name": payload.get("name") or ""})
        self.node.config.manual_peers = manual
        try:
            self.node.config.save()
        except OSError:
            pass
        return {"ok": True, "peer": peer.to_json()}

    def remove_peer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forget a device the user no longer wants in the list.

        Two cases matter:
          * a **manually added** entry (wrong IP typed in, a guest that left) - it is
            removed from the registry *and* from config, so it does not come back on
            the next start;
          * a **discovered** node - it is dropped from the registry, but an active
            peer will simply reappear on its next beacon (about 2 s). The response
            says which case happened so the page can explain it instead of lying.
        """
        target = str(payload.get("node_id") or payload.get("id") or "").strip()
        if not target:
            raise ApiError("missing node_id")

        peer = self.node.registry.get(target) or self.node.registry.resolve(target)
        if peer is None:
            raise ApiError("no such device: %s" % target, 404)

        was_manual = peer.manual
        host = peer.host
        node_id = peer.node_id

        removed = self.node.registry.remove(node_id)
        if not removed and host:
            removed = self.node.registry.remove("manual:" + host)

        # Drop it from the manual peer list too, otherwise a restart re-adds it.
        manual = [p for p in self.node.config.manual_peers if p.get("host") != host]
        changed = len(manual) != len(self.node.config.manual_peers)
        self.node.config.manual_peers = manual
        if changed:
            try:
                self.node.config.save()
            except OSError:
                pass

        # Stop any traffic aimed at the removed device.
        try:
            self.node.apply_routes()
        except Exception:
            pass

        return {
            "ok": True,
            "removed": node_id,
            "was_manual": was_manual,
            "note": "已永久删除（手动添加的设备）" if was_manual else "已从列表移除；如果它还开着，几秒后会重新被发现",
        }

    def set_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self.node
        changed: list[str] = []
        for key in ("name", "codec", "source_device", "sink_device", "accept_unknown"):
            if key in payload:
                setattr(node.config, key, payload[key])
                changed.append(key)
                if key == "name":
                    node.name = str(payload[key])
                    node.sender.node_name = node.name
                    if node.beacon is not None:
                        node.beacon.name = node.name
        for key in ("broadcast", "listen", "local", "auto_play"):
            if key in payload:
                setattr(node.config, key, bool(payload[key]))
                changed.append(key)
        if "open_panel_on_start" in payload:
            want = str(payload["open_panel_on_start"] or "none").strip().lower()
            if want not in ("none", "simple", "full"):
                raise ApiError("open_panel_on_start 只能是 none、simple 或 full", 400)
            node.config.open_panel_on_start = want
            changed.append("open_panel_on_start")
        if "channel_gain" in payload:
            node.config.channel_gain = max(0.0, min(4.0, float(payload["channel_gain"])))
            changed.append("channel_gain")
        if "channel_sources" in payload:
            raw = payload["channel_sources"]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise ApiError("channel_sources must be a list of device names")
            node.config.channel_sources = [str(x).strip() for x in raw if str(x).strip()][:8]
            changed.append("channel_sources")
        if "autoconnect" in payload:
            raw = payload["autoconnect"]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise ApiError("autoconnect must be a list of phone names")
            node.config.autoconnect = [str(x).strip() for x in raw if str(x).strip()][:8]
            changed.append("autoconnect")
            if "jitter_ms" in payload:
                node.config.jitter_ms = float(payload["jitter_ms"])
                for buf in node.receiver.buffers.values():
                    buf.target_frames = max(
                    rtpaudio.JITTER_MIN_MS, min(rtpaudio.JITTER_MAX_MS, float(payload["jitter_ms"]))
                    ) * 48
                changed.append("jitter_ms")
        try:
            node.config.save()
        except OSError as exc:
            raise ApiError("could not save config: %s" % exc, 500)
        node.refresh_devices(force=True)
        node.apply_routes()
        return {"ok": True, "changed": changed, "config": node.config.to_json()}

    def ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        host = str(payload.get("host") or "")
        if not host:
            raise ApiError("missing host")
        port = int(payload.get("port") or self.node.config.audio_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            from . import proto

            pkt = proto.Packet.control(proto.T_PING, {"t0": proto.now_ns(), "node": self.node.node_id})
            t0 = time.monotonic()
            sock.sendto(pkt.encode(), (host, port))
            data, addr = sock.recvfrom(4096)
            rtt_ms = (time.monotonic() - t0) * 1000.0
            parsed = proto.decode(data)
            return {
                "ok": parsed is not None,
                "host": addr[0],
                "rtt_ms": round(rtt_ms, 2),
                "type": proto.TYPE_NAMES.get(parsed.type, "?") if parsed else "invalid",
            }
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            sock.close()

    def test_tone(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Play a short tone on a local sink device — the 'can I hear it?' button."""
        seconds = min(10.0, float(payload.get("seconds", 2.0)))
        device = str(payload.get("device") or "")
        freq = float(payload.get("freq", 440.0))
        from .audio import base as audio_base
        from .audio.format import CHANNELS, SAMPLE_RATE, make_tone

        spec = self.node.default_sink_spec()
        if device:
            # `device` may name another backend's device (e.g. "null"), which a WASAPI
            # spec cannot open - and the silent null sink is exactly what a phone-path
            # check wants, so it must be reachable here.
            spec = audio_base.spec_for_selector(spec, device)
        try:
            sink = audio_base.open_sink(spec)
            sink.start()
        except Exception as exc:
            raise ApiError("could not open %s: %s" % (spec.get("device"), exc), 500)
        frames = 480
        tone = make_tone(freq, int(seconds * SAMPLE_RATE), 0.25, CHANNELS)
        written = 0
        tap = self.node.tap

        def run() -> None:
            nonlocal written
            # Paced: the sink takes a whole block immediately and buffers what the
            # device has not accepted, so an unpaced loop would dump the whole tone in
            # one go and have it dropped as backlog (the tone was nearly inaudible).
            # Every block is also mirrored to the listener tap, which makes this button
            # the way to check that a phone can hear this machine.
            written = audio_base.write_paced(sink, tone, frames_per_block=frames, on_block=tap.publish)
            time.sleep(0.3)
            sink.stop()

        threading.Thread(target=run, name="wavelink-tone", daemon=True).start()
        return {"ok": True, "device": spec.get("device"), "seconds": seconds, "freq": freq}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# WebSocket plumbing for the phone listener
# --------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept(key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key (RFC 6455)."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_frame(payload: bytes, opcode: int = 0x2) -> bytes:
    """One server->client frame. Server frames are never masked."""
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack(">H", size)
    else:
        header.append(127)
        header += struct.pack(">Q", size)
    return bytes(header) + payload


class _Handler(BaseHTTPRequestHandler):
    server_version = "WaveLink/0.1"
    api: WaveLinkApi
    quiet = True

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        if not self.quiet:
            super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _send_apk(self) -> None:
        """Hand the phone the app itself, over the same LAN the audio uses."""
        apk = apk_path()
        if apk is None:
            self._json(404, {"error": "这台电脑上还没有手机端安装包（wavelink-mic.apk）"})
            return
        try:
            # The same bytes the version stamp describes, from one place.
            data = apk.read_bytes()
        except OSError as exc:
            self._json(500, {"error": "读取安装包失败: %s" % exc})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % apk.name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _mic_stream(self, parsed) -> None:
        """Accept a block of PCM16 mono from a phone and queue it as a capture device.

        Content-Length rather than a WebSocket or chunked body: a phone can post a fixed
        100 ms block ten times a second over one keep-alive connection, which needs no
        framing code on either side - and the very same endpoint serves the browser page
        and the native Android app.
        """
        from .audio.backends import netmic as netmic_backend

        query = parse_qs(parsed.query)
        name = (query.get("name") or ["手机"])[0].strip() or "手机"
        try:
            rate = int((query.get("rate") or ["48000"])[0] or 48000)
        except ValueError:
            rate = 48000
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4_000_000 or length % 2:
            self._json(400, {"error": "expected a PCM16 body with an even byte count"})
            return
        body = self.rfile.read(length)
        if query.get("probe"):
            # A connectivity check from the phone app. It answers "can you hear me"
            # without becoming a device: registering it put a fake entry called
            # "连接测试" into every picker, and a one-click add wrote it into the config.
            self._json(200, {"ok": True, "probe": True, "bytes": len(body)})
            return
        known = netmic_backend.MICS.get(name)
        mic = netmic_backend.MICS.get(name)
        if mic is None or mic.device_rate != rate:
            mic = netmic_backend.open_mic(name, rate)
        if known is None:
            # First data from a phone: do not wait for the next poll to start playing it.
            try:
                self.api.node.wake()
            except Exception:
                pass
        frames = mic.push(body)
        state = mic.describe()
        self._json(200, {"ok": True, "frames": frames, "queued_frames": state["queued_frames"],
                         "dropped_frames": state["dropped_frames"]})

    def _websocket_audio(self) -> None:
        """Stream this node's output to a browser as raw PCM16, over WebSocket.

        Not plain request/response: audio is continuous, and a browser has no UDP
        socket at all, so it can never join the audio plane itself. The tap is filled
        by the render loop, which means a phone hears exactly what this node plays -
        no second audio path, and no extra latency beyond the socket.
        """
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._json(400, {"error": "this endpoint speaks websocket"})
            return
        conn = self.connection
        try:
            # Written by hand so the status line is HTTP/1.1, which some browsers
            # insist on for an upgrade, regardless of the server's default version.
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n\r\n" % ws_accept(key)
                ).encode("ascii")
            )
        except OSError:
            return
        self.close_connection = True

        tap = self.api.node.tap
        sid = tap.subscribe()
        block_s = 480 / 48000.0
        try:
            while True:
                payload = tap.drain(sid)
                if payload:
                    conn.sendall(ws_frame(payload))
                # Read whatever the client sent (close/ping) so it cannot pile up.
                ready, _, _ = select.select([conn], [], [], 0)
                if ready:
                    data = conn.recv(4096)
                    if not data:
                        break
                    opcode = data[0] & 0x0F
                    if opcode == 0x8:  # close
                        break
                    if opcode == 0x9:  # ping -> empty pong
                        conn.sendall(ws_frame(b"", opcode=0xA))
                time.sleep(block_s)
        except (OSError, ValueError):
            pass
        finally:
            tap.unsubscribe(sid)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("request too large", 413)
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("invalid JSON: %s" % exc)
        if not isinstance(obj, dict):
            raise ApiError("body must be a JSON object")
        return obj

    # -- verbs -------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/broadcast":
                # Am I broadcasting? is a per-phone question while the transport switch
                # is global, so the answer is both. Showing the global flag on every
                # phone made one phone's decision appear on all of them.
                node = self.api.node
                who = str((parse_qs(parsed.query).get("name") or [""])[0]).strip()
                sources = [str(s).strip() for s in node.config.channel_sources if str(s).strip()]
                excluded = [str(s).strip() for s in getattr(node.config, "broadcast_excluded", [])]
                self._json(200, {
                    "broadcast": bool(node.config.broadcast),
                    "mine": bool(who) and who in sources and who not in excluded,
                    "name": who,
                    "sources": sources,
                })
                return
            if path in ("/", "/index.html"):
                self._send(200, index_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in ("/listen", "/listen.html", "/radio"):
                self._send(200, listen_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/ws/audio":
                self._websocket_audio()
                return
            if path == "/api/bluetooth":
                # parsed.query is the raw string here; the dict comes from parse_qs, as
                # every other handler does. Asking a string for a key was an
                # AttributeError, which the panel showed as 读取失败.
                refresh = str((parse_qs(parsed.query).get("refresh") or [""])[0]).lower() in (
                    "1", "true", "yes")
                if refresh:
                    # A real listing starts a PowerShell process and takes about three
                    # seconds, and this endpoint is reachable by anything on the LAN. A
                    # button press is fine, a loop is not.
                    global _LAST_FORCED_BT
                    now = time.monotonic()
                    if now - _LAST_FORCED_BT < 5.0:
                        refresh = False
                    else:
                        _LAST_FORCED_BT = now
                devices = bluetooth.list_devices_cached(force=refresh)
                self._json(200, {"available": bluetooth.available(), "devices": devices})
                return
            if path in ("/mic", "/mic.html", "/microphone"):
                self._send(200, mic_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in ("/app", "/app.html", "/phone"):
                self._send(200, app_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/app":
                apk = apk_path()
                info = apk_info()
                self._json(200, {
                    "available": apk is not None,
                    "name": apk.name if apk else "",
                    "size_mb": round(apk.stat().st_size / 1048576.0, 1) if apk else 0,
                    "size_kb": round(apk.stat().st_size / 1024.0, 1) if apk else 0,
                    # A stamp on the file itself: "did the web page give me the new build?"
                    # had no answer on screen, so it was guessed at.
                    "built_at": (datetime.fromtimestamp(apk.stat().st_mtime).strftime("%m-%d %H:%M")
                                 if apk else ""),
                    # Hash the exact bytes the download serves, and name the file, so the
                    # stamp can never describe a different build than the one you get.
                    "sha1": (info or {}).get("sha1", ""),
                    "path": (info or {}).get("path", ""),
                    "bytes": (info or {}).get("bytes", 0),
                })
                return
            if path in ("/app/download", "/app/wavelink-mic.apk"):
                self._send_apk()
                return
            if path == "/api/mic":
                from .audio.backends import netmic as netmic_backend

                # active(), not every slot: a phone that stopped keeps its slot so it
                # can come back without disturbing the reader, but it must not stay in
                # the list - a dead entry looks exactly like a broken one.
                # active() takes the module lock itself, so it must never be called
                # from inside that lock: a plain Lock is not reentrant, and the second
                # acquisition wedged this thread forever - which took the whole node
                # down with it (every other thread waits on the same lock).
                live_mics = netmic_backend.active()
                live = [m.describe() for m in live_mics]
                live_names = {m.name for m in live_mics}
                with netmic_backend._LOCK:
                    quiet = [m.describe() for m in netmic_backend.MICS.values()
                             if m.bytes_in > 0 and m.name not in live_names]
                self._json(200, {"mics": live, "stopped": quiet})
                return
            # Dead-simple text endpoints. They exist so a phone that fails to render the
            # real page can be tested with something that cannot fail on the client:
            # no JavaScript, no CSS, no relative resources.
            if path in ("/ping", "/test"):
                self._send(
                    200,
                    (
                        "WaveLink OK\n"
                        "server=%s\n"
                        "web_port=%d\n"
                        "time=%s\n"
                        "\n"
                        "If you can read this on the phone, the network path and HTTP are fine,\n"
                        "and the problem is in rendering the full page instead.\n"
                        % (
                            self.api.node.name,
                            self.api.node.config.web_port,
                            time.strftime("%Y-%m-%d %H:%M:%S"),
                        )
                    ).encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
                return
            if path == "/plain":
                # An intentionally CSS/JS-free page: proves HTML rendering works too.
                self._send(
                    200,
                    (
                        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>WaveLink 测试页</title></head><body>"
                        "<h1>WaveLink 可以访问</h1>"
                        "<p>这个页面没有任何 JavaScript 和 CSS。你能看到这段文字，说明：</p>"
                        "<ul><li>手机和电脑网络互通</li><li>HTTP 正常</li><li>页面能渲染</li></ul>"
                        "<p>那问题就在完整控制台的脚本渲染上，请把这句话告诉我。</p>"
                        "<p>节点: %s &nbsp; 端口: %d</p>"
                        "</body></html>"
                        % (self.api.node.name, self.api.node.config.web_port)
                    ).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path == "/api/status":
                self._json(200, self.api.status())
                return
            if path == "/api/peers":
                self._json(200, self.api.peers())
                return
            if path == "/api/routes":
                self._json(200, self.api.routes())
                return
            if path == "/api/devices":
                self._json(200, self.api.devices())
                return
            if path == "/api/streams":
                self._json(200, self.api.streams())
                return
            if path == "/api/logs":
                self._json(200, self.api.logs())
                return
            if path == "/api/firewall":
                # 网页里的按钮发的是 POST，但地址栏直接访问也该能用，所以两种都收。
                from .tray import add_firewall_rules

                path = add_firewall_rules()
                self._json(200, {"ok": True, "bat": path,
                                 "note": "已请求以管理员身份放行。若没看到管理员确认窗口，"
                                         "请右键运行 " + path})
                return
            if path in ("/diagnose", "/diagnose.html"):
                # 单独一页，纯文本放在 pre 里，附一个返回监控的入口。
                # 不用新窗口链接（应用窗口会把它拦掉，点了没反应），也不依赖脚本。
                data = self.api.diagnose()
                body = ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                        "<title>WaveLink 诊断</title><style>"
                        "body{margin:0;background:#0e1116;color:#e8edf5;"
                        "font:13px/1.6 'Microsoft YaHei UI',system-ui,sans-serif}"
                        "header{display:flex;align-items:center;gap:8px;padding:14px 20px}"
                        "a.tab{padding:4px 12px;border-radius:8px;background:#1d2634;"
                        "color:#e8edf5;text-decoration:none;font-size:13px}"
                        "a.tab.on{background:#2f81f7;color:#06121f;font-weight:600}"
                        "pre{margin:0 20px 24px;padding:14px;background:#171c24;"
                        "border:1px solid #242c38;border-radius:10px;white-space:pre-wrap;"
                        "word-break:break-all}</style></head><body><header>"
                        "<b>WaveLink</b>"
                        "<a class=\"tab\" href=\"/monitor\">监控</a>"
                        "<a class=\"tab\" href=\"/\">控制台</a>"
                        "<a class=\"tab on\" href=\"/diagnose\">诊断</a>"
                        "<span style=\"color:#8b97a8\">全选这段文字即可复制</span>"
                        "</header><pre>" + _html.escape(data["text"]) + "</pre></body></html>")
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/diagnose":
                data = self.api.diagnose()
                self._send(200, data["text"].encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            if path == "/api/netcheck":
                self._json(200, self.api.netcheck())
                return
            if path == "/api/monitor":
                self._json(200, self.api.monitor())
                return
            if path in ("/monitor", "/monitor.html"):
                self._send(200, monitor_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/config":
                self._json(200, {"config": self.api.node.config.to_json()})
                return
            if path == "/api/net":
                self._json(200, self.api.network())
                return
            if path == "/healthz":
                self._json(200, {"ok": True, "uptime_s": round(time.time() - self.api.node.started_at, 1)})
                return
            if path == "/api/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._json(404, {"error": "not found", "path": path})
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path == "/api/mic/stream":
                # Binary body, so it must be handled before the JSON reader consumes it.
                self._mic_stream(parsed)
                return
            body = self._read_json()
            if path == "/api/firewall":
                # 放行防火墙。这条必须放在 POST 分支里，因为页面用的是 POST；
                # 之前只写进了 GET 分支，页面点按钮只会拿到 404。
                from .tray import add_firewall_rules

                bat = add_firewall_rules()
                self._json(200, {"ok": True, "bat": bat,
                                 "note": "已请求以管理员身份放行。若没看到管理员确认窗口，"
                                         "请右键运行 " + bat})
            elif path == "/api/routes":
                self._json(200, self.api.set_routes(body))
            elif path == "/api/routes/add":
                self._json(200, self.api.add_route(body))
            elif path == "/api/routes/update":
                self._json(200, self.api.update_route(body))
            elif path == "/api/routes/delete":
                self._json(200, self.api.delete_route(body))
            elif path == "/api/peers/add":
                self._json(200, self.api.add_peer(body))
            elif path == "/api/peers/remove":
                self._json(200, self.api.remove_peer(body))
            elif path == "/api/settings":
                self._json(200, self.api.set_settings(body))
            elif path == "/api/bluetooth/connect":
                bluetooth.invalidate_cache()
                self._json(200, bluetooth.connect(str(body.get("id") or "")))
            elif path == "/api/bluetooth/disconnect":
                bluetooth.invalidate_cache()
                self._json(200, bluetooth.disconnect(str(body.get("id") or "")))
            elif path == "/api/mic/gain":
                from .audio.backends import netmic as netmic_backend

                name = str(body.get("name") or "")
                gain = float(body.get("gain") or 1.0)
                if not netmic_backend.set_gain(name, gain):
                    raise ApiError("没有这个网络音源: %s" % name, 404)
            elif path == "/api/broadcast":
                # One call for the phone's 广播 button: put this phone into the channel
                # sources, turn the broadcast on, and play it here too - so every other
                # node and every listening phone hears it. Doing it in one request avoids
                # the phone having to read the config, edit it and write it back.
                node = self.api.node
                turn_on = bool(body.get("on"))
                name = str(body.get("name") or "").strip()
                current = [str(s).strip() for s in node.config.channel_sources if str(s).strip()]
                excluded = [str(s).strip() for s in getattr(node.config, "broadcast_excluded", [])]
                stop_transport = not turn_on and not name
                if name:
                    if turn_on:
                        # Back in the broadcast, and out of the opt-out list.
                        if name not in current:
                            node.config.channel_sources = current + [name]
                        node.config.broadcast_excluded = [s for s in excluded if s != name]
                    else:
                        # The phone keeps feeding the local mix but leaves the LAN
                        # broadcast, and the opt-out sticks: auto-play must not add it
                        # back a moment later, or the button can never be turned off.
                        node.config.broadcast_excluded = excluded + ([] if name in excluded else [name])
                        try:
                            usable, _missing = node._live_channel_sources()
                        except Exception:
                            usable = [s for s in current if s not in excluded and s != name]
                        if not [s for s in usable if s not in node.config.broadcast_excluded]:
                            stop_transport = True
                if turn_on:
                    node.config.broadcast = True
                elif stop_transport:
                    node.config.broadcast = False
                if turn_on:
                    node.config.local = True
                node.config.save()
                node.wake()
                sources = [str(s).strip() for s in node.config.channel_sources if str(s).strip()]
                excluded_now = [str(s).strip() for s in getattr(node.config, "broadcast_excluded", [])]
                self._json(200, {"ok": True, "broadcast": node.config.broadcast,
                                 "mine": bool(name) and name in sources and name not in excluded_now,
                                 "sources": sources,
                                 "local": node.config.local})
            elif path == "/api/mic/mute":
                from .audio.backends import netmic as netmic_backend

                name = str(body.get("name") or "")
                muted = bool(body.get("muted"))
                if not netmic_backend.set_muted(name, muted):
                    raise ApiError("没有这个网络音源: %s" % name, 404)
                self._json(200, {"ok": True, "name": name, "muted": muted})
            elif path == "/api/ping":
                self._json(200, self.api.ping(body))
            elif path == "/api/test-tone":
                self._json(200, self.api.test_tone(body))
            else:
                self._json(404, {"error": "not found", "path": path})
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})


class WebUI:
    """Owns the HTTP server thread."""

    def __init__(self, node, handler_factory: Callable[[], None] | None = None, host: str = "0.0.0.0", port: int | None = None) -> None:
        self.node = node
        self.host = host
        self.port = port or node.config.web_port
        self.api = WaveLinkApi(node, web=self, on_routes_changed=self.broadcast_routes)
        self.httpd: ThreadingHTTPServer | None = None
        self.raw: RawAudioServer | None = None
        self.thread: threading.Thread | None = None
        self.bound_port = self.port
        self.broadcast_log: list[str] = []
        self.broadcast_at: float = 0.0

    def broadcast_routes(self, routes: list[dict[str, Any]], removed: list[str] | None = None) -> None:
        """Push the same route table to every known peer so the whole LAN agrees.

        Nodes treat this as idempotent state, which makes the system self-healing:
        the next beacon refresh is enough to converge even if a push is lost.
        `removed` names routes the receiver must drop even though it owns them -
        without it, deleting a peer's route here would be undone by its next push.
        """
        self.broadcast_at = time.time()
        gone = [str(x) for x in (removed or [])]

        def run() -> None:
            from . import proto

            for peer in self.node.registry.online(include_local=True):
                if not peer.audio_port:
                    continue
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    body = {"routes": routes, "from": self.node.node_id, "ts": time.time()}
                    if gone:
                        body["removed"] = gone
                    pkt = proto.Packet.control(proto.T_ROUTE, body)
                    sock.sendto(pkt.encode(), (peer.host, peer.audio_port))
                    sock.close()
                    self.broadcast_log.append(
                        "%s pushed %d route(s)%s to %s"
                        % (time.strftime("%H:%M:%S"), len(routes),
                           " (removed %d)" % len(gone) if gone else "", peer.name)
                    )
                except OSError as exc:
                    self.broadcast_log.append("push to %s failed: %s" % (peer.name, exc))
            del self.broadcast_log[:-50]

        threading.Thread(target=run, name="wavelink-route-push", daemon=True).start()

    def start(self) -> int:
        handler = type("BoundHandler", (_Handler,), {"api": self.api})
        for attempt in range(10):
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.port + attempt), handler)
                self.bound_port = self.port + attempt
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError("could not bind a web port starting at %d" % self.port)
        self.node.config.web_port = self.bound_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="wavelink-web", daemon=True)
        self.thread.start()
        # The phone's audio arrives on its own plain TCP port, next to the panel. One
        # connection, raw PCM, no request/response per block.
        try:
            self.raw = RawAudioServer(self.node, self.bound_port + 1)
            self.raw.start()
        except Exception as exc:
            # Never silently: a channel that fails to start must say so, or the phone
            # keeps using the slower path with nothing to explain why.
            self.raw = None
            self.node.note("音频长连接通道启动失败：%s: %s" % (type(exc).__name__, exc))
        return self.bound_port

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None


# --------------------------------------------------------------------------
# The page itself
# --------------------------------------------------------------------------

# The control panel lives in wavelink/ui/index.html so that editing the UI never
# means editing Python. It is embedded into the executable by tools/build_exe.py
# (`--add-data`); when running from a source checkout it is read from disk. The
# placeholder below is only used if the file is genuinely missing, so the server
# can still explain itself instead of returning a stack trace.
FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>WaveLink</title>
<style>body{background:#0b0e14;color:#e8eef7;font:14px/1.7 system-ui,"Microsoft YaHei";padding:40px;max-width:720px;margin:auto}
code{background:#1a202c;padding:2px 6px;border-radius:4px}a{color:#60a5fa}</style></head><body>
<h2>WaveLink 控制台页面文件缺失</h2>
<p>后端正在运行，但没找到 <code>wavelink/ui/index.html</code>。接口仍然可用：</p>
<ul>
  <li><a href="/api/status">/api/status</a> — 节点状态</li>
  <li><a href="/api/peers">/api/peers</a> — 设备列表</li>
  <li><a href="/api/routes">/api/routes</a> — 路由表</li>
  <li><a href="/api/devices">/api/devices</a> — 本机音频设备</li>
</ul>
<p>如果你是从源码运行，请确认 <code>wavelink/ui/index.html</code> 存在；<br>
如果是打包版，请重新运行 <code>python tools/build_exe.py</code>。</p>
</body></html>
"""

_INDEX_CACHE: str | None = None


def index_html() -> str:
    """Return the control panel HTML, reading it from disk once."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE

    candidates: list[Path] = []
    # Frozen (PyInstaller): --add-data drops files under sys._MEIPASS.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "wavelink" / "ui" / "index.html")
        candidates.append(Path(base) / "ui" / "index.html")
    # Source checkout / installed package.
    candidates.append(Path(__file__).resolve().parent / "ui" / "index.html")

    for path in candidates:
        try:
            if path.is_file():
                _INDEX_CACHE = path.read_text(encoding="utf-8")
                return _INDEX_CACHE
        except OSError:
            continue

    _INDEX_CACHE = FALLBACK_HTML
    return _INDEX_CACHE


def mic_html() -> str:
    """The phone-facing page that pushes its microphone in as a capture device."""
    global _MIC_CACHE
    if _MIC_CACHE is not None:
        return _MIC_CACHE

    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "wavelink" / "ui" / "mic.html")
        candidates.append(Path(base) / "ui" / "mic.html")
    candidates.append(Path(__file__).resolve().parent / "ui" / "mic.html")

    for path in candidates:
        try:
            if path.is_file():
                _MIC_CACHE = path.read_text(encoding="utf-8")
                return _MIC_CACHE
        except OSError:
            continue

    _MIC_CACHE = FALLBACK_LISTEN_HTML
    return _MIC_CACHE


def apk_path() -> Path | None:
    """Where the phone app lives, if one was built or shipped alongside the node.

    Deliberately a search rather than a fixed location: whoever builds the APK can drop
    it next to the exe, into the ui folder, or point WAVELINK_APK at it.
    """
    candidates: list[Path] = []
    env = os.environ.get("WAVELINK_APK")
    if env:
        candidates.append(Path(env))
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "wavelink" / "ui" / "wavelink-mic.apk")
    here = Path(__file__).resolve().parent
    candidates.append(here / "ui" / "wavelink-mic.apk")
    try:
        candidates.append(Path(sys.executable).resolve().parent / "wavelink-mic.apk")
    except OSError:
        pass
    candidates.append(Path.cwd() / "wavelink-mic.apk")
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


_APP_CACHE: str | None = None


_APK_INFO: dict | None = None
_APK_INFO_KEY: tuple | None = None


def apk_info() -> dict | None:
    """Path, size and sha1 of the app the download actually serves.

    One record, computed once and reused by both /api/app and /app/download: computing it
    separately in each place produced a version stamp that described a different build
    than the file being handed out, which is worse than having no stamp at all.
    """
    global _APK_INFO, _APK_INFO_KEY
    apk = apk_path()
    if apk is None:
        return None
    try:
        stat = apk.stat()
        key = (str(apk), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if _APK_INFO is not None and _APK_INFO_KEY == key:
        return _APK_INFO
    try:
        data = apk.read_bytes()
    except OSError:
        return None
    _APK_INFO = {
        "path": str(apk),
        "bytes": len(data),
        "sha1": hashlib.sha1(data).hexdigest()[:10],
        "built_at": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
    }
    _APK_INFO_KEY = key
    return _APK_INFO


def monitor_html() -> str:
    """监控窗。谁给谁、走哪个通道、多少数据、延迟多少。"""
    return """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>WaveLink 监控</title>
<style>
 :root{--bg:#0e1116;--card:#171c24;--line:#242c38;--fg:#e8edf5;--dim:#8b97a8;
       --ok:#3fb950;--warn:#d29922;--accent:#2f81f7}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 "Microsoft YaHei UI",system-ui,sans-serif}
 header{display:flex;align-items:baseline;gap:12px;padding:16px 20px 10px}
 h1{font-size:16px;margin:0}
 .dot{color:var(--ok)}
 .dim{color:var(--dim);font-size:12px}
 .tabs{display:flex;gap:6px;margin-left:6px}
 .tab{padding:4px 12px;border-radius:8px;background:#1d2634;color:var(--fg);
      text-decoration:none;font-size:13px}
 .tab.on{background:var(--accent);color:#06121f;font-weight:600}
 table{width:100%;border-collapse:collapse;margin:0 20px;width:calc(100% - 40px)}
 th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);
       font-size:13px;vertical-align:top}
 th{color:var(--dim);font-weight:600;font-size:12px}
 .chip{display:inline-block;padding:2px 8px;border-radius:6px;background:#1d2634;
       font-size:12px}
 .wifi{background:rgba(47,129,247,.18);color:#8ec1ff}
 .bt{background:rgba(63,185,80,.16);color:#7ee787}
 .p2p{background:rgba(210,153,34,.16);color:#e3b341}
 .num{font-family:Consolas,monospace}
 .empty{padding:30px 20px;color:var(--dim)}
 footer{padding:14px 20px;color:var(--dim);font-size:12px}
</style></head>
<body>
<header>
  <h1><span class="dot">●</span> WaveLink</h1>
  <nav class="tabs">
    <a class="tab on" href="/monitor">监控</a>
    <a class="tab" href="/">控制台</a>
  </nav>
  <span class="dim" id="who"></span>
  <a id="dgLink" href="/diagnose"
     title="把当前状态整理成一份文字报告，在同一窗口打开，全选即可复制"
     style="padding:4px 12px;border-radius:8px;background:#1d2634;color:#e8edf5;
            text-decoration:none">诊断</a>
  <button id="fw" title="点对点或广播不通时，多半是本机防火墙挡了入站。点这里放行。"
          style="margin-left:auto;padding:4px 12px;border-radius:8px;border:0;
                 background:#1d2634;color:#e8edf5;cursor:pointer">放行防火墙</button>
  <span class="dim" id="when"></span>
</header>
<table>
  <thead><tr><th>从谁</th><th>给谁</th><th>通道</th><th>状态</th>
  <th>数据量</th><th>速率</th><th>延迟</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<div class="empty" id="empty" style="display:none">现在没有任何数据在流动</div>
<section id="checks" style="margin:16px 20px 0;padding:14px 16px;background:var(--card);
        border:1px solid var(--line);border-radius:10px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
    <b style="font-size:13px">网络自检</b>
    <button id="runChk" style="padding:3px 10px;border-radius:6px;border:0;
            background:#1d2634;color:#e8edf5;cursor:pointer">检查</button>
    <span class="dim" id="chkNote">连不通的时候点一下，看是哪一项没过</span>
  </div>
  <div class="dim" style="font-size:12px;margin-bottom:6px">
    规则：<b>广播</b>要对方打开「只听」才会播放，它不依赖路由；
    <b>点对点</b>只要路由在就播放，不需要对方配合。
    </div>
  <div style="display:none">
  </div>
  <div id="chkRows" class="dim" style="font-size:12px">还没有检查</div>
</section>
<footer id="foot"></footer>
<script>
function fmtBytes(n){
  if(n === null || n === undefined) return '不计量';
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if(n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}
function cls(ch){ return ch === '蓝牙' ? 'bt' : (ch === '点对点' ? 'p2p' : 'wifi'); }
async function tick(){
  try{
    const d = await (await fetch('/api/monitor', {cache:'no-store'})).json();
    document.getElementById('who').textContent = d.node || '';
    document.getElementById('when').textContent = '更新于 ' + new Date().toLocaleTimeString();
    const rows = d.flows || [];
    document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
    document.getElementById('rows').innerHTML = rows.map(f => `
      <tr>
        <td>${f.from || ''}</td>
        <td>${f.to || ''}</td>
        <td><span class="chip ${cls(f.channel)}">${f.channel || ''}</span></td>
        <td>${f.state || ''}${f.detail ? '<div class="dim">' + f.detail + '</div>' : ''}</td>
        <td class="num">${fmtBytes(f.bytes)}</td>
        <td class="num">${f.rate_kbps == null ? '-' : f.rate_kbps + ' KB/s'}</td>
        <td class="num">${f.latency_ms == null ? '-' : f.latency_ms + ' ms'}</td>
      </tr>`).join('');
    const peak = d.local_peak;
    document.getElementById('foot').textContent =
      '本机输出电平 ' + (peak == null ? '-' : Math.round(peak*100) + '%') +
      '　已运行 ' + Math.round((d.uptime_s||0)/60) + ' 分钟' +
      '　数据每秒刷新';
  }catch(e){
    document.getElementById('empty').textContent = '读不到状态：' + e;
    document.getElementById('empty').style.display = 'block';
  }
}
tick(); setInterval(tick, 1000);
document.getElementById('dgLink').addEventListener('click', () => {});
function on(id, event, handler){
  // 每个监听单独包起来。以前一个元素出问题会让后面所有监听都不生效，
  // 表现就是"按钮点了没反应"，还查不出原因。
  const el = document.getElementById(id);
  if (!el) { console.error('找不到元素 ' + id); return; }
  el.addEventListener(event, async (...args) => {
    try { await handler(...args); }
    catch (e) { alert('操作失败：' + e); }
  });
}
window.addEventListener('error', e => {
  // 脚本出错要看得见，否则页面只是"点了没反应"。
  const box = document.getElementById('chkNote');
  if (box) box.textContent = '页面脚本出错：' + e.message;
});
on('runChk', 'click', async () => {
  const box = document.getElementById('chkRows');
  box.textContent = '检查中…';
  try{
    const d = await (await fetch('/api/netcheck', {cache:'no-store'})).json();
    box.innerHTML = (d.checks || []).map(c => {
      const mark = c.ok === null ? '·' : (c.ok ? '✓' : '✗');
      const color = c.ok === null ? '#8b97a8' : (c.ok ? '#3fb950' : '#f85149');
      return '<div style="margin:4px 0"><span style="color:' + color + '">' + mark +
             '</span> <b>' + c.name + '</b>　' + (c.detail || '') + '</div>';
    }).join('');
    document.getElementById('chkNote').textContent = d.ok ? '全部通过' : '有项目没过，见下面';
  }catch(e){ box.textContent = '检查失败：' + e; }
});
on('fw', 'click', async () => {
  try{
    await fetch('/api/firewall', {method:'POST'});
    alert('已弹出管理员确认窗口，点"是"就会放行。两台电脑都要放行。');
  }catch(e){ alert('调用失败：' + e); }
});
</script>
</body></html>"""


def app_html() -> str:
    """The phone-facing install page for the Android app."""
    global _APP_CACHE
    if _APP_CACHE is not None:
        return _APP_CACHE

    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "wavelink" / "ui" / "app.html")
        candidates.append(Path(base) / "ui" / "app.html")
    candidates.append(Path(__file__).resolve().parent / "ui" / "app.html")

    for path in candidates:
        try:
            if path.is_file():
                _APP_CACHE = path.read_text(encoding="utf-8")
                return _APP_CACHE
        except OSError:
            continue

    _APP_CACHE = FALLBACK_LISTEN_HTML
    return _APP_CACHE


_LISTEN_CACHE: str | None = None
_MIC_CACHE: str | None = None

FALLBACK_LISTEN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WaveLink 收音机</title></head>
<body style="background:#0e1116;color:#e8edf5;font:15px system-ui;padding:24px">
<h1>WaveLink 收音机</h1>
<p>这个页面需要 WebSocket 支持。请用手机上的 Chrome / Safari 打开本机地址的 <code>/listen</code>，
例如 <code>http://192.168.x.x:17800/listen</code>。</p>
</body></html>"""


def listen_html() -> str:
    """The phone-facing page: a one-tap "tune in" player for this node's output."""
    global _LISTEN_CACHE
    if _LISTEN_CACHE is not None:
        return _LISTEN_CACHE

    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "wavelink" / "ui" / "listen.html")
        candidates.append(Path(base) / "ui" / "listen.html")
    candidates.append(Path(__file__).resolve().parent / "ui" / "listen.html")

    for path in candidates:
        try:
            if path.is_file():
                _LISTEN_CACHE = path.read_text(encoding="utf-8")
                return _LISTEN_CACHE
        except OSError:
            continue

    _LISTEN_CACHE = FALLBACK_LISTEN_HTML
    return _LISTEN_CACHE


# Kept as a module attribute for backwards compatibility with anything importing it.
INDEX_HTML = FALLBACK_HTML
