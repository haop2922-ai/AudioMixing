"""桌面启动器：后台起服务，用应用模式打开监控窗。

界面只有一个，就是网页面板的监控页，用 Edge 的应用模式打开，没有地址栏和标签页，
看起来就是一个普通程序窗口。原来的 tkinter 窗口已经删除，因为它和网页面板是两套
外观，既不好看也要多维护一份。

监控窗显示：谁给谁数据、走哪个通道、数据量多少、延迟多少。

两种用法
    双击 exe                    起服务并打开监控窗
    exe serve 或带其他参数       走命令行，行为和以前一样
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# HWHHYN 用 DEEPSEEK 制作
def _log_path() -> str:
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Audio Mixing")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.expanduser("~")
    return os.path.join(base, "launcher.log")


def _log(message: str) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


class _NullWriter:
    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


def _silence_std_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()

# HWHHYN 用 DEEPSEEK 制作

def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _find_edge() -> str:
    for path in (
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge",
                     "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge",
                     "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge",
                     "Application", "msedge.exe"),
    ):
        if path and os.path.isfile(path):
            return path
    return ""


def _window_handle() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.FindWindowW(None, "Audio Mixing 监控") or 0)
    except Exception:
        return 0


def _raise_window(handle: int) -> None:
    if not handle or sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.ShowWindow(handle, 9)
        user32.SetForegroundWindow(handle)
    except Exception:
        pass


def _port_owner(port: int) -> int:
    if sys.platform != "win32":
        return 0
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], creationflags=_NO_WINDOW,
                             capture_output=True, text=True, timeout=15).stdout or ""
    except Exception:
        return 0
    needle = ":%d" % port
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[1].endswith(needle) and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return 0


def _stop_port_owner(port: int) -> bool:
    pid = _port_owner(port)
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=_NO_WINDOW,
                       capture_output=True, timeout=15)
        _log("已结束无界面的服务 PID %d" % pid)
        return True
    except Exception as exc:
        _log("结束 PID %d 失败: %s" % (pid, exc))
        return False



def _wait_for_monitor_window(seconds: float) -> bool:
    """等监控窗出现。标题带「Audio Mixing 监控」就算出现。"""
    if sys.platform != "win32":
        return True
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            found = [False]

            from ctypes import wintypes

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def callback(hwnd, _lparam):
                if user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    if "Audio Mixing" in buf.value:
                        found[0] = True
                        return False
                return True

            user32.EnumWindows(WNDENUMPROC(callback), 0)
            if found[0]:
                return True
        except Exception:
            return True
        time.sleep(0.7)
    return False


def _close_panel_windows() -> int:
    """关掉我们打开过的监控窗和控制台窗，返回关掉几个。

    这些窗口是 Edge 的应用模式，启动时带了 Audio Mixing 自己的配置目录，
    所以命令行里有独一无二的标记，可以精确识别。用户自己的浏览器窗口
    没有这个标记，不会被误关。
    """
    if sys.platform != "win32":
        return 0
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*panel-profile*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             creationflags=_NO_WINDOW, capture_output=True, text=True,
                             timeout=20).stdout or ""
    except Exception as exc:
        _log("列出面板窗口进程失败: %s" % exc)
        return 0
    closed = 0
    killed = set()
    for line in out.splitlines():
        pid = line.strip()
        if not pid.isdigit() or pid in killed:
            continue
        killed.add(pid)
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], creationflags=_NO_WINDOW,
                           capture_output=True, timeout=15)
            closed += 1
        except Exception as exc:
            _log("关闭面板窗口 PID %s 失败: %s" % (pid, exc))
    if closed:
        _log("已关闭 %d 个面板窗口" % closed)
    return closed


def open_monitor(address: str) -> None:
    """用应用模式打开监控页。独立的用户配置目录，保证是独立窗口而不是标签页。"""
    url = address.rstrip("/") + "/monitor"
    edge = _find_edge()
    if edge:
        try:
            profile = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                   "Audio Mixing", "panel-profile")
            args = [edge, "--app=" + url, "--window-size=1000,700",
                    "--no-first-run", "--no-default-browser-check"]
            try:
                os.makedirs(profile, exist_ok=True)
                args.append("--user-data-dir=" + profile)
            except OSError as exc:
                _log("建配置目录失败: %s" % exc)
            subprocess.Popen(args, creationflags=_NO_WINDOW, close_fds=True)
            _log("已请求应用模式打开监控窗")
            # 应用模式不一定真的出窗口：这台机器上 Edge 有时会弹「无法更新」而开不出来。
            # 等几秒确认窗口在，不在就退回默认浏览器，不让用户对着空白等。
            if _wait_for_monitor_window(6.0):
                _log("监控窗已出现")
                return
            _log("应用模式没有出窗口，改用默认浏览器")
        except Exception as exc:
            _log("应用模式失败: %s" % exc)
    try:
        import webbrowser

        webbrowser.open(url)
        _log("已用默认浏览器打开监控页")
    except Exception as exc:
        _log("打不开监控页: %s" % exc)


def _existing_node_address() -> str:
    """已经有节点在跑就返回它的地址，否则空串。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:17800/healthz",
                                    timeout=2.5) as resp:
            if json.loads(resp.read().decode("utf-8")):
                return "http://127.0.0.1:17800"
    except Exception:
        pass
    return ""


