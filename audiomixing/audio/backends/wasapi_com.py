"""Minimal COM plumbing for the WASAPI backend (ctypes only, no dependencies).

Only what Audio Mixing needs: IUnknown reference counting, vtable slot lookup,
interface pointers, and the handful of GUID helpers WASAPI requires.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_void_p, cast
from ctypes.wintypes import DWORD, LPCWSTR, WORD

HRESULT = ctypes.c_long
ULONG = ctypes.c_ulong
UINT32 = ctypes.c_uint32
UINT64 = ctypes.c_uint64
REFERENCE_TIME = ctypes.c_longlong
BYTE = ctypes.c_ubyte

S_OK = 0
S_FALSE = 1
AUDCLNT_S_BUFFER_EMPTY = 0x08890001
AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED = -2004287096
AUDCLNT_E_DEVICE_INVALIDATED = -2004287480
AUDCLNT_S_NO_SINGLE_DEVICE = 0x08890008
AUDCLNT_E_UNSUPPORTED_FORMAT = -2004287482
AUDCLNT_E_BUFFER_TOO_LARGE = -2004287478
AUDCLNT_E_OUT_OF_ORDER = -2004287486
AUDCLNT_E_NOT_STOPPED = -2004287484
AUDCLNT_E_NOT_INITIALIZED = -2004287472
AUDCLNT_E_ALREADY_INITIALIZED = -2004287488
AUDCLNT_E_WRONG_ENDPOINT_TYPE = -2004287490
AUDCLNT_E_DEVICE_IN_USE = -2004287482
AUDCLNT_E_SERVICE_NOT_RUNNING = -2004287464

E_POINTER = -2147467261
E_NOINTERFACE = -2147467262
CLASS_E_NOAGGREGATION = -2147221232
E_INVALIDARG = -2147024809
REGDB_E_CLASSNOTREG = -2147221164
CO_E_NOTINITIALIZED = -2147221008


def hresult_name(hr: int) -> str:
    names = {
        S_OK: "S_OK",
        S_FALSE: "S_FALSE",
        AUDCLNT_S_BUFFER_EMPTY: "AUDCLNT_S_BUFFER_EMPTY",
        AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED: "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
        AUDCLNT_E_DEVICE_INVALIDATED: "AUDCLNT_E_DEVICE_INVALIDATED",
        AUDCLNT_E_UNSUPPORTED_FORMAT: "AUDCLNT_E_UNSUPPORTED_FORMAT",
        AUDCLNT_S_NO_SINGLE_DEVICE: "AUDCLNT_S_NO_SINGLE_DEVICE",
        AUDCLNT_E_BUFFER_TOO_LARGE: "AUDCLNT_E_BUFFER_TOO_LARGE",
        AUDCLNT_E_OUT_OF_ORDER: "AUDCLNT_E_OUT_OF_ORDER",
        AUDCLNT_E_NOT_STOPPED: "AUDCLNT_E_NOT_STOPPED",
        AUDCLNT_E_NOT_INITIALIZED: "AUDCLNT_E_NOT_INITIALIZED",
        AUDCLNT_E_ALREADY_INITIALIZED: "AUDCLNT_E_ALREADY_INITIALIZED",
        AUDCLNT_E_WRONG_ENDPOINT_TYPE: "AUDCLNT_E_WRONG_ENDPOINT_TYPE",
        AUDCLNT_E_DEVICE_IN_USE: "AUDCLNT_E_DEVICE_IN_USE",
        AUDCLNT_E_SERVICE_NOT_RUNNING: "AUDCLNT_E_SERVICE_NOT_RUNNING",
        E_POINTER: "E_POINTER",
        E_NOINTERFACE: "E_NOINTERFACE",
        E_INVALIDARG: "E_INVALIDARG",
        CO_E_NOTINITIALIZED: "CO_E_NOTINITIALIZED",
    }
    if hr in names:
        return names[hr]
    return "0x%08X" % (hr & 0xFFFFFFFF)


def failed(hr: int) -> bool:
    return hr < 0


def win_error(message: str, hr: int) -> OSError:
    return OSError(0, "%s failed: %s (%d)" % (message, hresult_name(hr), hr))


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        d4 = "".join("%02X" % b for b in self.Data4)
        return "{%08X-%04X-%04X-%s-%s}" % (
            self.Data1,
            self.Data2,
            self.Data3,
            d4[:4],
            d4[4:],
        )


def guid(text: str) -> GUID:
    t = text.strip().strip("{}")
    parts = t.split("-")
    if len(parts) != 5:
        raise ValueError("bad GUID: %r" % text)
    d4 = bytes.fromhex(parts[3] + parts[4])
    g = GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    for i in range(8):
        g.Data4[i] = d4[i]
    return g


def guid_ref(text: str) -> ctypes.Array:
    """A GUID suitable for passing as REFIID, cached per string."""
    cached = _GUID_CACHE.get(text)
    if cached is None:
        cached = guid(text)
        _GUID_CACHE[text] = cached
    return byref(cached)


_GUID_CACHE: dict[str, GUID] = {}


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------

class ComPtr:
    """Base for COM interface wrappers: holds a raw pointer and calls vtable slots.

    Subclasses declare the vtable order by listing method names in
    ``_method_names``, starting after the three IUnknown entries.
    """

    _method_names: tuple[str, ...] = ()
    _iid: str = ""
    _initialized = False

    def __init__(self, ptr: int = 0) -> None:
        self._p = ptr

    @classmethod
    def from_ptr(cls, ptr: int):
        obj = cls(ptr)
        if not cls._initialized:
            cls._bind()
        return obj

    @classmethod
    def _bind(cls) -> None:
        """Build ctypes prototypes for every vtable slot, once per class."""
        names = ("QueryInterface", "AddRef", "Release") + cls._method_names
        cls._slots = []
        cls._vtable_type = ctypes.c_void_p * len(names)
        for i, name in enumerate(names):
            proto = getattr(cls, "_proto_" + name, None)
            if proto is None:
                proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p)
            cls._slots.append((name, i, proto))
        cls._initialized = True

    @property
    def ptr(self) -> int:
        return self._p

    @property
    def valid(self) -> bool:
        return bool(self._p)

    def raw_vtable_ptr(self) -> int:
        vtable = cast(self._p, POINTER(ctypes.c_void_p))[0]
        return vtable

    def call(self, name: str, *args):
        if not self._initialized:
            type(self)._bind()
        for slot_name, index, proto in self._slots:  # type: ignore[attr-defined]
            if slot_name == name:
                vtable = self.raw_vtable_ptr()
                fn_ptr = cast(vtable, POINTER(ctypes.c_void_p))[index]
                fn = proto(fn_ptr)
                return fn(self._p, *args)
        raise AttributeError("no such COM method %r on %s" % (name, type(self).__name__))

    def release(self) -> None:
        if self._p:
            try:
                self.call("Release")
            except Exception:
                pass
            self._p = 0

    def query(self, iid: str, cls=None):
        """QueryInterface 到一个新接口（失败抛 OSError）。"""
        ptr = c_void_p()
        hr = self.call("QueryInterface", guid_ref(iid), byref(ptr))
        if failed(hr):
            raise win_error("QueryInterface(%s)" % iid, hr)
        target = cls or type(self)
        return target.from_ptr(ptr.value)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.release()
        except Exception:
            pass

    # -- QueryInterface/AddRef/Release prototypes (identical for all) -------
    _proto_QueryInterface = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
    _proto_AddRef = ctypes.WINFUNCTYPE(ULONG, c_void_p)
    _proto_Release = ctypes.WINFUNCTYPE(ULONG, c_void_p)


class IPropertyStore(ComPtr):
    _method_names = ("GetCount", "GetAt", "GetValue", "SetValue", "Commit")
    _proto_GetCount = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(DWORD))
    _proto_GetAt = ctypes.WINFUNCTYPE(HRESULT, c_void_p, DWORD, c_void_p)
    _proto_GetValue = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
    _proto_SetValue = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
    _proto_Commit = ctypes.WINFUNCTYPE(HRESULT, c_void_p)


class IMMDevice(ComPtr):
    _method_names = ("Activate", "OpenPropertyStore", "GetId", "GetState")
    _proto_Activate = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, DWORD, c_void_p, c_void_p)
    _proto_OpenPropertyStore = ctypes.WINFUNCTYPE(HRESULT, c_void_p, DWORD, c_void_p)
    _proto_GetId = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
    _proto_GetState = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(DWORD))

    def activate(self, iid: str, clsctx: int = 0):
        ptr = c_void_p()
        hr = self.call("Activate", guid_ref(iid), DWORD(clsctx), None, byref(ptr))
        if failed(hr):
            raise win_error("IMMDevice::Activate", hr)
        return ptr.value

    def open_property_store(self) -> IPropertyStore:
        ptr = c_void_p()
        hr = self.call("OpenPropertyStore", DWORD(0), byref(ptr))
        if failed(hr):
            raise win_error("IMMDevice::OpenPropertyStore", hr)
        return IPropertyStore.from_ptr(ptr.value)

    def get_id(self) -> str:
        ptr = c_void_p()
        hr = self.call("GetId", byref(ptr))
        if failed(hr):
            raise win_error("IMMDevice::GetId", hr)
        try:
            return ctypes.wstring_at(ptr.value)
        finally:
            ole32.CoTaskMemFree(ptr)

    def get_state(self) -> int:
        state = DWORD()
        hr = self.call("GetState", byref(state))
        if failed(hr):
            raise win_error("IMMDevice::GetState", hr)
        return state.value


class IMMDeviceCollection(ComPtr):
    _method_names = ("GetCount", "Item")
    _proto_GetCount = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(DWORD))
    _proto_Item = ctypes.WINFUNCTYPE(HRESULT, c_void_p, DWORD, c_void_p)

    def count(self) -> int:
        n = DWORD()
        hr = self.call("GetCount", byref(n))
        if failed(hr):
            raise win_error("IMMDeviceCollection::GetCount", hr)
        return n.value

    def item(self, index: int) -> IMMDevice:
        ptr = c_void_p()
        hr = self.call("Item", DWORD(index), byref(ptr))
        if failed(hr):
            raise win_error("IMMDeviceCollection::Item", hr)
        return IMMDevice.from_ptr(ptr.value)


class IMMDeviceEnumerator(ComPtr):
    _method_names = (
        "EnumAudioEndpoints",
        "GetDefaultAudioEndpoint",
        "GetDevice",
        "RegisterEndpointNotificationCallback",
        "UnregisterEndpointNotificationCallback",
    )
    _proto_EnumAudioEndpoints = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_int, DWORD, c_void_p)
    _proto_GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_int, ctypes.c_int, c_void_p)
    _proto_GetDevice = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR, c_void_p)

    def enum_audio_endpoints(self, data_flow: int, state_mask: int) -> IMMDeviceCollection:
        ptr = c_void_p()
        hr = self.call("EnumAudioEndpoints", data_flow, DWORD(state_mask), byref(ptr))
        if failed(hr):
            raise win_error("IMMDeviceEnumerator::EnumAudioEndpoints", hr)
        return IMMDeviceCollection.from_ptr(ptr.value)

    def get_default_audio_endpoint(self, data_flow: int, role: int) -> IMMDevice:
        ptr = c_void_p()
        hr = self.call("GetDefaultAudioEndpoint", data_flow, role, byref(ptr))
        if failed(hr):
            raise win_error("IMMDeviceEnumerator::GetDefaultAudioEndpoint", hr)
        return IMMDevice.from_ptr(ptr.value)

    def get_device(self, device_id: str) -> IMMDevice:
        ptr = c_void_p()
        hr = self.call("GetDevice", LPCWSTR(device_id), byref(ptr))
        if failed(hr):
            raise win_error("IMMDeviceEnumerator::GetDevice", hr)
        return IMMDevice.from_ptr(ptr.value)


class IAudioClient(ComPtr):
    _method_names = (
        "Initialize",
        "GetBufferSize",
        "GetStreamLatency",
        "GetCurrentPadding",
        "IsFormatSupported",
        "GetMixFormat",
        "GetDevicePeriod",
        "Start",
        "Stop",
        "Reset",
        "SetEventHandle",
        "GetService",
    )
    _proto_Initialize = ctypes.WINFUNCTYPE(
        HRESULT, c_void_p, ctypes.c_int, DWORD, REFERENCE_TIME, REFERENCE_TIME, c_void_p, c_void_p
    )
    _proto_GetBufferSize = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(UINT32))
    _proto_GetStreamLatency = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(REFERENCE_TIME))
    _proto_GetCurrentPadding = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(UINT32))
    _proto_IsFormatSupported = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_int, c_void_p, c_void_p)
    _proto_GetMixFormat = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
    _proto_GetDevicePeriod = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(REFERENCE_TIME), POINTER(REFERENCE_TIME))
    _proto_Start = ctypes.WINFUNCTYPE(HRESULT, c_void_p)
    _proto_Stop = ctypes.WINFUNCTYPE(HRESULT, c_void_p)
    _proto_Reset = ctypes.WINFUNCTYPE(HRESULT, c_void_p)
    _proto_SetEventHandle = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)
    _proto_GetService = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)

    def initialize(self, share_mode: int, flags: int, buffer_duration: int, periodicity: int, fmt_ptr: int, session_guid=None):
        hr = self.call(
            "Initialize",
            share_mode,
            DWORD(flags),
            REFERENCE_TIME(buffer_duration),
            REFERENCE_TIME(periodicity),
            ctypes.c_void_p(fmt_ptr),
            session_guid,
        )
        if failed(hr):
            raise win_error("IAudioClient::Initialize", hr)

    def get_buffer_size(self) -> int:
        n = UINT32()
        hr = self.call("GetBufferSize", byref(n))
        if failed(hr):
            raise win_error("IAudioClient::GetBufferSize", hr)
        return n.value

    def get_current_padding(self) -> int:
        n = UINT32()
        hr = self.call("GetCurrentPadding", byref(n))
        if failed(hr):
            raise win_error("IAudioClient::GetCurrentPadding", hr)
        return n.value

    def get_mix_format(self) -> int:
        ptr = c_void_p()
        hr = self.call("GetMixFormat", byref(ptr))
        if failed(hr):
            raise win_error("IAudioClient::GetMixFormat", hr)
        return ptr.value

    def get_device_period(self) -> tuple[int, int]:
        default = REFERENCE_TIME()
        minimum = REFERENCE_TIME()
        hr = self.call("GetDevicePeriod", byref(default), byref(minimum))
        if failed(hr):
            raise win_error("IAudioClient::GetDevicePeriod", hr)
        return default.value, minimum.value

    def start(self) -> None:
        hr = self.call("Start")
        if failed(hr):
            raise win_error("IAudioClient::Start", hr)

    def stop(self) -> None:
        hr = self.call("Stop")
        if failed(hr) and hr != AUDCLNT_E_NOT_STOPPED:
            raise win_error("IAudioClient::Stop", hr)

    def get_service(self, iid: str):
        ptr = c_void_p()
        hr = self.call("GetService", guid_ref(iid), byref(ptr))
        if failed(hr):
            raise win_error("IAudioClient::GetService(%s)" % iid, hr)
        return ptr.value

    def set_event_handle(self, handle: int) -> None:
        hr = self.call("SetEventHandle", ctypes.c_void_p(handle))
        if failed(hr):
            raise win_error("IAudioClient::SetEventHandle", hr)


class IAudioCaptureClient(ComPtr):
    _method_names = ("GetBuffer", "ReleaseBuffer", "GetNextPacketSize")
    _proto_GetBuffer = ctypes.WINFUNCTYPE(
        HRESULT, c_void_p, POINTER(c_void_p), POINTER(UINT32), POINTER(DWORD), POINTER(UINT64), POINTER(UINT64)
    )
    _proto_ReleaseBuffer = ctypes.WINFUNCTYPE(HRESULT, c_void_p, UINT32)
    _proto_GetNextPacketSize = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(UINT32))

    def get_next_packet_size(self) -> int:
        n = UINT32()
        hr = self.call("GetNextPacketSize", byref(n))
        if failed(hr):
            raise win_error("IAudioCaptureClient::GetNextPacketSize", hr)
        return n.value

    def get_buffer(self) -> tuple[int, int, int, int, int]:
        """Returns (ptr, frames, flags, device_position, qpc_position)."""
        data = c_void_p()
        frames = UINT32()
        flags = DWORD()
        dev_pos = UINT64()
        qpc = UINT64()
        hr = self.call(
            "GetBuffer",
            byref(data),
            byref(frames),
            byref(flags),
            byref(dev_pos),
            byref(qpc),
        )
        if failed(hr):
            raise win_error("IAudioCaptureClient::GetBuffer", hr)
        return data.value or 0, frames.value, flags.value, dev_pos.value, qpc.value

    def release_buffer(self, frames: int) -> None:
        hr = self.call("ReleaseBuffer", UINT32(frames))
        if failed(hr):
            raise win_error("IAudioCaptureClient::ReleaseBuffer", hr)


class IAudioRenderClient(ComPtr):
    _method_names = ("GetBuffer", "ReleaseBuffer")
    _proto_GetBuffer = ctypes.WINFUNCTYPE(HRESULT, c_void_p, UINT32, POINTER(c_void_p))
    _proto_ReleaseBuffer = ctypes.WINFUNCTYPE(HRESULT, c_void_p, UINT32, DWORD)

    def get_buffer(self, frames: int) -> int:
        data = c_void_p()
        hr = self.call("GetBuffer", UINT32(frames), byref(data))
        if failed(hr):
            raise win_error("IAudioRenderClient::GetBuffer", hr)
        return data.value or 0

    def release_buffer(self, frames: int, flags: int = 0) -> None:
        hr = self.call("ReleaseBuffer", UINT32(frames), DWORD(flags))
        if failed(hr):
            raise win_error("IAudioRenderClient::ReleaseBuffer", hr)


class IAudioMeterInformation(ComPtr):
    """某个音频端点**当前在出声多大声**的峰值表。

    为什么要它，而不是再开一路回环采集去量电平：真机反馈过"同一块设备多开一路采集
    会越听越卡"（尤其是蓝牙耳机）。这个接口是音频引擎自己维护的峰值表 —— 读一次
    float 就有数，**不占用设备、不影响正在播放的声音**，所以它是"直接显示当前设备的
    音量大小"最省事也最不打扰的做法。

    槽位顺序必须是 GetPeakValue → GetMeteringChannelCount → GetChannelsPeakValues
    → QueryHardwareSupport：少写一个名字会让后面所有槽位整体错位。这里用不到后两个，
    但仍然按声明顺序列全，保证索引正确。
    """

    _method_names = (
        "GetPeakValue",
        "GetMeteringChannelCount",
        "GetChannelsPeakValues",
        "QueryHardwareSupport",
    )
    _proto_GetPeakValue = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_float))
    _proto_GetMeteringChannelCount = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(UINT32))
    _proto_GetChannelsPeakValues = ctypes.WINFUNCTYPE(HRESULT, c_void_p, UINT32, c_void_p)
    _proto_QueryHardwareSupport = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(DWORD))

    def get_peak_value(self) -> float:
        value = ctypes.c_float()
        hr = self.call("GetPeakValue", byref(value))
        if failed(hr):
            raise win_error("IAudioMeterInformation::GetPeakValue", hr)
        return float(value.value)


# --------------------------------------------------------------------------
# Audio format structures
# --------------------------------------------------------------------------

class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", WORD),
        ("dwChannelMask", DWORD),
        ("SubFormat", GUID),
    ]


WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

KSDATAFORMAT_SUBTYPE_PCM = "{00000001-0000-0010-8000-00AA00389B71}"
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "{00000003-0000-0010-8000-00AA00389B71}"


def format_description(fmt_ptr: int) -> dict:
    """Decode a WAVEFORMATEX*/WAVEFORMATEXTENSIBLE* into a plain dict."""
    if not fmt_ptr:
        return {}
    base = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEX)).contents
    info = {
        "format_tag": base.wFormatTag,
        "channels": base.nChannels,
        "sample_rate": base.nSamplesPerSec,
        "bits": base.wBitsPerSample,
        "block_align": base.nBlockAlign,
        "avg_bytes": base.nAvgBytesPerSec,
        "cbSize": base.cbSize,
        "extensible": base.wFormatTag == WAVE_FORMAT_EXTENSIBLE,
        "sample_format": "",
        "channel_mask": 0,
        "valid_bits": base.wBitsPerSample,
    }
    if info["extensible"]:
        ext = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEXTENSIBLE)).contents
        info["valid_bits"] = ext.wValidBitsPerSample
        info["channel_mask"] = ext.dwChannelMask
        sub = repr(ext.SubFormat).upper()
        if KSDATAFORMAT_SUBTYPE_IEEE_FLOAT.strip("{}") in sub:
            info["sample_format"] = "f32"
            info["format_tag"] = WAVE_FORMAT_IEEE_FLOAT
        else:
            info["sample_format"] = "s16" if base.wBitsPerSample == 16 else "s%d" % base.wBitsPerSample
    else:
        if base.wFormatTag == WAVE_FORMAT_IEEE_FLOAT:
            info["sample_format"] = "f32"
        elif base.wFormatTag == WAVE_FORMAT_PCM:
            info["sample_format"] = {8: "u8", 16: "s16", 24: "s24", 32: "s32"}.get(
                base.wBitsPerSample, "s16"
            )
    return info


def make_format(sample_rate: int, channels: int, bits: int = 32, float_samples: bool = True) -> tuple[bytes, dict]:
    """Build a WAVEFORMATEXTENSIBLE block; returns (bytes, info)."""
    block_align = channels * bits // 8
    wfx = WAVEFORMATEXTENSIBLE()
    wfx.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE
    wfx.Format.nChannels = channels
    wfx.Format.nSamplesPerSec = sample_rate
    wfx.Format.wBitsPerSample = bits
    wfx.Format.nBlockAlign = block_align
    wfx.Format.nAvgBytesPerSec = sample_rate * block_align
    wfx.Format.cbSize = ctypes.sizeof(WAVEFORMATEXTENSIBLE) - ctypes.sizeof(WAVEFORMATEX)
    wfx.wValidBitsPerSample = bits
    wfx.dwChannelMask = 0x3 if channels == 2 else (0x4 if channels == 1 else 0)
    wfx.SubFormat = guid(KSDATAFORMAT_SUBTYPE_IEEE_FLOAT if float_samples else KSDATAFORMAT_SUBTYPE_PCM)
    info = {
        "format_tag": WAVE_FORMAT_EXTENSIBLE,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits": bits,
        "block_align": block_align,
        "avg_bytes": sample_rate * block_align,
        "cbSize": wfx.Format.cbSize,
        "extensible": True,
        "sample_format": "f32" if float_samples else ("s16" if bits == 16 else "s%d" % bits),
        "channel_mask": wfx.dwChannelMask,
        "valid_bits": bits,
    }
    return ctypes.string_at(ctypes.byref(wfx), ctypes.sizeof(wfx)), info


def free_format(fmt_ptr: int) -> None:
    if fmt_ptr:
        ole32.CoTaskMemFree(ctypes.c_void_p(fmt_ptr))


def make_waveformatex(sample_rate: int, channels: int = 2, bits: int = 16) -> tuple[bytes, dict]:
    """普通 WAVEFORMATEX（进程回环的官方示例就是用这个，cbSize=0）。"""
    block_align = channels * bits // 8
    wfx = WAVEFORMATEX()
    wfx.wFormatTag = WAVE_FORMAT_PCM
    wfx.nChannels = channels
    wfx.nSamplesPerSec = sample_rate
    wfx.wBitsPerSample = bits
    wfx.nBlockAlign = block_align
    wfx.nAvgBytesPerSec = sample_rate * block_align
    wfx.cbSize = 0
    info = {
        "format_tag": WAVE_FORMAT_PCM,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits": bits,
        "block_align": block_align,
        "avg_bytes": wfx.nAvgBytesPerSec,
        "cbSize": 0,
        "extensible": False,
        "sample_format": "s16" if bits == 16 else "s%d" % bits,
        "channel_mask": 0,
        "valid_bits": bits,
    }
    return ctypes.string_at(ctypes.byref(wfx), ctypes.sizeof(wfx)), info


# --------------------------------------------------------------------------
# ole32 exports
# --------------------------------------------------------------------------

from ctypes import wintypes  # noqa: E402  (after the structure definitions above)

ole32 = ctypes.WinDLL("ole32")
ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoInitialize.restype = HRESULT
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, c_void_p, DWORD, ctypes.c_void_p, ctypes.c_void_p]
ole32.CoCreateInstance.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, DWORD]
ole32.CoInitializeEx.restype = HRESULT

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 0x17

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"
IID_IAudioRenderClient = "{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
PKEY_Device_FriendlyName = "{A45C254E-DF1C-4EFD-8020-67D146A850E0},14"
PKEY_Device_DeviceDesc = "{A45C254E-DF1C-4EFD-8020-67D146A850E0},2"


def create_device_enumerator() -> IMMDeviceEnumerator:
    ptr = c_void_p()
    hr = ole32.CoCreateInstance(
        guid_ref(CLSID_MMDeviceEnumerator),
        None,
        DWORD(CLSCTX_ALL),
        guid_ref(IID_IMMDeviceEnumerator),
        byref(ptr),
    )
    if failed(hr):
        raise win_error("CoCreateInstance(MMDeviceEnumerator)", hr)
    return IMMDeviceEnumerator.from_ptr(ptr.value)


class PropVariant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("data", ctypes.c_byte * 16),
    ]


def propvariant_str(pv: PropVariant) -> str | None:
    VT_LPWSTR = 31
    if pv.vt != VT_LPWSTR:
        return None
    ptr = ctypes.cast(ctypes.byref(pv.data), POINTER(c_void_p)).contents.value
    if not ptr:
        return None
    return ctypes.wstring_at(ptr)


def device_friendly_name(dev: IMMDevice) -> str:
    store = dev.open_property_store()
    try:
        pv = PropVariant()
        key = guid(PKEY_Device_FriendlyName.split(",")[0])
        pid = DWORD(int(PKEY_Device_FriendlyName.split(",")[1]))
        PKSTRUCT = ctypes.Structure  # PROPERTYKEY = GUID + DWORD
        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", DWORD)]

        pk = PROPERTYKEY()
        pk.fmtid = key
        pk.pid = pid
        hr = store.call("GetValue", byref(pk), byref(pv))
        if failed(hr):
            return ""
        name = propvariant_str(pv)
        # The returned string is allocated by the property store (same task allocator).
        return name or ""
    finally:
        store.release()

# --------------------------------------------------------------------------
# Audio sessions: which process is currently making sound
# --------------------------------------------------------------------------

IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionEnumerator = "{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"

# AudioSessionState: 0=Inactive 1=Active 2=Expired
AUDIO_SESSION_STATE_ACTIVE = 1


class IAudioSessionEnumerator(ComPtr):
    _method_names = ("GetCount", "GetSession")
    _proto_GetCount = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_int))
    _proto_GetSession = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_int, c_void_p)


class IAudioSessionControl(ComPtr):
    _method_names = (
        "GetState", "GetDisplayName", "SetDisplayName", "GetIconPath", "SetIconPath",
        "GetGroupingParam", "SetGroupingParam", "RegisterAudioSessionNotification",
        "UnregisterAudioSessionNotification",
    )
    _proto_GetState = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_int))


class IAudioSessionControl2(IAudioSessionControl):
    # 继承来的 _initialized=True 会让 _bind() 跳过本类自己的槽位表，必须显式重置。
    _initialized = False
    _method_names = IAudioSessionControl._method_names + (
        "GetSessionIdentifier", "GetSessionInstanceIdentifier", "GetProcessId",
        "IsSystemSoundsSession", "SetDuckingPreference",
    )
    _proto_GetProcessId = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(DWORD))
    _proto_IsSystemSoundsSession = ctypes.WINFUNCTYPE(HRESULT, c_void_p)


IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"


class ISimpleAudioVolume(ComPtr):
    # ★★ 2026-11-16 致命 bug 修复：vtable 顺序必须是 IDL 里的声明顺序
    #    SetMasterVolume -> GetMasterVolume -> SetMute -> GetMute。
    #    2.40 按文档"方法列表"的字母序写成了 GetMasterVolume -> SetMasterVolume
    #    -> GetMute -> SetMute，于是 call("GetMute") 实际调到 **SetMute**，
    #    每枚举一个 App 的会话就把它静音 —— 面板一打开（/api/multiout 轮询）
    #    全系统 App 就都没声了。判断"是否静音"只能读，绝不能相信字母序。
    _method_names = ("SetMasterVolume", "GetMasterVolume", "SetMute", "GetMute")
    _proto_GetMasterVolume = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_float))
    _proto_GetMute = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_int))
    _proto_SetMute = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_int, c_void_p)


class IAudioSessionManager2(ComPtr):
    _method_names = (
        "GetAudioSessionControl", "GetSimpleAudioVolume", "GetSessionEnumerator",
        "RegisterSessionNotification", "UnregisterSessionNotification",
        "RegisterDuckNotification", "UnregisterDuckNotification",
    )
    _proto_GetSessionEnumerator = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)


def _sessions_on_endpoint(dev: IMMDevice) -> list[int]:
    """这块播放端点上，正在出声的会话所属 PID（去重）。"""
    pids: list[int] = []
    mgr = IAudioSessionManager2.from_ptr(dev.activate(IID_IAudioSessionManager2, CLSCTX_ALL))
    try:
        enum_ptr = c_void_p()
        hr = mgr.call("GetSessionEnumerator", byref(enum_ptr))
        if failed(hr):
            return pids
        en = IAudioSessionEnumerator.from_ptr(enum_ptr.value)
        try:
            count = ctypes.c_int()
            hr = en.call("GetCount", byref(count))
            if failed(hr):
                return pids
            for index in range(max(0, count.value)):
                ctl_ptr = c_void_p()
                hr = en.call("GetSession", index, byref(ctl_ptr))
                if failed(hr) or not ctl_ptr.value:
                    continue
                ctl = IAudioSessionControl.from_ptr(ctl_ptr.value)
                try:
                    state = ctypes.c_int()
                    if failed(ctl.call("GetState", byref(state))):
                        continue
                    if state.value != AUDIO_SESSION_STATE_ACTIVE:
                        continue
                    # 静音会话不算"正在出声"：用户把某个 App 静音了，列表里不该还挂着。
                    try:
                        volume = ctl.query(IID_ISimpleAudioVolume, ISimpleAudioVolume)
                    except OSError:
                        volume = None
                    if volume is not None:
                        try:
                            mute = ctypes.c_int()
                            if not failed(volume.call("GetMute", byref(mute))) and mute.value:
                                continue
                        finally:
                            volume.release()
                    try:
                        ctl2 = ctl.query(IID_IAudioSessionControl2, IAudioSessionControl2)
                    except OSError:
                        continue
                    try:
                        # S_OK = 系统提示音，没有对应的 App，不列。
                        if ctl2.call("IsSystemSoundsSession") == S_OK:
                            continue
                        pid = DWORD()
                        if failed(ctl2.call("GetProcessId", byref(pid))):
                            continue
                        value = int(pid.value)
                        if value in (0, 4) or value in pids:
                            continue
                        pids.append(value)
                    finally:
                        ctl2.release()
                finally:
                    ctl.release()
        finally:
            en.release()
    finally:
        mgr.release()
    return pids


def active_audio_pids() -> list[int]:
    """所有活动播放端点上正在出声的 PID（合并去重）。

    只看 ACTIVE 会话：暂停/已结束的 App 不该出现在"正在出声"列表里。
    """
    pids: list[int] = []
    enum = create_device_enumerator()
    try:
        coll = enum.enum_audio_endpoints(0, 0x1)   # eRender, DEVICE_STATE_ACTIVE
        try:
            for i in range(coll.count()):
                dev = coll.item(i)
                try:
                    for pid in _sessions_on_endpoint(dev):
                        if pid not in pids:
                            pids.append(pid)
                finally:
                    dev.release()
        finally:
            coll.release()
    finally:
        enum.release()
    return pids


def _iter_session_controls():
    """遍历所有活动播放端点上的音频会话，逐个 yield ``(ctl, pid)``。

    ★ 2026-11-16：2.40 静音 bug 的恢复与回归都要"按会话读/写静音状态"，
      这里只做遍历，不碰任何状态；用生成器是为了让调用方在 yield 期间
      还能继续用这个 ctl（query ISimpleAudioVolume），容器生命周期由栈持有。
    """
    enum = create_device_enumerator()
    try:
        coll = enum.enum_audio_endpoints(0, 0x1)   # eRender, DEVICE_STATE_ACTIVE
        try:
            for i in range(coll.count()):
                dev = coll.item(i)
                mgr = None
                en = None
                try:
                    mgr = IAudioSessionManager2.from_ptr(dev.activate(IID_IAudioSessionManager2, CLSCTX_ALL))
                    enum_ptr = c_void_p()
                    if failed(mgr.call("GetSessionEnumerator", byref(enum_ptr))):
                        continue
                    en = IAudioSessionEnumerator.from_ptr(enum_ptr.value)
                    count = ctypes.c_int()
                    if failed(en.call("GetCount", byref(count))):
                        continue
                    for index in range(max(0, count.value)):
                        ctl_ptr = c_void_p()
                        if failed(en.call("GetSession", index, byref(ctl_ptr))) or not ctl_ptr.value:
                            continue
                        ctl = IAudioSessionControl.from_ptr(ctl_ptr.value)
                        pid = 0
                        try:
                            ctl2 = ctl.query(IID_IAudioSessionControl2, IAudioSessionControl2)
                        except OSError:
                            ctl2 = None
                        if ctl2 is not None:
                            try:
                                value = DWORD()
                                if not failed(ctl2.call("GetProcessId", byref(value))):
                                    pid = int(value.value)
                            finally:
                                ctl2.release()
                        try:
                            yield ctl, pid
                        finally:
                            ctl.release()
                finally:
                    if en is not None:
                        en.release()
                    if mgr is not None:
                        mgr.release()
                    dev.release()
        finally:
            coll.release()
    finally:
        enum.release()


def session_mute_states() -> list:
    """所有活动播放端点上会话的静音快照：``[{"pid", "state", "muted"}]``。

    只读，绝不写。回归用例用它证明"面板轮询前后会话静音状态没变"。
    """
    rows = []
    for ctl, pid in _iter_session_controls():
        state = ctypes.c_int()
        if failed(ctl.call("GetState", byref(state))):
            continue
        if state.value != AUDIO_SESSION_STATE_ACTIVE:
            continue
        try:
            volume = ctl.query(IID_ISimpleAudioVolume, ISimpleAudioVolume)
        except OSError:
            continue
        try:
            mute = ctypes.c_int(-1)
            if failed(volume.call("GetMute", byref(mute))):
                continue
            rows.append({"pid": pid, "state": int(state.value), "muted": bool(mute.value)})
        finally:
            volume.release()
    return rows


def set_all_sessions_mute(muted: bool) -> dict:
    """把所有活动播放端点上的会话统一静音 / 取消静音。

    ★ 2026-11-16：2.40 每个 App 的静音状态被 Windows 记进音频策略，
      **不会随进程退出自动恢复**；用户之前被静音的 App 只能靠这里显式
      SetMute(FALSE) 恢复（一键恢复工具 tools/unmute_apps.py 走的就是它）。
    返回 ``{"changed": 实际改动数, "total": 扫到的会话数}``。
    """
    changed = 0
    total = 0
    for ctl, _pid in _iter_session_controls():
        try:
            volume = ctl.query(IID_ISimpleAudioVolume, ISimpleAudioVolume)
        except OSError:
            continue
        try:
            total += 1
            current = ctypes.c_int(-1)
            if failed(volume.call("GetMute", byref(current))):
                continue
            if bool(current.value) == bool(muted):
                continue
            if not failed(volume.call("SetMute", ctypes.c_int(1 if muted else 0), None)):
                changed += 1
        finally:
            volume.release()
    return {"changed": changed, "total": total}


def process_image_name(pid: int) -> str:
    """PID -> 进程可执行文件名（取不到返回空串）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [DWORD, ctypes.c_int, DWORD]
    kernel32.OpenProcess.restype = c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [c_void_p, DWORD, ctypes.c_wchar_p, POINTER(DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [c_void_p]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, DWORD(int(pid)))
    if not handle:
        return ""
    try:
        size = DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)):
            return ""
        return buf.value or ""
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# Per-process loopback (Windows 10 2004+)
# --------------------------------------------------------------------------

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
VT_BLOB = 65

