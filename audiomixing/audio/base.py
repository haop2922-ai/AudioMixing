"""Audio backend interface.

A backend is a small module that knows how to talk to one audio API family
(WASAPI, PipeWire, CoreAudio, plain files). It exposes:

    available() -> bool
    list_devices() -> list[AudioDevice]
    open_source(spec) -> AudioSource
    open_sink(spec) -> AudioSink

Everything outside of that exchanges **interleaved float32 @ 48 kHz stereo**
(``array('f')``), so the engine never has to care about the platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .format import CHANNELS, SAMPLE_RATE

KIND_SOURCE = "source"  # something we can capture (render-endpoint loopback, mic, file, tone)
KIND_SINK = "sink"  # something we can play to


@dataclass
class AudioDevice:
    id: str
    name: str
    kind: str  # KIND_SOURCE | KIND_SINK
    backend: str
    kind_detail: str = ""  # "loopback" | "capture" | "virtual" | "file" ...
    channels: int = CHANNELS
    sample_rate: int = SAMPLE_RATE
    is_default: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    # HWHHYN 用 DEEPSEEK 制作
    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "backend": self.backend,
            "kind_detail": self.kind_detail,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "is_default": self.is_default,
            "extra": self.extra,
        }


class AudioSource(Protocol):
    name: str
    sample_rate: int
    channels: int

    def start(self) -> None: ...
    def read(self) -> Any:  # -> array('f') interleaved float32, may be empty
        ...
    def stop(self) -> None: ...
    def describe(self) -> dict[str, Any]: ...


class AudioSink(Protocol):
    name: str
    sample_rate: int
    channels: int

    def start(self) -> None: ...
    def write(self, samples) -> int: ...
    def buffered_frames(self) -> int: ...
    def target_frames(self) -> int: ...
    def stop(self) -> None: ...
    def describe(self) -> dict[str, Any]: ...


class BackendBase:
    name = "base"
    platform = "any"

    @classmethod
    def available(cls) -> bool:
        return False

    @classmethod
    def list_devices(cls) -> list[AudioDevice]:
        return []


# --------------------------------------------------------------------------
# Backend registry
# --------------------------------------------------------------------------

_BACKENDS: dict[str, type] = {}
_AUTOLOADED = False


def register(backend_cls: type) -> type:
    _BACKENDS[backend_cls.name] = backend_cls
    return backend_cls


def _autoload() -> None:
    """把内置后端模块导入一遍（每个模块用 @register 自己登记）。

    ★ 这里【不能】写成 "if _BACKENDS: return" ★
    任何一处先 import 了单个后端（engine 里就有 `from .audio.backends import netmic`），
    `_BACKENDS` 就不再为空 —— 旧写法会直接返回，file / null / wasapi 永远不加载，
    于是后面要开 "null"（模拟/测试音）或 "wasapi"（真实声卡）时直接
    `unknown audio backend 'null' (have: netmic)`，表现是"什么都打不开、一点声音都没有"。
    2026-10-14 在测试里稳定复现（先跑过 netmic 的用例，后面 simulate 节点就开不了音源）。

    另外：光"导入模块"还不够 —— 模块可能已经在 sys.modules 里、但登记丢了
    （注册表被清空/重置过）。那种情况下 import 是空操作，注册表会一直缺后端；
    所以补一步：从已经缓存的模块里把后端类找出来重新登记（见下面的循环）。
    """
    global _AUTOLOADED
    if _AUTOLOADED:
        return
    _AUTOLOADED = True          # 先置位：下面的导入自己可能又调回来
    import importlib
    import sys as _sys

    for name in ("file", "netmic", "null", "wasapi", "pipewire"):
        if name in _BACKENDS:
            continue            # 已经登记过就别动它
        full = "%s.backends.%s" % (__package__, name)
        try:
            module = _sys.modules.get(full)
            if module is None:
                importlib.import_module(full)
                continue
        except Exception:  # pragma: no cover - 非 Windows 上没有 wasapi/pipewire
            continue
        # 模块已经在缓存里、但没登记（注册表被清空/重置过）：把它的后端类直接登记回来。
        # ★ 这里【不要】用 importlib.reload ★ —— reload 会造出一个新的类对象和新的模块级
        # 字典（比如 netmic 的 MICS），而别处手里还攥着旧的那一份引用，结果就是
        # "设备列表永远是空的"这类鬼问题（2026-10-14 实测踩到）。
        for value in list(vars(module).values()):
            try:
                if isinstance(value, type) and getattr(value, "name", "") == name:
                    register(value)
                    break
            except Exception:
                continue


def backends() -> dict[str, type]:
    _autoload()
    return dict(_BACKENDS)


def get_backend(name: str) -> type:
    bs = backends()
    if name not in bs:
        raise KeyError("unknown audio backend %r (have: %s)" % (name, ", ".join(sorted(bs))))
    return bs[name]


def available_backends() -> dict[str, bool]:
    out = {}
    for name, cls in backends().items():
        try:
            out[name] = bool(cls.available())
        except Exception:
            out[name] = False
    return out


def default_backend_name() -> str:
    for name, ok in available_backends().items():
        if ok and name != "null":
            return name
    return "null"


def list_all_devices() -> list[AudioDevice]:
    out: list[AudioDevice] = []
    for _name, cls in backends().items():
        try:
            if not cls.available():
                continue
        except Exception:
            continue
        try:
            out.extend(cls.list_devices())
        except Exception:
            continue
    return out


def find_device(selector: str) -> AudioDevice | None:
    """Resolve a device by exact id, name, or case-insensitive substring.

    `selector` may be prefixed with a backend name, e.g. ``wasapi:耳机``.
    """
    backend_filter = None
    if ":" in selector:
        head, tail = selector.split(":", 1)
        if head in backends():
            backend_filter, selector = head, tail
    devs = list_all_devices()
    if backend_filter:
        devs = [d for d in devs if d.backend == backend_filter]
    low = selector.lower()
    # Exact *name* first, then exact id. The Windows backend numbers its devices, so a
    # phone called "2" used to match the endpoint whose id happens to be "2" - and the
    # audio then came from (and was blocked as) the wrong device entirely.
    for d in devs:
        if d.name.lower() == low:
            return d
    for d in devs:
        if d.id == selector:
            return d
    matches = [d for d in devs if low in d.name.lower() or low in d.id.lower()]
    if matches:
        matches.sort(key=lambda d: (not d.is_default, len(d.name)))
        return matches[0]
    return None


def open_source(spec: dict[str, Any]):
    """spec: {"backend": "wasapi", "device": "...", "kind": "loopback", ...}"""
    backend = spec.get("backend") or default_backend_name()
    return get_backend(backend).open_source(spec)


def open_sink(spec: dict[str, Any]):
    backend = spec.get("backend") or default_backend_name()
    return get_backend(backend).open_sink(spec)


def spec_for_selector(base: dict[str, Any], selector: str) -> dict[str, Any]:
    """Resolve a user selector into a full spec, switching backend when needed.

    A selector may name a device that lives in another backend - ``tone:1000``,
    ``silence``, ``file:out.wav``, ``null`` - and a default WASAPI spec cannot open
    those. Every path that turns a selector into a spec goes through here, otherwise
    those names resolve to a real device (or fail) depending on who is asking.
    """
    spec = dict(base)
    selector = (selector or "").strip()
    if not selector:
        return spec
    spec["device"] = selector
    try:
        device = find_device(selector)
    except Exception:
        device = None
    if device is not None and device.backend and device.backend != spec.get("backend"):
        spec["backend"] = device.backend
        spec.pop("loopback", None)
    return spec


def write_paced(sink, samples, *, frames_per_block: int = 480, stop=None, on_block=None) -> int:
    """Play `samples` at real time; returns how many frames were handed over.

    A sink accepts a block as soon as it has taken ownership of it, buffering
    whatever the device does not take right now. That makes the return value useless
    for pacing: a caller that simply loops on it dumps seconds of audio into the sink
    in one go, and the backlog limit then throws most of it away - which is exactly
    how the panel's "can I hear it?" test tone ended up almost silent. Anything that
    plays a fixed buffer has to pace itself, like the engine's render loop does.

    ``on_block`` sees each block as it is handed over, which is how the test tone also
    reaches the phone listener tap.
    """
    import time

    total = len(samples) // CHANNELS
    step = max(1, frames_per_block)
    written = 0
    deadline = time.monotonic()
    while written < total:
        if stop is not None and stop.is_set():
            break
        take = min(step, total - written)
        chunk = samples[written * CHANNELS : (written + take) * CHANNELS]
        if on_block is not None:
            try:
                on_block(chunk)
            except Exception:
                pass
        got = sink.write(chunk)
        written += got if got > 0 else take
        deadline += take / float(SAMPLE_RATE)
        sleep_for = deadline - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        elif sleep_for < -0.25:
            deadline = time.monotonic()
    return written
