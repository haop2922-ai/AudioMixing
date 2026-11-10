"""The control panel: a dependency-free HTTP server plus a single-page UI.

Runs inside the Audio Mixing node process, so one command gives you both the audio
engine and the thing you click on. The page itself lives in ``audiomixing/ui/index.html``
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
from urllib.parse import parse_qs, quote, urlparse

from .net import discovery as disc
from .net import rtpaudio
from . import bluetooth
# 解 PowerShell 输出的编码（中文 Windows 上是 GBK）——见 netcheck.decode_powershell 的说明。
from .netcheck import decode_powershell as _decode_powershell

MAX_BODY = 256 * 1024
_LAST_FORCED_BT = 0.0

# ★ 2026-11-04（第 2 条）：离线设备卡片在面板上保留多久。
# 发现表本身留 90 秒（`PEER_PRUNE_TTL_S`，那是给"已暂停发送"的告警留证据），
# 但**卡片**不需要挂那么久 —— 用户真机看到同一台手机的两张卡里有一张早就该消失。
# 30 秒 = 15 个信标周期，Wi-Fi 抖一下不会闪没；真关机半分钟内卡片就回收。
# 手动添加的设备永远保留（那是用户明确写下的地址）。
PEER_CARD_TTL_S = 30.0


def _self_identity_keys(node: Any) -> set[str]:
    """本机的全部身份键：node_id、device_id、instance_id、历史别名、本机地址。

    ★ 2026-11-06（UI 残留）：判断"这张卡是不是本机"不能只比一种 id ——
    身份归一/换网之后，对端表里可能留着旧别名，只比 node_id 会漏。
    """
    ids = {str(getattr(node, "node_id", "") or ""),
           str(getattr(getattr(node, "config", None), "device_id", "") or ""),
           str(getattr(getattr(node, "config", None), "instance_id", "") or "")}
    ids.update(str(x) for x in (getattr(getattr(node, "config", None), "alias_ids", None) or []))
    try:
        ids.update(str(x) for x in (disc.local_ip_addresses() or []))
    except Exception:
        pass
    ids.discard("")
    return ids


def _peer_is_self(item: dict[str, Any], self_ids: set[str]) -> bool:
    """对端表里的这一条是不是本机自己（本机信标绕回 / 旧别名残留）。

    ★ 2026-11-06（双节点自检抓到）：**不能只按地址判**。同一台电脑上跑两个节点时
    （selftest_twonode、或用户开两个实例），两个 peer 的 host 都是 127.0.0.1 /
    192.168.0.105，按地址判会把对方也当成"本机"过滤掉 —— 自检的
    "控制台列出对端设备" 就是这样变成 peers=[] 的。地址只在**完全没有唯一身份**
    （手动按 IP 添加的设备）时才作为兜底判据。
    """
    if bool(item.get("local")):
        return True
    ids = {str(item.get("node_id") or ""), str(item.get("device_id") or ""),
           str(item.get("instance_id") or "")}
    ids.update(str(x) for x in (item.get("alias_ids") or []))
    ids.discard("")
    if ids & self_ids:
        return True
    if not ids:
        hosts = {str(item.get("host") or "")}
        hosts.update(str(x) for x in (item.get("hosts") or []))
        hosts.discard("")
        return bool(hosts & self_ids)
    return False

# ★ v2.28（用户新 UI 需求）：Windows 开机自启动。
#   只写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run，不需要管理员权限；
#   不碰系统服务、不写 HKLM。用 `serve` 启动：登录后服务在后台就绪，不弹面板。
_AUTOSTART_VALUE = "AudioMixing"
# ★ 2026-11-06（第 5 条）：老版本/w 其它名字写过的值也一起清 —— 取消勾选要"清干净"，
#   不能让一个旧的、指向临时目录的值继续把程序拉起来。
_AUTOSTART_LEGACY_VALUES = ("AudioMixing", "audiomixing", "AudioMixingNode", "AudioMixingLauncher")


def _in_onefile_temp(path: str) -> bool:
    """这个路径是不是 Nuitka onefile 每次运行生成的临时解包目录。

    onefile 的临时目录退出即删；把它的 `python.exe` 或 `audiomixing_launcher.py`
    写进 `Run` 等于写了一个**下次开机不存在的路径** —— 这就是现场"勾了自启动、
    开机不起"的直接原因。
    """
    low = str(path or "").lower()
    if not low:
        return True
    if "onefile_" in low:
        return True
    if os.environ.get("NUITKA_ONEFILE_PARENT") and ("\\temp\\" in low or "/temp/" in low):
        return True
    return False


def _onefile_parent_exe() -> str:
    """onefile 的"外层进程"对应的 exe 路径（Nuitka 把 PID 放在这个环境变量里）。

    `sys.argv[0]` 在某些启动方式（比如快捷方式/被别的进程拉起）下可能拿不到原始
    exe 路径，这时按 PID 问系统要可执行文件全路径，比猜测临时目录靠谱。
    """
    pid = os.environ.get("NUITKA_ONEFILE_PARENT")
    if not pid or sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(4096)
            size = wintypes.DWORD(len(buf))
            query = k32.QueryFullProcessImageNameW
            query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                              ctypes.POINTER(wintypes.DWORD)]
            if query(handle, 0, buf, ctypes.byref(size)):
                return str(buf.value or "")
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""
    return ""


def _program_exe_path() -> str:
    """打包后"用户双击的那颗 exe"的路径；源码运行时返回空串。

    判据必须和 `_is_frozen()` 一致：Nuitka **不设** `sys.frozen`，只看它就会
    走进开发分支、把 onefile 临时目录写进注册表。候选按可靠度排序：
    `__compiled__.original_argv0` → `sys.argv[0]` → `sys.executable`，
    任何一个落在 onefile 临时目录里的都丢弃。
    """
    if not (globals().get("__compiled__") or getattr(sys, "frozen", False)):
        return ""
    candidates: list[str] = []
    comp = globals().get("__compiled__")
    if comp is not None:
        try:
            original = getattr(comp, "original_argv0", None)
        except Exception:
            original = None
        if original:
            candidates.append(str(original))
    try:
        candidates.append(str(sys.argv[0] or ""))
    except Exception:
        pass
    candidates.append(str(getattr(sys, "executable", "") or ""))
    candidates.append(_onefile_parent_exe())

    for raw in candidates:
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if p.suffix.lower() != ".exe" or not p.is_file():
            continue
        if _in_onefile_temp(str(p)):
            continue
        return str(p)
    return ""


def _autostart_command() -> str:
    """返回写进 Run 键的命令行；开发态用 pythonw + launcher，打包态用 exe 自身。

    ★ 2026-11-06（第 5 条）：打包态必须写**外层 exe**（带引号，路径含空格也不怕），
    参数 `serve` 让它登录后只起后台服务。源码态才用 pythonw + launcher。
    """
    exe = _program_exe_path()
    if exe:
        return '"%s" serve' % exe
    python = sys.executable or "python"
    pythonw = str(Path(python).with_name("pythonw.exe"))
    if os.path.exists(pythonw):
        python = pythonw
    launcher = Path(__file__).resolve().parent.parent / "audiomixing_launcher.py"
    return '"%s" "%s" serve' % (python, launcher)


def _run_key():
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                          winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)


def autostart_state() -> dict[str, Any]:
    """注册表里现在到底是什么（面板/诊断据此显示"勾了但没写进去"）。"""
    state: dict[str, Any] = {"supported": sys.platform == "win32", "enabled": False,
                             "command": "", "expected": "", "ok": False, "problem": ""}
    if sys.platform != "win32":
        state["problem"] = "开机自启动只支持 Windows"
        return state
    try:
        import winreg
        expected = _autostart_command()
        state["expected"] = expected
        with _run_key() as key:
            for name in _AUTOSTART_LEGACY_VALUES:
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                state["enabled"] = True
                state["command"] = str(value)
                break
            else:
                return state              # 一个都没写
        state["ok"] = state["command"] == expected
        if not state["ok"]:
            state["problem"] = ("注册表里的启动命令和当前程序对不上："
                                "已写 %r，应为 %r（重新勾选一次即可修正）"
                                % (state["command"], expected))
    except Exception as exc:
        state["problem"] = "读取开机自启动失败：%s" % exc
    return state


def set_windows_autostart(enabled: bool) -> tuple[bool, str]:
    """写/删开机自启动项。返回 (ok, 错误文本)。非 Windows 明确返回不可用。"""
    if sys.platform != "win32":
        return False, "开机自启动只支持 Windows"
    try:
        import winreg
        with _run_key() as key:
            if enabled:
                command = _autostart_command()
                if not command:
                    return False, "无法确定程序路径，开机自启动没有写入"
                winreg.SetValueEx(key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, command)
                # 历史上别的名字写过的值一并删掉，避免同一台机器登录时被拉起两次
                # （老版本的名字指向旧 exe，更容易看起来"自启动坏了"）。
                # ★ 2026-11-06（Q4）：注册表值名**不区分大小写**。旧写法
                #   `if name != "AudioMixing"` 会把 `audiomixing` 当成"另一个名字"
                #   去删，实际删掉的就是刚写进去的 `AudioMixing`，回读于是
                #   [WinError 2]；本次改成大小写无关比较。
                for name in _AUTOSTART_LEGACY_VALUES:
                    if name.lower() != _AUTOSTART_VALUE.lower():
                        try:
                            winreg.DeleteValue(key, name)
                        except FileNotFoundError:
                            pass
                # 写完立刻读回来校验：被安全软件拦、路径写错，都能在这一步暴露，
                # 不再"看着成功、开机不起"。
                try:
                    written, _kind = winreg.QueryValueEx(key, _AUTOSTART_VALUE)
                except OSError as exc:
                    return False, "写入后读不回来：%s" % exc
                if str(written) != command:
                    return False, ("写入后校验不一致：注册表是 %r，应为 %r"
                                   % (written, command))
                if _in_onefile_temp(command):
                    return False, ("启动命令指向临时解包目录（%s），重启后就不存在；"
                                   "请用发布包里的 exe 重新勾选" % command)
            else:
                for name in _AUTOSTART_LEGACY_VALUES:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
        return True, ""
    except Exception as exc:
        return False, "写入开机自启动失败：%s" % exc

# Host 允许名单（每 30 秒刷新一次局域网 IP）：挡住 DNS rebinding ——
# 恶意域名解析到 127.0.0.1 时，浏览器仍会带 Host: evil.com；只看 Origin 是拦不住的。
_HOST_IPS: set[str] = set()
_HOST_AT = 0.0
_HOST_LOCK = threading.Lock()


def _host_allowed(raw: str) -> bool:
    if not raw:
        return True
    try:
        host = (urlparse("//" + raw).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return True
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    local_name = (socket.gethostname() or "").strip().lower()
    if local_name and (host == local_name or host.split(".")[0] == local_name):
        return True
    global _HOST_AT
    now = time.monotonic()
    with _HOST_LOCK:
        if now - _HOST_AT > 30.0:
            try:
                ips = {str(i.get("address") or "").strip().lower()
                       for i in (disc.lan_report().get("interfaces") or [])
                       if str(i.get("kind") or "") == "private"}
            except Exception:
                ips = set()
            _HOST_IPS.clear()
            _HOST_IPS.update(ips)
            _HOST_AT = now
        return host in _HOST_IPS


def _packaging_style() -> str:
    """这次跑的是源码、单文件还是免解包文件夹。

    ★ 2026-10-26：用户看到任务管理器里有两个 Audio Mixing，怀疑是重复启动。
      单文件（Nuitka onefile）本来就是两个进程：引导器负责解包并等待，真正的
      程序在它拉起来的子进程里。报告里必须先说清楚形态，用户才知道该不该紧张。
    """
    if not (globals().get("__compiled__") or getattr(sys, "frozen", False)):
        return "源码运行"
    exe = str(getattr(sys, "executable", "") or "").lower()
    # Nuitka onefile: %TEMP%\onefile_<pid>_<随机串>\...；PyInstaller onefile: \_MEIxxxx\...
    # 只认这两个特征，不能见到 "temp" 就判单文件（免解包版也可能放在 Temp 下）。
    if "onefile_" in exe or "\\_mei" in exe or "/_mei" in exe:
        return "单文件（Nuitka onefile）"
    return "免解包文件夹（Nuitka onedir）"


def _cpu_vendor() -> str:
    """CPU 厂商（GenuineIntel / AuthenticAMD）。诊断蓝牙适配用。

    ★ 2026-10-26 用户：「部分 inter（Intel）电脑蓝牙没声音」，随后要求「AMD 的也加上」。
      先确认平台，再结合蓝牙适配器型号给处置；AMD 机器的蓝牙同样是板载模块，
      不能因为 CPU 不是 Intel 就把“连接正常但不出声”当成别的问题。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "VendorIdentifier")[0] or "").strip()
    except Exception:
        return ""


def _parent_process_info() -> tuple[int, str]:
    """父进程 PID 和映像路径（拿不到就空）。只用于诊断，不参与运行逻辑。"""
    if sys.platform != "win32":
        return 0, ""
    try:
        ppid = int(os.getppid())
    except Exception:
        return 0, ""
    if ppid <= 0:
        return 0, ""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, ppid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ppid, ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            k32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return ppid, (buf.value if ok else "")
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ppid, ""


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
        super().__init__(daemon=True, name="audiomixing-rawmic")
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


