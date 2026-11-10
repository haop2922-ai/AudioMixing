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
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# HWHHYN 用 DEEPSEEK 制作
def _log_path() -> str:
    """日志放哪：优先跟配置目录走，其次 %APPDATA%\\AudioMixing，最后用户主目录。

    ★ 2026-10-21：用户账户是中文名时，路径里全是中文（C:\\Users\\张三\\AppData\\...）。
      原来这里只认 APPDATA，自检/沙箱运行时（AUDIOMIXING_HOME 指向临时目录）会把日志
      写进真实配置旁边；而且一旦 APPDATA 不可写就悄悄丢日志。现在先认 AUDIOMIXING_HOME，
      和 config.config_dir() 保持同一口径，中文路径也原样交给 Python/Windows 的宽字符 API。
    """
    override = os.environ.get("AUDIOMIXING_HOME") or os.environ.get("WAVELINK_HOME")
    candidates = []
    if override:
        candidates.append(override)
    candidates.append(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AudioMixing"))
    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            return os.path.join(base, "launcher.log")
        except OSError:
            continue
    return os.path.join(os.path.expanduser("~"), "launcher.log")


def _log(message: str) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except (OSError, UnicodeError):
        pass


def _log_environment() -> None:
    """把本次运行的真实环境写进日志 —— 中文路径问题只靠猜是查不出来的。"""
    try:
        comp = globals().get("__compiled__")
        fields = {}
        if comp is not None:
            for attr in ("original_argv0", "containing_dir", "containing_dir_absolute", "main_dir"):
                try:
                    fields[attr] = str(getattr(comp, attr, ""))
                except Exception:
                    pass
        _log("启动环境: Python=%s 系统=%s 架构=%s" %
             (sys.version.split()[0], platform.platform(), platform.machine()))
        _log("启动环境: executable=%r argv0=%r cwd=%r" %
             (sys.executable, (sys.argv or [""])[0], os.getcwd()))
        _log("启动环境: APPDATA=%r TEMP=%r" %
             (os.environ.get("APPDATA"), tempfile.gettempdir()))
        _log("启动环境: frozen=%r compiled=%r" %
             (getattr(sys, "frozen", False), fields or None))
    except Exception:
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


def _monitor_window_title(text: str) -> bool:
    """这个窗口标题是不是我们打开的监控/控制台/诊断窗。

    ★ 2026-10-26 用户现场：任务栏堆了十几个「Audio Mixing 监控」窗口。
      旧代码用 FindWindowW 精确匹配「Audio Mixing 监控」——用户一切到「控制台」
      或「诊断」，标题就变了，精确匹配立刻失配，再双击就又开一个新窗。
      改成前缀匹配，并排除普通浏览器标签页的标题后缀。
    """
    t = str(text or "").strip()
    if not t.startswith("Audio Mixing"):
        return False
    # ★ 只排除普通浏览器标签页的尾巴；不要再排 " - 个人" / " - 工作"
    #   —— Edge 应用模式带配置名时标题就长这样，排掉会把真窗口漏掉。
    for suffix in (" - Microsoft", " - Google Chrome", " - Mozilla", " - Edge"):
        if suffix in t:
            return False
    return True


def _window_handle() -> int:
    """找已经打开的监控窗（任意页面），返回 HWND；没有返回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = [0]

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if _monitor_window_title(buf.value):
                found[0] = int(hwnd)
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return found[0]
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
        # 中文 Windows 上 netstat 的输出是 GBK：只写 text=True 会按 UTF-8 解码并抛
        # UnicodeDecodeError，被下面的 except 吞掉 → 悄悄返回 0。后果很严重：
        # 接管永远拿不到 PID，旧实例永远关不掉，新旧两个实例一起抢 17800
        # （2026-10-14 实测）。加 encoding/errors 后 ASCII 关键字一定能解析出来。
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], creationflags=_NO_WINDOW,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15).stdout or ""
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


def _pid_path(pid: int) -> str:
    """PID → 可执行文件完整路径；拿不到就是空串（绝不猜）。"""
    if sys.platform != "win32" or not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            k32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD)]
            ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return str(buf.value) if ok else ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""


def _is_our_path(path: str) -> bool:
    """这一条路径是不是 Audio Mixing 自己的 exe（新旧版本都算）。

    ★ 2026-11-01：用户的另一个同类软件也用同一组端口。旧代码只看
      "谁占着端口"就直接 taskkill，会误杀别人。这里只认自己的文件名。
    """
    name = os.path.basename(str(path or "")).strip().lower()
    return ("audiomixing" in name) or ("audio mixing" in name)


def _stop_port_owner(port: int) -> bool:
    pid = _port_owner(port)
    if not pid:
        return False
    path = _pid_path(pid)
    if not _is_our_path(path):
        _log("端口 %d 被其他程序占用（PID %d，%s），不结束它；交给端口探测自动换口"
             % (port, pid, path or "路径未知"))
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=_NO_WINDOW,
                       capture_output=True, timeout=15)
        _log("已结束无界面的服务 PID %d" % pid)
        return True
    except Exception as exc:
        _log("结束 PID %d 失败: %s" % (pid, exc))
        return False



_TAKE_OVER = "takeover"
_DEFER = "defer"

# 进程级单实例锁的句柄。必须一直活着：句柄一关，锁就没了。
_SINGLETON_HANDLE = None


def _singleton_name() -> str:
    """同一台机器上，本程序只允许一个"服务实例"。

    名字**不带版本号**：v2.4 和 v2.5 也共用同一把锁，否则新旧版本会各起一个引擎。
    Local 前缀表示只在当前登录会话内有效，别的用户登录不互相干扰。
    """
    return "Local\\AudioMixing-Service-Singleton"


def _acquire_singleton() -> bool:
    """尝试拿单实例锁；拿到返回 True，已被别的本程序进程拿着返回 False。

    ★ 2026-10-26 用户真机：「打开软件时任务管理器里是两个软件，音频一卡一卡」。
      原来的唯一判据是"17800 上有没有健康检查应答"，而 `run_service()` 是
      **先 node.start()（占音频/发现端口）、再 ui.start()（才开网页端口）** ——
      从双击到 17800 能应答之间有几秒的空窗。用户在这几秒里再双击一次，
      第二个进程也会判定"没人在跑"，于是两个引擎同时活着：
        · 音频端口/发现端口都用 SO_REUSEADDR 绑定，Windows 允许两个都绑上；
        · 两个播放端各自往同一块声卡写同一路声音；
        · 两个面板互相推路由表。
      表现就是重复、断续、时大时小的声音，且任务管理器里真的有两个实例。
      命名互斥体在内核里，进程一退出（包括崩溃、强杀）就自动释放，没有残留。
    """
    global _SINGLETON_HANDLE
    if sys.platform != "win32":
        return True
    if _SINGLETON_HANDLE is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateMutexW(None, False, _singleton_name())
        if not handle:
            _log("单实例锁创建失败，退回端口检查")
            return True
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return False
        _SINGLETON_HANDLE = handle
        return True
    except Exception as exc:
        # 锁本身出问题时宁可放行（端口检查还在），也别让程序打不开。
        _log("单实例锁异常（放行，端口检查兜底）: %s" % exc)
        return True


def _wait_singleton(seconds: float) -> bool:
    """等另一个实例退出后再拿锁（接管旧版本时用）。"""
    deadline = time.time() + max(0.0, seconds)
    while True:
        if _acquire_singleton():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _parse_version(text: object) -> tuple:
    """"1.13" / "Audio Mixing v1.13" → (1, 13)；读不出来就是空元组。"""
    import re

    found = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if not found:
        return ()
    try:
        return tuple(int(x) for x in found.group(1).split("."))
    except ValueError:
        return ()


def decide_takeover(running_version: object, our_version: object) -> str:
    """运行中的实例该不该被新版本顶掉。

    只按版本号比：比我们旧 → 接管（关掉它）；同版或更新 → 让位（不动它）。
    任何一方版本读不出来就保守让位 —— 宁可多开一个窗，也绝不乱杀进程。

    2026-10-14 用户的真实现场：双击新版，旧版还在跑，新版只是把旧版的面板
    叫到前面就退出了 —— 于是他测的始终是旧代码。「新版要把旧版关掉」是他明确
    提出的要求，这里就是那个判据（纯函数，好测）。
    """
    mine = _parse_version(our_version)
    theirs = _parse_version(running_version)
    if not mine or not theirs:
        return _DEFER
    return _TAKE_OVER if theirs < mine else _DEFER


def _our_version() -> str:
    try:
        from audiomixing import __version__

        return str(__version__)
    except Exception:
        return ""


def _configured_web_port() -> int:
    """面板端口以配置文件为准 —— 用户在「设置」里改过端口后，启动器不能还认 17800。"""
    try:
        from audiomixing.config import Config

        port = int(Config.load().web_port)
        if 1024 <= port <= 65534:
            return port
    except Exception as exc:
        _log("读取面板端口失败（回退 17800）: %s" % exc)
    return 17800


def _running_status(timeout: float = 2.5, port: int | None = None):
    """读面板端口上那个实例的 /api/status；不是本程序（读不到）就返回 None。"""
    port = int(port or _configured_web_port())
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port,
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    # 只认自家产品：status 必须同时带 node 和 build 两段，并且验明身份。
    # 新版本带 app 键；旧版本没有 app 键时按端口占用者的 exe 路径认亲。
    if not (isinstance(data, dict) and isinstance(data.get("node"), dict)
            and isinstance(data.get("build"), dict)):
        return None
    path = _pid_path(_port_owner(port))
    if path:
        if not _is_our_path(path):
            _log("端口 %d 上的实例不是 Audio Mixing（%s），不认作本程序" % (port, path))
            return None
    elif data.get("app") != "audiomixing":
        _log("端口 %d 上的实例路径未知且没有 app 标记，不认作本程序" % port)
        return None
    return data


def _message_box(text: str, title: str = "Audio Mixing") -> None:
    """程序没有控制台，又必须如实告诉用户 —— 用系统弹窗。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception as exc:
        _log("弹窗失败: %s" % exc)


def _show_failure(detail: str) -> None:
    """启动失败时把最后几行异常贴到用户脸上，而不是写进日志就安静退出。

    ★ 2026-10-21：用户反馈“换了台电脑双击完全没反应”。原来的 run_service 把异常
      写进 launcher.log 就 return 1，没有控制台也没有弹窗，用户只能看到“什么都没发生”。
      现在失败一定会弹一个框，并给出日志路径；日志里还有启动环境（中文路径也在内）。
    """
    tail = "\n".join((detail or "").strip().splitlines()[-8:])
    _message_box("Audio Mixing 启动失败。\n\n%s\n\n完整日志：\n%s" % (tail, _log_path()))


def _take_over(pid: int, port: int | None = None) -> bool:
    """关掉占着面板端口的旧版本（连它的子进程），并把它开过的面板窗一并关掉。

    只杀端口占用者这一个进程树，不做任何按名字的横扫 —— 杀错别人的程序
    比"没能接管"严重得多。
    """
    port = int(port or _configured_web_port())
    if pid and pid != os.getpid() and not _is_our_path(_pid_path(pid)):
        _log("端口 %d 的 PID %d 不是 Audio Mixing（%s），拒绝接管"
             % (port, pid, _pid_path(pid) or "路径未知"))
        return False
    if pid and pid != os.getpid():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           creationflags=_NO_WINDOW, capture_output=True, timeout=15)
            _log("已请求关闭旧版本 PID %d" % pid)
        except Exception as exc:
            _log("关闭旧版本 PID %d 失败: %s" % (pid, exc))
    _close_panel_windows()
    for _ in range(12):
        time.sleep(0.5)
        if _port_owner(port) == 0:
            return True
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
                    if _monitor_window_title(buf.value):
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
                             encoding="utf-8", errors="replace", timeout=20).stdout or ""
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