IID_IActivateAudioInterfaceAsyncOperation = "{72A22D78-CDE4-431D-B8CC-843A71199B6D}"


class _AudioClientProcessLoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class _AudioClientActivationParams(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("ProcessLoopbackParams", _AudioClientProcessLoopbackParams),
    ]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", ULONG), ("pBlobData", POINTER(ctypes.c_ubyte))]


class _PropVariantBlob(ctypes.Structure):
    # PROPVARIANT on x64 is 24 bytes: 4x USHORT header + BLOB (ULONG + pointer).
    _fields_ = [
        ("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
        ("blob", _Blob),
    ]


class IActivateAudioInterfaceAsyncOperation(ComPtr):
    _method_names = ("GetActivateResult",)
    _proto_GetActivateResult = ctypes.WINFUNCTYPE(
        HRESULT, c_void_p, POINTER(HRESULT), POINTER(c_void_p)
    )


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", c_void_p), ("AddRef", c_void_p),
        ("Release", c_void_p), ("ActivateCompleted", c_void_p),
    ]


class _HandlerStruct(ctypes.Structure):
    _fields_ = [
        ("vtbl", POINTER(_HandlerVtbl)),
        ("refs", ctypes.c_long),
        ("event", c_void_p),
        ("operation", c_void_p),
        ("completed", ctypes.c_int),
    ]