class AudioMixingApi:
    """Thin logic layer; the HTTP handler only parses and serialises."""

    def __init__(self, node, *, web=None, on_routes_changed: Callable[..., None] | None = None,
                 restart_argv: list[str] | None = None) -> None:
        self.node = node
        self.web = web
        self.on_routes_changed = on_routes_changed
        # ★ 2026-11-01 用户：「修改端口号后自动重启」。
        #   这里存的是**重启时该带的命令行参数**：
        #     · 桌面形态（启动器）→ []（无参数，启动器自己读配置、开面板）
        #     · exe serve          → ["serve", "--host", ...]（去掉端口参数，改读新配置）
        #     · 单元测试 / 双节点自检 → None（绝不允许自动重启，否则会把测试进程杀掉）
        self.restart_argv = None if restart_argv is None else [str(x) for x in restart_argv]

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
        # ★ 2026-11-01：启动器要靠它区分"自己人"和同端口的同类软件。
        data["app"] = "audiomixing"
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
        """现在是打包后的 exe 还是源码在跑（Nuitka 不设 sys.frozen，所以两个都看）。

        口径必须只有这一处：诊断报告和面板都调它。曾经诊断报告自己写
        `getattr(sys, "frozen", False)`，结果跑的是 exe、报告里却写「源码运行」，
        和同一份报告上方的「版本 exe」自相矛盾（2026-10-14 实测）。
        """
        return bool(globals().get("__compiled__")) or bool(getattr(sys, "frozen", False))

    @staticmethod
    def _frozen_exe_path() -> Path | None:
        """打包后，用户双击的那个 exe 到底在哪（找不到返回 None）。

        Nuitka onefile 实测形态：``sys.executable`` 指向解包出来的临时
        ``...\\Temp\\onefile_<pid>_...\\python.exe``，**不是**那个 exe；
        于是对它取 mtime 会失败（异常被吞掉 → 面板显示「构建于 未知」）。
        真正的位置按可靠度排序：

          __compiled__.original_argv0  onefile 原始的 argv[0]，就是那个 exe
          __compiled__.containing_dir  onedir/standalone：exe 所在目录
          sys.argv[0] / sys.executable 兜底（onedir 时 sys.executable 就是它）

        注意：这几个候选可能指向不存在的路径（如上面那个临时 python.exe），
        所以逐个 is_file() 试，第一个真实存在的才算数。
        """
        candidates: list[str] = []
        comp = globals().get("__compiled__")
        if comp is not None:
            for attr in ("original_argv0", "containing_dir"):
                try:
                    value = getattr(comp, attr, None)
                except Exception:
                    continue
                if value:
                    candidates.append(str(value))
        try:
            candidates.append(str(sys.argv[0] or ""))
        except Exception:
            pass
        candidates.append(str(getattr(sys, "executable", "") or ""))

        for raw in candidates:
            if not raw:
                continue
            try:
                p = Path(raw).resolve()
            except (OSError, ValueError):
                continue
            if p.is_file():
                return p
        return None

    def build_info(self) -> dict[str, Any]:
        """当前跑的是哪个版本、什么时候编出来的、旁边有没有更新的版本。

        用户的真实现场：桌面上摆着 v1.2/v1.3/v1.4 三个 exe，他双击的是 v1.2
        （比当时的代码早 7 个小时），于是所有修复都不在他测的那个程序里 ——
        而面板上没有任何地方写着版本。这条必须显眼。
        """
        from . import __version__, __build_time__

        frozen = self._is_frozen()
        if frozen:
            # sys.executable 在 Nuitka onefile 下指向解包出来的临时 python.exe，
            # 不是那个 exe —— 必须另找（见 _frozen_exe_path 的说明）。
            real = self._frozen_exe_path()
            path = real if real is not None else Path(sys.executable)
            channel = "exe"
        else:
            path = Path(__file__).resolve().parent / "__init__.py"
            channel = "源码"
        # 打包时写死的时间优先：exe 复制到桌面后文件时间会变，构建时间不该跟着变。
        # 拿不到（源码运行 / 老包）才退回看文件时间。
        build_time = __build_time__ if (frozen and __build_time__) else ""
        if not build_time:
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
                          "把系统的输出设备也切到这块，或在「频道」的「音源」里改选声音真正在走的那一块。")
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
                    {"label": "放行并清理旧规则（需管理员）", "api": "/api/firewall", "kind": "ghost"},
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
        """手机浏览器能直接打开、用来听声音的地址。

        ★ 2026-10-26：原来取的是 `lan_report()` 里**第一块网卡**，在这台机器上
          拿到的是虚拟网卡（172.18.x.x，WSL/Hyper-V），手机根本连不上。
          改为复用 `_lan_host()` 的排序口径：优先 192.168 / 10 网段，跳过
          127 / 169.254，和二维码、面板上显示的地址保持一致。
        """
        try:
            host = _lan_host(self)
        except Exception:
            host = ""
        if not host:
            return ""
        return "http://%s/listen" % host

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
        return {
            "self": {
                "id": node.node_id,
                "short_id": disc.short_id(node.node_id),
                "name": node.name,
                # ★ 2026-11-03（Q1）：原来直接取 interfaces[0]，在这台机器上取到的是
                #   Hyper-V 默认交换机的 172.26.192.1，面板和手机都以为本机在 172.26 网段。
                #   _best_local_address() 会先找"和已知对端同网段"的地址，再退回物理网卡排序。
                "host": _best_local_address(node, report),
                "audio_port": node.config.audio_port,
                "web_port": node.config.web_port,
                "sources": node.source_list(),
                "sinks": node.sink_list(),
            },
            "peers": self._peers_with_smart(node),
            "network": _network_payload(node, report),
            # ★ v2.27：关系图人工合并（蓝牙↔局域网）也随设备列表下发，
            #   面板刷新时不用再多打一次 /api/config。
            "identity_merges": list(getattr(node.config, "identity_merges", None) or []),
            "relation_merges": list(getattr(node.config, "relation_merges", None) or []),
        }

    def _peers_with_smart(self, node) -> list[dict[str, Any]]:
        """设备列表 + 本机「智连」的**实况**（2026-11-01 用户：「智连不是真实反馈」）。

        以前卡片只读 `config.smart_link`（名单本身），引擎到底有没有替它建收听路由
        完全看不出来：按钮亮着、实际什么都没发生。这里把三件事一起给页面：
          · `smart_on`      本机给这台设备开没开「智连」（引擎口径）
          · `smart_auto`    现在是否真的有一条**替它自动建的**收听路由（引擎实况）
          · `smart_blocked` 它想听本机、但本机没开智连 → 已拦下不给声音
        """
        try:
            peers = list(node.registry.snapshot() or [])
        except Exception:
            peers = []
        # ★ 2026-11-06（UI 残留）：本机的信标会从本机网卡绕回来（local=True），
        #   以前原样下发，面板「局域网里的设备」里就多出一张标着「本机」的卡。
        #   这里在服务端先剔一遍；页面的 renderDeviceCards 也剔一遍（双保险）。
        #   本机身份包括 node_id、device_id、instance_id、全部历史别名和本机地址。
        self_ids = _self_identity_keys(node)

        # 离线卡片超时回收（第 2 条）：只影响"面板上显示哪些卡"，
        # 注册表里的记录仍保留到 PEER_PRUNE_TTL_S，供路由告警与解析使用。
        peers = [p for p in peers
                 if not _peer_is_self(p, self_ids)
                 and (p.get("manual") or float(p.get("age_s") or 0.0) <= PEER_CARD_TTL_S)]
        auto: dict[str, dict[str, Any]] = {}
        try:
            for item in (node.auto_listen_state().get("items") or []):
                auto[str(item.get("node_id") or "")] = item
        except Exception:
            auto = {}
        blocked: dict[str, dict[str, Any]] = {}
        try:
            for item in (getattr(node, "_senders_blocked", {}) or {}).values():
                if item.get("smart_blocked"):
                    blocked[str(item.get("node_id") or "")] = item
        except Exception:
            blocked = {}
        # ★ 2026-11-04（第 5 条）：最近一次挂断（双判据）的信息按 node_id 索引。
        #   卡片据此把"✓ 收听中"换成"已挂断 · 待命"，并显示 audio/heartbeat 两个数。
        hangs: dict[str, dict[str, Any]] = {}
        try:
            for item in (node.auto_listen_state().get("hangs") or []):
                hangs[str(item.get("node_id") or "")] = item
        except Exception:
            hangs = {}
        for item in peers:
            node_id = str(item.get("node_id") or "")
            name = str(item.get("name") or "")
            try:
                item["smart_on"] = bool(node.smart_link_allows(node_id, name))
            except Exception:
                item["smart_on"] = False
            info = auto.get(node_id) or hangs.get(node_id) or {}
            item["smart_auto"] = bool(info.get("active"))
            item["smart_blocked"] = node_id in blocked
            item["listen_audio_s"] = info.get("audio_silence_s")
            item["listen_heartbeat_s"] = info.get("heartbeat_s")
            item["listen_hung"] = hangs.get(node_id)
        return peers

    def network(self) -> dict[str, Any]:
        return disc.lan_report()

    def routes(self) -> dict[str, Any]:
        # ★ 2026-11-04（第 10 条）：这里必须走 `routes_json()`。
        #   面板的关系图读的就是这个接口，原来直接回 `node.routes`（原始配置，没有 stale 位），
        #   前端那句 `if (r.stale) return` 永远不成立 —— 幽灵节点和虚线于是一直画在图上。
        return {"routes": self.node.routes_json()}

    def purge_stale_routes(self) -> dict[str, Any]:
        """「清理失效路由」：只删已经被标失效的条目（端点长时间不存在）。"""
        return self.node.purge_stale_routes()

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

        def add(name: str, ok: bool | None, detail: str,
                summary: str | None = None, meta: dict[str, Any] | None = None) -> None:
            # ★ 2026-10-27：detail 给诊断/快速上手用；summary 给监控页主行用；
            #   meta 里放“历史规则”这类需要折叠的结构化数据，避免把几百条路径
            #   全塞进一行文字里。
            out["checks"].append({
                "name": name,
                "ok": ok,
                "detail": detail,
                "summary": detail if summary is None else summary,
                "meta": meta or {},
            })

        if sys.platform != "win32":
            add("防火墙", None, "只有 Windows 需要检查这一项")
            return out

        flags = getattr(sp, "CREATE_NO_WINDOW", 0)

        def ps(script: str) -> str:
            try:
                # ★ 2026-10-16 用户图1：规则里那些中文目录名（版本备份 / 成品）和网络位置名
                #   全显示成 "??" 方块 —— 就是这里原来写 `encoding="utf-8", errors="replace"`
                #   造成的：PowerShell 5.1 在中文 Windows 上输出的是 GBK 字节，按 UTF-8 解不出来
                #   就整片替换。改成**先按字节读回来**，再交给 decode_powershell 试 UTF-8 / 系统 ANSI。
                raw = sp.run(["powershell", "-NoProfile", "-Command", script],
                             creationflags=flags, capture_output=True,
                             timeout=25).stdout or b""
                return _decode_powershell(raw).strip()
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

        # 我们先确定"当前程序是谁"：防火墙规则绑的是程序路径，判定必须以它为准。
        #
        # ★ 2026-10-16 修正（用户图2 里两个永久红叉的根因）：这里原来按
        #   `Program -like '*avelink*'` 过滤程序路径 —— 改名成 Audio Mixing 之后，
        #   规则绑的程序路径里根本没有 "avelink" 这串字（连 "AudioMixing" 都不含它），
        #   所以**一条规则都匹配不到**，现场就是「入站 TCP 放行 ✗ / 入站 UDP 放行 ✗」
        #   永远红着，用户点多少次「放行防火墙」都不会变。
        #   现在按**规则名**取（新版 Audio Mixing* / 旧版 WaveLink*），并把绑定的
        #   程序路径一起读回来 —— 只有这样才能回答"覆盖的是不是当前这份 exe"。
        #
        # ★ 2026-10-27 用户：「用户如果和我一样拥有大量近似版本，应该也会这样子」。
        #   旧实现有两个问题：① 详情把每条规则都拼进一行，几百条旧版本路径铺满屏幕；
        #   ② tcp/udp 只看"任意一条历史规则"，旧版本规则会让主行显示“通过”，
        #   但当前这份 exe 实际没被放行。现在判定按当前路径 + Enabled + Profile，
        #   历史规则只聚合计数交给页面折叠，详情永远只写摘要。
        real = ""
        try:
            from .tray import _real_exe

            real = str(_real_exe() or "")
        except Exception:
            real = ""
        is_exe = real.lower().endswith(".exe")

        rules = ps(
            "Get-NetFirewallRule -DisplayName 'Audio Mixing*','WaveLink*' "
            "-ErrorAction SilentlyContinue | ForEach-Object { "
            "$p = $_ | Get-NetFirewallPortFilter; "
            "$a = $_ | Get-NetFirewallApplicationFilter; "
            "$prog = ''; if ($a) { $prog = [string]$a.Program }; "
            "if ($_.Direction -eq 'Inbound') { "
            "$_.DisplayName + '|' + $prog + '|' + $p.Protocol + '|' + "
            "$_.Action + '|' + $_.Enabled + '|' + $_.Profile } }")
        parsed: list[dict[str, str]] = []
        for raw in rules.splitlines():
            parts = [part.strip() for part in raw.split("|")]
            if len(parts) >= 5:
                parsed.append({"name": parts[0], "program": parts[1], "protocol": parts[2],
                               "action": parts[3], "enabled": parts[4],
                               "profile": parts[5] if len(parts) > 5 else ""})
        allowed = [r for r in parsed if r["action"].lower() == "allow"]

        # 当前的网络类型（Public / Private / Domain）。规则只覆盖 Domain 时不等于
        # 在公用 Wi-Fi 上放行；以前完全没看 Profile。
        current_profiles: set[str] = set()
        for line in prof.splitlines():
            if "|" not in line:
                continue
            category = line.rsplit("|", 1)[-1].strip().lower()
            if category == "domainauthenticated":
                category = "domain"
            if category in ("domain", "private", "public"):
                current_profiles.add(category)

        def rule_enabled(rule: dict[str, str]) -> bool:
            return str(rule.get("enabled") or "").strip().lower() in ("true", "1", "yes")

        def rule_profile_covers(rule: dict[str, str]) -> bool:
            text = str(rule.get("profile") or "").strip().lower()
            if not text or text == "any" or not current_profiles:
                return True
            parts = {part for part in text.replace(",", " ").split() if part}
            return bool(parts & current_profiles)

        def norm_program(value: str) -> str:
            text = str(value or "").strip().strip('"')
            if not text:
                return ""
            try:
                return os.path.normcase(os.path.normpath(os.path.expandvars(text)))
            except Exception:
                return os.path.normcase(text)

        current_key = norm_program(real)

        def is_current(rule: dict[str, str]) -> bool:
            return bool(is_exe and current_key
                        and norm_program(rule.get("program") or "") == current_key)

        current_rules = [r for r in allowed if is_current(r)]
        current_enabled = [r for r in current_rules if rule_enabled(r)]
        tcp = any(r["protocol"].upper() == "TCP" and rule_profile_covers(r)
                  for r in current_enabled)
        udp = any(r["protocol"].upper() == "UDP" and rule_profile_covers(r)
                  for r in current_enabled)

        # 历史规则：当前路径以外的全部规则。只保留计数 + 最多 5 条示例，
        # 不再把完整路径塞进 detail。
        historical = [r for r in allowed if not is_current(r)]
        history_rules: dict[tuple[str, str], dict[str, str]] = {}
        history_paths: set[str] = set()
        for rule in historical:
            program = norm_program(rule.get("program") or "")
            key = (rule["name"], program)
            if key not in history_rules:
                history_rules[key] = rule
            if program:
                history_paths.add(program)
        missing_files = sum(1 for path in history_paths if not os.path.isfile(path))
        examples: list[dict[str, Any]] = []
        for rule in list(history_rules.values())[:5]:
            program = str(rule.get("program") or "")
            examples.append({
                "name": rule["name"],
                "program": program,
                "protocol": rule["protocol"],
                "exists": bool(program and os.path.isfile(program)),
            })
        history_meta = {
            "total": len(historical),
            "unique_rules": len(history_rules),
            "unique_paths": len(history_paths),
            "missing_files": missing_files,
            "disabled": sum(1 for r in historical if not rule_enabled(r)),
            "examples": examples,
            "truncated": len(history_rules) > len(examples),
        }

        profile_cn = {"public": "公用", "private": "专用", "domain": "域"}
        profile_text = "、".join(profile_cn.get(p, p) for p in sorted(current_profiles)) or "当前网络"
        if not is_exe:
            rule_ok: bool | None = None
            summary = "脚本方式启动（不是 exe），防火墙规则只对 exe 有意义"
        elif tcp and udp:
            rule_ok = True
            summary = "当前程序已放行 TCP/UDP（%s）" % profile_text
        elif current_rules and not current_enabled:
            rule_ok = False
            summary = "当前程序的规则已被禁用"
        elif current_rules and not (tcp or udp):
            rule_ok = False
            summary = "当前程序的规则没有覆盖当前网络类型（%s）" % profile_text
        elif tcp or udp:
            rule_ok = False
            have = "TCP" if tcp else "UDP"
            lack = "UDP" if tcp else "TCP"
            summary = "当前程序只放行了 %s，%s 没放行" % (have, lack)
        elif parsed:
            rule_ok = False
            summary = "当前程序未放行"
        else:
            rule_ok = False
            summary = "没有任何和本程序有关的入站规则"

        history_text = ""
        if historical:
            history_text = "；历史规则 %d 条（%d 个路径，%d 个文件已不存在）" % (
                len(historical), len(history_paths), missing_files)
        detail = summary + history_text
        if rule_ok is False:
            detail += ("。点「放行防火墙」会清理旧版本规则，并为当前程序重建 TCP/UDP；"
                       "也可以右键托盘图标 →「放行防火墙（需管理员）」")
        add("入站规则（当前程序）", rule_ok, detail, summary=summary, meta={
            "current_path": real,
            "is_exe": is_exe,
            "covered": {"tcp": tcp, "udp": udp},
            "profiles": sorted(current_profiles),
            "rules_seen": len(parsed),
            "current_rules": [
                {"name": r["name"], "protocol": r["protocol"],
                 "profile": r["profile"], "enabled": rule_enabled(r)}
                for r in current_rules[:10]
            ],
            "history": history_meta,
        })

        # 正在跑的程序是哪一个：规则是绑**程序路径**的，换了目录/文件名旧规则就失效。
        #
        # ★ 2026-10-16 修正（同一个红叉的第二个根因）：原来靠
        #   `Get-Process -Name AudioMixing` 找正在跑的 exe，而桌面那份叫
        #   「Audio Mixing v1.27.exe」—— 进程名带空格又带版本号，按 AudioMixing
        #   **一个都匹配不到**，于是永远写「读不到」（用户截图就是这一行）。
        #   改成本进程**自己**的路径：tray._real_exe() 在 Nuitka onefile 下会去取
        #   __compiled__.original_argv0，也就是用户双击的那颗 exe，这才是"规则该覆盖
        #   哪一个程序"的正确答案。
        if is_exe:
            add("当前程序路径", True, real)
        elif real:
            # 措辞刻意避开"源码运行"四个字：诊断报告里那四个字有专门含义（见
            # test_diagnose_says_exe_not_source_when_frozen），这里说的是"这一份不是 exe"。
            add("当前程序路径", None,
                "脚本方式启动（不是 exe）：%s —— 防火墙规则只对 exe 有意义" % real)
        else:
            add("当前程序路径", False, "读不到")

        out["ok"] = all(c["ok"] for c in out["checks"] if c["ok"] is not None)
        return out


    def bluetooth_requests(self) -> list[dict[str, Any]]:
        """对端（手机）点过「走蓝牙」的记录 —— 面板 ⑦ 与诊断报告共用同一份。"""
        fn = getattr(self.node, "bluetooth_requests", None)
        return list(fn()) if callable(fn) else []

    # ★ 2026-11-04（第 11 条）：蓝牙卡片「本机播放」按钮与整条后端链路已删除
    #   （前端 btLocal() / POST /api/bluetooth/local / set_bluetooth_local_play）。
    #   驱动不出声时的手动兜底仍可用：「频道 → 音源」选中 `麦克风 (手机名 A2DP SNK)`，
    #   勾选「本机直通」即可，不需要这个按钮。

    def recover_audio(self) -> dict[str, Any]:
        """「恢复声音」：重开所有本机播放端 + 本机直通。

        ★ 2026-10-26 用户：其他终端（蓝牙/WiFi）连上后没声音，去 Windows 声音里
          切换几下输出才恢复，至少 7 个版本如此。程序现在会自动看门狗重开僵死
          的端点；这个接口是用户可点的兜底，作用等同手动切换一次输出。
        """
        return self.node.recover_audio()

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
        # 判据必须和面板/别处共用 _is_frozen()：Nuitka 不设 sys.frozen，
        # 曾经这里单看 sys.frozen，跑的是 exe 却报「源码运行」，与上方「版本 exe」矛盾。
        if self._is_frozen():
            exe = self._frozen_exe_path()
            lines.append("程序 %s" % (str(exe) if exe is not None else sys.executable))
        else:
            lines.append("程序 源码运行")
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

        # ★ 2026-10-26 用户现场：「双击后任务管理器里有两个 Audio Mixing，音频一卡一卡」。
        #   必须把"是不是两个实例"当场说清楚 —— 单文件打包的引导器本来就和真正的
        #   程序是两个进程，和"重复启动"是两回事。这段只读，不改任何东西。
        head("进程（任务管理器里那两个是什么）")
        lines.append("本进程 PID %d" % os.getpid())
        ppid, parent_exe = _parent_process_info()
        lines.append("父进程 PID %s%s" % (ppid or "?", ("　%s" % parent_exe) if parent_exe else ""))
        style = _packaging_style()
        lines.append("打包形态 %s" % style)
        if style.startswith("单文件"):
            lines.append("说明：单文件版启动时**本来就是两个进程**。父进程只负责把程序解包到")
            lines.append("      %TEMP%\\onefile_… 并等它结束，自己不碰音频；真正跑面板和音频的是")
            lines.append("      本进程。任务管理器「详细信息」里，路径在 Temp\\onefile_… 下的是子进程，")
            lines.append("      另一个是引导器。两个进程属正常，不会因此卡音频。")
            try:
                real = str(self._frozen_exe_path() or "")
            except Exception:
                real = ""
            if real and parent_exe and os.path.normcase(parent_exe) == os.path.normcase(real):
                lines.append("已核对：父进程就是本程序 exe（引导器），不是第二个 Audio Mixing 实例。")
        elif style.startswith("免解包"):
            lines.append("说明：免解包版只有一个 Audio Mixing 进程；如果任务管理器里还有第二个")
            lines.append("      同名进程，那才是重复启动 —— 请右键「结束任务」后重新打开。")

        # ★ 2026-10-26 用户：「部分 inter（Intel）电脑蓝牙没声音」；同一天又指出
        #   AMD 机器的诊断里只有「Intel 适配」，要求 AMD 也写清楚。CPU 厂商决定不了
        #   蓝牙模块：AMD 主板常见 MediaTek / Realtek / Qualcomm，Intel 机器也可能是
        #   别家模块；两边的现象和处置是一样的，所以这里按“实际适配器”说，不按 CPU 站队。
        head("适配（CPU / 蓝牙适配器）")
        vendor = _cpu_vendor()
        try:
            # ★ 这里必须用模块级别的 `bluetooth`，不能用 `bt`：同一个函数后面还有
            #   `from . import bluetooth as bt`，Python 会把整个函数里的 `bt` 当成
            #   局部变量；开头这句 `bt.radio_name()` 实际抛 UnboundLocalError，被
            #   except 吞掉 → 诊断永远写「蓝牙适配器 未知」（v2.7 起一直如此）。
            radio = bluetooth.radio_name()
        except Exception:
            radio = ""
        vlow = vendor.lower()
        cpu_label = ("Intel" if "genuineintel" in vlow else
                     "AMD" if "authenticamd" in vlow else (vendor or "未知"))
        lines.append("CPU %s（%s）" % (cpu_label, vendor or "厂商未知"))
        lines.append("蓝牙适配器 %s" % (radio or "未知（驱动没上报；不影响下面蓝牙段的判断）"))
        low = (vendor + " " + radio).lower()
        intel_bt = "intel" in low
        amd_cpu = "authenticamd" in vlow
        if intel_bt:
            lines.append("已知问题：部分 Intel 无线蓝牙驱动会「手机连接正常、媒体端点 active，但系统不出声」。")
            lines.append("  处置：设备管理器 → 蓝牙 → 卸载 Intel 蓝牙驱动 → 重启让 Windows 装通用驱动；")
            lines.append("        或更新/回退 Intel 蓝牙驱动后再试。")
            lines.append("  本工具兜底：手机媒体音频已到后，到「频道 → 音源」选中 "
                         "`麦克风 (手机名 A2DP SNK)` 并勾「本机直通」，由本工具采集并播放。")
        elif amd_cpu:
            lines.append("这是 AMD 平台。AMD 主板的蓝牙通常由板厂集成的模块提供（MediaTek / Realtek /")
            lines.append("Qualcomm 等），同样可能出现「手机连接正常、A2DP SNK 端点 active，但系统不出声」。")
            lines.append("  处置：先更新/重装主板或蓝牙模块驱动；仍无声就到「频道 → 音源」")
            lines.append("        选中 `麦克风 (手机名 A2DP SNK)` 并勾「本机直通」——")
            lines.append("        驱动不把端点列进 WASAPI 时，本工具会按注册表里的端点 id 直接打开它。")
        else:
            lines.append("不是 Intel 蓝牙；如果没声音，先看下面「蓝牙」段的媒体链路是否 active。")
        # ★ 老问题（用户说至少 7 个版本）：其他终端连上本机后没声音，去 Windows 声音里
        #   切换几下输出才恢复。下面这段把“程序自己已经在治”这件事写进报告，
        #   免得用户以为是随机故障。
        lines.append("跨终端无声：下面「播放端」段若出现「自动重开」> 0，就是本工具检测到端点僵死并")
        lines.append("  自行重建了 audio client；面板/托盘点「恢复声音」可立即重开全部播放端。")

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
            # ★ 2026-11-04（Q2）：用户"装了新 APK 但仍无效"时，第一件事就是确认
            #   手机上跑的到底是哪个版本、有没有把媒体音量报上来。老版本没有这两项，
            #   报告只能靠猜；现在直接写在「对端」一行里。
            # ★ v2.26 第 2 条：诊断要写清"vol 存到哪个键、查哪个键命中"，
            #   不能再把"未上报"一律归咎于"旧版不发"—— 2.14 是发 vol 的。
            vol = p.get("media_vol")
            keys = list(p.get("media_vol_keys") or [])
            hit = str(p.get("media_vol_key") or "")
            if vol is not None:
                vol_text = "%.2f（%s 秒前，命中键=%s）" % (
                    float(vol), p.get("media_vol_age_s"), hit or "?")
            else:
                vol_text = "未上报（已查 %d 个身份键未命中：%s）" % (
                    len(keys), "、".join(keys) or "无可用键")
            lines.append("%-14s host=%-16s 在线=%s 手动添加=%s 音频端口=%s 版本=%s 手机音量=%s id=%s" % (
                p.get("name"), p.get("host"), p.get("online"), p.get("manual"),
                p.get("audio_port"), p.get("version") or "?", vol_text, p.get("node_id")))
        vol_dbg = st.get("peer_volume_debug") or {}
        last_vol = vol_dbg.get("last") or {}
        if last_vol:
            lines.append("最近一次 vol 上报：node=%s host=%s vol=%s 存储键=%s（%s）" % (
                last_vol.get("node"), last_vol.get("host"), last_vol.get("vol"),
                "、".join(last_vol.get("keys") or []) or "无", last_vol.get("at")))
        elif vol_dbg:
            lines.append("最近一次 vol 上报：没有收到任何 vol（手机端可能还没开始发送，或旧版）")
        # ★ 2026-11-06（第 2 条）：把 vol 的**数值序列**写进诊断，直接回答
        #   "调 App 音量时手机上报的值到底动没动"。用户要的是证据，不是推断。
        for nid, series in (vol_dbg.get("series") or {}).items():
            samples = series.get("samples") or []
            seq = " → ".join("%.2f" % float(s.get("vol") or 0.0) for s in samples)
            lines.append("vol 序列 %s（%s）：%s　[样本 %s 个 · 最小 %.2f · 最大 %.2f · %s]" % (
                nid, series.get("host") or "?",
                seq or "（无）", series.get("count"), series.get("min"), series.get("max"),
                "有变化 ✓" if series.get("changed") else "一直没变 ✗（乘数没跟着 App 音量动）"))
        # ★ 2026-11-06（第 2 条）：手机 2.17+ 会把采集层的 Q14 乘数和增益前后峰值一起报上来。
        #   这行直接判定"乘法有没有生效"：post/raw 应当接近 total/16384。
        phone = (last_vol or {}).get("phone") or {}
        # ★ 2026-11-08（用户方案）：档位与乘数来源必须写在诊断里，用户一眼能看到
        #   "手机现在选的是哪一支、乘数从哪来"。老手机不发 vol_source 时留空，不猜。
        _src = str((last_vol or {}).get("vol_source") or "")
        _pol = str((last_vol or {}).get("vol_policy") or "")
        if _src == "external":
            mode_txt = "档位=外放档（手机已乘系统音量，电脑不叠加）"
        elif _src == "silent":
            mode_txt = "档位=静音档（手机不再叠加，电脑不叠加）"
        else:
            mode_txt = ""
        if _pol == "internal_only":
            mode_txt = (mode_txt + "　" if mode_txt else "") + "乘数来源=只用 App 内部音量（1.00）"
        elif _pol == "last_nonzero":
            mode_txt = (mode_txt + "　" if mode_txt else "") + "乘数来源=沿用进静音前的音量"
        if phone.get("vol_q14") is not None or phone.get("total_q14") is not None:
            q = float(phone.get("total_q14") or 0.0)
            raw = float(phone.get("raw_peak") or 0.0)
            post = float(phone.get("post_peak") or 0.0)
            ratio = (post / raw) if raw > 1e-4 else 0.0
            if raw > 1e-4 and q > 0:
                verdict = ("乘法生效 ✓（增益后/增益前 ≈ %.2f，应接近 %.2f）"
                           % (ratio, q / 16384.0)) if abs(ratio - q / 16384.0) <= 0.12 \
                    else ("乘法没生效 ✗（增益后/增益前 ≈ %.2f，但乘数应为 %.2f —— 有别的环节把它抹平了）"
                          % (ratio, q / 16384.0))
            else:
                verdict = "当前没有可比较的电平（原始峰值为 0）"
            lines.append("手机采集层：媒体音量 Q14=%s（=%.2f）· 本层增益 Q14=%s · 合成乘数 Q14=%s（=%.2f）"
                         "　原始峰值=%.3f → 增益后=%.3f　%s%s" % (
                             phone.get("vol_q14"), float(phone.get("vol_q14") or 0.0) / 16384.0,
                             phone.get("gain_q14"), q, q / 16384.0, raw, post, verdict,
                             ("　" + mode_txt) if mode_txt else ""))

        head("控制面（各走哪条路）")
        # ★ 2026-10-15 起控制消息分两条路（见 docs 与 net/discovery.py 开头）：
        #   对端信标里有 ctl:1 → 走【发现层】（那一层一直活着，接收器停着也收得到）；
        #   老版本对端不认这层外衣 → 只能走【音频端口】，而它的接收器是按需启停的，
        #   停下来的时候就收不到 —— 这正是"电脑送声音开着、手机的收听却不自动打开"的成因。
        #   把每台走哪条路如实写出来，用户不用再靠猜。
        ctl_stats = st.get("discovery") or {}
        lines.append("发现层控制消息：已发 %s 条 · 已收 %s 条%s" % (
            ctl_stats.get("ctl_sent", 0), ctl_stats.get("ctl_received", 0),
            ("　最后错误：%s" % ctl_stats.get("ctl_error")) if ctl_stats.get("ctl_error") else ""))
        # ★ 单播信标：手机端"经常丢失电脑端"就是因为电脑以前只发组播/广播（安卓省电会成批丢）。
        #   这个数一直停在 0 就说明这条路没在走 —— 一眼能看出来。
        lines.append("单播信标：已发 %s 份 · 这一轮 %s 台（手机端要靠它才不会丢设备）" % (
            ctl_stats.get("unicast_sent", 0), ctl_stats.get("unicast_targets", 0)))
        if not peers:
            lines.append("（没有对端，暂时没有控制消息可发）")
        for p in peers:
            lines.append("%-14s %s" % (
                p.get("name"),
                "走发现层（它信标里有 ctl:1 —— 接收器停着也收得到）" if p.get("ctl") else
                "只走音频端口（老版本对端：它的接收器一停就收不到控制消息）"))

        head("路由")
        routes = node.routes or []
        rewrites = ((st.get("identity") or {}).get("route_rewrites") or [])
        if rewrites:
            lines.append("身份归一改写记录（旧 id → 归一后的 device_id，最近 %d 条）：" % len(rewrites))
            for item in rewrites[-8:]:
                if item.get("old"):
                    lines.append("      %s %s: %s → %s（%s）" % (
                        str(item.get("route_id"))[:8], item.get("field"),
                        item.get("old"), item.get("new"), item.get("name") or item.get("host") or ""))
                else:
                    lines.append("      %s 合并重复路由：%s（保留 %s）" % (
                        str(item.get("route_id"))[:8], item.get("reason"),
                        str(item.get("kept"))[:8]))
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
            # ★ v2.26 第 1 条：诊断里直接对比"原始 token"和"identity 归一后的 id"，
            #   用户/我们下一次就能一眼看出还有没有 b1fbc20c 这种旧身份残留。
            try:
                frm_id = [node._display_identity_id(x) for x in frm]
                to_id = [node._display_identity_id(x) for x in to]
                if frm_id != frm or to_id != to:
                    lines.append("      身份归一：从=%s 到=%s（原始 从=%s 到=%s）" % (
                        frm_id, to_id, frm, to))
            except Exception as exc:
                lines.append("      身份归一：计算失败（%s）" % exc)

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
        recv_stats = self.streams().get("receiver") or {}
        denied = int(recv_stats.get("packets_denied") or 0)
        if denied:
            # ★ 2026-11-04（Q2）：未经接受的**点对点**单播不再被通配「只听」接住。
            #   这个数就是"被挡在门外"的证据；为 0 且听得到不该听的声音 → 说明对端
            #   没有上报 want_listen（老版本），只能按频道处理。
            lines.append("未接受的点对点包：已丢弃 %s 个（这些来源都没被本机接受；"
                         "频道广播按声明放行，点对点必须先在设备卡片开「收听」）" % denied)
        recv = recv_stats.get("streams") or []
        if not recv:
            lines.append("没有。对端没在发，或者路由的「到」没有指向本机。")
        for s in recv:
            packets = int(s.get("packets") or 0)
            lost = int(s.get("lost") or 0)
            total = packets + lost
            loss_pct = (lost * 100.0 / total) if total else 0.0
            lines.append("来自=%s　编码=%s　包=%s　字节=%s　丢包=%s（%.2f%%）" % (
                s.get("node_name"), s.get("codec"), packets, s.get("bytes"), lost, loss_pct))
            # ★ 2026-10-26：只听"包/字节"永远看不出"卡"在哪。把抖动缓冲自己的
            #   计数写全：下溢=真的补了静音，保持=用上一个采样补了几毫秒的小缺口，
            #   溢出/修剪=缓冲太深被丢掉，晚到包=整包到得太晚。
            # ★ 2026-11-03（Q2）：`late` 的代码语义是"整包到得晚、样本照播"，不是"丢音"。
            #   旧文案「迟到」让用户以为和"丢包"同义，和「溢出」一起看就像自相矛盾。
            #   统一叫「晚到包」，并在下面一行写明它不代表丢音（只代表时间线被推迟）。
            lines.append("      晚到包=%s　溢出=%s　修剪=%s　保持=%s　下溢=%s　重同步=%s" % (
                s.get("late"), s.get("overflow"), s.get("trimmed"), s.get("held_gaps"),
                s.get("underruns"), s.get("resyncs")))
            lines.append("      说明：晚到包=整包到得晚、样本仍会播，不代表丢音；"
                         "溢出/修剪=缓冲太深被丢掉，才真的丢样本")
            # ★ 2026-11-04（Q1）：`target` 是**目标**，不是下限。实际队列会围绕它上下浮动
            #   （用户现场 116.0 < 150.0 就是这么来的），旧文案写「最低」等于自相矛盾。
            lines.append("      缓冲(当前)=%.0fms / 目标=%.0fms（目标值不是下限，实际会上下浮动）　"
                         "收到音量=%.2f　来源音量=%.2f（这一路实际乘上的手机音量）　已播/已写=%s/%s 帧" % (
                float(s.get("depth_ms") or 0), float(s.get("target_ms") or 0),
                float(s.get("peak") or 0), float(s.get("media_vol", 1.0)),
                s.get("played"), s.get("written")))
            if s.get("clock_ready"):
                lines.append("      时钟=已同步（偏移 %.1fms，样本 %s）　晚到包=%.2f%%（累计）" % (
                    float(s.get("clock_ms") or 0.0), s.get("clock_samples", "?"),
                    float(s.get("late_pct") or 0.0)))
            else:
                lines.append("      时钟=未同步（包时间戳不在本机时间轴上，晚到包一律不计数）")

        head("播放端（本机往设备写）")
        sinks = st.get("sinks") or []
        if not sinks:
            lines.append("没有。收到数据但没有播放端，通常是路由对不上本机。")
        for item in sinks:
            dev = item.get("device") or {}
            ep = dev.get("endpoint_format") or {}
            name = dev.get("name") or dev.get("device") or item.get("device_selector") or "?"
            written = int(item.get("frames_written") or 0)
            lines.append("路由 %s → 设备 %s（选择器 %r）" % (
                str(item.get("route_id") or "")[:8], name, item.get("device_selector")))
            lines.append("      引擎：%s　已写 %s 帧（约 %.1f 秒）　峰值=%.3f　错误=%s" % (
                "运行中" if item.get("running") else "已停止", written,
                written / 48000.0, float(item.get("peak") or 0),
                item.get("error") or "无"))
            # ★ 2026-11-04（Q1）：让用户报这三个数就能定位"音量链"断在哪一环：
            #   ① 路由增益（面板滑杆）② 手机上报的媒体音量 ③ 实际写进设备的增益。
            #   若 ② 有值而 ③ 仍等于 ①，就是乘算没发生（不是"听不出来"）。
            vol_raw = item.get("media_vol")
            wildcard = str(item.get("route_id") or "") == "channel"
            # ★ 2026-11-06（Q1）：旧文案「× 手机音量 0.11 → 实际 1.00」读不通，因为
            #   0.11 是**手机**在采集端乘的，本机这一级只乘路由增益。现在把三个数
            #   各自标清是本机还是手机做的，用户不用再猜。
            lines.append("      音量链：本机路由增益 %.2f × 本机来源系数 %.2f = 本机实际 %.2f"
                         "　（手机上报音量 %s 已由手机乘进 PCM，本机不重复乘）%s%s" % (
                             float(item.get("route_gain") or 1.0),
                             float(item.get("volume_factor") or 1.0),
                             float(item.get("gain") or 0.0),
                             ("%.2f" % float(vol_raw)) if vol_raw is not None else "1.00（未上报）",
                             ("　vol 键=%s" % item.get("media_vol_source"))
                             if item.get("media_vol_source") else "",
                             "　（频道路由：手机音量按每条流各自乘，见上面接收端「来源音量」）"
                             if wildcard else ""))
            # ★ 2026-11-06（第 2 条）：把"这条链路到底乘了几次"写成独立一行。
            #   实际增益 = 路由增益 × 来源系数；来源系数为 1.0 表示已判定"手机采集端
            #   自己衰减过"，电脑不再乘第二次 —— 用户据此排除"被二次乘/被归零"。
            lines.append("      来源系数 %.2f（%s）" % (
                float(item.get("volume_factor") or 1.0),
                item.get("volume_note") or "按上报音量乘一次"))
            if dev.get("backend"):
                lines.append("      设备：%s　端点 %sHz/%s声道　缓冲总长 %sms　当前攒 %sms　"
                             "待写积压 %s 帧　重采样=%s" % (
                                 dev.get("backend"), ep.get("sample_rate") or "?",
                                 ep.get("channels") or "?", dev.get("capacity_ms") or 0,
                                 dev.get("buffer_ms") or 0, dev.get("backlog_frames") or 0,
                                 "是" if dev.get("resampled") else "否"))
                lines.append("      断音证据：设备放空 %s 次　设备满(不是断音) %s 次　"
                             "整块没塞进去 %s 次　丢弃 %s 帧" % (
                                 dev.get("starvations") or 0, dev.get("underruns") or 0,
                                 item.get("underruns") or 0, item.get("dropped_frames") or 0))
                lines.append("      设备实际写入 %s 帧（这个数停止增长 = 端点僵死，看门狗会重开）"
                             % (dev.get("frames_submitted") or 0))
                lines.append("      喂块节奏：最长间隔 %.1fms / 声卡缓冲 %.1fms　超过缓冲 %s 次（共 %s 块）" % (
                    float(item.get("max_block_gap_ms") or 0),
                    float(item.get("device_buffer_ms") or 0),
                    item.get("late_blocks") or 0, item.get("blocks") or 0))
                lines.append("      自动重开：%s 次%s" % (
                    item.get("recoveries") or 0,
                    ("（最近原因：%s）" % item.get("last_reopen_reason"))
                    if item.get("last_reopen_reason") else ""))
            else:
                lines.append("      设备：%s" % json.dumps(dev, ensure_ascii=False)[:200])
        if sinks:
            lines.append("判读：「设备放空」或「超过缓冲」大于 0，才是声卡这一侧真的断过；")
            lines.append("      两个都为 0、人耳仍觉得卡 → 看接收端的丢包/溢出/下溢，"
                         "并确认任务管理器里不是重复启动了第二个实例。")

        head("本机声音来源与直通")
        for item in (st.get("inputs") or []):
            lines.append("[%s] %-14s %s %s" % (
                item.get("path_label"), item.get("name"), item.get("state"),
                str(item.get("detail") or "")[:60]))
        lines.append("直通 " + json.dumps(st.get("local") or {}, ensure_ascii=False)[:200])

        # ★ 蓝牙这一段是 2026-10-15 用户报「测试7异常，听不到声音」时补的：
        #   以前报告里**完全没有**这一路的状态，只能靠用户描述，没法定位。
        #   现在把"电脑这边打开没打开 sink、手机的媒体链路到底有没有上来"写清楚。
        #   （2026-10-15 稍后：面板上的 ⑦ 已经并进 ②「设备与路由」里的那一段，
        #     所以这里不再写编号，改成把位置说清楚。）
        head("蓝牙（手机的声音经蓝牙进电脑 —— 面板在「设备与路由」里）")
        try:
            from . import bluetooth as bt

            if not bt.available():
                lines.append("这台机器不支持（不是 Windows，或找不到 powershell.exe）")
            else:
                devices = bt.cached_devices()
                if not devices:
                    lines.append("列表是空的：Windows 里还没配对过任何能送声音过来的手机")
                link_cn = {"connected": "正在接收手机声音",
                           "waiting": "手机已连接，但媒体音频未到",
                           "hfp": "只有通话通路（没有媒体声音）", "error": "出错", "off": "未连接"}
                auto_names = {str(x).strip().lower()
                              for x in (getattr(node.config, "autoconnect", None) or [])
                              if str(x).strip()}
                for dev in devices:
                    note = str(dev.get("state_note") or "").strip()
                    lines.append("%s｜%s｜state=%s｜电脑已打开sink=%s｜手机媒体链路=%s｜通话通路=%s%s" % (
                        link_cn.get(str(dev.get("link") or ""), str(dev.get("link") or "?")),
                        dev.get("name"), dev.get("state"), bool(dev.get("held")),
                        bool(dev.get("endpoint")), bool(dev.get("hfp")),
                        ("　（%s）" % note) if note else ""))
                    # ★ 2026-10-26：状态到底为什么是 False，必须能看到原始证据。
                    #   MMDevice 端点名 + DeviceState（1=active，8=未插入）直接写出来，
                    #   下次再出现"已经出声但报未到"时不用猜是名字没匹配还是真没到。
                    try:
                        eps = bt.endpoint_report(str(dev.get("name") or ""))
                    except Exception:
                        eps = []
                    if eps:
                        lines.append("      MMDevice 端点：%s" % "；".join(
                            "%s「%s」state=%s%s" % (
                                e.get("flow"), e.get("name"), e.get("state"),
                                "（active）" if e.get("active") else "（未激活）")
                            for e in eps[:6]))
                    else:
                        lines.append("      MMDevice 端点：没找到含「%s」的端点" % dev.get("name"))
                        try:
                            all_bt = bt.endpoint_report("")
                        except Exception:
                            all_bt = []
                        if all_bt:
                            lines.append("      本机所有 A2DP/SNK 端点：%s" % "；".join(
                                "%s「%s」state=%s" % (e.get("flow"), e.get("name"), e.get("state"))
                                for e in all_bt[:8]))
                    lines.append("      蓝牙智连：%s" % (
                        "开（启动时自动尝试连接一次，播放交给 Windows）"
                        if str(dev.get("name") or "").strip().lower() in auto_names else "关"))
                    lines.append("      听不到声音时：到「频道 → 音源」选中这块 A2DP SNK 并勾「本机直通」")
        except Exception as exc:
            lines.append("读取失败：%s" % exc)
        for req in (node.bluetooth_requests() if hasattr(node, "bluetooth_requests") else []):
            lines.append("对端请求走蓝牙（手机点了「走蓝牙」）：%s → %s%s" % (
                req.get("name"), req.get("state") or "?",
                ("　" + str(req.get("error"))) if req.get("error") else ""))

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

    def diagnose_bundle(self) -> tuple[bytes, str]:
        """一键诊断包：把报告、状态、配置、设备/蓝牙端点和日志打成一个 zip。

        ★ 2026-10-26 用户：「下次直接生成诊断包吧，老是复制好麻烦」。
          以前只能把一屏文字全选复制，真机上很难操作，还容易漏行。现在面板、
          托盘和诊断页都提供下载，用户只需要把 zip 发回来。
        """
        import io
        import platform
        import zipfile

        from .config import config_dir

        report = self.diagnose()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        build: dict[str, Any] = {}
        try:
            build = self.build_info()
        except Exception as exc:
            build = {"error": "%s: %s" % (type(exc).__name__, exc)}
        status: dict[str, Any] = {}
        try:
            status = self.status()
        except Exception as exc:
            status = {"error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            cfg = self.node.config.to_json()
        except Exception as exc:
            cfg = {"error": "%s: %s" % (type(exc).__name__, exc)}
        devices = []
        try:
            devices = [d.to_json() for d in (getattr(self.node, "devices", None) or [])]
        except Exception as exc:
            devices = [{"error": "%s: %s" % (type(exc).__name__, exc)}]
        bt_info: dict[str, Any] = {}
        try:
            bt_info = {
                "available": bluetooth.available(),
                "radio": bluetooth.radio_name(),
                "devices": bluetooth.cached_devices(),
                "endpoints": bluetooth.endpoint_report(""),
            }
        except Exception as exc:
            bt_info = {"error": "%s: %s" % (type(exc).__name__, exc)}
        env_lines = [
            "Audio Mixing 诊断包",
            "生成时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "版本 %s" % (build.get("text") or "?"),
            "构建时间 %s" % (build.get("built_at") or "?"),
            "打包形态 %s" % _packaging_style(),
            "系统 %s" % platform.platform(),
            "Python %s" % sys.version.replace("\n", " "),
            "PID %d" % os.getpid(),
            "程序 %s" % (self._frozen_exe_path() if self._is_frozen() else "源码运行"),
        ]
        try:
            env_lines.append("配置目录 %s" % config_dir())
        except Exception:
            pass
        node_log = "\n".join(str(x) for x in (getattr(self.node, "log", None) or [])[-4000:])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("诊断报告.txt", report.get("text") or "")
            zf.writestr("环境.txt", "\n".join(env_lines) + "\n")
            zf.writestr("运行日志.txt", node_log)
            zf.writestr("build.json", json.dumps(build, ensure_ascii=False, indent=2, default=str))
            zf.writestr("status.json", json.dumps(status, ensure_ascii=False, indent=2, default=str))
            zf.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=2, default=str))
            zf.writestr("devices.json", json.dumps(devices, ensure_ascii=False, indent=2, default=str))
            zf.writestr("bluetooth.json", json.dumps(bt_info, ensure_ascii=False, indent=2, default=str))
            # launcher.log 是排查启动/单实例问题最关键的一份，放在最外层容易找。
            log_paths: list[str] = []
            try:
                log_paths.append(str(config_dir() / "launcher.log"))
            except Exception:
                pass
            for base in (os.environ.get("AUDIOMIXING_HOME") or os.environ.get("WAVELINK_HOME"),
                         os.path.join(os.environ.get("APPDATA") or "", "AudioMixing"),
                         os.path.expanduser("~")):
                if base:
                    log_paths.append(os.path.join(str(base), "launcher.log"))
            seen: set[str] = set()
            index = 0
            for path in log_paths:
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = os.path.getsize(path)
                    with open(path, "rb") as fh:
                        if size > 1024 * 1024:
                            fh.seek(size - 1024 * 1024)
                        data = fh.read()
                except OSError:
                    continue
                index += 1
                zf.writestr("launcher%s.log" % ("" if index == 1 else "-%d" % index), data)
            zf.writestr("怎么发.txt",
                        "把整个 zip 发回给开发者即可。\n"
                        "里面包含：诊断报告、运行状态、配置、设备与蓝牙端点、启动日志。\n"
                        "不包含任何音频内容。\n")
        return buf.getvalue(), "AudioMixing-诊断包-%s.zip" % stamp

    def monitor(self) -> dict[str, Any]:
        """一条一条的流向：谁给谁，走哪个通道，多少数据，延迟多少。

        这里是给监控窗用的，只报能测到的东西。测不到的宁可写清楚，不编数字：
        Windows 的蓝牙 A2DP sink 是一块**采集设备**（麦克风 (<手机> A2DP SNK)），
        只有把这块设备加进「频道 → 音源」并播放之后，本工具才有数据可报；
        未加入时只报链路状态，不假装正在播放。
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
                # ★ 2026-10-17 用户（图1）：「我们的每一个声音都应当有断开选项」。
                #   这一行是手机经 Wi-Fi 送进来、正在本机播放的声音，原来只有数字没有出口。
                #   这里给的是【本机侧的断开】：把这台手机的采集设备静音，本机立刻不再播放它
                #   （手机那边照常发送，它自己的「送声音」开关我们不去动 —— 跨端改别人的设置
                #   正是上一轮用户明确否掉的做法）。id 单独放 disconnect_id，不占用 route_id，
                #   免得把"已配置但当前没有数据"那一行的去重逻辑搅乱。
                "disconnect": "mic",
                "disconnect_id": name,
                "hint": "断开这台手机经 Wi-Fi 送来的声音（本机不再播放它，手机那边照常发送）",
            })
        self._mon_prev = current_prev

        # 手机经蓝牙进来：Windows 把它暴露成一块 A2DP 采集设备（麦克风 (<手机> A2DP SNK)）。
        # 这里只报**链路和媒体音频的状态**；有没有真正播放，取决于它有没有被加进音源。
        try:
            from . import bluetooth as bt

            for device in bt.cached_devices():
                link = str(device.get("link") or "")
                # ★ 2026-10-21：不能只在 connected 时显示。用户现场是“手机上连着、
                #   但媒体音频没送过来”，这时监控页整行消失，用户以为蓝牙断了/程序丢了。
                #   waiting / hfp 也要留一行，写清“为什么现在没有声音”和下一步怎么做。
                if link not in ("connected", "hfp", "waiting"):
                    continue
                phone = str(device.get("name") or "")
                if link == "connected":
                    state = "手机媒体音频已到（系统播放）"
                    detail = ("本机由 Windows 直接播放，不需要再加音源。要把手机声音转发到别的设备/频道，"
                              "才到控制台「频道 → 音源」选中「麦克风 (%s A2DP SNK)」并勾「广播我的声音」；"
                              "「本机直通」会和系统播放重复，慎用。" % phone)
                elif link == "waiting":
                    state = "手机已连接，但媒体音频未到"
                    detail = ("现在没有声音：请在手机蓝牙设置里确认「媒体音频」已打开，"
                              "并在手机上播放声音、把输出选成这台电脑；改完把手机蓝牙关一次再开，"
                              "或点蓝牙卡片的「刷新」。")
                else:
                    state = "手机只连了通话(HFP)，没有媒体音频"
                    detail = ("请在手机蓝牙设置里把这台电脑的「媒体音频」打开"
                              "（同时确认媒体没有走到别的蓝牙耳机），再把手机蓝牙关一次再开。")
                flows.append({
                    "from": phone,
                    "to": "本机",
                    "channel": "蓝牙",
                    "state": state,
                    "bytes": None,
                    "rate_kbps": None,
                    "latency_ms": None,
                    "detail": detail,
                    # ★ 2026-10-16 用户（图3）：「蓝牙模式缺少断开按钮」。
                    #   这一行原来只有状态、没有可操作的按钮，用户想断蓝牙只能去系统设置里找。
                    #   设备 id 就是 bluetooth.disconnect() 要的那个 —— /api/monitor/disconnect
                    #   的 bluetooth 分支早就有，只是没人给它填这两格。
                    "disconnect": "bluetooth",
                    # ★ v2.26 第 3 条：合并后的卡有多条历史配对 id，断开要把它们全断掉，
                    #   否则另一条历史记录还会把链路拉起来。用逗号串起来交给 monitor_disconnect。
                    "route_id": ",".join(str(x) for x in (device.get("ids") or [device.get("id")]) if x),
                    "hint": "断开这台手机的蓝牙音频（电脑这边松手，手机会回到自己的扬声器）",
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
                "hint": "删掉这条路由（本机不再送给它）",
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
            # ★ 2026-11-03（图3）：先写清统计口径，避免"累计 9.2MB / 0KB/s"看起来互相矛盾。
            if stream.get("seconds") is not None:
                bits.append("自开始 %s 秒（字节/计数器均为累计）" % stream.get("seconds"))
            if stream.get("depth_ms") is not None:
                bits.append("缓冲 %s 毫秒" % stream.get("depth_ms"))
            if stream.get("target_ms") is not None:
                # ★ 2026-11-04（Q1）：目标值不是"最低"，实际缓冲可以短时低于它。
                bits.append("目标 %s 毫秒（不是下限）" % stream.get("target_ms"))
            if stream.get("clock_ready"):
                bits.append("时钟已同步 %s 毫秒" % stream.get("clock_ms"))
            else:
                bits.append("时钟未同步（晚到不计数）")
            for label, field in (("裁剪", "trimmed"), ("欠载", "underruns"),
                                 ("晚到包", "late"), ("溢出", "overflow"),
                                 ("重同步", "resyncs")):
                value = stream.get(field)
                if value:
                    bits.append("%s %s" % (label, value))
            if stream.get("late_pct"):
                bits.append("晚到 %.2f%%" % stream.get("late_pct"))
            # ★ 2026-10-17 用户（图1）：「每一个声音都应当有断开选项」—— 收进来的这一路
            #   原来只有一串数字，没有任何出口。路由 id 是【本机的】route id（接收端按
            #   desired_sinks() 的键给每条流打标，见 engine.assign_routes），所以能直接拿来断：
            #     · 有具名路由（点对点）→ 静音这条路由，本机立刻不再播放它；
            #     · 只剩频道那条（route_id 以 channel 开头，或根本没挂上任何路由）→ 关掉
            #       「听广播」这个开关（频道规则是引擎合成出来的、不落盘，update_route 改不到它）。
            #   route_id 可能是 "a,b" 这种多路由拼接，所以按逗号拆开再挑具名的那条。
            stream_routes = [x for x in str(stream.get("route_id") or "").split(",") if x]
            named_routes = [x for x in stream_routes if not x.startswith("channel")]
            row: dict[str, Any] = {
                "from": str(stream.get("node_name") or stream.get("source") or "其它电脑"),
                "to": "本机",
                # 带上 route_id：这样"已配置但当前没有数据"那一行才能认出这条路由
                # 其实正在收数据，不会再多显示一行（2026-10-12 修）。
                "route_id": str(stream.get("route_id") or ""),
                # ★ 2026-10-20 用户：「我手机用的点对点，这边监控怎么是点对点/广播」——
                #   老注释说"协议里不带这个信息所以不猜"，其实**归属已经算出来了**（上面 named_routes）：
                #   命中的是具名路由 = 点对点；只落进频道（通配）那条 = 广播。
                #   一律写"点对点/广播"只会让用户以为把点对点当成广播处理了。
                "channel": "点对点" if named_routes else "广播",
                # ★ 2026-11-03（图3）：状态原来写死"正在接收"，只要缓冲区对象还在就是
                #   正在接收，哪怕已经 N 秒没有新包。改成由"最近一次真实收到音频的时间"决定。
                "state": ("正在接收" if stream.get("receiving")
                          else "已停止（%s 秒无音频）" % stream.get("age_s", "?")),
                "bytes": int(stream.get("bytes") or 0),
                "rate_kbps": round(float(stream.get("kbps") or 0.0), 1),
                # 延迟只对"还在收"的流有意义；断流后残留的 depth 只会误导。
                "latency_ms": (round(float(stream.get("depth_ms") or 0.0), 1)
                               if stream.get("receiving") else None),
                "detail": "　".join(bits) or "丢包 %s 晚到包 %s" % (
                    stream.get("lost", 0), stream.get("late", 0)),
            }
            if named_routes:
                row["disconnect"] = "route"
                row["disconnect_id"] = named_routes[0]
                row["hint"] = "删掉这条路由：本机不再播放这台设备送来的声音"
            else:
                row["disconnect"] = "listen"
                row["hint"] = "停止收听广播：本机不再收听广播里的声音（广播方不受影响）"
            flows.append(row)

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

            # ★ v2.26 第 1 条：监控页也要用 identity 归一后的结果，并且**不允许**再出现
            #   "不在线 / 不可达"这种既不能操作、也不能清理的裸行。
            #   三种状态各有出口：
            #     missing  → "失效 · 设备身份已不存在（可清理）"，按钮写成"清理失效路由"；
            #     offline  → "对端离线 · 路由保留"，按钮"删除这条路由"（不想等就删）；
            #     no_data  → "已配置 · 当前没有数据"，按钮"断开"（= 删路由）。
            route_states = {str(r.get("id") or ""): r for r in (status.get("routes") or [])}
            # ★ v2.26 第 1 条（③）：没有强证据自动改写的失效路由，如果当前**恰好只有一台**
            #   在线对端，就提供一个"一键修复"按钮（点它 = 用户确认就是这台），
            #   而不是只给"清理"一个出路。
            online_peers = {}
            for p in (status.get("peers") or []):
                if not p.get("online") or p.get("manual"):
                    continue
                canonical = str(p.get("device_id") or p.get("node_id") or "")
                if canonical:
                    online_peers[canonical] = p
            repair_peer = next(iter(online_peers.values())) if len(online_peers) == 1 else None
            for rule in (node.routes or []):
                rid = str(rule.get("id") or "")
                if not rid or rid in shown:
                    continue
                info = route_states.get(rid) or rule
                src_ids = rule.get("from") or rule.get("source") or []
                dst_ids = rule.get("to") or rule.get("target") or []
                if isinstance(src_ids, str):
                    src_ids = [src_ids]
                if isinstance(dst_ids, str):
                    dst_ids = [dst_ids]
                # 归一后的 id 只用来判"还是不是同一台"；名字仍由 _name_of 解析成设备名。
                src_shown = "、".join(_name_of(x) for x in src_ids) or "?"
                dst_shown = "、".join(_name_of(x) for x in dst_ids) or "?"
                state = str(info.get("state") or "no_data")
                if state == "missing":
                    state_label = "失效 · 设备身份已不存在（可清理）"
                    detail = ("这条路由指向的旧设备（%s → %s）已经不在这台电脑的设备列表里，"
                              "不会再有任何声音。点右边按钮只删掉本机这条失效路由。"
                              % (src_shown, dst_shown))
                    if repair_peer:
                        detail += ("如果它就是当前在线的「%s」，点「改指向…」即可（会先备份配置）。"
                                   % repair_peer.get("name"))
                    action = "清理失效路由"
                    hint = "删掉这条失效路由（只删本机这条配置，不会动 Windows 蓝牙配对）"
                    repair = bool(repair_peer)
                    repair_action = ("改指向「%s」" % repair_peer.get("name")
                                     if repair_peer else "")
                elif state == "offline":
                    state_label = "对端离线 · 路由保留（等它回来）"
                    detail = ("对端暂时不在线（关机、换网或蓝牙断开）。路由会保留，"
                              "它回来以后会自动恢复；不想等就点右边删除。")
                    action = "删除这条路由"
                    hint = "删掉这条路由（两台都不再收发；对方那台也会清掉这一条）"
                else:
                    state_label = "已配置 · 当前没有数据"
                    detail = "这条路由已经建好了，但现在没有数据在流（对端没在发，或还没开始）"
                    action = "断开"
                    hint = "删掉这条路由（两台都不再收发；对方那台也会清掉这一条）"
                    repair = False
                    repair_action = ""
                flows.append({
                    "route_id": rid,
                    "disconnect": "route",
                    "disconnect_id": rid,
                    "hint": hint,
                    "action": action,
                    "repair": repair,
                    "repair_action": repair_action,
                    "from": src_shown,
                    "to": dst_shown,
                    "from_ids": [str(x) for x in src_ids],
                    "to_ids": [str(x) for x in dst_ids],
                    "channel": "点对点",
                    "state": state_label,
                    "bytes": 0,
                    "rate_kbps": 0.0,
                    "latency_ms": None,
                    "detail": detail,
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
            # ★ 2026-10-16：这里原来只取 local.*（本机直通那一路）。而手机走蓝牙进来时
            #   声音是 Windows 直接播放的，本机直通根本没有数据 → 监控页脚那行
            #   「本机输出电平」永远是 0。改用和面板同一个口径（output_level()）。
            "local_peak": self.node.output_level().get("peak"),
            "node": str(getattr(node, "name", "")),
        }

# HWHHYN 用 DEEPSEEK 制作

    def monitor_disconnect(self, kind: str, target: str = "") -> tuple[int, dict[str, Any]]:
        """监控页每一行的「断开」，按 kind 分派，返回 (HTTP 码, 响应体)。

        ★ 2026-10-17 用户（图1）：「我们的每一个声音都应当有断开选项（WiFi/蓝牙/广播）」。
        原来只有 broadcast / route / bluetooth 三种，而且**在 GET 和 POST 两个分支里各抄了一份**
        一模一样的判定 —— 补一种就得改两处、必然漏一处（"放行防火墙"那个按钮就是只写进 GET
        分支、点下去拿 404 才发现的）。所以收到 Api 这一层统一实现，页面只做一层转发，
        测试也能直接考它，不用为了测四种 kind 去起一个 HTTP 服务。

        kind 一览：
          broadcast  停掉本机的「广播我的声音」
          listen     关掉本机的「听广播」（频道那条规则由引擎合成、不落盘，改不到它，只能改开关）
          route      删掉某条路由（点对点：两台都不再收发；与设备卡片"再点一次就断开"同一个意思）
          mic        静音某台手机的 Wi-Fi 音源（本机不再播放它；手机那边照常发送，不去动别人的设置）
          bluetooth  断开某台手机的蓝牙音频（电脑这边松手）
        """
        if kind == "broadcast":
            self.set_settings({"broadcast": False})
            return 200, {"ok": True, "note": "已停止广播"}
        if kind == "listen":
            self.set_settings({"listen": False})
            return 200, {"ok": True, "note": "已停止收听广播"}
        if kind == "repair" and target:
            # ★ v2.26 第 1 条：失效路由的"一键修复"——点它等于用户确认目标设备，
            #   把旧 id 改写为当前对端归一后的 device_id（改写前已备份 config.json）。
            result = self.node.repair_route_identity(str(target))
            return 200, result
        if kind == "route" and target:
            # ★ 2026-10-18 用户（图1）：「点对点没有"断开"。你忘加了。」
            #   顺带把语义纠成一个：这条路由上的「断开」= **删掉这条路由**，不是"静音"。
            #   设备卡片上的「收听它的 / 送给它」本来就是"再点一次就断开"= 删掉；监控页却只静音，
            #   于是用户点了"断开"，那条路由还在（仍然显示"已配置 · 当前没有数据"）——
            #   又是一次"点了没反应"。两处必须同一个意思。
            self.delete_route({"id": target})
            return 200, {"ok": True, "note": "已删掉这条路由"}
        if kind == "mic" and target:
            from .audio.backends import netmic as netmic_backend

            if not netmic_backend.set_muted(target, True):
                # 设备已经不在了（手机停了送声音、窗口关了）→ 目标其实已经达成，不算失败。
                return 200, {"ok": False, "note": "这台手机现在没有在送声音"}
            return 200, {"ok": True, "note": "已断开 %s 经 Wi-Fi 送来的声音" % target}
        if kind == "bluetooth" and target:
            from . import bluetooth as bt

            # ★ v2.26 第 3 条：target 可能是合并卡的多条历史配对 id（逗号分隔）。
            results = [bt.disconnect(part) for part in str(target).split(",") if part]
            ok = any(bool(item.get("ok")) for item in results) or not results
            note = "；".join(str(item.get("state") or item.get("error") or "")
                            for item in results if item.get("state") or item.get("error"))
            return 200, {"ok": ok, "note": note}
        return 400, {"error": "不支持的断开类型: %r" % kind}

    def streams(self) -> dict[str, Any]:
        stats = self.node.receiver.stats()
        captures = self.node.hub.stats()
        return {"receiver": stats, "captures": captures, "senders": self.node.sender.stats()}

    def logs(self) -> dict[str, Any]:
        node = self.node
        extra = getattr(self, "broadcast_log", [])
        return {"log": node.log[-200:], "last_error": node.last_error, "broadcast": extra[-50:]}

    def _note_manual_auto_close(self, removed: list[dict[str, Any]]) -> None:
        """★ 2026-11-04（A4）：用户手动删掉"收听"路由 → 同时关掉那台设备的智连。

        只处理**收听方向**（from=对端、to=本机）；「我送它的」自动路由被删只是停止
        送声，不代表用户想关自己的智连，不能跟着动。

        ★ v2.27（Bug-3.1）：这里不再要求"这条是自动路由"。用户删掉**任何**一条
        收听路由（自动的、手工的、或 v2.27 收敛后保留下来的手工那条）都表示"我不要
        再听这台"；如果只删路由、智连还开着，下一包统计（约 2 秒）就会把自动路由
        又建回来 —— 现场表现就是"取消收听要点两次"。删一次就把智连关掉，才真的停下。
        """
        mine = {str(self.node.node_id).strip().lower(), str(self.node.name).strip().lower()}

        def _is_me(token: str) -> bool:
            return str(token or "").strip().lower() in mine

        for rule in removed or []:
            try:
                frm = [str(t) for t in rule.get("from") or []]
                to = [str(t) for t in rule.get("to") or []]
                if not any(_is_me(t) for t in to):
                    continue
                for token in frm:
                    if token and not _is_me(token):
                        self.node.note_manual_listen_off(token)
                        break
            except Exception as exc:
                self.node.note("手动关闭收听时处理路由失败：%s" % exc)

    # -- POST --------------------------------------------------------------
    def set_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
        routes = payload.get("routes")
        if routes is None and isinstance(payload.get("route"), dict):
            routes = [payload["route"]]
        if not isinstance(routes, list):
            raise ApiError("body must contain a 'routes' list")
        if len(routes) > 200:
            raise ApiError("too many routes (max 200)")
        # ★ 2026-11-01：这里原来写的是 `self.node.note_local_gain_change(route_id)`，
        #   可 `route_id` 在这个函数里根本没定义 —— payload 一带 "gain" 就 NameError。
        #   而且"整表保存"可能一次改好几条，正确做法是**对比改前改后**，把真的变了的
        #   每条都记一笔（共享增益判"谁最后动"要用它）。
        # ★ 2026-11-04（A4）手动优先：整表保存时，先把"用户删掉的自动收听路由"
        #   对应的智连按钮关掉，否则下一包音频会把路由又自动建回来（你越关它越开）。
        new_ids = {str(r.get("id") or "") for r in routes if isinstance(r, dict)}
        self._note_manual_auto_close(
            [r for r in self.node.routes if str(r.get("id") or "") not in new_ids])
        before = {str(r.get("id") or ""): r.get("gain") for r in self.node.routes}
        applied = self.node.set_routes(routes)
        for rule in applied:
            rid = str(rule.get("id") or "")
            if rid not in before:
                continue
            try:
                changed_gain = abs(float(before[rid]) - float(rule.get("gain", 1.0))) >= 0.005
            except (TypeError, ValueError):
                changed_gain = True
            if changed_gain:
                self.node.note_local_gain_change(rid)
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
        gain_changed = False
        # Any panel may adjust any route on the LAN - the volume slider is the
        # everyday case, and the node doing the playing is often not the node that
        # created the route. Ownership is preserved, so the author can still edit it.
        for existing in self.node.routes:
            if existing["id"] == route_id:
                merged = dict(existing)
                merged.update({k: v for k, v in payload.items() if k != "id"})
                routes.append(merged)
                found = True
                if "gain" in payload:
                    try:
                        gain_changed = abs(float(existing.get("gain", 1.0))
                                           - float(merged.get("gain", 1.0))) >= 0.005
                    except (TypeError, ValueError):
                        gain_changed = True
            else:
                routes.append(existing)
        if not found:
            raise ApiError("no such route: %s" % route_id, 404)
        # ★ 2026-11-01 用户：「电脑端调节音量条手机端不同步」。
        #   页面拖滑杆走的是这个接口，而旧实现**没有**记"本机用户刚动过"。
        #   于是对端每 2 秒照回来的旧值被当成"最后到达的"采纳，把电脑刚拖到的位置
        #   在两秒内改回去 —— 手机自然停在旧值，电脑自己也弹回。
        if gain_changed:
            self.node.note_local_gain_change(route_id)
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
        doomed = [r for r in self.node.routes if r["id"] == route_id]
        # ★ 2026-11-04（A4）：用户手动关掉自动打开的收听 → 同时关掉那台设备的智连，
        #   并等下一次人工打开才恢复。必须在删除前做（见 _note_manual_auto_close）。
        self._note_manual_auto_close(doomed)
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

    def identity_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """★ 2026-11-04（B 第一阶段）面板上"疑似同一台设备，是否合并？"的人工确认入口。

        请求体：{"keep": "<保留的 node_id/device_id>", "drop": "<被并掉的>"}。
        **不存在自动调用**：辅助证据（同一蓝牙 MAC + 同一 AP）只写进 status.identity.hints，
        人点了这个接口才合并；合并后旧 id 全部降级成 aliasIds，路由一条都不删。
        """
        keep = str(payload.get("keep") or "").strip()
        drop = str(payload.get("drop") or "").strip()
        if not keep or not drop:
            raise ApiError("identity merge 需要 keep 和 drop 两个设备标识", 400)
        result = self.node.identity_merge(keep, drop)
        if not result.get("ok"):
            raise ApiError(str(result.get("reason") or "合并失败"), 404)
        return result

    def relation_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """★ v2.27（用户第 6 条）：关系图里拖动两个气泡重叠 → 确认后合并。"""
        a = str(payload.get("a") or "").strip()
        b = str(payload.get("b") or "").strip()
        if not a or not b:
            raise ApiError("relation merge 需要 a 和 b 两个气泡标识", 400)
        result = self.node.relation_merge(a, b, str(payload.get("a_name") or ""),
                                          str(payload.get("b_name") or ""))
        if not result.get("ok"):
            raise ApiError(str(result.get("reason") or "合并失败"), 400)
        return result

    def relation_split(self, payload: dict[str, Any]) -> dict[str, Any]:
        """★ v2.27（用户第 6 条）：合并后的「拆开」入口。"""
        key = str(payload.get("key") or payload.get("node_id") or payload.get("bt") or "").strip()
        if not key:
            raise ApiError("relation split 需要 key", 400)
        result = self.node.relation_split(key, str(payload.get("name") or ""))
        if not result.get("ok"):
            raise ApiError(str(result.get("reason") or "拆开失败"), 404)
        return result

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

    # ── 端口保存后自动重启（2026-11-01 用户要求）────────────────────────────
    @staticmethod
    def _strip_port_args(argv: list[str]) -> list[str]:
        """去掉命令行里的三个端口参数，让重启后的进程从**新配置**读端口。

        只用于 ``exe serve`` 这种调试/脚本启动方式；用户双击的正常形态没有参数。
        支持 ``--web-port 50930`` 和 ``--web-port=50930`` 两种写法。
        """
        out: list[str] = []
        skip = False
        for item in argv:
            if skip:
                skip = False
                continue
            if item in ("--web-port", "--audio-port", "--discovery-port"):
                skip = True
                continue
            if item.startswith(("--web-port=", "--audio-port=", "--discovery-port=")):
                continue
            out.append(item)
        return out

    def _schedule_restart(self) -> tuple[bool, str]:
        """保存端口后，等本进程退出再由一个独立进程把同一个 exe 拉起来。

        为什么要等退出：端口在进程启动时绑死（尤其是面板端口 / 手机长连接端口
        web+1），不退出就不可能换端口。为什么要独立进程：本进程一退，
        单实例互斥体才释放；先起新进程会被它自己判定"已有实例"而退出。

        只在**打包后的 exe** 且调用方明确给了 `restart_argv` 时执行；
        源码运行 / 单元测试 / 双节点自检 `restart_argv=None` → 直接返回 blocked。
        返回值第二位是**为什么没重启**（写进接口返回值，真机上一眼就能查）。
        """
        if self.restart_argv is None:
            return False, "not-enabled"
        if os.name != "nt":
            return False, "not-windows"
        try:
            # ★ 2026-11-01：`_is_frozen` 是 **AudioMixingApi** 的方法（和 build_info 同一份口径）；
            #   写成 WebUI._is_frozen() 会 AttributeError → 被下面吞成 not-frozen，
            #   真机上表现为"保存成功但从不自动重启"。必须用 self。
            if not self._is_frozen():
                return False, "not-frozen"
        except Exception:
            return False, "not-frozen"
        argv = list(self.restart_argv)
        if argv and argv[0] != "serve":
            return False, "unsupported-argv"
        try:
            from .tray import _real_exe

            exe = str(_real_exe() or "")
        except Exception:
            exe = ""
        if not exe or not os.path.isfile(exe):
            return False, "exe-not-found"
        args = self._strip_port_args(argv) if argv else []
        cwd = os.path.dirname(exe) or os.getcwd()

        def psq(value: str) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        start = "Start-Process -FilePath %s -WorkingDirectory %s" % (psq(exe), psq(cwd))
        if args:
            start += " -ArgumentList @(%s)" % ",".join(psq(a) for a in args)
        script = (
            "$targetPid = %d; "
            "while (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) "
            "{ Start-Sleep -Milliseconds 250 }; "
            "Start-Sleep -Milliseconds 400; %s"
        ) % (os.getpid(), start)
        try:
            import subprocess

            # ★ 2026-11-01 真机自测：**不要加 DETACHED_PROCESS(0x8)**。
            #   PowerShell 在没有控制台时（DETACHED_PROCESS）会直接退出 / 不执行，
            #   实测 0x8 和 0x208 都不产出任何结果；只留 CREATE_NEW_PROCESS_GROUP(0x200)
            #   时，父进程退出后它仍然活着并把新 exe 拉起来（用文件探针逐种 flag 验证过）。
            flags = 0x00000200   # CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                creationflags=flags, close_fds=True,
            )
        except Exception as exc:
            self.node.note("自动重启的辅助进程没有起来：%s" % exc)
            return False, "spawn-failed"

        def exit_soon() -> None:
            # 先让这次 HTTP 响应完整发出去（页面要显示"正在重启"），再退。
            time.sleep(0.8)
            os._exit(0)

        threading.Thread(target=exit_soon, name="audiomixing-restart", daemon=True).start()
        self.node.note("端口已保存：正在自动重启 Audio Mixing（等待本进程退出后由辅助进程拉起）")
        return True, ""

    def set_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        node = self.node
        changed: list[str] = []
        smart_effect: dict[str, Any] | None = None
        for key in ("name", "codec", "source_device", "sink_device", "accept_unknown"):
            if key in payload:
                setattr(node.config, key, payload[key])
                changed.append(key)
                if key == "name":
                    node.name = str(payload[key])
                    node.sender.node_name = node.name
                    if node.beacon is not None:
                        node.beacon.name = node.name
        # ★ 2026-10-18 用户（图2）：「「正在发送的手机自动播放」就是蓝牙的「智连」」——
        #   原来这里还有一个 auto_play（面板上那个全局勾）可以改，现在整块删掉：
        #   收不收下某台设备送来的声音，只由那张卡片上的「智连」决定（局域网 smart_link /
        #   蓝牙 autoconnect，两张名单都在这下面各自处理）。
        for key in ("broadcast", "listen", "local"):
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
        if "launch_at_login" in payload:
            want = bool(payload["launch_at_login"])
            ok, err = set_windows_autostart(want)
            if not ok:
                raise ApiError(err, 500)
            node.config.launch_at_login = want
            changed.append("launch_at_login")
        if "open_panel_on_start" in payload:
            want = str(payload["open_panel_on_start"] or "none").strip().lower()
            if want not in ("none", "simple", "full"):
                raise ApiError("open_panel_on_start 只能是 none、simple 或 full", 400)
            node.config.open_panel_on_start = want
            changed.append("open_panel_on_start")
        if "smart_link" in payload:
            # ★ 2026-11-04（A 硬规则）：广播模式不允许和智连组合。
            #   广播开着时只能从名单里删，不能新增；UI 也会把按钮置灰。
            old_keys = {str(x).strip().lower() for x in (node.config.smart_link or [])}
            raw_keys = {str(x).strip().lower() for x in (
                [payload["smart_link"]] if isinstance(payload.get("smart_link"), str)
                else (payload.get("smart_link") or [])) if str(x).strip()}
            if node.config.broadcast and (raw_keys - old_keys):
                raise ApiError("广播模式不能和智连组合：智连只服务具名点对点。请先关闭广播。", 409)
        if "smart_link" in payload:
            # ★ 2026-10-18 用户第 6 条：每台设备一个「智连」，**默认空 = 一台都不自动**
            #   （用户原话「默认关闭」）。这里收的是**整张名单**，和设备卡片的 autoconnect 同一套做法。
            #   它取代了原来的两个旧口径：全局总开关 auto_accept_listen、
            #   以及"关过一次就永不再自动"的黑名单 listen_optout（后者正是"电脑送音手机不自动收"
            #   那条真机反馈的根因，已随这次改动一起删掉）。
            raw = payload["smart_link"]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                raise ApiError("smart_link must be a list of device ids or names")
            node.config.smart_link = [str(x).strip() for x in raw if str(x).strip()][:16]
            changed.append("smart_link")
            # ★ 2026-11-01 用户：「电脑的智连不是真实反馈，功能不完整」。
            #   旧实现只写名单：按钮已经变色，实际却要等下一包统计/下一轮清扫才动。
            #   这里立刻按新名单建/撤自动收听（对端正在送声音时当场就听得到）。
            try:
                smart_effect = node.apply_smart_link_now()
            except Exception as exc:
                smart_effect = {"error": str(exc)}
                node.note("「智连」立刻生效失败：%s" % exc)
        if "channel_gain" in payload:
            node.config.channel_gain = max(0.0, min(4.0, float(payload["channel_gain"])))
            changed.append("channel_gain")
        if "phone_volume_mode" in payload:
            # ★ 2026-11-04（Q2）：手机音量二次叠加：auto（自动判定）/ off（强制关）/ on（强制开）。
            want = str(payload["phone_volume_mode"] or "auto").strip().lower()
            if want not in ("auto", "off", "on"):
                raise ApiError("phone_volume_mode 只能是 auto、off 或 on", 400)
            node.config.phone_volume_mode = want
            changed.append("phone_volume_mode")
            # 立刻按新口径重算已有播放端的系数（不等下一轮调和，用户当场看到数变化）。
            try:
                node.apply_routes()
            except Exception as exc:
                node.note("「手机音量二次叠加」立即生效失败：%s" % exc)
        if "phone_silent_vol_policy" in payload:
            # ★ 2026-11-08（用户方案）：静音档乘数策略 internal_only / last_nonzero。
            want = str(payload["phone_silent_vol_policy"] or "internal_only").strip().lower()
            if want not in ("internal_only", "last_nonzero"):
                raise ApiError("phone_silent_vol_policy 只能是 internal_only 或 last_nonzero", 400)
            node.set_phone_silent_vol_policy(want)
            changed.append("phone_silent_vol_policy")
            try:
                node.apply_routes()
            except Exception as exc:
                node.note("「静音档乘数策略」立即生效失败：%s" % exc)
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
        # 端口自定义：别的软件占着端口、或同一局域网多人各用一套时，
        # 用户可以在「设置」页里改成自己机器空闲的端口。
        # ★ 端口在进程启动时就绑死了，所以这里只落盘，返回 restart_required，
        #   由用户重启 Audio Mixing 后再生效 —— 绝不假装"已经生效"。
        wanted_ports: dict[str, int] = {}
        for key, label in (("web_port", "面板端口"),
                           ("audio_port", "音频端口（UDP）"),
                           ("discovery_port", "发现端口（UDP）")):
            if key not in payload:
                continue
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                raise ApiError("%s 必须是 1024–65534 的整数" % label, 400)
            if not 1024 <= value <= 65534:
                raise ApiError("%s 只能在 1024–65534 之间（%d 不可用）" % (label, value), 400)
            wanted_ports[key] = value
        # 记下运行中那份的真实端口：下面只是临时改成新值写盘，写完必须还原。
        previous_ports = {k: getattr(node.config, k)
                          for k in ("web_port", "audio_port", "discovery_port")}
        real_port_changes: dict[str, int] = {}
        if wanted_ports:
            current = {
                "web_port": int(node.config.web_port),
                "audio_port": int(node.config.audio_port),
                "discovery_port": int(node.config.discovery_port),
            }
            current.update(wanted_ports)
            if len(set(current.values())) != 3:
                raise ApiError("三个端口不能重复：面板 %d / 音频 %d / 发现 %d"
                               % (current["web_port"], current["audio_port"],
                                  current["discovery_port"]), 400)
            # 面板端口 +1 是手机长连接端口（引擎自己也要绑它），一起查重。
            if current["web_port"] + 1 in (current["audio_port"], current["discovery_port"]):
                raise ApiError("面板端口 +1（%d）被音频/发现端口占用了，换一个面板端口"
                               % (current["web_port"] + 1), 400)
            # ★ 2026-11-01：保存后会**自动重启**，所以必须先把"新端口能不能真的绑上"
            #   查清楚。旧实现只校验范围和重复，端口被别的程序占着也会返回"保存成功"；
            #   重启后启动失败，用户看到的就是"端口改了，但软件打不开"。
            #   没变的端口跳过：本进程现在还合法地占着它。
            real_port_changes = {k: v for k, v in wanted_ports.items()
                                 if v != previous_ports[k]}
            if real_port_changes:
                from . import ports as _port_guard

                for key, value in real_port_changes.items():
                    if key == "web_port":
                        if not _port_guard._tcp_free(value) or not _port_guard._tcp_free(value + 1):
                            raise ApiError(
                                "面板端口 %d（或它 +1 的 %d）已被别的程序占用，"
                                "换一个空闲的再保存" % (value, value + 1), 400)
                    elif not _port_guard._udp_free(value):
                        raise ApiError(
                            "%s %d 已被别的程序占用，换一个空闲的再保存"
                            % ("音频端口" if key == "audio_port" else "发现端口", value), 400)
            for key, value in wanted_ports.items():
                setattr(node.config, key, value)
                if key in real_port_changes:
                    changed.append(key)
        # ★ 只改磁盘，不改"正在跑的这份"：端口在启动时绑死了，把运行中的
        #   config.web_port 改成新值会让信标/面板横幅立刻广播一个还没监听的端口，
        #   对端照着连就是空白。运行中的值保持原样，重启后从磁盘读新端口。
        try:
            # 嵌入到别的宿主里时由宿主注入保存路径；独立运行时走 Config 默认路径。
            # strict=True：端口是用户主动保存的，写盘失败必须如实报错。
            node.config.save(getattr(self, "config_save_path", None), strict=True)
        except OSError as exc:
            raise ApiError("could not save config: %s" % exc, 500)
        finally:
            for key, value in previous_ports.items():
                setattr(node.config, key, value)
        # ★ 2026-11-01 用户：「修改端口号后自动重启」。
        #   只有真的改了端口才重启；原样点一次保存（页面现在只提交发现端口，
        #   但命令行/测试也可能带三个）不能平白把正在跑的程序重启一遍。
        restart_scheduled, restart_blocked = (
            self._schedule_restart() if real_port_changes else (False, "no-port-change"))
        node.refresh_devices(force=True)
        node.apply_routes()
        return {"ok": True, "changed": changed, "config": node.config.to_json(),
                "smart_effect": smart_effect,
                "restart_required": bool(real_port_changes),
                "restart_scheduled": restart_scheduled,
                "restart_blocked": restart_blocked,
                "restart_note": (
                    "端口已保存，正在自动重启；面板端口如果也改了，新窗口会打开在新地址。"
                    if restart_scheduled else
                    ("端口已保存，请手动重启 Audio Mixing 后生效。"
                     if real_port_changes else "端口没有变化。")),
                "pending_ports": dict(real_port_changes)}

    # ★ 2026-11-04（第 8 条）：测连通按钮与 POST /api/ping 一起下线（安卓端不回 PING/PONG，结果无参考价值）。

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

        threading.Thread(target=run, name="audiomixing-tone", daemon=True).start()
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


# 并发上限：每条连接一个线程，局域网里有人乱开连接不能把线程数顶爆。
# 64 足够（一台手机 ≈ 2 条：面板 + 音频/长连接；家庭/小办公室远远用不完）。
MAX_CONNECTIONS = 64


class _BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer + 连接数上限（排队的连接直接关掉，不建线程）。"""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    server_version = "AudioMixing"
    sys_version = ""
    api: AudioMixingApi
    quiet = True
    # 给每个连接的请求/响应一个上限：慢连接（Slowloris）不能永远占着一个线程。
    # 音视频长连接由自己的循环负责，不会因为这一条被掐断（它们要么持续 send，
    # 要么由 select 守着读）。
    timeout = 30

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        if not self.quiet:
            super().log_message(fmt, *args)

    def version_string(self) -> str:
        """别把 Python 补丁号挂在 Server 头上（那等于给扫描器报菜名）。"""
        return self.server_version

    def _origin_ok(self) -> bool:
        """只接受同源请求；没有 Origin 的（手机 App / curl / 本机）照旧。

        说明：这是给"任意网页都能跨域读/写面板"补的锁，不是鉴权 ——
        同一局域网的人仍然能直接用 IP 访问（项目的红线就是局域网内无鉴权）。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        if origin == "null":
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and parsed.netloc.lower() == host

    def _bad_host(self) -> bool:
        """Host 只认本机/局域网地址；不认域名（防 DNS rebinding）。"""
        if _host_allowed(self.headers.get("Host") or ""):
            return False
        self._json(403, {"error": "Host 不被允许：请用本机或局域网 IP 访问"})
        return True

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 不再发 Access-Control-Allow-Origin: * —— 那会让任意网站读到面板数据。
        # 面板/手机页/下载都是同源，不需要 CORS。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
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
        self.send_header("Content-Disposition", content_disposition(apk_display_name(apk)))
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
        name = (query.get("name") or ["手机"])[0].strip()[:64] or "手机"
        try:
            rate = int((query.get("rate") or ["48000"])[0] or 48000)
        except ValueError:
            rate = 48000
        if not 8000 <= rate <= 192000:
            # 采样率决定重采样器的比例；让远端随手填个天文数字没有意义。
            self._json(400, {"error": "rate 必须在 8000–192000 之间"})
            return
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
            try:
                mic = netmic_backend.open_mic(name, rate)
            except ValueError as exc:
                # 槽位满了：这是远端的名字，不能让它无限建下去。
                self._json(429, {"error": str(exc)})
                return
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
        # 不做跨域预检：本机页面/手机页都是同源，不需要 CORS。
        # 任意网站发来的预检会因为没有 ACAO 而被浏览器自己拦下。
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        if self._bad_host():
            return
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
            # ★ v2.27（用户第 9 条）：手机 2.15 已能独立完成广播（SendFanout 直接发给
            #   所有在线对端，不借电脑转发信令），所以 /p2p、/p2p.html、/direct 三个入口
            #   一并下线。底层 net/p2p.py 暂留（没有入口，后续再清），不影响广播。
            if path in ("/p2p", "/p2p.html", "/direct") or path == "/ws/p2p":
                self._send(410, b"p2p entry removed in v2.27", "text/plain; charset=utf-8")
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
                # ★ 2026-11-04（第 3 条）：配对列表为空时，原因由**服务端按真实状态**
                #   算好（无适配器 / 蓝牙关闭 / 已打开但未配对），前端只负责显示。
                #   这样文案不会再把"蓝牙没开"说成"没配对"。
                try:
                    radio = bluetooth.radio_state()
                    empty_reason = "" if devices else bluetooth.empty_reason()
                except Exception:
                    radio, empty_reason = {}, ""
                self._json(200, {
                    "available": bluetooth.available(),
                    "devices": devices,
                    "radio": radio,
                    "empty_reason": empty_reason,
                    # ★ ④ 手机点过「走蓝牙」的记录：电脑这边去连它连成没连成，
                    #   面板 ⑦ 那张卡直接把结果写出来，用户不用猜。
                    "requests": self.api.bluetooth_requests(),
                })
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
                    # 用户看到的下载名带版本号（第 6 条），与 Content-Disposition 一致。
                    "name": apk_display_name(apk) if apk else "",
                    # Hash the exact bytes the download serves, and name the file, so the
                    # stamp can never describe a different build than the one you get.
                    "sha1": (info or {}).get("sha1", ""),
                    "path": (info or {}).get("path", ""),
                    "bytes": (info or {}).get("bytes", 0),
                })
                return
            if path in ("/app/download", "/app/AudioMixing-mic.apk",
                        "/app/AudioMixing-手机版.apk",
                        "/app/AudioMixing-手机版-v%s.apk" % __phone_version__,
                        "/app/wavelink-mic.apk"):
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
                        % (_html.escape(str(self.api.node.name)), self.api.node.config.web_port)
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
                # ★ 以前 GET 也执行放行脚本：任意网页一个 <img src=".../api/firewall">
                #   就能弹 UAC。现在只允许 POST（页面按钮本来就是 POST）。
                self._json(405, {"error": "放行防火墙只能用 POST"})
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
                        "a.tab,button.tab{padding:4px 12px;border-radius:8px;background:#1d2634;"
                        "color:#e8edf5;text-decoration:none;font-size:13px;border:0;cursor:pointer}"
                        "a.tab.on{background:#2f81f7;color:#06121f;font-weight:600}"
                        "pre{margin:0 20px 24px;padding:14px;background:#171c24;"
                        "border:1px solid #242c38;border-radius:10px;white-space:pre-wrap;"
                        "word-break:break-all}</style></head><body><header>"
                        "<b>Audio Mixing</b>"
                        "<a class=\"tab\" href=\"/monitor\">监控</a>"
                        "<a class=\"tab\" href=\"/\">控制台</a>"
                        "<a class=\"tab on\" href=\"/diagnose\">诊断</a>"
                        "<a class=\"tab\" href=\"/api/diagnose/bundle\" download>下载诊断包</a>"
                        "<button class=\"tab\" onclick=\"recoverAudio()\">恢复声音</button>"
                        "<span id=\"rec\" style=\"color:#8b97a8\">"
                        "下载 zip 直接发回，不用再全选复制</span>"
                        "</header><pre>" + _html.escape(data["text"]) + "</pre>"
                        "<script>async function recoverAudio(){"
                        "var b=document.getElementById('rec');b.textContent='正在重开播放端…';"
                        "try{var r=await fetch('/api/audio/recover',{method:'POST'});"
                        "var d=await r.json();b.textContent=d.ok?('已重开 '+d.restarted+' 路播放端'):"
                        "('失败：'+(d.error||'未知'));}catch(e){b.textContent='失败：'+e.message;}}"
                        "</script></body></html>")
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/diagnose/bundle":
                data, name = self.api.diagnose_bundle()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", content_disposition(name))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            if path == "/api/audio/recover":
                # 会重开播放端，属于写操作：只允许 POST（页面和托盘都用 POST）。
                self._json(405, {"error": "恢复声音只能用 POST"})
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
                # 监控页每行的"断开"。判定统一在 api.monitor_disconnect() 里（见那里的说明），
                # 这里只负责取参数 + 转发 —— 以前这里和 POST 分支各抄了一份判定，加一种 kind
                # 就得改两处、必然漏一处。
                body = self._read_json() if method == "POST" else {}
                kind = str(body.get("kind") or "")
                target = str(body.get("id") or "")
                code, result = self.api.monitor_disconnect(kind, target)
                self._json(code, result)
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
                # listen=手机当喇叭 app=装手机端（p2p 入口已于 v2.27 下线）
                tail = {"app": "/app"}.get(what, "/listen")
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
                self._json(200, {"config": self.api.node.config.to_json(),
                                 "autostart": autostart_state()})
                return
            if path == "/api/net":
                self._json(200, self.api.network())
                return
            if path == "/healthz":
                # app 键用于启动器"认亲"：同端口的同类软件不能被当成自己人，
                # 否则会误开它的窗口、甚至在接管时杀掉它的进程。
                self._json(200, {"ok": True, "app": "audiomixing",
                                 "uptime_s": round(time.time() - self.api.node.started_at, 1)})
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
        if self._bad_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not self._origin_ok():
            # 别的网站发来的写操作一律拒绝（读操作没这个限制，但也不再给 ACAO *）。
            self._json(403, {"error": "跨站请求被拒绝（只接受同源）"})
            return
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
                                 "note": "已请求以管理员身份放行：会先清理旧版本规则，"
                                         "再为当前程序重建 TCP/UDP。若没看到管理员确认窗口，"
                                         "请右键运行 " + bat})
            elif path == "/api/monitor/disconnect":
                # 监控页每行的"断开"：必须放在 POST 分支 —— 页面用的是 POST，
                # 只写进 GET 分支的话点按钮只会拿到 404（放行防火墙踩过同一个坑）。
                # 判定只有一处：api.monitor_disconnect()。
                code, result = self.api.monitor_disconnect(
                    str(body.get("kind") or ""), str(body.get("id") or ""))
                self._json(code, result)
            elif path == "/api/routes":
                self._json(200, self.api.set_routes(body))
            elif path == "/api/routes/add":
                self._json(200, self.api.add_route(body))
            elif path == "/api/routes/update":
                self._json(200, self.api.update_route(body))
            elif path == "/api/routes/delete":
                self._json(200, self.api.delete_route(body))
            elif path == "/api/routes/purge-stale":
                self._json(200, self.api.purge_stale_routes())
            elif path == "/api/peers/add":
                self._json(200, self.api.add_peer(body))
            elif path == "/api/peers/remove":
                self._json(200, self.api.remove_peer(body))
            elif path == "/api/identity/merge":
                self._json(200, self.api.identity_merge(body))
            elif path == "/api/relation/merge":
                self._json(200, self.api.relation_merge(body))
            elif path == "/api/relation/split":
                self._json(200, self.api.relation_split(body))
            elif path == "/api/settings":
                self._json(200, self.api.set_settings(body))
            elif path == "/api/guide/fix":
                # 「快速上手」里的"一键修"：换设备 / 试采 / 修广播。一次只做用户点的那一件。
                self._json(200, self.api.guide_fix(body))
            elif path == "/api/bluetooth/connect":
                bluetooth.invalidate_cache()
                self._json(200, bluetooth.connect(str(body.get("id") or "")))
            elif path == "/api/bluetooth/disconnect":
                bluetooth.invalidate_cache()
                # ★ v2.26 第 3 条：合并卡要一次断掉全部历史配对 id。
                ids = body.get("ids")
                if isinstance(ids, list) and ids:
                    results = [bluetooth.disconnect(str(x)) for x in ids if x]
                    ok = any(bool(item.get("ok")) for item in results)
                    self._json(200, {"ok": ok, "note": "；".join(
                        str(item.get("state") or item.get("error") or "") for item in results)})
                else:
                    self._json(200, bluetooth.disconnect(str(body.get("id") or "")))
            elif path == "/api/bluetooth/cleanup":
                # ★ v2.26 第 3 条：只清本程序的旧记录；Windows 配对删不掉，响应里写明。
                self._json(200, bluetooth.cleanup_stale_records())
            elif path == "/api/audio/recover":
                # 「恢复声音」：重开全部播放端 + 本机直通（等同在 Windows 声音里切一次输出）。
                self._json(200, self.api.recover_audio())
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

    def __init__(self, node, handler_factory: Callable[[], None] | None = None, host: str = "0.0.0.0",
                 port: int | None = None, *, restart_argv: list[str] | None = None) -> None:
        self.node = node
        self.host = host
        self.port = port or node.config.web_port
        self.api = AudioMixingApi(node, web=self, on_routes_changed=self.broadcast_routes,
                                  restart_argv=restart_argv)
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

        threading.Thread(target=run, name="audiomixing-route-push", daemon=True).start()

    def start(self) -> int:
        # ★ 2026-11-06（第 5 条）：用户勾过自启动，就以**当前这份程序**为准重写一次。
        #   exe 被替换或挪了位置后，注册表里的旧路径会指向一个不存在的文件；
        #   每次启动自愈，比要求用户回设置页重勾一遍可靠。只在打包态做（源码态
        #   写的是 launcher.py 的开发路径，不替用户做这个决定）。
        if sys.platform == "win32" and getattr(self.node.config, "launch_at_login", False) \
                and _program_exe_path():
            try:
                ok, err = set_windows_autostart(True)
                if not ok:
                    self.node.note("开机自启动没有生效：%s" % err)
            except Exception:
                pass
        handler = type("BoundHandler", (_Handler,), {"api": self.api})
        for attempt in range(10):
            try:
                self.httpd = _BoundedHTTPServer((self.host, self.port + attempt), handler)
                self.bound_port = self.port + attempt
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError("could not bind a web port starting at %d" % self.port)
        self.node.config.web_port = self.bound_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="audiomixing-web", daemon=True)
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

# The control panel lives in audiomixing/ui/index.html so that editing the UI never
# means editing Python. It is embedded into the executable by tools/build_exe.py
# (`--add-data`); when running from a source checkout it is read from disk. The
# placeholder below is only used if the file is genuinely missing, so the server
# can still explain itself instead of returning a stack trace.
FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Audio Mixing</title>
<style>body{background:#0b0e14;color:#e8eef7;font:14px/1.7 system-ui,"Microsoft YaHei";padding:40px;max-width:720px;margin:auto}
code{background:#1a202c;padding:2px 6px;border-radius:4px}a{color:#60a5fa}</style></head><body>
<h2>Audio Mixing 控制台页面文件缺失</h2>
<p>后端正在运行，但没找到 <code>audiomixing/ui/index.html</code>。接口仍然可用：</p>
<ul>
  <li><a href="/api/status">/api/status</a> — 节点状态</li>
  <li><a href="/api/peers">/api/peers</a> — 设备列表</li>
  <li><a href="/api/routes">/api/routes</a> — 路由表</li>
  <li><a href="/api/devices">/api/devices</a> — 本机音频设备</li>
</ul>
<p>如果你是从源码运行，请确认 <code>audiomixing/ui/index.html</code> 存在；<br>
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


# ★ 2026-11-04（第 6 条）：交付名必须带版本号（用户实机装错过，分不清 2.11/2.12）。
#   带版本的名字排最前；旧名字只做兜底搜索，避免老目录/老 exe 突然找不到包。
from . import __phone_version__

_APK_NAMES = ("AudioMixing-手机版-v%s.apk" % __phone_version__,
              "AudioMixing-手机版.apk", "AudioMixing-mic.apk", "wavelink-mic.apk")


def apk_display_name(apk: Path | None = None) -> str:
    """下载时对用户显示的文件名：**永远带手机端版本号**。"""
    return "AudioMixing-手机版-v%s.apk" % __phone_version__


def _apk_dirs() -> list[Path]:
    """可能放着手机端安装包的目录，按可靠度排序。

    必须覆盖三种运行形态 —— 2026-10-15 用户现场（手机扫二维码装 App，页面报
    「这台电脑上还没有手机端安装包」）就是这里漏了前两种：
      · 双击 exe：包就在**那颗真 exe 旁边**（桌面上叫 AudioMixing-手机版.apk）。
        注意 Nuitka onefile 下 sys.executable 指向解包出来的临时 python.exe，
        对它取目录必然落空 —— 必须走 _frozen_exe_path()。
      · 源码运行：源码树根目录、audiomixing/ui/、以及 tools/installer_payload（安装器载荷）。
      · PyInstaller 解包：sys._MEIPASS/audiomixing/ui。
    """
    dirs: list[Path] = []
    real = AudioMixingApi._frozen_exe_path()
    if real is not None:
        dirs.append(real.parent)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(Path(base) / "audiomixing" / "ui")
    here = Path(__file__).resolve().parent
    dirs.append(here / "ui")
    dirs.append(here.parent)
    dirs.append(here.parent / "tools" / "installer_payload")
    try:
        dirs.append(Path.cwd())
    except OSError:
        pass
    return dirs


def content_disposition(name: str) -> str:
    """给中文文件名的附件下载造一个**既能用又合法**的 Content-Disposition。

    为什么不能直接把文件名塞进头部：交付链上这颗安装包叫「AudioMixing-手机版.apk」，
    里面有非 ASCII 字符。HTTP 头按 RFC 只允许 ASCII，把 UTF-8 原样写进去，.NET 的
    HttpWebRequest 会直接判成"服务器提交了协议冲突（CR 后面必须是 LF）"，
    某些手机浏览器同样会当成坏响应 —— 用户点下载就失败（2026-10-15 实测）。
    所以：`filename=` 只放 ASCII 兜底名，真名用 RFC 5987 的 `filename*=UTF-8''百分号编码`。
    """
    stem, _dot, ext = str(name or "").rpartition(".")
    ascii_stem = "".join(ch for ch in stem if ch.isascii() and (ch.isalnum() or ch in "-_"))
    # ★ 2026-11-04（第 6 条）：名字加了版本号后，中文被过滤掉会留下两个连着的短横
    #   （「AudioMixing-手机版-v2.13」→「AudioMixing--v213」）。合并成一个再收边。
    while "--" in ascii_stem:
        ascii_stem = ascii_stem.replace("--", "-")
    ascii_stem = ascii_stem.strip("-_. ")          # 「AudioMixing-手机版」→「AudioMixing」，不留孤零零的短横
    ext = "".join(ch for ch in ext if ch.isascii() and ch.isalnum())
    safe = (ascii_stem or "AudioMixing") + ("." + ext if ext else "")
    return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (safe, quote(name))


def apk_path() -> Path | None:
    """手机端安装包在哪；找不到返回 None（安装页会给出该把它放哪）。

    为什么是"搜索"而不是一个固定位置：谁编的 APK 都可以丢在 exe 旁边、塞进 ui 目录、
    或者用 AUDIOMIXING_APK 指过来（旧名 WAVELINK_APK 仍然认）。**但搜索本身必须不能抛异常** —— 这里原来是
    `Path(base)` 而 base 在非 PyInstaller 时是 None，于是 `apk_path()` 直接抛
    TypeError，/api/app 跟着 500，安装页只剩一句"还没有安装包"（用户看到的现场）。
    """
    env = os.environ.get("AUDIOMIXING_APK") or os.environ.get("WAVELINK_APK")
    if env:
        try:
            p = Path(env)
            if p.is_file():
                return p
        except (OSError, ValueError):
            pass
    for folder in _apk_dirs():
        for name in _APK_NAMES:
            try:
                p = folder / name
            except (TypeError, ValueError):
                continue
            try:
                if p.is_file():
                    return p
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




def _best_local_address(node, report: dict[str, Any]) -> str:
    """这台机器应该对外报哪个局域网地址（面板「本机」栏、二维码、手机直连都用它）。

    ★ 2026-11-03（Q1）：`lan_report()` 的第一块网卡不一定是能连的那块 —— 这台机器上
      第一块是 Hyper-V 默认交换机的 172.26.192.1，而手机在 192.168.0.x，根本连不上。
      选择顺序：
        ① 和「已知对端」同 /24 的地址（有对端时可验证，最可靠）；
        ② 私网网段的物理网卡排序（192.168 优先，10. 次之，172.16~31 最后 ——
           WSL / Hyper-V 默认交换机的地址就在 172 段）；
        ③ 没有可用网卡时退回 127.0.0.1（只在完全离线的机器上出现）。
    """
    import ipaddress

    usable = [i for i in (report.get("interfaces") or [])
              if str(i.get("address") or "").strip()
              and not str(i.get("address")).startswith(("127.", "169.254."))]
    if not usable:
        return "127.0.0.1"
    peer_hosts: set[str] = set()
    try:
        for item in (node.registry.snapshot() or []):
            host = str(item.get("host") or "").strip()
            if host:
                peer_hosts.add(host)
    except Exception:
        pass
    try:
        for item in (getattr(node.config, "manual_peers", None) or []):
            host = str((item or {}).get("host") or "").strip()
            if host:
                peer_hosts.add(host)
    except Exception:
        pass
    peer_nets: set[str] = set()
    for host in peer_hosts:
        try:
            peer_nets.add(str(ipaddress.ip_interface("%s/24" % host).network))
        except ValueError:
            continue
    same_subnet = [i for i in usable if str(i.get("subnet") or "") in peer_nets]
    if same_subnet:
        return str(same_subnet[0].get("address"))

    def _rank(item: dict[str, Any]) -> tuple[int, str]:
        addr = str(item.get("address") or "")
        if addr.startswith("192.168."):
            return (0, addr)
        if addr.startswith("10."):
            return (1, addr)
        return (2, addr)  # 172.16~31 段（WSL / Hyper-V）和其余公网地址排在最后

    return str(sorted(usable, key=_rank)[0].get("address"))


def _network_payload(node: Any, report: dict[str, Any]) -> dict[str, Any]:
    """把网卡清单连同"面板该显示哪个地址"一起下发。

    ★ 2026-11-06（真实截图自查）：页面原来直接取 `interfaces` 的第一项，
      这台机器第一项是 172.23.128.1（WSL/Hyper-V 虚拟网卡），面板横幅和
      手机收听地址都印成了它 —— 手机根本连不上，而界面看上去"很正常"。
      服务端已经有 `_best_local_address()`（优先 192.168 / 10、跳过虚拟网卡），
      这里把它作为 `preferred` 一并给前端，前端不再自己猜第一项。
    """
    data = dict(report or {})
    try:
        data["preferred"] = _best_local_address(node, data)
    except Exception:
        data["preferred"] = ""
    return data


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
        host = _best_local_address(getattr(api, "node", None), report)
        if host and not host.startswith("127."):
            return "%s:%d" % (host, port)
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
<!-- ★ favicon：2026-10-17 用户（图2）反馈监控窗「logo 不显示」。和主页面同款内联 SVG
     （蓝色渐变圆角方块 + 白 A），不引外部文件 —— 局域网里外链图片加载不出来，
     tools/check_ui_syntax.py 也会把 http(s) 外链判为不合格。 -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%2338bdf8'/%3E%3Cstop offset='1' stop-color='%236366f1'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='url(%23g)'/%3E%3Cpath fill='%23fff' fill-rule='evenodd' d='M32 13L50 51L41.4 51L38.3 44L25.7 44L22.6 51L14 51Z M32 25.5L27.4 36L36.6 36Z'/%3E%3C/svg%3E">
<style>
 :root{--bg:#0e1116;--card:#171c24;--line:#242c38;--fg:#e8edf5;--dim:#8b97a8;
       --ok:#3fb950;--warn:#d29922;--accent:#2f81f7}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 "Microsoft YaHei UI",system-ui,sans-serif}
 header{display:flex;align-items:center;gap:12px;padding:16px 20px 10px}
 h1{font-size:16px;margin:0;display:flex;align-items:center;gap:8px}
 /* ★ 2026-10-17：原来标题上只有一个绿点「●」，用户（图2）说 logo 不显示。
    这里换成和主页面同一套的蓝色渐变方块 + 白 A（品牌字形变了，方块里的字跟着改成 A）。 */
 .mark{width:22px;height:22px;border-radius:7px;flex:0 0 auto;
       background:linear-gradient(140deg,#38bdf8,#6366f1);
       display:grid;place-items:center;color:#04121f;font-size:12px;font-weight:800}
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
  <h1><span class="mark">A</span>Audio Mixing</h1>
  <nav class="tabs">
    <a class="tab on" href="/monitor">监控</a>
    <a class="tab" href="/">控制台</a>
  </nav>
  <span class="dim" id="who"></span>
  <a id="dgLink" href="/diagnose"
     title="诊断报告页：可一键下载诊断包（zip），不用再全选复制"
     style="padding:4px 12px;border-radius:8px;background:#1d2634;color:#e8edf5;
            text-decoration:none">诊断</a>
  <!-- ★ 2026-10-20 用户：「监控模式右上角『放行防火墙』的左侧添一个『本机』激活按钮，
       就仅监控与本机有关的设备，而不是整个局域网的设备全部监控，同时在激活情况下隐藏时钟通道。
       不然看起来太乱了」——打开后只留"本机收发的那几路"，并把时钟通道藏起来（开关状态记在浏览器里，
       下次打开还是你上次选的那个）。 -->
  <button id="localOnly"
          title="只看和本机有关的设备：打开后只显示本机收发的那几路，并隐藏时钟通道（关掉则显示整屏）"
          style="padding:4px 12px;border-radius:8px;border:0;background:#1d2634;color:#e8edf5;
                 cursor:pointer">本机</button>
  <span style="margin-left:auto;display:flex;gap:8px">
    <button id="recover"
            title="蓝牙/WiFi 连上后没声音时点这里：重开所有播放端，等同在 Windows 声音里切换一次输出"
            style="padding:4px 12px;border-radius:8px;border:0;
                   background:#1d2634;color:#e8edf5;cursor:pointer">恢复声音</button>
    <button id="fw" title="点对点或广播不通时，多半是本机防火墙挡了入站。点这里清理旧版本规则，并为当前程序放行。"
            style="padding:4px 12px;border-radius:8px;border:0;
                   background:#1d2634;color:#e8edf5;cursor:pointer">放行防火墙</button>
  </span>
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
    <span class="dim" id="chkNote"></span>
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
/* ★ 2026-10-20：「本机」这颗开关 —— 打开后只看和本机有关的那几路，并隐藏时钟通道。 */
var localOnly = (localStorage.getItem('am.monitor.localOnly') === '1');
function paintLocalOnly(){
  var b = document.getElementById('localOnly');
  b.className = localOnly ? 'on' : '';
  b.textContent = localOnly ? '✓ 本机' : '本机';
  b.style.background = localOnly ? 'var(--accent)' : '#1d2634';
  b.style.color = localOnly ? '#06121f' : '#e8edf5';
  b.style.fontWeight = localOnly ? '600' : '400';
}
document.getElementById('localOnly').onclick = function(){
  localOnly = !localOnly;
  localStorage.setItem('am.monitor.localOnly', localOnly ? '1' : '0');
  paintLocalOnly();
  tick();
};
paintLocalOnly();      // 打开页面就按上次选的那一档画一次
async function tick(){
  try{
    const d = await (await fetch('/api/monitor', {cache:'no-store'})).json();
    document.getElementById('who').textContent = d.node || '';
    document.getElementById('when').textContent = '更新于 ' + new Date().toLocaleTimeString();
    let rows = d.flows || [];
    if(localOnly){
      /* 只留"本机收发的那几路"：其它设备之间的来往一律不显示；时钟通道（对端↔时钟对齐）也藏起来。
         ★ 2026-10-21 用户现场：打开「本机」后，唯一那行「手机 → 本机（蓝牙）」整行消失。
           原因是 /api/monitor 的本地端写的是字面量「本机」/「本机声音」，而这里只拿
           节点名（d.node）去比 —— 所有入向流都会被误判成"和本机无关"。
           本地端必须把节点名和这几种写法都认成本机。 */
      const me = String(d.node || '');
      const isLocalEnd = function(v){
        const s = String(v || '');
        return s === me || s === '本机' || s === '本机声音' || s === 'self';
      };
      rows = rows.filter(function(f){
        if(String(f.channel || '') === '时钟') return false;
        return isLocalEnd(f.from) || isLocalEnd(f.to);
      });
    }
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
        <td>${f.repair ? '<button class="disc" data-kind="repair" data-id="' +
             (f.repair_id || f.route_id || '') + '" title="把这条失效路由改指向当前唯一在线的对端（会先备份配置）">' +
             (f.repair_action || '一键修复') + '</button> ' : ''}${f.disconnect ?
             '<button class="disc" data-kind="' + f.disconnect +
             '" data-id="' + (f.disconnect_id || f.route_id || '') + '" title="' +
             (f.hint || '断开') + '">' + (f.action || '断开') + '</button>' : ''}</td>
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
function escHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
  });
}
on('runChk', 'click', async () => {
  const box = document.getElementById('chkRows');
  box.textContent = '检查中…';
  try{
    const d = await (await fetch('/api/netcheck', {cache:'no-store'})).json();
    box.innerHTML = (d.checks || []).map(c => {
      const mark = c.ok === null ? '·' : (c.ok ? '✓' : '✗');
      const color = c.ok === null ? '#8b97a8' : (c.ok ? '#3fb950' : '#f85149');
      const meta = c.meta || {};
      const h = meta.history || {};
      let extra = '';
      if(h.total){
        const rows = (h.examples || []).map(function(e){
          return '<div style="margin-left:18px;color:#8b97a8">' +
                 escHtml(e.name) + ' → ' + escHtml(e.program || '(未绑定程序)') +
                 (e.exists ? '' : '　[文件不存在]') + '</div>';
        }).join('');
        const more = h.truncated
          ? '<div style="margin-left:18px;color:#8b97a8">还有 ' +
            Math.max(0, (h.unique_rules || h.total) - (h.examples || []).length) +
            ' 条未展开；点右上角「放行防火墙」可清掉全部旧版本规则。</div>'
          : '';
        extra = '<details style="margin:3px 0 6px 18px"><summary style="cursor:pointer;color:#8b97a8">' +
                '历史规则 ' + h.total + ' 条 · ' + h.unique_paths + ' 个路径 · ' +
                h.missing_files + ' 个文件已不存在</summary>' + rows + more + '</details>';
      }
      return '<div style="margin:4px 0"><span style="color:' + color + '">' + mark +
             '</span> <b>' + escHtml(c.name) + '</b>　' +
             escHtml(c.summary || c.detail || '') + extra + '</div>';
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
on('recover', 'click', async () => {
  const r = await (await fetch('/api/audio/recover', {method:'POST'})).json();
  if(r && r.ok){
    alert('已重开 ' + (r.restarted || 0) + ' 路播放端' +
          (r.loop_stopped ? '，本机直通也重建了。' : '。'));
  }else{
    alert('恢复失败：' + ((r && r.error) || '未知'));
  }
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

# ★ v2.27（用户第 9 条）：手机端 2.15 已能独立完成广播（Engine.SendFanout 直接发给
#   所有在线对端，不借电脑转发信令），所以 /p2p、/p2p.html、/direct 三个入口下线。
#   页面与降级 HTML 一并删除，避免留下"能打开但什么都不做"的假入口。

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
