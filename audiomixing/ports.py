"""Startup port guard: never start on ports held by another app.

Why this exists (2026-11-01 user report): a second, separately built app derived
from Audio Mixing uses the same default ports. Both sockets set ``SO_REUSEADDR``,
so on Windows both binds succeed and the two programs fight over the same UDP
port; in the field both died. The engine itself cannot see this because
``SO_REUSEADDR`` deliberately allows sharing.

This module is called *before* the engine starts. It probes each configured port
with an **exclusive** bind and, when a port is taken, picks the next free one.
The caller updates the config and tells the user the new ports (the phone needs
to be set to the same discovery/audio ports).

Only the launcher uses this; unit tests pass ``save=False`` so the user's real
config is never touched.
"""

from __future__ import annotations

import socket
import sys
from typing import Any

TCP = "tcp"
UDP = "udp"
_MAX_SPAN = 200


def _tcp_free(port: int) -> bool:
    if not 1024 <= int(port) <= 65534:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False


def _udp_free(port: int) -> bool:
    if not 1024 <= int(port) <= 65534:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # On Windows SO_REUSEADDR lets two processes bind the same UDP port.
        # SO_EXCLUSIVEADDRUSE is the only way to ask "is somebody already there?".
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _next_tcp_pair(start: int, reserved: set[int]) -> int:
    for port in range(int(start) + 1, min(65535, int(start) + _MAX_SPAN)):
        if port in reserved or (port + 1) in reserved:
            continue
        if _tcp_free(port) and _tcp_free(port + 1):
            return port
    raise OSError(
        "端口 %d 附近 %d 个 TCP 端口都被占用，找不到一对可用端口。"
        "请关闭占用端口的程序，或在设置里手动换端口。" % (start, _MAX_SPAN)
    )


def _next_udp(start: int, reserved: set[int]) -> int:
    for port in range(int(start) + 1, min(65535, int(start) + _MAX_SPAN)):
        if port in reserved:
            continue
        if _udp_free(port):
            return port
    raise OSError(
        "端口 %d 附近 %d 个 UDP 端口都被占用，找不到可用端口。"
        "请关闭占用端口的程序，或在设置里手动换端口。" % (start, _MAX_SPAN)
    )


def ensure_ports(cfg: Any, *, save: bool = True) -> dict[str, int]:
    """Move conflicting ports to the next free ones; return ``{field: new}``.

    Checks the four things this program really listens on:
      * TCP web port (panel)
      * TCP web port + 1 (phone raw-audio long connection)
      * UDP audio port
      * UDP discovery port

    Ports stay as configured when free. Only a conflicting one moves; the new
    values are written back to ``cfg`` (and saved unless ``save=False``).
    """
    web = int(getattr(cfg, "web_port", 17800))
    audio = int(getattr(cfg, "audio_port", 47831))
    disc = int(getattr(cfg, "discovery_port", 17830))

    # A valid layout needs four distinct numbers and web/web+1 free.
    reserved = {audio, disc}
    if web in reserved or (web + 1) in reserved or not _tcp_free(web) or not _tcp_free(web + 1):
        new_web = _next_tcp_pair(web, reserved)
        changed_web = True
    else:
        new_web = web
        changed_web = False

    reserved = {new_web, new_web + 1, disc}
    if audio in reserved or not _udp_free(audio):
        new_audio = _next_udp(audio, reserved)
    else:
        new_audio = audio

    reserved = {new_web, new_web + 1, new_audio}
    if disc in reserved or not _udp_free(disc):
        new_disc = _next_udp(disc, reserved)
    else:
        new_disc = disc

    changed: dict[str, int] = {}
    if changed_web:
        cfg.web_port = new_web
        changed["web_port"] = new_web
    if new_audio != audio:
        cfg.audio_port = new_audio
        changed["audio_port"] = new_audio
    if new_disc != disc:
        cfg.discovery_port = new_disc
        changed["discovery_port"] = new_disc

    if changed and save:
        try:
            cfg.save()
        except Exception:
            # Caller still gets the in-memory values; a failed save only means the
            # next launch will probe again and make the same choice.
            pass
    return changed
