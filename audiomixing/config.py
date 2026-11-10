"""Persistent configuration (~/.audiomixing/config.json; on Windows: %APPDATA%\\AudioMixing)."""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .net.discovery import default_node_id
from .proto import LEGACY_PORT_DISCOVERY, PORT_DISCOVERY

CONFIG_VERSION = 1


# HWHHYN 用 DEEPSEEK 制作
def _migrate_old_dir(new_dir: "Path") -> None:
    """把旧的 %APPDATA%\\WaveLink 整棵复制到新目录（只做一次）。

    用户的设备选择、路由表、蓝牙配对都在里面 —— 直接换路径会让它们凭空消失。
    复制而不是移动：万一下次还要跑老版本，老目录仍然完整。
    """
    import shutil

    try:
        old = new_dir.parent / "WaveLink"
        if new_dir.exists() or not old.is_dir():
            return
        shutil.copytree(old, new_dir, dirs_exist_ok=True)
    except Exception:
        # 迁移失败不影响启动：大不了回到默认设置
        pass


def config_dir() -> Path:
    # 新名优先、旧名兜底：v1.22 及更早的启动器/脚本导出的还是 WAVELINK_HOME，
    # 换了名字之后不能让它们悄悄指丢配置目录。
    override = os.environ.get("AUDIOMIXING_HOME") or os.environ.get("WAVELINK_HOME")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        target = Path(base) / "AudioMixing"
        _migrate_old_dir(target)
        return target
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AudioMixing"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "audiomixing"


def config_path() -> Path:
    return config_dir() / "config.json"


def default_node_name() -> str:
    host = socket.gethostname() or "audiomixing"
    return host


# ★ 2026-11-03：这些 id 只会由测试/联调脚本生成，真实安装的 App 用的是 uuid4。
#   见到它们说明配置曾被测试污染，需要把身份修回真实值；但**不据此删除用户路由**。
_SYNTHETIC_NODE_IDS = {
    "self-node", "node-self", "peer-1", "peer-2", "test-node", "test-node-a",
    "test-node-b", "selftest-node", "selftest-node-a", "selftest-node-b",
}
_SYNTHETIC_ID_PREFIXES = ("self-node-", "node-self-", "selftest-", "test-node-",
                          "peer-", "am-test-")


def _is_synthetic_node_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text in _SYNTHETIC_NODE_IDS or text.startswith(_SYNTHETIC_ID_PREFIXES)


def _backup_before_migration(path: Path) -> Path:
    """把即将被迁移的 config.json 原样备份成 `config.json.bak-<时间戳>`。

    ★ 2026-11-03（Q1）：迁移会改写用户身份与 smart_link，虽然不是"删数据"，
      但用户可能正靠旧身份连着别的设备。改前留一份原件，出问题能人工回退。
      同一秒内跑两次时加 -2/-3 后缀，绝不覆盖已有备份。
    """
    import shutil
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name("%s.bak-%s" % (path.name, stamp))
    serial = 2
    while target.exists():
        target = path.with_name("%s.bak-%s-%d" % (path.name, stamp, serial))
        serial += 1
    shutil.copy2(path, target)
    return target


