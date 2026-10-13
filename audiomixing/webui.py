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
    # HWHHYN 用 DEEPSEEK 制作
    def __init__(self, message: str, status: int = 400, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        # 附带的机器可读信息（比如"哪些名字被拒了"）。页面据此能给出"一键移除"，
        # 而不是只把一句话丢给用户。旧调用方不传，等于空字典。
        self.data: dict[str, Any] = dict(data or {})


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
        # 版本号 + 构建时间必须每次刷新都带上：用户上次就是拿一个 7 小时前的旧 exe
        # 测了一整天，而面板上没有任何地方能让他发现这件事。
        data["build"] = self.build_info()
        web = getattr(self, "web", None)
        raw = getattr(web, "raw", None) if web is not None else None
        data["raw_audio"] = raw.stats() if raw is not None else {
            "port": 0, "error": "未启动", "clients": 0, "bytes_in": 0}
        return data

    # -- 版本可见性 --------------------------------------------------------
    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...]:
        """"1.9" / "v1.9" / "Audio Mixing v1.9.exe" → (1, 9)。解析不出来就是空元组。"""
        import re

        found = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
        if not found:
            return ()
        try:
            return tuple(int(x) for x in found.group(1).split("."))
        except ValueError:
            return ()

    @staticmethod
    def _is_frozen() -> bool:
        """现在是打包后的 exe 还是源码在跑（Nuitka 不设 sys.frozen，所以两个都看）。"""
        return bool(globals().get("__compiled__")) or bool(getattr(sys, "frozen", False))

    def build_info(self) -> dict[str, Any]:
        """当前跑的是哪个版本、什么时候编出来的、旁边有没有更新的版本。

        用户的真实现场：桌面上摆着 v1.2/v1.3/v1.4 三个 exe，他双击的是 v1.2
        （比当时的代码早 7 个小时），于是所有修复都不在他测的那个程序里 ——
        而面板上没有任何地方写着版本。这条必须显眼。
        """
        from . import __version__

        frozen = self._is_frozen()
        if frozen:
            path = Path(sys.executable)
            channel = "exe"
        else:
            path = Path(__file__).resolve().parent / "__init__.py"
            channel = "源码"
        build_time = ""
        try:
            build_time = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            pass
        running = self._version_tuple(__version__)

        # 旁边有没有更新的版本：桌面 + 当前程序所在目录。
        # 只认 "Audio Mixing v<数字>.exe" 这个命名（交付规则就是这么命名的）。
        import re

        newer: dict[str, Any] | None = None
        dirs: list[Path] = []
        try:
            dirs.append(path.resolve().parent)
        except OSError:
            pass
        desktop = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), "Desktop")
        if os.path.isdir(desktop):
            dirs.append(Path(desktop))
        pattern = re.compile(r"Audio\s*Mixing\s*v(\d+(?:\.\d+)*)\.exe$", re.IGNORECASE)
        seen: set[str] = set()
        for folder in dirs:
            try:
                entries = list(folder.glob("*.exe"))
            except OSError:
                continue
            for item in entries:
                found = pattern.search(item.name)
                if not found:
                    continue
                version = self._version_tuple(found.group(1))
                if version <= running:
                    continue
                try:
                    if frozen and item.resolve() == path.resolve():
                        continue           # 就是现在跑的这个，别自己提醒自己
                except OSError:
                    pass
                key = str(item).lower()
                if key in seen:
                    continue
                seen.add(key)
                if newer is None or version > self._version_tuple(newer["version"]):
                    newer = {
                        "name": item.name,
                        "version": found.group(1),
                        "path": str(item),
                        "size_mb": round(item.stat().st_size / 1048576.0, 1),
                        "built_at": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    }
        text = "Audio Mixing v%s · %s · 构建于 %s" % (__version__, channel, build_time or "未知")
        return {
            "version": __version__,
            "channel": channel,
            "build_time": build_time,
            "path": str(path),
            "text": text,
            "newer": newer,
        }

    def guide(self) -> dict[str, Any]:
        """「🚀 快速上手」四步自检：每步给结论 + 一键修。

        用户的五个真实失败没有一个是代码算错，全是"没人告诉你怎么配"。
        所以这里不引入任何新判定：结论全部复用引擎里已有的口径
        （capture_state / channel_sources_report / peers / test-tone）。
        """
        node = self.node
        st = self.status()
        channel = st.get("channel") or {}
        cap = node.capture_state()
        local = st.get("local") or {}
        steps: list[dict[str, Any]] = []

        # ---- 第 1 步：采集源到底有没有声音？ ----
        live = [s for s in cap["sources"] if s["state"] == "ok"]
        stalled = cap["stalled"]
        candidates = cap["candidates"]
        actions: list[dict[str, Any]] = []
        if live:
            worst_age = max((s.get("last_data_age_s") or 0.0) for s in live)
            text = "正在采集：%s —— %.1f 秒前还有数据 ✓" % (
                "、".join(s["name"] for s in live), worst_age)
            detail = ""
            # 有数据 ≠ 有声音：真声卡的环回在没人放声音时也会一直给静音块。
            # 所以再报一句电平，用户才知道"现在到底有没有声音在走这条路"。
            playing = bool(local.get("enabled")) and not local.get("error") \
                and int(local.get("frames") or 0) > 0
            if playing and not float(local.get("peak") or 0.0):
                detail = ("不过电平一直是 0 —— 如果你的游戏/音乐正在放，说明声音走的是另一块设备，"
                          "把系统的输出设备也切到这块，或在 ⑥ 的「音源」里改选声音真正在走的那一块。")
            steps.append({"id": "capture", "n": 1, "title": "采集源到底有没有声音？",
                          "state": "ok", "text": text, "detail": detail, "actions": []})
        elif stalled:
            worst = max(stalled, key=lambda s: float(s.get("last_data_age_s") or 999.0))
            text = ("采不到声音：「%s」%s。" % (worst["name"], worst.get("reason") or "现在没有数据")
                    + "虚拟声卡空转时就是采不到 —— 换成一块真有声音的设备。")
            actions.append({"label": "🔧 一键换到有声音的设备", "api": "/api/guide/fix",
                            "payload": {"action": "fix_broadcast"}, "kind": "primary"})
            for cand in candidates[:6]:
                alive = cand.get("alive")
                mark = ("有数据 ✓" if alive is True else
                        ("没数据 ✗" if alive is False else "未验证"))
                actions.append({"label": "换成 %s（%s）" % (cand["device"], mark),
                                "api": "/api/guide/fix",
                                "payload": {"action": "switch_capture", "device": cand["device"]},
                                "kind": "ghost"})
            if any(c.get("alive") is None for c in candidates):
                actions.insert(1, {"label": "先验一下候选设备（每块 2 秒）",
                                   "api": "/api/guide/fix",
                                   "payload": {"action": "probe_all"}, "kind": "ghost"})
            steps.append({"id": "capture", "n": 1, "title": "采集源到底有没有声音？",
                          "state": "bad", "text": text,
                          "detail": ("试采候选设备要 2 秒左右；标「未验证」的可以先换过去，"
                                     "换完这一步会自己重新判定。"),
                          "actions": actions})
        else:
            # 没在采集：分开说"还没打开"和"开了但设备还没起来"，否则用户会以为是自己没开。
            wanted = []
            if channel.get("broadcast"):
                wanted.append("广播")
            if local.get("enabled"):
                wanted.append("本机直通")
            if wanted:
                sw = cap.get("switch") or {}
                if sw and float(sw.get("age_s") or 9999) < 20:
                    # 刚刚自动换过设备：这时候"还没起来"是正常的，直接说清发生了什么。
                    text = ("刚刚自动把音源从「%s」换成了「%s」（%s）—— 正在重新开始采集，"
                            "两秒后点「重新检查」看结果。" % (
                                sw.get("from") or "?", sw.get("to") or "?", sw.get("why") or ""))
                    detail = "这次切换已经写进配置，下次启动不会再走那块坏设备。"
                else:
                    text = ("「%s」已经开着，但采集设备还没起来 —— 等两秒再点「重新检查」；"
                            "如果一直是这样，看第 2 步的音源是不是配错了。" % "、".join(wanted))
                    detail = ""
            else:
                text = "现在没有在采集任何设备（「广播我的声音」和「本机直通」都没开）。"
                detail = "开了之后这一台电脑的声音才有东西可发。"
            steps.append({"id": "capture", "n": 1, "title": "采集源到底有没有声音？",
                          "state": "warn", "text": text, "detail": detail,
                          "actions": [{"label": "打开广播并开始采集", "api": "/api/guide/fix",
                                       "payload": {"action": "fix_broadcast"}, "kind": "primary"}]})

        # ---- 第 2 步：声音发得出去吗？ ----
        name = str((st.get("node") or {}).get("name") or "本机")
        warnings = [w for w in (channel.get("warnings") or []) if w.get("scope") != "capture"]
        if not channel.get("broadcast"):
            steps.append({"id": "send", "n": 2, "title": "声音发得出去吗？", "state": "bad",
                          "text": "「广播我的声音」没开 —— 这台电脑的声音不会发给任何人。",
                          "detail": "", "actions": [{"label": "一键打开广播", "api": "/api/guide/fix",
                                                     "payload": {"action": "fix_broadcast"},
                                                     "kind": "primary"}]})
        elif channel.get("will_send"):
            who = "、".join(channel.get("live") or []) or "?"
            if int(channel.get("receivers") or 0):
                steps.append({"id": "send", "n": 2, "title": "声音发得出去吗？", "state": "ok",
                              "text": "广播正在发出声音（采的是「%s」），局域网里有 %d 台设备能收到 ✓"
                                      % (who, int(channel.get("receivers") or 0)),
                              "detail": "", "actions": []})
            else:
                steps.append({"id": "send", "n": 2, "title": "声音发得出去吗？", "state": "warn",
                              "text": "广播正在发出声音（采的是「%s」），但局域网上现在 0 台设备在线 —— "
                                      "还没有人听得到。" % who,
                              "detail": "声音这一半已经好了；下一步解决「谁来收」：两台设备要互相看得见（第 3 步）。",
                              "actions": []})
        elif not int(channel.get("receivers") or 0) and channel.get("live"):
            steps.append({"id": "send", "n": 2, "title": "声音发得出去吗？", "state": "bad",
                          "text": "就算有音源也没有可发的对象：局域网上现在 0 台设备在线。",
                          "detail": "先看第 3 步 —— 两台设备要互相看得见才谈得上发声音。",
                          "actions": []})
        else:
            # 注意优先级：没有音源比"没有听众"更根本，先说这一条。
            # 原因只取"音源本身"的那些（接收端 0 台是另一件事，放在补充说明里）。
            broken = [e for e in (channel.get("sources") or [])
                      if not e.get("is_local") or e.get("excluded")
                      or e.get("state") in ("stalled", "no_data")]
            if broken:
                reasons = "；".join("「%s」%s" % (e.get("name"), e.get("reason") or "现在不可用")
                                   for e in broken)
            elif not (channel.get("live") or []):
                reasons = "现在配置的音源一块都没在出声音（也可能是采集设备还没起来）"
            else:
                reasons = "现在配置的音源一个都用不了"
            detail = "点下面的按钮：清掉用不了的音源、换到本机默认输出、并打开广播。"
            if not int(channel.get("receivers") or 0):
                detail += "另外，局域网上现在 0 台设备在线 —— 修好音源之后还需要有人能收到（看第 3 步）。"
            steps.append({"id": "send", "n": 2, "title": "声音发得出去吗？", "state": "bad",
                          "text": "广播没有音源，一个字节都不会发。" + reasons,
                          "detail": detail,
                          "actions": [{"label": "🔧 一键修广播", "api": "/api/guide/fix",
                                       "payload": {"action": "fix_broadcast"}, "kind": "primary"}]})

        # ---- 第 3 步：局域网里能看到别的设备吗？ ----
        peers = [p for p in (st.get("peers") or [])
                 if p.get("online") and not p.get("local") and p.get("node_id") != (st.get("node") or {}).get("id")]
        if peers:
            steps.append({"id": "lan", "n": 3, "title": "局域网里能看到别的设备吗？", "state": "ok",
                          "text": "看到 %d 台：%s ✓" % (
                              len(peers),
                              "、".join("%s（%s）" % (p.get("name"), p.get("host")) for p in peers[:6])),
                          "detail": "", "actions": []})
        else:
            steps.append({
                "id": "lan", "n": 3, "title": "局域网里能看到别的设备吗？", "state": "bad",
                "text": "一台都没看到 —— 两台设备互相看不见时，声音一定发不过去。",
                "detail": ("按可能性从高到低排查：① 两台设备不在同一个 Wi-Fi（手机连了 4G/5G 或访客网络）；"
                           "② 路由器开了「AP 隔离 / 客户端隔离」；③ 电脑防火墙拦了入站 UDP（发现广播收不到）；"
                           "④ 手机上的 App 没打开（只打开网页的手机不是一个节点，永远不会出现在这里）；"
                           "⑤ 这台电脑有多个网卡/VPN，发现广播走错了网卡。"),
                "actions": [
                    {"label": "🧱 本机防火墙自检（约 5 秒）", "api": "/api/netcheck", "kind": "ghost"},
                    {"label": "以管理员放行防火墙", "api": "/api/firewall", "kind": "ghost"},
                ]})

        # ---- 第 4 步：放一段测试音，让人耳确认 ----
        # 优先放到"广播正在采、而且确实有数据"的那块设备上 —— 只有放给它，
        # 走 UDP 广播的手机才听得到。那块设备采不到声音时退回到本机默认输出：
        # 至少用户自己的耳朵和网页听众（tap）能听到，能确认播放这一半是好的。
        tone_device = ""
        for item in (channel.get("live") or []):
            try:
                if node._selector_is_loopback(item):
                    tone_device = item
                    break
            except Exception:
                continue
        payload: dict[str, Any] = {"seconds": 2, "freq": 440}
        if tone_device:
            payload["device"] = tone_device
        where = ("从「%s」采出去" % tone_device) if tone_device else "走本机默认输出"
        url = self.phone_listen_url()
        steps.append({
            "id": "tone", "n": 4, "title": "放一段测试音，让人耳确认", "state": "manual",
            "text": ("点下面的按钮：这台电脑会响 2 秒 440Hz 蜂鸣（%s），同时把它送给正在收听的人。" % where),
            "detail": ("手机上点「📻 听广播」，或者手机浏览器打开 %s 也能听到（不用装 App）—— "
                       "听到了说明整条链路是通的 ✓。没听到：先看手机端的「🩺 诊断」，"
                       "再回来确认第 1 步（电脑这边到底采到声音没有）。" % (url or "这个页面的地址 + /listen")),
            "actions": [{"label": "🔔 播放测试音（2 秒）", "api": "/api/test-tone",
                         "payload": payload, "kind": "primary"}]})

        # 前 3 步全绿才算"可以用了"；第 4 步是让人耳确认，不参与自动判定。
        problems = [s for s in steps[:3] if s["state"] != "ok"]
        ready = not problems
        if ready:
            summary = ("可以用了 ✓ 手机上点「📻 听广播」，或点「🎧 收听 %s」。" % name)
        else:
            summary = "还有 %d 步没过 —— 按上面的按钮修一下，再点「重新检查」。" % len(problems)
        return {
            "steps": steps,
            "summary": summary,
            "ready": ready,
            "build": st.get("build") or {},
            "capture": cap,
            "channel": {k: channel.get(k) for k in
                        ("broadcast", "will_send", "live", "effective", "receivers", "missing")},
        }

    def phone_listen_url(self) -> str:
        """手机浏览器能直接打开、用来听声音的地址（用发现层挑出来的第一块网卡）。"""
        try:
            report = disc.lan_report()
            primary = (report.get("interfaces") or [{}])[0]
            host = str(primary.get("address") or "")
        except Exception:
            host = ""
        if not host:
            return ""
        return "http://%s:%s/listen" % (host, self.node.config.web_port)

    def guide_fix(self, payload: dict[str, Any]) -> dict[str, Any]:
        """向导里的"一键修"。只做用户点的那一件事，做完把改了什么原样回报。"""
        action = str(payload.get("action") or "")
        node = self.node
        if action == "switch_capture":
            device = str(payload.get("device") or "").strip()
            if not device:
                raise ApiError("没有指定要换成哪一块设备")
            result = node.switch_capture(device, why="在「快速上手」里点的「换成这块设备」")
            note = "已把音源换成「%s」。" % device
            if result.get("switch", {}).get("from"):
                note = "已从「%s」换成「%s」。" % (result["switch"]["from"], device)
            return {"ok": True, "note": note, "switch": result.get("switch"),
                    "sources": result.get("sources")}
        if action == "probe":
            device = str(payload.get("device") or "").strip()
            if not device:
                raise ApiError("没有指定要试采哪一块设备")
            started = node.probe_capture_async(device)
            return {"ok": True, "started": bool(started),
                    "note": ("正在试采「%s」，约 2 秒后看结果。" % device) if started
                            else "已经有一块设备在试采了，等一下再看。"}
        if action == "probe_all":
            result = node.probe_candidates_async()
            return {"ok": True, "started": bool(result.get("started")),
                    "devices": result.get("devices") or [],
                    "note": result.get("note") or ""}
        if action == "fix_broadcast":
            result = node.auto_fix_channel()
            return {"ok": True, "note": result.get("note") or "已修好",
                    "changed": result.get("changed"), "picked": result.get("picked"),
                    "dropped": result.get("dropped")}
        raise ApiError("不认识的修复动作: %s" % action)

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
        howto = ("右键右下角托盘里的 Audio Mixing 图标，点「放行防火墙（需管理员）」，"
                 "在弹窗里点「是」，然后重新检查")
        add("入站 TCP 放行", tcp,
            "；".join(lines) if tcp else
            "没有任何和本程序有关的入站规则，别人连不进来。" + howto)
        add("入站 UDP 放行", udp, "已放行" if udp else
            "没放行 UDP，收不到对方的发现广播，对端不知道你在哪。" + howto)

        # 正在跑的 exe 路径：规则是绑路径的，换了目录旧规则就失效
        exe = ps("(Get-Process -Name AudioMixing -ErrorAction SilentlyContinue | "
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

        lines.append("Audio Mixing 诊断报告")
        lines.append("生成时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            import platform

            lines.append("系统 %s" % platform.platform())
        except Exception:
            pass
        lines.append("程序 %s" % (sys.executable if getattr(sys, "frozen", False) else "源码运行"))
        # 版本必须在报告里：用户上次拿一个 7 小时前的旧 exe 测了一整天，
        # 而报告里没有任何一处写着"这是哪个版本"。
        try:
            build = self.build_info()
            lines.append("版本 %s" % build.get("text"))
            if build.get("newer"):
                lines.append("★ 发现更新的版本：%s（%s 构建）—— 现在跑的是 v%s，"
                             "请关掉现在这个，双击新的那个" % (
                                 build["newer"]["name"], build["newer"]["built_at"],
                                 build.get("version")))
        except Exception as exc:
            lines.append("版本读取失败: %s: %s" % (type(exc).__name__, exc))

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

        head("频道广播音源判定（发不发得出去，为什么）")
        # "广播开着、上行 0 kbps"以前在报告里看不出原因：音源可能是另一台设备的名字、
        # 可能被排除、也可能采着但一直没有数据。这里直接把结论写出来。
        try:
            report = node.channel_sources_report()
        except Exception as exc:
            report = {}
            lines.append("判定失败：%s: %s" % (type(exc).__name__, exc))
        if report:
            lines.append("音源清单：%s" % ("、".join(report.get("configured") or []) or "（空）"))
            lines.append("广播实际采的设备：%s" % ("、".join(report.get("effective") or []) or "（没有）"))
            lines.append("其中现在真的在出音频的：%s" % ("、".join(report.get("live") or []) or "（没有）"))
            lines.append("能收到这条广播的在线设备：%s 台" % report.get("receivers"))
            lines.append("结论：%s" % ("会发出音频" if report.get("will_send")
                                      else "不会发出音频（0 字节）"))
            for item in report.get("sources") or []:
                lines.append("· %s　状态=%s%s　%s" % (
                    item.get("name"), item.get("state"),
                    "　[已排除]" if item.get("excluded") else "",
                    item.get("reason") or "正常"))
            for warn in report.get("warnings") or []:
                lines.append("[%s] %s" % (warn.get("level"), warn.get("text")))
                if warn.get("advice"):
                    lines.append("      建议：%s" % warn.get("advice"))

        head("快速上手自检（面板上「🚀 快速上手」那四步的结论）")
        try:
            for step in (self.guide().get("steps") or []):
                lines.append("第 %d 步 %s → %s" % (step.get("n"), step.get("title"), step.get("text")))
                if step.get("detail"):
                    lines.append("      %s" % step["detail"])
        except Exception as exc:
            lines.append("自检失败: %s: %s" % (type(exc).__name__, exc))

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

        head("音源状态（每块采集源有没有数据）")
        # 只报事实：这块源收到过多少、最后一次收到是多久以前。
        # 空的那块会显示"没有音频数据"，用户自己就能看出该换成正在用的那一块。
        # 判断依据与设备类型无关（蓝牙/有线/USB/虚拟声卡一视同仁）。
        sources = st.get("sources") or []
        if not sources:
            lines.append("当前没有任何采集源（程序没在采集）")
        for item in sources:
            spec = item.get("spec") or {}
            age = item.get("last_data_age_s")
            if item.get("blocks_read"):
                state = "有数据 ✓　最后一块 %.1f 秒前" % (age or 0.0)
            else:
                state = "没有音频数据 ✗　这块设备当前没有在出声（或没在推流、被独占、已断开）"
            lines.append("%s　[%s]　%s" % (
                item.get("name") or "?",
                str(spec.get("backend") or "") + ("/loopback" if spec.get("loopback") else ""),
                state))
            lines.append("    已收到 %s 块 / %s 帧　运行 %.0f 秒　错误=%s" % (
                item.get("blocks_read"), item.get("frames_read"),
                float(item.get("alive_for_s") or 0), item.get("error") or "无"))

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
                                "Audio Mixing")
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
                "to": "本机",
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
                    "to": "本机",
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
            host = str(info.get("host") or "")
            port = info.get("port")
            if not target and host:
                # "给谁"要写对端的节点名，不是 IP:端口 ——
                # 端口是内部细节，写出来只会让人以为是故障。
                for peer in (getattr(node, "registry", None).snapshot()
                             if getattr(node, "registry", None) is not None else []) or []:
                    if str(peer.get("host") or "") == host and peer.get("name"):
                        target = str(peer.get("name"))
                        break
            if not target:
                target = host or rid
            endpoint = (host + (":" + str(port) if port else "")) if host else ""
            amount = int(info.get("bytes") or info.get("sent_bytes") or 0)
            rate = round(float(info.get("kbps") or 0.0), 1)
            device = str(info.get("source") or "")
            # "从谁"这一列写的是**谁在发**，也就是这台机器的节点名（配置里那个名字），
            # 不是采集设备的名字。设备名放进说明里，免得让人以为声音来自耳机。
            node_name = str(getattr(node, "name", "") or "本机")
            source_label = node_name
            where = ("采自 " + device) if device else ""
            # ★ 会话键是 "<路由id>|<目标节点id>"，判断"残留"必须只取前半段。
            # 以前拿整个键去和路由表里的 id 比，永远匹配不上 —— 于是【正在正常发送】
            # 的行也被标成"残留"，用户看到的就是"明明在响却写着残留"（2026-10-12 定位）。
            # _sweep_dead_senders 里取 id 用的就是 split("|")[0]，这里保持一致。
            route_key = str(rid).split("|", 1)[0]
            stale = bool(live_ids) and not is_channel and route_key not in live_ids
            if is_channel:
                flows.append({
                    "disconnect": "broadcast",
                    "hint": "停止广播",
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
                "disconnect": "route",
                "route_id": str(rid).split("|", 1)[0],
                "hint": "静音这条路由",
                "from": source_label,
                "to": target,
                "channel": "点对点",
                "state": "残留（这条路由已经删了，但它还在发）" if stale else "正在发送",
                "bytes": amount,
                "rate_kbps": rate,
                "latency_ms": None,
                "detail": ("残留：这条路由已经删了但它还在发　对端 " + endpoint if stale
                           else ("　".join(x for x in (where, ("对端 " + endpoint) if endpoint else "")
                                           if x) or str(info.get("error") or ""))),
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
            # 缓冲细节：音质出问题时，能一眼看出是不是在反复裁剪/欠载。
            bits = []
            if stream.get("depth_ms") is not None:
                bits.append("缓冲 %s 毫秒" % stream.get("depth_ms"))
            if stream.get("target_ms") is not None:
                bits.append("目标 %s 毫秒" % stream.get("target_ms"))
            for label, field in (("裁剪", "trimmed"), ("欠载", "underruns"),
                                 ("迟到", "late"), ("溢出", "overflow"),
                                 ("重同步", "resyncs")):
                value = stream.get(field)
                if value:
                    bits.append("%s %s" % (label, value))
            flows.append({
                "from": str(stream.get("node_name") or stream.get("source") or "其它电脑"),
                "to": "本机",
                # 带上 route_id：这样"已配置但当前没有数据"那一行才能认出这条路由
                # 其实正在收数据，不会再多显示一行（2026-10-12 修）。
                "route_id": str(stream.get("route_id") or ""),
                # 收到的流可能来自广播通道，也可能来自点对点路由；协议里不带这个信息，
                # 所以不猜：写清楚两种都可能，要看本机是否开着「只听」（只听只影响广播）。
                "channel": "点对点/广播",
                "state": "正在接收",
                "bytes": int(stream.get("bytes") or 0),
                "rate_kbps": round(float(stream.get("kbps") or 0.0), 1),
                "latency_ms": round(float(stream.get("depth_ms") or 0.0), 1),
                "detail": "　".join(bits) or "丢包 %s 迟到 %s" % (
                    stream.get("lost", 0), stream.get("late", 0)),
            })

        try:
            clocks = (node.receiver.stats() or {}).get("clocks") or []
        except Exception:
            clocks = []
        for item in clocks:
            if not isinstance(item, dict):
                continue
            flows.append({
                "from": str(item.get("peer") or "")[:8] or "对端",
                "to": "时钟对齐",
                "channel": "时钟",
                "state": "已对齐",
                "bytes": None,
                "rate_kbps": None,
                "latency_ms": None,
                "detail": "墙钟差 %s 毫秒（已在对齐里扣除）　往返 %s 毫秒　样本 %s" % (
                    item.get("offset_ms"), item.get("rtt_ms"), item.get("samples")),
            })

        # 已配置、但当前没有数据在流的路由，也要显示出来。
        # 不然"对端没在发"的时候监控页一片空白，用户会以为路由丢了 ——
        # 实际上路由还在，只是没有数据（2026-10-11 用户反馈）。
        try:
            shown = {str(f.get("route_id") or "") for f in flows if f.get("route_id")}

            def _name_of(node_id: str) -> str:
                text = str(node_id or "")
                if not text:
                    return "?"
                try:
                    peer = node.registry.get(text)
                    if peer is not None and peer.name:
                        return str(peer.name)
                except Exception:
                    pass
                if text == str(getattr(node, "node_id", "")):
                    return str(getattr(node, "name", "") or "本机")
                return text[:8]

            for rule in (node.routes or []):
                rid = str(rule.get("id") or "")
                if not rid or rid in shown:
                    continue
                src_ids = rule.get("from") or rule.get("source") or []
                dst_ids = rule.get("to") or rule.get("target") or []
                if isinstance(src_ids, str):
                    src_ids = [src_ids]
                if isinstance(dst_ids, str):
                    dst_ids = [dst_ids]
                flows.append({
                    "route_id": rid,
                    "from": "、".join(_name_of(x) for x in src_ids) or "?",
                    "to": "、".join(_name_of(x) for x in dst_ids) or "?",
                    "channel": "点对点",
                    "state": "已配置 · 当前没有数据",
                    "bytes": 0,
                    "rate_kbps": 0.0,
                    "latency_ms": None,
                    "detail": "这条路由已经建好了，但现在没有数据在流（对端没在发，或还没开始）",
                })
        except Exception:
            pass

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

# HWHHYN 用 DEEPSEEK 制作

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
        if "gain" in payload:
            # 本机用户刚动了这条路由的音量：记下时刻。共享增益判"谁最后动"要用它，
            # 否则对端一个在途的旧值会把用户刚拖到的位置抢回去。
            self.node.note_local_gain_change(route_id)
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
                "%s 不是局域网地址。Audio Mixing 只在同一局域网内工作（不做穿透/中继）。" % host,
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
                value = bool(payload[key])
                setattr(node.config, key, value)
                changed.append(key)
                # 广播与只听互斥：要么说，要么听。这是功能层的规则，
                # 放在这里做，引擎的发送逻辑保持原样，正常的双向点对点不受影响。
                if value and key == "broadcast" and node.config.listen:
                    node.config.listen = False
                    changed.append("listen")
                elif value and key == "listen" and node.config.broadcast:
                    node.config.broadcast = False
                    changed.append("broadcast")
        if "open_panel_on_start" in payload:
            want = str(payload["open_panel_on_start"] or "none").strip().lower()
            if want not in ("none", "simple", "full"):
                raise ApiError("open_panel_on_start 只能是 none、simple 或 full", 400)
            node.config.open_panel_on_start = want
            changed.append("open_panel_on_start")
        if "auto_accept_listen" in payload:
            node.config.auto_accept_listen = bool(payload["auto_accept_listen"])
            changed.append("auto_accept_listen")
        if "listen_optout" in payload:
            raw = payload["listen_optout"]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise ApiError("listen_optout must be a list")
            node.config.listen_optout = [str(x).strip() for x in raw if str(x).strip()][:16]
            changed.append("listen_optout")
        if "channel_gain" in payload:
            node.config.channel_gain = max(0.0, min(4.0, float(payload["channel_gain"])))
            changed.append("channel_gain")
        if "channel_sources" in payload:
            raw = payload["channel_sources"]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise ApiError("channel_sources must be a list of device names")
            wanted = [str(x).strip() for x in raw if str(x).strip()][:8]
            # ★ 这里收下的名字必须是【这台电脑现在真的能采到的设备】。
            # 网络上的另一台设备（手机/电脑）不是本机的音源 —— 用户很自然地把
            # "要广播给谁"理解成了"音源是谁"，把手机名选了进来，于是广播永远没有音源、
            # 一个字节都不发，而界面上没有任何地方告诉他。所以这里不再"静默存入"。
            try:
                local = node._local_capture_names()
            except Exception:
                local = set()
            bad = [name for name in wanted if name.lower() not in local]
            if bad:
                reasons = node._explain_selectors(bad)
                raise ApiError(
                    "这些名字不是这台电脑上的采集设备，不能当「广播我的声音」的音源：%s"
                    % reasons,
                    400,
                    {"rejected": bad, "reasons": reasons},
                )
            node.config.channel_sources = wanted
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
        """Probe one device with a UDP PING. Reports what actually happened, not a verdict.

        2026-10-14 的现场：用户截图里写着「192.168.0.101 不通：timed out」，看上去像
        手机没救了 —— 其实【安卓端根本不回 PING/PONG】（AudioRecv 把控制包记一笔就返回，
        它的注释也写明不做时钟对齐）。所以：

        * 端口优先用对端广播里报的 audio_port。以前这里是拿【本机】的 audio_port 去探，
          对端端口不一样时就探错了地方（编的端口当然没人回）。
        * 超时不再等于"不通"：UDP 没有握手，"没回应"只是一个事实，note 里写清它意味着什么。
        """
        host = str(payload.get("host") or "")
        if not host:
            raise ApiError("missing host")
        # 1) 页面上带的端口 2) 发现列表里对端自己报的端口 3) 兜底才是本机端口
        port = int(payload.get("port") or 0)
        peer = None
        if not port:
            try:
                peer = self.node.registry.resolve(host)
            except Exception:
                peer = None
            if peer is not None and getattr(peer, "audio_port", 0):
                port = int(peer.audio_port)
        if not port:
            port = int(self.node.config.audio_port)
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
            kind = proto.TYPE_NAMES.get(parsed.type, "?") if parsed else "invalid"
            return {
                "ok": True,
                "host": addr[0],
                "port": port,
                "rtt_ms": round(rtt_ms, 2),
                "type": kind,
                "note": "%s 回了 %s（往返 %.0f ms）" % (addr[0], kind, rtt_ms),
            }
        except OSError as exc:
            note = ("%s:%d 没有回应 UDP 探测包（%s）。"
                    "注意：安卓/手机端不回 PING/PONG（它只把控制包记一笔），"
                    "所以「超时」不等于连不通 —— 能不能听到声音请看它有没有在发声音、"
                    "以及「① 局域网里的设备」里是不是显示在线。"
                    % (host, port, exc))
            return {"ok": False, "error": str(exc), "host": host, "port": port, "note": note}
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


def _ws_read_frame(conn: Any, max_len: int = 1 << 20) -> tuple[int, bytes] | None:
    """读一个**客户端发来**的 WebSocket 帧，返回 (opcode, payload)；连接断了返回 None。

    项目里原来只有"服务端往浏览器发"这一条路（/ws/audio 是单向的），所以一直没有
    读帧的代码。手机直连这条通道是双向的，必须真的解析：客户端帧一定带掩码，
    长度有三种编码，SDP 还可能被分片（continuation 0x0）—— 这里都按 RFC 6455 处理。
    """
    def read_exact(n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    head = read_exact(2)
    if head is None:
        return None
    b0, b1 = head[0], head[1]
    opcode = b0 & 0x0F
    fin = bool(b0 & 0x80)
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = read_exact(2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = read_exact(8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
    if length > max_len:
        return None
    mask = b""
    if masked:
        got = read_exact(4)
        if got is None:
            return None
        mask = got
    payload = b""
    if length:
        payload = read_exact(length)
        if payload is None:
            return None
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

    # 分片：把后续的 continuation 帧拼起来（控制帧不参与拼接）
    if not fin and opcode in (0x1, 0x2):
        chunks = [payload]
        total = len(payload)
        first = opcode
        while True:
            nxt = _ws_read_frame(conn, max_len=max_len)
            if nxt is None:
                return None
            op2, pay2 = nxt
            total += len(pay2)
            if total > max_len:
                return None
            chunks.append(pay2)
            if op2 != 0x0:          # 控制帧：丢掉，继续等续帧
                continue
            break
        return first, b"".join(chunks)
    return opcode, payload


_P2P_ROOMS: Any = None


def _p2p_rooms() -> Any:
    """整个进程共用一张手机直连房间表（所有连接共用同一实例）。"""
    global _P2P_ROOMS
    if _P2P_ROOMS is None:
        from .net.p2p import P2PRooms

        _P2P_ROOMS = P2PRooms()
    return _P2P_ROOMS


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
    server_version = "AudioMixing/1.0"
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
            self._json(404, {"error": "这台电脑上还没有手机端安装包（AudioMixing-mic.apk）"})
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

    def _stream_pcm(self) -> None:
        """把本机正在播放的声音按固定块长分块推给手机（24kHz 单声道 PCM16）。

        响应头里带上采样率和声道数，手机端不用猜；块长固定 480 字节。
        没人听的时候 tap 里没有数据，这里就静静地等，不占 CPU。
        """
        conn = self.connection
        tap = self.api.node.tap
        sid = tap.subscribe()
        try:
            conn.sendall(
                (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Cache-Control: no-store\r\n"
                    "X-Sample-Rate: 24000\r\n"
                    "X-Channels: 1\r\n"
                    "X-Block-Bytes: %d\r\n"
                    "Connection: close\r\n\r\n" % BLOCK_BYTES_MONO
                ).encode("ascii")
            )
        except OSError:
            tap.unsubscribe(sid)
            return
        self.close_connection = True
        try:
            while True:
                payload = tap.drain(sid)
                if payload:
                    mono = _to_24k_mono(payload)
                    for off in range(0, len(mono), BLOCK_BYTES_MONO):
                        piece = mono[off : off + BLOCK_BYTES_MONO]
                        if len(piece) != BLOCK_BYTES_MONO:
                            continue
                        # 分块传输：十六进制长度 + CRLF + 数据 + CRLF
                        conn.sendall(b"%x\r\n" % len(piece) + piece + b"\r\n")
                else:
                    time.sleep(0.005)
        except OSError:
            pass
        finally:
            tap.unsubscribe(sid)

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
        # 手机听众这条路走 WebSocket，未压缩 48k 立体声是 1536 KB/s，
        # 手机上根本吃不下：缓冲涨到一秒就开丢。这里降成 24k 单声道，
        # 数据量变四分之一，够放音乐和视频，手机也能跟上。
        try:
            while True:
                payload = tap.drain(sid)
                if payload:
                    # 一块一块地重切，保证客户端每次收到的长度完全一样。
                    mono = _to_24k_mono(payload)
                    for off in range(0, len(mono), BLOCK_BYTES_MONO):
                        piece = mono[off : off + BLOCK_BYTES_MONO]
                        if len(piece) == BLOCK_BYTES_MONO:
                            conn.sendall(ws_frame(piece))
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

    def _websocket_p2p(self, parsed) -> None:
        """手机↔手机直连（WebRTC）的**信令**通道。

        这个接口只转发几十字节的协商信息（offer/answer/ICE）。音频是两台手机
        WebRTC 直连的，**完全不经过这台电脑** —— 这是功能的设计前提，
        所以这里绝不接收、缓存或转发任何二进制音频（p2p 层会拒收二进制并计数）。

        线程模型：`ThreadingHTTPServer` 下，每条连接一个线程，而对端线程会替我写
        我的 socket（转发、bye、error）。所以每个连接都要一把写锁，否则两个线程
        同时 sendall 会写出交错的帧，浏览器直接报协议错。
        """
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._json(400, {"error": "this endpoint speaks websocket"})
            return

        qs = parse_qs(parsed.query)
        room = str((qs.get("room") or [""])[0]).strip()
        role = str((qs.get("role") or ["host"])[0]).strip().lower() or "host"
        name = str((qs.get("name") or ["手机"])[0]).strip() or "手机"
        if not (len(room) == 4 and room.isdigit()):
            # 4 位数字房间号：够局域网内用，也便于另一台手机手输。
            self._json(400, {"error": "房间号要 4 位数字"})
            return

        conn = self.connection
        try:
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

        from .net.p2p import P2PRooms

        send_lock = threading.Lock()
        closed = threading.Event()

        def send_text(text: str) -> None:
            """线程安全地把一条消息发回去（对端线程也会调它）。"""
            with send_lock:
                if closed.is_set():
                    return
                try:
                    conn.sendall(ws_frame(str(text).encode("utf-8"), opcode=0x1))
                except OSError:
                    closed.set()

        def close_conn() -> None:
            closed.set()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        rooms = _p2p_rooms()
        member = rooms.join(room, role, name, send_text, close_conn)
        if member is None:
            # 房间已满：error 已经回给对方，连接也已经关了，这里直接收工。
            return
        try:
            while not closed.is_set():
                frame = _ws_read_frame(conn)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:            # close
                    break
                if opcode == 0x9:            # ping -> pong
                    with send_lock:
                        if not closed.is_set():
                            try:
                                conn.sendall(ws_frame(payload[:125], opcode=0xA))
                            except OSError:
                                closed.set()
                    continue
                if opcode == 0xA:            # pong：忽略
                    continue
                if opcode in (0x1, 0x2):
                    # 文本原样转发；二进制交给 p2p 层拒收（那是"绝不转发音频"的红线，
                    # 所以这里【不要】先 decode 成 str，否则等于放行了二进制）。
                    text: Any = payload.decode("utf-8", "replace") if opcode == 0x1 else payload
                    rooms.relay(room, member, text)
        except (OSError, ValueError):
            pass
        finally:
            rooms.leave(room, member)

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
            if path in ("/p2p", "/p2p.html", "/direct"):
                # 手机之间直连（WebRTC）。页面自己发起协商，电脑只转发信令。
                self._send(200, p2p_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/ws/p2p":
                self._websocket_p2p(parsed)
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
            if path in ("/app/download", "/app/AudioMixing-mic.apk", "/app/wavelink-mic.apk"):
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
                        "Audio Mixing OK\n"
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
                        "<title>Audio Mixing 测试页</title></head><body>"
                        "<h1>Audio Mixing 可以访问</h1>"
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
            if path == "/api/guide":
                # 「快速上手」四步自检：结论 + 一键修。判定全部复用引擎里已有的口径。
                self._json(200, self.api.guide())
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
                        "<title>Audio Mixing 诊断</title><style>"
                        "body{margin:0;background:#0e1116;color:#e8edf5;"
                        "font:13px/1.6 'Microsoft YaHei UI',system-ui,sans-serif}"
                        "header{display:flex;align-items:center;gap:8px;padding:14px 20px}"
                        "a.tab{padding:4px 12px;border-radius:8px;background:#1d2634;"
                        "color:#e8edf5;text-decoration:none;font-size:13px}"
                        "a.tab.on{background:#2f81f7;color:#06121f;font-weight:600}"
                        "pre{margin:0 20px 24px;padding:14px;background:#171c24;"
                        "border:1px solid #242c38;border-radius:10px;white-space:pre-wrap;"
                        "word-break:break-all}</style></head><body><header>"
                        "<b>Audio Mixing</b>"
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
            if path == "/api/monitor/disconnect":
                # 监控页每行的"断开"。三种都走这里，按 kind 分派：
                #   broadcast 关掉广播开关；route 静音那条路由；bluetooth 断开手机。
                body = self._read_json() if method == "POST" else {}
                kind = str(body.get("kind") or "")
                target = str(body.get("id") or "")
                if kind == "broadcast":
                    self.api.set_settings({"broadcast": False})
                    self._json(200, {"ok": True, "note": "已停止广播"})
                    return
                if kind == "route" and target:
                    self.api.update_route({"id": target, "mute": True})
                    self._json(200, {"ok": True, "note": "已静音这条路由"})
                    return
                if kind == "bluetooth" and target:
                    from . import bluetooth as bt

                    result = bt.disconnect(target)
                    self._json(200, {"ok": bool(result.get("ok")),
                                     "note": result.get("state") or result.get("error") or ""})
                    return
                if kind == "incoming":
                    self._json(200, {"ok": False,
                                     "note": "收到的流由对方决定是否发送：请在对方那台停止广播，"
                                             "或删掉/静音对端那条路由"})
                    return
                self._json(400, {"error": "不支持的断开类型: %r" % kind})
                return
            if path == "/qr":
                # 手机上扫一下就进来，省得手输 IP。参数 what=listen|app，
                # 地址用访问这个页面时用的那个主机名，天然就是局域网地址。
                from urllib.parse import parse_qs as _parse_qs

                query = _parse_qs(parsed.query)
                what = (query.get("what") or ["listen"])[0]
                # 一律用局域网地址：在电脑上用 127.0.0.1 打开监控页时，
                # 如果用请求里的地址生成二维码，手机扫了会连到自己，永远打不开。
                host = _lan_host(self.api, self.headers.get("Host") or "")
                # listen=手机当喇叭 app=装手机端 p2p=手机之间直连（WebRTC）
                tail = {"app": "/app", "p2p": "/p2p"}.get(what, "/listen")
                target = "http://%s%s" % (host or "127.0.0.1:17800", tail)
                png = _qr_png(target)
                if png is None:
                    self._json(503, {"error": "二维码库不可用"})
                    return
                self._send(200, png, "image/png")
                return
            if path == "/audio.pcm":
                # 手机 APK 用的低门槛入口：普通 HTTP 分块传输，读就行。
                self._stream_pcm()
                return
            if path == "/api/multiout":
                node = self.api.node
                out = node.multi_out.stats()
                out["available"] = node.multi_out.available_devices()
                self._json(200, out)
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
            # 附带的机器可读字段一起发出去（例如 rejected: 哪些音源名被拒了），
            # 页面才能给出"一键移除"，而不是只把一句话丢给用户。
            body: dict[str, Any] = {"error": str(exc)}
            body.update(getattr(exc, "data", None) or {})
            self._json(exc.status, body)
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
            if path == "/api/multiout":
                # 本机多设备输出：勾选几个输出设备就同时响几个。
                # 这里用 body（POST 分支里请求体叫 body，只有 GET 分支叫 payload，
                # 上次照 GET 抄成 payload 就 NameError 了）。
                raw = body.get("devices")
                items: list[dict] = []
                if isinstance(raw, list):
                    for entry in raw:
                        if not isinstance(entry, dict):
                            continue
                        name = str(entry.get("device") or "").strip()
                        if not name:
                            continue
                        try:
                            gain = float(entry.get("gain", 1.0))
                        except (TypeError, ValueError):
                            gain = 1.0
                        try:
                            delay = float(entry.get("delay_ms", 0.0))
                        except (TypeError, ValueError):
                            delay = 0.0
                        items.append({"device": name,
                                      "gain": max(0.0, min(4.0, gain)),
                                      "delay_ms": max(0.0, min(2000.0, delay))})
                self.api.node.set_multi_out(items)
                out = self.api.node.multi_out.stats()
                _excl: list[str] = []
                try:
                    # 两个来源都问一遍：多设备输出是从 default_source_spec() 那块
                    # 设备的回环采的，它一定已经在响；_default_render_name() 是
                    # Windows 当前的默认输出，通常与前者相同。
                    spec_now = self.api.node.default_source_spec() or {}
                    name_now = str(spec_now.get("device") or "")
                    if name_now:
                        _excl.append(name_now)
                except Exception:
                    pass
                try:
                    default_now = self.api.node._default_render_name()
                    if default_now:
                        _excl.append(default_now)
                except Exception:
                    pass
                try:
                    for item in self.api.node.devices or []:
                        if getattr(item, "is_default", False):
                            _excl.append(str(getattr(item, "name", "") or ""))
                except Exception:
                    pass
                _excl = [x for x in dict.fromkeys(_excl) if x]
                out["available"] = self.api.node.multi_out.available_devices(_excl)
                out["playing_now"] = _excl
                self._json(200, out)
                return
            if path == "/api/firewall":
                # 放行防火墙。这条必须放在 POST 分支里，因为页面用的是 POST；
                # 之前只写进了 GET 分支，页面点按钮只会拿到 404。
                from .tray import add_firewall_rules

                bat = add_firewall_rules()
                self._json(200, {"ok": True, "bat": bat,
                                 "note": "已请求以管理员身份放行。若没看到管理员确认窗口，"
                                         "请右键运行 " + bat})
            elif path == "/api/monitor/disconnect":
                # 监控页每行的"断开"。必须放在 POST 分支：页面用的是 POST，
                # 只写进 GET 分支的话点按钮只会拿到 404（放行防火墙踩过同一个坑）。
                kind = str(body.get("kind") or "")
                target = str(body.get("id") or "")
                if kind == "broadcast":
                    self.api.set_settings({"broadcast": False})
                    self._json(200, {"ok": True, "note": "已停止广播"})
                elif kind == "route" and target:
                    self.api.update_route({"id": target, "mute": True})
                    self._json(200, {"ok": True, "note": "已静音这条路由"})
                elif kind == "bluetooth" and target:
                    from . import bluetooth as bt

                    result = bt.disconnect(target)
                    self._json(200, {"ok": bool(result.get("ok")),
                                     "note": result.get("state") or result.get("error") or ""})
                elif kind == "incoming":
                    self._json(200, {"ok": False,
                                     "note": "收到的流由对方决定是否发送：请在对方那台停止广播，"
                                             "或删掉/静音对端那条路由"})
                else:
                    self._json(400, {"error": "不支持的断开类型: %r" % kind})
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
            elif path == "/api/listen/optout":
                # ① 自动收听：撤销时把设备记进"永不再自动"名单；"这次打开"时移出去。
                node = self.api.node
                on = bool(body.get("on", True))
                node_id = str(body.get("node_id") or "").strip()
                name = str(body.get("name") or "").strip()
                if not node_id and not name:
                    raise ApiError("missing node_id or name")
                node.set_listen_optout(node_id, name, on=on)
                if not on:
                    # 这次打开：立刻按请求把它加回来（等同再收到一次 want_listen）
                    node._grant_listen_request(node_id, name, str(body.get("host") or ""))
                self._json(200, {"ok": True, "on": on,
                                 "note": ("已加入「不再自动收听」名单：" + (name or node_id)) if on
                                         else ("已允许自动收听：" + (name or node_id))})
            elif path == "/api/guide/fix":
                # 「快速上手」里的"一键修"：换设备 / 试采 / 修广播。一次只做用户点的那一件。
                self._json(200, self.api.guide_fix(body))
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
                warning = ""
                if name and turn_on:
                    # 这台手机必须"确实在送声音"才配当音源：它现在要么已经是本机的采集设备，
                    # 要么至少往这台电脑推过音频。否则收下它 = 广播永远没有音源、一个字节都
                    # 不发，而手机那边只看到按钮亮了 —— 缺陷 1 的另一扇门就是这个。
                    try:
                        known = name.lower() in node._local_capture_names() or name.lower() in node._phone_names()
                    except Exception:
                        known = True
                    if not known:
                        warning = ("这台手机现在没有在送声音，没有把它设成音源："
                                   "请先在手机上打开「送声音」，再点「广播我的声音」。")
                        turn_on = False
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
                                 "warning": warning,
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
            # 附带的机器可读字段一起发出去（例如 rejected: 哪些音源名被拒了），
            # 页面才能给出"一键移除"，而不是只把一句话丢给用户。
            body: dict[str, Any] = {"error": str(exc)}
            body.update(getattr(exc, "data", None) or {})
            self._json(exc.status, body)
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
<html lang="zh-CN"><head><meta charset="utf-8"><title>Audio Mixing</title>
<style>body{background:#0b0e14;color:#e8eef7;font:14px/1.7 system-ui,"Microsoft YaHei";padding:40px;max-width:720px;margin:auto}
code{background:#1a202c;padding:2px 6px;border-radius:4px}a{color:#60a5fa}</style></head><body>
<h2>Audio Mixing 控制台页面文件缺失</h2>
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
        candidates.append(Path(base) / "audiomixing" / "ui" / "index.html")
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


def _phone_html(kind: str) -> str:
    """手机端页面：收听和送声音合成同一个界面（用页面内切换，不用两套 UI）。

    两个入口都发同一个 phone.html：
        /listen  → 默认「收听」模式
        /mic     → 默认「送声音」模式
    页面自己按路径判断默认模式（所以这里不需要注入任何东西）。
    """
    cache = _PHONE_CACHE.get(kind)
    if cache is not None:
        return cache

    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "audiomixing" / "ui" / "phone.html")
        candidates.append(Path(base) / "ui" / "phone.html")
    candidates.append(Path(__file__).resolve().parent / "ui" / "phone.html")

    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                _PHONE_CACHE[kind] = text
                return text
        except OSError:
            continue

    _PHONE_CACHE[kind] = FALLBACK_LISTEN_HTML
    return _PHONE_CACHE[kind]


def mic_html() -> str:
    """The phone-facing page that pushes its microphone in as a capture device."""
    return _phone_html("mic")


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
        candidates.append(Path(base) / "audiomixing" / "ui" / "AudioMixing-mic.apk")
    candidates.append(Path(base) / "audiomixing" / "ui" / "wavelink-mic.apk")
    here = Path(__file__).resolve().parent
    candidates.append(here / "ui" / "AudioMixing-mic.apk")
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




def _lan_host(api, fallback: str = "") -> str:
    """这台机器的局域网地址（带网页端口），拿不到就用 fallback。

    只认私有网段的 IPv4：127.0.0.1、公网地址、虚拟网卡（Radmin 之类）都不要，
    因为手机连不上它们。
    """
    port = 17800
    try:
        node = api.node
        port = int(getattr(node.config, "web_port", 0) or 17800)
    except Exception:
        pass
    try:
        from .net import discovery as _disc

        report = _disc.lan_report()
        candidates = []
        for item in report.get("interfaces") or []:
            if str(item.get("kind")) != "private":
                continue
            addr = str(item.get("address") or "")
            if not addr or addr.startswith("127.") or addr.startswith("169.254."):
                continue
            candidates.append(addr)
        if candidates:
            # 优先 192.168 / 10. 网段，其次 172.16~31
            candidates.sort(key=lambda a: (not a.startswith("192.168."),
                                           not a.startswith("10.")))
            return "%s:%d" % (candidates[0], port)
    except Exception:
        pass
    host = fallback.split(":")[0] if fallback else ""
    if host and not host.startswith("127."):
        return "%s:%d" % (host, port)
    return ""

def _qr_png(text: str) -> bytes | None:
    """把一段文字生成二维码 PNG。库不在就返回 None，页面会显示提示。"""
    try:
        import io as _io

        import qrcode

        image = qrcode.make(text, border=2)
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


BLOCK_BYTES_MONO = 480  # 24kHz 单声道 10 毫秒


def _to_24k_mono(pcm16: bytes) -> bytes:
    """把 48kHz 立体声 PCM16 变成 24kHz 单声道，并按固定长度重新切块。

    规则（照现场量出来的问题定的）：
      * 只认 48k 立体声 4 字节一帧的输入；长度不是 4 的倍数就裁掉尾巴。
      * 每两个立体声帧取一帧，只留左声道 -> 24kHz 单声道，每帧 2 字节。
      * 输出一律重新切成 480 字节一块（24kHz 的 10 毫秒）。
        客户端最怕同一条流里长短不一：它会按一种长度播，另一种就变调。

    转换不成就返回空，让调用方丢掉这一块 —— 绝不把旧格式混进去。
    """
    try:
        import array as _array

        usable = len(pcm16) - (len(pcm16) % 4)
        if usable <= 0:
            return b""
        frames = _array.array("h")
        frames.frombytes(pcm16[:usable])
        mono = _array.array("h", [frames[i] for i in range(0, len(frames) - 1, 4)])
        raw = mono.tobytes()
        if len(raw) % BLOCK_BYTES_MONO:
            raw += b"\x00" * (BLOCK_BYTES_MONO - (len(raw) % BLOCK_BYTES_MONO))
        return raw
    except Exception:
        return b""



def monitor_html() -> str:
    """监控窗。谁给谁、走哪个通道、多少数据、延迟多少。"""
    return """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Audio Mixing 监控</title>
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
 .disc{padding:2px 10px;border-radius:6px;border:1px solid #3d2a2a;background:#2a1a1a;
       color:#ffb3b3;cursor:pointer;font-size:12px}
 .empty{padding:30px 20px;color:var(--dim)}
 footer{padding:14px 20px;color:var(--dim);font-size:12px}
</style></head>
<body>
<header>
  <h1><span class="dot">●</span> Audio Mixing</h1>
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
  <th>数据量</th><th>速率</th><th>延迟</th><th></th></tr></thead>
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
        <td>${f.disconnect ? '<button class="disc" data-kind="' + f.disconnect +
             '" data-id="' + (f.route_id || '') + '" title="' +
             (f.hint || '断开') + '">断开</button>' : ''}</td>
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
document.getElementById('rows').addEventListener('click', async (ev) => {
  const btn = ev.target.closest ? ev.target.closest('button.disc') : null;
  if(!btn) return;
  const kind = btn.dataset.kind, id = btn.dataset.id || '';
  btn.disabled = true;
  try{
    const r = await (await fetch('/api/monitor/disconnect', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind: kind, id: id})
    })).json();
    if(r && r.ok === false){ alert(r.note || '没能断开'); }
    tick();
  }catch(e){ alert('断开失败：' + e); }
  finally{ btn.disabled = false; }
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
        candidates.append(Path(base) / "audiomixing" / "ui" / "app.html")
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
# 手机页只有一个文件（phone.html），/listen 和 /mic 各自缓存一份读到的内容
_PHONE_CACHE: dict[str, str] = {}
_P2P_CACHE: str | None = None

FALLBACK_P2P_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio Mixing 手机直连</title></head>
<body style="background:#070b12;color:#eaf1fb;font:15px system-ui;padding:24px">
<h1>手机直连</h1>
<p>这个页面没能加载出来（<code>audiomixing/ui/p2p.html</code> 不在）。</p>
<p>手机之间直连需要一个支持 WebRTC 的浏览器：安卓 Chrome / Edge，或 iPhone 上的 Safari。
也可以用控制台上的「手机当喇叭」二维码收听电脑的声音。</p>
</body></html>"""


def p2p_html() -> str:
    """手机之间直连的页面（WebRTC）。电脑只转发协商信息，音频不经过电脑。"""
    global _P2P_CACHE
    if _P2P_CACHE is not None:
        return _P2P_CACHE

    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "audiomixing" / "ui" / "p2p.html")
        candidates.append(Path(base) / "ui" / "p2p.html")
    candidates.append(Path(__file__).resolve().parent / "ui" / "p2p.html")

    for path in candidates:
        try:
            if path.is_file():
                _P2P_CACHE = path.read_text(encoding="utf-8")
                return _P2P_CACHE
        except OSError:
            continue

    _P2P_CACHE = FALLBACK_P2P_HTML
    return _P2P_CACHE

FALLBACK_LISTEN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio Mixing 收音机</title></head>
<body style="background:#0e1116;color:#e8edf5;font:15px system-ui;padding:24px">
<h1>Audio Mixing 收音机</h1>
<p>这个页面需要 WebSocket 支持。请用手机上的 Chrome / Safari 打开本机地址的 <code>/listen</code>，
例如 <code>http://192.168.x.x:17800/listen</code>。</p>
</body></html>"""


def listen_html() -> str:
    """The phone-facing page: a one-tap "tune in" player for this node's output."""
    return _phone_html("listen")


# Kept as a module attribute for backwards compatibility with anything importing it.
INDEX_HTML = FALLBACK_HTML
