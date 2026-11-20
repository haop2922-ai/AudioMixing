"""Windows 默认播放设备：读取 + 切换（IPolicyConfig）。

★ Q2（2026-11-16 用户要求）：
   多耳机开启时把 Windows 默认播放强制锁到一块专用输出，根治
   「选了 Windows 默认设备是耳机，一开多耳机就炸声音」。

边界（写死，不要扩大）：
   只改**系统默认播放端点**（eConsole 角色），不碰任何 App 内部音量，
   也不碰本项目的增益链 —— 「两端合计只乘一次」不变式保持不变。

实现说明：
   Windows 没有公开 API 改默认设备，这里用系统自带的 IPolicyConfig COM 接口
   （CLSID 870af99c-171d-4f9e-af0d-e63df40c2bc9）的 SetDefaultEndpoint。
   它和「设置 → 系统 → 声音 → 输出」下拉选中的是同一件事。
"""

from __future__ import annotations

import sys
import threading
from typing import Any

from . import base as audio_base

# 用户口中的设备名。系统里没有同名设备时不会猜，直接在页面上报中文原因，
# 并列出真实可选的设备，让人自己选。
DEFAULT_LOCK_NAME = "AUDIO音频"

_ROLE_CONSOLE = 0  # ERole.eConsole：系统默认播放（不是通信设备）
_CLSID_POLICY = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"
_IID_POLICY = "{f8679f50-850a-41cf-9c72-430f290290c8}"
_SET_DEFAULT_INDEX = 13  # IUnknown(3) + IPolicyConfig 前 10 个方法之后

_guard = threading.Lock()


def _new_guid(text: str):
    import ctypes
    from ctypes import c_uint32
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_uint32), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    guid = GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    if hr != 0:
        raise RuntimeError("CLSIDFromString 失败（%s）" % text)
    return guid


def _open_policy() -> tuple[Any, Any]:
    """打开 IPolicyConfig（CoCreateInstance）；失败抛中文 RuntimeError。"""
    import ctypes
    from ctypes import byref, c_void_p

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)  # STA；重复调用是引用计数，无害
    ptr = c_void_p()
    hr = ole32.CoCreateInstance(byref(_new_guid(_CLSID_POLICY)), None, 1,
                                byref(_new_guid(_IID_POLICY)), byref(ptr))
    if hr != 0 or not ptr.value:
        raise RuntimeError("拿不到系统策略接口（CoCreateInstance hr=0x%08X）" % (hr & 0xFFFFFFFF))
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(c_void_p)))[0]
    return ptr, vtbl


def _set_default_endpoint(endpoint_id: str) -> str:
    """把 eConsole 默认播放端点切到 endpoint_id；成功返回空串。"""
    import ctypes

    if sys.platform != "win32":
        return "只有 Windows 需要锁默认播放设备"
    if not str(endpoint_id or "").strip():
        return "端点 ID 为空，无法切换"
    with _guard:
        try:
            ptr, vtbl = _open_policy()
        except RuntimeError as exc:
            return str(exc)
        try:
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                       ctypes.c_wchar_p, ctypes.c_int)
            fn = proto(vtbl[_SET_DEFAULT_INDEX])
            hr = fn(ptr, str(endpoint_id), _ROLE_CONSOLE)
            if hr != 0:
                return "系统拒绝了这次切换（hr=0x%08X）；设备可能被独占或已拔出" % (hr & 0xFFFFFFFF)
            return ""
        except Exception as exc:
            return "调用系统接口失败：%s" % exc


def list_render_endpoints(devices: list | None = None) -> list[dict[str, Any]]:
    """所有真实的播放端点（含 id / 名称 / 是否系统默认）。

    ``devices`` 允许调用方复用一次枚举结果：面板每秒轮询，如果
    available / endpoints / lock 各自再枚举一遍，就会每秒多次打开
    所有端点（包括蓝牙）读混音格式。传进来就只枚举一次。
    """
    out: list[dict[str, Any]] = []
    try:
        for item in (devices if devices is not None else audio_base.list_all_devices()):
            if getattr(item, "kind", "") != audio_base.KIND_SINK:
                continue
            if str(getattr(item, "backend", "")) in ("file", "null"):
                continue
            name = str(getattr(item, "name", "") or "").strip()
            dev_id = str(getattr(item, "id", "") or "").strip()
            if not name or not dev_id:
                continue
            out.append({"id": dev_id, "name": name,
                        "is_default": bool(getattr(item, "is_default", False))})
    except Exception:
        pass
    return out


def current_default_render(devices: list | None = None) -> dict[str, Any] | None:
    for item in list_render_endpoints(devices):
        if item.get("is_default"):
            return item
    return None


def find_render_endpoint(name: str, devices: list | None = None
                         ) -> tuple[dict[str, Any] | None, list[str]]:
    """按名字找端点：先精确，再忽略大小写，再「包含」。

    返回 (命中的端点 或 None, 全部候选名字)。候选名字用于"找不到"时的中文提示。
    """
    wanted = str(name or "").strip()
    endpoints = list_render_endpoints(devices)
    names = [str(e["name"]) for e in endpoints]
    if not wanted:
        return None, names
    for item in endpoints:
        if item["name"] == wanted:
            return item, names
    low = wanted.lower()
    for item in endpoints:
        if item["name"].lower() == low:
            return item, names
    for item in endpoints:
        if low in item["name"].lower() or item["name"].lower() in low:
            return item, names
    return None, names


def set_default_render(endpoint_id: str) -> str:
    """切换系统默认播放端点；成功返回空串，失败返回中文原因。"""
    return _set_default_endpoint(endpoint_id)


def describe() -> dict[str, Any]:
    """给日志用的一份快照：当前默认 + 全部候选。"""
    current = current_default_render() or {}
    return {"current": current, "endpoints": list_render_endpoints()}
