"""Windows Audio Session API backend (ctypes, no third-party dependencies).

Two things matter here and both are easy to get wrong:

* **Loopback capture.** To record what the machine is *playing* you must ask for a
  capture client on a *render* endpoint and pass ``AUDCLNT_STREAMFLAGS_LOOPBACK``.
  No virtual cable and no elevation are required. This is exactly what the office
  scenario needs: every notification, call and video on the laptop becomes a
  Audio Mixing source.
* **Shared-mode formats.** The device's mix format is whatever the user configured
  in Windows (often 48 kHz float32, sometimes 44.1 kHz or 16-bit). We always
  convert to/from interleaved float32 @ 48 kHz here so the rest of Audio Mixing never
  sees a platform detail.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from array import array
from ctypes import POINTER, byref, c_void_p
from ctypes.wintypes import DWORD
from typing import Any

from ..base import AudioDevice, BackendBase, KIND_SINK, KIND_SOURCE, register
from ..format import CHANNELS, SAMPLE_RATE, Resampler, as_float_array, channel_map, to_float32
from . import wasapi_com as wc

# --- constants -------------------------------------------------------------
eRender = 0
eCapture = 1
eAll = 2
DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_DISABLED = 0x2
DEVICE_STATE_NOTPRESENT = 0x4
DEVICE_STATE_UNPLUGGED = 0x8

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_SHAREMODE_EXCLUSIVE = 1

AUDCLNT_STREAMFLAGS_CROSSPROCESS = 0x00010000
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_NOPERSIST = 0x00080000

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4

REFTIMES_PER_SEC = 10_000_000
REFTIMES_PER_MS = 10_000

_com_ready = False
_com_lock = threading.Lock()


def _ensure_com() -> None:
    """Initialise COM once per process; called from worker threads too."""
    global _com_ready
    with _com_lock:
        hr = wc.ole32.CoInitializeEx(None, wc.COINIT_MULTITHREADED)
        if wc.failed(hr) and hr != -2147417850:  # RPC_E_CHANGED_MODE is acceptable
            raise wc.win_error("CoInitializeEx", hr)
        _com_ready = True


def _decode_24_in_32(raw: bytes, frames: int, channels: int) -> array:
    out = array("f", bytes(4 * frames * channels))
    i = 0
    for f in range(frames):
        base = f * channels * 4
        for c in range(channels):
            o = base + c * 4
            v = raw[o] | (raw[o + 1] << 8) | (raw[o + 2] << 16) | (raw[o + 3] << 24)
            if v & 0x80000000:
                v -= 0x100000000
            out[i] = v / 2147483648.0
            i += 1
    return out


def _decode_device_bytes(raw: bytes, frames: int, info: dict) -> array:
    fmt = info.get("sample_format") or "s16"
    ch = info.get("channels", CHANNELS)
    bits = info.get("bits", 16)
    if fmt == "f32":
        return to_float32(raw[: frames * ch * 4], "f32")
    if fmt == "s16":
        return to_float32(raw[: frames * ch * 2], "s16")
    if fmt == "s32" and bits == 32:
        return to_float32(raw[: frames * ch * 4], "s32")
    if bits == 24:
        return _decode_24_in_32(raw[: frames * ch * 4], frames, ch)
    if bits == 32:
        return _decode_24_in_32(raw[: frames * ch * 4], frames, ch)
    if bits == 8:
        return to_float32(raw[: frames * ch], "u8")
    raise ValueError("unsupported WASAPI mix format: %r" % (info,))


def _encode_device_bytes(samples, info: dict) -> bytes:
    fmt = info.get("sample_format") or "f32"
    ch = info.get("channels", CHANNELS)
    bits = info.get("bits", 32)
    if fmt == "f32":
        return as_float_array(samples).tobytes()
    if fmt == "s16":
        return array("h", (max(-32768, min(32767, int(round(s * 32767.0)))) for s in samples)).tobytes()
    if fmt == "s32":
        return array("i", (max(-2147483648, min(2147483647, int(round(s * 2147483647.0)))) for s in samples)).tobytes()
    if bits == 24:
        out = bytearray(len(samples) * 4)
        for i, s in enumerate(samples):
            v = max(-8388608, min(8388607, int(round(s * 8388607.0))))
            o = i * 4
            out[o] = v & 0xFF
            out[o + 1] = (v >> 8) & 0xFF
            out[o + 2] = (v >> 16) & 0xFF
            out[o + 3] = 0xFF if v < 0 else 0x00
        return bytes(out)
    if bits == 8:
        return bytes(max(0, min(255, int(round(s * 127.0)) + 128)) for s in samples)
    raise ValueError("unsupported WASAPI output format: %r" % (info,))


# --------------------------------------------------------------------------
# Device enumeration
# --------------------------------------------------------------------------

def _endpoint_devices(data_flow: int, kind: str) -> list[AudioDevice]:
    _ensure_com()
    enum = wc.create_device_enumerator()
    out: list[AudioDevice] = []
    try:
        collection = enum.enum_audio_endpoints(data_flow, DEVICE_STATE_ACTIVE)
        try:
            try:
                default_id = enum.get_default_audio_endpoint(data_flow, 0).get_id()
            except OSError:
                default_id = ""
            n = collection.count()
            for i in range(n):
                dev = collection.item(i)
                try:
                    dev_id = dev.get_id()
                    name = wc.device_friendly_name(dev) or dev_id
                    info = _mix_format_of(dev)
                    detail = "loopback" if kind == KIND_SOURCE and data_flow == eRender else (
                        "capture" if kind == KIND_SOURCE else "render"
                    )
                    out.append(
                        AudioDevice(
                            id=dev_id,
                            name=name,
                            kind=kind,
                            backend="wasapi",
                            kind_detail=detail,
                            channels=info.get("channels", CHANNELS),
                            sample_rate=info.get("sample_rate", SAMPLE_RATE),
                            is_default=(dev_id == default_id),
                            extra={
                                "data_flow": "render" if data_flow == eRender else "capture",
                                "sample_format": info.get("sample_format", ""),
                                "bits": info.get("bits", 0),
                            },
                        )
                    )
                finally:
                    dev.release()
        finally:
            collection.release()
    finally:
        enum.release()
    return out


def _mix_format_of(dev: wc.IMMDevice) -> dict:
    """Read the shared-mode mix format of an endpoint without keeping the client."""
    try:
        ptr = dev.activate(wc.IID_IAudioClient)
    except OSError:
        return {}
    client = wc.IAudioClient.from_ptr(ptr)
    try:
        fmt_ptr = client.get_mix_format()
        try:
            return wc.format_description(fmt_ptr)
        finally:
            wc.free_format(fmt_ptr)
    except OSError:
        return {}
    finally:
        client.release()


def _name_key(text: str) -> str:
    """把设备名压成用于比较的键：忽略空白、全/半角括号与连字符差异。

    ★ 2026-11-06（第 3 条）：Windows 的「声音」面板显示 `2- C34CHR (...)`，
      而 WASAPI 的 friendly name 是 `2 - C34CHR (...)`（`-` 两边有空格）。
      原来直接 `low in name.lower()`（或精确相等）都对不上，用户从系统面板抄来的
      名字就永远打不开端点。比较前统一归一化，两边都按同一个键匹配。
    """
    import unicodedata

    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = (s.replace("（", "(").replace("）", ")")
          .replace("－", "-").replace("–", "-").replace("—", "-"))
    return "".join(s.split())


def _a2dp_registry_endpoint_id(selector: str, data_flow: int) -> str:
    """按注册表里的 A2DP/SNK 端点名，拼出完整 MMDevice id。

    ★ 2026-10-26 用户：「部分 inter（Intel）电脑蓝牙没声音」。
      实际排查发现：A2DP SNK 端点在 MMDevices 注册表里 DeviceState=1（active），
      但 WASAPI 的 `enum_audio_endpoints(..., DEVICE_STATE_ACTIVE)` 根本不返回它。
      这时按名字枚举会失败，但这块端点仍然可以按 `IMMDeviceEnumerator.GetDevice(id)`
      直接打开。id 由注册表 GUID 拼出来：Capture 是 `{0.0.1.00000000}.{guid}`，
      Render 是 `{0.0.0.00000000}.{guid}`。
    """
    if sys.platform != "win32":
        return ""
    want = _name_key(selector)
    if not want:
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    flow = "Capture" if data_flow == eCapture else "Render"
    prefix = "{0.0.1.00000000}." if data_flow == eCapture else "{0.0.0.00000000}."
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % flow
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            index = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, guid) as dev:
                        try:
                            state, _ = winreg.QueryValueEx(dev, "DeviceState")
                        except OSError:
                            continue
                        if int(state) != 1:
                            continue
                        friendly = ""
                        try:
                            with winreg.OpenKey(dev, "Properties") as props:
                                try:
                                    friendly, _ = winreg.QueryValueEx(
                                        props, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                                except OSError:
                                    pass
                        except OSError:
                            pass
                except OSError:
                    continue
                text = str(friendly or "")
                norm = _name_key(text)
                if not norm or want not in norm:
                    continue
                # ★ 2026-11-06（第 3 条）：渲染端点（送声/回环）不需要名字里带 A2DP/SNK。
                #   蓝牙耳机在 Windows 里的渲染端点常叫「耳机 (某蓝牙耳机)」；
                #   原来只认 A2DP/SNK，导致蓝牙渲染端点不在 ACTIVE 枚举里时永远回落不到。
                if data_flow == eCapture and "a2dp" not in norm and "snk" not in norm:
                    continue
                return prefix + guid
    except OSError:
        return ""
    return ""


def _find_endpoint(selector: str, data_flow: int, kind: str) -> wc.IMMDevice:
    """Resolve a user selector (device id, exact name, or substring) to an endpoint.

    Ownership: the returned IMMDevice carries one reference for the caller, and we
    hold one extra reference on the enumerator so it can never be collected before
    a device we hand out. Releasing the enumerator in the ``finally`` drops ours.
    """
    _ensure_com()
    enum = wc.create_device_enumerator()
    try:
        if not selector or selector.lower() in ("default", "@default"):
            return enum.get_default_audio_endpoint(data_flow, 0)

        # 1. Exact endpoint id.
        try:
            dev = enum.get_device(selector)
            if dev.valid:
                return dev
        except OSError:
            pass

        # 2. Exact name, then case-insensitive substring, over active endpoints.
        collection = enum.enum_audio_endpoints(data_flow, DEVICE_STATE_ACTIVE)
        chosen: wc.IMMDevice | None = None
        try:
            count = collection.count()
            names: list[tuple[wc.IMMDevice, str, str]] = []
            for i in range(count):
                dev = collection.item(i)
                try:
                    name = wc.device_friendly_name(dev) or ""
                    dev_id = dev.get_id()
                except OSError:
                    dev.release()
                    continue
                names.append((dev, name, dev_id))

            low = _name_key(selector)
            if not low:
                raise OSError("设备名是空的，无法定位端点")
            for dev, name, _dev_id in names:
                if _name_key(name) == low:
                    chosen = dev
                    break
            if chosen is None:
                for dev, name, dev_id in names:
                    if low in _name_key(name) or low in _name_key(dev_id):
                        chosen = dev
                        break
            if chosen is None:
                # ★ 2026-10-26：A2DP SNK 在注册表 active、但 WASAPI ACTIVE 枚举里没有。
                #   按注册表 GUID 拼 id，直接 GetDevice；这条路在 Intel/Realtek 驱动上实测可用。
                registry_id = _a2dp_registry_endpoint_id(selector, data_flow)
                if registry_id:
                    try:
                        dev = enum.get_device(registry_id)
                        if dev.valid:
                            return dev
                        dev.release()
                    except OSError:
                        pass
                flow = "输出/回环" if data_flow == eRender else "采集"
                raise OSError("找不到%s端点 %r：它现在可能没连上/被禁用，或名字和系统里的不一致" % (flow, selector))

            # Hold a reference for the caller, drop the rest.
            chosen.call("AddRef")
            for dev, _name, _dev_id in names:
                if dev is not chosen:
                    dev.release()
            return chosen
        finally:
            collection.release()
    finally:
        enum.release()


# --------------------------------------------------------------------------
# Capture (what the machine is playing, or a microphone)
# --------------------------------------------------------------------------

class WasapiSource:
    """Loopback capture of a render endpoint (system sound) or a capture endpoint."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.device_name = spec.get("device") or "@default"
        self.loopback = bool(spec.get("loopback", True))
        self._data_flow = eRender if self.loopback else eCapture
        self._stop = threading.Event()
        self._dev: wc.IMMDevice | None = None
        self._client: wc.IAudioClient | None = None
        self._capture: wc.IAudioCaptureClient | None = None
        self._info: dict = {}
        self._resampler: Resampler | None = None
        self._fmt_ptr = 0
        self.frames_captured = 0
        self.overflows = 0
        self.glitches = 0
        self.name = ""

    def start(self) -> None:
        _ensure_com()
        self._dev = _find_endpoint(self.device_name, self._data_flow, KIND_SOURCE)
        endpoint_name = wc.device_friendly_name(self._dev)
        ptr = self._dev.activate(wc.IID_IAudioClient)
        self._client = wc.IAudioClient.from_ptr(ptr)
        self._fmt_ptr = self._client.get_mix_format()
        self._info = wc.format_description(self._fmt_ptr)
        self.name = "%s%s" % (endpoint_name or self.device_name, " [loopback]" if self.loopback else "")
        flags = AUDCLNT_STREAMFLAGS_NOPERSIST
        if self.loopback:
            flags |= AUDCLNT_STREAMFLAGS_LOOPBACK
        # Shared mode, 20 ms engine buffer, periodicity 0 (let WASAPI choose).
        self._client.initialize(
            AUDCLNT_SHAREMODE_SHARED,
            flags,
            20 * REFTIMES_PER_MS,
            0,
            self._fmt_ptr,
        )
        self._capture = wc.IAudioCaptureClient.from_ptr(self._client.get_service(wc.IID_IAudioCaptureClient))
        device_rate = self._info.get("sample_rate", SAMPLE_RATE)
        device_ch = self._info.get("channels", CHANNELS)
        if device_rate != SAMPLE_RATE:
            self._resampler = Resampler(device_rate, SAMPLE_RATE, CHANNELS)
        self._device_channels = device_ch
        self._client.start()

    def read(self):
        """Drain everything available; returns interleaved float32 @48k stereo."""
        if self._stop.is_set() or self._capture is None or self._client is None:
            return array("f")
        out = array("f")
        idle = 0
        while not self._stop.is_set():
            try:
                available = self._capture.get_next_packet_size()
            except OSError:
                break
            if available == 0:
                if out:
                    break
                idle += 1
                if idle > 4:
                    break
                time.sleep(0.002)
                continue
            idle = 0
            try:
                data_ptr, frames, flags, _dev_pos, _qpc = self._capture.get_buffer()
            except OSError:
                break
            try:
                if frames:
                    if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                        self.glitches += 1
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr:
                        block = array("f", bytes(4 * frames * self._device_channels))
                    else:
                        size = frames * self._device_channels * max(1, self._info.get("bits", 16) // 8)
                        raw = ctypes.string_at(data_ptr, size)
                        block = _decode_device_bytes(raw, frames, self._info)
                    if self._device_channels != CHANNELS:
                        block = channel_map(block, self._device_channels, CHANNELS)
                    if self._resampler is not None:
                        block = self._resampler.process(block)
                    out.extend(block)
                    self.frames_captured += len(block) // CHANNELS
            finally:
                try:
                    self._capture.release_buffer(frames)
                except OSError:
                    pass
        return out

    def stop(self) -> None:
        self._stop.set()
        for obj, name in ((self._client, "client"), (self._capture, "capture")):
            if obj is not None:
                try:
                    if name == "client":
                        obj.stop()
                except Exception:
                    pass
                obj.release()
        self._client = None
        self._capture = None
        if self._fmt_ptr:
            wc.free_format(self._fmt_ptr)
            self._fmt_ptr = 0
        if self._dev is not None:
            self._dev.release()
            self._dev = None

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "wasapi",
            "device": self.device_name,
            "name": self.name,
            "kind": "loopback" if self.loopback else "capture",
            "endpoint_format": self._info,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames_captured": self.frames_captured,
            "glitches": self.glitches,
        }