_QI_PROTO = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
_ADDREF_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_RELEASE_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
_COMPLETED_PROTO = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)

_ACTIVATION_HANDLERS: dict[int, _HandlerStruct] = {}


@_QI_PROTO
def _handler_query_interface(this, riid, ppv):
    if ppv:
        ctypes.cast(ppv, POINTER(c_void_p))[0] = c_void_p(this)
    handler = _ACTIVATION_HANDLERS.get(int(this))
    if handler is not None:
        handler.refs += 1
    return S_OK


@_ADDREF_PROTO
def _handler_add_ref(this):
    handler = _ACTIVATION_HANDLERS.get(int(this))
    if handler is not None:
        handler.refs += 1
        return max(1, handler.refs)
    return 1


@_RELEASE_PROTO
def _handler_release(this):
    handler = _ACTIVATION_HANDLERS.get(int(this))
    if handler is not None:
        handler.refs -= 1
        return max(0, handler.refs)
    return 0


@_COMPLETED_PROTO
def _handler_completed(this, operation):
    handler = _ACTIVATION_HANDLERS.get(int(this))
    if handler is not None:
        handler.operation = operation
        handler.completed = 1
        try:
            _KERNEL32.SetEvent(handler.event)
        except Exception:
            pass
    return S_OK


_HANDLER_VTBL = _HandlerVtbl(
    ctypes.cast(_handler_query_interface, c_void_p),
    ctypes.cast(_handler_add_ref, c_void_p),
    ctypes.cast(_handler_release, c_void_p),
    ctypes.cast(_handler_completed, c_void_p),
)


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
_KERNEL32.CreateEventW.restype = c_void_p
_KERNEL32.SetEvent.argtypes = [c_void_p]
_KERNEL32.SetEvent.restype = ctypes.c_int
_KERNEL32.WaitForSingleObject.argtypes = [c_void_p, DWORD]
_KERNEL32.WaitForSingleObject.restype = DWORD
_KERNEL32.CloseHandle.argtypes = [c_void_p]

