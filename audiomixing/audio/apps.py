"""正在出声的 App 列表 —— 按 App 采音（process loopback）的入口。

为什么要它：桌面回环只能拿到"所有 App 混在一起"的声音。用户想把 BILIBILI 的
声音单独送到一个耳机时，必须先知道"这台电脑上现在有哪些 App 在出声、它们的
PID 是多少"，再用 process loopback 只采那一个进程。

数据来源：WASAPI 音频会话（IAudioSessionManager2）→ PID → 进程名。
只列状态为 Active 的会话，跳过系统提示音（PID 0/4、IsSystemSoundsSession）
和被静音的会话 —— 静音的 App 不算"正在出声"。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

# 常见播放 App 的中文/产品名。查不到就用 exe 名，绝不编。
_FRIENDLY = {
    "bilibili": "哔哩哔哩",
    "哔哩哔哩": "哔哩哔哩",
    "chrome": "Chrome",
    "msedge": "Edge",
    "firefox": "Firefox",
    "qqmusic": "QQ音乐",
    "cloudmusic": "网易云音乐",
    "spotify": "Spotify",
    "potplayermini64": "PotPlayer",
    "potplayermini": "PotPlayer",
    "potplayer": "PotPlayer",
    "vlc": "VLC",
    "wmplayer": "Windows Media Player",
    "foobar2000": "foobar2000",
    "wechat": "微信",
    "weixin": "微信",
    "qq": "QQ",
    "telegram": "Telegram",
    "discord": "Discord",
    "obs64": "OBS",
}


def _friendly(stem: str) -> str:
    text = (stem or "").strip()
    if not text:
        return "未知应用"
    return _FRIENDLY.get(text.lower(), text)


def supported() -> tuple[bool, str]:
    if not sys.platform.startswith("win"):
        return False, "按 App 采音只在 Windows 上可用"
    try:
        from .backends import wasapi_com as wc

        return wc.process_loopback_available()
    except Exception as exc:
        return False, "按 App 采音不可用：%s" % exc


def list_audio_apps(exclude_pids: list[int] | None = None) -> dict[str, Any]:
    """列出本机正在出声的 App。

    返回 {"supported", "reason", "apps": [{"pid","name","exe","path"}], "error"?}
    supported=False 时 reason 是给用户看的中文原因；读会话失败时另给 error。
    """
    ok, reason = supported()
    result: dict[str, Any] = {"supported": bool(ok), "reason": reason, "apps": []}
    if not ok:
        return result
    try:
        from .backends import wasapi as wasapi_backend
        from .backends import wasapi_com as wc

        wasapi_backend._ensure_com()
        pids = wc.active_audio_pids()
    except Exception as exc:
        result["error"] = "读不到正在出声的 App：%s" % exc
        return result
    own = {os.getpid()} | set(exclude_pids or [])
    seen: set[int] = set()
    apps: list[dict[str, Any]] = []
    for pid in pids:
        value = int(pid)
        if value in (0, 4):
            # 系统音 / System Idle：没有对应的用户 App，也不该出现在列表里。
            continue
        if value in own or value in seen:
            continue
        seen.add(value)
        path = ""
        try:
            path = wc.process_image_name(value)
        except Exception:
            path = ""
        exe = os.path.basename(path) if path else ""
        stem = os.path.splitext(exe)[0] if exe else ""
        apps.append({
            "pid": value,
            "name": _friendly(stem) if stem else ("进程 %d" % value),
            "exe": exe,
            "path": path,
        })
    apps.sort(key=lambda item: (item["name"].lower(), item["pid"]))
    result["apps"] = apps
    result["scanned_at"] = round(time.time(), 1)
    return result


def unmute_all_sessions() -> dict[str, Any]:
    """一键取消所有 App 音频会话的静音（2.40 误静音的恢复入口）。

    ★ 2026-11-16：Windows 会把每个 App 的静音状态记进音频策略，进程退出也不会
      自动恢复。被 2.40 静音过的 App 需要显式恢复，返回
      {"ok", "changed", "total", "reason"}。
    """
    if not sys.platform.startswith("win"):
        return {"ok": False, "changed": 0, "total": 0, "reason": "恢复静音只在 Windows 上可用"}
    try:
        from .backends import wasapi as wasapi_backend
        from .backends import wasapi_com as wc

        wasapi_backend._ensure_com()
        stats = wc.set_all_sessions_mute(False)
    except Exception as exc:
        return {"ok": False, "changed": 0, "total": 0, "reason": "恢复失败：%s" % exc}
    return {"ok": True, "changed": int(stats.get("changed") or 0),
            "total": int(stats.get("total") or 0), "reason": ""}