# --------------------------------------------------------------------------
# Render (play to a chosen output device)
# --------------------------------------------------------------------------

class WasapiSink:
    """Plays interleaved float32 @48k stereo to one output endpoint."""

    # How many device buffers of resampled audio may be held while the endpoint is
    # not draining. Bounded so a dead device cannot turn into seconds of latency.
    MAX_BACKLOG_BLOCKS = 6

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.device_name = spec.get("device") or "@default"
        self._stop = threading.Event()
        self._dev: wc.IMMDevice | None = None
        self._client: wc.IAudioClient | None = None
        self._render: wc.IAudioRenderClient | None = None
        self._info: dict = {}
        self._fmt_ptr = 0
        self._buffer_frames = 0
        self._clock_start_ns = 0
        self._frames_submitted = 0
        self._resampler: Resampler | None = None
        self.name = ""
        self.underruns = 0
        # Resampled samples the device has not accepted yet, plus a count of what
        # had to be thrown away when a stalled endpoint let the backlog grow.
        self._out = array("f")
        self.dropped_frames = 0
        # ★ 2026-10-26：真正的"声卡已经放空"次数。原来的 underruns 统计的是
        #   "这次没把整块塞进声卡"（余量还在 _out 里），稳态下经常发生，**不是断音**；
        #   这个计数只在两次写入之间设备缓冲真的归零时 +1，才是听得到的断档证据。
        self.starvations = 0
        self.target_ms = float(spec.get("target_ms", 40.0))

    def start(self) -> None:
        _ensure_com()
        self._dev = _find_endpoint(self.device_name, eRender, KIND_SINK)
        endpoint_name = wc.device_friendly_name(self._dev)
        ptr = self._dev.activate(wc.IID_IAudioClient)
        self._client = wc.IAudioClient.from_ptr(ptr)
        self._fmt_ptr = self._client.get_mix_format()
        self._info = wc.format_description(self._fmt_ptr)
        device_rate = self._info.get("sample_rate", SAMPLE_RATE)
        requested = device_rate if self.spec.get("match_device_rate", True) else SAMPLE_RATE
        if requested != device_rate:
            # Ask for our own rate; WASAPI will insert its own SRC if it accepts it.
            buf, info = wc.make_format(SAMPLE_RATE, self._info.get("channels", CHANNELS), 32, True)
            cbuf = ctypes.create_string_buffer(buf, len(buf))
            try:
                self._client.initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_NOPERSIST,
                    20 * REFTIMES_PER_MS,
                    0,
                    ctypes.cast(cbuf, c_void_p).value,
                )
                self._info = info
                device_rate = SAMPLE_RATE
            except OSError:
                pass
        if device_rate != SAMPLE_RATE:
            self._resampler = Resampler(SAMPLE_RATE, device_rate, CHANNELS)
        hr = self._client.call(
            "Initialize",
            AUDCLNT_SHAREMODE_SHARED,
            DWORD(AUDCLNT_STREAMFLAGS_NOPERSIST),
            wc.REFERENCE_TIME(20 * REFTIMES_PER_MS),
            wc.REFERENCE_TIME(0),
            ctypes.c_void_p(self._fmt_ptr),
            None,
        )
        import os as _os

        if _os.environ.get("AUDIOMIXING_DEBUG") or _os.environ.get("WAVELINK_DEBUG"):
            print("[wasapi] Initialize hr=%s fmt_ptr=0x%X info=%s" % (wc.hresult_name(hr), self._fmt_ptr, self._info))
        if wc.failed(hr):
            raise wc.win_error("IAudioClient::Initialize", hr)
        self._buffer_frames = self._client.get_buffer_size()
        self._render = wc.IAudioRenderClient.from_ptr(self._client.get_service(wc.IID_IAudioRenderClient))
        self._client.start()
        self._clock_start_ns = time.monotonic_ns()
        self.name = endpoint_name or self.device_name

    def write(self, samples) -> int:
        """Feed one block; returns how many *input* frames were accepted.

        Rate conversion happens here rather than in the caller. When a resampler is
        in the path the device takes a different number of frames than it was handed
        (48000 in, 44100 out), so returning a device-frame count made the caller's
        cursor skip input it had never consumed - heard as severe distortion, and it
        only happened on devices that do not run at 48 kHz. Whatever the device does
        not take is buffered here, so nothing is lost either way.
        """
        if self._stop.is_set() or self._render is None or self._client is None:
            return 0
        samples = as_float_array(samples)
        if not samples:
            return 0
        in_frames = len(samples) // CHANNELS
        if self._resampler is not None:
            samples = as_float_array(self._resampler.process(samples))
            if not samples:
                return in_frames
        if self._out:
            samples = self._out + samples
            self._out = array("f")
        total_frames = len(samples) // CHANNELS
        written = 0
        # 写之前先看一眼设备缓冲：已经归零 = 上一块放完到这一块之间有一小段空气，
        # 这才是"卡一下"的直接证据。启动前/还没喂过数据时不算。
        try:
            if (self._client.get_current_padding() <= 0
                    and self._frames_submitted >= self._buffer_frames
                    and not self._stop.is_set()):
                self.starvations += 1
        except OSError:
            pass
        while written < total_frames and not self._stop.is_set():
            try:
                padding = self._client.get_current_padding()
            except OSError:
                break
            space = self._buffer_frames - padding
            if space <= 0:
                break
            take = min(space, total_frames - written)
            chunk = samples[written * CHANNELS : (written + take) * CHANNELS]
            dev_ch = self._info.get("channels", CHANNELS)
            if dev_ch != CHANNELS:
                chunk = channel_map(chunk, CHANNELS, dev_ch)
            try:
                dst = self._render.get_buffer(take)
                payload = _encode_device_bytes(chunk, self._info)
                ctypes.memmove(dst, payload, len(payload))
                self._render.release_buffer(take, 0)
            except OSError:
                break
            written += take
            self._frames_submitted += take
        if written < total_frames:
            self.underruns += 1
            self._out = samples[written * CHANNELS :]
            # Bound the backlog: a stalled endpoint must not grow it forever.
            cap = self._buffer_frames * self.MAX_BACKLOG_BLOCKS
            if len(self._out) > cap * CHANNELS:
                self.dropped_frames += (len(self._out) - cap * CHANNELS) // CHANNELS
                self._out = self._out[-cap * CHANNELS :]
        return in_frames

    def buffered_frames(self) -> int:
        if self._client is None:
            return 0
        try:
            return int(self._client.get_current_padding())
        except OSError:
            return 0

    def target_frames(self) -> int:
        return int(self._buffer_frames * 0.6)

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
        for obj in (self._render, self._client):
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
        self._render = None
        self._client = None
        if self._fmt_ptr:
            wc.free_format(self._fmt_ptr)
            self._fmt_ptr = 0
        if self._dev is not None:
            self._dev.release()
            self._dev = None

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "wasapi",
            "device": self.device_name,
            "name": self.name,
            "kind": "render",
            "endpoint_format": self._info,
            "buffer_frames": self._buffer_frames,
            "frames_submitted": self._frames_submitted,
            "underruns": self.underruns,
            "starvations": self.starvations,
            "dropped_frames": self.dropped_frames,
            "backlog_frames": len(self._out) // CHANNELS,
            "resampled": self._resampler is not None and self._resampler.enabled,
            # buffer_ms 是"现在攒了多少毫秒"；capacity_ms 才是这块声卡的缓冲总时长。
            "buffer_ms": round(self.buffered_frames() * 1000.0 / SAMPLE_RATE, 2),
            "capacity_ms": round(self._buffer_frames * 1000.0 / SAMPLE_RATE, 2),
        }


