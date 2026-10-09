"""Minimal COM plumbing for the WASAPI backend (ctypes only, no dependencies).

Only what WaveLink needs: IUnknown reference counting, vtable slot lookup,
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
