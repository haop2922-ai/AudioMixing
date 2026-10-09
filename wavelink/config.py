"""Persistent configuration (~/.wavelink/config.json)."""

from __future__ import annotations

import json
import os
import platform
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .net.discovery import default_node_id

CONFIG_VERSION = 1


# HWHHYN 用 DEEPSEEK 制作
def config_dir() -> Path:
    override = os.environ.get("WAVELINK_HOME")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "WaveLink"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "WaveLink"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "wavelink"


def config_path() -> Path:
    return config_dir() / "config.json"


def default_node_name() -> str:
    host = socket.gethostname() or "wavelink"
    return host


@dataclass
class Config:
    node_id: str = field(default_factory=default_node_id)
    name: str = field(default_factory=default_node_name)
    # Ports
    discovery_port: int = 47830
    audio_port: int = 47831
    web_port: int = 17800
    # Routing / privacy
    routes: list[dict[str, Any]] = field(default_factory=list)
    accept_unknown: bool = False
    manual_peers: list[dict[str, Any]] = field(default_factory=list)
    # Local device choices
    forward_to: str = ""  # "host:port" for forward-only mode
    source_device: str = "@default"
    sink_device: str = "@default"
    codec: str = "pcm_s16"
    jitter_ms: float = 55.0
    autostart: bool = True
    # LAN "radio channel". `broadcast` sends this machine's sound to every node on the
    # network; `listen` plays whatever arrives on the channel. A listener sends
    # nothing, which is what makes the channel loop-free by construction - the same
    # reason a radio receiver never feeds back into the transmitter.
    broadcast: bool = False
    listen: bool = False
    channel_gain: float = 1.0
    # What the channel broadcasts. Empty means "this machine's own source_device".
    # Several entries are mixed into one stream, which is how several phones - each on
    # its own Bluetooth adapter - or a phone plus this machine's sound travel as one.
    channel_sources: list[str] = field(default_factory=list)
    # Play the channel's sources on *this* machine's output as well. This is what makes
    # "phone over Bluetooth -> this PC -> my headphones" work without any second node.
    local: bool = False
    # Phones to connect by themselves on startup, matched by name. Empty means "connect
    # nothing": opening an A2DP sink resets that phone's link, so this stays opt-in.
    autoconnect: list[str] = field(default_factory=list)
    # A phone that starts sending gets picked up and played at once: this is a LAN, the
    # operator knows what is on it, and requiring a click first was the step that kept
    # swallowing the audio.
    auto_play: bool = True
    # Which window to open when the app starts: "none", "simple" or "full".
    # Set from the detailed panel; it is a rarely used preference, so it is hidden in
    # the simple view and never appears in the desktop window.
    open_panel_on_start: str = "none"
    # Phones that turned their own broadcast off. They stay in channel_sources (the local
    # mix still uses them) but are left out of the LAN broadcast - and auto-play must not
    # quietly add them back, which is exactly why the phone's button appeared stuck on.
    broadcast_excluded: list[str] = field(default_factory=list)
    # Cushion for phone audio over Wi-Fi, in milliseconds. Deliberately separate from
    # jitter_ms above: that one is the network stream buffer between computers, this one
    # is how long a phone's audio is held before it is played. Android coalesces its
    # timers, so a phone sends in bursts and needs a bigger cushion than a PC does.
    netmic_jitter_ms: int = 150
    version: int = CONFIG_VERSION

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None, create: bool = True) -> "Config":
        """Read the config, creating it with defaults when missing.

        ``create=False`` is for read-only tools (`netcheck`): loading must not have
        the side effect of writing a file into the user's config directory, which
        once made a pure diagnostic run leave test ports behind.
        """
        p = path or config_path()
        if not p.is_file():
            cfg = cls()
            if create:
                cfg.save(p)
            return cfg
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = cls()
            if create:
                cfg.save(p)
            return cfg
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if not cfg.node_id:
            cfg.node_id = default_node_id()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        p = path or config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": CONFIG_VERSION,
            "node_id": self.node_id,
            "name": self.name,
            "discovery_port": self.discovery_port,
            "audio_port": self.audio_port,
            "web_port": self.web_port,
            "routes": self.routes,
            "accept_unknown": self.accept_unknown,
            "manual_peers": self.manual_peers,
            "forward_to": self.forward_to,
            "source_device": self.source_device,
            "sink_device": self.sink_device,
            "codec": self.codec,
            "jitter_ms": self.jitter_ms,
            "autostart": self.autostart,
            "broadcast": self.broadcast,
            "listen": self.listen,
            "channel_gain": self.channel_gain,
            "channel_sources": self.channel_sources,
            "local": self.local,
            "autoconnect": self.autoconnect,
            "auto_play": self.auto_play,
            # 启动时顺手打开面板窗口。在详细版面板里可以勾选。
            "open_panel_on_start": self.open_panel_on_start,
            "broadcast_excluded": self.broadcast_excluded,
            "netmic_jitter_ms": self.netmic_jitter_ms,
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return p

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "discovery_port": self.discovery_port,
            "audio_port": self.audio_port,
            "web_port": self.web_port,
            "routes": self.routes,
            "accept_unknown": self.accept_unknown,
            "manual_peers": self.manual_peers,
            "forward_to": self.forward_to,
            "source_device": self.source_device,
            "sink_device": self.sink_device,
            "codec": self.codec,
            "jitter_ms": self.jitter_ms,
            "autostart": self.autostart,
            "broadcast": self.broadcast,
            "listen": self.listen,
            "channel_gain": self.channel_gain,
            "channel_sources": self.channel_sources,
            "local": self.local,
            "autoconnect": self.autoconnect,
            "auto_play": self.auto_play,
            "open_panel_on_start": self.open_panel_on_start,
            "netmic_jitter_ms": self.netmic_jitter_ms,
            "path": str(config_path()),
        }
