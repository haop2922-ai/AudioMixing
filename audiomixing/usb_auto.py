"""可选：插上 USB 自动启动手机端并尝试打开 USB 网络共享。

为什么需要它：Android 第三方应用没有 `TETHER_PRIVILEGED` 权限，不能自己开关
「USB 网络共享」。spacedesk 那类"插线即用"走的是 USB 调试 + ADB：
电脑端通过 adb 启动手机 App、切换 USB 功能。这里做的是同一件事，但**只在本机已装
adb 且用户显式打开开关时**才执行，命令是固定的，不接收任何用户输入。

不下载、不打包 adb；找不到就什么都不做。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable


APP_PACKAGE = "com.audiomixing.app"
APP_ACTIVITY = "com.audiomixing.app/.MainActivity"
_POLL_S = 3.0
#: 对"不是我们的手机"多久重查一次（避免每 3 秒跑一次 adb shell pm path）
_APP_CHECK_S = 30.0


def as_bool(value: Any) -> bool:
    """把 API/AI 传来的 true/"false"/1/0/"on" 归一成布尔。

    ★ 2.46.8：`bool("false")` 是 True —— 外部接口发字符串时会把
    「插上 USB 自动触发」误开，然后真的去跑 adb 命令。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "开", "是")


#: find_adb 的结果缓存（★ 2.46.9）。`usb_auto_state()` 会被 /api/status 每 800ms
#: 调一次；没有 adb 的机器上每次都扫 PATH（本机实测 2.8ms/次）纯属浪费。
_ADB_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "path": ""}
_ADB_CACHE_TTL_S = 15.0