class WasapiMeter:
    """当前输出设备"在出声多大声"的直读表：不采集、不播放、不动系统音量。

    为什么要有它：手机经蓝牙把声音送进来时，播放是 **Windows 直接做的**，本工具那一路
    （本机直通 / 我们自己的播放端）根本没有数据，面板上的"本机播放电平"就永远是 0
    （用户真机反馈「手机播放声音这个电平没有反应」，实机 /api/status 里
    ``local.note`` 正是"音源就是输出设备本身，这里不再重复处理"）。
    回环采集也能测到，但用户反馈过"同一块设备多开一路采集会越听越卡"。
    IAudioMeterInformation 是音频引擎自带的峰值表：读一次 float 就有数，不占设备、
    也不打扰正在播放的声音 —— 这正是用户要的"直接显示当前设备的音量大小"。
    """

    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        self.spec = dict(spec or {})
        self.device_name = str(self.spec.get("device") or "@default")
        self._dev: wc.IMMDevice | None = None
        self._meter: wc.IAudioMeterInformation | None = None
        self.name = ""
        self.error = ""

    def open(self) -> bool:
        """打开（或重新打开）这块端点的峰值表。失败不抛，调用方看 error。"""
        self.close()
        try:
            _ensure_com()
            self._dev = _find_endpoint(self.device_name, eRender, KIND_SINK)
            self.name = wc.device_friendly_name(self._dev) or self.device_name
            self._meter = wc.IAudioMeterInformation.from_ptr(
                self._dev.activate(wc.IID_IAudioMeterInformation)
            )
            # 立刻读一次：有的端点上 Activate 成功、读值却失败，那样必须当场认出来。
            self._meter.get_peak_value()
            self.error = ""
            return True
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.close()
            return False

    def peak(self) -> float | None:
        """0..1 的峰值；接口没了返回 None（调用方据此重开）。"""
        if self._meter is None:
            return None
        try:
            return float(self._meter.get_peak_value())
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.close()
            return None

    def device(self) -> str:
        return self.name or self.device_name

    def describe(self) -> dict[str, Any]:
        return {"backend": "wasapi", "kind": "meter", "device": self.device_name,
                "name": self.name, "error": self.error, "open": self._meter is not None}

    def close(self) -> None:
        if self._meter is not None:
            self._meter.release()
            self._meter = None
        if self._dev is not None:
            self._dev.release()
            self._dev = None


@register
class WasapiBackend(BackendBase):
    name = "wasapi"
    platform = "win32"

    @classmethod
    def available(cls) -> bool:
        if not hasattr(ctypes, "WinDLL"):
            return False
        try:
            _ensure_com()
            enum = wc.create_device_enumerator()
            enum.release()
            return True
        except Exception:
            return False

    @classmethod
    def list_devices(cls) -> list[AudioDevice]:
        devices = _endpoint_devices(eRender, KIND_SOURCE)
        devices += _endpoint_devices(eCapture, KIND_SOURCE)
        devices += _endpoint_devices(eRender, KIND_SINK)
        return devices

    @classmethod
    def open_source(cls, spec: dict[str, Any]) -> WasapiSource:
        return WasapiSource(spec)

    @classmethod
    def open_sink(cls, spec: dict[str, Any]) -> WasapiSink:
        return WasapiSink(spec)