_MONITOR_LOCK_NAME = "Local\\AudioMixing-Monitor-Open"


def _acquire_monitor_open_lock(timeout_ms: int = 15000) -> int:
    """跨进程串行化“打开监控窗”，成功后返回互斥体句柄（0=没拿到）。

    这把锁只负责“谁去开窗”，和单实例锁是两回事：服务已经在跑时，多个双击
    进程都会走到“没有监控窗”这一步；没有这把锁就会各开一个 Edge 窗口。
    等待超时说明另一个进程正在开，调用方只 FindWindow 提窗、不再重复开。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        handle = k32.CreateMutexW(None, False, _MONITOR_LOCK_NAME)
        if not handle:
            return 0
        waited = k32.WaitForSingleObject(handle, int(timeout_ms))
        if waited in (0x00000000, 0x00000080):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            return int(handle)
        k32.CloseHandle(handle)
        return 0
    except Exception:
        return 0


def _release_monitor_open_lock(handle: int) -> None:
    if not handle:
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.ReleaseMutex(handle)
        k32.CloseHandle(handle)
    except Exception:
        pass


def _monitor_process_exists() -> bool:
    """是否已经有一个带面板配置目录的 Edge 应用进程。

    ★ 2026-10-26 用户反馈：窗口标题在真机上不一定能认出来（Edge 版本/配置名
      会改标题）。如果只靠标题判断，每次双击都还会再开一个。这里直接看进程：
      有 `--app=` 且带 `panel-profile` 的 msedge 在，就认定“监控窗已经有了”，
      只记日志，不再开新窗。
    """
    if sys.platform != "win32":
        return False
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*panel-profile*' -and "
        "$_.CommandLine -like '*--app=*' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             creationflags=_NO_WINDOW, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout or ""
        return bool(out.strip())
    except Exception:
        return False


def open_monitor(address: str, page: str = "monitor") -> None:
    """打开/提到监控窗。同一时刻只允许一个进程真正去开，避免窗口无限堆叠。

    ★ 2026-10-26 用户现场：任务栏里堆了十几个「Audio Mixing 监控」。
      原因有三层：
        1. 服务已就绪、但监控窗还没出现的那几秒里，多个双击实例各开一次；
        2. 窗口检查用了精确标题，切到控制台/诊断就失配；
        3. 托盘双击走的是另一条 `_open_url`，完全绕过去重。
      这一版用跨进程锁串行化“检查 → 打开 → 等窗口”，并加进程级兜底：
      只要已经有一个带 panel-profile 的 Edge 应用进程，就绝不再开新窗。
    """
    if sys.platform == "win32":
        lock = _acquire_monitor_open_lock(15000)
        if not lock:
            existing = _window_handle()
            if existing:
                _raise_window(existing)
                _log("监控窗已在打开/已存在，提到最前面")
            else:
                _log("另一个实例正在打开监控窗，本进程不再重复开")
            return
        try:
            existing = _window_handle()
            if existing:
                _raise_window(existing)
                _log("监控窗已存在，提到最前面")
                return
            if _monitor_process_exists():
                _log("已有监控窗进程（标题未识别），不再开第二个")
                return
            _open_monitor_unlocked(address, page)
        finally:
            _release_monitor_open_lock(lock)
        return
    _open_monitor_unlocked(address, page)


def _open_monitor_unlocked(address: str, page: str = "monitor") -> None:
    """真正去拉起监控窗；调用方必须已经拿到 `_MONITOR_LOCK_NAME`。"""
    url = address.rstrip("/") + ("/" + page.lstrip("/") if page else "/")
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
            # ★ 2026-10-26：Edge 已经拉起了就绝不再 webbrowser.open —— 那会多开一个
            #   标题认不出来的普通浏览器页；下一次双击又会再开一个，正是“无限开”的放大器。
            if _wait_for_monitor_window(10.0):
                _log("监控窗已出现")
                return
            _log("Edge 应用模式没有出现可识别窗口；不再退回浏览器")
            _message_box("服务已经启动，但监控窗没有自动出现。\n\n"
                         "请手动打开：\n%s\n\n"
                         "如果任务栏里已经有「Audio Mixing」，点它即可。" % url)
            return
        except Exception as exc:
            _log("应用模式失败: %s" % exc)
    try:
        import webbrowser

        opened = bool(webbrowser.open(url))
        _log("已用默认浏览器打开监控页" if opened else "默认浏览器没有打开监控页")
    except Exception as exc:
        opened = False
        _log("打不开监控页: %s" % exc)
    if not opened:
        _message_box("服务已经启动，但没能自动打开监控窗口。\n\n"
                     "请在浏览器里手动打开：\n%s" % url)


def _existing_node_address(port: int | None = None) -> str:
    """已经有**本程序**的节点在跑就返回它的地址，否则空串。

    ★ 2026-11-01：同类软件也会在同一个端口上回一个 JSON。旧代码只要
      "能解析出 JSON" 就当成本程序，于是会把对方的窗口当自己的打开，
      甚至后续接管时把人家的进程杀掉。现在必须验明身份：
      新版本 healthz 里带 `"app":"audiomixing"`；
      旧版本没有这个键，就用端口占用者的 exe 路径来判断。
    """
    port = int(port or _configured_web_port())
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port,
                                    timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    # 路径是最硬的证据：同类软件就算抄了 app 键，只要 exe 不是我们的就不认。
    path = _pid_path(_port_owner(port))
    if path:
        return ("http://127.0.0.1:%d" % port) if _is_our_path(path) else ""
    # 拿不到路径（权限/进程刚退出）才退回 app 键
    return ("http://127.0.0.1:%d" % port) if data.get("app") == "audiomixing" else ""


def run_service() -> int:
    """起节点并打开监控窗，自己不占窗口。"""
    try:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.net import discovery as disc
        from audiomixing.webui import WebUI

        cfg = Config.load()
        # ★ 2026-11-01：另一个基于本项目做的软件会用同一组默认端口。
        #   两个 UDP socket 都设了 SO_REUSEADDR，Windows 会允许两边都绑上，
        #   于是两个程序抢同一组端口、现场表现为"两个一起死"。
        #   这里在引擎启动前用**独占绑定**逐口探测；冲突就自动换成下一组空闲端口，
        #   并把新端口写回配置。手机端**只需要填相同的发现端口**：音频端口在信标里
        #   公布，电脑按它发过去（2026-11-01 用户要求：只统一发现端口）。
        from audiomixing import ports as port_guard

        moved = port_guard.ensure_ports(cfg)
        if moved:
            _log("端口冲突，已自动改用空闲端口: %s" % moved)
            _message_box(
                "检测到端口被另一个程序占用（可能是另一个同类软件）。\n\n"
                "已自动改用空闲端口：\n"
                "  面板 TCP   %d\n"
                "  音频 UDP   %d\n"
                "  发现 UDP   %d\n\n"
                "手机端请在「详细 → 端口设置」里把「发现端口」填成 %d，\n"
                "保存后重启手机 App。音频端口由信标自动协商，不需要手填。"
                % (cfg.web_port, cfg.audio_port, cfg.discovery_port, cfg.discovery_port)
            )
        port = int(cfg.web_port)
        wanted_port = port
        node = AudioMixingNode(cfg, audio_port=cfg.audio_port,
                            discovery_port=cfg.discovery_port, simulate=False)
        # ★ 2026-11-01 用户：「修改端口号后自动重启」—— 桌面形态无参数重启，
        #   启动器会重新读配置、按新端口开面板窗口。
        ui = WebUI(node, host="0.0.0.0", port=port, restart_argv=[])
        node.start()
        # 面板上"声音大小"那根条 / 圆心那个百分比来自**直读当前输出设备**的电平表 ——
        # 手机走蓝牙进来时，播放可能由 Windows 或其他程序完成，本工具那条路不一定有数据；
        # 直读输出设备仍然能看到"现在到底有没有声音"。
        node.enable_output_meter()
        port = int(ui.start() or port)
        if port != wanted_port:
            # ★ 2026-10-26：WebUI 在 17800 被占时会悄悄往 17801/17802 顺延，但
            #   node.start() 里的信标已经拿着 17800 广播出去了（手机/另一台电脑
            #   找过来就是空的），面板自己还会把实际端口写进配置。这种现象比
            #   "启动失败"更难查：看着哪儿都活着，就是连不上。
            #   单实例锁已经保证不是本程序的另一个实例占着它，所以这里直接报错。
            try:
                ui.stop()
            except Exception:
                pass
            try:
                node.stop()
            except Exception:
                pass
            raise OSError("网页端口 %d 已被别的程序占用（实际只能绑到 %d）。"
                          "请关掉占用该端口的程序，或把端口改回空闲的再启动。"
                          % (wanted_port, port))
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
            # ★ 用户真机反馈：「关闭服务后，手机声音依旧通过蓝牙传入电脑」。
            #   蓝牙连接的寿命绑在助手子进程上，主进程退掉并不会挂断它 —— 必须显式
            #   把所有还握着的连接放掉（见 bluetooth.disconnect_all 的说明）。
            try:
                from audiomixing import bluetooth as _bt

                result = _bt.disconnect_all()
                _log("退出时挂断蓝牙连接 %d 个（失败 %d 个）"
                     % (result.get("count", 0), len(result.get("failed") or [])))
            except Exception as exc:
                _log("挂断蓝牙连接失败: %s" % exc)
            # 退出就该退干净：把程序自己开的窗口也关掉，
            # 否则图标没了、声音停了，窗口还留在桌面上。
            _close_panel_windows()

        # 托盘图标是唯一的控制入口：程序没有窗口，所以必须有个地方能停它。
        # ★ 2026-10-26：把 open_monitor 直接注入给托盘，冻结包里不靠 import 主模块。
        from audiomixing import tray as tray_module

        tray_module.set_panel_opener(open_monitor)
        tray = tray_module.Tray(address, on_quit=stop_everything)
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
        _show_failure(detail)
        return 1

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _log("收到中断，退出")
    return 0


def _main(argv: list[str] | None = None) -> int:
    _silence_std_streams()
    _enable_dpi_awareness()
    _log_environment()
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv:
        from audiomixing.cli import main as cli_main

        return cli_main(argv)

    # ★ 2026-10-26：先抢内核里的单实例锁，再谈"面板端口有没有应答"。
    #   端口应答在启动完成前是空的，只靠它判断会让第二次双击也起一个完整引擎。
    # ★ 2026-10-30（v2.10）：端口不再写死 17800 —— 用户在「设置」里改过之后，
    #   这里必须跟着配置走，否则第二次双击会去错地方、还误报"没有实例在跑"。
    web_port = _configured_web_port()
    has_lock = _acquire_singleton()
    if not has_lock:
        _log("单实例锁已被占用：已有一个 Audio Mixing 在启动或运行")
        # 给它最多 15 秒把服务开起来（单文件版要先解包，慢的时候确实要几秒）。
        # 这里只等，绝不在这期间去起第二个引擎。
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if _existing_node_address():
                break
            time.sleep(0.5)

    # 判断依据只能是"服务在不在跑"，不能是"有没有监控窗"。
    # 已经打开的监控窗在关掉之前一直存在，而它关了并不代表服务停了；
    # 反过来若拿窗口当依据，就会出现「窗口还在、服务没了」时程序拒绝启动。
    # 必须是**本程序**在应答，不能只看"这个端口上有东西回 JSON"。
    # 同类软件也用同一组端口；认错会把它的窗口当自己的、甚至接管时误杀它。
    existing = _existing_node_address(web_port)
    running = bool(existing)

    if running:
        # 服务在跑。先问清"跑的是谁、哪一版"，再决定顶掉它还是让位。
        # 2026-10-14 用户的真实现场：双击新版，旧版还在跑，新版只是把旧版的面板
        # 叫到前面就退出 —— 他测的始终是旧代码。新版必须能把旧版关掉。
        status = _running_status(port=web_port)
        if status is not None:
            running_version = (status.get("build") or {}).get("version") or ""
            if decide_takeover(running_version, _our_version()) == _TAKE_OVER:
                _log("面板端口 %d 上是旧版本 v%s，本版 v%s 接管"
                     % (web_port, running_version, _our_version()))
                if not _take_over(_port_owner(web_port), web_port):
                    _log("接管失败：旧版本 v%s 没能关掉" % running_version)
                    _message_box("检测到旧版本 v%s 在运行，但没能把它关掉。\n"
                                 "请手动退出它（托盘图标 → 退出），再双击本程序。"
                                 % running_version)
                    return 1
                _log("旧版本已关闭，端口已让出，开始启动本版")
                # 旧进程的锁可能还没释放完（进程刚被杀）；等它一下再起新服务。
                if not _wait_singleton(6.0):
                    _log("旧版本已关闭，但单实例锁迟迟没有释放")
                    _message_box("旧版本已关闭，但它占着的启动锁还没释放。\n"
                                 "请等几秒再双击本程序。")
                    return 1
                has_lock = True
                return run_service()
            # 同版或更新版：把它的监控窗叫到前面就够了，不要再起一个
            handle = _window_handle()
            if handle:
                _log("服务在跑（v%s），监控窗已存在，提到最前面" % running_version)
                _raise_window(handle)
            else:
                _log("服务在跑（v%s），打开监控窗" % running_version)
                open_monitor("http://127.0.0.1:%d" % web_port)
            return 0
        # 读不到状态：端口上确实有东西在应答，但认不出是哪一版。
        # 这时绝不乱杀（杀错别人的程序更糟），沿用旧行为把面板打开。
        handle = _window_handle()
        if handle:
            _log("服务在跑（读不到版本），提窗")
            _raise_window(handle)
        else:
            _log("服务在跑（读不到版本），打开监控窗")
            open_monitor("http://127.0.0.1:%d" % web_port)
        return 0

    if not has_lock:
        # 走了 15 秒还没有健康应答，但锁仍在别的进程手里：那是有实例在启动/卡住。
        # 这时候再起一个引擎就会变成"两个软件一起往一块声卡写"。
        _log("另一个实例仍未就绪，本进程不启动第二个引擎")
        _message_box("已有一个 Audio Mixing 正在启动（单文件版可能正在解包），请稍等几秒再双击。\n\n"
                     "如果一直这样：在任务管理器「详细信息」里结束所有 Audio Mixing 进程，"
                     "再重新打开。")
        return 1

    # 服务不在跑：这里可能是上一轮留下的无界面进程占着端口
    if _stop_port_owner(web_port):
        time.sleep(4.0)

    return run_service()


def main(argv: list[str] | None = None) -> int:
    """最外层兜底：连 _main 里的导入/环境错误也要让用户看见。"""
    try:
        return _main(argv)
    except Exception:
        detail = traceback.format_exc()
        _log("启动器顶层异常\n" + detail)
        _show_failure(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
