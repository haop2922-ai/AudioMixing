"""Port-guard regression: conflicting ports must move, free ports must stay."""

from __future__ import annotations

import socket
import unittest

from audiomixing.config import Config
from audiomixing.ports import ensure_ports


def _tcp_bindable(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False


def _free_tcp() -> int:
    """返回一个【它和 +1 都能绑】的端口。

    ensure_ports 要求 web 与 web+1 同时空闲；只用 bind(0) 拿到的临时端口
    偶尔会撞上已经被别的进程占着的 +1，测试就会误判成"空闲端口被挪动"。
    """
    for _ in range(64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 0))
            port = int(s.getsockname()[1])
        if port < 65534 and _tcp_bindable(port) and _tcp_bindable(port + 1):
            return port
    raise RuntimeError("找不到一对连续空闲的 TCP 端口")


def _free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


class TestPortGuard(unittest.TestCase):
    def _cfg(self) -> Config:
        cfg = Config()
        cfg.web_port = _free_tcp()
        cfg.audio_port = _free_udp()
        cfg.discovery_port = _free_udp()
        return cfg

    def test_free_ports_are_kept(self) -> None:
        cfg = self._cfg()
        before = (cfg.web_port, cfg.audio_port, cfg.discovery_port)
        moved = ensure_ports(cfg, save=False)
        self.assertEqual(moved, {}, "空闲端口不该被改动")
        self.assertEqual((cfg.web_port, cfg.audio_port, cfg.discovery_port), before)

    def test_occupied_web_port_moves_to_a_free_pair(self) -> None:
        cfg = self._cfg()
        # 占住网页端口（同时把 +1 也占住，模拟"另一个同类软件"）
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            base = _free_tcp()
            first.bind(("0.0.0.0", base))
            second.bind(("0.0.0.0", base + 1))
            cfg.web_port = base
            moved = ensure_ports(cfg, save=False)
            self.assertIn("web_port", moved, "被占的网页端口必须换掉")
            self.assertNotEqual(cfg.web_port, base)
            # 新的一对必须真的能绑
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("0.0.0.0", cfg.web_port + 1))
            finally:
                probe.close()
        finally:
            first.close()
            second.close()

    def test_occupied_audio_port_moves_and_keeps_others(self) -> None:
        cfg = self._cfg()
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            holder.bind(("0.0.0.0", cfg.audio_port))
            moved = ensure_ports(cfg, save=False)
            self.assertIn("audio_port", moved)
            self.assertNotEqual(cfg.audio_port, moved.get("web_port", -1))
            self.assertNotEqual(cfg.audio_port, cfg.discovery_port)
        finally:
            holder.close()