def find_adb(force: bool = False) -> str:
    """找本机已有的 adb.exe；找不到返回空串。"""
    key = (str(sys.argv[0] if sys.argv else ""), str(getattr(sys, "executable", "") or ""))
    now = time.time()
    if not force and _ADB_CACHE.get("key") == key \
            and now - float(_ADB_CACHE.get("at") or 0.0) < _ADB_CACHE_TTL_S:
        return str(_ADB_CACHE.get("path") or "")
    candidates: list[pathlib.Path] = []
    # ★ 2.46.8：onefile 下 __file__ 指向临时解压目录，不是 exe 所在目录。
    #   设置页写的是"把 adb.exe 放到程序目录"，所以必须先看 argv[0] / sys.executable。
    for raw in ((sys.argv[0] if sys.argv else ""), getattr(sys, "executable", "")):
        try:
            exe = pathlib.Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if not exe.name or exe.name.lower() in ("python.exe", "pythonw.exe"):
            continue      # 源码运行时这两个是解释器，不是我们的程序目录
        candidates.extend([
            exe.parent / "platform-tools" / "adb.exe",
            exe.parent / "adb.exe",
        ])
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(pathlib.Path(base) / "platform-tools" / "adb.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(pathlib.Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    root = pathlib.Path(__file__).resolve().parent.parent
    candidates.extend([
        root / "platform-tools" / "adb.exe",
        root / "adb.exe",
        root.parent / "platform-tools" / "adb.exe",
        pathlib.Path("C:/platform-tools/adb.exe"),
    ])
    which = shutil.which("adb")
    if which:
        candidates.append(pathlib.Path(which))
    found = ""
    for path in candidates:
        try:
            if path.is_file():
                found = str(path)
                break
        except OSError:
            continue
    _ADB_CACHE.update({"key": key, "at": now, "path": found})
    return found


def _run(adb: str, args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [adb, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:
        return -1, "", "%s: %s" % (type(exc).__name__, exc)


def is_usb_serial(serial: str) -> bool:
    """这条 adb 序列号是不是「真的插在这台电脑上的 USB 手机」。

    ★ 2.46.9：`adb devices` 里还会出现无线调试（`192.168.1.5:5555`）和模拟器
    （`emulator-5554`）。它们不是"插上 USB"，却被当成插线设备执行
    `svc usb setFunctions rndis` —— 隔着 Wi-Fi 改对方 USB 功能，可能直接断开调试。
    """
    text = str(serial or "").strip()
    if not text:
        return False
    low = text.lower()
    if ":" in text:
        return False
    if low.startswith("emulator-") or low.startswith("127.0.0.1"):
        return False
    return True


def device_states(adb: str) -> dict[str, str] | None:
    """序列号 -> adb 状态；**adb 本身失败时返回 None**（和“一台设备都没有”区分开）。

    这个区分很重要：`_handled` 只在“成功读到设备列表”时才清理已处理序列号。
    否则 adb server 偶发重启会被当成“手机拔掉了”，下一次轮询又把 App 拉起来一遍。
    """
    code, out, _err = _run(adb, ["devices"])
    if code != 0:
        return None
    states: dict[str, str] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def list_devices(adb: str) -> list[str]:
    """已授权、状态为 device 的序列号。"""
    states = device_states(adb) or {}
    return [serial for serial, state in states.items() if state == "device"]


def has_app(adb: str, serial: str) -> bool:
    """这台设备装没装我们的手机端。

    ★ 必须先问这一句：`adb devices` 里可能还有别人的手机 / 开发板，
      不能给不相干的设备打开 USB 网络共享（那是改动对方的网络设置）。
    """
    code, out, _err = _run(adb, ["-s", serial, "shell", "pm", "path", APP_PACKAGE])
    return code == 0 and "package:" in out


def prepare_device(adb: str, serial: str,
                   should_run: Callable[[], bool] | None = None) -> dict[str, Any]:
    """启动手机 App，并依次尝试几种开启 USB 网络共享的命令。

    顺序：先 `am start`（切换 USB 功能可能让 adb 掉线），再试 tether。
    每条命令都是固定的；失败只记录，不影响其它命令。

    ★ 2.46.9：`should_run` 每执行一条命令前问一次。用户在设置里关掉开关后，
    正在跑的这条命令（最长 8 秒）结束就停，不会把剩下三条命令继续跑完。
    """
    results: list[dict[str, Any]] = []
    commands = [
        ["shell", "am", "start", "-n", APP_ACTIVITY],
        ["shell", "svc", "usb", "setFunctions", "rndis"],
        ["shell", "cmd", "usb", "setFunctions", "rndis"],
        ["shell", "cmd", "tethering", "start", "--usb"],
    ]
    for cmd in commands:
        if should_run is not None and not should_run():
            break
        code, out, err = _run(adb, ["-s", serial, *cmd])
        results.append({"cmd": " ".join(cmd), "code": code,
                        "out": out[:200], "err": err[:200]})
    return {"serial": serial, "results": results}


class UsbAutoBridge(threading.Thread):
    """后台轮询 adb；插上已授权的手机就启动 App + 尝试开 USB 共享。"""

    def __init__(self, on_note: Callable[[str], None] | None = None,
                 enabled: bool = True) -> None:
        super().__init__(daemon=True, name="audiomixing-usb-auto")
        self.adb = find_adb()
        self._stop = threading.Event()
        # ★ 2.46.9：开关不再靠“销毁线程”实现。关掉＝清 enabled（线程停在等待里，
        #   不再跑 adb），再打开＝重新置位同一个线程 —— 不会出现“关→开”时旧线程
        #   还在跑 prepare_device、新线程已经起来，两个线程同时对一台设备发命令。
        self._enabled = threading.Event()
        if enabled:
            self._enabled.set()
        # ★ 2.46.8：这轮已经处理过的序列号。手机一直插着就只跑一次；
        #   拔掉后从集合里消失，再插上才会重新执行（这才是"插上直接触发"）。
        self._handled: set[str] = set()
        # 不是我们的手机：30 秒重查一次，别每 3 秒跑一次 adb shell。
        self._checked_at: dict[str, float] = {}
        self._unauth: set[str] = set()
        self._on_note = on_note
        self.last_error = ""

    def set_enabled(self, on: bool) -> None:
        if on:
            self._enabled.set()
        else:
            self._enabled.clear()

    def enabled(self) -> bool:
        return self._enabled.is_set()

    def stop(self) -> None:
        self._enabled.clear()
        self._stop.set()

    def note(self, text: str) -> None:
        if self._on_note is not None:
            try:
                self._on_note(text)
            except Exception:
                pass

    def _should_run(self) -> bool:
        return self._enabled.is_set() and not self._stop.is_set()

    def run(self) -> None:
        if not self.adb:
            self.last_error = "未找到 adb.exe（可选：装 Android SDK Platform-Tools）"
            return
        self.note("USB 自动模式：等待手机接入（adb=%s）" % self.adb)
        while not self._stop.is_set():
            if not self._enabled.is_set():
                self._stop.wait(_POLL_S)
                continue
            try:
                states = device_states(self.adb)
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                states = None
            states_ok = states is not None
            if not states_ok:
                if not self.last_error:
                    self.last_error = "adb devices 读取失败"
                states = {}
            unauth = {s for s, state in states.items() if state == "unauthorized"}
            if unauth and unauth != self._unauth:
                # 只提醒一次：每 3 秒重复同一句话只会把日志刷满。
                self.note("USB 自动模式：手机已连上，但还没授权 USB 调试 —— 请在手机弹窗里点「允许」")
            self._unauth = unauth
            # ★ 2.46.9：只认真 USB —— 无线调试（host:port）与模拟器不算“插上线”。
            serials = [s for s, state in states.items()
                       if state == "device" and is_usb_serial(s)]
            # 手机拔掉后从 _handled 里消失；再插上才会重新执行。
            # ★ 只有“成功读到列表”才清理：adb server 偶发重启不能被当成拔出，
            #   否则下一次轮询又会把 App 拉起来一遍。
            if states_ok:
                self._handled.intersection_update(serials)
            for serial in serials:
                if not self._should_run():
                    break
                if serial in self._handled:
                    continue
                now = time.time()
                if now - float(self._checked_at.get(serial) or 0.0) < _APP_CHECK_S:
                    continue
                self._checked_at[serial] = now
                if not has_app(self.adb, serial):
                    continue          # 不是我们的手机：一律不碰
                if not self._should_run():
                    break
                self._handled.add(serial)
                result = prepare_device(self.adb, serial, should_run=self._should_run)
                if not self._should_run():
                    # ★ 2.46.9：用户中途关掉了开关 —— 这不是失败，别记“系统不允许”。
                    continue
                ok = any(int(item.get("code") or -1) == 0 for item in result["results"][1:])
                if ok:
                    self.note("USB 自动模式：已尝试启动手机端并打开 USB 网络共享（%s）" % serial)
                else:
                    errors = "；".join(
                        "%s: %s" % (item["cmd"], item["err"] or item["out"] or item["code"])
                        for item in result["results"][1:] if int(item.get("code") or -1) != 0)
                    self.last_error = errors[:300]
                    self.note("USB 自动模式：adb 已连上，但系统不允许自动开共享；"
                              "请在手机下拉通知里点「USB 网络共享」（%s）" % serial)
            self._stop.wait(_POLL_S)
