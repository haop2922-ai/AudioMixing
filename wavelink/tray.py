"""托盘图标：程序在后台跑，右下角有个图标可以管它。

为什么要自己写
    不用第三方库。Windows 的 Shell_NotifyIcon 用 ctypes 直接调，几十行就够，
    打包出来的体积也不增加。

菜单
    左键双击   打开监控
    右键       打开监控 / 打开控制台 / 停止服务并退出
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import urllib.request
from ctypes import wintypes

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
ID_OPEN_MONITOR = 1001
ID_OPEN_PANEL = 1002
ID_DIAGNOSE = 1005
ID_FIREWALL = 1004
ID_QUIT = 1003

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]



class WNDCLASSW(ctypes.Structure):
    """窗口类结构。ctypes.wintypes 里没有，得自己写。"""
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _open_url(url: str) -> None:
    """用应用模式打开页面；不行就退回默认浏览器。"""
    try:
        from wavelink_launcher import _find_edge  # 同目录，源码运行时可用

        edge = _find_edge()
    except Exception:
        edge = ""
    if edge:
        try:
            profile = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                   "WaveLink", "panel-profile")
            args = [edge, "--app=" + url, "--window-size=1000,700",
                    "--no-first-run", "--no-default-browser-check"]
            try:
                os.makedirs(profile, exist_ok=True)
                args.append("--user-data-dir=" + profile)
            except OSError:
                pass
            subprocess.Popen(args, creationflags=_NO_WINDOW, close_fds=True)
            return
        except Exception:
            pass
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def add_firewall_rules() -> str:
    """给当前程序在防火墙上放行入站 TCP 和 UDP，返回批处理文件的路径。

    为什么写成批处理再以管理员运行：
        之前是把整串 netsh 命令拼成一个字符串交给 cmd，引号和大于号太容易出错，
        点了没反应也看不出哪里错。改成一行一条命令的批处理文件，路径用系统
        编码写（程序可能放在中文目录里），双击或右键以管理员运行都能用。
        弹不出确认窗口时，用户还可以自己右键那个文件运行。

    放行入站 UDP 同样重要：收不到对方的发现广播，对端就不知道你在哪。
    """
    import tempfile

    frozen = getattr(sys, "frozen", False)
    exe = sys.executable if frozen else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "wavelink_launcher.py"))

    # 不带 chcp：批处理文件用系统编码写，控制台保持系统编码，中文路径才不会变乱码
    lines = ["@echo off"]
    for proto in ("TCP", "UDP"):
        name = "WaveLink " + proto
        lines.append('netsh advfirewall firewall delete rule name="%s" >nul 2>&1' % name)
        lines.append('netsh advfirewall firewall add rule name="%s" dir=in action=allow '
                     'program="%s" protocol=%s enable=yes profile=any'
                     % (name, exe, proto))
        lines.append('netsh advfirewall firewall add rule name="%s-out" dir=out action=allow '
                     'program="%s" protocol=%s enable=yes profile=any'
                     % (name, exe, proto))
    lines.append("echo.")
    lines.append("echo 已为下面这个程序放行入站与出站 TCP/UDP：")
    lines.append("echo %s" % exe)
    lines.append("echo.")
    lines.append("netsh advfirewall firewall show rule name=\"WaveLink TCP\" | findstr /C:\"WaveLink TCP\"")
    lines.append("pause")

    path = os.path.join(tempfile.gettempdir(), "wavelink-firewall.bat")
    text = "\r\n".join(lines) + "\r\n"
    try:
        with open(path, "w", encoding="mbcs", errors="replace") as fh:
            fh.write(text)
    except OSError:
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)

    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", path, None, None, 1)
    except Exception:
        pass
    return path



def open_diagnose(address: str) -> None:
    """打开诊断报告，方便复制发人。"""
    url = address.rstrip("/") + "/api/diagnose"
    path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "WaveLink", "诊断报告.txt")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            text = resp.read().decode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    try:
        os.startfile(path)          # noqa: S606 - 用户自己点开的菜单
    except Exception:
        _open_url(address.rstrip("/") + "/monitor")



class Tray:
    """托盘图标与它的菜单。必须在主线程跑消息循环。"""

    def __init__(self, address: str, on_quit) -> None:
        self.address = address.rstrip("/")
        self.on_quit = on_quit
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        self.kernel32 = ctypes.windll.kernel32
        self.hwnd = None
        self._wndproc = None
        self._declare()

    def _declare(self) -> None:
        """声明用到的函数原型。

        不声明的话 ctypes 默认按 32 位整数传参和返回，句柄会被截断、
        大的 lparam 会报 OverflowError。这些声明是必须的，不是保险。
        """
        from ctypes import c_int, c_ssize_t, c_void_p

        u = self.user32
        u.DefWindowProcW.restype = c_ssize_t
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, c_int, c_int, c_int, c_int,
                                      wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE,
                                      c_void_p]
        u.RegisterClassW.argtypes = [c_void_p]
        u.RegisterClassW.restype = wintypes.WORD
        u.CreatePopupMenu.restype = wintypes.HANDLE
        u.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t,
                                  wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, c_int, c_int,
                                     wintypes.UINT, wintypes.HWND, c_void_p]
        u.TrackPopupMenu.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [c_void_p]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.DestroyMenu.argtypes = [wintypes.HANDLE]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [c_int]
        u.GetMessageW.argtypes = [c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = c_int
        u.TranslateMessage.argtypes = [c_void_p]
        u.DispatchMessageW.argtypes = [c_void_p]
        u.DispatchMessageW.restype = c_ssize_t
        # LoadIconW 的第二个参数既可能是字符串也可能是整数资源号，
        # 所以只声明返回值，不声明参数，交给 ctypes 自己转换。
        u.LoadIconW.restype = wintypes.HANDLE
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
        u.EnumWindows.argtypes = [c_void_p, wintypes.LPARAM]

        s = self.shell32
        s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, c_void_p]
        s.Shell_NotifyIconW.restype = wintypes.BOOL
        s.ExtractIconW.restype = wintypes.HANDLE
        s.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
        self.kernel32.GetModuleHandleW.restype = wintypes.HANDLE
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    # -- 窗口与图标 --------------------------------------------------------
    def _create_window(self) -> bool:
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def handler(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONDBLCLK:
                    _open_url(self.address + "/monitor")
                elif lparam == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == ID_OPEN_MONITOR:
                    _open_url(self.address + "/monitor")
                elif cmd == ID_OPEN_PANEL:
                    _open_url(self.address + "/")
                elif cmd == ID_DIAGNOSE:
                    open_diagnose(self.address)
                elif cmd == ID_FIREWALL:
                    add_firewall_rules()
                elif cmd == ID_QUIT:
                    self._remove_icon()
                    try:
                        self.on_quit()
                    except Exception:
                        pass
                    self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.user32.PostQuitMessage(0)
                return 0
            return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(handler)
        hinstance = self.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSW()
        cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        cls.hInstance = hinstance
        cls.lpszClassName = "WaveLinkTrayWnd"
        if not self.user32.RegisterClassW(ctypes.byref(cls)):
            err = self.kernel32.GetLastError()
            if err != 1410:                      # 1410 = 类已注册
                return False
        self.hwnd = self.user32.CreateWindowExW(0, "WaveLinkTrayWnd", "WaveLink", 0,
                                                0, 0, 0, 0, None, None, hinstance, None)
        return bool(self.hwnd)

    def _icon(self):
        # 优先用 exe 自带的图标，这样托盘和程序图标一致
        try:
            path = sys.executable if getattr(sys, "frozen", False) else ""
            if path:
                hicon = self.shell32.ExtractIconW(None, path, 0)
                if hicon and hicon != 1:
                    return hicon
        except Exception:
            pass
        return self.user32.LoadIconW(None, 32512)     # IDI_APPLICATION

    def _add_icon(self) -> bool:
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon()
        data.szTip = "WaveLink 正在运行，双击打开监控"
        return bool(self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))

    def _remove_icon(self) -> None:
        try:
            data = NOTIFYICONDATA()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            data.hWnd = self.hwnd
            data.uID = 1
            self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        except Exception:
            pass

    def _show_menu(self, hwnd) -> None:
        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, MF_STRING, ID_OPEN_MONITOR, "打开监控")
        self.user32.AppendMenuW(menu, MF_STRING, ID_OPEN_PANEL, "打开控制台")
        self.user32.AppendMenuW(menu, MF_STRING, ID_DIAGNOSE, "生成诊断报告")
        self.user32.AppendMenuW(menu, MF_STRING, ID_FIREWALL, "放行防火墙（需管理员）")
        self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self.user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "停止服务并退出")
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        # 菜单关闭前要有人 SetForegroundWindow，否则点别处菜单不消失
        self.user32.SetForegroundWindow(hwnd)
        self.user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
        self.user32.DestroyMenu(menu)

    # -- 对外 -------------------------------------------------------------
    def run(self) -> bool:
        if sys.platform != "win32":
            return False
        if not self._create_window():
            return False
        if not self._add_icon():
            return False
        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()
        return True