@dataclass
class Config:
    node_id: str = field(default_factory=default_node_id)
    # ★ 2026-11-04（B 第一阶段身份层）：
    #   device_id —— 设备自生成、持久化、可备份的稳定主键。同一台设备的多条链路
    #     （WiFi / 蓝牙 / 其它）靠它合并成一个节点；旧配置没有这个键时在 load()
    #     里迁移成 node_id（这样老路由一条都不用改，也不会突然变成"别的设备"）。
    #   instance_id —— 每次安装生成；同一 device_id + 不同 instance_id = 两台真机
    #     共用了同一份配置（换机搬数据），必须分开显示，绝不自动合并。
    #   alias_ids —— 旧 node_id / manual:IP 一律降级成别名，只用于解析路由。
    device_id: str = ""
    instance_id: str = ""
    alias_ids: list[str] = field(default_factory=list)
    identity_migrated: bool = False
    # 人工确认过的"这两条其实是同一台设备"映射（绝不自动写入）。
    identity_merges: list[dict[str, Any]] = field(default_factory=list)
    # ★ v2.27（用户第 6 条）：关系图里"蓝牙卡片 ↔ 某台局域网设备"的人工合并。
    #   只影响面板显示，不动注册表身份；面板上可以随时「拆开」。
    relation_merges: list[dict[str, Any]] = field(default_factory=list)
    name: str = field(default_factory=default_node_name)
    # Ports
    # ★ 2026-11-01 用户：默认发现端口改为 17830（两端必须一致）。老配置里的 47830
    #   在 load() 里自动迁移；用户自己填的其它值不动。
    discovery_port: int = PORT_DISCOVERY
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
    # ★ v2.27（用户新 UI 需求）：Windows 开机自启动（写 HKCU\...\Run）。
    #   与上面的 autostart 不是一回事：那个是"启动后自动连蓝牙"。
    launch_at_login: bool = False
    # LAN "radio channel". `broadcast` sends this machine's sound to every node on the
    # network; `listen` plays whatever arrives on the channel. A listener sends
    # nothing, which is what makes the channel loop-free by construction - the same
    # reason a radio receiver never feeds back into the transmitter.
    broadcast: bool = False
    listen: bool = False
    channel_gain: float = 1.0
    # ★ 2026-11-04（Q2）：手机音量二次叠加开关。
    #   "auto"  —— 自动判定（比较上报 vol 与这条流实际收到的峰值）；
    #   "off"   —— 强制关：PC 只乘路由增益，不再乘手机 vol；
    #   "on"    —— 强制开：沿用旧行为（路由增益 × 手机 vol）。
    #   默认 auto；判定样本不足时按"叠加"处理（与 2.10 之前的行为一致）。
    phone_volume_mode: str = "auto"
    # ★ 2026-11-08（用户方案）：静音档乘数策略。
    #   internal_only —— 默认：静音档里 STREAM_MUSIC 被钉在 0，这个 0 没有信息量，乘 1.0，
    #                    只保留 PCM 里的 App 内部增益（"调了没反应"的反面证据见 docs）；
    #   last_nonzero  —— 沿用进入静音前最后一次非零系统音量（外放档压到 25% 的信息不丢）。
    #   外放档永远乘当前系统媒体音量，与本项无关。
    phone_silent_vol_policy: str = "internal_only"
    # What the channel broadcasts. Empty means "this machine's own source_device".
    # Several entries are mixed into one stream, which is how several phones - each on
    # its own Bluetooth adapter - or a phone plus this machine's sound travel as one.
    channel_sources: list[str] = field(default_factory=list)
    # ① 自动收听（与手机端同名同义）：
    #   smart_link —— ★ 2026-10-18 用户第 6 条：全局总开关换成**每台设备一个「智连」**。
    #     名单里有的那台设备，"它送声音过来"时才自动开始收听它（= 接受它的「送」）；
    #     同时也表示"允许它听我"（它请求听本机时自动把声音送给它）。
    #     **默认空 = 一台都不自动**（用户原话「默认关闭」）。
    #     用户明确否掉的旧口径：那个"关过一次就永不再自动"的黑名单（listen_optout）——
    #     它正是"电脑送音、手机不自动收"那条真机反馈的根因，现在删掉了。
    smart_link: list[str] = field(default_factory=list)
    # 本机多设备输出：勾选的输出设备（每项含 device/gain/delay_ms）
    multi_out_devices: list[dict[str, Any]] = field(default_factory=list)
    # Play the channel's sources on *this* machine's output as well. This is what makes
    # "phone over Bluetooth -> this PC -> my headphones" work without any second node.
    local: bool = False
    # Phones to connect by themselves on startup, matched by name. Empty means "connect
    # nothing": opening an A2DP sink resets that phone's link, so this stays opt-in.
    autoconnect: list[str] = field(default_factory=list)
    # ★ 2026-10-18 用户（图2）：「「正在发送的手机自动播放」就是蓝牙的「智连」」——
    #   原来这里有一个全局开关 `auto_play`（面板上那个勾：手机一开始送声音就自动收编成音源播放）。
    #   它和"每台设备的「智连」"说的是同一件事，所以整块删掉：要不要自动收下这台设备的声音，
    #   只看那台设备的卡片开没开「智连」（局域网看 smart_link，蓝牙看 autoconnect）。
    #   默认 = 一台都不自动（用户第 6 条的原话「默认关闭」）。
    #   老配置里那个 "auto_play" 键不再读（from_dict 会当未知键忽略），不会报错。
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
        # ★ 2026-11-04（B 第一阶段）身份迁移：
        #   ① 先备份（迁移失败/不想迁移就保留原配置，绝不删用户路由）；
        #   ② device_id 缺省 → 取 node_id（老路由、老设备立刻继续可用）；
        #   ③ instance_id 缺省 → 本机生成一个（用于识破"换机搬配置"）；
        #   ④ 每条路由的老 node_id / manual:IP 原样保留，只**追加**到 aliasIds；
        #      路由条数一条都不减（用户明确要求：不许删用户路由）。
        if not cfg.device_id:
            cfg.device_id = cfg.node_id
        if not cfg.instance_id:
            cfg.instance_id = str(uuid.uuid4())
        if not cfg.identity_migrated:
            # 合成测试身份会走下面那段"换掉假 node_id"的迁移；它已经备份一次。
            # 这里再备份会让用户看到两份 .bak，既有测试也按"恰好一份"卡着。
            if create and not _is_synthetic_node_id(cfg.node_id):
                try:
                    _backup_before_migration(p)
                except OSError as exc:
                    print("AudioMixing: 身份迁移前备份失败，本次不迁移（原配置保留）：%s" % exc,
                          file=sys.stderr)
                    return cfg
            for rule in cfg.routes:
                if not isinstance(rule, dict):
                    continue
                aliases = rule.get("aliasIds")
                alias_list = [str(x) for x in aliases] if isinstance(aliases, list) else []
                for key in ("from", "to"):
                    values = rule.get(key)
                    if not isinstance(values, list):
                        continue
                    for value in values:
                        token = str(value or "").strip()
                        if token and token.lower() != cfg.node_id.lower() and token not in alias_list:
                            alias_list.append(token)
                if alias_list:
                    rule["aliasIds"] = alias_list
            cfg.identity_migrated = True
            if create:
                cfg.save(p)
        # ★ 2026-11-03 用户（图1）：设备图里永远是 `xxxxxxxx-xxxx-…` 这类历史节点。
        #   根因之一是测试/历史联调把**假 node_id**（`self-node`、`node-self`、`peer-1`…）
        #   写进了用户配置；用它建出来的路由，端点对不上任何真实设备，于是永远"不在线"。
        #   这里只做一件确定安全的事：把假身份换成真 UUID，并把路由里指向假身份的 token
        #   原地改写成新身份（路由语义不变）。**不删任何路由** —— 真设备只是关机了的情况
        #   由引擎按"有没有见过"判断（见 DiscoveryRegistry.ever_seen）。
        if _is_synthetic_node_id(cfg.node_id):
            old_id = str(cfg.node_id)
            backup: Path | None = None
            if create:
                # ★ 2026-11-03（Q1）：动用户配置前先留一份原件。备份失败就不迁移 ——
                #   宁可这一轮还是假身份，也不能在无法回退的情况下改写用户配置。
                try:
                    backup = _backup_before_migration(p)
                except OSError as exc:
                    print("AudioMixing: 配置备份失败，本次不改身份（原配置保留）：%s" % exc,
                          file=sys.stderr)
                    return cfg
            cfg.node_id = default_node_id()
            # 身份迁移若刚把 device_id 取成旧（假）node_id，这里跟着换成新身份，
            # 否则信标里的 device_id 会和本机 node_id 分家。
            if str(cfg.device_id or "").strip().lower() == old_id.lower():
                cfg.device_id = cfg.node_id
            for rule in cfg.routes:
                if not isinstance(rule, dict):
                    continue
                for key in ("from", "to"):
                    values = rule.get(key)
                    if isinstance(values, list):
                        rule[key] = [cfg.node_id if str(v).strip().lower() == old_id.lower() else v
                                     for v in values]
                if str(rule.get("origin") or "").strip().lower() == old_id.lower():
                    rule["origin"] = cfg.node_id
            cfg.smart_link = [x for x in cfg.smart_link if not _is_synthetic_node_id(str(x))]
            if create:
                cfg.save(p)
                if backup is not None:
                    print("AudioMixing: 已把测试身份 %r 迁成 %r，原配置备份在 %s"
                          % (old_id, cfg.node_id, backup), file=sys.stderr)
        # ★ 2026-11-02 用户：电脑在别人设备上显示成「本机」。
        #   老版本把默认名写成「本机」，这个值跟着配置一路存到现在；
        #   现在新默认是主机名，老默认值一律迁移。用户自己起的名字不动。
        if str(getattr(cfg, "name", "") or "").strip() in ("", "本机", "我的电脑", "电脑"):
            cfg.name = default_node_name()
            if create:
                cfg.save(p)
        # ★ 2026-11-01 用户：两端默认发现端口改为 17830。老配置里存着旧默认 47830 ——
        #   那是"从没改过"的值，跟着新默认一起搬（手机端同样迁移），否则新默认永远落不了地。
        #   用户自己填过的其它端口（例如截图里的 17930）一律不动。
        #   create=False（netcheck 这种只读工具）只改内存，不往用户配置目录写文件。
        if int(getattr(cfg, "discovery_port", 0) or 0) == LEGACY_PORT_DISCOVERY:
            cfg.discovery_port = PORT_DISCOVERY
            if create:
                cfg.save(p)
        return cfg

    def save(self, path: Path | None = None, strict: bool = False) -> Path:
        p = path or config_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            # 建不了目录也要能跑：安全软件（火绒 HIPS 之类）会拦"新程序往
            # %APPDATA% 建目录"，以前这里直接抛异常 → 启动即崩。
            # 但「保存端口」这类用户动作必须知道失败，所以给了 strict。
            if strict:
                raise OSError("建不了配置目录 %s: %s" % (p.parent, exc)) from exc
            return p
        if not self.device_id:
            self.device_id = self.node_id
        if not self.instance_id:
            self.instance_id = str(uuid.uuid4())
        data = {
            "version": CONFIG_VERSION,
            "node_id": self.node_id,
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "alias_ids": self.alias_ids,
            "identity_migrated": self.identity_migrated,
            "identity_merges": self.identity_merges,
            "relation_merges": self.relation_merges,
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
            "launch_at_login": self.launch_at_login,
            "broadcast": self.broadcast,
            "listen": self.listen,
            "channel_gain": self.channel_gain,
            "phone_volume_mode": self.phone_volume_mode,
            "phone_silent_vol_policy": self.phone_silent_vol_policy,
            "channel_sources": self.channel_sources,
            "smart_link": self.smart_link,
            "local": self.local,
            "autoconnect": self.autoconnect,
            # 启动时顺手打开面板窗口。在详细版面板里可以勾选。
            "open_panel_on_start": self.open_panel_on_start,
            "broadcast_excluded": self.broadcast_excluded,
            "netmic_jitter_ms": self.netmic_jitter_ms,
        }
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            # 写不进去就跳过：启动时不能因为"存不了设置"而崩掉。
            # 用户主动保存（strict）则必须如实报错，不能假装成功。
            if strict:
                raise OSError("写不了配置文件 %s: %s" % (p, exc)) from exc
            return p
        return p

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "device_id": self.device_id,
            "instance_id": self.instance_id,
            "alias_ids": self.alias_ids,
            "identity_migrated": self.identity_migrated,
            "identity_merges": self.identity_merges,
            "relation_merges": self.relation_merges,
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
            "launch_at_login": self.launch_at_login,
            "broadcast": self.broadcast,
            "listen": self.listen,
            "channel_gain": self.channel_gain,
            "phone_volume_mode": self.phone_volume_mode,
            "phone_silent_vol_policy": self.phone_silent_vol_policy,
            "channel_sources": self.channel_sources,
            "local": self.local,
            "autoconnect": self.autoconnect,
            "open_panel_on_start": self.open_panel_on_start,
            "netmic_jitter_ms": self.netmic_jitter_ms,
            "path": str(config_path()),
        }