def run_service() -> int:
    """起节点并打开监控窗，自己不占窗口。"""
    try:
        from wavelink.config import Config
        from wavelink.engine import WaveLinkNode
        from wavelink.net import discovery as disc
        from wavelink.webui import WebUI

        cfg = Config.load()
        port = int(cfg.web_port)
        node = WaveLinkNode(cfg, audio_port=cfg.audio_port,
                            discovery_port=cfg.discovery_port, simulate=False)
        ui = WebUI(node, host="0.0.0.0", port=port)
        node.start()
        port = int(ui.start() or port)
        address = "http://127.0.0.1:%d" % port
        try:
            for item in disc.lan_report()["interfaces"]:
                if item["kind"] == "private":
                    address = "http://%s:%d" % (item["address"], port)
                    break
        except Exception as exc:
            _log("取局域网地址失败: %s" % exc)
        _log("服务已启动 %s" % address)
        open_monitor(address)

        def stop_everything() -> None:
            """托盘菜单里选退出时走这里：先停服务，再结束进程。"""
            _log("从托盘退出")
            try:
                ui.stop()
            except Exception as exc:
                _log("停止面板失败: %s" % exc)
            try:
                node.stop()
            except Exception as exc:
                _log("停止节点失败: %s" % exc)
            # 退出就该退干净：把程序自己开的窗口也关掉，
            # 否则图标没了、声音停了，窗口还留在桌面上。
            _close_panel_windows()

        # 托盘图标是唯一的控制入口：程序没有窗口，所以必须有个地方能停它。
        from wavelink.tray import Tray

        tray = Tray(address, on_quit=stop_everything)
        _log("准备在托盘中显示图标")
        if tray.run():
            _log("托盘图标已退出，进程结束")
            return 0
        _log("托盘不可用，退化为无界面常驻")
    except Exception:
        detail = traceback.format_exc()
        # 端口被占是最常见的一种：另一个实例已经在这个端口上跑着，
        # 这时不该报错，而应该打开那个实例的监控窗然后安静退出。
        existing = _existing_node_address()
        if existing:
            _log("已经有节点在 %s 上运行，改为打开它的监控窗" % existing)
            open_monitor(existing)
            return 0
        _log("启动失败\n" + detail)
        return 1

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _log("收到中断，退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    _silence_std_streams()
    _enable_dpi_awareness()
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv:
        from wavelink.cli import main as cli_main

        return cli_main(argv)

    # 判断依据只能是"服务在不在跑"，不能是"有没有监控窗"。
    # 已经打开的监控窗在关掉之前一直存在，而它关了并不代表服务停了；
    # 反过来若拿窗口当依据，就会出现「窗口还在、服务没了」时程序拒绝启动。
    running = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:17800/healthz", timeout=1.5) as resp:
            json.loads(resp.read().decode("utf-8"))
        running = True
    except Exception:
        running = False

    if not running:
        # 健康检查失败也可能只是那一瞬间忙：再看一眼是不是端口被占着
        existing = _existing_node_address()
        if existing:
            running = True

    if running:
        # 服务在跑：把监控窗叫到前面就够了，不要再起一个
        handle = _window_handle()
        if handle:
            _log("服务在跑，监控窗已存在，提到最前面")
            _raise_window(handle)
        else:
            _log("服务在跑，打开监控窗")
            open_monitor("http://127.0.0.1:17800")
        return 0

    # 服务不在跑：这里可能是上一轮留下的无界面进程占着端口
    if _stop_port_owner(17800):
        time.sleep(4.0)

    return run_service()


if __name__ == "__main__":
    raise SystemExit(main())
