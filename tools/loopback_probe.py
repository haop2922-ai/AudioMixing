"""Real-hardware check: play a tone to the default output and capture it back
through WASAPI loopback, then verify the measured frequency.

Run:  python tools/loopback_probe.py [seconds] [device-substring]
      python tools/loopback_probe.py --scan [seconds-per-device]

``--scan`` answers a different question: *which playback endpoints currently
have sound on them?* A machine with virtual devices (remote-desktop audio,
streaming shims) often has its Windows default pointed at one of those, so
capturing "@default" yields silence while everything still reports success.
"""

import math
import sys
import threading
import time

sys.path.insert(0, r"E:\AI\deep blue\wavelink")

import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

# Verification scripts must never read or write the user's real config: they start
# real nodes, and a stray run once saved test ports into %APPDATA%\WaveLink.
# The sandbox is removed again on exit so a test run leaves nothing behind.
if "WAVELINK_HOME" not in _os.environ:
    _sandbox = _tempfile.mkdtemp(prefix="wavelink-check-")
    _os.environ["WAVELINK_HOME"] = _sandbox
    _atexit.register(lambda: _shutil.rmtree(_sandbox, ignore_errors=True))
from wavelink.audio.base import open_sink, open_source, write_paced
from wavelink.audio.format import CHANNELS, SAMPLE_RATE, make_tone, measure_frequency, peak, rms, signal_report

SCAN = "--scan" in sys.argv
_POSITIONAL = [a for a in sys.argv[1:] if not a.startswith("--")]
SECONDS = float(_POSITIONAL[0]) if _POSITIONAL else 3.0
DEVICE = _POSITIONAL[1] if len(_POSITIONAL) > 1 else "@default"
FREQ = 1000.0

captured = []


def capture_worker(src, stop_evt):
    src.start()
    while not stop_evt.is_set():
        block = src.read()
        if block:
            captured.extend(block)
    src.stop()


def main() -> int:
    src = open_source({"backend": "wasapi", "device": DEVICE, "loopback": True})
    sink = open_sink({"backend": "wasapi", "device": DEVICE})

    stop_evt = threading.Event()
    t = threading.Thread(target=capture_worker, args=(src, stop_evt), daemon=True)
    t.start()
    time.sleep(0.3)  # let the loopback client spin up

    sink.start()
    frames_per_block = 480
    total_frames = int(SECONDS * SAMPLE_RATE)
    tone = make_tone(FREQ, total_frames, 0.35, CHANNELS)
    # Paced: a sink accepts a whole block immediately and buffers the rest, so an
    # unpaced loop would hand it the entire tone at once and lose it to the backlog.
    written = write_paced(sink, tone, frames_per_block=frames_per_block)
    time.sleep(0.4)
    stop_evt.set()
    time.sleep(0.2)
    sink.stop()
    t.join(timeout=2.0)

    print("played %.2f s, captured %.2f s (%d frames)" % (written / SAMPLE_RATE, len(captured) / CHANNELS / SAMPLE_RATE, len(captured) // CHANNELS))
    print("source:", src.describe())
    print("sink  :", sink.describe())
    if not captured:
        print("RESULT: FAIL - no audio captured")
        return 1
    report = signal_report(captured)
    print("capture report: %s" % report)
    measured = report["frequency"]
    print("measured frequency %.2f Hz (played %.2f Hz)" % (measured, FREQ))
    ok = abs(measured - FREQ) < 40 and report["rms"] > 0.02
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _print_role_defaults() -> None:
    """Windows keeps three separate "default" endpoints; they can disagree.

    A remote-desktop or streaming tool often grabs eConsole while media apps
    keep playing to eMultimedia. Reading only one of them is how a program ends
    up capturing (or rendering to) a device that never carries the real sound.
    """
    try:
        from wavelink.audio.backends import wasapi as W
        from wavelink.audio.backends import wasapi_com as wc

        W._ensure_com()
        enum = wc.create_device_enumerator()
    except Exception as exc:
        print("  （默认设备角色读取失败: %s）" % exc)
        return
    labels = ((0, "eConsole 控制台"), (1, "eMultimedia 多媒体"), (2, "eCommunications 通信"))
    for role, label in labels:
        try:
            dev = enum.get_default_audio_endpoint(0, role)  # 0 = eRender
            print("  默认播放设备[%-22s] = %s" % (label, wc.device_friendly_name(dev)))
        except Exception as exc:
            print("  默认播放设备[%-22s] 读取失败: %s" % (label, exc))
    try:
        dev = enum.get_default_audio_endpoint(1, 0)  # 1 = eCapture
        print("  默认录音设备[%-22s] = %s" % ("eConsole", wc.device_friendly_name(dev)))
    except Exception as exc:
        print("  默认录音设备读取失败: %s" % exc)


def scan(seconds: float) -> int:
    """Report the live signal level of every loopback (playback) endpoint."""
    import numpy as np

    from wavelink.audio import base as audio_base

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("WaveLink 声卡活动扫描（每个设备 %.1f 秒）" % seconds)
    _print_role_defaults()
    print("")
    print("  %-4s %-6s %8s %8s  设备" % ("默认", "虚拟", "峰值", "有效值"))

    loopbacks = [d for d in audio_base.list_all_devices()
                 if d.kind == audio_base.KIND_SOURCE and (d.kind_detail or "") == "loopback"]
    if not loopbacks:
        print("  没有找到可环回采集的播放设备")
        return 1

    from wavelink.engine import WaveLinkNode

    virtual_hints = WaveLinkNode._VIRTUAL_HINTS
    live = []
    for dev in loopbacks:
        try:
            src = open_source({"backend": "wasapi", "device": dev.id, "loopback": True})
        except Exception as exc:
            print("  %-4s %-6s %8s %8s  %s  (打不开: %s)"
                  % ("-", "-", "-", "-", dev.name, exc))
            continue
        samples = []
        stop_evt = threading.Event()
        worker = threading.Thread(target=capture_worker, args=(src, stop_evt), daemon=True)
        worker.start()
        time.sleep(seconds)
        stop_evt.set()
        time.sleep(0.15)
        worker.join(timeout=2.0)
        samples = captured[:]
        del captured[:]
        try:
            src.stop()
        except Exception:
            pass

        if not samples:
            print("  %-4s %-6s %8s %8s  %s  (没采到数据)"
                  % ("是" if dev.is_default else "-", "是" if _is_virtual(dev.name, virtual_hints) else "-",
                     "-", "-", dev.name))
            continue
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        pk = float(np.max(np.abs(arr))) if arr.size else 0.0
        rms_v = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        if rms_v > 0.001:
            live.append(dev.name)
        print("  %-4s %-6s %8.4f %8.4f  %s"
              % ("是" if dev.is_default else "-", "是" if _is_virtual(dev.name, virtual_hints) else "-",
                 pk, rms_v, dev.name))

    print("")
    if live:
        print("检测到有声音的设备: %s" % "; ".join(live))
    else:
        print("所有播放设备都是静音的 —— 现在放点音乐/视频再扫一次，")
        print("就能知道你的声音实际走的是哪块声卡（把那个名字填进 source_device）。")
    return 0


def _is_virtual(name: str, hints) -> bool:
    low = (name or "").lower()
    return any(h in low for h in hints)


if __name__ == "__main__":
    if SCAN:
        raise SystemExit(scan(SECONDS))
    raise SystemExit(main())