_MMDEVAPI = ctypes.WinDLL("mmdevapi")
_MMDEVAPI.ActivateAudioInterfaceAsync.argtypes = [
    LPCWSTR, c_void_p, c_void_p, c_void_p, POINTER(c_void_p)
]
_MMDEVAPI.ActivateAudioInterfaceAsync.restype = HRESULT


def process_loopback_available() -> tuple[bool, str]:
    """这台系统能不能做"按进程回环"。"""
    try:
        import sys as _sys
        build = int(_sys.getwindowsversion().build)
    except Exception:
        build = 0
    if build and build < 19041:
        return False, "系统版本太旧（需要 Windows 10 2004 及以上），只能用桌面混音"
    return True, ""


def activate_process_loopback(pid: int, include_tree: bool = True,
                              timeout_ms: int = 5000) -> IAudioClient:
    """拿到一个按进程采集的 IAudioClient（还没 Initialize）。"""
    params = _AudioClientActivationParams()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = DWORD(int(pid))
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE if include_tree
        else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
    )
    raw = ctypes.string_at(ctypes.byref(params), ctypes.sizeof(params))
    blob_buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    pv = _PropVariantBlob()
    pv.vt = VT_BLOB
    pv.blob.cbSize = len(raw)
    pv.blob.pBlobData = ctypes.cast(blob_buf, POINTER(ctypes.c_ubyte))

    handler = _HandlerStruct()
    handler.vtbl = ctypes.pointer(_HANDLER_VTBL)
    handler.refs = 1
    handler.event = _KERNEL32.CreateEventW(None, 1, 0, None)
    if not handler.event:
        raise OSError("按 App 采音：创建等待事件失败")
    address = ctypes.addressof(handler)
    _ACTIVATION_HANDLERS[address] = handler

    operation = c_void_p()
    try:
        hr = _MMDEVAPI.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            ctypes.cast(guid_ref(IID_IAudioClient), c_void_p),
            ctypes.byref(pv),
            c_void_p(address),
            byref(operation),
        )
        if failed(hr):
            raise win_error("ActivateAudioInterfaceAsync", hr)
        if _KERNEL32.WaitForSingleObject(handler.event, int(timeout_ms)) != 0:
            raise OSError("按 App 采音启动超时（这个 App 可能没有在放声音）")
        if not handler.operation:
            raise OSError("按 App 采音没有返回操作对象")
        async_op = IActivateAudioInterfaceAsyncOperation.from_ptr(handler.operation)
        try:
            result = HRESULT(0)
            iface = c_void_p()
            hr = async_op.call("GetActivateResult", byref(result), byref(iface))
            if failed(hr):
                raise win_error("GetActivateResult", hr)
            if failed(result.value):
                raise win_error("按 App 采音", result.value)
            if not iface.value:
                raise OSError("按 App 采音没有返回音频接口")
            return IAudioClient.from_ptr(iface.value)
        finally:
            async_op.release()
    finally:
        _ACTIVATION_HANDLERS.pop(address, None)
        try:
            _KERNEL32.CloseHandle(handler.event)
        except Exception:
            pass
