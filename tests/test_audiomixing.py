"""Audio Mixing test suite (stdlib unittest, no extra dependencies).

    python -m unittest discover -s tests -v
    python tests/test_audiomixing.py
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import math
import os
import pathlib
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from array import array
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The tests build real AudioMixingNode objects, and `set_routes()` persists by default,
# so without this they wrote test ports and a test node name ("node-self"/"本机")
# into the user's %APPDATA%\AudioMixing\config.json. Sandbox it, and clean up after.
#
# ★ 所有测试用临时目录统一走 _mkdtemp()：以前各处直接 tempfile.mkdtemp()，
#   每跑一轮就在 %TEMP% 里留一堆 am-blocked- / am-ports- / am-mig- 目录。
_SANDBOXES: list[pathlib.Path] = []


def _mkdtemp(prefix: str = "") -> pathlib.Path:
    """建一个测试沙箱，并登记到退出时统一删除。"""
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    _SANDBOXES.append(path)
    return path


def _cleanup_sandboxes() -> None:
    for path in _SANDBOXES:
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup_sandboxes)
_TEST_SANDBOX = _mkdtemp(prefix="audiomixing-tests-")
os.environ["AUDIOMIXING_HOME"] = str(_TEST_SANDBOX)

import numpy as np  # noqa: E402

from audiomixing import proto  # noqa: E402
from audiomixing.config import Config  # noqa: E402
from audiomixing.audio import base as audio_base  # noqa: E402
from audiomixing.audio.format import (  # noqa: E402
    CHANNELS,
    SAMPLE_RATE,
    Resampler,
    channel_map,
    correlation,
    make_tone,
    measure_frequency,
    rms,
    saturating_mix_into,
    signal_report,
)
from audiomixing.engine import SinkEngine, AudioMixingNode  # noqa: E402

# ★ 2026-11-02：单元测试不能去"拉取对端路由表"。
#   测试节点会对自己加的手动对端发 HTTP /api/routes；如果用户此时正开着真实
#   Audio Mixing（监听 17800），测试就会把用户真实路由拉进沙箱，出现
#   "点对点发送端凭空消失"这种假失败。隔离掉这条网络路径。
AudioMixingNode._pull_peer_routes = lambda self: None  # noqa: E731
from audiomixing.net.discovery import BeaconService, DiscoveryRegistry, build_beacon  # noqa: E402
from audiomixing.net.rtpaudio import JitterBuffer, Receiver, SenderSession, StreamInfo, stable_ssrc  # noqa: E402

BLOCK = 480


class ToneSource:
    """Deterministic in-memory source: `blocks` packets of a sine, paced in time."""

    name = "test-tone"
    sample_rate = SAMPLE_RATE
    channels = CHANNELS

    # HWHHYN 用 DEEPSEEK 制作
    def __init__(self, freq: float = 1000.0, blocks: int = 200, pace: float = 0.0, gain: float = 0.35) -> None:
        self.freq = freq
        self.blocks = blocks
        self.pace = pace
        self.gain = gain
        self.i = 0
        self.eof = False

    def start(self) -> None:
        pass

    def read(self):
        if self.i >= self.blocks:
            self.eof = True
            return []
        self.i += 1
        if self.pace:
            time.sleep(self.pace)
        return make_tone(self.freq, BLOCK, self.gain, CHANNELS)

    def stop(self) -> None:
        pass


def free_udp_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------

class TestProtocol(unittest.TestCase):
    def test_header_layout_is_stable(self):
        self.assertEqual(proto.HEADER_SIZE, 28)
        self.assertEqual(proto.HEADER.format, "<HBBBBHIIQhH")

    def test_round_trip(self):
        pkt = proto.Packet(
            type=proto.T_AUDIO,
            payload=b"\x00\x01\x02\x03",
            codec=proto.CODEC_PCM_S16,
            seq=4321,
            ssrc=0xDEADBEEF & 0x7FFFFFFF,
            samples=960,
            ts_ns=1_700_000_000_000_000_000,
            gain_q14=7500,
        )
        out = proto.decode(pkt.encode())
        assert out is not None
        self.assertEqual(out.type, proto.T_AUDIO)
        self.assertEqual(out.seq, 4321)
        self.assertEqual(out.ssrc, 0xDEADBEEF & 0x7FFFFFFF)
        self.assertEqual(out.samples, 960)
        self.assertEqual(out.ts_ns, 1_700_000_000_000_000_000)
        self.assertEqual(out.gain_q14, 7500)
        self.assertAlmostEqual(out.gain, 0.75)
        self.assertEqual(out.payload, b"\x00\x01\x02\x03")

    def test_rejects_garbage(self):
        self.assertIsNone(proto.decode(b""))
        self.assertIsNone(proto.decode(b"hello world, not an audiomixing packet"))
        self.assertIsNone(proto.decode(b"WL\x01\x01\x00\x00\x00\x00"))
        good = proto.Packet(type=proto.T_AUDIO, payload=b"x" * 10).encode()
        truncated = good[:-5]
        self.assertIsNone(proto.decode(truncated))

    def test_sequence_wraparound(self):
        for seq in (0, 1, 0xFFFF, 0x8000):
            decoded = proto.decode(proto.Packet(type=proto.T_AUDIO, seq=seq).encode())
            assert decoded is not None
            self.assertEqual(decoded.seq, seq)

    def test_pcm_codecs_round_trip(self):
        samples = make_tone(1000.0, BLOCK, 0.5, CHANNELS)
        f32 = proto.decode_payload(proto.CODEC_PCM_F32, proto.encode_payload(proto.CODEC_PCM_F32, samples))
        self.assertLess(max(abs(a - b) for a, b in zip(samples, f32)), 1e-6)
        s16 = proto.decode_payload(proto.CODEC_PCM_S16, proto.encode_payload(proto.CODEC_PCM_S16, samples))
        self.assertLess(max(abs(a - b) for a, b in zip(samples, s16)), 1.0 / 32767 + 1e-6)

    def test_clock_sync_recovers_offset(self):
        # Remote clock is 5 s ahead; 2 ms one-way delay.
        remote_offset = 5_000_000_000
        t0 = proto.now_ns()
        ping = proto.Packet.control(proto.T_PING, proto.ping_body(t0))
        t1 = t0 + remote_offset + 2_000_000
        t2 = t1 + 300_000
        pong = proto.Packet.control(proto.T_PONG, proto.pong_body(ping, t1, t2))
        t3 = t2 - remote_offset + 2_000_000
        sync = proto.ClockSync("remote")
        self.assertTrue(sync.update(pong, t3))
        self.assertAlmostEqual(sync.offset_ns, remote_offset, delta=200_000)
        self.assertAlmostEqual(sync.rtt_ns, 4_000_000, delta=300_000)
        # Mapping a remote timestamp back to our clock must cancel the offset.
        self.assertAlmostEqual(sync.to_local_ns(t0 + remote_offset), t0, delta=200_000)

    def test_clock_sync_ignores_broken_pongs(self):
        sync = proto.ClockSync("x")
        self.assertFalse(sync.update(proto.Packet.control(proto.T_PONG, {"t0": 1}), proto.now_ns()))


# --------------------------------------------------------------------------
# audio format helpers
# --------------------------------------------------------------------------

class TestAudioFormat(unittest.TestCase):
    def test_tone_and_measure(self):
        tone = make_tone(1000.0, SAMPLE_RATE, 0.5, CHANNELS)
        report = signal_report(tone)
        self.assertAlmostEqual(report["peak"], 0.5, places=2)
        self.assertAlmostEqual(report["frequency"], 1000.0, delta=2.0)

    def test_measure_frequency_ignores_leading_silence(self):
        silence = array("f", bytes(4 * SAMPLE_RATE * CHANNELS))
        tone = make_tone(440.0, SAMPLE_RATE, 0.4, CHANNELS)
        combined = silence + tone
        self.assertAlmostEqual(measure_frequency(combined), 440.0, delta=2.0)

    def test_channel_map(self):
        stereo = make_tone(500.0, 240, 0.3, 2)
        mono = channel_map(stereo, 2, 1)
        self.assertEqual(len(mono), 240)
        self.assertAlmostEqual(mono[0], (stereo[0] + stereo[1]) / 2, places=6)
        back = channel_map(mono, 1, 2)
        self.assertEqual(len(back), 480)
        self.assertAlmostEqual(back[0], mono[0], places=6)
        self.assertAlmostEqual(back[1], mono[0], places=6)

    def test_saturating_mix(self):
        a = make_tone(1000.0, 480, 0.6, CHANNELS)
        dst = array("f", bytes(4 * len(a)))
        saturating_mix_into(dst, a)
        saturating_mix_into(dst, a)
        self.assertAlmostEqual(max(dst), 1.0, places=6)
        self.assertAlmostEqual(min(dst), -1.0, places=6)

    def test_resampler_matches_direct_sinc(self):
        """The streaming resampler must equal an exact windowed-sinc interpolator.

        The implementation reads its filter taps from a 4096-entry table, so the
        phase is quantised to 1/4096 of a sample; the tolerance below matches that
        (a 1 kHz tone moves ~0.37 in amplitude over that phase step at the steepest
        part of the waveform, near the zero crossing).
        """
        import math

        src = list(make_tone(1000.0, 2000, 0.5, 1))
        rs = Resampler(44100, 48000, 1)
        # `flush()` releases the lookahead, so a whole-signal pass has the same
        # length as the zero-padded reference below.
        got = list(rs.process(src)) + list(rs.flush())
        want = []
        pos = 0.0
        ratio = 44100 / 48000.0
        z = 16
        while True:
            i = int(pos)
            frac = pos - i
            if i >= len(src):
                break
            acc = 0.0
            for k in range(-z, z):
                j = i + k
                if 0 <= j < len(src):
                    x = k - frac
                    sinc = 1.0 if x == 0 else math.sin(math.pi * x) / (math.pi * x)
                    t = (x / z + 1.0) * 0.5
                    window = 0.42 - 0.5 * math.cos(2 * math.pi * t) + 0.08 * math.cos(4 * math.pi * t)
                    acc += src[j] * sinc * window
            want.append(acc)
            pos += ratio
        # `flush()` may emit one sample past the end of the signal (its window centre
        # sits on the last frame), so the lengths agree to within one.
        self.assertLessEqual(abs(len(got) - len(want)), 1)
        got = got[: len(want)]
        # Compare only the steady-state region: the first and last `z` samples are
        # inside the clamping window where the reference visits fewer taps.
        a = got[z : -z]
        b = want[z : -z]
        self.assertLess(max(abs(x - y) for x, y in zip(a, b)), 0.4)
        self.assertGreater(correlation(a, b), 0.9999)

    def test_resampler_block_size_invariance(self):
        src = list(make_tone(700.0, 4410, 0.5, 1))
        one = list(Resampler(44100, 48000, 1).process(src))
        blocked = []
        rs = Resampler(44100, 48000, 1)
        for i in range(0, len(src), 137):  # deliberately awkward block size
            blocked.extend(rs.process(src[i : i + 137]))
        self.assertLess(abs(len(one) - len(blocked)), 3)
        n = min(len(one), len(blocked))
        self.assertGreater(correlation(one[:n], blocked[:n]), 0.995)

    def test_resampler_preserves_duration(self):
        for src_rate in (44100, 22050, 96000):
            src = list(make_tone(1000.0, src_rate, 0.4, 1))
            out = Resampler(src_rate, 48000, 1).process(src)
            self.assertAlmostEqual(len(out) / 48000.0, len(src) / src_rate, delta=0.002)

    def test_resampler_identity_when_rates_match(self):
        src = make_tone(1000.0, 480, 0.5, 2)
        rs = Resampler(48000, 48000, 2)
        self.assertFalse(rs.enabled)
        out = rs.process(src)
        self.assertEqual(list(out), list(src))

    def test_resampler_survives_downsampling_short_blocks(self):
        """48000 -> 44100 (a common Bluetooth output) has ratio > 1.

        The playhead can then land past the end of the block we were handed, which
        used to silently shorten the kept history. The next short block - a render
        device accepts only a few frames at a time - then indexed off the end of
        that history and raised IndexError, killing playback for good. The symptom
        was a sink that kept reporting "IndexError: list index out of range" while
        writing nothing at all.
        """
        r = Resampler(48000, 44100, 2)
        for n in (1, 2, 3, 1, 479, 5, 1, 13):
            r.process([0.05] * (n * 2))
        self.assertGreaterEqual(len(r._in) // 2, r._z, "must always keep z frames of history")

        for _ in range(300):
            r.process([0.2, 0.2])
        self.assertGreaterEqual(len(r._in) // 2, r._z, "must survive a run of 1-frame blocks")
        out = r.process([0.05] * (4800 * 2))
        self.assertAlmostEqual(len(out) / 2, 4800 * (44100 / 48000.0), delta=80)

    def test_resampler_blocks_match_a_single_pass(self):
        """Streaming in awkward blocks must equal one pass over the whole signal.

        This is the invariant that matters for audio quality. Correlation over the
        whole signal is too forgiving - the previous edge handling dropped the
        history for the first z outputs of every block and the newest frame for the
        last z, which put a large error on ~12% of the samples at every block
        boundary (measured max error 0.27 on a 0.5-amplitude tone).
        """
        for src_rate, dst_rate in ((44100, 48000), (48000, 44100), (48000, 32000)):
            tone = list(make_tone(700.0, 4800, 0.5, 1))
            one = list(Resampler(src_rate, dst_rate, 1).process(tone))
            blocked: list[float] = []
            rs = Resampler(src_rate, dst_rate, 1)
            for i in range(0, len(tone), 137):  # deliberately awkward block size
                blocked.extend(rs.process(tone[i : i + 137]))
            n = min(len(one), len(blocked))
            self.assertGreater(n, 3000)
            worst = max(abs(a - b) for a, b in zip(one[:n], blocked[:n]))
            self.assertLess(worst, 0.01, "%d->%d: block edges differ by %.4f" % (src_rate, dst_rate, worst))

    def test_write_paced_plays_in_real_time(self):
        """A sink that accepts everything must still be fed at real time.

        A sink takes ownership of a whole block and buffers what the device has not
        accepted, so its return value cannot pace a caller. Looping on it dumped the
        entire buffer at once and the backlog limit discarded almost all of it - the
        panel's "can I hear it?" test tone was nearly inaudible because of this.
        """
        from audiomixing.audio.base import write_paced

        class _Greedy:
            def __init__(self) -> None:
                self.frames = 0

            def write(self, samples) -> int:
                n = len(samples) // CHANNELS
                self.frames += n
                return n

        sink = _Greedy()
        frames = SAMPLE_RATE // 2  # half a second
        started = time.monotonic()
        written = write_paced(sink, make_tone(1000.0, frames, 0.3, CHANNELS), frames_per_block=480)
        elapsed = time.monotonic() - started
        self.assertEqual(written, frames)
        self.assertEqual(sink.frames, frames)
        self.assertGreater(elapsed, 0.4, "0.5 s of audio must take about 0.5 s to hand over")
        self.assertLess(elapsed, 0.9)

    def test_linear_resampler_is_a_usable_reference(self):
        """`resample_linear` is the simple reference; it must agree with the sinc one.

        Keeps the reference implementation honest instead of leaving it as dead code,
        and catches a regression where the reference and the real resampler drift
        apart (which would make future debugging misleading).
        """
        from audiomixing.audio.format import resample_linear

        frames = 4800
        src = make_tone(500.0, frames, 0.5, 1)
        linear = resample_linear(src, 44100, 48000, 1)
        rs = Resampler(44100, 48000, 1)
        sinc = rs.process(src) + rs.flush()
        # Both must land within one sample of the expected output length.
        self.assertLess(abs(len(linear) - len(sinc)), max(4, frames * 0.001))
        n = min(len(linear), len(sinc))
        self.assertGreater(correlation(list(linear[:n]), list(sinc[:n])), 0.99)
        # A 500 Hz tone must survive both without changing length semantics badly.
        report = signal_report(list(linear))
        self.assertGreater(report["peak"], 0.4)

    def test_best_lag_finds_a_known_delay(self):
        """`best_lag` is what the doctor uses to measure end-to-end latency.

        A **broadband** signal is required: with a pure 1 kHz sine at 48 kHz every
        48th lag correlates perfectly (the wave repeats), so a sine cannot pin down a
        delay at all. This test therefore uses a chirp, and asserts that a sine is
        genuinely ambiguous - documenting the trap rather than tripping over it.
        """
        from audiomixing.audio.format import Chirp, best_lag

        shift = 37
        chirp = Chirp(300.0, 3000.0, seconds=0.4)
        reference = list(chirp.read(4000, 1))
        delayed = [0.0] * shift + reference[: len(reference) - shift]
        lag, score = best_lag(reference, delayed, max_lag=120)
        self.assertAlmostEqual(lag, shift, delta=2, msg="positive lag must mean 'late'")
        self.assertGreater(score, 0.9)

        # No delay reads as zero lag.
        lag0, score0 = best_lag(reference, reference, max_lag=120)
        self.assertEqual(lag0, 0)
        self.assertGreater(score0, 0.99)

        # The documented trap: a sine is periodic, so a lag one period away scores
        # just as well. This is why latency is measured with a chirp.
        sine = list(make_tone(1000.0, 4000, 0.5, 1))
        period = int(SAMPLE_RATE / 1000.0)
        sine_shifted = [0.0] * period + sine[: len(sine) - period]
        _, sine_score = best_lag(sine, sine_shifted, max_lag=120)
        self.assertGreater(sine_score, 0.99, "a shifted sine still correlates (the trap)")

    def test_sinks_accept_numpy_samples(self):
        """The mixer hands numpy arrays to sinks; every backend must accept them.

        `array('f', <ndarray>)` silently iterates rows instead of samples, so this
        is a real trap rather than a theoretical one.
        """
        import tempfile
        import wave

        block = np.asarray(make_tone(440.0, BLOCK, 0.4, CHANNELS), dtype=np.float32).reshape(BLOCK, CHANNELS)

        # null sink
        null_sink = audio_base.open_sink({"backend": "null", "device": "null"})
        null_sink.start()
        self.assertEqual(null_sink.write(block), BLOCK)
        null_sink.stop()

        # wav sink, file on disk (no real-time pacing: we are not on the audio clock)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.wav")
            wav_sink = audio_base.open_sink({"backend": "file", "path": path, "realtime": False})
            wav_sink.start()
            self.assertEqual(wav_sink.write(block), BLOCK)
            wav_sink.write(block)
            wav_sink.stop()
            with wave.open(path, "rb") as wf:
                self.assertEqual(wf.getnframes(), BLOCK * 2)
                self.assertEqual(wf.getnchannels(), CHANNELS)
                raw = wf.readframes(BLOCK)
            self.assertGreater(float(max(abs(float(v)) for v in array("f", raw))), 0.3)

        # wasapi sink, only when real hardware is available
        if audio_base.available_backends().get("wasapi"):
            try:
                sink = audio_base.open_sink({"backend": "wasapi", "device": "@default"})
                sink.start()
                written = sink.write(block)
                sink.stop()
                self.assertGreater(written, 0, "WASAPI sink should accept a numpy block")
            except OSError as exc:  # no output device on this machine
                self.skipTest("no usable WASAPI output: %s" % exc)

    def test_engine_writes_to_a_wav_sink_end_to_end(self):
        """Routing two local sources into a WAV file proves the whole mix chain."""
        import tempfile
        import wave

        from audiomixing.config import Config

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "mix.wav")
            cfg = Config()
            cfg.node_id = "recorder"
            cfg.name = "recorder"
            cfg.routes = []
            cfg.manual_peers = []
            node = AudioMixingNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
            node.default_sink_spec = lambda: {"backend": "file", "path": path, "realtime": True}  # type: ignore[method-assign]
            node.start(announce=False)
            try:
                node.registry.update(
                    {"wl": 1, "id": "sender-x", "name": "sender-x", "audio_port": 1, "web_port": 1},
                    "127.0.0.1",
                )
                node.set_routes([{"id": "rec", "from": ["sender-x"], "to": ["self"], "codec": "pcm_s16"}], persist=False)
                node.apply_routes()
                self.assertIn("rec", node.sinks, "a local sink should have been opened")
                time.sleep(0.5)
            finally:
                node.stop()
            self.assertTrue(os.path.isfile(path), "the WAV sink should have created its file")


# --------------------------------------------------------------------------
# jitter buffer
# --------------------------------------------------------------------------

class TestJitterBuffer(unittest.TestCase):
    def test_priming_then_playback(self):
        buf = JitterBuffer()
        t0 = proto.now_ns()
        played = 0
        for i in range(60):
            buf.write(make_tone(1000.0, BLOCK, 0.4, CHANNELS), t0 + i * 10_000_000, 0.0)
            block = buf.read(BLOCK)
            played += int(np.count_nonzero(block))
            time.sleep(0.01)
        stats = buf.stats()
        self.assertGreater(stats["played"], 480 * 40, "should have played most frames: %s" % stats)
        self.assertLess(stats["overflow"], 480 * 3, "should not overflow: %s" % stats)

    def test_late_packet_does_not_shift_the_timeline(self):
        buf = JitterBuffer()
        t0 = proto.now_ns()
        for i in range(10):
            buf.write(make_tone(1000.0, BLOCK, 0.4, CHANNELS), t0 + i * 10_000_000, 0.0)
        for _ in range(6):
            buf.read(BLOCK)
        before = buf._play_at
        # A packet that is 500 ms late must not move the playhead.
        buf.write(make_tone(1000.0, BLOCK, 0.4, CHANNELS), t0 - 500_000_000, 0.0)
        self.assertEqual(buf._play_at, before)

    def test_restart_resyncs_the_timeline(self):
        buf = JitterBuffer()
        t0 = proto.now_ns()
        for i in range(10):
            buf.write(make_tone(1000.0, BLOCK, 0.4, CHANNELS), t0 + i * 10_000_000, 0.0)
        for _ in range(6):
            buf.read(BLOCK)
        stale = buf._play_at
        # Sender restarted 3 seconds later: the buffer must re-anchor forward.
        for i in range(6):
            buf.write(make_tone(1000.0, BLOCK, 0.4, CHANNELS), t0 + 3_000_000_000 + i * 10_000_000, 0.0)
        self.assertGreater(buf._play_at, stale)
        self.assertGreaterEqual(buf.resyncs, 1)

    def test_silence_when_empty(self):
        buf = JitterBuffer()
        block = buf.read(BLOCK)
        self.assertEqual(float(np.max(np.abs(block))), 0.0)
        self.assertEqual(buf.underruns, 1)

    def test_a_tiny_gap_holds_the_last_sample_instead_of_a_click(self):
        """★ 2026-10-19 用户：「本次测试中发现各个链路的音频质量出现了下降，存在短小杂音」。

        一个块只差**几个采样**时（网络抖动让这一包晚到几毫秒），原来直接补静音 ——
        波形被硬切到 0 再跳回来，那正是一声"咔"。现在缺口不超过 JITTER_HOLD_MS 就
        **保持最后一个采样**，并单独记在 held_gaps 里（不算断流，也就不会去撑大缓冲）；
        真正的大缺口还是补静音 + 记一次 underrun。
        """
        from audiomixing.net import rtpaudio

        buf = JitterBuffer()
        t0 = proto.now_ns()
        flat = np.full(BLOCK * CHANNELS, 0.3, dtype=np.float32)   # 直流 0.3：不会撞上过零点
        # 按真实节奏喂：每 10ms 一包 + 睡 10ms，playhead 才会收敛到"现在 - 目标"这种健康位置
        # （不睡的话 8 次 read 会在零点几毫秒里把播放头推到"现在"前面，那一块就整个缺了）
        for i in range(15):
            buf.write(flat, t0 + i * 10_000_000, 0.0)
            time.sleep(0.01)
            buf.read(BLOCK)
        self.assertTrue(buf._primed, "先要播起来")

        # 让真实时间往前走一点：播放头会落后于"现在"（正常播放中的样子），再补一包让"最近有包"成立；
        # 否则播放头贴着（甚至超过）"现在"，due 只剩几毫秒，缺口会大得不像"小缺口"。
        time.sleep(0.06)
        buf.write(flat, proto.now_ns(), 0.0)
        time.sleep(0.01)

        # 小缺口：环里只剩 440 帧（比一个块少 40 帧 ≈ 0.83ms）→ 这一块必然差 40 帧
        buf._count = BLOCK - 40
        gaps, unders = buf.held_gaps, buf.underruns
        block = buf.read(BLOCK)
        self.assertGreater(buf.held_gaps, gaps, "小缺口该用「保持上一个采样」补，不是补静音")
        self.assertEqual(buf.underruns, unders, "听不出来的小缺口不许记成断流")
        self.assertGreater(abs(float(block[-1][0])), 0.1, "补上去的是上一个采样（0.3），不是 0")

        # 大缺口：环里一帧都没有 → 这一块整个缺（> JITTER_HOLD_MS）→ 仍旧静音 + 记一次 underrun
        buf._count = 0
        unders = buf.underruns
        block = buf.read(BLOCK)
        self.assertEqual(float(np.max(np.abs(block))), 0.0, "真断流就该是静音")
        self.assertGreater(buf.underruns, unders, "真断流要记 underrun")
        self.assertTrue(hasattr(rtpaudio, "JITTER_HOLD_MS"))

    def test_ring_never_reports_more_than_capacity(self):
        buf = JitterBuffer(capacity_ms=100.0)
        t0 = proto.now_ns()
        for i in range(100):
            buf.write(make_tone(500.0, BLOCK, 0.3, CHANNELS), t0 + i * 10_000_000, 0.0)
        self.assertLessEqual(buf._count, buf.capacity)
        self.assertGreater(buf.dropped_overflow, 0)

    def test_late_packet_ratio_above_ten_percent_raises_the_target(self):
        """★ 2026-11-03（Q2）：目标 25ms 却持续晚到，说明下限不够，自适应要看迟到比例。

        数据来自用户截图：9.2MB ≈ 48 秒 ≈ 4800 包，迟到 4670 —— 大部分包都晚到。
        迟到比例 50% 时按 +10ms 涨；只涨到 JITTER_MAX_MS 为止。
        """
        from audiomixing.net import rtpaudio

        buf = JitterBuffer()
        buf.target_frames = rtpaudio._ms_to_frames(25.0)
        buf._last_adjust_ns = 0
        buf.samples_written = 480 * 100        # 这一秒 100 个 10ms 包
        buf.dropped_late = 50                  # 其中 50 个晚到 = 50%
        buf._adapt(10 ** 18)
        self.assertAlmostEqual(buf.stats()["target_ms"], 35.0, places=0,
                               msg="迟到一半的包，目标应涨 10ms")

    def test_a_handful_of_late_packets_does_not_inflate_the_target(self):
        from audiomixing.net import rtpaudio

        buf = JitterBuffer()
        buf.target_frames = rtpaudio._ms_to_frames(25.0)
        buf._last_adjust_ns = 0
        buf.samples_written = 480 * 100
        buf.dropped_late = 5                   # 5%，低于 10% 阈值
        buf._adapt(10 ** 18)
        self.assertAlmostEqual(buf.stats()["target_ms"], 25.0, places=0,
                               msg="偶发几包晚到不该把缓冲撑大")

    def test_overflow_never_raises_the_target(self):
        """溢出 = 队列已经太深/播放端停顿；此时涨目标只会更糟，只记录不涨。"""
        from audiomixing.net import rtpaudio

        buf = JitterBuffer()
        buf.target_frames = rtpaudio._ms_to_frames(25.0)
        buf._last_adjust_ns = 0
        buf.samples_written = 480 * 100
        buf.dropped_late = 80                  # 大量晚到
        buf.dropped_overflow = 480 * 30        # 同时溢出
        buf._adapt(10 ** 18)
        self.assertAlmostEqual(buf.stats()["target_ms"], 25.0, places=0,
                               msg="溢出这一秒不能涨目标")

    def test_pre_roll_overflow_does_not_stay_on_the_panel(self):
        """★ 2026-11-03（Q2）：还没开始播放时积压的溢出，开播那一刻清零一次。"""
        buf = JitterBuffer()
        t0 = proto.now_ns()
        for i in range(120):                   # 1.2 秒的包塞进 400ms 的环 → 必然溢出
            buf.write(make_tone(1000.0, BLOCK, 0.3, CHANNELS), t0 + i * 10_000_000, 0.0)
        self.assertGreater(buf.dropped_overflow, 0, "前提：预滚阶段真的溢出了")
        self.assertFalse(buf._primed)
        buf.read(BLOCK)                        # 开始播放
        self.assertTrue(buf._primed)
        self.assertEqual(buf.dropped_overflow, 0, "开播前的溢出不能永久挂在面板上")
        self.assertEqual(buf.dropped_late, 0)


# --------------------------------------------------------------------------
# LAN data plane (two sockets on this machine)
# --------------------------------------------------------------------------

class TestUdpPath(unittest.TestCase):
    def test_tone_travels_over_udp(self):
        port = free_udp_port()
        receiver = Receiver("test-rx", port, bind_host="127.0.0.1")
        receiver.start()
        session = SenderSession(ToneSource(1000.0, 150, pace=0.0), "127.0.0.1", port, codec=proto.CODEC_PCM_S16)
        session.start()
        outs = []
        deadline = time.monotonic()
        end = deadline + 1.2
        while time.monotonic() < end:
            outs.append(receiver.mix(BLOCK))
            deadline += BLOCK / float(SAMPLE_RATE)
            sleep = deadline - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -0.05:
                deadline = time.monotonic()
        session.stop()
        time.sleep(0.1)
        receiver.stop()
        flat = np.concatenate([np.asarray(o, dtype=np.float32).reshape(-1) for o in outs])
        report = signal_report(list(flat))
        self.assertGreater(report["frames"], 40000)
        self.assertGreater(report["peak"], 0.3)
        self.assertAlmostEqual(report["frequency"], 1000.0, delta=25.0)
        self.assertGreater(rms(list(flat)), 0.2)

    def test_clock_is_locked_from_ping_pong(self):
        port = free_udp_port()
        receiver = Receiver("test-rx", port, bind_host="127.0.0.1")
        receiver.start()
        # A second receiver probes the first one, exactly like a node does.
        probe = Receiver("probe", free_udp_port(), bind_host="127.0.0.1")
        probe.start()
        threading.Thread(
            target=probe.ping_loop,
            args=(lambda: [("127.0.0.1", port, "test-rx")],),
            kwargs={"interval": 0.2},
            daemon=True,
        ).start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            clocks = probe.clock_stats()
            if clocks and clocks[0]["samples"] > 0:
                break
            time.sleep(0.1)
        clocks = probe.clock_stats()
        receiver.stop()
        probe.stop()
        self.assertTrue(clocks, "the probing receiver should have locked the peer clock")
        self.assertLess(abs(clocks[0]["offset_ms"]), 5.0)
        self.assertGreater(clocks[0]["samples"], 0)

    def test_rejects_foreign_datagrams(self):
        port = free_udp_port()
        receiver = Receiver("test-rx", port, bind_host="127.0.0.1")
        receiver.start()
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"not an audiomixing packet at all", ("127.0.0.1", port))
        s.close()
        time.sleep(0.2)
        stats = receiver.stats()
        receiver.stop()
        self.assertEqual(stats["packets_received"], 0)
        self.assertEqual(stats["packets_rejected"], 1)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

class TestDiscovery(unittest.TestCase):
    def test_beacon_round_trip_through_registry(self):
        registry = DiscoveryRegistry("node-self")
        payload = {
            "wl": 1,
            "id": "11111111-2222-3333-4444-555555555555",
            "name": "客厅电脑",
            "platform": "Windows-11",
            "version": "0.1.0",
            "audio_port": 47831,
            "web_port": 17800,
            "sources": [{"id": "a", "name": "扬声器 [loopback]", "detail": "loopback"}],
            "sinks": [{"id": "b", "name": "耳机", "detail": "render"}],
        }
        raw = build_beacon(
            payload["id"], payload["name"], payload["audio_port"], payload["web_port"], payload["sources"], payload["sinks"]
        )
        import json

        registry.update(json.loads(raw.decode("utf-8")), "192.168.1.20")
        peers = registry.all()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].name, "客厅电脑")
        self.assertEqual(peers[0].host, "192.168.1.20")
        self.assertTrue(peers[0].online)
        # Resolve by name, short id and host.
        self.assertIsNotNone(registry.resolve("客厅电脑"))
        self.assertIsNotNone(registry.resolve("11111111"))
        self.assertIsNotNone(registry.resolve("192.168.1.20"))
        self.assertIsNone(registry.resolve("nothing-like-this"))

    def test_beacon_carries_my_bluetooth_roster(self):
        """④ 的前半段：信标要带上"我能用蓝牙收谁的音"。

        用户原话（2026-10-15）：「手机界面点击送声音后，应当判断手机是否在电脑的蓝牙
        设备列表里。如果是就让用户判断到底用蓝牙还是 WiFi。」
        名单只能由电脑给出（列表在它手里），所以它必须出现在每台设备都收得到的信标里。
        """
        import json

        registry = DiscoveryRegistry("node-self")
        raw = build_beacon(
            "11111111-2222-3333-4444-555555555555", "客厅电脑", 47831, 17800, [], [],
            bt_devices=["小红的手机", "平板"],
        )
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["bt"], ["小红的手机", "平板"])
        peer = registry.update(payload, "192.168.1.20")
        assert peer is not None
        self.assertEqual(peer.bt_devices, ["小红的手机", "平板"])
        self.assertEqual(peer.to_json()["bt"], ["小红的手机", "平板"])

    def test_a_peer_without_the_bt_field_parses_as_empty(self):
        """老版本 / 没有蓝牙的机器：字段缺失只能是空表，不能是 None。

        否则手机端拿到 None 会在"名字在不在名单里"那一步炸掉 ——
        而这条路的正常结果本来就该是"没有名单 → 直接走 WiFi"。
        """
        registry = DiscoveryRegistry("me")
        peer = registry.update({"wl": 1, "id": "old", "name": "旧版", "audio_port": 1}, "127.0.0.1")
        assert peer is not None
        self.assertEqual(peer.bt_devices, [])
        self.assertEqual(peer.to_json()["bt"], [])

    def test_a_broken_bt_provider_never_blocks_the_beacon(self):
        """蓝牙那一路出问题（读缓存抛异常）不能连累发现层：信标照发，名单算空。"""
        import json

        def boom() -> list[str]:
            raise RuntimeError("蓝牙列表挂了")

        service = BeaconService(
            DiscoveryRegistry("me"), "me", "本机", 47831, 17800, lambda: [], lambda: [],
            bt_provider=boom,
        )
        payload = json.loads(service._beacon_bytes().decode("utf-8"))
        self.assertEqual(payload["bt"], [])
        self.assertEqual(payload["id"], "me")

    def test_own_beacon_is_ignored(self):
        registry = DiscoveryRegistry("me")
        registry.update({"wl": 1, "id": "me", "name": "me"}, "127.0.0.1")
        self.assertEqual(registry.all(), [])

    def test_offline_after_timeout(self):
        registry = DiscoveryRegistry("me")
        peer = registry.update({"wl": 1, "id": "other", "name": "other", "audio_port": 1}, "127.0.0.1")
        assert peer is not None
        peer.last_seen = time.time() - 60
        self.assertFalse(peer.online)
        self.assertEqual(registry.online(), [])
# HWHHYN 用 DEEPSEEK 制作

    def test_beacon_service_hears_another_node(self):
        """Two BeaconServices on the loopback interface must find each other."""
        port = free_udp_port()
        reg_a = DiscoveryRegistry("node-a")
        reg_b = DiscoveryRegistry("node-b")
        a = BeaconService(reg_a, "node-a", "A", 47831, 17800, list, list, port=port)
        b = BeaconService(reg_b, "node-b", "B", 47832, 17801, list, list, port=port)
        try:
            a.start()
            b.start()
            b.add_manual_target("127.0.0.1", port)
            a.add_manual_target("127.0.0.1", port)
            deadline = time.time() + 8.0
            while time.time() < deadline and not (reg_a.all() and reg_b.all()):
                time.sleep(0.2)
            self.assertTrue(reg_a.all(), "A should have heard B")
            self.assertEqual(reg_a.all()[0].name, "B")
        finally:
            a.stop()
            b.stop()


class TestControlPlaneOnDiscovery(unittest.TestCase):
    """★ 控制面搬家（2026-10-15）：控制消息从音频端口改走发现层。

    为什么（用户原话：「这一步不是瞎搞吗。我们设计了同一局域网设备能互相发现。
    那它本身就是多个设备都在互相同步数据啊」）：这一层信标每 2 秒一发一收、<b>设计上
    一直活着</b>；音频接收端是按需启停的（没在收听、没在听广播、没在送声音时它是停的）。
    控制消息（"我在把声音送给你，请自动打开收听"、共享音量、"请电脑去连我的蓝牙"）
    原来发到音频端口，接收器一停对端就永远收不到 —— 现场表现就是
    "电脑送声音打开了，手机的收听不会自己打开"。

    这几条盯住搬家之后不能塌的四件事：能力位、同 socket 不乱认设备、按能力分流、
    老版本对端照旧能收到。
    """

    def _service(self, port: int | None = None) -> BeaconService:
        return BeaconService(DiscoveryRegistry("self-node"), "self-node", "本机", 47831, 0,
                             lambda: [], lambda: [], port=port or free_udp_port())

    def test_the_beacon_advertises_the_control_channel(self) -> None:
        """信标里要有 ctl:1 和 disc_port —— 对端就是靠这两条决定"发给发现层还是音频端口"。"""
        import json

        port = free_udp_port()
        payload = json.loads(self._service(port)._beacon_bytes().decode("utf-8"))
        self.assertEqual(payload["ctl"], 1, "缺了它，对端只会照旧发音频端口")
        self.assertEqual(payload["disc_port"], port, "端口被占时换过端口，这里必须如实写")

        registry = DiscoveryRegistry("other")
        peer = registry.update(payload, "192.168.1.20")
        assert peer is not None
        self.assertTrue(peer.ctl)
        self.assertEqual(peer.disc_port, port)
        self.assertEqual(peer.to_json()["ctl"], True)
        self.assertEqual(peer.to_json()["disc_port"], port)

    def test_an_old_peer_is_read_as_plain_audio_only(self) -> None:
        """老版本没有这两个键 → ctl=False、disc_port=0，于是控制消息照旧走音频端口。

        这一条是"新旧混跑不打哑"的根据：判错成 True 就会把消息全发到一条
        老版本根本收不到的路上。
        """
        peer = DiscoveryRegistry("me").update(
            {"wl": 1, "id": "old", "name": "旧版", "audio_port": 47831}, "127.0.0.1")
        assert peer is not None
        self.assertFalse(peer.ctl)
        self.assertEqual(peer.disc_port, 0)

    def test_an_old_version_drops_the_envelope_without_creating_a_device(self) -> None:
        """老版本收到这份外衣必须【什么都不做】，绝不能多出一台假设备。

        老版本走的就是 registry.update（没有 ctl 那一层判断）：它只看 id，没有 id 直接
        返回 None。万一以后有人图省事给外衣也塞一个 id，这台"没名字的假设备"就会
        出现在每一台老版本的设备列表里。
        """
        registry = DiscoveryRegistry("other")
        self.assertIsNone(registry.update({"wl": 1, "ctl": {"t": "stats", "node": "x"}},
                                         "192.168.0.5"))
        self.assertEqual(registry.all(), [])

    def test_a_control_message_rides_the_beacon_socket(self) -> None:
        """真机跑一遍：这条消息必须到达对端的发现端口，正文原样，且不留下一台假设备。"""
        import json
        import socket

        port = free_udp_port()
        listener = self._service(port)
        raw = None
        listener.start()
        try:
            sender = self._service(free_udp_port())
            # 只给它一个 socket 就够：这一条只验"发得出去、对面收得到"。
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw.bind(("127.0.0.1", free_udp_port()))
            sender._sock = raw
            body = {"t": "stats", "node": "self-node", "name": "本机", "sending": True,
                    "want_listen": True, "gain": 0.5, "gain_ts": 11,
                    "gain_rx": 1.0, "gain_rx_ts": 22}
            self.assertTrue(sender.send_control(body, "127.0.0.1", port))

            deadline = time.time() + 3.0
            while time.time() < deadline and not listener.control_messages:
                time.sleep(0.05)
            msgs = listener.control_messages
            self.assertEqual(len(msgs), 1, "控制消息要真的到达对面的发现端口")
            msg = msgs[0]
            # ★ 下划线三个字段必须补齐：引擎处理两处消息走的是同一段代码。
            self.assertEqual(msg["_type"], "STATS")
            self.assertTrue(str(msg["_from"]).startswith("127.0.0.1:"))
            self.assertLess(abs(float(msg["_at"]) - time.monotonic()), 5.0)
            # ★ 正文原样透传：手机端/引擎都按原来那套字段名解析，一个都不许改。
            self.assertTrue(msg["want_listen"])
            self.assertAlmostEqual(float(msg["gain"]), 0.5, places=3)
            self.assertEqual(int(msg["gain_rx_ts"]), 22)
            self.assertEqual(listener.registry.all(), [],
                             "控制消息不是信标，绝不能变成一台设备")
        finally:
            if raw is not None:
                raw.close()
            listener.stop()

    def test_a_send_that_cannot_leave_reports_it(self) -> None:
        """socket 还没起来（start 之前 / 已经 stop）时必须返回 False。

        调用方靠这个返回值决定"退回音频端口再发一份" —— 报成功就等于把消息静默丢掉，
        而这正是最难查的一类故障。
        """
        service = self._service()
        self.assertFalse(service.send_control({"t": "stats"}, "127.0.0.1", 47830))


# --------------------------------------------------------------------------
# engine: routing decisions
# --------------------------------------------------------------------------

class TestRouting(unittest.TestCase):
    def setUp(self):
        self.node = AudioMixingNode(
            _config_stub(name="本机", node_id="node-self"),
            audio_port=free_udp_port(),
            discovery_port=free_udp_port(),
            simulate=True,
        )
        self.node.start(announce=False)

    def tearDown(self):
        self.node.stop()

    def _add_peer(self, node_id: str, name: str, audio_port: int) -> None:
        self.node.registry.update(
            {"wl": 1, "id": node_id, "name": name, "audio_port": audio_port, "web_port": 1, "sinks": [{"name": "耳机", "id": "x"}]},
            "192.168.1.50",
        )

    def test_no_route_no_traffic(self):
        self.node.set_routes([])
        self.assertEqual(self.node.desired_senders(), [])
        self.assertEqual(self.node.desired_sinks(), {})

    def test_self_to_peer_creates_a_sender(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["伴侣电脑"], "codec": "pcm_s16"}])
        wanted = self.node.desired_senders()
        self.assertEqual(len(wanted), 1)
        session_key, target, rule = wanted[0]
        # The key is "<route>|<target node>": one route may name several speakers and
        # each needs its own stream, so the route id alone is not unique.
        self.assertEqual(session_key, "r1|peer-1")
        self.assertEqual(rule["id"], "r1")
        self.assertEqual(target.node_id, "peer-1")
        self.assertEqual(target.host, "192.168.1.50")

    def test_one_route_to_three_speakers_creates_three_senders(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self._add_peer("peer-2", "客厅音箱", 47991)
        self._add_peer("peer-3", "书房耳机", 47992)
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["伴侣电脑", "客厅音箱", "书房耳机"]}])
        wanted = self.node.desired_senders()
        self.assertEqual(len(wanted), 3, "every target must get its own stream")
        self.assertEqual(sorted(k for k, _t, _r in wanted), ["r1|peer-1", "r1|peer-2", "r1|peer-3"])

    def test_peer_to_self_creates_a_sink(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["伴侣电脑"], "to": ["self"]}])
        sinks = self.node.desired_sinks()
        self.assertIn("r1", sinks)
        self.assertEqual(self.node.desired_senders(), [])

    def test_changing_a_device_rebuilds_the_stream(self):
        """Editing 采集设备/播放设备 must take effect on an already-running stream.

        The reconciler used to reuse a session whenever the *destination* was
        unchanged, so a new device setting was silently ignored until the route was
        deleted and re-added - the panel looked broken.
        """
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.devices = []  # no device table here, so skip the sink-name filter
        # The route below names a made-up device ("devA"), which no real backend can
        # open. Stub the sink spec the same way this test already stubs the source spec,
        # so what it exercises stays "the reconciler rebuilds the stream" and not
        # "which backend happens to accept a nonsense device name".
        self.node.default_sink_spec = lambda: {"backend": "null", "device": "@default"}  # type: ignore[method-assign]
        self.node.set_routes([
            {"id": "up", "from": ["self"], "to": ["peer-1"]},
            {"id": "down", "from": ["peer-1"], "to": ["self"], "sink_device": "devA"},
        ], persist=False)
        up1 = self.node.sender.sessions["up|peer-1"]
        down1 = self.node.sinks["down"]
        self.assertEqual(down1.device_selector, "devA")

        self.node.default_source_spec = lambda: {"backend": "null", "device": "tone:1000", "freq": 1000.0}  # type: ignore[method-assign]
        self.node.set_routes([
            {"id": "up", "from": ["self"], "to": ["peer-1"]},
            {"id": "down", "from": ["peer-1"], "to": ["self"], "sink_device": "devB"},
        ], persist=False)

        up2 = self.node.sender.sessions["up|peer-1"]
        down2 = self.node.sinks["down"]
        self.assertIsNot(up1, up2, "a new capture device must rebuild the sender")
        self.assertEqual(up2.source_key, ("null", "tone:1000"))
        self.assertIsNot(down1, down2, "a new playback device must rebuild the sink")
        self.assertEqual(down2.device_selector, "devB")

    def test_the_configured_default_output_is_used_as_it_is(self):
        """Play to the endpoint Windows calls the default; do not second-guess it.

        Choosing "a nicer" device moved the audio to the speakers by itself as soon as
        a second route appeared, because the endpoint being captured was skipped.
        """
        self.node.simulate = False
        self.node.devices = [
            audio_base.AudioDevice("a", "耳机 (Air4 Stereo)", audio_base.KIND_SINK, "wasapi", "render", is_default=True),
            audio_base.AudioDevice("b", "扬声器 (Senary Audio)", audio_base.KIND_SINK, "wasapi", "render"),
            audio_base.AudioDevice("c", "耳机 (Air4 Stereo)", audio_base.KIND_SOURCE, "wasapi", "loopback", is_default=True),
        ]

        class _FakeSession:
            source = types.SimpleNamespace(name="@default", endpoint_name="耳机 (Air4 Stereo)")
            source_name = "@default"
            source_key = ("wasapi", "@default")

            def stop(self) -> None:
                pass

        # Even while that very endpoint is being captured, the default still wins:
        # the operator asked for it, and going elsewhere is worse than an echo.
        self.node.sender.sessions["r1|peer-1"] = _FakeSession()
        self.assertEqual(self.node.default_sink_spec()["device"], "耳机 (Air4 Stereo)")

        # A *virtual* default is the one case worth overriding: never audible.
        self.node.devices[0].is_default = False
        self.node.devices[1].is_default = False
        self.node.devices.append(
            audio_base.AudioDevice("v", "扬声器 (Steam Streaming Speakers)", audio_base.KIND_SINK, "wasapi", "render", is_default=True)
        )
        picked = self.node.default_sink_spec()["device"]
        self.assertNotIn("Steam", picked, "a virtual default must not be used for playback")
        self.assertIn(picked, ("耳机 (Air4 Stereo)", "扬声器 (Senary Audio)"))

        # An explicit choice still wins over everything.
        self.node.config.sink_device = "扬声器 (Senary Audio)"
        self.assertEqual(self.node.default_sink_spec()["device"], "扬声器 (Senary Audio)")

    def test_a_route_output_name_from_another_machine_is_a_hint(self):
        """The route's sink device is written on one panel and used on another.

        Demanding a match silenced the receiving machine whenever the name described
        the *sender's* hardware, which is what a shared route table easily produces.
        """
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.devices = [
            audio_base.AudioDevice("a", "耳机 (1MORE HQ51)", audio_base.KIND_SINK, "wasapi", "render", is_default=True),
        ]
        self.node.set_routes([
            {"id": "r1", "from": ["peer-1"], "to": ["self"], "sink_device": "耳机 (OPPO Enco Air4 Stereo)"},
        ], persist=False)
        sinks = self.node.desired_sinks()
        self.assertIn("r1", sinks, "a device we do not have must not silence the route")
        self.assertEqual(sinks["r1"]["rule"]["sink_device"], "", "fall back to this machine's own default")

        # A name this machine *does* have is still honoured.
        self.node.set_routes([
            {"id": "r1", "from": ["peer-1"], "to": ["self"], "sink_device": "1MORE HQ51"},
        ], persist=False)
        self.assertEqual(self.node.desired_sinks()["r1"]["rule"]["sink_device"], "1MORE HQ51")

    def test_capture_follows_the_windows_default_device(self):
        """`@default` is not a fixed endpoint - Windows switches it at will.

        A capture bound to the old default keeps reading a device that nothing plays
        to any more, so the peer hears silence: exactly the "I changed my output and
        the other side went quiet" report.
        """
        self.node.devices = [
            audio_base.AudioDevice("l1", "扬声器 (Senary Audio)", audio_base.KIND_SOURCE, "wasapi", "loopback", is_default=False),
            audio_base.AudioDevice("l2", "耳机 (Air4 Stereo)", audio_base.KIND_SOURCE, "wasapi", "loopback", is_default=True),
        ]
        spec = {"backend": "wasapi", "device": "@default", "loopback": True}
        first = self.node._resolved_source_key(spec)
        self.assertIn("Air4", first[1], "the key must name the endpoint @default resolves to")
        # The user plugs a different headset in; Windows makes it the default.
        self.node.devices[0].is_default = True
        self.node.devices[1].is_default = False
        self.assertNotEqual(first, self.node._resolved_source_key(spec), "a new default must be a new key")
        self.assertEqual(self.node._resolved_source_key({"backend": "wasapi", "device": "Senary Audio"}),
                         ("wasapi", "Senary Audio"), "an explicit device is already an identity")

    def test_a_running_sink_keeps_its_endpoint_unless_it_stops_making_sense(self):
        """Device choice is a preference, not something to re-decide every second.

        Windows flips its default whenever a Bluetooth device sleeps, so re-running
        the preference on every refresh made the sink hop between two equally good
        endpoints every few seconds, which is heard as constant dropouts.
        """
        self.node.simulate = False
        self.node.devices = [
            audio_base.AudioDevice("a", "耳机 (Air4 Stereo)", audio_base.KIND_SINK, "wasapi", "render", is_default=True),
            audio_base.AudioDevice("b", "扬声器 (Senary Audio)", audio_base.KIND_SINK, "wasapi", "render"),
            audio_base.AudioDevice("c", "耳机 (Air4 Stereo)", audio_base.KIND_SOURCE, "wasapi", "loopback", is_default=True),
        ]
        rule: dict = {"id": "r1", "sink_device": ""}
        engine = SinkEngine(route_id="r1", device_selector="扬声器 (Senary Audio)")
        # Still present and the route does not name another: keep it, even though it is
        # also being captured (an echo is preferable to a device that keeps reopening).
        self.assertTrue(self.node._sink_device_still_valid(engine, rule))

        # The route now names a different device: follow it.
        self.assertFalse(self.node._sink_device_still_valid(engine, {"id": "r1", "sink_device": "耳机 (Air4 Stereo)"}))

        # And a device that disappeared must be re-resolved.
        engine.device_selector = "已经不存在的耳机"
        self.assertFalse(self.node._sink_device_still_valid(engine, rule))

    def test_listen_only_plays_the_channel_and_sends_nothing(self):
        """The FM model: a receiver never transmits, so no loop can form."""
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "up", "from": ["self"], "to": ["peer-1"]}], persist=False)
        self.assertEqual(len(self.node.desired_senders()), 1, "without the channel it would send")

        self.node.config.listen = True
        # 旧行为是「只听 = 完全不发送」。那会把普通点对点路由的发送端一起关掉，
        # 现场表现就是：在发送端打开只听之后，点对点的声音凭空消失，对方什么都收不到。
        # 现在只拦广播通道的发送（那才是回环的来源），普通路由照常发送。
        wanted = {key.split("|", 1)[0] for key, _t, _r in self.node.desired_senders()}
        self.assertIn("up", wanted, "只听模式下普通路由的发送端必须保留")
        self.assertNotIn("channel", wanted, "只听模式下不该往广播通道发送")
        sinks = self.node.desired_sinks()
        self.assertIn("channel", sinks)
        self.assertEqual(sinks["channel"]["rule"]["kind"], "listen")
        self.assertFalse(self.node._is_origin_for(sinks["channel"]["rule"]))

    def test_broadcast_sends_this_machine_to_every_node(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self._add_peer("peer-2", "客厅音箱", 47991)
        self.node.config.broadcast = True
        keys = sorted(k for k, _t, _r in self.node.desired_senders())
        self.assertEqual(keys, ["channel|peer-1", "channel|peer-2"])
        self.assertNotIn("channel", self.node.desired_sinks(), "a station does not play itself")

    def test_two_stations_mix_into_one_channel_route(self):
        """Every station stamps the same route id, so a listener gets one mixed block.

        Without that a listener would need one sink per station, each on its own
        timeline, instead of a single mixed stream.
        """
        recv = self.node.receiver
        for ssrc, owner in ((11, "node-b"), (22, "node-c")):
            buf = JitterBuffer(name="s%d" % ssrc)
            recv.streams[ssrc] = StreamInfo(ssrc=ssrc, node_id=owner)
            recv.buffers[ssrc] = buf
            recv.assign_routes(ssrc, {"channel"})
            start = proto.now_ns() - 300_000_000
            for i in range(8):
                blk = np.zeros((BLOCK, CHANNELS), dtype=np.float32)
                blk[:, 0] = 0.4
                buf.write(blk.reshape(-1), start + i * 10_000_000, 0.0)
        out = recv.mix_by_route(BLOCK, routes=["channel"])
        self.assertIn("channel", out)
        self.assertGreater(float(np.max(np.abs(out["channel"]))), 0.6,
                           "two 0.4 stations must sum, not replace each other")

    def test_a_capture_device_is_not_opened_as_a_loopback(self):
        """A microphone - or a phone arriving over Bluetooth A2DP - is an input.

        Windows exposes the phone's A2DP stream as "麦克风 (<phone> A2DP SNK)", i.e. a
        capture endpoint. Asking WASAPI for a *render* endpoint of that name finds
        nothing, so the source never opened at all.
        """
        self.node.devices = [
            audio_base.AudioDevice("l1", "扬声器 (Senary Audio)", audio_base.KIND_SOURCE, "wasapi", "loopback",
                                   is_default=True),
            audio_base.AudioDevice("c1", "麦克风 (瘾控 A2DP SNK)", audio_base.KIND_SOURCE, "wasapi", "capture"),
            audio_base.AudioDevice("c2", "麦克风阵列 (英特尔智音技术)", audio_base.KIND_SOURCE, "wasapi", "capture"),
        ]
        self.assertTrue(self.node._selector_is_loopback("@default"))
        self.assertTrue(self.node._selector_is_loopback("Senary Audio"))
        self.assertFalse(self.node._selector_is_loopback("瘾控 A2DP SNK"))
        self.assertFalse(self.node._selector_is_loopback("麦克风阵列 (英特尔智音技术)"))
        self.assertTrue(self.node._selector_is_loopback("没见过的设备"), "unknown keeps the old behaviour")

        self.node.simulate = False
        self.node.config.source_device = "瘾控 A2DP SNK"
        spec = self.node.default_source_spec()
        self.assertFalse(spec["loopback"], "a phone over Bluetooth must be opened as an input")
        self.node.config.source_device = "Senary Audio"
        self.assertTrue(self.node.default_source_spec()["loopback"])

    def test_mixer_combines_inputs_and_tolerates_a_silent_one(self):
        """Several sources become one stream - the point of the multi-source channel.

        A phone that is connected but quiet must not stall the others or add silence
        in a way that shifts them in time.
        """
        import array as _array

        from audiomixing.audio.capture import MixReader

        class _Tap:
            """Stands in for a TapReader, feeding one block per read."""

            def __init__(self, value: float, blocks: int) -> None:
                self.value = value
                self.blocks = blocks
                self.got = 0
                self.endpoint_name = "tap-%s" % value

            def drain_nowait(self):
                if self.got >= self.blocks:
                    return _array.array("f")
                self.got += 1
                return _array.array("f", [self.value] * (480 * CHANNELS))

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        loud, quiet = _Tap(0.4, 30), _Tap(0.2, 30)
        mixer = MixReader([loud, quiet], name="test")
        mixer.start()
        out = mixer.read()
        self.assertEqual(len(out), 480 * CHANNELS, "a mixer always emits one whole block")
        self.assertAlmostEqual(max(out), 0.6, places=3, msg="the two inputs must sum")

        silent = _Tap(0.5, 0)  # connected but sending nothing at all
        mixer2 = MixReader([_Tap(0.3, 30), silent], name="test2")
        mixer2.start()
        alone = mixer2.read()
        self.assertAlmostEqual(max(alone), 0.3, places=3, msg="a silent input contributes nothing")

    def test_the_channel_can_mix_several_sources(self):
        """`channel_sources` turns the broadcast into a mix of several devices."""
        self.node.config.broadcast = True
        self.node.config.channel_sources = ["麦克风 (瘾控 A2DP SNK)", "扬声器 (Senary Audio)"]
        rule = [r for r in self.node.channel_routes() if r.get("kind") == "broadcast"][0]
        spec = self.node._source_spec_for(rule)
        self.assertEqual(spec["sources"], ["麦克风 (瘾控 A2DP SNK)", "扬声器 (Senary Audio)"])

    def test_the_local_loop_refuses_to_capture_its_own_output(self):
        """Capturing the endpoint we play to is a howl, not a feature.

        A user heard "a lot of noise" the moment the local pass-through was switched
        on, because the channel's source list still named the output device. Silence
        plus an explanation is the right answer.
        """
        self.node.devices = [
            audio_base.AudioDevice("l1", "耳机 (1MORE HQ51)", audio_base.KIND_SOURCE, "wasapi", "loopback",
                                   is_default=True),
            audio_base.AudioDevice("r1", "耳机 (1MORE HQ51)", audio_base.KIND_SINK, "wasapi", "render",
                                   is_default=True),
        ]
        self.node.config.local = True
        self.node.config.sink_device = "耳机 (1MORE HQ51)"
        self.node.config.channel_sources = ["耳机 (1MORE HQ51)"]
        spec, _sink_spec, _key = self.node._local_loop_spec()
        self.assertEqual(self.node._capture_names_for(spec), ["耳机 (1MORE HQ51)"])
        self.assertTrue(self.node._looks_like_same_device("耳机 (1MORE HQ51)", "耳机 (1MORE HQ51)"))
        self.assertFalse(self.node._looks_like_same_device("麦克风 (瘾控 A2DP SNK)", "耳机 (1MORE HQ51)"),
                         "the phone is not the output device, so it must be allowed")
        self.node.config.local = False
        self.node.config.channel_sources = []

    def test_muted_route_is_inert(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["伴侣电脑"], "mute": True}])
        self.assertEqual(self.node.desired_senders(), [])

    def test_wildcard_prefers_a_deterministic_origin(self):
        """With a '*' source rule exactly one node may capture, and it must be the
        same node on every machine (the lowest node id), or the sound is doubled."""
        self._add_peer("zzz-peer", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["*"], "to": ["*"]}])
        # "node-self" < "zzz-peer", so this node is the origin here.
        self.assertTrue(self.node._is_origin_for(self.node.routes[0]))

    def test_wildcard_yields_to_a_lower_peer_id(self):
        self._add_peer("aaa-peer", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["*"], "to": ["*"]}])
        self.assertFalse(self.node._is_origin_for(self.node.routes[0]))

    def test_unrelated_route_does_not_match(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self._add_peer("peer-2", "客厅音箱", 47991)
        self.node.set_routes([{"id": "r1", "from": ["伴侣电脑"], "to": ["客厅音箱"]}])
        # Neither end is us, so this node does nothing at all.
        self.assertEqual(self.node.desired_senders(), [])
        self.assertEqual(self.node.desired_sinks(), {})

    def test_sink_device_name_is_only_a_hint(self):
        """The name is typed on one machine but used on another (shared table).

        It used to be a hard filter, so a route naming the *sender's* output device
        made the receiving machine go completely silent.
        """
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self.node.set_routes([{"id": "r1", "from": ["伴侣电脑"], "to": ["self"], "sink_device": "不存在的设备"}], persist=False)
        sinks = self.node.desired_sinks()
        self.assertIn("r1", sinks, "an unknown device name must not disable the route")
        self.assertEqual(sinks["r1"]["rule"]["sink_device"], "", "fall back to the local default")

    def test_two_routes_share_one_capture(self):
        self._add_peer("peer-1", "伴侣电脑", 47990)
        self._add_peer("peer-2", "客厅音箱", 47991)
        self.node.set_routes(
            [
                {"id": "r1", "from": ["self"], "to": ["伴侣电脑"]},
                {"id": "r2", "from": ["self"], "to": ["客厅音箱"]},
            ]
        )
        self.node.apply_routes()
        time.sleep(0.4)
        captures = self.node.hub.stats()
        self.assertEqual(len(captures), 1, "the same capture device must be opened once")
        self.assertEqual(captures[0]["consumers"], 2)
        self.assertEqual(len(self.node.sender.sessions), 2)
        # Both sessions must carry the same samples (fan-out, not interleaving).
        self.assertEqual(captures[0]["frames_read"] > 0, True)


def _config_stub(name: str = "test", node_id: str = "node-test"):
    from audiomixing.config import Config

    cfg = Config()
    cfg.node_id = node_id
    cfg.name = name
    cfg.codec = "pcm_s16"
    cfg.routes = []
    cfg.manual_peers = []
    cfg.accept_unknown = False
    return cfg


class TestEngineEndToEnd(unittest.TestCase):
    """Two nodes on this machine, engine to engine, using the built-in generators."""

    def test_audio_flows_between_two_nodes(self):
        audio_port_a = free_udp_port()
        audio_port_b = free_udp_port()
        node_a = AudioMixingNode(_config_stub("A", "node-a"), audio_port=audio_port_a, discovery_port=free_udp_port(), simulate=True)
        node_b = AudioMixingNode(_config_stub("B", "node-b"), audio_port=audio_port_b, discovery_port=free_udp_port(), simulate=True)
        node_a.start(announce=False)
        node_b.start(announce=False)
        try:
            # A sends its built-in tone to B's null sink.
            node_a.registry.update({"wl": 1, "id": "node-b", "name": "B", "audio_port": audio_port_b, "web_port": 1}, "127.0.0.1", local=True)
            node_b.registry.update({"wl": 1, "id": "node-a", "name": "A", "audio_port": audio_port_a, "web_port": 1}, "127.0.0.1", local=True)
            route = {"id": "r1", "from": ["A"], "to": ["B"], "codec": "pcm_s16", "gain": 1.0}
            node_a.set_routes([route], persist=False)
            node_b.set_routes([route], persist=False)
            node_a.tick()
            node_b.tick()
            deadline = time.time() + 6.0
            received = 0
            while time.time() < deadline:
                node_a.tick()
                node_b.tick()
                received = node_b.receiver.stats()["packets_received"]
                if received > 30:
                    break
                time.sleep(0.1)
            self.assertGreater(received, 30, "B should be receiving audio from A")
            self.assertGreaterEqual(node_b.receiver.stats()["active_streams"], 1)
            self.assertGreater(len(node_a.sender.sessions), 0)
        finally:
            node_a.stop()
            node_b.stop()


class TestCaptureHub(unittest.TestCase):
    """Fan-out readers must release their device when the last consumer leaves."""

    class _FakeSource:
        name = "假设备 [loopback]"
        sample_rate = SAMPLE_RATE
        channels = CHANNELS

        def start(self) -> None:
            pass

        def read(self):
            time.sleep(0.01)
            return None

        def stop(self) -> None:
            pass

        def describe(self) -> dict:
            return {"name": self.name}

    def test_a_capture_without_consumers_is_released(self):
        from audiomixing.audio.capture import CaptureHub

        hub = CaptureHub()
        hub.configure(lambda spec: self._FakeSource())
        reader = hub.reader({"backend": "null", "device": "x"}, name="x")
        self.assertEqual(len(hub.stats()), 1)
        reader.stop()
        hub.reap()
        self.assertEqual(hub.stats(), [], "a capture with no consumers must be released")

    def test_reconciliation_reaps_unused_captures(self):
        """The engine has to call reap(); nothing else does."""
        node = AudioMixingNode(_config_stub(name="t", node_id="node-hub"), audio_port=free_udp_port(),
                            discovery_port=free_udp_port(), simulate=True)
        node.start(announce=False)
        try:
            calls = []
            original = node.hub.reap

            def spy() -> None:
                calls.append(1)
                original()

            node.hub.reap = spy  # type: ignore[method-assign]
            node.apply_routes()
            self.assertTrue(calls, "apply_routes must reap captures that lost their consumers")
        finally:
            node.stop()


class TestRoutePortability(unittest.TestCase):
    """A route has to mean the same thing on every node.

    `self` is relative to whoever wrote the route. Sending it over the wire and
    letting the reader resolve it against *itself* turned "play A's sound on B"
    into a self-loop on A and a do-nothing rule on B, so no node ever opened a
    sender and the audio simply never started - while both panels looked fine.
    """

    def setUp(self):
        self.a = self._node("node-a", "A机")
        self.b = self._node("node-b", "B机")
        for node, other in ((self.a, ("node-b", "B机")), (self.b, ("node-a", "A机"))):
            node.registry.update(
                {"wl": 1, "id": other[0], "name": other[1], "audio_port": 47990, "web_port": 1,
                 "sinks": [{"name": "耳机", "id": "x"}]},
                "192.168.1.50",
            )

    def _node(self, node_id: str, name: str) -> AudioMixingNode:
        node = AudioMixingNode(_config_stub(name=name, node_id=node_id), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
        node.start(announce=False)
        self.addCleanup(node.stop)
        return node

    def test_self_is_resolved_to_the_authoring_node(self):
        # B's user drags "本机 -> A机".
        self.b.set_routes([{"id": "r1", "from": ["self"], "to": ["node-a"]}], persist=False)
        rule = self.b.routes[0]
        self.assertEqual(rule["from"], ["node-b"], "self must become B's own id")
        self.assertEqual(rule["to"], ["node-a"])
        self.assertEqual(rule["origin"], "node-b")

        wanted = self.b.desired_senders()
        self.assertEqual(len(wanted), 1, "B is the one that must capture and send")
        self.assertEqual(wanted[0][1].node_id, "node-a")

        # A receives exactly that table over the wire.
        self.a.set_routes(self.b.routes, persist=False, author_id="node-b", author_name="B机")
        self.assertEqual(self.a.desired_senders(), [], "A must not capture anything")
        self.assertIn("r1", self.a.desired_sinks(), "A must play B's audio")

    def test_a_peer_push_does_not_delete_my_own_routes(self):
        self.a.set_routes([{"id": "mine", "from": ["node-a"], "to": ["node-b"]}], persist=False)
        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"]}], persist=False,
                          author_id="node-b", author_name="B机")
        self.assertEqual(sorted(r["id"] for r in self.a.routes), ["mine", "theirs"])
        self.assertEqual([r["id"] for r in self.a.local_routes()], ["mine"])

    def test_an_echoed_route_is_not_duplicated(self):
        self.a.set_routes([{"id": "mine", "from": ["node-a"], "to": ["node-b"]}], persist=False)
        # A peer that re-broadcasts the whole merged table hands our own route back.
        self.a.set_routes(list(self.a.routes), persist=False, author_id="node-b", author_name="B机")
        self.assertEqual([r["id"] for r in self.a.routes], ["mine"])
        self.assertEqual([r["id"] for r in self.a.local_routes()], ["mine"], "ownership must survive the round trip")

    def test_deleting_a_peer_route_removes_it_here_too(self):
        """Omitting a peer's route from the list is not enough to delete it.

        The merge keeps every route whose owner is someone else, so it restored the
        route from the current table and the delete appeared to do nothing.
        """
        from audiomixing.webui import AudioMixingApi

        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"]}], persist=False,
                          author_id="node-b", author_name="B机")
        api = AudioMixingApi(self.a)
        api.delete_route({"id": "theirs"})
        self.assertEqual(self.a.routes, [], "the merge must not restore a route we just deleted")

    def test_deleting_a_peer_route_sticks(self):
        """Either panel may delete a route, and the owner must drop it too.

        Without the explicit "removed" list the owner kept its copy and re-pushed it
        on its next change, so the deleted route came straight back.
        """
        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"]}], persist=False,
                          author_id="node-b", author_name="B机")
        self.assertEqual([r["id"] for r in self.a.routes], ["theirs"])

        # B removes its own route and says so in the push.
        self.a.receiver.control_messages.append({
            "_type": "ROUTE", "from": "node-b", "ts": time.time() + 1,
            "routes": [], "removed": ["theirs"],
        })
        self.a._apply_pushed_routes()
        self.assertEqual(self.a.routes, [])

    def test_any_panel_can_adjust_any_route(self):
        """The volume slider lives on whichever machine is doing the playing."""
        from audiomixing.webui import AudioMixingApi

        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"], "gain": 1.0}],
                          persist=False, author_id="node-b", author_name="B机")
        self.assertEqual([r["id"] for r in self.a.local_routes()], [], "A does not own it")

        api = AudioMixingApi(self.a)
        applied = api.update_route({"id": "theirs", "gain": 0.4, "mute": True})["routes"]
        rule = [r for r in applied if r["id"] == "theirs"][0]
        self.assertAlmostEqual(rule["gain"], 0.4)
        self.assertTrue(rule["mute"])
        self.assertEqual(rule["origin"], "node-b", "ownership must survive a foreign edit")

    def test_ssrc_is_stable_per_sender_and_route(self):
        self.assertEqual(stable_ssrc("n1", "r1"), stable_ssrc("n1", "r1"))
        self.assertNotEqual(stable_ssrc("n1", "r1"), stable_ssrc("n2", "r1"))
        self.assertNotEqual(stable_ssrc("n1", "r1"), stable_ssrc("n1", "r2"))


class TestStreamRouteAttach(unittest.TestCase):
    """One incoming stream must reach every local sink that wants it - and only those."""

    def setUp(self):
        self.node = AudioMixingNode(_config_stub(name="本机", node_id="node-a"), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
        self.node.start(announce=False)
        self.addCleanup(self.node.stop)
        self.node.registry.update(
            {"wl": 1, "id": "node-b", "name": "B机", "audio_port": 47990, "web_port": 1, "sinks": [{"name": "耳机", "id": "x"}]},
            "192.168.1.50",
        )

    def test_two_sinks_for_one_source_are_both_found(self):
        # ★ v2.27：同端点、同输出设备的两条路由会被收敛成一条（重复播放的根因），
        #   所以这里用**两个不同的输出设备**表达"一台设备的声给两个喇叭"。
        self.node.set_routes([
            {"id": "r1", "from": ["node-b"], "to": ["self"], "sink_device": "speaker-a"},
            {"id": "r2", "from": ["node-b"], "to": ["self"], "sink_device": "speaker-b"},
        ], persist=False)
        self.assertEqual(self.node.routes_for_sender("node-b"), {"r1", "r2"})
        self.assertEqual(self.node.routes_for_sender("node-unknown"), set())

    def test_assign_routes_feeds_both_sinks_the_same_audio(self):
        buf = JitterBuffer(name="t")
        info = StreamInfo(ssrc=7)
        self.node.receiver.streams[7] = info
        self.node.receiver.buffers[7] = buf
        self.node.receiver.assign_routes(7, {"r1", "r2"})
        self.assertEqual(buf.route_ids, {"r1", "r2"})
        # 8 blocks of 0.5, timestamped in the past so they are already due.
        start = proto.now_ns() - 300_000_000
        for i in range(8):
            blk = np.zeros((BLOCK, CHANNELS), dtype=np.float32)
            blk[:, 0] = 0.5
            buf.write(blk.reshape(-1), start + i * 10_000_000, 0.0)
        out = self.node.receiver.mix_by_route(BLOCK, routes=["r1", "r2"])
        self.assertEqual(sorted(out), ["r1", "r2"])
        for key in ("r1", "r2"):
            self.assertGreater(float(np.max(np.abs(out[key]))), 0.4, "route %s got no audio" % key)
        self.assertIsNot(out["r1"], out["r2"], "shared blocks must be copied, not aliased")
        # A route that nobody asked for must stay silent.
        self.assertEqual(self.node.receiver.mix_by_route(BLOCK, routes=["r3"]), {})


class TestRenderLoop(unittest.TestCase):
    """The render loop must never throw away samples the device refused."""

    class _DripSink:
        """Accepts only a few frames per call, like a real device buffer."""

        def __init__(self, per_call: int) -> None:
            self.per_call = per_call
            self.out = array("f")

        def target_frames(self) -> int:
            return 0

        def write(self, samples) -> int:
            from audiomixing.audio.format import as_float_array

            arr = as_float_array(samples)
            take = min(self.per_call, len(arr) // CHANNELS)
            self.out.extend(arr[: take * CHANNELS])
            return take

        def stop(self) -> None:
            pass

        def describe(self) -> dict:
            return {"device": "drip"}

    def test_partial_writes_do_not_drop_samples(self):
        sink = self._DripSink(per_call=200)
        engine = SinkEngine(route_id="r1", device_selector="drip", frames_per_block=BLOCK, sink=sink)
        node = AudioMixingNode(_config_stub(name="t", node_id="node-render"), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
        state = {"pos": 0}

        class _Mixer:
            def mix_by_route(self, frames, routes=None):
                start = state["pos"]
                state["pos"] = start + frames
                ramp = np.arange(start, start + frames, dtype=np.float32)
                return {"r1": np.repeat(ramp, CHANNELS).reshape(frames, CHANNELS)}

        node.receiver = _Mixer()  # type: ignore[assignment]
        timer = threading.Timer(0.35, engine.stop_event.set)
        timer.start()
        try:
            node._render_loop(engine)
        finally:
            timer.cancel()

        written = np.asarray(sink.out, dtype=np.float32).reshape(-1)
        self.assertGreater(len(written), BLOCK * CHANNELS, "the loop must have produced audio")
        expected = np.repeat(np.arange(len(written) // CHANNELS, dtype=np.float32), CHANNELS)
        first_bad = int(np.argmax(written != expected)) if bool(np.any(written != expected)) else -1
        self.assertEqual(first_bad, -1, "render loop lost samples the device had not accepted (first mismatch at %d)" % first_bad)


class TestAudioRecovery(unittest.TestCase):
    """★ 2026-10-26 老问题：其他终端连上后没声音，Windows 里切换几次输出才恢复。

    自动看门狗负责“端点打开着但不再消费”的僵死；这里钉住手动兜底：
    `recover_audio()` 必须真的停掉每一路播放端并重新按当前默认输出打开。
    """

    def test_recover_audio_stops_every_sink(self) -> None:
        node = AudioMixingNode(_config_stub(name="t", node_id="node-recover"),
                               audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)

        class _FakeSink:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        fake = _FakeSink()
        node.sinks["r1"] = SinkEngine(route_id="r1", device_selector="d", sink=fake)
        result = node.recover_audio()
        self.assertTrue(result["ok"])
        self.assertIn("r1", result["stopped"])
        self.assertTrue(fake.stopped, "恢复声音必须真的关掉旧的 audio client")
        self.assertEqual(node.sinks, {}, "旧的播放端不能留在表里冒充还在工作")

    def test_the_watchdog_evidence_is_in_the_sink_status(self) -> None:
        engine = SinkEngine(route_id="r1", device_selector="d")
        engine.recoveries = 2
        engine.last_reopen_reason = "设备 3.2 秒没有收下任何音频"
        out = engine.describe()
        self.assertEqual(out["recoveries"], 2)
        self.assertIn("3.2", out["last_reopen_reason"])


class TestPhoneTap(unittest.TestCase):
    """The phone listener path: a browser cannot join the UDP plane, so the node it
    talks to mirrors what it plays over WebSocket instead."""

    def test_ws_accept_matches_the_rfc_example(self):
        from audiomixing.webui import ws_accept

        # RFC 6455 section 1.3 - the one published test vector.
        self.assertEqual(ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_ws_frame_encodes_all_three_length_forms(self):
        from audiomixing.webui import ws_frame

        self.assertEqual(ws_frame(b"abc")[:2], bytes([0x82, 3]))
        medium = ws_frame(b"x" * 200)
        self.assertEqual(medium[:4], bytes([0x82, 126, 0, 200]))
        large = ws_frame(b"y" * 70000)
        self.assertEqual(large[:2], bytes([0x82, 127]))
        self.assertEqual(struct.unpack(">Q", large[2:10])[0], 70000)
        self.assertEqual(ws_frame(b"", opcode=0xA)[:2], bytes([0x8A, 0]), "a pong frame")

    def test_tap_fans_out_to_every_listener(self):
        from audiomixing.audio.tap import AudioTap

        tap = AudioTap()
        first, second = tap.subscribe(), tap.subscribe()
        tap.publish([0.5, -0.5])
        self.assertEqual(tap.drain(first), tap.drain(second), "each listener gets the same audio")
        self.assertEqual(tap.drain(first), b"", "drain must empty the queue")
        tap.unsubscribe(second)
        tap.publish([0.1, 0.1])
        self.assertEqual(tap.drain(second), b"", "an unsubscribed listener stops receiving")

    def test_a_stalled_listener_cannot_grow_without_bound(self):
        """A phone paused in a background tab must not stall playback or eat memory."""
        from audiomixing.audio.tap import AudioTap

        tap = AudioTap(max_blocks=4)
        sid = tap.subscribe()
        for _ in range(50):
            tap.publish([0.1, 0.1])
        self.assertLessEqual(len(tap.drain(sid)), 4 * 2 * 2, "the queue must stay bounded")
        self.assertGreater(tap.stats()["dropped_blocks"], 0, "and the drops must be counted")

    def test_pcm16_clips_instead_of_wrapping(self):
        from audiomixing.audio.tap import to_pcm16

        values = struct.unpack("<5h", to_pcm16([0.0, 1.0, -1.0, 2.0, -2.0]))
        self.assertEqual(values, (0, 32767, -32767, 32767, -32767))

    def test_a_selector_from_another_backend_switches_the_spec(self):
        """`null`, `tone:1000` and `file:out.wav` live outside WASAPI.

        Without this the silent null sink - the only way to exercise the phone path
        without making a sound - could not be selected at all.
        """
        base = {"backend": "wasapi", "device": "@default", "loopback": True}
        self.assertEqual(audio_base.spec_for_selector(base, "null"),
                         {"backend": "null", "device": "null"})
        self.assertEqual(audio_base.spec_for_selector(base, "tone:1000")["backend"], "null")
        kept = audio_base.spec_for_selector(base, "耳机 (1MORE HQ51)")
        self.assertEqual(kept["backend"], "wasapi")
        self.assertTrue(kept.get("loopback"), "a normal selector keeps the base spec")
        self.assertEqual(audio_base.spec_for_selector(base, "")["device"], "@default")

    def test_write_paced_reports_every_block(self):
        from audiomixing.audio.base import write_paced

        seen: list[int] = []

        class _Sink:
            def write(self, samples) -> int:
                return len(samples) // CHANNELS

        frames = SAMPLE_RATE // 5
        write_paced(_Sink(), make_tone(1000.0, frames, 0.2, CHANNELS),
                    frames_per_block=480, on_block=lambda chunk: seen.append(len(chunk) // CHANNELS))
        self.assertEqual(sum(seen), frames, "every block must be reported exactly once")
        self.assertTrue(all(n <= 480 for n in seen))


class TestBluetoothBridge(unittest.TestCase):
    """Phones send audio over Bluetooth A2DP; the PC is driven through PowerShell.

    Python/WinRT cannot call `DeviceInformation.FindAllAsync` (every arity raises
    "Invalid parameter count"), and a hand-built device id is rejected with
    E_NOINTERFACE. Windows PowerShell 5.1 can call WinRT directly, which is why the
    bridge shells out instead of using the winrt packages.
    """

    def test_the_parser_accepts_both_json_shapes(self):
        from audiomixing import bluetooth as bt

        original = bt._run
        try:
            # PowerShell 5.1 renders a one-element array as a bare object.
            bt._run = lambda *a, **k: '[{"id":"a","name":"手机A","state":"Closed"}]'
            self.assertEqual([d["name"] for d in bt.list_devices()], ["手机A"])
            bt._run = lambda *a, **k: '{"id":"b","name":"手机B","state":"Open"}'
            self.assertEqual([d["name"] for d in bt.list_devices()], ["手机B"])
            bt._run = lambda *a, **k: 'not json at all'
            self.assertEqual(bt.list_devices(), [], "garbage must not raise")
            bt._run = lambda *a, **k: ''
            self.assertEqual(bt.list_devices(), [], "no output means no devices")
        finally:
            bt._run = original

    def test_a_dead_helper_is_forgotten_instead_of_shown_as_connected(self):
        """The Windows endpoint outlives the phone, so it must never mean "connected".

        A phone switched off leaves its A2DP endpoint present (Windows still reports it
        as OK) and a helper that died leaves `open:Opened` behind. Neither may let the
        panel claim a connection: a user hit exactly that - the phone's Bluetooth was
        off, the panel said 已连接, and pressing 断开 changed nothing.
        """
        import json as _json

        from audiomixing import bluetooth as bt

        device = {"id": r"\\?\BTHENUM#x\SNK", "name": "手机A", "state": "Opened", "endpoint": True}
        original_run, original_dir = bt._run, bt._STATE_DIR
        try:
            bt._run = lambda *a, **k: _json.dumps(device)
            bt._STATE_DIR = _mkdtemp()
            key = bt._key(device["id"])
            (bt._STATE_DIR / ("%s.keep" % key)).write_text("1", encoding="utf-8")
            (bt._STATE_DIR / ("%s.pid" % key)).write_text("999999", encoding="utf-8")
            (bt._STATE_DIR / ("%s.status" % key)).write_text("open:Opened", encoding="utf-8")

            row = bt.list_devices()[0]
            self.assertEqual(row["link"], "off", "a dead helper must not read as connected")
            self.assertFalse(row["held"])
            self.assertTrue(row["endpoint"], "the endpoint flag is still reported, as a hint")
            self.assertFalse((bt._STATE_DIR / ("%s.status" % key)).exists(),
                             "stale files must be cleaned up, not left to mislead")
        finally:
            bt._run, bt._STATE_DIR = original_run, original_dir

    def test_open_sink_without_the_phone_media_endpoint_is_waiting(self):
        """2026-10-21 现场：面板写「正在接收手机声音」，实际一点声音都没有。

        helper 的 `open:Opened` 只说明电脑这边把 A2DP sink 打开了；手机可能只挂着
        连接/控制通路，Windows 的 `麦克风 (<手机> A2DP SNK)` 采集端点还没起来
        （endpoint=False）。这时必须是 waiting（黄色提示），不能是 connected。
        """
        from audiomixing import bluetooth as bt

        device = {"id": r"\\?\BTHENUM#x\SNK", "name": "手机A", "state": "open:Opened",
                  "endpoint": False, "held": True}
        original_dir = bt._STATE_DIR
        sandbox = pathlib.Path(tempfile.mkdtemp(prefix="audiomixing-bt-test-"))
        try:
            bt._STATE_DIR = sandbox
            key = bt._key(device["id"])
            (sandbox / (key + ".keep")).write_text("1", encoding="utf-8")
            (sandbox / (key + ".pid")).write_text(str(os.getpid()), encoding="utf-8")
            (sandbox / (key + ".status")).write_text("open:Opened", encoding="utf-8")

            row = bt._apply_live_states([dict(device)])[0]
            self.assertEqual(row["link"], "waiting",
                             "sink 打开但手机媒体端点没起来 → 不能报 connected")
            self.assertTrue(row["held"])
            self.assertFalse(row["endpoint"])

            device["endpoint"] = True
            row = bt._apply_live_states([dict(device)])[0]
            self.assertEqual(row["link"], "connected",
                             "手机媒体端点也起来了 → 才是 connected")
        finally:
            bt._STATE_DIR = original_dir
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_connect_without_an_id_is_refused(self):
        from audiomixing import bluetooth as bt

        self.assertFalse(bt.connect("")["ok"])

    def test_the_helper_key_is_stable_and_path_safe(self):
        from audiomixing import bluetooth as bt

        device_id = r"\\?\BTHENUM#{0000110a-...#abc\SNK"
        first = bt._key(device_id)
        self.assertEqual(first, bt._key(device_id), "the same device must map to the same file")
        self.assertNotEqual(first, bt._key(device_id + "x"))
        self.assertTrue(first.isalnum(), "the temp filename must not carry path separators")


class TestLocalLoop(unittest.TestCase):
    """The single-machine path: capture -> render on this PC, no LAN at all.

    This is how a phone arriving over Bluetooth reaches this machine's headphones, and
    it is checked with built-in devices only (a tone generator as the source, the
    silent null sink as the output), so it needs no phone and makes no sound.
    """

    def test_the_local_loop_carries_capture_into_render(self):
        from audiomixing.audio.local import LocalLoop
        from audiomixing.audio.tap import AudioTap

        source = audio_base.open_source({"backend": "null", "device": "tone:1000"})
        sink = audio_base.open_sink({"backend": "null", "device": "null"})
        tap = AudioTap()
        sid = tap.subscribe()
        loop = LocalLoop(source, sink, gain=0.5, tap=tap)
        loop.start()
        time.sleep(1.0)
        loop.stop()
        time.sleep(0.2)
        info = loop.describe()
        self.assertTrue(info["ready"], info["error"])
        self.assertGreater(info["frames"], 38000, "it must move real-time amounts of audio")
        self.assertLess(info["frames"], 58000)
        self.assertGreater(info["peak"], 0.01, "audio must actually flow through it")
        self.assertLess(info["peak"], 0.3, "and the gain must have been applied")
        self.assertGreater(len(tap.drain(sid)), 0, "the phone listener page hears it too")

    def test_a_loop_that_cannot_open_reports_instead_of_crashing(self):
        from audiomixing.audio.local import LocalLoop

        class _Broken:
            endpoint_name = "broken"

            def start(self):
                raise OSError("device in use")

            def stop(self):
                pass

            def read(self):
                return None

            def write(self, samples):
                return 0

        loop = LocalLoop(_Broken(), _Broken())
        loop.start()
        loop.join(timeout=3)
        self.assertFalse(loop.describe()["ready"])
        self.assertIn("device in use", loop.describe()["error"])


class TestStartupPreset(unittest.TestCase):
    """Startup presets: connect the chosen phones, restore the channel switches.

    The channel switches (broadcast/listen/local) are plain config fields, so `start()`
    already applies them through set_routes -> apply_routes. Only the Bluetooth half
    needs new code, and it must never touch a phone the operator did not tick: opening
    an A2DP sink resets that phone's link.
    """

    def setUp(self):
        cfg = Config()
        cfg.node_id = "11111111-1111-1111-1111-111111111111"
        cfg.name = "本机"
        cfg.autostart = False
        self.cfg = cfg
        self.node = AudioMixingNode(cfg, simulate=True)
        self.addCleanup(self.node.stop)

    def test_only_the_ticked_phones_are_connected(self):
        from audiomixing import bluetooth as bt

        devices = [
            {"id": "id-A", "name": "手机A", "link": "off", "held": False},
            {"id": "id-B", "name": "手机B", "link": "off", "held": False},
            {"id": "id-C", "name": "手机C", "link": "connected", "held": True},
        ]
        opened: list[str] = []
        # The node reads the cached listing (a real one takes seconds), so the test has to
        # stand in for both entry points.
        original_list, original_connect = bt.list_devices, bt.connect
        original_cached = bt.list_devices_cached
        try:
            bt.list_devices = lambda: devices
            bt.list_devices_cached = lambda *a, **k: devices

            def fake_connect(device_id):
                opened.append(device_id)
                return {"ok": True, "state": "open:Opened"}

            bt.connect = fake_connect
            self.cfg.autoconnect = ["手机A", "手机C", "不存在的手机"]
            done = self.node.autoconnect_phones()
        finally:
            bt.list_devices, bt.connect = original_list, original_connect
            bt.list_devices_cached = original_cached

        self.assertEqual(opened, ["id-A"], "only the ticked, present, not-yet-open phone")
        self.assertEqual(sorted(done), ["手机A", "手机C"], "an already-open phone counts as done")

    def test_nothing_is_connected_when_nothing_is_ticked(self):
        from audiomixing import bluetooth as bt

        opened: list[str] = []
        original = bt.connect
        try:
            bt.connect = lambda device_id: (opened.append(device_id), {"ok": True})[1]
            self.cfg.autoconnect = []
            self.assertEqual(self.node.autoconnect_phones(), [])
        finally:
            bt.connect = original
        self.assertEqual(opened, [], "an untouched install must not poke any phone")

    def test_the_preset_survives_a_config_round_trip(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "config.json"
        self.addCleanup(shutil.rmtree, str(path.parent), True)
        self.cfg.autoconnect = ["瘾控", "悦瘾"]
        self.cfg.broadcast = True
        self.cfg.listen = True
        self.cfg.save(path)
        again = Config.load(path, create=False)
        self.assertEqual(again.autoconnect, ["瘾控", "悦瘾"])
        self.assertTrue(again.broadcast and again.listen,
                        "the channel switches must come back on the next start")


class TestNetworkMic(unittest.TestCase):
    """A phone's audio arrives as PCM over the node's web port.

    It then has to behave like any other capture device, because that is what makes the
    whole pipeline - channel source, local pass-through, broadcast - work with a phone
    that has no way of joining the UDP audio plane at all.
    """

    def _push_tone(self, mic, freq=1000.0, amp=12000, frames=4800):
        buf = array("h")
        for i in range(frames):
            buf.append(int(amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)))
        mic.push(buf.tobytes())

    def test_pushed_audio_reads_back_as_a_real_capture_device(self):
        from audiomixing.audio.backends import netmic

        mic = netmic.open_mic("测试手机", SAMPLE_RATE)
        try:
            # A device only becomes visible after a quarter of a second of audio, so
            # the app's 100 ms connectivity probe never turns into a phantom entry.
            for _ in range(6):
                self._push_tone(mic)
            state = mic.describe()
            # The queue is capped on purpose: a listener wants to be current, and a deep
            # backlog is felt as delay rather than as safety. Asserted as a range - the
            # exact sample count depends on when the last push crossed the cap.
            self.assertLessEqual(state["queued_frames"], netmic.MAX_QUEUED_FRAMES)
            self.assertGreater(state["queued_frames"], 0)
            self.assertTrue(state["live"])
            names = [d.name for d in audio_base.list_all_devices()
                     if d.backend == "netmic" and d.kind == audio_base.KIND_SOURCE]
            self.assertIn("测试手机", names, "it must show up in every device picker")

            mic.start()
            got = array("f")
            deadline = time.time() + 0.4
            while time.time() < deadline:
                got.extend(mic.read())
            self.assertGreater(len(got), 20000, "it must run at real time")
            from audiomixing.audio.format import peak as _peak

            self.assertAlmostEqual(_peak(got), 12000 / 32768.0, places=2)
            self.assertAlmostEqual(measure_frequency(got), 1000.0, delta=30.0)
        finally:
            netmic.close_mic("测试手机", mic)
        self.assertNotIn("测试手机", [d.name for d in audio_base.list_all_devices()])

    def test_a_phone_that_stops_sending_is_dropped(self):
        """Walking out of Wi-Fi range must not leave a dead device in the pickers."""
        from audiomixing.audio.backends import netmic

        mic = netmic.open_mic("走远的手机", SAMPLE_RATE)
        try:
            self._push_tone(mic, frames=14400)   # over the visibility threshold
            self.assertIn("走远的手机", [d.name for d in audio_base.list_all_devices()])
            mic.last_push -= netmic.STALE_S + 1
            netmic.reap()
            self.assertNotIn("走远的手机", [d.name for d in audio_base.list_all_devices()])
        finally:
            netmic.close_mic("走远的手机", mic)

    def test_an_odd_byte_count_does_not_corrupt_the_stream(self):
        from audiomixing.audio.backends import netmic

        mic = netmic.open_mic("半字节手机", SAMPLE_RATE)
        try:
            self.assertEqual(mic.push(b"\x01\x02\x03"), 1, "the trailing odd byte is dropped")
            self.assertEqual(mic.describe()["queued_frames"], 1)
        finally:
            netmic.close_mic("半字节手机", mic)


class TestNoMachineSpecificCode(unittest.TestCase):
    """Nothing may key off one particular machine's hardware.

    Test fixtures may use realistic names - they exercise parsing and matching. Shipped
    code may not: a hint that reads "the output is currently <my headset>" is wrong the
    moment somebody else installs this, and a branch on a device name is wrong
    everywhere. This keeps that promise from eroding.
    """

    FORBIDDEN = [
        "1MORE", "瘾控", "悦瘾", "红腕", "真我", "M332BF", "OPPO Enco", "EDIFIER",
        "Senary", "RZ616", "2024TSJ", "2025BJB", "192.168.0.108", "192.168.0.109",
    ]

    # Third-party product names are a different thing from one user's hardware: they are
    # hints that apply to anyone who happens to have that product installed. The one
    # place that uses them is the virtual-endpoint heuristic, which prefers a real
    # speaker when Windows' default is a remote-desktop/streaming shim. Listing the
    # exception here keeps it visible instead of implicit - and name-based detection is
    # still a heuristic, so a structural signal (endpoint form factor) would be better.
    PRODUCT_HINTS = {
        "engine.py": {"Steam Streaming", "WO Mic"},
    }

    def test_the_shipped_package_names_no_particular_machine(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "audiomixing"
        offenders: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".html", ".js"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            allowed = self.PRODUCT_HINTS.get(path.name, set())
            for name in self.FORBIDDEN:
                if name in text and name not in allowed:
                    offenders.append("%s mentions %r" % (path.relative_to(root), name))
        self.assertEqual(offenders, [], "shipped code is specialised to one machine: %s" % offenders)


class TestWedgedCapture(unittest.TestCase):
    """A source whose read() never returns used to freeze the pipeline silently."""

    def test_a_capture_that_never_produced_a_block_is_wedged(self):
        from audiomixing.audio.capture import WEDGED_AFTER_S, _Capture

        class _Blocked:
            name = "blocked"
            sample_rate = SAMPLE_RATE
            channels = CHANNELS

            def start(self): pass
            def read(self): return None
            def stop(self): pass
            def describe(self): return {}

        cap = _Capture({"backend": "test"}, _Blocked(), lambda spec: _Blocked())
        cap.consumers = 1
        cap.created_at = time.monotonic() - (WEDGED_AFTER_S + 1)
        self.assertTrue(cap.wedged(), "no block, someone waiting, too long: it is stuck")
        cap.blocks_read = 5
        self.assertFalse(cap.wedged(), "a capture that works is never dropped")
        cap.blocks_read = 0
        cap.closed = True
        self.assertFalse(cap.wedged(), "a closed capture is reaped, not rebuilt")



    def test_a_module_lock_is_never_taken_twice(self):
        """Calling a lock-taking helper from inside that same lock deadlocks the node.

        This is not hypothetical: /api/mic held the netmic lock and then called
        netmic.active(), which takes it again - a plain Lock is not reentrant, so the
        thread never let go and the whole node stopped answering until it was restarted.
        The check is precise on purpose: it looks for calls to functions that are known
        to take that lock, so it does not cry wolf about every `with self._lock:`.
        """
        import pathlib as _path
        import re as _re

        root = _path.Path(__file__).resolve().parent.parent / "audiomixing"
        # Which functions of each module take that module's lock?
        takers: dict[str, set[str]] = {}
        sources: dict[str, str] = {}
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            sources[path.stem] = text
            names: set[str] = set()
            current = ""
            for line in text.split("\n"):
                if _re.match(r"\s*def ", line):
                    current = line.strip().split("(")[0].replace("def ", "")
                elif "with _LOCK" in line and current:
                    names.add(current)
            takers[path.stem] = names

        offenders: list[str] = []
        for name, text in sources.items():
            lines = text.split("\n")
            for index, line in enumerate(lines):
                match = _re.search(r"with\s+([A-Za-z_][A-Za-z0-9_]*)\._LOCK\s*:", line)
                if not match:
                    continue
                owner = match.group(1)
                indent = len(line) - len(line.lstrip())
                helpers = takers.get(owner, set())
                if not helpers:
                    continue
                for follow in lines[index + 1:]:
                    if follow.strip() and (len(follow) - len(follow.lstrip())) <= indent:
                        break
                    for helper in sorted(helpers):
                        if "%s.%s(" % (owner, helper) in follow:
                            offenders.append("%s:%d calls %s.%s() while holding its lock"
                                             % (name + ".py", index + 1, owner, helper))
        self.assertEqual(offenders, [], "nested module lock: %s" % offenders)

    def test_inputs_lists_every_path(self):
        """Every path must appear, otherwise a Bluetooth phone is invisible again.

        The panel used to show only the Wi-Fi phones, so a phone feeding audio over
        Bluetooth was missing from the machine's own status even while the Bluetooth
        card showed it playing.
        """
        from audiomixing import bluetooth as bt_mod
        from audiomixing import config as config_mod
        from audiomixing import engine as engine_mod

        node = engine_mod.AudioMixingNode(config_mod.Config(), audio_port=free_udp_port(),
                                       discovery_port=free_udp_port(), simulate=True)
        try:
            node.devices = []
            real = bt_mod.cached_devices

            def fake():
                return [
                    {"name": "电话A", "link": "connected"},
                    {"name": "电话B", "link": "hfp"},
                    {"name": "电话C", "link": "off"},
                    {"name": "电话D", "link": "waiting"},
                ]

            bt_mod.cached_devices = fake
            try:
                inputs = node.inputs_state()
            finally:
                bt_mod.cached_devices = real
            paths = {item["path"] for item in inputs}
            names = {item["name"] for item in inputs}
            self.assertIn("bluetooth", paths, "a connected phone must be listed")
            self.assertIn("电话A", names, "connected phone missing")
            self.assertIn("电话B", names, "hands-free link must be listed too")
            self.assertNotIn("电话C", names, "an off phone must not be listed")
            self.assertNotIn("电话D", names, "a phone only being waited for is not sending")
            labels = {item["name"]: item["path_label"] for item in inputs}
            self.assertIn("通话通路", labels.get("电话B", ""),
                          "hands-free is a different, lower-quality path and must say so")
        finally:
            node.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestTodayRegressions(unittest.TestCase):
    """今天真实出过的问题，钉在这里，避免再犯。

    每一条都对应一次现场故障：
      * 删掉路由之后发送端还在跑 —— 界面上就是一行"残留"，对端收不到东西；
      * 同一块设备被广播和点对点各开一路采集 —— 两路抢设备，声音时有时无；
      * 默认播放后端被解析成 "file" —— 两台电脑的声音全被写进文件，一点声音都没有。
    """

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "self"
        cfg.routes = []
        cfg.broadcast = False
        cfg.listen = False
        self.node = AudioMixingNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
        self.node.devices = []

    def test_removing_a_route_stops_its_sender(self) -> None:
        """删掉路由后，sender.sessions 里不能再留着它。"""
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["*"], "gain": 1.0}],
                            persist=False)
        self.node.apply_routes()
        # 没有可用的对端，所以不会有会话；直接造一个假的来验证清理逻辑。
        class _Fake:
            host = "127.0.0.1"
            port = 47831
            node_id = "peer-x"
            source_name = "@default"
            started_at = 0.0

            def stats(self):
                return {"bytes": 0}

            def stop(self):
                pass

        self.node.sender.sessions["r1|peer-x"] = _Fake()
        self.node.apply_routes()
        self.assertNotIn("r1|peer-x", self.node.sender.sessions,
                         "路由被删后，它的发送端必须被停掉")

    def test_a_zero_byte_session_is_swept_even_if_a_route_still_mentions_it(self) -> None:
        """路由表里还留着同名记录时，零字节的会话也必须被清掉。

        现场就是这样：界面上挂着一行 0 B 的残留，路由列表里已经看不到它，
        但对端每次推路由表又会把它带回来，于是"路由不存在"这个条件永远不成立，
        清扫永远不动手。所以判定标准只能是"有没有数据"。
        """
        class _Fake:
            host = "127.0.0.1"
            port = 47831
            source_name = "@default"
            started_at = 0.0

            def stats(self):
                return {"bytes": 0}

            def stop(self):
                pass

        self.node.routes = [{"id": "keep", "from": ["self"], "to": ["*"], "gain": 1.0}]
        self.node.sender.sessions["keep|peer-z"] = _Fake()
        self.node._sweep_dead_senders()
        self.node._session_first_seen["keep|peer-z"] = time.time() - 30.0
        self.node._sweep_dead_senders()
        self.assertNotIn("keep|peer-z", self.node.sender.sessions,
                         "零字节的会话必须被清掉，哪怕路由表里还留着它的名字")

    def test_the_dead_session_sweep_removes_zero_byte_leftovers(self) -> None:
        """兜底清扫：路由已不存在、且零字节的会话必须被清掉。"""
        class _Fake:
            host = "127.0.0.1"
            port = 47831
            source_name = "@default"
            started_at = 0.0

            def stats(self):
                return {"bytes": 0}

            def stop(self):
                pass

        self.node.sender.sessions["gone|peer-y"] = _Fake()
        # 第一次看见它只是登记时间；清扫有 20 秒宽限期，所以这里把时间往前挪，
        # 再跑一次，模拟"它已经挂了很久"。
        self.node._sweep_dead_senders()
        self.assertIn("gone|peer-y", self.node.sender.sessions, "刚出现的会话给宽限期")
        self.node._session_first_seen["gone|peer-y"] = time.time() - 30.0
        self.node._sweep_dead_senders()
        self.assertNotIn("gone|peer-y", self.node.sender.sessions,
                         "零字节且路由不存在的会话必须被清扫掉")

    def test_two_readers_of_one_device_share_a_single_capture(self) -> None:
        """同一规格的采集只应打开一次，广播和点对点共用它。"""
        spec = {"backend": "null", "device": "silence"}
        first = self.node.hub.reader(spec, name="a")
        second = self.node.hub.reader(spec, name="b")
        self.assertIsNot(first, second, "读取器是每路一个包装")
        # 关键是底层采集只有一路：hub 按 (后端, 设备) 复用，两个读取者共用它。
        stats = self.node.hub.stats()
        names = [str(x.get("device") or x.get("endpoint") or "") for x in stats] \
            if isinstance(stats, list) else list(stats or {})
        self.assertEqual(len(stats), 1,
                         "同一设备只应有一路采集，否则广播和点对点会抢设备：%s" % (stats,))

    def test_opus_decoder_output_survives_the_receiver_check(self) -> None:
        """解码结果必须能通过接收端的判空。

        接收端写的是 if not samples: return。PCM 解码器返回列表没问题，
        但返回 numpy 数组时真值判断会抛异常，整包被丢弃 —— 现场就是
        "Opus 编码的点对点完全没声音"。这条测试直接守住那个写法。
        """
        from audiomixing.audio import opus as op

        if not op.available():
            self.skipTest("这台机器没有 Opus 库")
        enc = op.OpusEncoder(channels=2)
        dec = op.OpusDecoder(channels=2)
        samples = dec.decode(enc.encode([0.0] * 960 * 2))
        self.assertTrue(samples, "解码结果不能为空")
        self.assertEqual(len(samples), 960 * 2)
        truthy = True
        try:
            truthy = bool(not samples)
        except Exception as exc:      # numpy 数组会在这里抛
            self.fail("解码结果不能触发真值判断异常：%s" % exc)
        self.assertFalse(truthy)

    def test_opus_round_trip_compresses_a_lot(self) -> None:
        """Opus 往返：能压很多倍，而且还原出来的样本数对得上。

        这条测试守着"多一台设备就糊"的根因：未压缩 1536 KB/s，
        三台就吃满 Wi-Fi。Opus 之后每路十几 KB/s。
        """
        import numpy as np

        from audiomixing.audio import opus as op

        if not op.available():
            self.skipTest("这台机器没有 Opus 库：%s" % op.load_error())
        enc = op.OpusEncoder(channels=2)
        dec = op.OpusDecoder(channels=2)
        frames = 960
        tone = np.zeros(frames * 2, dtype=np.float32)
        packet = enc.encode(tone)
        self.assertLess(len(packet), frames * 2 * 2 / 4,
                        "一个 20 毫秒的包应该远小于原始 PCM")
        back = dec.decode(packet)
        self.assertEqual(len(back), frames * 2, "还原的样本数必须和输入一致")

    def test_a_shared_sink_still_describes_itself(self) -> None:
        """借用的播放端也要能 describe()。

        状态接口对 self.sinks 里每个对象都调 describe()。占位对象少了这个方法，
        /api/status 直接 500 —— 现场就是"一打开只听，监控页就报错"。
        """
        from audiomixing.engine import _SinkFeed

        class _Holder:
            route_id = "primary"
            thread = None

        feed = _SinkFeed("secondary", "设备名", _Holder())
        info = feed.describe()
        self.assertEqual(info["route_id"], "secondary")
        self.assertEqual(info["device_selector"], "设备名")
        self.assertIn("feeds_into", info)

    def test_listen_only_does_not_kill_ordinary_route_senders(self) -> None:
        """只听只该停掉广播通道的发送，不该停点对点。

        现场故障：发送端打开只听之后，点对点的声音凭空消失（对方什么都收不到）。
        原因是 desired_senders() 在只听模式下直接 return []，把所有路由的发送端
        一起关掉了。
        """
        self.node.registry.add_manual("127.0.0.1", 47990, "peer-1")
        self.node.config.listen = True
        self.node.config.broadcast = False
        self.node.set_routes([
            {"id": "p2p", "from": ["self"], "to": ["peer-1"]},
        ], persist=False)
        wanted = {key.split("|", 1)[0] for key, _t, _r in self.node.desired_senders()}
        self.assertIn("p2p", wanted, "只听模式下点对点的发送端必须保留")
        self.assertNotIn("channel", wanted, "只听模式下不该往广播通道发送")

    def test_one_capture_feeds_two_output_devices(self) -> None:
        """一份采集要能同时喂两个输出设备。

        需求是"一台电脑给两个耳机同时出声音"。结构上就是：采集中心给每个设备
        一个独立读取器（同一份采集），每个读取器配一个播放端。
        这里用两个 WAV 文件当播放端，跑一秒，两个文件都必须有数据。
        """
        import wave

        from audiomixing.audio.capture import CaptureHub
        from audiomixing.audio.format import CHANNELS, SAMPLE_RATE, make_tone
        from audiomixing.audio.multiout import MultiOut

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            a = os.path.join(tmp, "one.wav")
            b = os.path.join(tmp, "two.wav")
            hub = CaptureHub()
            out = MultiOut(hub)
            spec = {"backend": "file", "device": os.path.join(tmp, "src.wav"),
                    "loopback": False}
            # 音源文件：一秒 440Hz
            tone = make_tone(440.0, SAMPLE_RATE, 0.25, CHANNELS)
            with wave.open(spec["device"], "wb") as fh:
                fh.setnchannels(CHANNELS)
                fh.setsampwidth(2)
                fh.setframerate(SAMPLE_RATE)
                fh.writeframes(b"".join(__import__("struct").pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
                              for s in tone))
            try:
                out.configure(spec, [{"device": a, "gain": 1.0},
                                     {"device": b, "gain": 0.5, "delay_ms": 50.0}])
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    done = 0
                    for path in (a, b):
                        try:
                            if os.path.getsize(path) > 8000:
                                done += 1
                        except OSError:
                            pass
                    if done == 2:
                        break
                    time.sleep(0.1)
                stats = out.stats()
                self.assertEqual(len(stats["devices"]), 2, "应该有两路输出")
                sizes = []
                for path in (a, b):
                    try:
                        sizes.append(os.path.getsize(path))
                    except OSError:
                        sizes.append(0)
                self.assertTrue(all(s > 8000 for s in sizes),
                                "两个输出设备都应该收到数据，实际大小：%s" % sizes)
            finally:
                out.stop()
                # 采集中心没有 close：后台线程是守护线程，跟着进程退出即可

    def test_the_default_sink_is_a_real_sound_card(self) -> None:
        """默认播放后端必须是真实声卡，绝不能是写文件。"""
        from audiomixing.audio import base as audio_base

        spec = self.node.default_sink_spec()
        self.assertIn(spec["backend"], ("wasapi", "null", "coreaudio", "pipewire", "pulse"),
                      "默认播放不能落到 file 后端，否则声音会被写进文件")


class TestChannelSourceSafety(unittest.TestCase):
    """电脑端两个设计缺陷的回归测试（2026-10-12 用户实测）。

    现场一（★核心★）：`channel_sources=["M332BF"]`（那是他的【手机】）、
    `broadcast_excluded=["M332BF"]`、`broadcast=true` —— 唯一的音源被排除，
    广播永远没有音源、一个字节都不发；而界面上没有任何地方告诉他这件事。
    用户把"要广播给谁"理解成"音源是谁"，这是设计缺陷，不是用户的错。

    现场二：采集源是「扬声器 (UU远程虚拟音频设备)」，`blocks_read` 9512、
    `last_data_age_s` 145.5 —— 采了 95 秒之后就不再有数据，面板却写着"有数据"。

    这里钉住三件事：
      ① 网络上的另一台设备不能静默存成音源；
      ② 用不了的音源必须说清原因（网络节点 / 手机没在送 / 设备已拔出）；
      ③ 采集源空转、广播没有音源时，面板能拿到"一个字节都不会发"这句话。
    """

    UU = "扬声器 (UU远程虚拟音频设备)"

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.broadcast = True
        cfg.listen = False
        cfg.channel_sources = []
        cfg.broadcast_excluded = []
        self.node = AudioMixingNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
        self.node.devices = []
        self.api = AudioMixingApi(self.node)
        # 测试不能依赖"跑测试的这台机器上插了什么声卡/装了哪些虚拟声卡"：
        # 把本机设备清单固定成用例自己给的那一份。
        self._real_list_all = audio_base.list_all_devices
        audio_base.list_all_devices = lambda: list(self.node.devices)

    def tearDown(self) -> None:
        audio_base.list_all_devices = self._real_list_all
        try:
            self.node.stop()
        except Exception:
            pass

    # -- 小工具 -----------------------------------------------------------
    def _set_devices(self, *devices) -> None:
        self.node.devices = list(devices)
        self.node.devices_refreshed_at = time.time()

    @staticmethod
    def _source_device(name: str) -> audio_base.AudioDevice:
        return audio_base.AudioDevice("src-1", name, audio_base.KIND_SOURCE, "wasapi",
                                      "loopback", is_default=True)

    @staticmethod
    def _sink_device(name: str) -> audio_base.AudioDevice:
        return audio_base.AudioDevice("snk-1", name, audio_base.KIND_SINK, "wasapi",
                                      "render", is_default=True)

    def _add_peer(self, name: str) -> None:
        """让局域网里出现一台叫 name 的其它设备（真实场景：他的手机 M332BF）。"""
        self.node.registry.update(
            {"wl": 1, "id": "peer-" + name, "name": name, "audio_port": 47831,
             "web_port": 8080}, "198.51.100.10")

    def _fake_capture(self, name: str, blocks: int = 9512, age: float | None = 145.5,
                      alive: float = 253.8) -> None:
        """伪造采集端的实时计数，不真的去开声卡。"""
        self.node.hub.stats = lambda: [{          # type: ignore[method-assign]
            "name": name,
            "spec": {"backend": "wasapi", "device": name},
            "blocks_read": blocks,
            "frames_read": blocks * 480,
            "last_data_age_s": age,
            "alive_for_s": alive,
            "error": "",
        }]

    # -- 缺陷 1：音源不能是"网络上的另一台设备" -----------------------------
    def test_a_network_node_cannot_be_saved_as_a_channel_source(self) -> None:
        """PUT channel_sources 收下网络设备名 = 广播静默失效。必须拒绝并说人话。"""
        from audiomixing.webui import ApiError

        self._add_peer("M332BF")
        with self.assertRaises(ApiError) as ctx:
            self.api.set_settings({"channel_sources": ["M332BF"]})
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("M332BF", message)
        self.assertIn("是网络上的另一台设备，不是本机的音源", message)
        self.assertIn("送声音", message, "必须告诉用户正确的做法")
        self.assertEqual(ctx.exception.data.get("rejected"), ["M332BF"])
        self.assertEqual(self.node.config.channel_sources, [],
                         "不能静默存进配置 —— 存了就是一个字节都不发")

    def test_a_local_capture_device_is_still_accepted(self) -> None:
        """本机真实存在的采集设备必须照常能存（别把正常路径一起堵死）。"""
        speaker = "扬声器 (Senary Audio)"
        self._set_devices(self._sink_device(speaker), self._source_device(speaker))
        self.api.set_settings({"channel_sources": [speaker]})
        self.assertEqual(self.node.config.channel_sources, [speaker])

    def test_the_users_own_config_now_explains_itself(self) -> None:
        """用户那条真实配置：手机名当音源 + 被排除 —— 必须出现"用不了"的说明。"""
        self._add_peer("M332BF")
        self.node.config.channel_sources = ["M332BF"]
        self.node.config.broadcast_excluded = ["M332BF"]
        report = self.node.channel_sources_report()
        entry = report["sources"][0]
        self.assertEqual(entry["state"], "remote_node")
        self.assertTrue(entry["excluded"])
        self.assertIn("M332BF 是网络上的另一台设备，不是本机的音源", entry["reason"])
        self.assertFalse(report["will_send"])
        texts = [w["text"] for w in report["warnings"]]
        self.assertTrue(any("现在用不了" in t and "M332BF" in t for t in texts),
                        "面板必须有一条说清这条音源用不了：%s" % texts)

    def test_a_phone_that_stopped_sending_is_named_as_such(self) -> None:
        """手机之前送过声音、现在没在送 —— 要和"设备已拔出"分开报。"""
        from audiomixing.audio.backends import netmic as netmic_backend

        mic = netmic_backend.open_mic("M332BF")
        try:
            self.node.config.channel_sources = ["M332BF"]
            report = self.node.channel_sources_report()
            entry = report["sources"][0]
            self.assertEqual(entry["state"], "phone_offline")
            self.assertIn("没有在送", entry["reason"])
        finally:
            netmic_backend.close_mic("M332BF", mic)

    def test_an_unplugged_device_says_it_is_gone(self) -> None:
        """真正拔掉的设备：说"现在不是这台电脑上的采集设备"，不要猜成网络节点。"""
        self.node.config.channel_sources = ["麦克风 (USB 声卡)"]
        entry = self.node.channel_sources_report()["sources"][0]
        self.assertEqual(entry["state"], "gone")
        self.assertIn("不是这台电脑上的采集设备", entry["reason"])

    # -- 缺陷 2：采集到了但一直没有数据 -------------------------------------
    def test_a_stalled_capture_is_flagged_with_a_suggestion(self) -> None:
        """用户的真实数字：blocks_read 9512、145 秒没有新音频 —— 必须醒目报出来。"""
        self._set_devices(self._sink_device(self.UU), self._source_device(self.UU))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU)
        channel = self.node.channel_state()
        entry = channel["sources"][0]
        self.assertEqual(entry["state"], "stalled")
        self.assertIn("没有数据", entry["reason"])
        self.assertGreaterEqual(entry["last_data_age_s"], 10.0)
        self.assertFalse(channel["will_send"])
        capture_warn = [w for w in channel["warnings"] if w["scope"] == "capture"]
        self.assertTrue(capture_warn, "音源状态区域必须有一条醒目警告")
        advice = " ".join(w.get("advice", "") for w in capture_warn)
        self.assertIn("虚拟声卡空转", advice)
        # 广播真的没有音源时，要直说"一个字节都不会发"，而不是只给一个 0 B。
        bad = [w for w in channel["warnings"] if w["level"] == "bad"]
        self.assertTrue(any("广播没有音源，一个字节都不会发" in w["text"] for w in bad),
                        "面板必须说清一个字节都不会发：%s" % [w["text"] for w in bad])

    def test_the_loopback_suffix_does_not_hide_a_stalled_capture(self) -> None:
        """采集对象的名字会多一个「 [loopback]」（用户机器上就是这样）。

        面板不能因此找不到它的计数 —— 否则"空转"永远报不出来，这正是缺陷 2。
        """
        self._set_devices(self._sink_device(self.UU), self._source_device(self.UU))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU + " [loopback]")
        channel = self.node.channel_state()
        self.assertEqual(channel["sources"][0]["state"], "stalled")
        self.assertFalse(channel["will_send"])

    def test_a_capture_with_fresh_data_is_not_flagged(self) -> None:
        """有数据时不能乱报警 —— 否则用户学会忽略这条警告。"""
        self._add_peer("手机")   # 局域网上有人能收到，排除"没有可发对象"那条警告
        self._set_devices(self._sink_device(self.UU), self._source_device(self.UU))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU, age=0.4)
        channel = self.node.channel_state()
        self.assertEqual(channel["sources"][0]["state"], "ok")
        self.assertTrue(channel["will_send"])
        self.assertEqual(channel["warnings"], [])

    def test_every_source_excluded_is_visible(self) -> None:
        """全部音源都被排除时，面板要看得见，而不是静悄悄退回"跟随采集设备"。"""
        speaker = "扬声器 (Senary Audio)"
        self._set_devices(self._sink_device(speaker), self._source_device(speaker))
        self.node.config.channel_sources = [speaker]
        self.node.config.broadcast_excluded = [speaker]
        self._fake_capture(speaker, age=0.4)   # 回落到"跟随采集设备"时采的就是它
        report = self.node.channel_sources_report()
        self.assertTrue(report["sources"][0]["excluded"])
        self.assertIn("已被排除", report["sources"][0]["reason"])
        self.assertTrue(any("现在用不了" in w["text"] for w in report["warnings"]),
                        "被排除的音源必须在面板上说清楚")
        self.assertTrue(report["effective"], "面板要能看到广播实际在采哪一块设备")

    # -- 只听（频道收听）：对方还没被登记也要"准备好出声" -----------------
    def test_listening_builds_the_channel_sink_before_the_broadcaster_is_known(self) -> None:
        """★ 2026-10-16 用户第 6 条：「广播模式下。电脑开只听没有声音。」

        根因（探针复现）：`desired_sinks()` 原来对**所有**规则都要求"至少有一个在线对端"，
        而频道收听那条规则的 from 是通配 `["*"]` —— 收听方本来就不需要先认识广播方。
        于是对方还没被登记成在线设备时（手动填的 IP、信标丢了、路由器 AP 隔离），
        播放端一个都不建 → 开关亮着、声音一个字节也没有、面板上看不出原因。
        """
        self.node.config.broadcast = False
        self.node.config.listen = True
        self.node.registry._peers.clear()          # 谁都不在线
        wanted = self.node.desired_sinks()
        self.assertIn("channel", wanted, "开着只听就必须先建好频道的播放端（谁在发就听谁）")
        self.assertEqual(wanted["channel"]["peers"], [], "这时确实还没认出任何发送方")

    def test_audio_from_an_unknown_sender_still_reaches_the_channel_sink(self) -> None:
        """同上：解析不出身份的流（手动 IP / 信标丢了 / 路由器 AP 隔离）也要送进频道那个播放端。

        播放端只播"route id 命中"的那几路缓冲，所以这里必须把 channel 命中上；
        否则流到了也只能在日志里写一句 "has no local sink yet"，用户听到的是静音。
        """
        self.node.config.broadcast = False
        self.node.config.listen = True
        self.node.registry._peers.clear()
        self.assertEqual(self.node.routes_for_sender("从未见过的节点"), {"channel"})

    def test_a_named_route_wins_over_the_wildcard_so_audio_is_played_once(self) -> None:
        """★ 我既开着「听广播」又点名收这一台时，只走具名那条 —— 否则同一声声音放两遍（音量翻倍）。

        （手机端那句说明写着同一件事：「同一台设备的声音如果从两条路都进来，只会播一路、不叠加」。）
        """
        self.node.config.broadcast = False
        self.node.config.listen = True
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机", "audio_port": 47831,
                                   "web_port": 0}, "198.51.100.10")
        self.node.set_routes([{"id": "named", "from": ["peer-1"],
                               "to": [self.node.config.node_id]}], persist=False)
        self.assertEqual(self.node.routes_for_sender("peer-1"), {"named"},
                         "具名路由命中时不要再把频道那条也算上")
        # 认不出身份的发送方（别人的流）照旧落频道那条 —— 通配那条不会因此失效
        self.assertEqual(self.node.routes_for_sender("谁都不是"), {"channel"})

    def test_a_broadcast_session_never_asks_the_peer_to_listen(self) -> None:
        """★ 2026-10-16 用户钉死的广播语义：「广播是和路由独立开的…不能关闭或者开启其他设备的听说…就像 FM 广播一样」。

        所以电脑开着「广播我的声音」时，那条频道会话**不许**再报 want_listen —— 那是
        "我在针对性送给你、请你自动打开收听"，它会替对端自动开出一条收听路由（= 替别人动"听"）。
        音频照发（另一条测试盯着同一条线上"没人听也不会静音"），这里只管控制面这一个字段。
        （点对点路由照旧报 want_listen —— 那是用户点名要的那一台，两端自动接通是既有约定。）
        """
        import socket as _socket

        class _FakeSession:
            host = "127.0.0.1"
            port = 0
            node_id = "peer-1"
            route_id = "channel|peer-1"
            packets_sent = 42
            bytes_sent = 80640

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.3)
        _FakeSession.port = sock.getsockname()[1]
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机", "audio_port": 47831,
                                   "web_port": 0}, "198.51.100.10")
        self.node.sender.sessions["channel|peer-1"] = _FakeSession()   # type: ignore[assignment]
        self.node.routes = []
        self.node.receiver._sock = sock
        self.node._send_peer_stats(force=True)
        try:
            data, _addr = sock.recvfrom(4096)
        finally:
            sock.close()
        body = proto.decode(data).json()
        self.assertFalse(body["want_listen"],
                         "广播这条线上不许请求对端自动收听（那是替别人开关『听』）")
        # 换成点对点那条 + 这张设备开着「智连」：必须请对端自动收听
        # （★ 2026-10-19 用户口径：智连没开就不许自动开关对端的「听」，所以这里要开着才验得出来）
        _FakeSession.route_id = "r1"
        # ★ 2026-11-04（A 硬规则）：广播模式不允许和智连组合；要点对点验"请对端自动收听"，
        #   必须先关广播。广播开着时 want_listen 必须为 False（见上一条断言）。
        self.node.config.broadcast = False
        self.node.config.smart_link = ["peer-1"]
        sock2 = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock2.bind(("127.0.0.1", 0))
        sock2.settimeout(0.3)
        _FakeSession.port = sock2.getsockname()[1]
        self.node.sender.sessions["r1|peer-1"] = _FakeSession()        # type: ignore[assignment]
        self.node.sender.sessions.pop("channel|peer-1", None)
        self.node.routes = [{"id": "r1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.0}]
        self.node.receiver._sock = sock2
        self.node._send_peer_stats(force=True)
        try:
            data2, _addr2 = sock2.recvfrom(4096)
        finally:
            sock2.close()
        self.assertTrue(proto.decode(data2).json()["want_listen"],
                        "点对点那条仍然要请对端自动收听（既有约定，用户点名要过）")


    def test_stats_go_to_a_peer_we_only_listen_to(self) -> None:
        """★ 2026-11-01 用户截图：电脑只在听手机时，两端音量条 1.00 / 0.59 对不上。

        根因：`_send_peer_stats` 原来只遍历"我正在送声音的会话"，电脑单纯收听时
        一个统计包都不发 —— 手机永远收不到电脑的 `gain_rx`，它的「我送它的」
        就只能停在本地值。手机端一直会对"收听目标"回话，电脑这里补齐。
        """
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.3)
        port = sock.getsockname()[1]
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": port, "web_port": 0}, "127.0.0.1")
        self.node.sender.sessions.clear()
        self.node.routes = [{"id": "in1", "from": ["peer-1"],
                             "to": [self.node.config.node_id], "gain": 0.75}]
        self.node.receiver._sock = sock
        try:
            self.node._send_peer_stats(force=True)
            data, _addr = sock.recvfrom(4096)
        finally:
            sock.close()
        body = proto.decode(data).json()
        self.assertFalse(body["sending"], "单纯收听时不能谎报『我在送声音』")
        self.assertAlmostEqual(float(body["gain_rx"]), 0.75, places=3,
                               msg="电脑的『我听它的』必须报给手机，否则两端音量条永远对不上")

    def test_stats_go_to_a_peer_with_a_route_but_no_audio_yet(self) -> None:
        """★ 2026-11-01「电脑端修改音量手机不同步」的另一个成因。

        `_send_peer_stats` 原来的收件人只来自 `sender.sessions`，且要求
        `packets_sent > 0`。用户先调音量、后开声音（或采集刚好空转）时，这条
        唯一携带 `gain` 的消息一个都不发，手机只能停在旧值 / 滑杆写「—」。
        路由存在就已经说明"这条链路存在" —— 必须把共享增益报过去。
        """
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.3)
        port = sock.getsockname()[1]
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": port, "web_port": 0}, "127.0.0.1")
        self.node.sender.sessions.clear()          # 还没有任何发送会话
        self.node.routes = [{"id": "out1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.56}]
        self.node.receiver._sock = sock
        try:
            self.node._send_peer_stats(force=True)
            data, _addr = sock.recvfrom(4096)
        finally:
            sock.close()
        body = proto.decode(data).json()
        self.assertFalse(body["sending"], "没发出过音频时不能谎报『正在送』")
        self.assertAlmostEqual(float(body["gain"]), 1.56, places=3,
                               msg="电脑的『我送它的』必须在还没出声时也能同步给手机")

    def test_a_remote_gain_burst_lands_on_the_last_value(self) -> None:
        """★ 2026-11-01 真机自测：快速拖动手机滑杆，电脑停在中间值 1.45，松手值 0.51 丢了。

        原因：`_apply_shared_gain` 采纳一次**远端**值后也写 `_gain_local_at`，
        接下来 1.2 秒的远端更新全被"本机用户正在拖"那条 hold 挡掉。
        这条 hold 只该由本机 UI 真正拖动（`note_local_gain_change`）写入。
        """
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "web_port": 0}, "198.51.100.10")
        self.node.routes = [{"id": "out1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.56}]
        # 模拟一次拖动：远端连续发来中间值，最后松手在 0.50
        for value in (1.45, 0.50):
            self.node._apply_shared_gain("peer-1", "out", value, 1)
        rule = [r for r in self.node.routes if str(r.get("id")) == "out1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 0.50, places=3,
                               msg="一次拖动的最后一个值必须落地，不能被 1.2 秒 hold 挡掉")

    def test_a_mirror_echo_never_undoes_the_local_volume_change(self) -> None:
        """★ 2026-11-01 用户：「电脑端调节音量条手机端不同步」（电脑自己还弹回旧值）。

        对端每 2 秒把它那份"我听它的"（gain_rx）照回来 —— 那就是本机「我送它的」的
        **镜像回声**。旧逻辑按"最后到达的为准"采纳，于是电脑刚拖到 1.05，下一包回声
        1.30 又把路由改回 1.30；手机端看到的永远是旧值。
        这里钉住两件事：
          · 最近报出去的那个值回来 → 只当回声，不采纳；
          · 真正不同的值（对端用户自己拖的，例如 0.51）仍然照常落地。
        """
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "web_port": 0}, "198.51.100.10")
        self.node.routes = [{"id": "out1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.30}]
        # 本机曾经把 1.30 报给对端（_link_gains 会记下"最近报出去的值"）
        self.node._link_gains("peer-1")
        # 本机用户刚把滑杆拖到 1.05，而且已经过了 1.2 秒防抢 hold
        self.node.note_local_gain_change("out1")
        self.node._gain_local_at["out1"] = time.monotonic() - 2.0
        self.node.routes = [{"id": "out1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.05}]
        self.node._apply_shared_gain("peer-1", "out", 1.30, 1)
        rule = [r for r in self.node.routes if str(r.get("id")) == "out1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 1.05, places=3,
                               msg="对端照回来的旧值（镜像回声）不能把本机刚改的音量顶回去")
        # 对端用户真的拖到 0.51：这个值本机没报过，必须照常采纳
        self.node._apply_shared_gain("peer-1", "out", 0.51, 2)
        rule = [r for r in self.node.routes if str(r.get("id")) == "out1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 0.51, places=3,
                               msg="对端用户改的新值仍然要同步过来")

    def test_the_panel_volume_slider_stamps_a_local_change(self) -> None:
        """拖面板滑杆走的是 /api/routes/update —— 它必须记"本机刚动过"。

        旧实现只在 /api/routes（整表保存）里记，而且那里的 `route_id` 根本没定义
        （payload 一 gain 就 NameError）。结果：电脑拖完音量没有任何本地保护，
        对端下一包回声就把值改回去。
        """
        self.node.routes = [{"id": "out1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.30}]
        result = self.api.update_route({"id": "out1", "gain": 1.05})
        self.assertTrue(result["ok"])
        self.assertIn("out1", self.node._gain_local_at,
                      "面板拖滑杆必须写本地 hold（否则对端可以立刻抢回音量）")
        rule = [r for r in self.node.routes if str(r.get("id")) == "out1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 1.05, places=3)

    def test_smart_capable_peer_decides_the_request_not_this_pc(self) -> None:
        """★ 2026-11-01 用户：手机开了智连、电脑没开，电脑送声音时手机不会自动收听。

        根因：`want_listen` 被**电脑自己的**「智连」挡掉了。新手机信标里有 `smart:1`
        → 请求必须照发，由手机自己的开关决定（请求 ≠ 命令）；旧手机没这个键时
        继续用保守口径（本机没开智连就不发），免得它收到就替自己打开收听。
        """
        import socket as _socket

        class _FakeSession:
            host = "127.0.0.1"
            port = 0
            node_id = "peer-1"
            route_id = "r1"
            packets_sent = 42
            bytes_sent = 80640

        def send_once(peer_payload: dict) -> bool:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.3)
            _FakeSession.port = sock.getsockname()[1]
            self.node.registry.update(peer_payload, "198.51.100.10")
            self.node.receiver._sock = sock
            self.node._send_peer_stats(force=True)
            try:
                data, _addr = sock.recvfrom(4096)
            finally:
                sock.close()
            return bool(proto.decode(data).json().get("want_listen"))

        self.node.config.smart_link = []            # 电脑这张卡片没开智连
        self.node.routes = [{"id": "r1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 1.0}]
        self.node.sender.sessions["r1|peer-1"] = _FakeSession()   # type: ignore[assignment]
        self.assertTrue(send_once({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": _FakeSession.port, "web_port": 0,
                                   "smart": 1}),
                        "对端支持智连时，请求要照发 —— 收不收由它自己决定")
        self.assertFalse(send_once({"wl": 1, "id": "peer-1", "name": "手机",
                                    "audio_port": _FakeSession.port, "web_port": 0,
                                    "smart": 0}),
                         "旧对端没有智连能力时，本机没开智连就不该请求（保守口径）")

    def test_without_listening_an_unknown_sender_is_not_played(self) -> None:
        """反向：没开「只听」时，认不出身份的流绝不能被随便放进播放端（那是乱收别人的声音）。"""
        self.node.config.broadcast = False
        self.node.config.listen = False
        self.node.registry._peers.clear()
        self.assertEqual(self.node.routes_for_sender("从未见过的节点"), set())

    # -- 面板脚本：同名函数不能再来一份（正是它让定时刷新失效的） -----------
    def test_the_page_defines_the_source_picker_once(self) -> None:
        """index.html 里 `loadChannelSources` 只能有一份定义。

        以前有两份（后一份覆盖前一份），于是"每 4 秒刷新一次、手机一开始送声音
        就会出现在列表里"的那份变成死代码 —— 页面上看不出任何异常。
        """
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("async function loadChannelSources("), 1)
        # 候选清单必须按"现在真的能采到"过滤（netmic 只在正在送声音时才算设备）
        self.assertIn("x.extra && x.extra.live", html)


class TestCustomPorts(unittest.TestCase):
    """v2.10：自定义端口只出现在「设置」页；保存只落盘，重启才生效。"""

    def setUp(self) -> None:
        import tempfile as _tempfile

        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        cfg = Config()
        cfg.web_port = 17800
        cfg.audio_port = 47831
        cfg.discovery_port = 47830
        self.node = AudioMixingNode(cfg, audio_port=47831, discovery_port=47830)
        self.node.devices = []
        self.api = AudioMixingApi(self.node)
        self.cfg_file = _mkdtemp(prefix="am-ports-") / "config.json"
        # 注入保存路径：测试不能写进用户真正的 config.json
        self.api.config_save_path = self.cfg_file
        self._real_list_all = audio_base.list_all_devices
        audio_base.list_all_devices = lambda: []

    def tearDown(self) -> None:
        audio_base.list_all_devices = self._real_list_all
        try:
            self.node.stop()
        except Exception:
            pass

    def test_legacy_default_name_migrates_to_hostname(self) -> None:
        """★ 2026-11-02 用户：电脑在别人设备上显示成「本机」。

        老配置把默认名存成了「本机」；新默认是主机名（2024TSJ）。
        只有"从没改过"的旧默认值才迁移，用户自己起的名字必须原样保留。
        """
        import json as _json

        from audiomixing.config import Config, default_node_name

        def load_with(name: str) -> str:
            p = _mkdtemp(prefix="am-name-") / "config.json"
            p.write_text(_json.dumps({"name": name, "node_id": "n1",
                                      "discovery_port": 17830,
                                      "audio_port": 47831, "web_port": 17800},
                                     ensure_ascii=False), encoding="utf-8")
            cfg = Config.load(p)
            saved = _json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(saved["name"], cfg.name, "迁移必须落盘，下一次启动才是新名字")
            return cfg.name

        host = default_node_name()
        self.assertEqual(load_with("本机"), host, "旧默认「本机」必须迁成主机名")
        self.assertEqual(load_with("我的电脑"), host, "旧默认「我的电脑」必须迁成主机名")
        self.assertEqual(load_with("客厅电脑"), "客厅电脑", "用户自己起的名字一律不动")

    def test_old_default_discovery_port_migrates_but_custom_stays(self) -> None:
        """★ 2026-11-01 用户：两端默认发现端口改为 17830。

        老配置里的 47830 是"从没改过"的默认值 → 升级时自动搬；用户自己填的
        17930 之类不能动，否则会把人家特意避开冲突的设置改掉。
        """
        import json as _json
        import tempfile as _tempfile

        from audiomixing.config import Config

        def load_with(port: int) -> int:
            p = _mkdtemp(prefix="am-mig-") / "config.json"
            p.write_text(_json.dumps({"discovery_port": port, "audio_port": 47831,
                                      "web_port": 17800}), encoding="utf-8")
            cfg = Config.load(p)
            saved = _json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(saved["discovery_port"], cfg.discovery_port,
                             "迁移必须落盘，下一次启动才是新默认")
            return cfg.discovery_port

        self.assertEqual(load_with(47830), 17830, "旧默认必须迁到 17830")
        self.assertEqual(load_with(17930), 17930, "用户自定义端口一律不动")

    def test_ports_are_saved_and_need_restart(self) -> None:
        result = self.api.set_settings({"web_port": 18001, "audio_port": 18031,
                                        "discovery_port": 18030})
        self.assertTrue(result["restart_required"],
                        "端口是启动时绑死的，保存后必须如实返回'要重启'")
        self.assertFalse(result["restart_scheduled"],
                         "源码/测试模式绝不能自动重启调用者进程")
        self.assertEqual(result["restart_blocked"], "not-enabled")
        self.assertIn("手动重启", result["restart_note"])
        # 运行中的这份不能改：信标/横幅还在用旧端口，改了就会广播一个没在听的端口
        self.assertEqual(self.node.config.web_port, 17800)
        self.assertEqual(result["pending_ports"]["web_port"], 18001)
        import json as _json
        saved = _json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["web_port"], 18001)
        self.assertEqual(saved["audio_port"], 18031)
        self.assertEqual(saved["discovery_port"], 18030)

    def test_saving_the_same_ports_does_not_restart(self) -> None:
        """★ 2026-11-01：页面每次保存都会带三个端口；原样点一下不能把程序重启一遍。"""
        result = self.api.set_settings({"web_port": 17800, "audio_port": 47831,
                                        "discovery_port": 47830})
        self.assertFalse(result["restart_required"])
        self.assertFalse(result["restart_scheduled"])
        self.assertEqual(result["changed"], [], "端口没变时不要报成已修改")
        self.assertEqual(result["restart_note"], "端口没有变化。")

    def test_port_args_are_dropped_for_the_restart_command_line(self) -> None:
        """自动重启时 `serve` 的命令行必须去掉三个端口，改从新配置读。"""
        from audiomixing.webui import AudioMixingApi

        self.assertEqual(
            AudioMixingApi._strip_port_args(
                ["serve", "--host", "127.0.0.1", "--web-port", "50930",
                 "--audio-port=50931", "--discovery-port", "50932", "--simulate"]),
            ["serve", "--host", "127.0.0.1", "--simulate"])

    def test_the_restart_helper_is_reachable_when_frozen(self) -> None:
        """★ 2026-11-01 真机自测抓到的回归：`_is_frozen` 是 AudioMixingApi 的方法。

        写成 `WebUI._is_frozen()` 会 AttributeError，被 except 吞成 `not-frozen`，
        表现就是"端口保存成功但从不自动重启"。这里把 frozen 伪装成 True，
        并 mock 掉真正拉起进程 / 退出进程的两步，只钉住判断链。
        """
        import sys as _sys
        from unittest import mock

        from audiomixing.webui import AudioMixingApi

        api = AudioMixingApi(self.node, restart_argv=["serve", "--host", "127.0.0.1"])
        api._is_frozen = lambda: True          # type: ignore[assignment]
        with mock.patch("audiomixing.tray._real_exe", return_value=_sys.executable), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch("threading.Thread") as thread:
            ok, blocked = api._schedule_restart()
        self.assertTrue(ok, "打包形态 + 给了 restart_argv 时必须能调度重启")
        self.assertEqual(blocked, "")
        popen.assert_called_once()
        script = popen.call_args[0][0][-1]
        self.assertIn("Start-Process", script)
        thread.assert_called_once()

    def test_port_equal_to_web_plus_one_is_rejected(self) -> None:
        from audiomixing.webui import ApiError

        with self.assertRaises(ApiError) as ctx:
            self.api.set_settings({"audio_port": 17801})   # 面板端口 +1 是手机长连接口
        self.assertEqual(ctx.exception.status, 400)

    def test_out_of_range_port_is_rejected(self) -> None:
        from audiomixing.webui import ApiError

        with self.assertRaises(ApiError) as ctx:
            self.api.set_settings({"web_port": 80})
        self.assertEqual(ctx.exception.status, 400)

    def test_unwritable_config_is_reported_not_faked(self) -> None:
        """磁盘写不进去时必须报错，不能回一个“已保存”。"""
        from audiomixing.webui import ApiError

        blocker = _mkdtemp(prefix="am-blocked-") / "blocker"
        blocker.write_text("x", encoding="utf-8")
        self.api.config_save_path = blocker / "config.json"   # 父路径是个文件
        with self.assertRaises(ApiError) as ctx:
            self.api.set_settings({"web_port": 18002})
        self.assertEqual(ctx.exception.status, 500)
        # 运行中的端口不能被改坏
        self.assertEqual(self.node.config.web_port, 17800)

    def test_launcher_reads_port_from_config(self) -> None:
        """启动器不能还写死 17800：改端口后再双击必须找到同一个实例。"""
        from audiomixing.config import Config

        home = _mkdtemp(prefix="am-home-")
        cfg = Config()
        cfg.web_port = 18010
        cfg.save(home / "config.json")
        old = os.environ.get("AUDIOMIXING_HOME")
        os.environ["AUDIOMIXING_HOME"] = str(home)
        try:
            import audiomixing_launcher as launcher
            self.assertEqual(launcher._configured_web_port(), 18010)
        finally:
            if old is None:
                os.environ.pop("AUDIOMIXING_HOME", None)
            else:
                os.environ["AUDIOMIXING_HOME"] = old


def _free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _free_tcp_pair() -> int:
    for _ in range(80):
        port = _free_tcp_port()
        if port >= 65534:
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise RuntimeError("找不到连续两个空闲 TCP 端口")


class TestHttpSecurity(unittest.TestCase):
    """v2.11：面板不再对任意网站开放；写接口只认同源；输出做转义。"""

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import WebUI

        self._real_list_all = audio_base.list_all_devices
        audio_base.list_all_devices = lambda: []
        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        self.node = AudioMixingNode(cfg, audio_port=_free_tcp_port(),
                                    discovery_port=_free_tcp_port(), simulate=True)
        self.node.devices = []
        self.ui = WebUI(self.node, host="127.0.0.1", port=_free_tcp_pair())
        self.port = self.ui.start()
        self.base = "http://127.0.0.1:%d" % self.port

    def tearDown(self) -> None:
        try:
            self.ui.stop()
        except Exception:
            pass
        try:
            self.node.stop()
        except Exception:
            pass
        audio_base.list_all_devices = self._real_list_all

    def _request(self, method: str, path: str, body=None, origin: str | None = None,
                 raw: bytes | None = None, ctype: str = "application/json"):
        data = raw if raw is not None else (None if body is None
                                            else json.dumps(body).encode("utf-8"))
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", ctype)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_no_cors_and_no_python_version_in_headers(self) -> None:
        status, headers, _ = self._request("GET", "/plain")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"),
                          "不能再让任意网站读到面板")
        self.assertEqual(headers.get("Server"), "AudioMixing",
                         "Server 头不能泄露 Python 版本")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_options_does_not_allow_cross_origin(self) -> None:
        status, headers, _ = self._request("OPTIONS", "/api/settings",
                                           origin="http://evil.example")
        self.assertEqual(status, 204)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_cross_origin_post_is_rejected(self) -> None:
        status, _, _ = self._request("POST", "/api/settings", {"codec": "pcm_s16"},
                                     origin="http://evil.example")
        self.assertEqual(status, 403)

    def test_same_origin_post_is_allowed(self) -> None:
        status, _, body = self._request("POST", "/api/settings", {"codec": "pcm_s16"},
                                        origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_plain_page_escapes_node_name(self) -> None:
        self.node.name = "<img src=x onerror=alert(1)>"
        status, _, body = self._request("GET", "/plain")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("&lt;img", text)
        self.assertNotIn("<img src=x", text)

    def test_firewall_get_is_rejected(self) -> None:
        # 以前一个 <img src=".../api/firewall"> 就能弹 UAC；现在只收 POST。
        with mock.patch("audiomixing.tray.add_firewall_rules") as called:
            status, _, _ = self._request("GET", "/api/firewall")
            self.assertEqual(status, 405)
            called.assert_not_called()

    def test_mic_stream_rejects_absurd_rate(self) -> None:
        status, _, _ = self._request("POST", "/api/mic/stream?name=phone&rate=999999",
                                     raw=b"\x00\x00", ctype="application/octet-stream")
        self.assertEqual(status, 400)

    def test_foreign_host_is_rejected(self) -> None:
        """DNS rebinding：域名解析到 127.0.0.1 时 Host 仍是恶意域名，必须拒绝。"""
        req = urllib.request.Request(self.base + "/healthz")
        req.add_unredirected_header("Host", "evil.example")
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 403)


class TestNetMicLimits(unittest.TestCase):
    """名字/采样率/槽位都要有上限 —— 否则局域网一台机器就能把内存刷起来。"""

    def test_name_and_rate_are_clamped(self) -> None:
        from audiomixing.audio.backends import netmic

        mic = netmic.open_mic("x" * 200, 10 ** 9)
        try:
            self.assertEqual(len(mic.name), 64)
            self.assertEqual(mic.device_rate, 48000)
        finally:
            netmic.close_mic(mic.name, mic)

    def test_slot_count_is_capped(self) -> None:
        from audiomixing.audio.backends import netmic

        for m in list(netmic.MICS.values()):
            netmic.close_mic(m.name, m)
        for i in range(netmic.MAX_MICS):
            netmic.open_mic("填充音源-%d" % i)
        with self.assertRaises(ValueError):
            netmic.open_mic("再多一个音源")
        for m in list(netmic.MICS.values()):
            netmic.close_mic(m.name, m)


class TestQuickStartAndVersion(unittest.TestCase):
    """「🚀 快速上手」四步自检 + 采集源空转自动回退 + 版本可见性（2026-10-12 第二轮）。

    用户的五个真实失败没有一个是他操作错，全是"没人告诉你怎么配"：
      ① 电脑在采一块空转的虚拟声卡（145 秒没有数据）—— 面板只显示 0 B；
      ② 广播音源被设成一台手机 —— 静默失效、0 字节；
      ③ 桌面上三个 exe，他跑的是 7 小时前的 v1.2 —— 所有修复都不在里面。

    这一组钉住的是"第一次用必然成功"：
      · 向导四步的结论必须和人看到的一致，并且每步都能一键修；
      · 采集源真的没数据时自动换到一块**试采确实收到过音频**的设备，并明确告知；
      · 面板要显示版本号与构建时间，发现更新的 exe 要提醒。
    """

    UU = "扬声器 (UU远程虚拟音频设备)"
    SPEAKER = "扬声器 (Realtek(R) Audio)"

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "2024TSJ"
        cfg.routes = []
        cfg.broadcast = True
        cfg.listen = False
        cfg.local = True
        cfg.channel_sources = []
        cfg.broadcast_excluded = []
        self.node = AudioMixingNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
        self.node.devices = []
        self.api = AudioMixingApi(self.node)
        self._real_list_all = audio_base.list_all_devices
        audio_base.list_all_devices = lambda: list(self.node.devices)
        self._real_probe = self.node._probe_capture

    def tearDown(self) -> None:
        audio_base.list_all_devices = self._real_list_all
        self.node._probe_capture = self._real_probe          # type: ignore[method-assign]
        try:
            self.node.stop()
        except Exception:
            pass

    # -- 小工具 -----------------------------------------------------------
    def _set_devices(self, *devices) -> None:
        self.node.devices = list(devices)
        self.node.devices_refreshed_at = time.time()

    @staticmethod
    def _source_device(name: str) -> audio_base.AudioDevice:
        return audio_base.AudioDevice("src-1", name, audio_base.KIND_SOURCE, "wasapi",
                                      "loopback", is_default=True)

    @staticmethod
    def _sink_device(name: str, default: bool = False) -> audio_base.AudioDevice:
        return audio_base.AudioDevice("snk-" + name[:6], name, audio_base.KIND_SINK, "wasapi",
                                      "render", is_default=default)

    def _add_peer(self, name: str, host: str = "198.51.100.10") -> None:
        self.node.registry.update(
            {"wl": 1, "id": "peer-" + name, "name": name, "audio_port": 47831,
             "web_port": 8080}, host)

    def _fake_capture(self, name: str, blocks: int = 0, age: float | None = None,
                      alive: float = 0.5) -> None:
        self.node.hub.stats = lambda: [{          # type: ignore[method-assign]
            "name": name + " [loopback]",
            "spec": {"backend": "wasapi", "device": name, "loopback": True},
            "blocks_read": blocks,
            "frames_read": blocks * 480,
            "last_data_age_s": age,
            "alive_for_s": alive,
            "error": "",
        }]

    def _mark_dead_for(self, name: str, seconds: float) -> None:
        """把"这块设备已经连续 N 秒没数据"直接写进引擎的记录里（跨采集重建那份）。"""
        self.node._dead_since[self.node._device_name_key(name)] = time.monotonic() - seconds

    def _stub_probe_alive(self, alive: bool = True) -> list[str]:
        """替掉真开设备的试采：记录被试采过谁，并按 alive 返回。"""
        tried: list[str] = []

        def fake(name, timeout=2.0):
            tried.append(name)
            return {"alive": alive, "blocks": 3 if alive else 0, "frames": 1440,
                    "peak": 0.08 if alive else 0.0, "error": "", "seconds": 0.1}

        self.node._probe_capture = fake                 # type: ignore[method-assign]
        return tried

    # -- 第 1 步：采集源到底有没有声音？ -----------------------------------
    def test_the_guide_reports_a_stalled_capture(self) -> None:
        """用户现场：采的是空转的虚拟声卡 —— 向导必须说"采不到声音"，并给一键换。"""
        self._set_devices(self._sink_device(self.UU, default=True), self._source_device(self.UU),
                          self._sink_device(self.SPEAKER))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU)
        self._mark_dead_for(self.UU, 145.5)
        guide = self.api.guide()
        step1 = guide["steps"][0]
        self.assertEqual(step1["state"], "bad")
        self.assertIn("采不到声音", step1["text"])
        self.assertIn(self.UU, step1["text"])
        apis = {a["api"] for a in step1["actions"]}
        self.assertIn("/api/guide/fix", apis, "第 1 步必须给「一键修」按钮")

    def test_the_guide_says_it_works_when_everything_is_green(self) -> None:
        """四步都过时，必须给一句明确的结论（用户要的就是这句"可以用了"）。"""
        self._add_peer("M332BF")
        self._set_devices(self._sink_device(self.SPEAKER, default=True),
                          self._source_device(self.SPEAKER))
        self.node.config.channel_sources = [self.SPEAKER]
        self.node.config.source_device = self.SPEAKER
        self._fake_capture(self.SPEAKER, blocks=900, age=0.2)
        guide = self.api.guide()
        states = [s["state"] for s in guide["steps"][:3]]
        self.assertEqual(states, ["ok", "ok", "ok"], guide["steps"][:3])
        self.assertTrue(guide["ready"])
        self.assertIn("可以用了 ✓", guide["summary"])

    def test_the_phone_listen_address_skips_virtual_adapters(self) -> None:
        """★ 2026-10-26：快速上手第 4 步给的地址必须是手机真能连上的那块网卡。

        现场报告里给的是 172.18.x.x（WSL/Hyper-V 虚拟网卡），手机打不开；
        真实局域网是 192.168.137.x。排序口径必须和二维码/面板一致。
        """
        from unittest import mock

        from audiomixing.net import discovery as disc

        report = {"interfaces": [
            {"address": "172.18.0.1", "kind": "private"},
            {"address": "192.168.1.10", "kind": "private"},
        ]}
        with mock.patch.object(disc, "lan_report", return_value=report):
            url = self.api.phone_listen_url()
        self.assertEqual(url, "http://192.168.1.10:17800/listen", url)

    def test_panel_banner_uses_the_best_address_not_interface_zero(self) -> None:
        """★ 2026-11-06（真实截图自查）：横幅原来取 interfaces[0]。

        这台机器第一项是 Hyper-V/WSL 的 172.23.128.1，界面把它印成
        "别的电脑/手机用这个网址打开"，手机连不上；服务端必须下发 preferred。
        """
        from audiomixing.webui import _network_payload

        report = {"interfaces": [
            {"address": "172.23.128.1", "kind": "private"},
            {"address": "192.168.0.105", "kind": "private"},
        ]}
        node = types.SimpleNamespace(
            registry=types.SimpleNamespace(snapshot=lambda: []),
            config=types.SimpleNamespace(manual_peers=[]))
        payload = _network_payload(node, report)
        self.assertEqual(payload["preferred"], "192.168.0.105")
        self.assertEqual(len(payload["interfaces"]), 2)

    def test_startup_banner_lists_the_reachable_address_first(self) -> None:
        """★ 2026-11-06（真实启动自查）：控制台横幅也照 interfaces[0] 打印。

        真实输出里第一行是 `http://172.23.128.1:...`（WSL/Hyper-V），手机连不上；
        `--open-browser` 还会直接打开这个死地址。启动横幅必须把推荐地址排第一。
        """
        from audiomixing import cli

        report = {"interfaces": [
            {"address": "172.23.128.1", "kind": "private"},
            {"address": "192.168.0.105", "kind": "private"},
            {"address": "26.60.122.130", "kind": "public"},
        ]}
        node = types.SimpleNamespace(
            registry=types.SimpleNamespace(snapshot=lambda: []),
            config=types.SimpleNamespace(manual_peers=[]))
        ips = cli._ordered_lan_ips(node, report)
        self.assertEqual(ips[0], "192.168.0.105", ips)
        self.assertNotIn("26.60.122.130", ips, "非局域网地址不许进横幅")
        self.assertEqual(len(ips), 2)

    def test_self_host_prefers_the_subnet_a_known_peer_is_on(self) -> None:
        """★ 2026-11-03（Q1）：本机地址不能取 interfaces[0]（Hyper-V 的 172.26.x）。

        有对端时，和它对端同 /24 的那块网卡才是手机真正能到达的本机地址。
        """
        from audiomixing.webui import _best_local_address

        report = {"interfaces": [
            {"address": "172.26.192.1", "subnet": "172.26.192.0/24", "kind": "private"},
            {"address": "192.168.0.111", "subnet": "192.168.0.0/24", "kind": "private"},
        ]}
        node = types.SimpleNamespace(
            registry=types.SimpleNamespace(snapshot=lambda: [{"host": "192.168.0.100"}]),
            config=types.SimpleNamespace(manual_peers=[]))
        self.assertEqual(_best_local_address(node, report), "192.168.0.111")

    def test_self_host_falls_back_to_the_wired_private_range_without_peers(self) -> None:
        """没有对端时退回网段排序：192.168 优先，172.16~31（WSL/Hyper-V）最后。"""
        from audiomixing.webui import _best_local_address

        report = {"interfaces": [
            {"address": "172.26.192.1", "subnet": "172.26.192.0/24", "kind": "private"},
            {"address": "10.0.0.9", "subnet": "10.0.0.0/24", "kind": "private"},
            {"address": "192.168.0.111", "subnet": "192.168.0.0/24", "kind": "private"},
        ]}
        node = types.SimpleNamespace(
            registry=types.SimpleNamespace(snapshot=lambda: []),
            config=types.SimpleNamespace(manual_peers=[]))
        self.assertEqual(_best_local_address(node, report), "192.168.0.111")
        self.assertEqual(_best_local_address(node, {"interfaces": []}), "127.0.0.1")

    def test_the_peers_payload_does_not_report_a_virtual_adapter(self) -> None:
        """面板「本机」那一栏读的就是 /api/peers，不能显示 172.26.x 的虚拟网卡。"""
        from unittest import mock

        from audiomixing import webui as webui_mod

        report = {"interfaces": [
            {"address": "172.26.192.1", "subnet": "172.26.192.0/24", "kind": "private"},
            {"address": "192.168.0.111", "subnet": "192.168.0.0/24", "kind": "private"},
        ]}
        with mock.patch.object(webui_mod.disc, "lan_report", return_value=report):
            self_host = self.api.peers()["self"]["host"]
        self.assertEqual(self_host, "192.168.0.111")


    def test_the_diagnosis_separates_device_stutter_from_network_loss(self) -> None:
        """★ 2026-10-26：用户说"音频一卡一卡"，旧报告只写包/字节，根本分不清
        是网络丢了包还是声卡没喂上。现在必须同时给出两侧的证据。

        这份数据复刻现场：收包正常、丢 6 个；播放端声卡放空 1 次、喂块最长间隔
        38ms（声卡缓冲 20ms）—— 报告要能直接读出"声卡这侧断过"。
        """
        from unittest import mock

        from audiomixing import bluetooth as bt

        st = {
            "node": {"id": "self-node", "name": "本机", "web_port": 17800,
                     "audio_port": 47831, "discovery_port": 47830, "uptime_s": 66},
            "peers": [], "discovery": {}, "senders": [], "inputs": [],
            "local": {"enabled": False}, "sources": [],
            "sinks": [{
                "route_id": "c1bff8a5", "device_selector": "耳机", "gain": 1.0,
                "mute": False, "frames_written": 1096320, "underruns": 0,
                "dropped_frames": 0, "running": True, "error": "", "peak": 0.7112,
                "blocks": 1000, "late_blocks": 2, "max_block_gap_ms": 38.0,
                "device_buffer_ms": 20.0,
                "device": {"backend": "wasapi", "name": "耳机",
                           "endpoint_format": {"sample_rate": 48000, "channels": 2},
                           "capacity_ms": 20.0, "buffer_ms": 11.0, "backlog_frames": 0,
                           "resampled": False, "starvations": 1, "underruns": 40,
                           "dropped_frames": 0},
            }],
        }
        streams = {"receiver": {"streams": [{
            "node_name": "手机A", "codec": "PCM_F32", "packets": 3819,
            "bytes": 7332480, "lost": 6, "late": 0, "overflow": 0,
            "trimmed": 0, "held_gaps": 0, "underruns": 0, "resyncs": 0,
            "depth_ms": 50.0, "target_ms": 50.0, "peak": 0.7112,
            "played": 1096320, "written": 1100000,
        }]}}
        with mock.patch.object(self.api, "status", return_value=st), \
                mock.patch.object(self.api, "streams", return_value=streams), \
                mock.patch.object(self.api, "guide", return_value={"steps": []}), \
                mock.patch.object(self.api, "netcheck", return_value={"checks": []}), \
                mock.patch.object(self.api, "build_info", return_value={"text": "Audio Mixing v2.4"}), \
                mock.patch("audiomixing.webui._cpu_vendor", return_value="AuthenticAMD"), \
                mock.patch.object(bt, "radio_name", return_value="Test Bluetooth(R) Adapter"), \
                mock.patch.object(bt, "cached_devices", return_value=[]), \
                mock.patch.object(bt, "endpoint_report", return_value=[]):
            text = self.api.diagnose()["text"]
        self.assertIn("丢包=6（0.16%）", text)
        self.assertIn("下溢=0", text)
        self.assertIn("设备放空 1 次", text)
        self.assertIn("最长间隔 38.0ms / 声卡缓冲 20.0ms", text)
        self.assertIn("超过缓冲 2 次", text)
        # ★ 2026-10-26 用户图1：AMD 机器也要有适配说明，不能只写「Intel 适配」。
        self.assertIn("CPU AMD（AuthenticAMD）", text)
        self.assertIn("这是 AMD 平台", text)
        # ★ 2026-11-04（第 11 条）：诊断里的兜底提示改成「频道 → 音源 + 本机直通」，
        #   不再提已经删掉的「本机播放」按钮。
        self.assertIn("本机直通", text)
        # 老问题的自愈证据要出现在报告里。
        self.assertIn("自动重开：0 次", text)
        self.assertIn("蓝牙适配器 Test Bluetooth(R) Adapter", text,
                      "诊断里的适配器型号不能因为函数内局部 import 被吞成「未知」")

    def test_the_diagnostic_bundle_is_one_downloadable_zip(self) -> None:
        """★ 2026-10-26 用户：「下次直接生成诊断包吧，老是复制好麻烦」。

        一次下载就要拿到报告、状态、配置、设备/蓝牙端点和启动日志，
        不能再让用户在一屏文字里全选复制。
        """
        import io
        import zipfile
        from unittest import mock

        from audiomixing import bluetooth as bt

        with mock.patch.object(self.api, "diagnose", return_value={"text": "诊断正文"}), \
                mock.patch.object(self.api, "build_info",
                                  return_value={"text": "Audio Mixing v9.9",
                                                "built_at": "2026-10-26 23:00"}), \
                mock.patch.object(self.api, "status", return_value={"node": {"name": "本机"}}), \
                mock.patch.object(bt, "radio_name", return_value="Test Bluetooth(R) Adapter"), \
                mock.patch.object(bt, "cached_devices", return_value=[]), \
                mock.patch.object(bt, "endpoint_report", return_value=[]):
            data, name = self.api.diagnose_bundle()
        self.assertIn("诊断包", name)
        self.assertTrue(name.endswith(".zip"), name)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            for wanted in ("诊断报告.txt", "status.json", "config.json",
                           "devices.json", "bluetooth.json", "怎么发.txt"):
                self.assertIn(wanted, names, "诊断包缺少 %s" % wanted)
            self.assertIn("诊断正文", zf.read("诊断报告.txt").decode("utf-8"))

    def test_the_guide_flags_a_network_device_as_the_source(self) -> None:
        """第 2 步：音源是网络上的另一台设备 —— 直接说清"一个字节都不会发"。"""
        self._add_peer("M332BF")
        self.node.config.channel_sources = ["M332BF"]
        self._set_devices(self._sink_device(self.SPEAKER, default=True),
                          self._source_device(self.SPEAKER))
        self._fake_capture(self.SPEAKER, blocks=900, age=0.2)
        guide = self.api.guide()
        step2 = guide["steps"][1]
        self.assertEqual(step2["state"], "bad")
        self.assertIn("一个字节都不会发", step2["text"])
        self.assertTrue(any(a["payload"].get("action") == "fix_broadcast"
                            for a in step2["actions"]), "必须给一键修")

    def test_the_guide_lists_only_real_output_devices_as_candidates(self) -> None:
        """候选里不能出现写文件/空输出这类测试后端 —— 换过去一样没声音。"""
        self._set_devices(
            self._sink_device(self.SPEAKER, default=True),
            audio_base.AudioDevice("f1", "WAV 文件（输出）", audio_base.KIND_SINK, "file", "file"),
            audio_base.AudioDevice("n1", "空输出（仅统计）", audio_base.KIND_SINK, "null", "null"),
        )
        names = [c["device"] for c in self.node.capture_candidates()]
        self.assertEqual(names, [self.SPEAKER])

    # -- 一键修：换设备 / 修广播 -------------------------------------------
    def test_the_one_click_fix_moves_to_a_device_with_data(self) -> None:
        """【一键修广播】：清掉用不了的音源，换到一块真有数据的设备，并打开广播。"""
        self._set_devices(self._sink_device(self.UU, default=True), self._source_device(self.UU),
                          self._sink_device(self.SPEAKER))
        self.node.config.channel_sources = ["M332BF", self.UU]   # 一个是网络设备名，一个是空转设备
        self.node.config.source_device = self.UU
        self.node.config.broadcast = False
        self._fake_capture(self.UU)
        self._mark_dead_for(self.UU, 40.0)
        # 让"试采过并且有数据"这件事有据可依（真机上由引擎自己试采）
        self.node._capture_health[self.node._device_name_key(self.SPEAKER)] = {
            "alive": True, "blocks": 3, "peak": 0.05, "error": "", "checked_s": time.monotonic(),
            "note": "试采到 3 块音频（电平 0.050）"}
        result = self.api.guide_fix({"action": "fix_broadcast"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.node.config.channel_sources, [self.SPEAKER])
        self.assertEqual(self.node.config.source_device, self.SPEAKER)
        self.assertTrue(self.node.config.broadcast, "一键修必须把广播打开")
        self.assertIn("M332BF", result["dropped"])
        self.assertIn(self.SPEAKER, result["note"])

    # -- 采集源空转自动回退 ------------------------------------------------
    def test_a_stalled_capture_is_switched_automatically_and_persisted(self) -> None:
        """空转 10 秒以上 + 存在试采有数据的设备 → 自动切过去，并写进配置。"""
        from audiomixing.config import Config as _Config

        self._set_devices(self._sink_device(self.UU, default=True), self._source_device(self.UU),
                          self._sink_device(self.SPEAKER))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self.node.config.save()
        self._fake_capture(self.UU)
        self._mark_dead_for(self.UU, 20.0)
        tried = self._stub_probe_alive(True)

        self.node._recover_stalled_capture()        # 起试采（后台线程）
        deadline = time.time() + 5.0
        while self.node._probe_result is None and time.time() < deadline:
            time.sleep(0.05)
        self.node._capture_probe_tick()             # 收试采结果
        self.node._recover_stalled_capture()        # 应用切换

        self.assertEqual(self.node.config.channel_sources, [self.SPEAKER])
        self.assertEqual(self.node.config.source_device, self.SPEAKER)
        self.assertIn(self.SPEAKER, tried, "必须先真的试采过那块设备")
        switch = self.node._last_capture_switch or {}
        self.assertEqual(switch.get("from"), self.UU)
        self.assertEqual(switch.get("to"), self.SPEAKER)
        self.assertTrue(switch.get("auto"))
        self.assertIn("没有数据", switch.get("why") or "")
        # 持久化：下次启动不再走那块坏设备
        saved = _Config.load()
        self.assertEqual(saved.channel_sources, [self.SPEAKER])
        self.assertEqual(saved.source_device, self.SPEAKER)

    def test_no_alternative_device_means_no_switch(self) -> None:
        """没有别的输出设备（或试采也没有数据）时只告警，绝不能瞎切。"""
        self._set_devices(self._sink_device(self.UU, default=True), self._source_device(self.UU))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU)
        self._mark_dead_for(self.UU, 30.0)
        self.node._recover_stalled_capture()
        self.assertIsNone(self.node._last_capture_switch, "没有替代设备就不能切")
        self.assertEqual(self.node.config.channel_sources, [self.UU])

    def test_the_dead_timer_survives_capture_rebuilds(self) -> None:
        """坏设备每 6 秒被判一次"卡住"并重建 —— 判定必须跨重建累计，否则永远等不到 10 秒。"""
        stat = {"name": self.UU + " [loopback]", "spec": {"device": self.UU},
                "blocks_read": 0, "last_data_age_s": None, "alive_for_s": 0.4, "error": ""}
        state, _reason, definite = self.node._capture_data_state(stat)
        self.assertEqual(state, "no_data")
        self.assertFalse(definite, "刚打开 0.4 秒不能算「确定采不到」")
        self._mark_dead_for(self.UU, 12.0)
        state, reason, definite = self.node._capture_data_state(stat)
        self.assertTrue(definite, "连续 12 秒没数据才算「确定采不到」")
        self.assertIn("12 秒", reason)
        # 真的出数据了就立刻清掉，不能一直背着「坏设备」的标签
        self.node.hub.stats = lambda: [{          # type: ignore[method-assign]
            "name": self.UU + " [loopback]", "spec": {"device": self.UU},
            "blocks_read": 500, "last_data_age_s": 0.1, "alive_for_s": 9.0, "error": ""}]
        self.node._track_dead_captures()
        self.assertIsNone(self.node._dead_age_for(self.UU))

    def test_the_auto_switch_is_announced_with_from_and_to(self) -> None:
        """自动切换不能静默：面板要拿到"从哪换到哪、为什么"。"""
        self._set_devices(self._sink_device(self.UU, default=True), self._source_device(self.UU),
                          self._sink_device(self.SPEAKER))
        self.node.config.channel_sources = [self.UU]
        self.node.config.source_device = self.UU
        self._fake_capture(self.UU)
        self._mark_dead_for(self.UU, 11.0)
        self.node._stub_probe = None
        result = self.node.switch_capture(self.SPEAKER, why="测试里手动换的")
        self.assertTrue(result["ok"])
        state = self.node.channel_state()
        switch = state.get("switch") or {}
        self.assertEqual(switch.get("from"), self.UU)
        self.assertEqual(switch.get("to"), self.SPEAKER)
        self.assertFalse(switch.get("auto"))
        # capture_state() 是向导读的那份，也要带同样的记录
        self.assertEqual((self.node.capture_state().get("switch") or {}).get("to"), self.SPEAKER)

    # -- 版本可见性 --------------------------------------------------------
    def test_build_info_finds_a_newer_exe_next_to_the_desktop(self) -> None:
        """桌面上有更新版本时要提醒（用户就是拿旧 exe 测了一整天）。"""
        from unittest import mock

        from audiomixing import __version__
        from audiomixing.webui import AudioMixingApi

        with tempfile.TemporaryDirectory() as tmp:
            desk = pathlib.Path(tmp) / "Desktop"
            desk.mkdir()
            (desk / "Audio Mixing v9.9.exe").write_bytes(b"fake")
            (desk / "Audio Mixing v0.1.exe").write_bytes(b"fake")
            (desk / "别的东西.exe").write_bytes(b"fake")
            with mock.patch.dict(os.environ, {"USERPROFILE": tmp}):
                info = self.api.build_info()
        # 版本号每轮都涨，测试跟着 __version__ 走，别把某一版写死
        self.assertEqual(info["version"], __version__)
        self.assertIn("Audio Mixing v%s" % __version__, info["text"])
        self.assertTrue(info["build_time"], "必须给出构建时间")
        self.assertTrue(AudioMixingApi._version_tuple("9.9") > AudioMixingApi._version_tuple(__version__))
        self.assertIsNotNone(info["newer"], "应该发现 v9.9")
        self.assertEqual(info["newer"]["name"], "Audio Mixing v9.9.exe")

    def test_build_info_ignores_older_versions(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            desk = pathlib.Path(tmp) / "Desktop"
            desk.mkdir()
            (desk / "Audio Mixing v1.0.exe").write_bytes(b"fake")
            with mock.patch.dict(os.environ, {"USERPROFILE": tmp}):
                info = self.api.build_info()
        self.assertIsNone(info["newer"], "旧版本不该被当成「更新的版本」")

    # -- 打包形态（Nuitka onefile）下的版本可见性 ---------------------------
    # 2026-10-14 用户实测 exe 时发现两处：面板写「构建于 未知」、诊断报告写「源码运行」
    # 而同一份报告上方却写着「版本 exe」。两条的根因都是没按 Nuitka 的实际形态判。
    def _fake_nuitka(self, tmp: str, exe: pathlib.Path, ghost_dir: str = "") -> types.SimpleNamespace:
        """造一个 __compiled__，模拟 Nuitka onefile 编译后的模块。

        original_argv0 才是用户双击的那个 exe（实测 sys.executable 指向解包出来的
        临时 python.exe，所以这里也故意给一个不存在的路径当 ghost）。
        """
        return types.SimpleNamespace(
            original_argv0=str(exe),
            containing_dir=(ghost_dir or str(pathlib.Path(tmp) / "onefile_tmp")),
            onefile=True, standalone=True)

    def test_frozen_detection_covers_nuitka(self) -> None:
        """Nuitka 不设 sys.frozen，但跑的就是 exe —— 判据必须覆盖 __compiled__。"""
        from audiomixing import webui

        self.assertFalse(webui.AudioMixingApi._is_frozen(), "源码运行时不该被判成 exe")
        webui.__compiled__ = types.SimpleNamespace()      # 模拟 Nuitka 编译后的模块
        try:
            self.assertTrue(webui.AudioMixingApi._is_frozen())
        finally:
            del webui.__compiled__

    def test_build_info_reads_the_real_exe_under_nuitka_onefile(self) -> None:
        """缺陷 1 回归：onefile 下 sys.executable 指向临时 python.exe，
        构建时间不能因此变成「未知」—— 要去 __compiled__.original_argv0 找那个真 exe。
        """
        from unittest import mock

        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            os.utime(exe, (1_600_000_000, 1_600_000_000))
            expected = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_600_000_000))
            ghost = pathlib.Path(tmp) / "onefile_19100_393045" / "python.exe"  # 故意不存在
            webui.__compiled__ = self._fake_nuitka(tmp, exe)
            try:
                # 必须把"打包时写死的时间"清掉，这条测的才是"退回到 exe 文件时间"那一步。
                # 不清的话，只要有人**正在跑打包**（audiomixing/_buildmeta.py 正躺在源码树里），
                # 这条就会读到那次构建的时间而误报失败 —— 测试不该依赖构建产物在不在。
                with mock.patch.dict(os.environ, {"USERPROFILE": tmp}), \
                        mock.patch.object(sys, "executable", str(ghost)), \
                        mock.patch("audiomixing.__build_time__", "", create=True):
                    info = self.api.build_info()
            finally:
                del webui.__compiled__
        self.assertEqual(info["channel"], "exe")
        self.assertEqual(info["build_time"], expected, "应取到真 exe 的时间，而不是空")
        self.assertNotIn("未知", info["text"])

    def test_diagnose_says_exe_not_source_when_frozen(self) -> None:
        """缺陷 2 回归：跑 exe 时报告里绝不能写「源码运行」（与上方「版本 exe」矛盾）。"""
        from unittest import mock

        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            ghost = pathlib.Path(tmp) / "onefile_tmp" / "python.exe"       # 故意不存在
            webui.__compiled__ = self._fake_nuitka(tmp, exe)
            try:
                with mock.patch.object(sys, "executable", str(ghost)):
                    report = self.api.diagnose()
            finally:
                del webui.__compiled__
        text = report["text"]
        self.assertIn("程序", text)
        self.assertNotIn("源码运行", text, "跑 exe 时报告不许说源码运行")
        self.assertIn(str(exe), text)
        # ★ 2026-10-26：用户看到任务管理器里两个 Audio Mixing，怀疑重复启动。
        #   onefile 的引导器 + 子进程本来就有两个，报告必须自己说清楚。
        self.assertIn("打包形态", text)
        self.assertIn("两个进程", text, "单文件版必须解释任务管理器里那两个进程是什么")

    def test_embedded_build_time_wins_over_file_mtime(self) -> None:
        """打包时写死的时间优先：exe 复制到桌面后文件时间变了，构建时间不该跟着变。"""
        from unittest import mock

        import audiomixing as pkg
        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            os.utime(exe, (1_600_000_000, 1_600_000_000))
            webui.__compiled__ = self._fake_nuitka(tmp, exe)
            try:
                with mock.patch.dict(os.environ, {"USERPROFILE": tmp}), \
                        mock.patch.object(sys, "executable", str(exe)), \
                        mock.patch.object(pkg, "__build_time__", "2026-10-14 22:00", create=True):
                    info = self.api.build_info()
            finally:
                del webui.__compiled__
        self.assertEqual(info["build_time"], "2026-10-14 22:00")

    # -- 手机端安装包：面板的「扫码装 App」页必须能自己找到它 -------------------
    # 2026-10-15 用户现场：手机扫二维码装 App，页面报「这台电脑上还没有手机端安装包」。
    # 真因有两层：① apk_path() 里 `Path(base)` 的 base 在非 PyInstaller 时是 None，
    # 于是直接抛 TypeError、/api/app 跟着 500，页面只剩兜底文案；② 候选名单里没有
    # 交付链用的新文件名（桌面那颗叫 AudioMixing-手机版.apk）。
    def test_apk_lookup_never_raises_outside_pyinstaller(self) -> None:
        from unittest import mock

        from audiomixing import webui

        self.assertFalse(getattr(sys, "_MEIPASS", None), "测试环境不该有 _MEIPASS")
        with mock.patch.object(webui, "_apk_dirs", return_value=[]):
            self.assertIsNone(webui.apk_path(), "找不到只能是 None，不许抛异常")
        found = webui.apk_path()
        if found is not None:
            self.assertTrue(found.is_file())

    def test_apk_lookup_prefers_the_package_next_to_the_exe(self) -> None:
        """onefile 下 sys.executable 是临时 python.exe，要按真 exe 的目录找安装包。"""
        from unittest import mock

        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            (pathlib.Path(tmp) / "AudioMixing-手机版-v2.20.apk").write_bytes(b"fake-apk")
            webui.__compiled__ = self._fake_nuitka(tmp, exe)
            try:
                ghost = str(pathlib.Path(tmp) / "onefile_tmp" / "python.exe")
                with mock.patch.object(sys, "executable", ghost), \
                        mock.patch.dict(os.environ, {"AUDIOMIXING_APK": ""}):
                    found = webui.apk_path()
            finally:
                del webui.__compiled__
        self.assertIsNotNone(found, "exe 旁边那颗 apk 必须被找到")
        self.assertEqual(found.name, "AudioMixing-手机版-v2.20.apk",
                         "★ 第 6 条：交付名必须带版本号")

    def test_apk_lookup_knows_every_shipped_filename(self) -> None:
        from audiomixing import webui

        self.assertIn("AudioMixing-手机版-v2.20.apk", webui._APK_NAMES,
                      "★ 第 6 条：交付名必须带版本号")
        self.assertIn("AudioMixing-mic.apk", webui._APK_NAMES)
        src = (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8")
        self.assertIn("/app/AudioMixing-手机版-v%s.apk", src, "下载路由要认带版本号的新文件名")

    def test_the_apk_download_header_stays_ascii_safe(self) -> None:
        """中文文件名的附件头必须仍然合法 —— 否则用户点「下载」就失败。

        交付链上这颗包叫「AudioMixing-手机版.apk」。HTTP 头只许 ASCII，原样写进去
        .NET 会报"协议冲突（CR 后面必须是 LF）"（2026-10-15 实测）。
        """
        from urllib.parse import unquote

        from audiomixing.webui import content_disposition

        for name in ("AudioMixing-手机版-v2.14.apk", "AudioMixing-手机版.apk", "AudioMixing-mic.apk", ""):
            header = content_disposition(name)
            header.encode("ascii")                      # 编码不过就是非法头
            self.assertIn("filename*=UTF-8''", header)
            if name:
                self.assertIn(unquote(header.split("UTF-8''")[-1]), name)
        # ASCII 兜底名也要像个正常文件名（不留下孤零零或连续的短横）
        header = content_disposition("AudioMixing-手机版-v2.14.apk")
        self.assertIn('filename="AudioMixing-v214.apk"', header)
        self.assertNotIn('filename="AudioMixing--', header)
        self.assertIn('filename="AudioMixing.apk"',
                      content_disposition("AudioMixing-手机版.apk"))

    def test_the_install_page_uses_the_current_brand_and_filename(self) -> None:
        """用户原话「名字也是老版不对货」：安装页不许再写 WaveLink / wavelink-mic.apk。

        ★ 2026-11-04（第 6 条）：页面还要明确"文件名带版本号"，否则用户分不清装的是哪版
        （他实机就装错过一次）。
        """
        html = (ROOT / "audiomixing" / "ui" / "app.html").read_text(encoding="utf-8")
        self.assertIn("Audio Mixing 手机端", html)
        self.assertIn("AudioMixing-手机版-v", html, "要写明文件名形如 …-v版本号.apk")
        self.assertNotIn("WaveLink 手机端", html)
        self.assertNotIn("wavelink-mic.apk", html)

    def test_version_parsing(self) -> None:
        from audiomixing.webui import AudioMixingApi

        self.assertEqual(AudioMixingApi._version_tuple("Audio Mixing v1.9.exe"), (1, 9))
        self.assertEqual(AudioMixingApi._version_tuple("v1.10.2"), (1, 10, 2))
        self.assertEqual(AudioMixingApi._version_tuple("没有数字"), ())

    def test_the_page_shows_the_version_and_the_guide(self) -> None:
        """面板必须有版本号、更新提示和「快速上手」入口 —— 这是给人看的那一半。"""
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        for token in ('id="versionTag"', 'id="versionWarn"', 'id="guideMask"',
                      'id="guideSteps"', "🚀 快速上手", "async function openGuide("):
            self.assertIn(token, html)
        self.assertEqual(html.count("async function openGuide("), 1)


class TestDeviceCardUi(unittest.TestCase):
    """④ 电脑端路由改成"对设备的按键"（用户原话：不要拖动了，都改成对设备的按键）。

    旧版靠"把设备拖进两个框"生成路由：要先用鼠标拖，还得先理解"源→目标"这套模型；
    用户觉得手机端那种"一台设备一张卡片、卡片上两个按钮"更好用。
    这里钉住：卡片在、按钮在、拖拽那套真的清掉了、多目标/音量/编码这些能力没丢。
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_every_device_gets_a_card_with_two_buttons(self) -> None:
        html = self._html()
        self.assertIn('id="deviceCards"', html)
        self.assertEqual(html.count("function renderDeviceCards("), 1)
        self.assertIn("🎧 收听它的", html)
        self.assertIn("🎤 送给它", html)
        # 卡片上要能看到"名字 · IP · 状态 · 电平"
        for token in ("dvname", "dvmeta", "dvbar", "dvflow", "dvtag"):
            self.assertIn(token, html)
        # 路由增删走按钮：一个方向一条路由，再点一次断开
        self.assertEqual(html.count("async function quickRoute("), 1)
        self.assertIn("/api/routes/add", html)
        self.assertIn("/api/routes/delete", html)

    def test_the_drag_based_route_builder_is_gone(self) -> None:
        """「拖两个框就是一条路由」那一套（高级区里的拖拽建路由）已经删干净了。

        ★ 2026-10-18 注意：删的是**那一套**（`renderDragList` / `beginDrag` / `dropFrom` /
        `dragList` / `ghost` 这些 id 和函数）。设备卡片上的按钮（`quickRoute`）+ 勾选式高级区
        仍然是建路由的正路。**「拖到这里」这四个字现在合法地出现在「设备关系图」的圆点
        tooltip 里** —— 那是 2026-10-18 用户新提的、另一套刻意做的拖拽（见
        `test_the_relation_graph_can_really_build_a_route`），所以这里不再拿这句话当"标志物"。
        """
        html = self._html()
        for gone in ("renderDragList", "addToDraft", "beginDrag", "moveDrag", "endDrag",
                     'id="dropFrom"', 'id="dropTo"', 'id="dragList"', 'id="ghost"',
                     "拖两个框就是一条路由"):
            self.assertNotIn(gone, html, "拖拽式路由应该已经删干净：%s" % gone)

    def test_multi_target_and_per_route_controls_survive(self) -> None:
        """能力不能丢：多目标混音、每路音量、编码、指定输出设备。

        ★ 2026-10-16 用户（图5）：「这个路由不需要了，我们已经有卡片显示当前状态了」——
        「已经连上的」那一整段连同 `setGain()` / `toggleMute()` 都删掉了。**静音**这个
        能力是随之丢掉的（已在版本说明里如实写明），所以这里不再断言它；音量改由设备卡片上
        的滑块（`setCardGain`）承担，断开改由卡片上的按钮（`quickRoute`）承担。
        """
        html = self._html()
        for token in ('id="pickFrom"', 'id="pickTo"', 'id="newGain"', 'id="newCodec"',
                      'id="newSinkDevice"', "function toggleDraft(", "function setCardGain(",
                      "quickRoute(", "async function addRoute("):
            self.assertIn(token, html)
        for gone in ('id="routesBox"', "function toggleMute(", "function setGain("):
            self.assertNotIn(gone, html, "已删掉的东西不许半留着（%s）" % gone)
        # 本机 ID 的几种写法要先归一，否则卡片上的按钮认不出"这条路由已经存在"
        self.assertIn("function normEnds(", html)
        self.assertIn("isLocalId(x) ? 'self' : String(x)", html)
        self.assertIn("renderDeviceCards();", html, "刷新循环里必须真的重建卡片")

    def test_gain_wording_says_which_layer_it_controls(self) -> None:
        """音量语义要和手机端对齐：滑杆管的是"我们这一层"，不是系统音量。

        手机端这一轮把滑杆标题改成了「我们这一层的音量」（另有一行只读的系统媒体音量）。
        电脑端如果还写着光秃秃的"频道音量"，用户就会以为它能改 Windows 的音量。
        """
        html = self._html()
        self.assertIn("频道音量</label>", html, "频道滑杆的标签还在")
        # ★ 2026-10-16（图5）之后「已经连上的」那段路由列表删了，它里面那句
        #   「我们这一层的音量（不动 Windows 系统音量）」随之消失。同样的语义现在由
        #   设备卡片上那个滑动条的标题承担，这里改钉那一处。
        self.assertIn("我们这一层的增益（", html, "卡片滑块的标题要写明只管我们这一层")
        self.assertIn("不动 Windows 系统音量", html)
        # ★ 2026-10-16（图4）：原来这里还断言过「我们这一层的音量：只改这台手机传进来的增益」
        #   —— 那句是「Wi-Fi 送声音的手机」那一列卡片上滑杆的 title，那一段已随图4 整块删掉。
        #   同样的语义现在由设备卡片上「我听它的」滑杆承担（它以名字找到这台手机的采集增益）。


class TestThemeParityWithPhone(unittest.TestCase):
    """⑥ 电脑面板的视觉必须和手机端是同一套（用户："手机 app 的 UI 我觉得电脑的太丑"）。

    做法是【照抄】phone.html 的 :root 与那几个组件取值，不是在电脑端另创一套。
    这组测试把"是同一套"变成可核对的事实：令牌逐个比、关键圆角/渐变/字号逐个比。
    以后谁把电脑端改回自己的配色，这里会直接红。
    """

    #: 手机端 :root 定义的主题令牌 —— 电脑端必须逐个相同
    SHARED_TOKENS = ("--bg", "--card", "--line", "--fg", "--dim", "--acc", "--acc2",
                     "--ok", "--warn", "--err")

    @staticmethod
    def _tokens(text: str) -> dict:
        import re

        block = re.search(r":root\{(.*?)\}", text, re.S).group(1)
        return {name: value.strip()
                for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)}

    def setUp(self) -> None:
        self.pc = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.phone = (ROOT / "audiomixing" / "ui" / "phone.html").read_text(encoding="utf-8")

    def test_the_theme_tokens_are_the_same_as_the_phone(self) -> None:
        pc, phone = self._tokens(self.pc), self._tokens(self.phone)
        for key in self.SHARED_TOKENS:
            self.assertIn(key, pc, "电脑端缺少令牌 %s" % key)
            self.assertEqual(pc[key], phone[key], "令牌 %s 与手机端不一致" % key)

    def test_the_shared_component_values_match(self) -> None:
        import re

        pairs = [
            ("卡片圆角", r"\.card\{[^}]*border-radius:(\d+px)", r"\.card\{[^}]*border-radius:(\d+px)"),
            ("分段控件圆角", r"\.seg\{[^}]*border-radius:(\d+px)",
             r"\.seg\{display:flex;background:var\(--card\)[^}]*border-radius:(\d+px)"),
            ("电平条圆角", r"\.bars i\{[^}]*border-radius:(\d+px)",
             r"\.bars i\{width:5px;border-radius:(\d+px)"),
        ]
        for label, pc_pat, ph_pat in pairs:
            a = re.search(pc_pat, self.pc, re.S)
            b = re.search(ph_pat, self.phone, re.S)
            self.assertTrue(a and b, label)
            self.assertEqual(a.group(1), b.group(1), label)
        # 字号/行高与主按钮渐变：手机端是权威
        self.assertIn("font:15px/1.5 -apple-system", self.pc)
        self.assertIn("font:15px/1.5 -apple-system", self.phone)
        self.assertIn("linear-gradient(140deg,var(--acc),var(--acc2))", self.pc)
        self.assertIn("linear-gradient(140deg,var(--acc),var(--acc2))", self.phone)

    def test_the_phone_style_big_ring_and_22_segment_meter_exist(self) -> None:
        """手机端的大状态圆环 + 22 段电平条，电脑端也要有（同一套做法）。

        ★ 2026-10-16 用户（图1）：「这些字全删了。只留信号强度和声音大小百分比
        （三个数据采用相对应环的颜色。3秒切换一次）」——圆心那两行字（#ringUnit /
        #ringState）删掉了，换成三格轮换（RING_CELLS / renderRingCore），所以这里改成
        钉三格轮换那套；圆环本体和 22 段电平条原样保留。
        """
        for token in ('id="ring"', 'id="ringProg"', 'id="ringMetric"', 'id="levelBars"',
                      "const LEVEL_BARS = 22", "function pushLevel(", "function renderLevelMeter(",
                      "const RING_CELLS", "function renderRingCore(", "RING_CELL_MS"):
            self.assertIn(token, self.pc)
        for gone in ('id="ringUnit"', 'id="ringState"'):
            self.assertNotIn(gone, self.pc, "圆心的说明文字必须真的删干净")
        self.assertIn("const BARS = 22", self.phone, "手机端也是 22 段（改手机端时这里要跟着改）")

    def test_the_browse_density_words_match_the_phone(self) -> None:
        """浏览密度三档的词要和手机端一致：简洁 / 详细 / 监控。

        ★ 2026-10-16（图2）：这三项从顶栏的分段控件搬进了左边那条**一级菜单**
        （用户要求照 LocalSend 重排），所以 `#modeSeg` 那个 id 不再存在 ——
        词没变、位置变了，测试跟着改（不然它会一直红着，而那并不代表功能坏了）。
        """
        for word in (">简洁<", ">详细<", ">监控<"):
            self.assertIn(word, self.pc)
        self.assertIn('data-rail="simple"', self.pc, "简洁是左边一级菜单里的一项")
        self.assertIn('data-rail="full"', self.pc, "详细也是")


class TestAudioPacketSamplesField(unittest.TestCase):
    """★ 根因回归 ★ `samples` 必须是【本包帧数】，不是累计帧偏移。

    2026-10-14 用户真机诊断报告：手机端把电脑发出的**每一个**音频包都判成坏包
    （"帧长不是 480：10266"），于是手机永远听不到电脑的声音。真凶在电脑端：

        samples=frame_offset & 0xFFFFFFFF   # 480 × seq，一直在涨

    Python 接收端不读这个字段（它按解码后的负载长度算帧数），所以电脑↔电脑一直没症状；
    手机端拿它做准入判据（`samples > 每包帧数` 即判坏包）才暴露出来。
    这组测试钉死语义：每包都写"这一包有多少帧"，与它是第几包无关。
    """

    class _SilentSource:
        """什么都不产出的音源，让发送线程安静地待着，测试自己直接调 _send_chunk。"""

        name = "silent"
        sample_rate = SAMPLE_RATE
        channels = CHANNELS

        def start(self) -> None:
            pass

        def read(self):
            time.sleep(0.01)
            return []

        def stop(self) -> None:
            pass

    def test_the_header_layout_is_untouched(self) -> None:
        """28 字节布局与版本号是跨语言对拍过的，谁都不许改。"""
        self.assertEqual(proto.HEADER_SIZE, 28)
        self.assertEqual(proto.VERSION, 1)
        self.assertEqual(proto.MAGIC, b"WL")
        self.assertEqual([n for n, _f in proto.HEADER_FIELDS],
                         ["magic", "version", "ptype", "flags", "codec", "seq", "ssrc",
                          "samples", "ts_ns", "gain_q14", "payload_len"])

    def test_every_packet_carries_its_own_frame_count(self) -> None:
        """连发多包：每包 samples 都必须等于本包帧数（不能随包号变大）。"""
        import socket

        port = free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))
        sock.settimeout(3.0)
        session = SenderSession(ToneSource(440.0, 200, pace=0.0), "127.0.0.1", port,
                                codec=proto.CODEC_PCM_S16)
        session.start()
        seen: list[tuple[int, int]] = []          # (samples, 按负载算出来的帧数)
        try:
            end = time.time() + 1.5
            while time.time() < end and len(seen) < 12:
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                pkt = proto.decode(data)
                if pkt is None or pkt.type != proto.T_AUDIO:
                    continue
                seen.append((pkt.samples, len(pkt.payload) // 2 // CHANNELS))
        finally:
            session.stop()
            sock.close()
        self.assertGreaterEqual(len(seen), 5, "至少要收到几包才看得出语义：%s" % seen)
        for samples, frames in seen:
            self.assertEqual(
                samples, frames,
                "samples 必须是本包帧数；实际 %d，负载里是 %d 帧 —— 这就是那个根因" % (samples, frames))
        self.assertLess(max(s for s, _f in seen), 480 * 20,
                        "samples 不该随包号一路涨（一路涨就是累计偏移的写法）")

    def test_a_short_tail_packet_reports_its_own_frames(self) -> None:
        """收尾的短包也要按自己的帧数填，不能一律填 480。"""
        import socket

        port = free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))
        sock.settimeout(3.0)
        session = SenderSession(self._SilentSource(), "127.0.0.1", port, codec=proto.CODEC_PCM_S16)
        session.start()
        try:
            time.sleep(0.05)
            session._send_chunk(array("f", [0.0] * (300 * CHANNELS)))
            data, _addr = sock.recvfrom(4096)
        finally:
            session.stop()
            sock.close()
        pkt = proto.decode(data)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt.type, proto.T_AUDIO)
        self.assertEqual(pkt.samples, 300, "短包要填它自己的帧数")

    def test_the_python_receiver_never_reads_samples(self) -> None:
        """写清楚"为什么一直没暴露"：接收端不读这个字段，所以填错了它也不报错。

        这条同时也是护栏：哪天接收端开始依赖 samples，就必须先跟手机端重新对拍语义。
        用 AST 找真正的属性访问（`pkt.samples`），注释和文档字符串里提到它不算。
        """
        import ast
        import inspect

        from audiomixing.net import rtpaudio as _rtpaudio

        tree = ast.parse(inspect.getsource(_rtpaudio))
        reads = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "samples"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("pkt", "packet", "parsed")):
                reads.append(getattr(node, "lineno", 0))
        self.assertEqual(reads, [],
                         "接收端一旦开始依赖 samples，就得重新跨语言对拍这个字段的语义（第 %s 行）" % reads)


class TestCardVolumeSlidersAndPing(unittest.TestCase):
    """设备卡片上的可调音量条 + "测连通"的诚实结论。

    用户原话：「我要求的是各设备**可调节**的音量条。当然现在有的显示实际音量条也不错」
    —— 所以滑块（可调）和电平条（实时显示）两个都要有，滑块控制我们这一层的增益。
    另一条来自截图：面板写着「198.51.100.10 不通：timed out」——查清是手机没回还是我们等错了。
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        self.node = AudioMixingNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
                                 simulate=True)
        self.api = AudioMixingApi(self.node)

    def tearDown(self) -> None:
        try:
            self.node.stop()
        except Exception:
            pass

    # -- 卡片上的滑块与电平条 ---------------------------------------------
    def test_every_card_has_two_adjustable_sliders(self) -> None:
        html = self._html()
        self.assertIn("我听它的", html)
        self.assertIn("我送它的", html)
        self.assertIn("function setCardGain(", html)
        self.assertIn("gainRow('in'", html)
        self.assertIn("gainRow('out'", html)
        # 调完立刻生效：走的是既有的路由更新接口（update_route → set_routes → apply_routes）
        self.assertIn("/api/routes/update", html)
        self.assertIn("cardGainTimer", html, "拖动时要防抖，不然每动一下打一次接口")
        # 没有对应路由时滑块必须禁用，避免调一个不存在的音量
        self.assertIn("${on ? '' : 'disabled'}", html)

    def test_the_level_bars_are_kept_next_to_the_sliders(self) -> None:
        """用户说了"显示实际音量条也不错" —— 三条实时电平条要留着。

        ★ 2026-10-16 用户（图6）：「电脑手机现在都只有"送""收"两个条，现在定义为音量
        显示条。并添加一个信号质量条。」→ 每张设备卡片现在是 **收 / 送 / 信号** 三条，
        而且它们显示的是**音量**（峰值），不再是码率。本机那张卡片的"收"换成「输出」。
        """
        html = self._html()
        self.assertIn('<span class="lbl">送</span>', html)
        self.assertIn('<span class="lbl">信号</span>', html)
        self.assertIn('<span class="lbl">${inLabel}</span>', html,
                      "非本机卡的第一条是「收」、本机那张是「输出」")
        self.assertIn("const inLabel = self ? '输出' : '收';", html)
        self.assertIn("function levelSegs(", html)
        self.assertGreaterEqual(html.count("levelSegs("), 4, "三条（含蓝牙方块那条）都要用 22 段条画")
        self.assertIn("function inboundKbps(", html, "下行码率要按字节增量算，不能只看累计")
        # 音量要用 sender/receiver 各自报出来的峰值 —— 码率和"声音大小"毫无关系。
        self.assertIn("flows.in_peak", html)
        self.assertIn("flows.out_peak", html)

    def test_slider_ids_are_per_device(self) -> None:
        """多张卡片同时在页面上：数字标签的 id 必须带设备 id，否则会互相覆盖。"""
        html = self._html()
        self.assertNotIn('id="cg_in"', html)
        self.assertNotIn('id="cg_out"', html)
        self.assertIn("$('cg_' + slot + '_' + uid)", html)

    # ---- ★ 2026-11-04（第 8 条）：删除「测连通」按钮与 /api/ping -----------------
    def test_the_ping_button_and_api_are_gone(self) -> None:
        """用户点了「测连通」没有任何反馈（手机端本来就不回 PING/PONG）→ 整条下线。"""
        from audiomixing import webui

        self.assertFalse(hasattr(webui.AudioMixingApi, "ping"), "服务端 ping 方法必须删掉")
        self.assertFalse(hasattr(self.api, "ping"))
        import re

        html = re.sub(r"<!--.*?-->", "", self._html(), flags=re.S)
        html = re.sub(r"/\*.*?\*/", "", html, flags=re.S)     # 只查代码，注释里说明"已删除"不算残留
        self.assertNotIn("测连通", html, "前端按钮必须删掉")
        self.assertNotIn("pingPeer", html)
        self.assertNotIn("/api/ping", html)
        src = pathlib.Path(webui.__file__).read_text(encoding="utf-8")
        live = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("/api/ping", live, "服务端路由必须删掉，不留死接口")


class TestPeerLivenessAndAutoListen(unittest.TestCase):
    """④ 对端没了就停发（安全）+ ① 自动收听 / 共享增益。

    用户真机现场：「手机蓝牙向电脑传输音频，电脑关闭应用，手机声音还在向电脑发送」。
    根因是发送端的目标表只在用户点选时写一次，对方关机后没人移除它 ——
    而且 `PeerEndpoints.online` 对【手动添加】的对端恒为 True，连 15 秒的超时都不生效。
    """

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        self.node = AudioMixingNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
                                 simulate=True)
        self.node.devices = []
        # ★ 2026-10-18 用户第 6 条：自动收听现在是**每台设备一个「智连」**、默认全关。
        #   这个类测的是"请求来了要不要自动开"，所以这里统一给这些对端开上智连；
        #   "默认全关"本身由 test_a_device_without_smart_link_is_never_auto_added 单独钉住。
        self.node.config.smart_link = ["peer-1", "phone-abc", "M332BF", "node-phone"]

    def tearDown(self) -> None:
        try:
            self.node.stop()
        except Exception:
            pass

    def _peer(self, node_id: str = "peer-1", name: str = "手机", host: str = "198.51.100.10",
              age: float = 0.0, audio_port: int = 47831) -> None:
        """登记一台对端，并把它"最后一次被听到"设成 age 秒之前。"""
        self.node.registry.update(
            {"wl": 1, "id": node_id, "name": name, "audio_port": audio_port, "web_port": 0}, host)
        peer = self.node.registry.get(node_id)
        peer.last_seen = time.time() - age

    def _route_to_peer(self, peer: str = "peer-1") -> None:
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": [peer]}], persist=False)

    def _stats_from(self, node_id: str, host: str, **extra) -> None:
        body = {"t": "stats", "node": node_id, "name": node_id, "_type": "STATS",
                "_from": "%s:47831" % host, "_at": time.monotonic()}
        body.update(extra)
        self.node.receiver.control_messages.append(body)

    # ---- ④ 对端没了就停发 -------------------------------------------------
    def test_a_peer_that_went_silent_is_not_sent_to(self) -> None:
        self._peer(age=30.0)
        self._route_to_peer()
        self.assertEqual(self.node.desired_senders(), [],
                         "对端 30 秒没有任何消息了，不能再往它发声音")
        blocked = self.node.status()["senders_blocked"]
        self.assertTrue(blocked, "面板要能说明为什么停发")
        self.assertIn("已暂停向", blocked[0]["text"],
                      "★ 第 2 条：措辞要如实 —— 只是「这个 ID 没有消息」，不是断言关机")
        self.assertIn("秒没有任何消息", blocked[0]["text"])
        self.assertNotIn("关机", blocked[0]["text"],
                         "对端可能只是换了 ID/网络；报成关机就是误报")
        self.assertGreaterEqual(blocked[0]["silent_for_s"], 30.0)

    def test_a_manual_peer_also_expires(self) -> None:
        """手动添加的对端以前永远 online（online 对 manual 恒 True）→ 会一直白发。"""
        self.node.registry.add_manual("198.51.100.10", 47831, "手机")
        peer = self.node.registry.resolve("198.51.100.10")
        self.assertTrue(peer.online, "Discovery 层对手动设备就是这么设计的（不改它）")
        peer.last_seen = time.time() - 40.0
        self._route_to_peer(peer.node_id)
        self.assertEqual(self.node.desired_senders(), [], "手动对端同样要按活跃度判死活")

    def test_a_freshly_heard_peer_is_still_sent_to(self) -> None:
        self._peer(age=1.0)
        self._route_to_peer()
        wanted = self.node.desired_senders()
        self.assertEqual(len(wanted), 1, "刚听到的对端当然要继续发")
        self.assertEqual(self.node.status()["senders_blocked"], [])

    def test_the_grace_period_is_between_ten_and_fifteen_seconds(self) -> None:
        from audiomixing.engine import PEER_SILENCE_GRACE_S

        self.assertGreaterEqual(PEER_SILENCE_GRACE_S, 10.0, "太短会把 Wi-Fi 抖动判成离线")
        self.assertLessEqual(PEER_SILENCE_GRACE_S, 15.0, "太长会让用户觉得「关了还在发」")
        self._route_to_peer()
        self._peer(age=PEER_SILENCE_GRACE_S - 1.0)
        self.assertEqual(len(self.node.desired_senders()), 1)
        self._peer(age=PEER_SILENCE_GRACE_S + 1.0)
        self.assertEqual(self.node.desired_senders(), [])

    def test_recent_stats_keep_a_peer_alive_without_beacons(self) -> None:
        """信标停了但手机还在每 2 秒发统计包 → 它活着，不能停发。"""
        self._peer(age=60.0)
        self._route_to_peer()
        self._stats_from("peer-1", "198.51.100.10")
        self.node._track_peer_liveness()
        self.assertEqual(len(self.node.desired_senders()), 1)

    def test_an_old_stats_message_stops_counting(self) -> None:
        """旧消息不能反复当成新证据，否则死掉的对端永远不会被判死。"""
        self._peer(age=60.0)
        self._route_to_peer()
        self._stats_from("peer-1", "198.51.100.10")
        self.node.receiver.control_messages[-1]["_at"] = time.monotonic() - 60.0
        self.node._track_peer_liveness()
        self.assertEqual(self.node.desired_senders(), [],
                         "60 秒前的统计包不能再算「它还活着」")

    def test_audio_arriving_from_the_peer_keeps_it_alive(self) -> None:
        self._peer(age=60.0)
        self._route_to_peer()
        self.node.receiver.stats = lambda: {"streams": [{"node_id": "peer-1"}]}   # type: ignore[method-assign]
        self.node._track_peer_liveness()
        self.assertEqual(len(self.node.desired_senders()), 1)

    # ---- ① 自动收听 -------------------------------------------------------
    def test_a_listen_request_creates_a_route_marked_auto(self) -> None:
        self.node.config.save()
        self._stats_from("peer-1", "198.51.100.10",
                         want_listen=True)
        self.node._handle_peer_requests()
        auto = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(auto), 1, "对方请求收听 → 自动建一条路由")
        self.assertEqual(auto[0]["from"], ["peer-1"])
        self.assertEqual(auto[0]["to"], [self.node.config.node_id])
        self.assertIn("对方请求收听", auto[0]["label"])
        state = self.node.auto_listen_state()
        self.assertEqual(state["items"][0]["node_id"], "peer-1")
        self.assertTrue(state["items"][0]["active"])

    def test_a_listen_request_without_a_node_id_still_opens(self) -> None:
        """真机的 T_STATS 负载里【没有 node 字段】—— 手机端实测只报 name/target。

        电脑端原来拿不到 id 就在 _grant_listen_request 里直接早退，于是出现
        「手机按了发送、电脑不接收」：日志里反复的 "stream … has no local sink yet"。
        现在按来源 IP 从发现表反查 id，所以这条必须仍然开出自动路由。
        """
        self._peer(node_id="phone-abc", name="M332BF", host="198.51.100.10")
        self.node.config.save()
        self.node.receiver.control_messages.append({
            "t": "stats", "name": "M332BF", "_type": "STATS",
            "_from": "198.51.100.10:47831", "_at": time.monotonic(),
            "want_listen": True, "sending": True,          # ← 故意不带 node 字段
        })
        self.node._handle_peer_requests()
        auto = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(auto), 1, "没有 node 字段时也要靠 IP 反查出来、自动开门")
        self.assertEqual(auto[0]["from"], ["phone-abc"])
        self.assertEqual(auto[0]["to"], [self.node.config.node_id])

    def test_an_unknown_host_is_never_claimed(self) -> None:
        """发现表里查不到这台设备时不许瞎认领（宁可不开门，也不要认错人）。"""
        self.node.receiver.control_messages.append({
            "t": "stats", "name": "谁", "_type": "STATS",
            "_from": "10.9.9.9:47831", "_at": time.monotonic(), "want_listen": True,
        })
        self.node._handle_peer_requests()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [])

    def test_the_shared_gain_also_lands_without_a_node_id(self) -> None:
        """同根因：手机改过的音量（`gain_rx` = 它→我 那条链路）原来也认不到 id、落不下来，
        于是电脑「我送它的」停在旧值、手机「我听它的」是新值 ——
        用户截图里就是 1.45 对 0.68。
        """
        self._peer(node_id="phone-abc", name="M332BF", host="198.51.100.10")
        self.node.set_routes([{"id": "r1", "from": [self.node.config.node_id],
                               "to": ["phone-abc"], "gain": 1.45}], persist=False)
        ahead = int(time.time() * 1000) + 10_000        # 明显比本机的记录新
        self.node.receiver.control_messages.append({
            "t": "stats", "name": "M332BF", "_type": "STATS",
            "_from": "198.51.100.10:47831", "_at": time.monotonic(),
            "gain_rx": 0.68, "gain_rx_ts": ahead,       # ← 故意不带 node 字段
        })
        self.node._handle_peer_requests()
        rule = [r for r in self.node.routes if r["id"] == "r1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 0.68, places=3,
                               msg="认不到 id 就采纳不了对端的音量 —— 两端音量条会对不上")

    def test_a_listen_request_does_not_duplicate_an_existing_route(self) -> None:
        self.node.set_routes([{"id": "mine", "from": ["peer-1"], "to": ["self"]}], persist=False)
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"], "已经有路由就别再加一条")

    def test_revoking_removes_only_the_auto_route(self) -> None:
        self.node.set_routes([{"id": "mine", "from": ["peer-1"], "to": ["self"]}], persist=False)
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "已经有一条 它→本机 的路由了，不该再加一条")
        # 对方关掉送声音：只撤"自动"那种，用户自己建的路由必须原样留着
        self.node.receiver.control_messages.clear()
        self._stats_from("peer-1", "198.51.100.10", want_listen=False)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "撤销只能撤自动建的那条，用户自己建的不能动")

    def test_revoking_removes_the_auto_route_but_keeps_the_users(self) -> None:
        """自动建了一条之后，用户自己又建了一条 → 撤销只删自动那条。

        注意：协议里 want_listen 是幂等的（没有"取消"消息），撤销是【用户】的动作
        （面板上的「撤销」/关掉那台的收听），走的也是这个入口。
        ★ v2.27（Bug-3.1）：同语义的自动路由与手工路由会收敛成一条（手工优先）——
        两条都留着正是"取消收听要点两次"的根因，所以这里断言只剩手工那条。
        """
        self.node.config.save()
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.node.set_routes(list(self.node.routes)
                             + [{"id": "mine", "from": ["peer-1"], "to": ["self"]}],
                             persist=False)
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "同端点的自动路由与手工路由必须收敛成一条（手工优先）")
        self.node._revoke_auto_listen("peer-1")      # 用户点「撤销」
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "撤销只删自动那条，用户自己建的不动")

    # ---- ① 共享增益 -------------------------------------------------------
    def test_a_shared_gain_from_the_peer_updates_our_route(self) -> None:
        """对端的 gain 是"它→我"这条链路（= 我的「我听它的」）。"""
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 1.0}],
                             persist=False)
        self._stats_from("peer-1", "198.51.100.10", gain=0.5, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        rule = [r for r in self.node.routes if r["id"] == "in"][0]
        self.assertAlmostEqual(rule["gain"], 0.5, places=3, msg="两端共享同一个增益")

    def test_a_local_gain_change_wins_over_an_in_flight_value(self) -> None:
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["peer-1"], "gain": 1.2}],
                             persist=False)
        self.node.note_local_gain_change("r1")                 # 用户刚拖过这个滑块
        self._stats_from("peer-1", "198.51.100.10", gain_rx=0.5, gain_rx_ts=1700000000000)
        self.node._handle_peer_requests()
        rule = [r for r in self.node.routes if r["id"] == "r1"][0]
        self.assertAlmostEqual(rule["gain"], 1.2, places=3,
                               msg="本机刚动过，不能被对端在途的旧值抢回去")

    def test_gain_rx_updates_the_other_direction(self) -> None:
        """对端报的 gain_rx 是"我→它"这条链路（我的「我送它的」）。"""
        self.node.set_routes([
            {"id": "out", "from": ["self"], "to": ["peer-1"], "gain": 1.0},
            {"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 1.0},
        ], persist=False)
        self._stats_from("peer-1", "198.51.100.10", gain=0.4, gain_ts=1700000000000,
                         gain_rx=0.9, gain_rx_ts=1700000000000)
        self.node._handle_peer_requests()
        by_id = {r["id"]: r["gain"] for r in self.node.routes}
        self.assertAlmostEqual(by_id["in"], 0.4, places=3, msg="对端 gain → 我的「我听它的」")
        self.assertAlmostEqual(by_id["out"], 0.9, places=3, msg="对端 gain_rx → 我的「我送它的」")

    def test_shared_gain_works_on_an_auto_listen_route_authored_by_the_peer(self) -> None:
        """★ 2026-11-02 用户截图：PC「我听它的」=1.60，手机同一链路「我送它的」=0.40。

        智连自动建的 `从它到本机` 路由，`origin` 记的是**对端**（它请求的）。
        而共享增益/统计收件人原来只扫 `local_routes()`（origin=本机）—— 恰好把
        这条唯一的路由整条跳过：手机报来的 0.40 落不下来，本机的 1.60 也报不回去。
        链路事实必须读整张路由表，不能读"哪些行归我编辑"。
        """
        self._peer(node_id="peer-1", name="手机", host="198.51.100.10")
        self.node.set_routes([{
            "id": "auto-listen-peer1", "from": ["peer-1"],
            "to": [self.node.config.node_id], "gain": 1.6,
            "label": "智连自动收听", "origin": "peer-1", "auto": True,
        }], persist=False)
        # 本机要能读到这条链路的两个方向（含报给手机的 gain_rx）
        gains = self.node._link_gains("peer-1")
        self.assertAlmostEqual(float(gains["gain_rx"]), 1.6, places=3,
                               msg="这条链路是本机在听，必须报 gain_rx")
        self.assertIn("peer-1", self.node._listening_peer_targets(),
                      "只听手机时，统计收件人必须包含它")
        # 手机把它那边的「我送它的」拖到 0.40 → 本机「我听它的」必须落地
        self._stats_from("peer-1", "198.51.100.10", want_listen=True,
                         gain=0.4, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        rule = [r for r in self.node.routes if r["id"] == "auto-listen-peer1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 0.4, places=3,
                               msg="origin=对端 的自动收听路由也必须参与共享增益同步")

    def test_an_equal_value_does_not_bounce_back(self) -> None:
        """值一样时只对齐时间戳，绝不回推（否则两端会互相弹）。"""
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 0.7}],
                             persist=False)
        self._stats_from("peer-1", "198.51.100.10", gain=0.7, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        self.assertEqual(self.node._gain_ts["in"], 1700000000000, "时间戳要对齐，值不动")
        self.assertAlmostEqual([r for r in self.node.routes if r["id"] == "in"][0]["gain"],
                               0.7, places=3)

    def test_a_peer_whose_clock_is_behind_is_adopted_without_waiting_five_seconds(self) -> None:
        """★ 2026-10-16 真机：「拖动音量调大约 5 秒另一个设备同步」+「有时候听送两条音量条都自己刷回 1」。

        根因是同一条**时钟偏差兜底**：本机改过之后，5 秒内对端的值一律不采纳（ts 比不过），
        而 5 秒一到又会把对端那个值（老电脑上可能是"没有那条路由"时的默认 1.0）采纳进来 ——
        延迟和回弹是同一个原因。现在判据里不再有时钟：值不一样、本机 1.2 秒内没动过 → 采纳。
        """
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 1.0}],
                             persist=False)
        # 本机 2 秒前动过（还没到原来那条 5 秒线），而且对端的墙钟明显更慢
        self.node._gain_local_at["in"] = time.monotonic() - 2.0
        self.node._gain_ts["in"] = 9_999_999_999_999
        self._stats_from("peer-1", "198.51.100.10", gain=0.3, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        self.assertAlmostEqual([r for r in self.node.routes if r["id"] == "in"][0]["gain"], 0.3,
                               places=3, msg="对端时钟慢不该让它干等 5 秒")

        # 本机 0.3 秒前刚动过 → 不抢（用户还在拖）
        self.node._gain_local_at["in"] = time.monotonic() - 0.3
        self.node.receiver.control_messages.clear()
        self._stats_from("peer-1", "198.51.100.10", gain=1.7, gain_ts=1700000001000)
        self.node._handle_peer_requests()
        self.assertAlmostEqual([r for r in self.node.routes if r["id"] == "in"][0]["gain"], 0.3,
                               places=3, msg="用户正在拖的时候对端不许抢")

    def test_a_gain_is_not_reported_at_all_when_there_is_no_route(self) -> None:
        """★ 同一个反馈的另一半：「有时候会出现听送两条音量条都自己刷回 1」。

        `_link_gains` 原来不管有没有那条路由都报 `gain=1.0` —— "没有这条路由"和"音量就是 1.0"
        在对面看起来一模一样，对面就把这个凭空来的 1.00 采纳过去，音量条当场跳回 1。
        现在没路由就一个字段都不报（缺字段对面什么都不做）。
        """
        self._peer(node_id="peer-1", name="手机", host="198.51.100.10")
        self.assertEqual(self.node._link_gains("peer-1"), {},
                         "两条路由都没有时，不该报任何增益值")
        self.node.set_routes([{"id": "out", "from": [self.node.config.node_id], "to": ["peer-1"],
                               "gain": 0.75}], persist=False)
        gains = self.node._link_gains("peer-1")
        self.assertEqual(gains.get("gain"), 0.75)
        self.assertNotIn("gain_rx", gains, "没有「它→我」那条路由就别报反方向的值")

    def test_a_huge_timestamp_is_parsed_as_an_integer(self) -> None:
        """13 位毫秒时间戳不许经过浮点（手机端踩过：被截断成 1.7e8）。"""
        from audiomixing.engine import AudioMixingNode

        self.assertEqual(AudioMixingNode._as_ms(1700000000000), 1700000000000)
        self.assertEqual(AudioMixingNode._as_ms("1700000000000"), 1700000000000)
        self.assertEqual(AudioMixingNode._as_ms(1700000000000.0), 1700000000000)
        self.assertEqual(AudioMixingNode._as_ms(1.7e12), 1700000000000)
        self.assertEqual(AudioMixingNode._as_ms(None), 0)

    # ---- ① 每台设备的「智连」（2026-10-18 用户第 6 条） --------------------
    def test_a_device_without_smart_link_is_never_auto_added(self) -> None:
        """★ 默认**一台都不自动**（用户原话「默认关闭」）。

        没开智连的设备送声音过来，引擎什么都不做 —— 也不再有"被拒请求"要通报
        （用户第 6 条：那种警告不要出现在界面上）。
        """
        self.node.config.smart_link = []          # 默认状态
        self.node.config.save()
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "没开「智连」的设备不许被自动加进来")
        self.assertEqual(self.node.auto_listen_state()["items"], [])

    def test_a_device_with_smart_link_is_always_auto_added(self) -> None:
        """★ 用户第 1 条那个 bug 的回归：开了「智连」之后**每次都自动**，不会再被卡死。

        旧口径里有一份"关过一次就永不再自动"的黑名单，而它只在"对端不再请求"时才过期 ——
        可电脑一直在请求（want_listen 一直 true），于是手机端那台电脑被**永久**卡在名单里，
        电脑再怎么点「送给它」手机都不再自动打开收音。现在这条路径整个删掉了。
        """
        self.node.config.smart_link = ["peer-1"]
        self.node.config.save()
        for round_no in range(3):
            self.node.receiver.control_messages.clear()
            self._stats_from("peer-1", "198.51.100.10", want_listen=True)
            self.node._handle_peer_requests()
            self.assertEqual(len([r for r in self.node.routes if r.get("auto")]), 1,
                             "第 %d 轮：开着智连就一直自动打开，不许半路卡死" % (round_no + 1))
            self.node._revoke_auto_listen("peer-1")      # 用户点「撤销」：只撤这一条
            self.assertEqual([r for r in self.node.routes if r.get("auto")], [])

    def test_a_sending_device_with_smart_link_is_listened_to_without_want_listen(self) -> None:
        """★ 2026-10-26 用户钉死的 Wi-Fi 方向：本机开着「智连」的设备一开始送声音，
        本机自己就打开收听 —— 不等对方先发 want_listen。

        旧路径只认对端主动请求；对端不发（旧版本/手动路由/控制消息丢了）时，
        本机明明开着智连却什么都没发生。新路径由本机在收到无路由的流时排队建路由。
        """
        self.node.config.smart_link = []
        self.node._queue_auto_listen("peer-1", "手机", "198.51.100.10")
        self.node._apply_pending_auto_listen()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "没开智连：对方送声音也不许自动收听")

        self.node.config.smart_link = ["peer-1"]
        self.node._queue_auto_listen("peer-1", "手机", "198.51.100.10")
        self.node._apply_pending_auto_listen()
        autos = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(autos), 1, "开了智连：不用对方请求也要自己打开收听")
        self.assertEqual(autos[0].get("from"), ["peer-1"])
        self.assertEqual(autos[0].get("to"), [self.node.node_id])

    # ---- ★ 2026-11-04（第 11 条）：删除「本机播放」与 /api/bluetooth/local ----
    def test_the_local_play_button_and_engine_path_are_gone(self) -> None:
        """用户确认删掉这个入口；兜底改为「频道 → 音源 + 本机直通」，并在诊断里写明。"""
        from audiomixing import webui

        self.assertFalse(hasattr(self.node, "set_bluetooth_local_play"))
        self.assertFalse(hasattr(self.node, "bluetooth_local_playing"))
        self.assertFalse(hasattr(webui.AudioMixingApi, "bluetooth_local"))
        src = pathlib.Path(webui.__file__).read_text(encoding="utf-8")
        live = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("/api/bluetooth/local", live, "只检查真实路由，注释里说明删除不算残留")
        index = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        import re

        code = re.sub(r"<!--.*?-->", "", index, flags=re.S)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)   # 注释里记录"已删除"不算残留
        self.assertNotIn("本机播放", code, "前端按钮必须删掉")
        self.assertNotIn("btLocal(", code)
        self.assertIn("本机直通",
                      pathlib.Path(webui.__file__).read_text(encoding="utf-8"),
                      "驱动不出声时的兜底路径要在诊断里告诉用户")

    def test_turning_smart_link_off_stops_it_and_on_resumes_it(self) -> None:
        """关掉智连 → 不再自动；再开回来 → 又自动了（没有任何"记忆"挡在中间）。"""
        self.node.config.smart_link = []
        self.node.config.save()
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [])

        self.node.set_smart_link("peer-1", "手机", on=True)
        self.assertIn("peer-1", self.node.config.smart_link)
        self.node.receiver.control_messages.clear()
        self._stats_from("peer-1", "198.51.100.10", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual(len([r for r in self.node.routes if r.get("auto")]), 1)

        self.node.set_smart_link("peer-1", "手机", on=False)
        self.assertNotIn("peer-1", self.node.config.smart_link)
        self.assertNotIn("peer-1", [str(x) for x in self.node.config.smart_link])

    def test_turning_smart_link_on_takes_effect_at_once(self) -> None:
        """★ 2026-11-01 用户：「电脑的智连不是真实反馈，功能不完整」。

        旧实现只把名单写进配置：按钮变色了，引擎却要等下一包统计/下一轮清扫才动。
        这里钉住：对端**正在送声音**时打开「智连」→ 当场就有一条自动收听路由；
        关掉 → 当场撤回（两个方向都清）。没在送声音时不建（不白占输出设备）。
        """
        self._peer()
        # 假装这台手机已经有一路流进来（receiver.stats 是界面/引擎判"正在收"的口径）
        self.node.receiver.stats = lambda: {"streams": [
            {"node_id": "peer-1", "host": "198.51.100.10", "packets": 10}]}
        self.node.config.smart_link = []
        self.node.config.routes = []
        self.node.routes = []
        self.node.apply_smart_link_now()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "没开智连时不能平白建自动收听")

        self.node.config.smart_link = ["peer-1"]
        effect = self.node.apply_smart_link_now()
        auto = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(auto), 1, "开着智连、对端又在送声音 → 必须当场建好收听")
        self.assertIn("peer-1", [str(t) for t in auto[0].get("from") or []])
        self.assertEqual(effect["granted"], ["手机"])

        self.node.config.smart_link = []
        self.node.apply_smart_link_now()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "关掉智连要当场撤回自动建的收听（不等下一轮清扫）")

    def test_smart_link_on_does_not_open_a_route_when_nobody_is_sending(self) -> None:
        """没在送声音时只显示"等它送声音"，不要建一条空路由占着输出设备。"""
        self._peer()
        self.node.receiver.stats = lambda: {"streams": []}
        self.node.config.routes = []
        self.node.routes = []
        self.node.config.smart_link = ["peer-1"]
        effect = self.node.apply_smart_link_now()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [])
        self.assertEqual(effect["granted"], [])

    def test_routes_from_the_config_come_back_after_a_restart(self) -> None:
        """★ 2026-11-02 用户：「电脑端调节音量条手机端不同步」的另一半根因。

        `config.routes` 一直在写盘，但 `start()` 从来没把它读回内存 —— 每次重启
        （端口保存后的自动重启、用户自己重开）整张路由表都是空的：两条音量滑杆
        全变灰，手机端没有任何共享音量可同步。现场看起来就是"调音量不同步"。
        """
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = [{"id": "out1", "from": ["self-node"], "to": ["peer-1"],
                       "gain": 1.05, "mute": False, "codec": "pcm_s16"}]
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        try:
            node.start(announce=False)
            rule = [r for r in node.routes if str(r.get("id")) == "out1"]
            self.assertTrue(rule, "重启后上次的路由必须回到内存里（否则音量条全灰）")
            self.assertAlmostEqual(float(rule[0]["gain"]), 1.05, places=3)
        finally:
            try:
                node.stop()
            except Exception:
                pass

    # ---- ② 「正在发送的手机自动播放」并进「智连」（2026-10-18 用户图2） --------------
    def test_a_sending_phone_is_adopted_only_when_its_smart_link_is_on(self) -> None:
        """★ 用户（图2）：「「正在发送的手机自动播放」就是蓝牙的「智连」」。

        面板上那个全局勾（服务端 `auto_play`）整块删掉了 —— 收不收下一台正在送声音的设备，
        只看那张卡片上的「智连」：
          · 没开 → 什么都不做（默认全关，不替用户做主）；
          · 开了 → 自动把它加成「频道音源」（即自动播放它）。
        """
        from audiomixing.audio.backends import netmic as netmic_backend

        known = netmic_backend.open_mic("M332BF")        # setUp 里这台开着「智连」
        known.bytes_in = 10 ** 6
        known.last_push = time.monotonic()
        stranger = netmic_backend.open_mic("陌生手机")     # 这台不在任何名单里
        stranger.bytes_in = 10 ** 6
        stranger.last_push = time.monotonic()
        try:
            self.node.config.channel_sources = []
            self.node._auto_play_sources()
            self.assertEqual(self.node.config.channel_sources, ["M332BF"],
                             "只自动收下开了「智连」的那一台")

            self.node.config.smart_link = ["peer-1"]      # 把 M332BF 的「智连」关掉
            self.node.config.channel_sources = []
            self.node._auto_play_sources()
            self.assertEqual(self.node.config.channel_sources, [],
                             "关掉「智连」就不再自动收下它（默认关）")
        finally:
            netmic_backend.close_mic("M332BF")
            netmic_backend.close_mic("陌生手机")

    def test_bluetooth_smart_link_only_auto_connects_and_never_auto_captures(self) -> None:
        """蓝牙那一路：手机的声音在 Windows 里是一块采集设备「麦克风 (<手机名> A2DP SNK)」。

        ★ 2026-10-26 用户重新钉死语义：「蓝牙的智联应当是打开软件自动尝试一次连接
        有的蓝牙设备」。连上后手机媒体音频由 **Windows 直接播放**（用户真机已经
        听得到），本工具不能再自动把这块 A2DP SNK 塞进「音源」—— 那会采第二遍、
        再放一遍，和系统播放叠在一起。

        所以这里钉住：即使蓝牙卡片的「智连」（服务端 autoconnect）开着，
        `_auto_play_sources()` 也不许改动 channel_sources。要转发手机声音时，
        用户仍然可以手动在「音源」里选 A2DP SNK。
        """
        from audiomixing.engine import bt_sink_phone_name

        self.assertEqual(bt_sink_phone_name("麦克风 (手机A A2DP SNK)"), "手机A")
        self.assertEqual(bt_sink_phone_name("耳机 (某耳机)"), "", "只有 A2DP 采集设备才算手机进来的声音")
        self.assertEqual(bt_sink_phone_name(""), "")

        self.node.devices = [
            audio_base.AudioDevice("c1", "麦克风 (手机A A2DP SNK)",
                                   audio_base.KIND_SOURCE, "wasapi", "capture"),
        ]
        self.node.config.autoconnect = ["手机A"]
        self.node.config.channel_sources = []
        self.node.config.local = False
        self.node._auto_play_sources()
        self.assertEqual(self.node.config.channel_sources, [],
                         "蓝牙音频由 Windows 直接播放，绝不能再自动收进音源")
        self.assertFalse(self.node.config.local, "也不许因此打开本机直通")

    # ---- ★ 2026-10-19 孤儿路由（整条都是没见过的设备）清扫 --------------------
    def test_a_route_about_devices_we_never_met_is_swept(self) -> None:
        """★ 用户连着两次在面板里看到 `preview-…`「不在线 / 不可达」的点和红线。

        那是我为了改功能在同一台电脑上起的**临时测试节点**留下的路由：路由表全网共享，
        被合进某台机器的内存表之后**再没人清**（作者早就不在了），两台机器还会互相推来推去
        —— 实测本机删掉 8 条后另一台还留着 7 条，只删一边就会被推回来。
        判据保守：只有"作者 + 两端，这台电脑一个都没见过"才清；宽限期内一条都不动。
        """
        self.node._route_first_seen = {}
        self.node.set_routes([
            {"id": "ghost", "from": ["preview-aaaa-1111"], "to": ["preview-bbbb-2222"],
             "origin": "preview-aaaa-1111"},
            {"id": "mine", "from": [self.node.config.node_id], "to": ["2025BJB"],
             "origin": self.node.config.node_id},
        ], persist=False)
        # 宽限期没过 → 一条都不清（防止"路由先到、对方信标后到"被误删）
        self.node._sweep_orphan_routes()
        self.assertIn("ghost", [r["id"] for r in self.node.routes],
                      "刚进表的路由不许立刻扫掉")

        old = time.monotonic() - self.node.ORPHAN_GRACE_S - 1
        self.node._route_first_seen = {"ghost": old, "mine": old}
        self.node._sweep_orphan_routes()
        ids = [r["id"] for r in self.node.routes]
        self.assertNotIn("ghost", ids, "整条说的设备都没见过 → 清掉")
        self.assertIn("mine", ids, "本机自己建的路由一条都不许动")

    def test_a_route_to_an_offline_peer_is_kept(self) -> None:
        """对方只是关机/睡着了，路由必须留着。

        注册表里的条目在本进程内**不会被删**（掉线只是 online=False），所以"查不到"才等于
        "从来没见过"——这正是这条判据能保守到"不影响正常路由"的原因。
        """
        self._peer(age=600.0)                       # 十分钟没有信标 → 判离线
        self.node._route_first_seen = {}
        self.node.set_routes([{"id": "keep", "from": ["peer-1"], "to": ["self"],
                               "origin": "peer-1"}], persist=False)
        self.node._route_first_seen = {
            "keep": time.monotonic() - self.node.ORPHAN_GRACE_S - 1}
        self.node._sweep_orphan_routes()
        self.assertIn("keep", [r["id"] for r in self.node.routes],
                      "对方只是离线，不许当成孤儿清掉")

    # ---- ★ 2026-10-19 串台：认得出的发送方没有收听路由，绝不许挂到别的端上 ----
    def test_a_known_sender_without_any_route_is_never_played(self) -> None:
        """★ 用户实测的严重串台（他原话："我怀疑你在代码里把路由混淆为广播了"）。

        现场：只在 2024TSJ 上开了「收听 2025BJB」（全表**唯一一条**入向路由），
        手机 瘾控 送声音过来 —— 2024TSJ 居然放出了手机的声音，而且监控页上两个流
        **共用同一个 route_id**（c2c2d7f6），那就是"被挂到别人那条路由上"的指纹。

        根因：`_attach_stream_routes` 里"只有一个播放端就兜底"那条老路，本来只该给
        **认不出身份**的手动对端（信标丢了 / AP 隔离），结果认得出身份、但本机没开它收听的
        发送方也走了兜底。现在只给真认不出身份的兜底。

        ★ 2026-11-04（审计 A1）：又把"认不出身份"收紧成"**用户手动添加的对端**"。
        原因：认不出身份不等于手动对端 —— 没接受的点对点单播，只要对方信标丢了
        （Wi-Fi 丢包 / AP 隔离 / 换了 IP），在这台电脑上就同样表现为"认不出身份"，
        于是照样被兜底收下。核对：没手动加过 127.0.0.1 → 一个端都不挂；加过才兜底。
        """
        class _Info:
            def __init__(self, node_id: str) -> None:
                self.node_id = node_id

        self._peer(node_id="peer-1", name="手机")        # 认得出的设备（注册表里有）
        self.node.sinks = {"only-sink": object()}          # 恰好只有一个播放端
        self.node.routes = []
        self.node.config.manual_peers = []
        self.assertEqual(self.node._attach_stream_routes(_Info("peer-1"), ("127.0.0.1", 1)),
                         set(),
                         "认得出身份、但本机没给它开收听 → 一个端都不许挂（挂了就是串台）")
        self.assertEqual(self.node._attach_stream_routes(_Info(""), ("127.0.0.1", 1)),
                         set(),
                         "认不出身份、又不是手动对端 → 不许兜底（这是没接受的点对点的第二条路）")
        self.node.config.manual_peers = [{"name": "手动电脑", "host": "10.0.0.9"}]
        self.assertEqual(self.node._attach_stream_routes(_Info(""), ("10.0.0.9", 1)),
                         {"only-sink"},
                         "用户手动添加过的对端，信标丢了也要兜底（这是原本的意图）")

    # ---- ★ 2026-11-04（审计 A4）：一条路由写了两台来源时，取最安静的那台 ----
    def test_a_route_with_two_sources_uses_the_quietest_volume(self) -> None:
        """改前：`_peer_media_volume_source` 命中第一个 token 就返回。

        一条具名路由 `from=[A, B]` 送进同一个播放端时，整条 sink 只有一份 gain，
        于是 A 的 0.9 会把 B 调到 0.2 的事实完全盖掉。改成取最小值。
        """
        now = time.monotonic()
        self.node._peer_volume["peer-a"] = (0.9, now)
        self.node._peer_volume["peer-b"] = (0.2, now)
        value, key = self.node._peer_media_volume_source({"from": ["peer-a", "peer-b"]})
        self.assertAlmostEqual(value, 0.2, places=3, msg="两台来源必须取最安静的那台")
        self.assertIn(key, ("peer-a", "peer-b"))
        self.assertAlmostEqual(
            self.node._peer_media_volume({"from": ["peer-a"]}), 0.9, places=3,
            msg="单源路由不受影响（行为不变）")

    # ---- ★ 2026-11-04（审计 A1 的回归）：开播时没认出身份的流必须能补上 ----
    def test_a_stream_that_started_before_the_beacon_gets_its_identity_later(self) -> None:
        """改前：`node_id` 只在第一个包时解析一次，之后永不更新。

        信标比音频晚到（同机自检、Wi-Fi 刚连上、对方刚启动）时，这条流拿着空身份
        匹配不上任何具名路由，既挂不上路由也不建播放端 —— 整场没声音且不会自愈。
        `_refresh_stream_identities()` 每半秒补一次空身份。
        """
        class _Info:
            node_id = ""
            host = "10.0.0.9"

        info = _Info()
        self.node.receiver.streams[9] = info
        self.node._refresh_stream_identities()
        self.assertEqual(info.node_id, "", "发现表里还没这台，就不许瞎认")
        # ★ 2026-11-04（审计 B1，第 2 轮）：手动添加只写 `manual:IP` 占位，那不是
        #   路由里认得的身份。把它当成 node_id 会让这条流永远匹配不上具名路由。
        self.node.registry.add_manual("10.0.0.9", 47831, "手动电脑")
        self.node._refresh_stream_identities()
        self.assertEqual(info.node_id, "",
                         "手动占位不算身份（改前会被写成 manual:10.0.0.9，整场挂不上路由）")
        # 同一 IP 上真实身份晚到（信标 / HTTP 探测）时，必须补上真实 id。
        self.node.registry.update({"wl": 1, "id": "real-node", "name": "真名",
                                   "audio_port": 47831}, "10.0.0.9")
        self.node._refresh_stream_identities()
        self.assertEqual(info.node_id, "real-node",
                         "真实身份一到，这条流就要补上（且不被 manual:IP 盖住）")
        self.node.receiver.streams[10] = _Info()
        self.node.receiver.streams[10].node_id = "peer-1"
        self.node._refresh_stream_identities()
        self.assertEqual(self.node.receiver.streams[10].node_id, "peer-1",
                         "已经认出身份的不许被覆盖")

    # ---- ★ 2026-11-04（审计 B1，第 2 轮）：手动占位不许盖住真实身份 ----
    def test_a_manual_placeholder_never_shadows_the_real_identity(self) -> None:
        """`add_manual()` 写的 `manual:IP` 记录可能比真实信标先入表。

        `registry.resolve(host)` 按字典顺序返回，先命中占位时，流身份会被写成
        `manual:IP`；之后即使真实信标到了也不会再更新（只补空身份）。这里直接钉住
        "同 host 两条记录、占位在前"时也必须解析出真实 id。
        """
        self.node.registry.add_manual("10.0.0.9", 47831, "手动电脑")
        self.node.registry.update({"wl": 1, "id": "real-node", "name": "真名",
                                   "audio_port": 47831}, "10.0.0.9")
        self.assertEqual(self.node._node_id_for_host("10.0.0.9"), "real-node",
                         "占位 manual:10.0.0.9 不许盖住真实 id")
        self.assertEqual(self.node._node_id_for_host(""), "", "没有地址就不认领")
        self.assertEqual(self.node._node_id_for_host("10.0.0.8"), "",
                         "发现表里没有这台，不许瞎认")

    def test_the_first_packet_records_only_the_real_identity(self) -> None:
        """首包解析也必须走同一条规则，否则占位身份会在开播那一刻就被写死。"""
        from audiomixing.net.rtpaudio import StreamInfo

        self.node.registry.add_manual("10.0.0.9", 47831, "手动电脑")
        self.node.registry.update({"wl": 1, "id": "real-node", "name": "真名",
                                   "audio_port": 47831}, "10.0.0.9")
        info = StreamInfo(ssrc=7)
        self.node._on_stream_start(info, ("10.0.0.9", 47831))
        self.assertEqual(info.host, "10.0.0.9")
        self.assertEqual(info.node_id, "real-node", "首包不许记成 manual:10.0.0.9")
        self.assertEqual(info.node_name, "真名")


    def test_a_non_finite_route_gain_cannot_poison_the_sink(self) -> None:
        """改前：`gain: float(raw.get("gain", 1.0))` 原样接收 JSON 的 NaN / Infinity。

        一旦写进路由，`apply_gain` 把 NaN 乘进每一声采样，整条输出变成 NaN ——
        听感是"彻底没声音"，而且不重启好不了（NaN 会一直留在总线上）。
        """
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(self.node._normalize_route({"gain": bad})["gain"], 1.0,
                             "非有限值必须退回 1.0（改前会原样写进播放端）")
        self.assertEqual(self.node._normalize_route({"gain": "abc"})["gain"], 1.0)
        self.assertEqual(self.node._normalize_route({"gain": 100})["gain"], 8.0,
                         "超大增益夹到上限，避免削顶成方波")
        self.assertEqual(self.node._normalize_route({"gain": -5})["gain"], 0.0,
                         "负增益夹到 0（不允许反相）")
        rule = self.node._normalize_route({"sink_device": {"evil": 1}})
        self.assertIsInstance(rule["sink_device"], str,
                              "设备字段必须是字符串，否则渲染线程比较时抛 TypeError")

    # ---- ★ 2026-10-19 实测：智连没开，按钮也会自己亮（对端旧版本 + 本机请求）--------
    def test_we_never_ask_the_peer_to_auto_listen_when_its_smart_link_is_off(self) -> None:
        """★ 用户实测：「无论是否开启智连，设备都自动开启「听」「送」」。

        根因之一是**本机自己**那条请求：送声音时原来**无条件**发 `want_listen=True`
        （"我在送你声音，请自动打开收听"）—— 对端（尤其旧版本：全局自动接受、默认开着）
        收到就替它自己开了收听，等于**绕过本机的智连替对端开了「听」**。
        现在智连没开就不发这条请求（对端收不到，旧版本也一样）。
        """
        self._peer(node_id="peer-1", name="手机")
        self.node.config.smart_link = []
        self.assertFalse(self.node._smart_wants_listen("peer-1"), "没开智连就不许请求它自动开听")
        self.node.config.smart_link = ["peer-1"]
        self.assertTrue(self.node._smart_wants_listen("peer-1"), "开了智连才请求它自动开听")

    def test_an_auto_route_from_an_old_peer_is_recognised_and_cleared(self) -> None:
        """★ 2026-10-19：判据必须跨版本 —— 旧版本对端建的那条自动路由**没有 `auto` 字段**。

        只按 `auto` 判的话，旧版本（另一台电脑还是 v1.34 / 手机没重装）推来的自动收听路由
        会被当成"用户手动建的"：照样收发、两颗按钮照样自己亮。所以 id 前缀 `auto-listen-`
        与 label 文本也要认。认出之后还要**清掉**（两个方向都清），否则按钮还是会亮。
        """
        self._peer(node_id="peer-1", name="手机")
        self.node.config.smart_link = []
        # ① 旧版本对端推来的两条：一条"它听本机"（本机→它），一条"本机听它"（它→本机）
        old_peer_routes = [
            {"id": "auto-listen-peer1", "from": ["self"], "to": ["peer-1"],
             "label": "对方请求收听（自动）", "origin": self.node.config.node_id},   # 无 auto 字段
            {"id": "auto-listen-peer2", "from": ["peer-1"], "to": ["self"],
             "label": "对方请求收听（自动）", "origin": "peer-1"},
        ]
        self.node.set_routes(old_peer_routes, persist=False)
        self.assertTrue(all(self.node._is_auto_route(r) for r in self.node.routes),
                        "旧版本那两条也要认出来是自动路由")
        self.node._sweep_unwanted_auto_routes()
        self.assertEqual(self.node.routes, [], "智连没开 → 两个方向的自动路由都清掉（按钮才会如实熄灭）")

        # ② 开了智连 → 这两条要留着（那是应该的自动行为）。
        #    用**新的 id**：上一步清掉时把旧 id 记进了"已删除"名单（120 秒内挡住对端再推同样的 id），
        #    那是刻意的行为，不该用同一批 id 去验"留着"。
        self.node.config.smart_link = ["peer-1"]
        kept_routes = [
            {"id": "auto-listen-keep1", "from": ["self"], "to": ["peer-1"],
             "label": "对方请求收听（自动）", "origin": self.node.config.node_id},
            {"id": "auto-listen-keep2", "from": ["peer-1"], "to": ["self"],
             "label": "对方请求收听（自动）", "origin": "peer-1"},
        ]
        self.node.set_routes(kept_routes, persist=False)
        self.node._sweep_unwanted_auto_routes()
        ids = sorted(str(r["id"]) for r in self.node.routes)
        self.assertEqual(ids, ["auto-listen-keep1", "auto-listen-keep2"], "开了智连就该留着")

    # ---- ★ 2026-10-19 对端自己"自动来听本机"：智连没开就不给声音 --------------
    def test_a_remote_auto_listen_is_refused_unless_its_smart_link_is_on(self) -> None:
        """★ 用户：「智连按键反应的情况不真实 —— 智连未开启，设备也会自动开启「听」」。

        对端自己"自动开始听本机"时（它的引擎按**它的**名单决定的），会在共享表里放一条
        `auto` 路由 本机→它（label=「对方请求收听（自动）」）。按第 6 条的口径：
        **只有这张卡片开着「智连」才自动接受它的「听」**，没开就不给声音 ——
        而且要让面板能写出"已拦下"（不能只靠"居然还听得到声音"去反推）。
        用户自己点「送给它」建的那条（不带 auto）在任何情况下都照发。
        """
        self._peer(node_id="peer-1", name="手机")
        auto_rule = {"id": "auto-listen-peer1", "from": ["self"], "to": ["peer-1"],
                     "label": "对方请求收听（自动）", "origin": self.node.config.node_id,
                     "auto": True}
        # ① 智连关着 → 一条都不发，且面板能看到"被智连拦下"
        self.node.config.smart_link = []
        self.node.set_routes([auto_rule], persist=False)
        self.assertEqual(self.node.desired_senders(), [], "智连没开就不许给声音")
        blocked = [b for b in self.node.blocked_senders() if b.get("smart_blocked")]
        self.assertEqual(len(blocked), 1, "拦下的那条要让面板看得到")
        self.assertIn("智连", blocked[0]["text"])
        # ② 智连开着 → 正常发（这就是"自动接受它的听"）
        self.node.config.smart_link = ["peer-1"]
        self.assertTrue(self.node.desired_senders(), "开了「智连」就该自动接受它的「听」")
        # ③ 用户自己点「送给它」建的普通路由，任何时候都发
        self.node.config.smart_link = []
        self.node.set_routes([{"id": "manual-out", "from": ["self"], "to": ["peer-1"]}], persist=False)
        senders = self.node.desired_senders()
        self.assertTrue(senders, "手动送的路由不受「智连」影响")
        self.assertFalse([b for b in self.node.blocked_senders() if b.get("smart_blocked")])

    # ---- ① 发出去的报文（给手机端对齐用） --------------------------------
    def test_the_python_sender_announces_request_and_gain(self) -> None:
        """T_STATS 的负载里必须真的带 request_listen / route_gain，字段名就是协议。"""
        import socket

        from audiomixing import proto

        port = free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))
        sock.settimeout(3.0)
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["peer-1"], "gain": 0.75}],
                             persist=False)

        class _FakeSession:
            host = "127.0.0.1"
            port = 0            # 类体里不能写 `port = port`（那会去全局找，NameError）
            node_id = "peer-1"
            route_id = "r1"
            packets_sent = 42
            bytes_sent = 80640

        _FakeSession.port = port
        self.node.sender.sessions["r1|peer-1"] = _FakeSession()   # type: ignore[assignment]
        # 控制包是从接收端那个 socket 发出去的；测试里不启动整个节点，直接给它一个。
        self.node.receiver._sock = sock
        self.node._send_peer_stats(force=True)
        try:
            data, _addr = sock.recvfrom(4096)
        finally:
            sock.close()
        pkt = proto.decode(data)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt.type, proto.T_STATS, "复用 T_STATS（旧版本忽略未知字段，不会崩）")
        body = pkt.json()
        # ★ 字段名与手机端（task-17）逐字对齐，不许另写一套 ★
        #   只钉"有那条路由的方向"——反方向没有路由时**一个字段都不报**（见下一条断言）。
        for key in ("want_listen", "gain", "gain_ts"):
            self.assertIn(key, body, "手机端要对齐的字段：%s" % key)
        self.assertNotIn("gain_rx", body,
                         "反方向没有路由 → 不许报一个凭空来的 1.0（那会把对端音量条刷回 1）")
        self.assertTrue(body["want_listen"], "幂等：还在送就一直 true")
        self.assertAlmostEqual(float(body["gain"]), 0.75, places=3,
                               msg="gain = 我→你 这条链路（我的「我送它的」）")
        self.assertIsInstance(body["gain_ts"], int, "时间戳必须是整数毫秒")

    # ---- ★ 2026-10-16 真机崩溃（栈溢出）与"同步太慢"------------------------------
    def test_a_phone_stats_message_never_recurses_into_set_routes(self) -> None:
        """★ 2026-10-16 真机崩溃的回归：应用在拖音量时**整个消失**。

        WER 记录：`Audio Mixing v1.31.exe` / `audiomixing_launcher.dll` / **0xC00000FD
        （栈溢出）**，崩在用户拖音量那几分钟里。探针复现出的回路是：

            set_routes() → apply_routes() → _handle_peer_requests()
              → _apply_shared_gain() / _grant_listen_request() → set_routes() → apply_routes() → …

        `set_routes` 结尾要 reconcile，而 reconcile 又会处理对端刚发来的控制消息、
        再回头改路由表 —— 手机每 2 秒一条 T_STATS（带 want_listen + gain/gain_rx）
        就会在这条回路上嵌套几百层（实测 198 层）。源码运行时只是慢
        （每条控制消息白跑几百遍完整 reconcile，这正是用户说的"调音量反应很久"），
        Nuitka 打包版每帧占的 C 栈更大，1MB 线程栈当场撑爆。

        修法是 `apply_routes` 里的**同线程重入保护**：内层直接返回，外层那一轮
        带着新的路由表把收发两端 reconcile 完（_handle_peer_requests 在 desired_senders
        之前执行，所以不会漏事）。这条测试断言两件事：不许再递归、而且效果没丢。
        """
        self._peer(node_id="phone-abc", name="M332BF", host="198.51.100.10")
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["phone-abc"],
                               "gain": 1.0}], persist=False)
        ahead = int(time.time() * 1000) + 10_000
        self._stats_from("phone-abc", "198.51.100.10", want_listen=True,
                         gain=0.8, gain_ts=ahead, gain_rx=1.28, gain_rx_ts=ahead)
        self.node.apply_routes()          # 旧代码：这里就会无限递归（打包版直接崩）
        self.assertGreaterEqual(int(getattr(self.node, "_apply_reentered", 0)), 1,
                                "这条消息必须真的触发过重入 —— 否则这条测试是假通过")
        # 效果一样不能丢：对端请求收听 → 自动那条路由要在，共享增益也要落下去
        auto = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(auto), 1, "自动收听那条路由必须还在（内层被挡掉不等于事情没做）")
        rule = [r for r in self.node.routes if r["id"] == "r1"][0]
        self.assertAlmostEqual(float(rule["gain"]), 1.28, places=3,
                               msg="对端报来的 gain_rx 要落到「我送它的」那条路由上")

    def test_a_local_change_is_pushed_without_waiting_for_the_two_second_cycle(self) -> None:
        """★ 用户真机反馈第 3、5 条：「两边的音量调同步太慢了，要反应很久」、
        「跨端自动启停（电脑开始送音→手机自动开收音 / 电脑停送→自动关）同样慢」。

        唯一携带 want_listen / gain / gain_rx 的消息是 `_send_peer_stats`，而它原来是
        **2 秒一次的节流循环** —— 用户拖完滑块，对端最多要等 2 秒（再叠上对端自己的周期）。
        现在本机一改路由表就 `push_peer_stats_now()`：置一个"这一次别等"的标记，
        维护循环下一次跑 `_send_peer_stats` 时就不再受那 2 秒限制（并且它会被立刻叫醒）。
        """
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        port = sock.getsockname()[1]        # 控制包要发到这个端口，才看得到

        class _FakeSession:
            host = "127.0.0.1"
            port = 0            # 类体里不能写 `port = port`（那会去全局找，NameError）
            node_id = "peer-1"
            route_id = "r1"
            packets_sent = 42
            bytes_sent = 80640

        _FakeSession.port = port

        self._peer(node_id="peer-1", name="手机", host="198.51.100.10")
        self.node.sender.sessions["r1|peer-1"] = _FakeSession()   # type: ignore[assignment]
        self.node.routes = [{"id": "r1", "from": [self.node.config.node_id],
                             "to": ["peer-1"], "gain": 0.75}]
        self.node.receiver._sock = sock      # 控制包就是从这个 socket 发出去的

        def received() -> bool:
            try:
                sock.recvfrom(4096)
                return True
            except Exception:
                return False

        self.node._last_peer_stats = time.monotonic()     # 刚刚发过 → 正常路径会被节流
        self.node._send_peer_stats()
        self.assertFalse(received(), "2 秒节流窗口内不该发（这是原来的行为）")
        # 本机动了一下（面板拖音量 / 加删路由都会走到这里）→ 应该立刻补发一条
        self.node.push_peer_stats_now()
        self.node._send_peer_stats()
        self.assertTrue(received(), "本机改过音量/路由之后要立刻推给对端，不能再等那 2 秒")
        sock.close()

    def test_a_powershell_output_is_decoded_with_chinese(self) -> None:
        """★ 用户图1：「就是中文出现了点问题」（网络自检里规则路径中文变 "??" 方块）。

        PowerShell 5.1 在中文 Windows 上重定向输出用的是**系统编码（GBK）**，
        而面板那边原来写 `encoding="utf-8", errors="replace"` —— 解不出来就整片替换成方块：
        `Audio Mixing - LAN audio casting → E:\\AI\\Audio\\AudioMixing\\??\\v1.12-…`。
        现在先按字节读回来，再试 UTF-8、失败退系统 ANSI。
        """
        from audiomixing.netcheck import decode_powershell

        text = "版本备份\\v1.12-20261014-0406\\成品\\audiomixing v1.12.exe"
        self.assertEqual(decode_powershell(text.encode("utf-8")), text, "UTF-8 的输出照旧能解")
        self.assertEqual(decode_powershell(text.encode("mbcs")), text,
                         "GBK（中文 Windows 的默认）也必须能解对 —— 图1 那个乱码就是这条")
        self.assertNotIn("?", decode_powershell(text.encode("mbcs")))


class TestFirewallScriptCleansUpOldRules(unittest.TestCase):
    """★ 2026-10-15 自查 + 2026-10-27 用户要求：规则绑程序路径，每换一版就多一堆旧规则。

    用户机器上有大量 v1.11/v1.12/版本备份 的 exe，旧逻辑只删“文件已不存在”的规则，
    于是 `Audio Mixing*` 名下堆到几百条。用户明确要求「直接清理旧版本规则」：
    点一次「放行防火墙」= 清掉所有 Audio Mixing*/WaveLink* 旧规则，再为当前 exe
    重建 TCP/UDP（入站+出站）。只匹配我们自己的显示名，不碰其它软件。
    """

    def test_the_script_clears_all_old_versions_and_permits_this_exe(self) -> None:
        import pathlib
        from unittest import mock

        from audiomixing import tray

        with mock.patch.object(tray.ctypes.windll.shell32, "ShellExecuteW", return_value=42):
            path = tray.add_firewall_rules()          # 弹 UAC 那句被挡掉了，只生成脚本
        text = pathlib.Path(path).read_text(encoding="mbcs", errors="replace")
        self.assertIn("Remove-NetFirewallRule", text, "要清掉旧版本规则")
        self.assertIn("Audio Mixing*", text, "只匹配新版显示名")
        self.assertIn("WaveLink*", text, "旧版显示名也要清")
        self.assertNotIn("Test-Path", text,
                         "现在直接清全部旧版本规则，不再只删“文件已不存在”的")
        self.assertIn('name="Audio Mixing TCP"', text, "当前这份 exe 照旧要放行")
        self.assertNotIn("chcp", text, "批处理不带 chcp（中文路径会变乱码）")


class TestNoHardcodedMachinePaths(unittest.TestCase):
    """★ 2026-10-15 自查发现的真 bug：代码里躺着**别的机器/别的目录**的绝对路径。

    现场：`tools/selftest_twonode.py` 和 `tools/loopback_probe.py` 开头都写着一行
    `sys.path.insert(0, r"<另一个目录的绝对路径>")` —— 那个目录早就不存在了，于是这两个
    **文档里写着**的自检怎么跑都起不来（ModuleNotFoundError: No module named 'audiomixing'），
    而且报错完全指不到真凶。`tools/check_drag_logic.js` 同理：包目录改名成 audiomixing 之后
    它还去旧目录找 index.html（那一份已经删掉了，它测的也是早就删掉的拖拽路由）。
    更严重的一处是 `audiomixing/netcheck.py` —— 那是**打包进 exe** 的代码，里面却拿一个
    机器上的目录名去猜"防火墙规则是不是别处来的"，换台机器只会一直误报。

    这类字面量一旦留下，换机器/换目录就静默失效，所以钉成一条规则。
    """

    # 机器相关的字面量。注意：**不允许**在注释里也留着它们（那样这条规则就不成立了）——
    # 要说明"以前写的是什么"，就用文字描述，别把字面量抄进去。
    BAD = ("deep blue", "E:\\", "E:/")

    def test_no_machine_specific_paths_in_code(self) -> None:
        bad = []
        roots = [ROOT / "audiomixing", ROOT / "tools",
                 ROOT / "android" / "app" / "src", ROOT / "android" / "tools"]
        for root in roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in (".py", ".js", ".java", ".sh"):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for needle in self.BAD:
                    if needle in text:
                        bad.append("%s 含 %r" % (path.relative_to(ROOT).as_posix(), needle))
        self.assertEqual(bad, [], "机器相关的绝对路径/旧目录名不该出现在代码里：%s" % bad)

    def test_the_two_probes_bootstrap_from_their_own_location(self) -> None:
        """两个自检必须从**自己所在的文件**反推仓库根，而不是写死一个路径。"""
        for name in ("selftest_twonode.py", "loopback_probe.py"):
            text = (ROOT / "tools" / name).read_text(encoding="utf-8")
            self.assertIn("__file__", text, "%s 要从 __file__ 反推仓库根" % name)
            i = text.index("sys.path.insert")
            line = text[i:text.index("\n", i)]
            self.assertNotIn("\\", line, "%s 的 sys.path 不许写死路径：%s" % (name, line))


class TestBuildSelfCheckNeverTouchesUserConfig(unittest.TestCase):
    """★ 2026-10-16 审计工具抓到的真事：**打包自检把随机端口写进了用户真实配置**。

    自检为了不撞用户正在跑的实例，改成跑在临时空端口上 —— 但 exe 的 `serve` 会顺手把
    web_port/audio_port 存进配置，于是那两个随机端口就成了用户的正式端口：
    他下次双击，面板跑到 57590 那种端口上，手机和另一台电脑都找不到他。
    （`tools/selftest_twonode.py` 早就给自己一个临时配置目录，打包自检漏了这一步。）

    所以这条钉死：自检必须给 exe 一个一次性的 `AUDIOMIXING_HOME`，而且真的通过
    `Popen(env=...)` 传下去 —— 只写在注释里不算。
    """

    def test_the_self_check_runs_against_a_throwaway_config_dir(self) -> None:
        text = (ROOT / "tools" / "build_nuitka.py").read_text(encoding="utf-8")
        tail = text[text.index("def main("):]
        # 只查代码形状：注释里为了说明这件事必须提到这个变量名，不能算证据。
        code = "\n".join(l for l in tail.splitlines() if not l.strip().startswith("#"))
        self.assertIn("AUDIOMIXING_HOME", code, "自检要给 exe 一个临时配置目录")
        self.assertIn("env=env", code, "那个环境变量必须真的传给 exe（Popen 的 env=）")
        self.assertIn("mkdtemp", code, "临时配置目录要一次性生成")
        self.assertIn("rmtree", code, "用完要删掉，源码/临时目录里不留垃圾")


class TestRenameKeepsOldNamesWorking(unittest.TestCase):
    """★ 2026-10-15：项目从 WaveLink 改名成 Audio Mixing 之后，**旧名字不能一改就断**。

    换名会牵连到几处"外面还在用旧名字"的地方，它们必须继续认：
      * 环境变量 —— v1.22 及更早的启动器/脚本导出的还是 WAVELINK_HOME / WAVELINK_APK；
        新名优先、旧名兜底，否则那些调用方的配置目录/安装包会凭空找不到。
      * 类名 —— 从 WaveLinkNode / WaveLinkApi 换成带新品牌的名字，且旧名不许再定义
        （改一半留一半最容易出"某个文件还在 import 旧类名"的隐形故障）。
    """

    @staticmethod
    def _env(**overrides: str):
        """只留需要的那几个环境变量：先清掉两个 HOME，再看 overrides。"""
        import os
        from unittest import mock

        env = dict(os.environ)
        env.pop("AUDIOMIXING_HOME", None)
        env.pop("WAVELINK_HOME", None)
        env.pop("AUDIOMIXING_APK", None)
        env.pop("WAVELINK_APK", None)
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_the_new_env_name_is_honoured(self) -> None:
        from audiomixing import config as config_mod

        with self._env(AUDIOMIXING_HOME="C:\\tmp\\am-home"):
            self.assertEqual(str(config_mod.config_dir()), "C:\\tmp\\am-home")

    def test_the_old_env_name_still_works(self) -> None:
        from audiomixing import config as config_mod

        with self._env(WAVELINK_HOME="C:\\tmp\\wl-home"):
            self.assertEqual(str(config_mod.config_dir()), "C:\\tmp\\wl-home")

    def test_the_new_env_name_wins_when_both_are_set(self) -> None:
        from audiomixing import config as config_mod

        with self._env(AUDIOMIXING_HOME="C:\\tmp\\new", WAVELINK_HOME="C:\\tmp\\old"):
            self.assertEqual(str(config_mod.config_dir()), "C:\\tmp\\new")

    def test_the_apk_env_var_knows_both_names(self) -> None:
        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            new = pathlib.Path(tmp) / "new.apk"
            old = pathlib.Path(tmp) / "old.apk"
            new.write_bytes(b"fake")
            old.write_bytes(b"fake")
            with self._env(AUDIOMIXING_APK=str(new)):
                self.assertEqual(webui.apk_path(), new)
            with self._env(WAVELINK_APK=str(old)):
                self.assertEqual(webui.apk_path(), old, "旧名 WAVELINK_APK 仍要认")
            with self._env(AUDIOMIXING_APK=str(new), WAVELINK_APK=str(old)):
                self.assertEqual(webui.apk_path(), new, "两个都设时新名优先")

    def test_the_public_classes_carry_the_new_brand(self) -> None:
        import ast
        import inspect

        from audiomixing import engine, webui

        self.assertTrue(inspect.isclass(engine.AudioMixingNode))
        self.assertTrue(inspect.isclass(webui.AudioMixingApi))
        for mod in (engine, webui):
            defined = {n.name for n in ast.walk(ast.parse(inspect.getsource(mod)))
                       if isinstance(n, ast.ClassDef)}
            self.assertNotIn("WaveLinkNode", defined, "旧类名不该再定义")
            self.assertNotIn("WaveLinkApi", defined, "旧类名不该再定义")


class TestPanelLayoutAndSimpleMode(unittest.TestCase):
    """★ 用户那一轮的第 4/5/6 条（电脑面板的排版决定）。

    这三条都是"打开看一眼就知道对不对"的事，真机/浏览器上反而不好自动断言 ——
    所以这里钉住它们的**根据**：谁把规则删掉或改回去，这里立刻红。
    （浏览器里也实测过一遍，但那是手动的，不能防回归。）
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_the_status_card_is_the_hero_row(self) -> None:
        """第 6 条（2026-10-16 更版）：原来靠"主角位独占一整行"，现在「本机状态」是一个
        标签页（用户图2：「本机状态」就写在二级菜单那一行上）—— 所以这里改钉"它是一个页、
        大圆球在这一页里"。球本身还是"本机状态在中间"那个视觉中心。"""
        html = self._html()
        self.assertIn('data-card="status" data-pane="status"', html,
                      "本机状态现在是二级菜单里的一页")
        self.assertIn(".card[data-pane=\"status\"] .ring{", html,
                      "大圆球只在这一页里放大，不改手机端那套")
        # 查的是**属性写法**（data-hero="1"），不是那串字 —— 注释里说明"主角位那套删了"
        # 必然要提到它，提一次是为了让人看得懂为什么没有它。
        self.assertNotIn("data-hero=", html,
                         "主角位那套（含 data-hero 属性）随两列装箱一起删掉了")

    def test_the_route_status_block_is_gone(self) -> None:
        """第 4 条（2026-10-16 更版）：用户（图5）「这个路由不需要了，我们已经有卡片显示
        当前状态了」→「已经连上的」整段整体删掉，不再是"简洁档藏起来"。

        这条原来叫 test_simple_mode_hides_the_empty_route_block，钉的是
        `html:not(.fullui) #routesBlock.empty{display:none}`；现在那个块连 CSS 规则一起
        删了，所以这里改成反过来钉"它真的没了" —— 每条路由的状态由设备卡片承担。
        """
        html = self._html()
        for gone in ('id="routesBlock"', 'id="routesBox"', "function renderRoutes(",
                     "#routesBlock.empty"):
            self.assertNotIn(gone, html, "整段删除的东西不许半留着：%s" % gone)
        for keep in ("function deviceCard(", "function quickRoute("):
            self.assertIn(keep, html, "它的职责搬到设备卡片上了：%s" % keep)

    def test_the_multi_output_block_has_its_own_tab(self) -> None:
        """第 5 条（2026-10-16 更版）：以前它挤在「本机状态」那张卡里，现在按用户图2
        独立成二级菜单的一页（「多耳机」）。搬出来还有个好处：本机状态那张卡少了一整块，
        高度降下来了 —— v1.27 遗留的"这张卡下不去"就有解了。"""
        html = self._html()
        # 只数 id，不数那句标题文字 —— 注释里也会提到它。
        self.assertEqual(html.count('id="moList"'), 1, "整页只许有一份（id 唯一）")
        self.assertEqual(html.count('id="moToggle"'), 1)
        self.assertNotIn('id="multiOutCard"', html, "旧的那张独立卡片要真的没了")
        self.assertIn('data-card="multiout" data-pane="multi"', html, "多耳机要自己占一页")
        status = html.index('data-card="status" data-pane="status"')
        block = html.index('id="moList"')
        self.assertLess(status, block, "多耳块排在本机状态之后")
        self.assertLess(block, html.index("手机扫码", status), "多耳块要排在扫码之前")
        for keep in ('id="moToggle"', 'id="moBody"', 'id="moSummary"', 'id="moApply"'):
            self.assertIn(keep, html, "下面那段脚本按 id 取节点，一个都不许改：%s" % keep)

    def test_no_auto_listen_warning_is_shown_any_more(self) -> None:
        """★ 2026-10-18 用户第 6 条：「这种警告就不要出现了」。整块删掉，一条都不许留。

        （原来这里钉的是反过来的东西：被「不再自动收听」名单挡住的请求要写在设备卡片上、
        还要带一键恢复。现在自动打开不再是默认行为，也就没有"被拒"要通报了。）
        """
        html = self._html()
        for gone in ("deniedHere", "恢复自动收听", ".dvdenied{", "dvdenied",
                     "正在请求收听本机", "没有自动打开", "这次打开"):
            self.assertNotIn(gone, html, "第 6 条已经不要这类告警了：%s" % gone)


class TestControlChannelPicksTheRightRoad(unittest.TestCase):
    """★ 控制消息按对端能力分流（2026-10-15）：支持 ctl 的走发现层，老版本照旧走音频端口。

    这一条最重要的一点是<b>混跑不能互相打哑</b>：
      · 对端信标里有 ctl:1 → 发到它的发现端口（那一层一直活着，接收器停着也收得到）；
      · 老版本对端不认这层外衣 → 必须照旧发音频端口的二进制 T_STATS，一个字都不能少；
      · 从发现层收到的请求，和从音频端口收到的一样要能自动开门。
    """

    def setUp(self) -> None:
        self.node = AudioMixingNode(_config_stub(name="本机", node_id="node-self"),
                                 audio_port=free_udp_port(),
                                 discovery_port=free_udp_port(), simulate=True)
        self.node.devices = []
        self._socks: list = []

    def tearDown(self) -> None:
        for s in self._socks:
            try:
                s.close()
            except Exception:
                pass
        try:
            self.node.stop()
        except Exception:
            pass

    def _beacon(self) -> tuple:
        """给引擎挂一个"发现层 socket 是我们自己的"的 BeaconService，返回 (service, socket)。"""
        import socket

        service = BeaconService(self.node.registry, "node-self", "本机", 47831, 0,
                                lambda: [], lambda: [], port=free_udp_port())
        raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        raw.bind(("127.0.0.1", free_udp_port()))
        raw.settimeout(1.5)
        self._socks.append(raw)
        service._sock = raw
        self.node.beacon = service
        return service, raw

    def _session(self, port: int) -> None:
        class _FakeSession:
            host = "127.0.0.1"
            node_id = "peer-1"
            route_id = "r1"
            packets_sent = 42
            bytes_sent = 80640

        session = _FakeSession()
        session.port = port
        self.node.sender.sessions["r1|peer-1"] = session   # type: ignore[assignment]

    def _route(self) -> None:
        self.node.set_routes([{"id": "r1", "from": [self.node.config.node_id],
                               "to": ["peer-1"], "gain": 0.75}], persist=False)

    def test_a_control_peer_is_served_over_its_discovery_port(self) -> None:
        import json

        _service, raw = self._beacon()
        port = raw.getsockname()[1]        # 对端公布给我们的那个发现端口
        # 顺序要紧：先建路由（此刻对端还没登记，引擎不会真去起采集），再登记对端，
        # 最后塞一条"已经发过 42 个包"的假会话 —— 反过来会被 apply_routes 清理掉。
        self._route()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "ctl": 1, "disc_port": port},
                                  "127.0.0.1")
        self._session(0)                   # 音频端口故意给 0：走错了就什么也收不到
        # ★ 2026-10-19 用户口径：这条"请对端自动打开收听"的请求只在「智连」开着时才发。
        self.node.config.smart_link = ["peer-1"]
        self.node._send_peer_stats(force=True)

        data, _addr = raw.recvfrom(8192)
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(payload["wl"], 1)
        self.assertIsInstance(payload["ctl"], dict, "外衣：ctl 是对象 = 控制消息")
        body = payload["ctl"]
        self.assertTrue(body["want_listen"], "幂等：还在送 + 开着智连就一直 true")
        self.assertAlmostEqual(float(body["gain"]), 0.75, places=3)
        # 字段名和原来 T_STATS 一字不差；★ 但**没有那条路由的方向一个字段都不报**
        #   （2026-10-17：凭空报 gain_rx=1.0 会把对端的音量条刷回 1，见 _link_gains 的说明）。
        for key in ("gain", "gain_ts"):
            self.assertIn(key, body, "正文和原来 T_STATS 的字段名一字不差：%s" % key)
        self.assertNotIn("gain_rx", body, "反方向没有路由 → 不许报默认 1.0")

    def test_a_legacy_peer_still_gets_the_audio_port_packet(self) -> None:
        """老版本对端不认外衣 → 必须照旧收二进制 T_STATS（否则升级电脑就把老手机打哑）。"""
        import socket
        import json

        _service, raw_ctl = self._beacon()
        audio_port = free_udp_port()
        audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        audio_sock.bind(("127.0.0.1", audio_port))
        audio_sock.settimeout(1.5)
        self._socks.append(audio_sock)
        self.node.receiver._sock = audio_sock
        self._route()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "老版本手机",
                                   "audio_port": audio_port}, "127.0.0.1")   # ← 没有 ctl
        self._session(audio_port)
        # ★ 2026-10-19 用户口径："请对端自动打开收听"这条请求只在「智连」开着时才发。
        #    ★★ 特别针对这个用例：**旧版本对端**（没有智连这一层、全局自动接受默认开着）
        #    正是用户实测"智连没开却自动开了听"的来源 —— 所以这条更必须由本机把关。
        self.node.config.smart_link = ["peer-1"]
        self.node._send_peer_stats(force=True)

        data, _addr = audio_sock.recvfrom(4096)
        pkt = proto.decode(data)
        self.assertIsNotNone(pkt, "老对端收到的还是原来那个二进制包")
        self.assertEqual(pkt.type, proto.T_STATS)
        self.assertTrue(pkt.json()["want_listen"])
        with self.assertRaises(socket.timeout):
            raw_ctl.recvfrom(4096)     # 发现层这条路对老对端一个字都不该发

    def test_a_request_arriving_on_the_discovery_layer_still_opens_a_route(self) -> None:
        """⑤ 的正面：请求从发现层来（接收器可能根本没开）也必须自动开门。"""
        service, _raw = self._beacon()
        self.node.config.save()
        # ★ 2026-10-18 第 6 条：自动收听由每台设备的「智连」决定，默认关 —— 这条测的是
        #   "从发现层收到的请求也走同一条路"，所以先把这台开上智连。
        self.node.config.smart_link = ["peer-1"]
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831}, "198.51.100.10")
        service._queue_control({"t": "stats", "node": "peer-1", "name": "手机",
                                "want_listen": True},
                               ("198.51.100.10", 47830), time.monotonic())
        self.node._handle_peer_requests()

        auto = [r for r in self.node.routes if r.get("auto")]
        self.assertEqual(len(auto), 1, "从发现层收到的「请我听它」同样要自动建路由")
        self.assertEqual(auto[0]["from"], ["peer-1"])

    # ---- ★ 第 2 条：手机端「经常丢失电脑端」的根因 -------------------------
    def test_the_pc_beacon_unicasts_to_a_peer_it_already_knows(self) -> None:
        """电脑以前只发组播/广播 —— 而安卓省电时会成批丢这两种帧。

        真机现场（2026-10-15 用户：「手机端经常丢失电脑端」）：74 秒里电脑发了 ~37 轮信标，
        手机只收到 8 个；手机端 15 秒没信标就把设备摘掉。更不对称的是手机一直在**单播**给
        电脑，所以电脑始终看得见手机、手机却经常丢掉电脑 —— 一个纯看现象根本解释不了的
        怪状态。补上单播这一路，两边就对称了。
        """
        service, _raw = self._beacon()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "ctl": 1, "disc_port": 47833},
                                  "198.51.100.10")
        targets = service._unicast_targets()
        self.assertEqual(targets, [("198.51.100.10", 47833)],
                         "按对端自己公布的发现端口单播回去（端口被占时它可能不是 47830）")

        # 15 秒都没任何消息的对端不再单播：对端真走了就别一直往一个死地址发。
        peer = self.node.registry.get("peer-1")
        peer.last_seen = time.time() - 30.0
        self.assertEqual(service._unicast_targets(), [])

    def test_the_unicast_beacon_shows_up_in_the_stats(self) -> None:
        """「单播给了几台」必须能在诊断里看见 —— 不然用户没法确认这条路真的在走。"""
        service, _raw = self._beacon()
        stats = service.stats()
        self.assertIn("unicast_sent", stats)
        self.assertIn("unicast_targets", stats)
        self.assertEqual(stats["unicast_targets"], 0, "还没见过任何对端时是 0")

    def test_the_stats_report_what_this_machine_received_from_the_peer(self) -> None:
        """电脑端以前只报 sent、不报 recv —— 手机上那句"它收 0 包"是条假证据。

        2026-10-15 自查补的缺口：手机端设备卡片那行「它收 N 包 丢 M 缓冲 Xms」读的就是
        recv / lost / buf_ms / streams，而电脑端从来没发过这几个字段，
        于是手机上永远显示"它收 0 包"，明明正在收 —— 跨端排查时会被当成"包没到"。
        """
        import json

        _service, raw = self._beacon()
        port = raw.getsockname()[1]
        self._route()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "ctl": 1, "disc_port": port},
                                  "127.0.0.1")
        self._session(0)
        # 假装接收端有一条来自 peer-1 的流（一条路由一条流，多路由时会求和）
        self.node.receiver.stats = lambda: {"streams": [                       # type: ignore[method-assign]
            {"node_id": "peer-1", "packets": 120, "lost": 3, "buffered_ms": 45.0},
            {"node_id": "peer-1", "packets": 30, "lost": 1, "buffered_ms": 10.0},
            {"node_id": "peer-9", "packets": 999, "lost": 0, "buffered_ms": 0.0},
        ]}
        self.node._send_peer_stats(force=True)
        body = json.loads(raw.recvfrom(8192)[0].decode("utf-8"))["ctl"]
        self.assertEqual(body["recv"], 150, "同一台的多条流要加起来（120+30）")
        self.assertEqual(body["lost"], 4)
        self.assertEqual(body["buf_ms"], 45, "取最厚的那个缓冲")
        self.assertEqual(body["streams"], 2)
        self.assertEqual(body["sent"], 42, "送出去的数照旧")

    # ---- ★ 第 3 条：电脑停了「送声音」，手机还在收 -------------------------
    def test_a_stopped_sender_keeps_saying_it_stopped(self) -> None:
        """第 3 条：我一停，会话立刻消失 —— 但"我停了"这句话必须送出去。

        对端是**没有状态**的接收方：我不说，它就一直保持"我还在送"那个状态，
        自动打开的那条收听永远关不掉。所以停掉之后还要再报 30 秒 want_listen=false。
        （手机端早就为同一个原因加了 30 秒宽限，电脑端一直漏着。）
        """
        import json

        _service, raw = self._beacon()
        port = raw.getsockname()[1]
        self._route()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "ctl": 1, "disc_port": port},
                                  "127.0.0.1")
        self._session(0)
        # ★ 2026-10-19 用户口径：「智连」没开就不许自动开关对端的「听」——
        #   于是这条"请对端自动打开收听"的请求只在智连开着时才发。这里开着，验的是邀请本身。
        self.node.config.smart_link = ["peer-1"]

        self.node._send_peer_stats(force=True)
        first = json.loads(raw.recvfrom(8192)[0].decode("utf-8"))
        self.assertTrue(first["ctl"]["want_listen"], "还在送 + 开着智连 → 请对端自动打开收听")

        # 用户把「送声音」关掉：会话立刻没了（这正是老代码什么都发不出去的那一刻）
        self.node.sender.sessions.clear()
        self.node._send_peer_stats(force=True)
        second = json.loads(raw.recvfrom(8192)[0].decode("utf-8"))
        self.assertFalse(second["ctl"]["want_listen"],
                         "停了也要说一声，否则对端会一直收下去")
        self.assertFalse(second["ctl"]["sending"])
        self.assertEqual(second["ctl"]["sent"], 0)

    def test_discovery_layer_traffic_counts_as_the_peer_still_being_alive(self) -> None:
        """发现层收到的统计包也是"它还活着"的证据。

        漏掉这一条，聊天改走发现层之后对端就会被判成"没了"而停发 ——
        表现成"明明在传，过一会儿自己停了"。
        """
        service, _raw = self._beacon()
        self._route()
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831}, "198.51.100.10")
        peer = self.node.registry.get("peer-1")
        peer.last_seen = time.time() - 30.0     # 信标早就过期了
        self.assertEqual(self.node.desired_senders(), [], "先确认它现在是被判死的")

        service._queue_control({"t": "stats", "node": "peer-1", "name": "手机"},
                               ("198.51.100.10", 47830), time.monotonic())
        self.node._track_peer_liveness()
        peer = self.node.registry.get("peer-1")
        peer.last_seen = time.time() - 30.0     # 信标这一路仍然过期
        self.assertEqual(len(self.node.desired_senders()), 1,
                         "发现层刚收到的统计包就够证明它还活着")


class TestStopsSendingWithoutThePanel(unittest.TestCase):
    """④ 的清理必须在【后台线程】里跑，不能挂在界面上。

    手机端（task-17）的真根因就是这个：清理只写在界面 tick 里，一锁屏就停，
    所以"电脑关了还在发"能一直持续。电脑端的清理在引擎自己的循环里（tick → apply_routes），
    这条测试不打开任何页面，只让引擎自己跑，证明发送端真的会停。
    """

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.codec = "pcm_s16"
        cfg.routes = []
        self.node = AudioMixingNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
                                 simulate=True)

    def tearDown(self) -> None:
        try:
            self.node.stop()
        except Exception:
            pass

    def test_the_engine_stops_by_itself_when_the_peer_vanishes(self) -> None:
        # 用本机地址 + 关闭的端口：这样引擎的 HTTP/发现探测会立刻失败而不是挂在局域网上
        # （挂在网上的探测会让 tick 变慢，测试就会时灵时不灵）。
        self.node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": free_udp_port(), "web_port": free_udp_port()},
                                  "127.0.0.1")
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["peer-1"], "gain": 1.0}],
                             persist=True)
        self.node.start(announce=False)          # 只启动引擎自己的线程，不开任何页面
        deadline = time.time() + 10.0
        while time.time() < deadline and not self.node.sender.sessions:
            time.sleep(0.1)
        self.assertTrue(self.node.sender.sessions,
                        "先要有一路在发（说明对端被判成活着）；引擎日志：%s"
                        % " | ".join(self.node.log[-4:]))
        peer = self.node.registry.get("peer-1")
        peer.last_seen = time.time() - 60.0     # 对端消失：信标停 60 秒，也没有统计/音频
        self.node._peer_seen.clear()
        deadline = time.time() + 12.0
        while time.time() < deadline and self.node.sender.sessions:
            time.sleep(0.2)
        self.assertEqual(self.node.sender.sessions, {},
                         "对端没了，后台循环必须自己把发送端停掉（不依赖面板）")
        self.assertTrue(self.node.status()["senders_blocked"], "而且要留下原因给面板显示")


class TestTask16PanelTexts(unittest.TestCase):
    """②③ 面板行为 + ①④ 的文案（这些是用户直接看到的那一半）。"""

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_detail_view_keeps_offline_devices_for_a_minute(self) -> None:
        import re  # noqa: F401  (本方法以外的用例也可能用到)
        html = self._html()
        self.assertIn("const OFFLINE_KEEP_S = 60;", html)
        self.assertIn("已离线 ", html)
        # 只作用于详细档的那张表（简洁档的设备是 ② 里的卡片，不在这张表里）
        self.assertIn("n.local || n.online || (n.age_s || 0) <= OFFLINE_KEEP_S", html)
        self.assertIn("tr class=\"${n.local || n.online ? '' : 'offline'}\"", html)

    def test_the_device_list_appears_only_once(self) -> None:
        """用户原话「局域网里的设备和路由有相当一部分重叠」（2026-10-15）。

        同一批设备以前出现两次：① 卡里一行一台（.brief），② 卡里又是一张张卡片。
        简洁档只留 ② 的卡片；① 只保留"详细档那张表"（它能采到什么/能当谁的音响）。
        """
        html = self._html()
        self.assertNotIn('class="brief"', html, "重复的那一行设备列表要删掉")
        self.assertNotIn(".peerline", html)
        # ① 的表仍在，且仍归详细档
        self.assertIn('<div class="full"><table>', html)
        # ① 卡的标题不能再叫"局域网里的设备"（那是设备与路由的活）
        self.assertIn('>找设备', html)
        # 2026-10-16 用户：「连编号也去掉」—— 标题里不许再有编号徽章
        self.assertNotIn('<h2><span class="n">', html, "编号徽章要全部去掉")

    def test_explanations_are_hidden_in_the_simple_view(self) -> None:
        """③ 简洁档要真的简洁：解释性文字（扫码怎么用/为什么）收进详细档。

        判据用"这句话必须落在会隐藏的容器里"——只写文字不写容器，简洁档就会漏出来。
        """
        import re

        html = self._html()
        self.assertIn('class="mini" style="font-size:12px;opacity:.72', html,
                      "扫码那四行解释必须是 .mini（简洁档隐藏）")
        hidden = re.search(r"html:not\(\.fullui\) \.mini", html)
        self.assertTrue(hidden, "简洁档必须有一条隐藏 .mini 的规则")

    def test_the_bluetooth_devices_are_square_blocks(self) -> None:
        """蓝牙设备要和局域网设备一样，是"小方块"（用户 2026-10-16 原话：
        「把蓝牙的设备也做成局域网内的设备一样的小方块」）。

        以前蓝牙 / 网络音源是"一行一台、按列对齐"的细行；现在两个列表都是和
        #deviceCards 同一套方块栅格 —— 所以这里钉的是**方块栅格**，不再是行栅格。
        """
        import re

        html = self._html()
        # ★ 2026-10-16（图4）：「Wi-Fi 送声音的手机」那一段整块删掉了（手机 WiFi 已并进路由），
        #   所以这里只剩蓝牙那一个列表要钉方块栅格。
        m = re.search(re.escape("#deviceCards, #btList{") + r"[^}]*\}", html, re.S)
        self.assertTrue(m, "蓝牙列表要落在那条共用的方块栅格上")
        rule = m.group(0)
        self.assertIn("display:grid", rule)
        self.assertRegex(rule, r"grid-template-columns:\s*repeat\(auto-fill,minmax\(240px,1fr\)\)",
                         "方块栅格：两个列表共用一条，且必须 auto-fill（见图1 那条满行的卡）")
        # 方块里的名字列必须能"要多宽有多宽"：2026-10-15 用户截图里稍长的手机名
        # 被截成半截 —— 就是因为有一版把这一列封了顶。
        self.assertNotIn("minmax(72px,150px)", html, "封顶的名字列会把长设备名截断")
        self.assertIn('<span class="dvname" title="${esc(d.name)}"', html, "长名字要有 title 兜底")
        # 蓝牙那块真的要落到方块上（尺寸/按钮/状态说明都在块里，不再是细行）。
        self.assertNotIn("点「连接」让手机把它当成蓝牙音箱", html,
                         "方块里那行灰色说明要去掉（用户：「图3…为什么不删？」）")
        self.assertIn('class="dvacts"', html, "方块里的按钮行")
        # ★ ③ 里"每条进来的声音"那几行仍然用 .btrow（那是行、不是设备方块），
        #   所以这里只钉设备列表，不能一刀切地把 .btrow 判死。
        self.assertIn('<div id="btList"', html)
        # ★ 2026-10-16（图4）：原来这里还断言过网络音源那一列方块（`${esc(x.name)}`）——
        #   那一段已随图4 整块删掉（手机 WiFi 并进路由），所以这里只钉蓝牙方块那一份。
        self.assertNotIn("netAudioList", html, "网络音源那一段不许半留着")

    def test_blocked_senders_are_explained_on_the_panel(self) -> None:
        html = self._html()
        self.assertIn("senders_blocked", html)
        self.assertIn("已停发", html)
        self.assertIn("badrow", html)
        self.assertIn("blockedHere", html, "设备卡片上也要写清它为什么停发")

    def test_the_card_slider_says_which_layer_it_controls(self) -> None:
        """①-3 的修复：滑块要知道对象是哪一层（路由 / 手机采集增益 / 频道音量）。"""
        html = self._html()
        self.assertIn("function gainTargetFor(", html)
        self.assertIn("kind:'route'", html)
        self.assertIn("kind:'mic'", html)
        self.assertIn("kind:'channel'", html)
        self.assertIn("/api/mic/gain", html, "手机送进来的声音要能调（走这台手机的采集增益）")
        self.assertIn("channel_gain", html, "频道/直通那一路要能调")
        self.assertIn("dvtag2", html, "滑块旁边要标出调的是哪一层")

    def test_the_smart_link_switch_lives_on_every_device_card(self) -> None:
        """★ 2026-10-18 用户第 6 条：「直接在如图3的每个对应设备的小卡片加入第4按键「智连」。
        默认关闭，开启后，自动接受其他设备的听和送」。

        一并钉住**被删掉的那一整套**：全局总开关、「对方请求收听、已自动打开」的提示条、
        一键撤销/全部收回、「正在请求收听但没自动打开」的告警、以及那个
        "关过一次就永不再自动"的黑名单接口（用户第 1 条那个 bug 的根因）。
        """
        html = self._html()
        # 局域网设备卡片：第 4 颗按钮，默认是「智连」（不是 ✓ 智连）
        self.assertIn("toggleSmart(", html)
        self.assertIn("smartOnFor(", html)
        # ★ 2026-11-04（A 硬规则）：广播开着时按钮要置灰并写"广播中不可用"，
        #   所以文案再也不是固定两个词。这里断言"智连按钮存在 + 有禁用分支"。
        self.assertIn("onclick=\"toggleSmart(", html)
        self.assertIn("广播中不可用", html)
        self.assertIn("smartAvail", html)
        self.assertIn("kind:'route'", html)  # 别把卡片上的既有按钮删了
        # 蓝牙那张卡片的「自启」并进同一个词（用户选了"局域网+蓝牙都加"）
        self.assertIn("btAuto(", html)
        self.assertIn(">${btAutoList.includes(d.name) ? '✓ 智连' : '智连'}</button>", html)
        # 旧的那一套必须真的没了
        for gone in ('id="autoListenBox"', 'id="autoAcceptListen"', 'id="autoListenState"',
                     "function revokeAutoListen(", "function allowAutoListen(",
                     "function revokeAllAutoListen(", "saveAutoAcceptListen",
                     "al.denied", "deniedHere", ".dvdenied{", "/api/listen/optout",
                     "auto_accept_listen"):
            self.assertNotIn(gone, html, "第 6 条已经换掉了这一套，不许半留着：%s" % gone)
        self.assertIn("smart_link", html, "名单要真的 POST 回服务端")
        # ★ 2026-10-18 用户（图2）：「「正在发送的手机自动播放」就是蓝牙的「智连」」——
        #   那个全局勾（前端 id/函数 + 服务端 auto_play）整块删掉，行为改由「智连」逐台决定。
        for gone in ('id="autoPlay"', "saveAutoPlay", "auto_play"):
            self.assertNotIn(gone, html, "那个全局勾已经并进「智连」了，不许半留着：%s" % gone)

    def test_only_the_pc_side_of_the_graph_can_start_a_link(self) -> None:
        """★ 2026-10-18 用户第 2 条：「comfyui中应当只能控制电脑的主动发送声音。

        我刚刚连线发现，手机设备的发送线也能拖拽。」——只有**本机**那两个圆点能当拖拽起点：
        右侧「送」拖到某台设备 = 本机送给它；左侧「收」拖到某台设备 = 本机收听它。
        设备那侧只有一个落点圆点（而且整张卡片都是落点，不用精确命中那个小圆点）。
        """
        html = self._html()
        self.assertIn('class="rk send"', html)
        self.assertIn('class="rk recv"', html)
        self.assertIn('class="rk drop"', html)
        # 起点判定必须**同时**要求是本机那一个节点 —— 少一个条件就等于"设备也能拖"（用户报的 bug）
        i = html.index("box.addEventListener('mousedown'")
        body = html[i:html.index("\n  });", i)]
        self.assertIn("key === 'self'", body, "只有本机的圆点能当起点")
        self.assertIn("relDragLink = {from: key", body)
        # 松手落点：**按几何位置**算（relHitNode）。
        # ★ 2026-10-18 实测：原来用 elementFromPoint，SVG 里那条线（.ln/.lnhot 是
        #   pointer-events:stroke）会把命中抢走，指针明明在卡片上却摸到一条线 →
        #   拖过去"什么也没发生"，探针里还出现"落点被解析成蓝牙卡"的怪结果。
        self.assertIn("function relHitNode(", html)
        j = html.index("document.addEventListener('mouseup'")
        body2 = html[j:html.index("\n  });", j)]
        self.assertIn("relHitNode(ev.clientX, ev.clientY)", body2, "整张卡片都是落点")
        self.assertNotIn("elementFromPoint", body2, "落点不许再用 elementFromPoint")
        self.assertNotIn("closest('.rk.in')", body2, "不许再要求精确命中那个小圆点")
        # 同一条线拖两次不许堆出两条完全重叠的路由（那条线点了只会删掉其中一条 = "点了没反应"）
        self.assertIn("这条线已经有了", html)

    def test_the_monitor_window_can_show_only_this_machine(self) -> None:
        """★ 2026-10-20 用户：「监控模式右上角『放行防火墙』的左侧添一个『本机』激活按钮，
        就仅监控与本机有关的设备……同时在激活情况下隐藏时钟通道。不然看起来太乱了」。

        这颗按钮必须真的按"本机收发的那几路"过滤，而且要把时钟通道藏掉；状态要记住
        （存在浏览器里，下次打开还是上次选的那档）。
        """
        from audiomixing import webui as webui_mod

        html = webui_mod.monitor_html()
        self.assertIn('id="localOnly"', html, "监控页右上角要有这颗「本机」按钮")
        # 位置：必须排在「放行防火墙」**左边**
        self.assertLess(html.index('id="localOnly"'), html.index('id="fw"'),
                        "『本机』要在『放行防火墙』的左侧")
        self.assertIn("am.monitor.localOnly", html, "开关状态要记住")
        self.assertIn("=== '时钟'", html, "打开后要隐藏时钟通道")
        # ★ 2026-10-21 用户现场：打开「本机」后「瘾控 → 本机（蓝牙）」整行消失。
        #   /api/monitor 的本地端是字面量「本机」/「本机声音」，不是节点名（2024TSJ）；
        #   过滤必须把这几种写法都认成本机，否则所有入向流都会被误判成"和本机无关"。
        self.assertIn("s === me || s === '本机' || s === '本机声音' || s === 'self'", html,
                      "本地端的几种写法都要认成本机")
        self.assertIn("isLocalEnd(f.from) || isLocalEnd(f.to)", html,
                      "只留本机收发的那几路")
        self.assertIn("paintLocalOnly();", html, "打开页面就按上次选的那档画一次")

    def test_a_solid_line_means_audio_is_really_flowing(self) -> None:
        """★ 2026-10-19 用户：「线的形式和实际情况不匹配」。

        他截了两张图：关系图上「本机 → 2025BJB」是一条**实线**，而设备卡片上同时写着
        「离线」「已停止向它发送：36 秒没有任何消息」「送：没有在送」。实线在图例里的意思
        是"现在真的有连接"，跟实际对不上。

        根因：监控页里「已配置 · 当前没有数据」那几行**也带 route_id**（断开按钮要用它），
        而关系图原来只按"这个 id 在不在监控里"判实线 —— 于是"配了路由但一个字节没流过"
        也被画成实线。现在按"这条流里真的有数据"判。
        """
        html = self._html()
        i = html.index("(relData.flows || []).forEach")
        body = html[i:html.index("const lanOnline", i)]
        self.assertIn("Number(f.bytes", body, '要按「这条流真的有数据」判，不是「有这个 id」')
        self.assertIn("Number(f.rate_kbps", body)
        self.assertNotIn("if(rid){", body, "光看 route_id 就算在流 → 离线设备会被画成实线")
        self.assertIn("现在真的有声音在流", html, "图例口径要跟判定一致")
        self.assertIn("虚线 = 现在没有", html)

    def test_the_graph_zooms_with_ctrl_wheel_without_zooming_the_page(self) -> None:
        """★ 第 3 条：「comfyui不要有滚动条，可以用按住CTRL+滚轮放大缩小。像网页一样。

        但是不能连着外面主页面一起变（现在的主页面也可以滚轮放大缩小，别删。就是分开放大缩小）」
        """
        html = self._html()
        self.assertIn("#relWrap{position:relative;height:400px;overflow:hidden", html,
                      "这一块不许出现滚动条")
        self.assertIn("transform-origin:0 0", html, "缩放要以左上角为基准，平移才好算")
        self.assertIn("ev.ctrlKey", html)
        self.assertIn("ev.preventDefault()", html, "不挡的话浏览器会把整页一起放大")
        self.assertIn("{passive: false}", html, "要能 preventDefault 就必须非 passive")
        self.assertIn("function relApplyView(", html)
        self.assertIn("relPan", html, "空白处按住拖 = 平移（没有滚动条时的逛法）")
        # 不按 Ctrl 时滚轮必须原样交给页面（用户明确说主页面那套别删）
        i = html.index("wrap.addEventListener('wheel'")
        body = html[i:html.index("\n    }, {passive: false});", i)]
        self.assertIn("if(!ev.ctrlKey) return;", body)

    def test_a_loop_is_warned_at_the_top_and_drawn_in_red(self) -> None:
        """★ 第 4 条：「如果comfyui的线图出现了回环，即声音回环。要在ui最上方表明警告。

        并将回环线路标红，但是正常运行，一切后果让使用者自己承担。」——只警告、不拦。
        """
        html = self._html()
        self.assertIn('id="loopWarn"', html)
        self.assertIn("function routeCycleInfo(", html)
        self.assertIn("function renderLoopWarn(", html)
        self.assertIn("#relSvg .ln.cycle{stroke:#f87171", html, "回环的线要单独一个红色类")
        self.assertIn("检测到声音回环", html)
        self.assertIn("程序照常运行，后果自负", html, "只警告，不拦着跑")
        # 警告必须**每一页**都刷（回环是整套路由的性质，不该非得点进「版本号」才知道）
        i = html.index("async function refreshAll(")
        self.assertIn("renderLoopWarn();", html[i:html.index("setInterval(refreshAll", i)])
        # 回环**只算点对点**：广播 + 只听同时开着是用户要的 FM 电台用法，不能天天误报
        j = html.index("function routeCycleInfo(")
        body = html[j:html.index("\n}", j)]
        self.assertIn("state.routes", body)
        self.assertNotIn("kind === 'bc'", body)

    def test_the_auto_listen_switch_says_what_it_is_doing(self) -> None:
        """★ 2026-10-17 那一版的"全局总开关 + 常驻状态行"在 2026-10-18 被用户否掉了。

        用户第 6 条改成「每台设备一个「智连」」之后，那一整套（复选框、状态行、提示条）
        整块删掉 —— 这条留着只为钉一件事：**它真的删干净了**，别哪天又从注释里长回来。
        新行为由 `test_the_smart_link_switch_lives_on_every_device_card` 与
        `test_a_device_without_smart_link_is_never_auto_added` 负责。
        """
        html = self._html()
        self.assertNotIn("自动收听路由声音", html, "全局总开关已经换成每台设备的「智连」了")
        self.assertNotIn("autoListenState", html)


    def test_the_secondary_menu_is_hidden_on_the_qr_and_version_pages(self) -> None:
        """★ 2026-10-18 用户：「二维码和版本号的二级菜单不要显示。只仅在详细和简洁有二级菜单」。"""
        html = self._html()
        self.assertIn('body[data-pane="qr"] .tabs .tab', html)
        self.assertIn('body[data-pane="ver"] .tabs .tab', html)

    def test_the_version_page_is_not_blank_in_the_simple_density(self) -> None:
        """★ 连带修掉的缺陷：**简洁档点「版本号」原来是一整页空白**。

        这两页本来就没有"密度"这一维（setUiMode 里那句注释就是这么写的），可「版本与运行」
        「设备关系图」这两张卡片带着 `full` 类，会被 `html:not(.fullui) .full{display:none!important}`
        一起藏掉。修法是把这两张的 `full` 去掉（**不是**用 !important 盖住整页 —— 那会把「日志」
        也一起放出来，简洁档不该露日志）。
        """
        html = self._html()
        self.assertIn('<section class="card" data-card="ver" data-pane="ver">', html)
        self.assertIn('<section class="card" data-card="rel" data-pane="ver">', html)
        # 日志仍然只在详细档露
        self.assertIn('<section class="card full" data-card="logs" data-pane="ver">', html)

    def test_a_bluetooth_card_is_rebuilt_alone(self) -> None:
        """★ 2026-10-18 用户：「电脑端蓝牙每次操作都会让整个模块重新刷新，我要求仅被操作卡片刷新」。

        可静态验证的那一半（另外一半在浏览器里实测过）：
          · 每张卡片有自己的 DOM 节点 id，操作走"只换那一张"；
          · btToggle 里**不再**有任何写进整个列表容器的语句 —— 那正是"整块刷新"的根因。
        """
        html = self._html()
        self.assertIn('id="btcard-${i}"', html, "每张蓝牙卡片要有自己的 DOM id")
        for fn in ("function btCardHtml(d, i){", "function btBusy(", "function btRefreshOne("):
            self.assertIn(fn, html)
        i = html.index("async function btToggle(")
        body = html[i:html.index("\n}", i)]
        self.assertNotIn("$('btList')", body, "不许再把整个蓝牙列表的 innerHTML 换掉")
        self.assertIn("btBusy(index,", body, "忙态只能落在被点的那一张卡片里")
        j = html.index("async function btAuto(")
        body2 = html[j:html.index("\n}", j)]
        self.assertNotIn("$('btList')", body2, "自启也只重画那一张")
        self.assertIn("btCardHtml(d, index)", body2)

    def test_the_relation_graph_can_really_build_a_route(self) -> None:
        """★ 2026-10-18 用户：「在版本号那边加入comefyui…支持拖拽，可以连线最好」。

        用户当场选的是「**拖线 = 真建路由**」和「连设备之间的路由也画」，所以这一条要钉住：
          · 图里的容器与两层（SVG 画线 / div 画节点）都在；
          · 三种连接方式各有一个颜色类（WiFi / 蓝牙 / 广播）；
          · 拖拽是三件套（mousedown / mousemove / mouseup），松手落到接收圆点上就真去建路由；
          · 线是曲线（贝塞尔 C 命令），不是直线。
        """
        html = self._html()
        for anchor in ('id="relWrap"', 'id="relSvg"', 'id="relNodes"', 'id="relCanvas"'):
            self.assertIn(anchor, html, "关系图的 %s 缺了" % anchor)
        for cls in (".ln.wifi", ".ln.bt", ".ln.bc"):
            self.assertIn(cls, html, "线的颜色类 %s 缺了" % cls)
        self.assertIn("function relCreate(", html)
        self.assertIn("'/api/routes/add'", html)
        self.assertIn("relDragLink", html)
        self.assertIn("function relPath(", html)
        self.assertIn(" C ", html, "线必须是柔和的曲线（三次贝塞尔）")
        # 局域网隔离：不许引任何外部资源（自检脚本也会判，但这里把"关系图"这一块单独钉住）
        self.assertNotIn("cdn.", html)
        self.assertNotIn("unpkg", html)
        # 蓝牙不是路由端点：拖它建不出路由，必须给一句人话，不能静默什么都不做
        self.assertIn("蓝牙是点对点的物理链路，不能当路由端点", html)

    def test_the_relation_graph_never_enumerates_bluetooth_itself(self) -> None:
        """★ 2026-10-18 自查抓到的一条硬规则：关系图**不许**自己去问蓝牙。

        `bluetooth.list_devices_cached()` 冷缓存时会真的起一个约 3 秒的 PowerShell 枚举，
        而且枚举本身会**扰动正在保持的蓝牙连接** —— `bluetooth.py` 里那条"绝不能在面板轮询的
        请求上跑"的禁令就是为它写的。关系图原来是每 2.5 秒问一次 `/api/bluetooth`，
        那样每 10 秒（缓存的 max_age）就会真枚举一次，正好踩在这条禁令上。
        现在它只复用蓝牙卡片读回来的 `btSnapshot`，自己一次都不发起。
        """
        html = self._html()
        i = html.index("async function relRefresh(")
        body = html[i:html.index("\n}", i)]
        self.assertNotIn("/api/bluetooth", body, "关系图不许自己去问蓝牙（那会触发一次真枚举）")
        self.assertIn("/api/monitor", body, "便宜的那一条照旧问（监控页自己每秒都在问）")
        # 蓝牙那一份只能来自 btSnapshot，而且必须由蓝牙卡片写进去。
        j = html.index("function relBuild(")
        build = html[j:html.index("\n}", j)]
        self.assertIn("btSnapshot", build, "关系图的蓝牙节点要取自 btSnapshot")
        self.assertNotIn("relData.bt", build, "关系图不许再自己存一份蓝牙名单")
        self.assertIn("btSnapshot = all;", html, "蓝牙卡片要把自己读回来那份留给关系图")


class TestPhoneLogoIsTheSameSquareEverywhere(unittest.TestCase):
    """★ 2026-10-18 用户：「现在这个版本的手机版，左上角角标还没有更换」。

    上一轮只换了**桌面启动图标**（mipmap 里的 ic_launcher）和电脑端/网页上的方块，
    手机 App 界面里左上角那枚角标画的还是旧标识的四条音频柱 —— 里外又对不上，正是用户
    图3 说的那件事。手机 UI 在这台机器上跑不起来，所以这一条只能静态钉住那几行关键代码。
    """

    @staticmethod
    def _logo() -> str:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "MainActivity.java").read_text(encoding="utf-8")
        i = java.index("private static final class LogoView")
        return java[i:java.index("\n    }\n", i)]

    def test_the_header_badge_is_the_blue_square_with_a_white_a(self) -> None:
        body = self._logo()
        self.assertIn('cv.drawText("A"', body, "角标里画的必须是字母 A")
        self.assertIn("drawRoundRect", body, "还是那个圆角方块")
        self.assertIn("LinearGradient", body, "还是蓝色渐变")
        self.assertNotIn("HEIGHTS", body, "旧的四条音频柱不许再回来")

    def test_the_a_is_optically_centred_not_baseline_guessed(self) -> None:
        """基线必须用 FontMetrics 算出来 —— 直接把基线放 h/2 会明显偏上。"""
        self.assertIn("getFontMetrics()", self._logo())


class TestBackendAutoload(unittest.TestCase):
    """内置后端必须"谁先被 import 都不影响其它后端能加载"。

    现场（测试里稳定复现）：先跑过用 netmic 的用例之后，`_BACKENDS` 里只剩 netmic，
    老的 `_autoload()` 看到非空就直接返回 —— 于是 simulate 节点开 "null" 音源时报
    `unknown audio backend 'null' (have: netmic)`，表现是"什么都打不开、一点声音都没有"。
    """

    def test_a_lost_registration_is_repaired(self) -> None:
        """注册表被清空（模块早就 import 过、登记却丢了）时，下一次 backends() 必须补回来。

        真实现场：老代码用 "注册表非空 = 已经加载过" 当判据，于是一处先 import 了 netmic，
        其它后端就永远不加载 —— 开音源时报 unknown audio backend，一点声音都没有。
        """
        import ast
        import inspect

        from audiomixing.audio import base as audio_base

        # AST 判据（文档串里引用旧写法不算）：函数里不许出现 "if _BACKENDS: return"
        tree = ast.parse(inspect.getsource(audio_base._autoload))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            bare_return = (len(node.body) == 1 and isinstance(node.body[0], ast.Return)
                           and node.body[0].value is None)
            if bare_return and isinstance(test, ast.Name) and test.id == "_BACKENDS":
                self.fail("不能拿「注册表非空」当「已经加载过」的判据（这就是那个 bug）")
        saved = dict(audio_base._BACKENDS)
        saved_flag = audio_base._AUTOLOADED
        try:
            audio_base._BACKENDS.clear()
            audio_base._AUTOLOADED = False
            names = set(audio_base.backends().keys())
        finally:
            # 原样恢复全局状态：这些后端类是全进程共用的，别给后面的用例留坑
            audio_base._BACKENDS.clear()
            audio_base._BACKENDS.update(saved)
            audio_base._AUTOLOADED = saved_flag
        for name in ("file", "netmic", "null", "wasapi"):
            self.assertIn(name, names, "%s 后端必须能自己补回来" % name)


class TestMonitorWindowReuse(unittest.TestCase):
    """★ 2026-10-26 用户现场「窗口无限开」：任务栏叠了十几个「Audio Mixing 监控」。

    根因是服务已就绪、但监控窗还没出现的那几秒里，多个双击实例都走到
    “没有窗口”这一步，各自执行 Edge --app。修法有两层：
      · 窗口识别改成按前缀找任意页面（监控/控制台/诊断），不再精确匹配标题；
      · 真正去开窗之前抢一把跨进程锁，第一个开，后面的只提窗、不再开。
    """

    def test_window_title_accepts_any_panel_page_but_not_browser_tabs(self) -> None:
        import audiomixing_launcher as launcher

        for title in ("Audio Mixing 监控", "Audio Mixing 控制台", "Audio Mixing 诊断",
                      "Audio Mixing 简洁", "Audio Mixing", "Audio Mixing 监控 - 个人"):
            self.assertTrue(launcher._monitor_window_title(title), title)
        for title in ("Audio Mixing 监控 - Google Chrome",
                      "Audio Mixing 监控 - Microsoft Edge",
                      "Audio Mixing - Mozilla Firefox",
                      "别的程序 Audio Mixing 监控", ""):
            self.assertFalse(launcher._monitor_window_title(title), title)

    def test_an_existing_window_is_raised_instead_of_opening_another(self) -> None:
        from unittest import mock

        import audiomixing_launcher as launcher

        with mock.patch.object(launcher, "_acquire_monitor_open_lock", return_value=1), \
                mock.patch.object(launcher, "_release_monitor_open_lock"), \
                mock.patch.object(launcher, "_window_handle", return_value=4242), \
                mock.patch.object(launcher, "_raise_window") as raise_win, \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.open_monitor("http://127.0.0.1:17800")
        raise_win.assert_called_once_with(4242)
        popen.assert_not_called()

    def test_only_the_first_opener_launches_edge_and_later_ones_raise(self) -> None:
        from unittest import mock

        import audiomixing_launcher as launcher

        # 第一次检查：没有窗口 → 开；第二次检查：窗口已经在了 → 只提窗。
        with mock.patch.object(launcher, "_acquire_monitor_open_lock", return_value=1), \
                mock.patch.object(launcher, "_release_monitor_open_lock"), \
                mock.patch.object(launcher, "_window_handle", side_effect=[0, 4242]), \
                mock.patch.object(launcher, "_monitor_process_exists", return_value=False), \
                mock.patch.object(launcher, "_raise_window") as raise_win, \
                mock.patch.object(launcher, "_find_edge", return_value="msedge.exe"), \
                mock.patch.object(launcher, "_wait_for_monitor_window", return_value=True), \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.open_monitor("http://127.0.0.1:17800")
            launcher.open_monitor("http://127.0.0.1:17800")
        self.assertEqual(popen.call_count, 1, "同一个监控页只能拉起一次 Edge")
        raise_win.assert_called_once_with(4242)

    def test_a_loser_that_cannot_get_the_open_lock_never_opens_another_window(self) -> None:
        from unittest import mock

        import audiomixing_launcher as launcher

        with mock.patch.object(launcher, "_acquire_monitor_open_lock", return_value=0), \
                mock.patch.object(launcher, "_window_handle", return_value=0), \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.open_monitor("http://127.0.0.1:17800")
        popen.assert_not_called()

    def test_an_existing_monitor_process_blocks_a_second_window_even_if_title_is_unreadable(self) -> None:
        """★ 2026-10-26 真机兜底：Edge 版本可能把标题改成认不出的样子。

        只要已经有一个带 `panel-profile` 的 Edge 应用进程，就不许再开新窗。
        """
        from unittest import mock

        import audiomixing_launcher as launcher

        with mock.patch.object(launcher, "_acquire_monitor_open_lock", return_value=1), \
                mock.patch.object(launcher, "_release_monitor_open_lock"), \
                mock.patch.object(launcher, "_window_handle", return_value=0), \
                mock.patch.object(launcher, "_monitor_process_exists", return_value=True), \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.open_monitor("http://127.0.0.1:17800")
        popen.assert_not_called()

    def test_edge_launch_without_detectable_window_does_not_open_a_browser_too(self) -> None:
        """Edge 已经拉起来了但窗口没认出来时，不许再 webbrowser.open 补一个。

        那会开出一个标题不可识别的普通浏览器页，下一次双击又再开一个 —— 正是
        “窗口无限开”的放大器。
        """
        from unittest import mock

        import audiomixing_launcher as launcher

        with mock.patch.object(launcher, "_acquire_monitor_open_lock", return_value=1), \
                mock.patch.object(launcher, "_release_monitor_open_lock"), \
                mock.patch.object(launcher, "_window_handle", return_value=0), \
                mock.patch.object(launcher, "_monitor_process_exists", return_value=False), \
                mock.patch.object(launcher, "_find_edge", return_value="msedge.exe"), \
                mock.patch.object(launcher, "_wait_for_monitor_window", return_value=False), \
                mock.patch.object(launcher, "_message_box") as box, \
                mock.patch.object(launcher.subprocess, "Popen"), \
                mock.patch("webbrowser.open") as browser:
            launcher.open_monitor("http://127.0.0.1:17800")
        browser.assert_not_called()
        self.assertTrue(box.called, "窗口没出现时要给用户一个手动打开的提示")

    def test_the_tray_double_click_uses_the_same_deduped_opener(self) -> None:
        """★ 2026-10-26 真机：托盘双击原来走 `_open_url`，绕过互斥体。

        这里钉住：托盘开监控必须调用启动器的 `open_monitor`，不能再自己开新窗。
        """
        from unittest import mock

        import audiomixing.tray as tray

        opener = mock.Mock()
        with mock.patch.object(tray, "_resolve_panel_opener", return_value=opener), \
                mock.patch.object(tray, "_open_url") as old_open:
            tray._open_panel("http://127.0.0.1:17800", "monitor")
        opener.assert_called_once_with("http://127.0.0.1:17800", "monitor")
        old_open.assert_not_called()

    def test_the_launcher_injects_its_opener_into_the_tray(self) -> None:
        """冻结包里 `import audiomixing_launcher` 不可靠，必须由启动器直接注入。"""
        from unittest import mock

        import audiomixing.tray as tray

        old = tray._PANEL_OPENER
        try:
            opener = mock.Mock()
            tray.set_panel_opener(opener)
            self.assertIs(tray._resolve_panel_opener(), opener)
        finally:
            tray.set_panel_opener(old)


class TestTakeOverOldVersion(unittest.TestCase):
    """「新版要把旧版关掉」——2026-10-14 用户明确提出的要求。

    真实现场：双击新版，旧版还在跑，新版只是把旧版的面板叫到前面就退出了
    （启动器原来就是这么写的：检测到有服务在跑就不再启动），于是用户测的
    始终是旧代码。这里钉住判据：比我们旧 → 接管（关掉它）；同版 / 更新 /
    读不到版本 → 让位，绝不乱杀 —— 杀错别人的程序比「没能接管」严重得多。
    """

    def test_an_older_version_is_taken_over(self) -> None:
        import audiomixing_launcher as launcher

        self.assertEqual(launcher.decide_takeover("1.12", "1.13"), launcher._TAKE_OVER)
        self.assertEqual(launcher.decide_takeover("1.9", "1.13"), launcher._TAKE_OVER)

    def test_same_or_newer_version_is_left_alone(self) -> None:
        import audiomixing_launcher as launcher

        self.assertEqual(launcher.decide_takeover("1.13", "1.13"), launcher._DEFER)
        self.assertEqual(launcher.decide_takeover("1.14", "1.13"), launcher._DEFER)
        self.assertEqual(launcher.decide_takeover("2.0", "1.13"), launcher._DEFER)

    def test_unreadable_versions_never_kill_anything(self) -> None:
        import audiomixing_launcher as launcher

        for running, mine in (("", "1.13"), ("1.13", ""), ("未知", "1.13"), (None, None)):
            self.assertEqual(launcher.decide_takeover(running, mine), launcher._DEFER,
                             "读不到版本时绝不能接管（杀错别人的程序更糟）")

    def test_version_text_is_parsed_out_of_the_status_line(self) -> None:
        import audiomixing_launcher as launcher

        self.assertEqual(launcher._parse_version("1.13"), (1, 13))
        self.assertEqual(launcher._parse_version("Audio Mixing v1.13 · exe"), (1, 13))
        self.assertEqual(launcher._parse_version("v1.10.2"), (1, 10, 2))
        self.assertEqual(launcher._parse_version(""), ())
        self.assertEqual(launcher._parse_version(None), ())

    def test_our_version_comes_from_the_package(self) -> None:
        """判据用的是产品自己的版本号，不是写死的常量 —— 版本号一涨它就跟着变。"""
        import audiomixing as pkg
        import audiomixing_launcher as launcher

        self.assertEqual(launcher._our_version(), pkg.__version__)
        self.assertTrue(launcher._parse_version(pkg.__version__))

    def test_build_forces_a_baseline_cpu_isa(self) -> None:
        """★ 2026-10-21 真机 0xc000001d：构建机 Ryzen 9800X3D 有 AVX-512，
        对方 Intel Core Ultra 5 125H 没有，zig 在**链接时**选了 AVX-512 的 memcpy，
        程序进 Python 之前就崩。只加 CFLAGS 不够，必须同时给 LDFLAGS；
        这里钉住构建环境，防止下次又交付一颗只有编译机能跑的 exe。
        """
        import importlib.util
        import pathlib
        import shutil

        root = pathlib.Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "build_nuitka_under_test", root / "tools" / "build_nuitka.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        env = module.build_env()
        try:
            for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
                self.assertIn("-mcpu=baseline", env[key], key)
            args = module.nuitka_args(onedir=True)
            self.assertIn("--zig", args)
            self.assertIn("--target-arch=x86_64", args)
        finally:
            shutil.rmtree(pathlib.Path(env["ZIG_GLOBAL_CACHE_DIR"]).parent,
                          ignore_errors=True)

    def test_the_port_owner_lookup_survives_gbk_output(self) -> None:
        """中文 Windows 上 netstat 输出是 GBK：只写 text=True 会按 UTF-8 解码并抛
        UnicodeDecodeError，被 except 吞掉就悄悄返回 0 —— 于是接管永远拿不到 PID、
        旧实例永远关不掉，新旧两个实例一起抢 17800（2026-10-14 实测现场）。
        这里用 GBK 字节的假 netstat 输出来钉住它。
        """
        import types as _types
        from unittest import mock

        import audiomixing_launcher as launcher

        gbk = ("\n活动连接\n\n  协议  本地地址          外部地址        状态            PID\n"
               "  TCP    0.0.0.0:17800          0.0.0.0:0              LISTENING       43268\n"
               ).encode("gbk")

        def fake_run(_args, **kwargs):
            # 真实 subprocess 的行为：没给 errors 就按 utf-8 严解码 → 抛异常
            if kwargs.get("errors") != "replace":
                raise UnicodeDecodeError("utf-8", gbk, 0, 1, "invalid start byte")
            return _types.SimpleNamespace(stdout=gbk.decode("utf-8", errors="replace"),
                                          stderr="")

        with mock.patch.object(launcher.subprocess, "run", side_effect=fake_run):
            self.assertEqual(launcher._port_owner(17800), 43268,
                             "必须能在 GBK 输出里认出监听端口的那一行")


class TestTrayOpensAWindow(unittest.TestCase):
    """双击右下角托盘图标，必须开出「应用窗口」，不是默认浏览器里的网页版。

    现场（2026-10-14）：tray.py 里写的是 ``from wavelink_launcher import _find_edge``，
    而模块早改名成 audiomixing_launcher —— 那个 import 必然失败、异常又被吞掉，
    于是 edge 恒为空、每次都 webbrowser.open。用户看到的是「网页版」，
    任务栏里也不是那个应用窗。这两条测试钉住"必须走 Edge 应用模式"。
    """

    def test_no_leftover_import_of_the_old_module_name(self) -> None:
        """改名事故的遗留：不许再 import 旧模块名（docstring 里提到它没关系）。"""
        import ast
        import inspect

        from audiomixing import tray

        for node in ast.walk(ast.parse(inspect.getsource(tray))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("wavelink", alias.name.split(".")[0].lower())
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("wavelink", node.module.split(".")[0].lower())
        self.assertTrue(callable(getattr(tray, "_find_edge", None)), "要能自己找到 Edge")

    def test_it_uses_edge_app_mode_when_edge_exists(self) -> None:
        from unittest import mock

        from audiomixing import tray

        with mock.patch.object(tray, "_find_edge", return_value=r"C:\fake\msedge.exe"), \
                mock.patch.object(tray.subprocess, "Popen") as popen:
            tray._open_url("http://127.0.0.1:17800/monitor")
        popen.assert_called_once()
        argv = [str(a) for a in popen.call_args[0][0]]
        self.assertTrue(any(a.startswith("--app=") for a in argv),
                        "必须是 --app= 的应用模式（没有地址栏，才是那个窗口）")
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in argv))

    def test_it_falls_back_to_the_browser_when_edge_is_missing(self) -> None:
        import sys
        from unittest import mock

        from audiomixing import tray

        fake_browser = mock.Mock()
        with mock.patch.object(tray, "_find_edge", return_value=""), \
                mock.patch.object(tray.subprocess, "Popen") as popen, \
                mock.patch.dict(sys.modules, {"webbrowser": fake_browser}):
            tray._open_url("http://127.0.0.1:17800/monitor")
        popen.assert_not_called()
        fake_browser.open.assert_called_once()


class TestTrayKnowsTheRealExe(unittest.TestCase):
    """托盘那两处「当前程序是谁」必须按 Nuitka onefile 的真实形态算。

    自检现场（2026-10-15）：查防火墙规则绑定的程序路径，发现托盘菜单「放行防火墙」
    写出来的规则绑在 ``...\\Temp\\onefile_<pid>_<随机串>\\audiomixing_launcher.py`` ——
    一个**每次运行都不同**的临时路径，等于什么都没放行：用户点完按钮，诊断报告
    还是写「规则没有覆盖当前路径」，手机照样连不上。同一个判断还让托盘图标退化成
    系统默认图标（取不到 exe 自带的那一个）。
    """

    @staticmethod
    def _fake_onefile(tmp: str, exe: pathlib.Path, ghost_dir: str = "") -> types.SimpleNamespace:
        return types.SimpleNamespace(
            original_argv0=str(exe),
            containing_dir=(ghost_dir or str(pathlib.Path(tmp) / "onefile_tmp")),
            onefile=True, standalone=True)

    def test_the_real_exe_is_the_one_the_user_launched(self) -> None:
        from audiomixing import tray

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            tray.__compiled__ = self._fake_onefile(tmp, exe)
            try:
                self.assertEqual(tray._real_exe(), str(exe))
            finally:
                del tray.__compiled__

    def test_source_runs_still_point_at_the_launcher_script(self) -> None:
        """源码运行没有 __compiled__：只能是入口 .py，不能是临时目录里的东西。"""
        from audiomixing import tray

        self.assertFalse(getattr(tray, "__compiled__", None))
        path = tray._real_exe()
        self.assertTrue(os.path.isfile(path))
        self.assertNotIn("onefile_", path)
        self.assertTrue(path.lower().endswith("audiomixing_launcher.py"))

    def test_the_firewall_script_targets_the_real_exe_not_a_temp_dir(self) -> None:
        """这条是缺陷回归：写进批处理的路径必须是真 exe，不许出现解包临时目录。"""
        from unittest import mock

        from audiomixing import tray

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v9.9.exe"
            exe.write_bytes(b"fake")
            tray.__compiled__ = self._fake_onefile(tmp, exe)
            try:
                # 千万不要真的弹 UAC：ShellExecuteW 换成空实现
                with mock.patch.object(tray.ctypes.windll.shell32, "ShellExecuteW",
                                       return_value=42):
                    path = tray.add_firewall_rules()
            finally:
                del tray.__compiled__
        try:
            text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.assertIn(str(exe), text, "规则必须绑在用户双击的那颗 exe 上")
        self.assertNotIn("onefile_", text, "临时解包目录不许出现在规则里")
        self.assertIn("protocol=TCP", text)
        self.assertIn("protocol=UDP", text)


class TestAutoListenRevoke(unittest.TestCase):
    """④':对端停了「送声音」→ 本机替它自动打开的那条收听要收回来。

    用户真机反馈（2026-10-15）：「手机 WiFi 送声音关闭后，电脑听的选项不会自动关。」
    边界同样重要：只撤**我们替他建的**那条；用户自己建的收听不能被动。
    """

    @staticmethod
    def _node():
        return AudioMixingNode(_config_stub("本机", "node-self"), audio_port=free_udp_port(),
                            discovery_port=free_udp_port(), simulate=True)

    def test_it_takes_back_the_route_we_opened(self) -> None:
        node = self._node()
        node.config.smart_link = ["node-phone"]    # ★ 第 6 条：先开智连，它才会自动开门
        node._grant_listen_request("node-phone", "手机", "192.168.1.9")
        self.assertTrue(any(str(r.get("auto")) == "True" or r.get("auto") for r in node.routes),
                        "先确认自动收听真的开过")
        node._revoke_auto_listen("node-phone")
        self.assertEqual([r for r in node.routes
                          if "node-phone" in [str(t) for t in r.get("from") or []]], [],
                         "对端停了，替它开的那条收听要收回去")

    def test_a_route_the_user_made_is_never_touched(self) -> None:
        """用户自己建的收听不算"我们替他开的"，撤销时必须原样留着。"""
        node = self._node()
        own = {"id": "my-route", "from": ["node-phone"], "to": [node.node_id], "gain": 1.0,
               "mute": False, "codec": node.config.codec, "source_device": "",
               "sink_device": "", "label": "我手动建的"}
        node.set_routes([own])
        node._grant_listen_request("node-phone", "手机", "192.168.1.9")   # 只记一笔（created=False）
        node._revoke_auto_listen("node-phone")
        self.assertEqual([str(r.get("id")) for r in node.routes], ["my-route"])


class TestBluetoothRequest(unittest.TestCase):
    """④ 的电脑端：手机点了「走蓝牙」之后，**由电脑**去连它。

    用户原话（2026-10-15）：「手机确认走蓝牙后应当由电脑发送蓝牙请求并配对互相连接。」
    他能做到的是"连接"（打开 A2DP sink）；"配对"Windows 不给普通程序 API，所以
    没配对过的设备必须如实回报 no_device，而不是假装成功。
    """

    @staticmethod
    def _node():
        return AudioMixingNode(_config_stub("本机", "node-bt"), audio_port=free_udp_port(),
                            discovery_port=free_udp_port(), simulate=True)

    def test_connect_by_name_finds_the_paired_phone(self) -> None:
        from unittest import mock

        from audiomixing import bluetooth as bt

        device = {"id": "\\\\?\\BTHENUM#dev#SNK", "name": "瘾控", "link": "off"}
        with mock.patch.object(bt, "list_devices_cached", return_value=[device]), \
                mock.patch.object(bt, "connect", return_value={"ok": True, "state": "Opened"}) as conn:
            result = bt.connect_by_name("瘾控")
        self.assertTrue(result["ok"])
        conn.assert_called_once_with(device["id"])

    def test_connect_by_name_does_not_reconnect_what_is_already_up(self) -> None:
        from unittest import mock

        from audiomixing import bluetooth as bt

        device = {"id": "x\\SNK", "name": "瘾控", "link": "waiting"}
        with mock.patch.object(bt, "list_devices_cached", return_value=[device]), \
                mock.patch.object(bt, "connect") as conn:
            result = bt.connect_by_name("瘾控")
        self.assertEqual(result["state"], "already")
        conn.assert_not_called()

    def test_connect_by_name_is_honest_when_the_phone_was_never_paired(self) -> None:
        """没配对过就要说清楚 —— 假成功比失败更害人（用户会一直等声音）。"""
        from unittest import mock

        from audiomixing import bluetooth as bt

        with mock.patch.object(bt, "list_devices_cached", return_value=[]):
            result = bt.connect_by_name("瘾控")
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "no_device")
        self.assertIn("配对", result["error"])

    def test_a_phone_asking_for_bluetooth_makes_the_pc_connect_once(self) -> None:
        from unittest import mock

        import audiomixing.bluetooth as bt

        node = self._node()
        with mock.patch.object(bt, "connect_by_name",
                               return_value={"ok": True, "state": "Opened", "device": "瘾控"}) as fn:
            node._request_bluetooth("node-phone", "瘾控")
            for _ in range(60):                      # 后台线程：最多等 3 秒
                reqs = node.bluetooth_requests()
                if reqs and reqs[0].get("state") != "connecting":
                    break
                time.sleep(0.05)
            node._request_bluetooth("node-phone", "瘾控")      # 30 秒内第二次：必须被节流
            time.sleep(0.2)
        self.assertEqual(fn.call_count, 1, "同一台 30 秒内只许去连一次（否则会把链路顶掉）")
        self.assertTrue(node.bluetooth_requests()[0].get("ok"))

    def test_the_diagnosis_report_has_a_bluetooth_section(self) -> None:
        """蓝牙这一路一直定位不了，就是因为报告里**根本没有它的状态**（2026-10-15 补上）。"""
        from unittest import mock

        import audiomixing.bluetooth as bt

        from audiomixing.webui import AudioMixingApi

        node = self._node()
        device = {"id": "x\\SNK", "name": "瘾控", "link": "waiting", "state": "Opened",
                  "held": True, "endpoint": False, "hfp": False}
        with mock.patch.object(bt, "cached_devices", return_value=[device]):
            text = AudioMixingApi(node).diagnose()["text"]
        self.assertIn("蓝牙（手机的声音经蓝牙进电脑", text)
        self.assertIn("手机已连接，但媒体音频未到", text)
        self.assertNotIn("电脑已就绪", text, "文案砍掉后半句（用户：「然后『手机未连接』」）")
        self.assertIn("瘾控", text)


class TestBluetoothAddress(unittest.TestCase):
    """★ 2026-10-16 新增：蓝牙方块上要标「蓝牙地址」（用户：「最好还能标记蓝牙地址」）。

    地址就在 Windows 的设备实例 ID 里；下面这个形状是实机抄下来的（真机名不写进来）。
    解析结果与 Windows 自己的 DEVPKEY_Bluetooth_DeviceAddress 一致 —— 实机核对过。
    """

    def test_endpoint_state_prefers_the_a2dp_endpoint_and_accepts_any_case(self) -> None:
        """★ 2026-10-26 用户真机：手机蓝牙已经出声，面板还报「媒体音频未到」。

        根因之一是只扫 Capture、要求名字里必须有 "A2DP SNK"，而且比较大小写敏感。
        这里钉住：优先用 A2DP/SNK 端点；名字归一化后大小写/空格不影响匹配。
        """
        from unittest import mock

        from audiomixing import bluetooth as bt

        self.assertEqual(bt._norm_device_text("  Phone   A "), "phonea")
        fake = [{"flow": "Capture", "name": "PHONE A A2DP SNK",
                 "state": 1, "active": True, "bluetooth": True}]
        with mock.patch.object(bt, "mmdevice_endpoints", return_value=fake) as fn:
            self.assertTrue(bt._endpoint_state_from_registry("Phone A"))
            fn.assert_called_once_with("Phone A")

    def test_a_non_a2dp_endpoint_is_only_a_fallback(self) -> None:
        """找不到 A2DP/SNK 名字时退回任何含设备名的端点；有 A2DP 时以它为准。"""
        from unittest import mock

        from audiomixing import bluetooth as bt

        # 只有 HFP 端点、且未激活 → False（至少证明"找到了，但没声音"）。
        fake = [{"flow": "Capture", "name": "Phone A Hands-Free",
                 "state": 8, "active": False, "bluetooth": False}]
        with mock.patch.object(bt, "mmdevice_endpoints", return_value=fake):
            self.assertFalse(bt._endpoint_state_from_registry("Phone A"))

        # A2DP 端点 active，HFP 未激活 → True（以 A2DP 为准）。
        fake.append({"flow": "Capture", "name": "Phone A A2DP SNK",
                     "state": 1, "active": True, "bluetooth": True})
        with mock.patch.object(bt, "mmdevice_endpoints", return_value=fake):
            self.assertTrue(bt._endpoint_state_from_registry("Phone A"))

        # 一个端点都找不到 → None（调用方沿用旧值，不瞎改）。
        with mock.patch.object(bt, "mmdevice_endpoints", return_value=[]):
            self.assertIsNone(bt._endpoint_state_from_registry("Phone A"))

    def test_it_reads_the_address_out_of_the_instance_id(self) -> None:
        from audiomixing import bluetooth as bt

        inst = (r"\\?\BTHENUM#{0000110a-0000-1000-8000-00805f9b34fb}"
                r"_VID&0001001d_PID&0000#b&62612bf&0&5CA06CCA0A6E_C00000000"
                r"#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\SNK")
        self.assertEqual(bt.device_address(inst), "5C:A0:6C:CA:0A:6E")

    def test_a_foreign_id_gives_empty_not_a_guess(self) -> None:
        from audiomixing import bluetooth as bt

        for bad in ("", "not-an-id", r"\\?\BTHENUM#junk#b&1&0&ZZZZ_C0#x\SNK",
                    r"\\?\BTHENUM#x#b&62612bf&0&5CA06CCA0A6E"):
            self.assertEqual(bt.device_address(bad), "", "认不出来就写空，不许瞎猜")


class TestListRowGrids(unittest.TestCase):
    """⑦ 蓝牙 / ⑧ 网络音源 两行的栅格，必须各自对上自己的元素数。

    现场（2026-10-15 用户截图「UI 重叠」）：蓝牙那行只有 5 个元素
    （名字 / 胶囊 / 3 个按钮），网络音源那行有 6 个，两行却共用一套
    ``repeat(5, minmax(0,auto))`` —— 列数对不上，而且 minmax(0,…) 允许把列压成 0 宽，
    胶囊（min-width:126px）于是溢出自己的格子、压到旁边的按钮上。
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_the_two_device_lists_share_one_square_grid(self) -> None:
        """2026-10-16 起两个列表都是"设备方块"，共用同一套栅格 —— 不再各写各的列数。

        （以前是"一行一台、按列对齐"的细行，蓝牙 5 列、网络音源 6 列；现在名字/胶囊/
        按钮都放进 .dvcard 里自由换行，列数这个概念就没有了。）
        """
        html = self._html()
        self.assertIn("#deviceCards, #btList{", html, "两个设备列表要共用同一条栅格")
        self.assertIn("grid-template-columns:repeat(auto-fill,minmax(240px,1fr))", html,
                      "方块栅格：能并排就并排，窄了就落成单列")
        self.assertNotIn("auto-fit,minmax(240px,1fr)", html,
                         "auto-fit 会把空轨道折掉 → 只有一张卡时那张卡被拉满整行（图1）")
        self.assertNotIn("netAudioList", html,
                         "「Wi-Fi 送声音的手机」那一段随图4 整块删掉，不该再有 id 残留")

    def test_every_device_card_keeps_one_width_but_not_one_height(self) -> None:
        """★ 2026-10-16（图1）「这些设备的卡片大小保持一致」→（图2）「同宽就好了，不需要同高」。

        上一轮先做到了"一样大"（共用栅格 + 所有卡片统一 `min-height:196px`），
        用户看到蓝牙方块下面那一片留白后改口：**宽度一致就够，高度由内容决定**。
        所以这条现在钉的是：
          * 两类卡片（局域网 deviceCard / 蓝牙 dvcard phone）都必须是 `.dvcard`，
            否则下面这些规则管不到它们；
          * `.dvcard` 不许再有 `min-height`（那正是留白的来源）；
          * 宽度一致靠两个列表共用同一条栅格（见上一条测试）。
        """
        import re

        html = self._html()
        rule = re.search(r"\.dvcard\{[^}]*\}", html, re.S)
        self.assertTrue(rule, "找不到 .dvcard 的规则")
        text = rule.group(0)
        self.assertIn("display:flex", text, "卡片要能自己排版")
        self.assertIn("flex-direction:column", text)
        self.assertNotIn("min-height", text,
                         "用户图2：「不需要同高（不然会留白不好看）」—— 统一高度那套不许回来")
        # 两类卡片都必须是 .dvcard，否则上面那条规则管不到它们。
        self.assertIn('return `<div class="dvcard${self ? \' self\' : \'\'}', html)
        # ★ 2026-10-18：蓝牙那张卡片多了个 `id="btcard-<i>"`（为了"只重画被操作的那一张"，
        #   见 test_a_bluetooth_card_is_rebuilt_alone）—— 所以这里跟着实际标记一起更新，
        #   否则这条测试会拿一个已经不存在的字符串去比，变成假阴性。
        self.assertIn('return `<div class="dvcard phone" id="btcard-${i}">', html)
        # 按钮文案里不许夹标注（用户：「括号内内容不要写进按键哦」）。
        self.assertNotIn("（点一下断开）", html, "那条括号注解要留在 title 里，不进按钮文案")

    def test_no_zero_width_columns_any_more(self) -> None:
        self.assertNotIn("grid-template-columns:minmax(96px,150px) repeat(5, minmax(0,auto))",
                         self._html(),
                         "列数对不上、还能被压成 0 宽的写法不许再回来（那正是重叠的原因）")


class TestOutputLevelAndBluetoothCleanup(unittest.TestCase):
    """2026-10-16 三件真机反馈的回归：

    * 「手机播放声音这个电平没有反应」—— 手机走蓝牙进来时，声音是 **Windows 直接播放**
      的，本机直通那一路根本没有数据，所以面板上的电平恒为 0。修法是**直读当前输出
      设备的峰值表**（不采集、不播放、不动系统音量），见 engine.output_level()。
    * 「电脑手机现在都只有送/收两个条」—— 那两根条要改成**音量**条，不是码率条
      （静音也照样按 1536kbps 出数据）。所以 sender / receiver 各自报出 peak。
    * 「关闭服务后，手机声音依旧通过蓝牙传入电脑」—— 连接握在助手子进程里，
      主进程退掉不会挂断它，必须退出时显式 disconnect_all()。
    """

    @staticmethod
    def _node(config=None):
        cfg = config or _config_stub("本机", "node-meter")
        return AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)

    # ---- 电平 -------------------------------------------------------------
    def test_chunk_peak_accepts_every_shape_a_block_arrives_in(self) -> None:
        """同一块音频可能以 numpy / array / list 三种形状到这里，不能因为形状就抛异常。

        之前写成 `if not samples:` —— numpy 数组一进来就抛
        "truth value of an array with more than one element is ambiguous"，
        整条发送线程直接死掉（测试当场抓到）。
        """
        from audiomixing.net import rtpaudio

        self.assertEqual(rtpaudio._chunk_peak(None), 0.0)
        self.assertEqual(rtpaudio._chunk_peak([]), 0.0)
        self.assertEqual(rtpaudio._chunk_peak(np.zeros(0, dtype=np.float32)), 0.0)
        self.assertAlmostEqual(rtpaudio._chunk_peak([0.0, -0.5, 0.25]), 0.5, places=5)
        self.assertAlmostEqual(rtpaudio._chunk_peak(array("f", [0.0, 0.75])), 0.75, places=5)
        self.assertAlmostEqual(
            rtpaudio._chunk_peak(np.array([-0.9, 0.0], dtype=np.float32)), 0.9, places=5)

    def test_the_receiver_reports_how_loud_the_incoming_sound_is(self) -> None:
        """「收」那根条要显示**收到多大声**；从前只有码率，静音也显示满格。"""
        from audiomixing.net.rtpaudio import JitterBuffer

        buf = JitterBuffer(name="手机A", route_id="r1")
        self.assertEqual(buf.stats()["peak"], 0.0)
        loud = array("f", [0.6, -0.6] * 480)
        buf.write(loud, 0, 0)
        self.assertGreaterEqual(buf.stats()["peak"], 0.55, "收到大声的音频，峰值必须上来")

    def test_the_sender_reports_how_loud_the_outgoing_sound_is(self) -> None:
        """「送」那根条要显示**送出去多大声**，并且把这一层的增益算进去。"""
        from audiomixing.net import rtpaudio

        class _Sock:
            def send(self, payload):
                return len(payload)

        session = rtpaudio.SenderSession(
            None, "127.0.0.1", 9, codec=proto.CODEC_PCM_S16,
            gain=2.0, route_id="r1", node_id="node-self")
        session._sock = _Sock()
        session._send_chunk(array("f", [0.3, -0.3] * 480))
        # 0.3 × 增益 2.0 = 0.6：条上看到的必须是用户实际送出去的那份大小。
        self.assertAlmostEqual(session.stats()["peak"], 0.6, places=2)

    def test_the_sender_leaves_the_shared_gain_scaling_to_the_receiver(self) -> None:
        """★ 2026-11-02：同一条共享增益两端各乘一次 → 0.40 实际只有 0.16。

        现在发送端只写 `gain_q14`（如实描述这条流），样本原样发；接收端
        （PC 的 sink / 手机的 AudioRecv#hostGain）乘一次。电平台仍按增益显示。
        """
        from audiomixing.net import rtpaudio

        captured = {}

        class _Sock:
            def send(self, payload):
                captured["packet"] = payload
                return len(payload)

        session = rtpaudio.SenderSession(
            None, "127.0.0.1", 9, codec=proto.CODEC_PCM_S16,
            gain=0.4, route_id="r1", node_id="node-self")
        session._sock = _Sock()
        session._send_chunk(array("f", [0.3, -0.3] * 480))
        sent = proto.decode(captured["packet"])
        samples = proto.decode_pcm_s16(sent.payload)
        self.assertAlmostEqual(abs(samples[0]), 0.3, delta=0.002,
                               msg="发送端不能自己缩样本，否则接收端再乘一次就变成 0.12")
        self.assertAlmostEqual(sent.gain, 0.4, places=3,
                               msg="包头里的 gain_q14 仍要如实描述这条链路的音量")
        self.assertAlmostEqual(session.stats()["peak"], 0.12, places=2,
                               msg="「送」电平仍按最终会听到的大小显示")

    def test_output_level_stays_zero_when_nothing_is_playing(self) -> None:
        out = self._node().output_level()
        self.assertEqual(out["peak"], 0.0)
        self.assertEqual(out["source"], "none")
        self.assertFalse(out["measured"])

    def test_output_level_takes_the_loudest_of_meter_local_and_sinks(self) -> None:
        """三个来源各看得见声音的一部分，取最大 —— 手机蓝牙那种"我们没参与"的声音
        只有直读表看得见，而别的机器走我们自己的播放端时只有 sink 看得见。"""
        node = self._node()
        node.sinks["r1"] = types.SimpleNamespace(peak=0.4)
        self.assertEqual(node.output_level()["source"], "sink")
        self.assertAlmostEqual(node.output_level()["peak"], 0.4, places=4)
        node._out_meter = types.SimpleNamespace(peak=0.9, device="耳机", error="")
        top = node.output_level()
        self.assertEqual(top["source"], "meter")
        self.assertAlmostEqual(top["peak"], 0.9, places=4)
        self.assertEqual(top["device"], "耳机")
        self.assertTrue(top["meter_on"])
        self.assertTrue(top["meter_ok"])

    def test_enable_output_meter_is_off_in_tests_and_on_when_asked(self) -> None:
        """直读表只由真正的服务进程打开：模拟运行/没有 wasapi 时必须拒绝，
        否则跑一次单元测试就会去碰用户的真实声卡。"""
        from audiomixing import engine as engine_mod

        node = self._node()
        self.assertFalse(node.enable_output_meter(), "模拟运行不许打开真实声卡的电平表")

        with mock.patch.object(audio_base, "available_backends", return_value=["wasapi"]), \
                mock.patch.object(engine_mod, "_OutputLevelMeter") as meter_cls:
            node.simulate = False
            self.assertTrue(node.enable_output_meter())
            meter_cls.return_value.start.assert_called_once()
        node._out_meter = None

    def test_the_status_json_carries_the_output_level(self) -> None:
        """面板读的是 /api/status 里的 output 段，字段少一个界面就只能退回写 0。"""
        node = self._node()
        node._out_meter = types.SimpleNamespace(peak=0.33, device="耳机", error="")
        out = node.status()["output"]
        self.assertAlmostEqual(out["peak"], 0.33, places=4)
        self.assertEqual(out["source"], "meter")

    # ---- 退出时挂断蓝牙 ---------------------------------------------------
    def test_disconnect_all_hangs_up_every_held_helper(self) -> None:
        """退出服务时必须把所有还握着的蓝牙连接放掉，否则手机的声音会一直往里送。"""
        from unittest import mock

        from audiomixing import bluetooth as bt

        original_dir = bt._STATE_DIR
        try:
            bt._STATE_DIR = _mkdtemp()
            keys = [bt._key("dev-a"), bt._key("dev-b")]
            for key in keys:
                (bt._STATE_DIR / ("%s.keep" % key)).write_text("1", encoding="utf-8")
                (bt._STATE_DIR / ("%s.pid" % key)).write_text("999999", encoding="utf-8")
                (bt._STATE_DIR / ("%s.status" % key)).write_text("open:Opened", encoding="utf-8")
            stopped = []
            with mock.patch.object(bt, "_stop_helper", side_effect=stopped.append):
                result = bt.disconnect_all()
            self.assertEqual(result["count"], 2)
            self.assertEqual(sorted(result["closed"]), sorted(keys))
            self.assertEqual(len(stopped), 2, "每个助手都要收到一次停止请求")
            for key in keys:
                self.assertFalse((bt._STATE_DIR / ("%s.keep" % key)).exists(),
                                 "哨兵必须删掉，否则助手会一直握着连接")
                self.assertFalse((bt._STATE_DIR / ("%s.pid" % key)).exists())
            self.assertFalse(list(bt._STATE_DIR.glob("*.keep")), "退出后不该再留下任何握着的连接")
        finally:
            bt._STATE_DIR = original_dir

    def test_an_empty_bluetooth_list_is_still_cached(self) -> None:
        """★ 2026-10-16 实测抓到的**性能真故障**（v1.29 打包后跑 unittest 时抓到的）：

        枚举"这台机器现在一台配对的蓝牙设备都没有"时，原来**不进缓存**（旧代码要求缓存
        非空才算命中），于是每一次调用都重跑那个约 2.6 秒的 PowerShell。而引擎的
        `apply_routes()` 每一轮（维护循环 0.5 秒一轮）都要问一次蓝牙名单 ——
        结果整台机器的音频路径被一个 2.6 秒的子进程反复拖住：实测 `tick()` 单次 2.7 秒，
        两个节点之间的音频流直接超时（三条引擎测试当场变红）。用户侧的表现就是"越用越卡"。

        现在空结果同样算"刚查过"：查过就在 max_age 内命中缓存。
        """
        from unittest import mock

        from audiomixing import bluetooth as bt

        with mock.patch.object(bt, "list_devices", return_value=[]) as fn:
            bt.invalidate_cache()
            bt.list_devices_cached(max_age=60.0)
            bt.list_devices_cached(max_age=60.0)
            bt.list_devices_cached(max_age=60.0)
        self.assertEqual(fn.call_count, 1, "空表也要进缓存：三次调用只许真枚举一次")

    def test_a_forced_refresh_still_enumerates(self) -> None:
        """面板上那颗「刷新」按钮要真的去问一次（force / 缓存过期都不能被这条挡住）。"""
        from unittest import mock

        from audiomixing import bluetooth as bt

        with mock.patch.object(bt, "list_devices", return_value=[]) as fn:
            bt.invalidate_cache()
            bt.list_devices_cached(max_age=60.0)
            bt.list_devices_cached(max_age=60.0, force=True)
        self.assertEqual(fn.call_count, 2, "force=True 必须真的重查一次")

    def test_disconnect_all_is_safe_with_nothing_held(self) -> None:
        from audiomixing import bluetooth as bt

        original_dir = bt._STATE_DIR
        try:
            bt._STATE_DIR = _mkdtemp()
            result = bt.disconnect_all()
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 0)
        finally:
            bt._STATE_DIR = original_dir


class TestSignalWithoutAudio(unittest.TestCase):
    """★ 2026-10-16 用户（图1）：「虽然二者没有收发声音，但是设备间信号应当存在」。

    缺陷：这一格原来只在"正在收它的音频流"时才有分数（netmic 的丢包/缓冲），
    两台设备明明互相看得见、链路好好的，卡片上却写着"还没有可测的链路"。
    修法是用**不依赖音频**的证据，顺序从精确到粗糙：正在收的声音 → PING/PONG 往返时延
    → 信标心跳（手机不回 PING/PONG，只能走这条），三种都没有才写"没有依据"。
    """

    @staticmethod
    def _node():
        cfg = _config_stub("本机", "node-signal")
        return AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)

    def test_the_scores_are_monotonic_and_bounded(self) -> None:
        from audiomixing import engine as engine_mod

        self.assertEqual(engine_mod._rtt_score(1.0), 100, "1ms 的局域网时延就是满分")
        self.assertLess(engine_mod._rtt_score(60.0), engine_mod._rtt_score(10.0))
        self.assertEqual(engine_mod._rtt_score(400.0), 0, "再差的时延也不会是负分")
        self.assertEqual(engine_mod._heartbeat_score(1.0, None), 100, "心跳刚收到 → 满分")
        self.assertEqual(engine_mod._heartbeat_score(5.0, None), 80, "迟到 5 秒 → 80 分")
        self.assertEqual(engine_mod._heartbeat_score(15.0, None), 0)
        self.assertLess(engine_mod._heartbeat_score(2.0, 40.0),
                        engine_mod._heartbeat_score(2.0, None), "心跳忽长忽短要再扣")
        self.assertIsNone(engine_mod._loss_score({"packets": 0, "lost": 0}),
                          "一个包都没统计到 = 没有依据（None），不是 0 分")
        self.assertEqual(engine_mod._loss_score({"packets": 100, "lost": 0}), 100)

    def test_the_registry_records_beacon_intervals(self) -> None:
        """心跳节奏是"没有音频流时"唯一的证据，所以必须真的被记下来。"""
        from audiomixing.net.discovery import DiscoveryRegistry

        reg = DiscoveryRegistry("node-self")
        payload = {"id": "node-phone", "name": "手机A", "audio_port": 47831}
        reg.update(payload, "198.51.100.10")
        peer = reg.get("node-phone")
        self.assertIsNotNone(peer)
        for gap in (2.0, 2.0, 2.1, 2.0):
            peer.last_seen -= gap        # 假装信标是 gap 秒之前到的
            reg.update(payload, "198.51.100.10")
        beats = reg.get("node-phone")
        self.assertIsNotNone(beats.beat_ms, "满 3 个样本之后就该有中位间隔")
        self.assertAlmostEqual(beats.beat_ms, 2000.0, delta=120.0)
        self.assertIsNotNone(beats.beat_jitter_ms)
        self.assertIn("beat_ms", beats.to_json())

    def test_a_visible_peer_with_no_audio_still_has_a_signal(self) -> None:
        """这就是图1 的那个缺陷：没在收发声音，也要有信号。"""
        from unittest import mock

        node = self._node()
        peer = type("P", (), {})()
        peer.node_id, peer.age_s, peer.online = "node-phone", 1.2, True
        peer.beat_ms, peer.beat_jitter_ms = 2000.0, 20.0
        with mock.patch.object(node.registry, "all", return_value=[peer]), \
                mock.patch.object(node.receiver, "stats", return_value={"streams": []}), \
                mock.patch.object(node.receiver, "clock_stats", return_value=[]):
            sig = node.signals_state()["node-phone"]
        self.assertTrue(sig["known"], "看得见它就必须给得出信号")
        self.assertEqual(sig["source"], "heartbeat", "手机不回 PING/PONG，只能用心跳")
        self.assertGreaterEqual(sig["score"], 90, "心跳准点（抖动 20ms）应该接近满分")
        self.assertEqual(sig["beat_ms"], 2000.0)

    def test_rtt_beats_heartbeat_and_the_worst_evidence_wins(self) -> None:
        """三种证据都在时取**最差**的：哪条路有毛病都要看得见。"""
        from unittest import mock

        node = self._node()
        peer = type("P", (), {})()
        peer.node_id, peer.age_s, peer.online = "node-pc", 1.0, True
        peer.beat_ms, peer.beat_jitter_ms = 2000.0, 50.0
        streams = [{"node_id": "node-pc", "packets": 900, "lost": 100,
                    "buffered_ms": 40.0, "target_ms": 55.0}]
        clocks = [{"peer": "node-pc", "rtt_ms": 3.0, "samples": 20}]
        with mock.patch.object(node.registry, "all", return_value=[peer]), \
                mock.patch.object(node.receiver, "stats", return_value={"streams": streams}), \
                mock.patch.object(node.receiver, "clock_stats", return_value=clocks):
            sig = node.signals_state()["node-pc"]
        self.assertEqual(sig["source"], "stream", "丢包那一路最差 → 报它")
        self.assertEqual(sig["score"], 60, "丢 10% → 扣 40 分（缓冲正常，不再额外扣）")
        self.assertEqual(sig["rtt_ms"], 3.0, "三个证据的原始值都要留着，界面要说明它拿什么量的")

    def test_a_peer_that_never_answered_says_no_evidence(self) -> None:
        from unittest import mock

        node = self._node()
        with mock.patch.object(node.registry, "all", return_value=[]), \
                mock.patch.object(node.receiver, "stats", return_value={"streams": []}), \
                mock.patch.object(node.receiver, "clock_stats", return_value=[]):
            self.assertEqual(node.signals_state(), {}, "没有任何设备就没有任何信号条目")

    def test_the_status_json_carries_the_signals(self) -> None:
        node = self._node()
        self.assertIn("signals", node.status(), "面板读的是 /api/status 里的 signals 段")


class TestTheFigureTwoShell(unittest.TestCase):
    """★ 2026-10-16 用户（图2）：「UI问题，学习localsend。按我的图2排版。括号内内容不要写进按键哦」。

    一级菜单 = 左边一竖条（LOGO / 监控 / 简洁 / 详细 / 二维码 / 版本号）；
    二级菜单 = 上面一行标签（本机状态 / 设备与路由 / 广播 / 多耳机）。
    并要求「这次直接改ui设计免得你对不齐」→ 那套"两列 + 按高度装箱"必须真的删干净。
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_the_rail_has_exactly_the_six_items_he_drew(self) -> None:
        html = self._html()
        self.assertIn('<div class="app">', html)
        self.assertIn('<aside class="rail"', html, "一级菜单是左边那一竖条")
        self.assertIn(">Audio Mixing</div>", html, "LOGO")
        for label in (">监控<", ">简洁<", ">详细<", ">二维码<", ">设置<"):
            self.assertIn(label, html, "一级菜单少了 %s" % label)
        # v2.10 用户要求：原来那一页叫「版本号」，改名为「设置」
        self.assertNotIn(">版本号</button>", html)

    def test_the_tabs_are_the_four_he_drew(self) -> None:
        html = self._html()
        self.assertIn('<nav class="tabs"', html, "二级菜单是上面那一行标签")
        for label in (">本机状态<", ">设备与路由<", ">广播<", ">多耳机<"):
            self.assertIn(label, html, "二级菜单少了 %s" % label)

    def test_the_mockup_annotation_never_leaks_into_a_label(self) -> None:
        """他明确说了：「括号内内容不要写进按键哦。我标注给你看的」。

        括号里那句（「蓝牙和WiFi」）是**给我看的标注**，不是按钮文案 —— 按钮上只有
        「设备与路由」四个字。这条测试就是把这句话钉住。

        查的是**元素的文字本体**（`<button>/<a>` 之间那几个字），不是整个文件 ——
        注释里引用图上的标注是"说明他把这句话标给我看"，那不算漏进界面。
        """
        import re

        html = self._html()
        # 「蓝牙和WiFi」这串字整份文件里都不许出现（注释里说明这件事时也用的是
        # 「蓝牙 + WiFi」，故意不写成同一串 —— 这条断言就靠它守住）。
        self.assertNotIn("蓝牙和WiFi", html, "图2 那句标注（蓝牙和WiFi）漏进了界面")
        # 先把注释剥掉再抠元素文字 —— 否则注释里为了说明"左栏有一个 <a> 的监控"而写的
        # `<a>` 这几个字会被当成真的元素，把后面一整段 CSS 当成按钮文案抠出来。
        bare = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        bare = re.sub(r"/\*.*?\*/", "", bare, flags=re.S)
        # 静态标记里的按钮/链接文字都不许带括号；`${…}` 是页面脚本现场拼的。
        # （2026-10-16 起连现场拼的那些也不许带了：设备卡上「（点一下断开）」那条注解
        #   已经挪进 title —— 见 test_every_device_card_keeps_one_size。）
        labels = re.findall(r'<(?:button|a)\b[^>]*>([^<]*)<', bare)
        leaked = [x.strip() for x in labels if "（" in x and "${" not in x]
        self.assertFalse(leaked, "按钮/链接的文字里混进了标注（括号）：%s" % leaked)
        self.assertNotIn("（点一下断开）", html)
        # 二级菜单那四个标签就该是那四个词，一个字都不多、不少。
        tabs = [t.strip() for t in re.findall(r'class="tab[^"]*"[^>]*>([^<]*)<', html) if t.strip()]
        self.assertEqual(tabs, ["本机状态", "设备与路由", "广播", "多耳机"])

    def test_the_two_column_packer_is_really_gone(self) -> None:
        """「这次直接改ui设计免得你对不齐」——装箱那套留着就是"半留半删"。"""
        html = self._html()
        # 只查"代码形状"的写法，不查那几个名字本身 —— 注释里必须说明"这套删了"，
        # 提一次是为了让人知道为什么界面上没有它。
        for gone in ("function packColumns(", "applyCardLayout()", "packColumns()",
                     "data-hero=", "class=\"wrap\"", "class=\"col\"", ".wrap > .col.hero"):
            self.assertNotIn(gone, html, "旧的两列装箱残留：%s" % gone)
        for keep in ("function setTab(", "function applyPane(", "function initPane(",
                     "body[data-pane="):
            self.assertIn(keep, html, "新的标签页机制不在：%s" % keep)

    def test_the_two_menus_are_two_independent_dimensions(self) -> None:
        """★ 2026-10-16 第三轮（用户图1/图2）：「第一菜单有时候不能保持，直接第二菜单生效了。
        我要求是双维度控制。就像乘法一样。」

        上一版的根因：两个菜单共用一个状态变量 —— 点标签会把二维码/版本号的选中冲掉，
        点二维码又看不出左边该亮哪一项。现在必须是**两个独立状态**：
          · 页面维度 wl-pane（四个标签 + 二维码 + 版本号）；
          · 密度维度 wl-mode（简洁 / 详细，对所有页生效）。
        并且左栏**永远恰好一项高亮**：在标签页时亮密度那一项，在二维码/版本号页时亮它自己
        （上一版出现"二维码亮着、简洁也像亮着"，用户就说它优先级高了）。
        顶部标签则永远亮"上一次所在的标签页"，所以在二维码那页两个维度同时可见。
        """
        html = self._html()
        self.assertIn("const TABS = ['status', 'devices', 'cast', 'multi']", html, "二级菜单那一维")
        self.assertIn("let lastTab = 'status'", html, "上一次所在的标签页要单独记着")
        self.assertIn("function setTab(", html)
        self.assertIn("function setRail(", html)
        self.assertIn("function densityName()", html, "密度那一维单独取值，不和页面混在一起")
        self.assertIn("const onRailPage = (activePane === 'qr' || activePane === 'ver')", html)
        self.assertIn("if(r === 'qr' || r === 'ver') b.classList.toggle('on', activePane === r)", html)
        self.assertIn("b.classList.toggle('on', !onRailPage && r === densityName())", html)
        self.assertIn("b.classList.toggle('on', b.dataset.tab === lastTab)", html,
                      "顶部标签按 lastTab 亮 —— 在二维码那页也保持上面那一维可见")
        self.assertIn("if (activePane === 'qr' || activePane === 'ver') {", html,
                      "点密度项时若停在二维码/版本号，要回到上一次的标签页（否则看着像点了没反应）")
        self.assertNotIn("uiSegButtons", html, "上一版那套按 data-mode 比的写法不许回来")

    def test_the_version_line_wraps_and_sits_at_the_bottom_left(self) -> None:
        """★ 2026-10-16（图5）：「在内容不足的时候版本号不会在框的左下角。
        版本号和日期要换行（精确到日就好了）」。"""
        html = self._html()
        # 左栏要被拉伸到视口高度，`.verline{margin-top:auto}` 才推得到底
        self.assertIn(".app{display:flex;align-items:stretch;min-height:100vh", html)
        self.assertIn("min-height:100vh;max-height:100vh;overflow:auto", html)
        # 两行：版本一行、构建日期一行；日期只到天（时分没有意义）
        self.assertIn("line.innerHTML = `<b>v${esc(b.version)}</b><br>构建 ${esc(day)}`;", html)
        self.assertIn("String(b.build_time || '').slice(0, 10)", html, "日期精确到日")

    def test_the_lan_device_cards_are_compacted_like_the_bluetooth_ones(self) -> None:
        """★ 2026-10-16（图3）：「局域网的设备卡片也应该像蓝牙的卡片一样小小的」，
        之后（图1）又强调「我一直在说这些设备的卡片大小保持一致」。

        尺寸收紧到和蓝牙同一档（同一条栅格 → 宽度一致，见
        test_the_two_device_lists_share_one_square_grid / test_every_device_card_keeps_one_width_but_not_one_height），
        但三根音量条、两个滑杆、三个按钮**一个都不许顺手删** —— 那都是用户点名要的东西。
        """
        html = self._html()
        self.assertIn("grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:10px", html)
        self.assertIn("padding:8px 10px;transition", html, "卡片内边距要收紧")
        for keep in ('<span class="lbl">${inLabel}</span>', "<span class=\"lbl\">送</span>",
                     "<span class=\"lbl\">信号</span>", "gainRow('in', '我听它的')",
                     "gainRow('out', '我送它的')", "${acts}"):
            self.assertIn(keep, html, "收紧不等于删，这些都得留着：%s" % keep)

    def test_every_card_belongs_to_exactly_one_pane(self) -> None:
        """每个内容块都必须挂在一个菜单页上，否则它永远显示不出来（点了没反应那种故障）。"""
        import re

        html = self._html()
        cards = re.findall(r'<section class="card[^"]*"([^>]*)>', html)
        self.assertTrue(cards, "页面里应该有内容块")
        for attrs in cards:
            self.assertIn("data-pane=", attrs, "这个内容块没有归属的菜单页：%s" % attrs)
        panes = set(re.findall(r'data-pane="([a-z]+)"', html))
        for pane in panes:
            self.assertIn('body[data-pane="%s"] .card[data-pane="%s"]' % (pane, pane),
                          html, "菜单页 %s 的显示规则没写" % pane)


class TestNetworkSelfCheckAndBluetoothRow(unittest.TestCase):
    """图2（网络自检永远红叉）与图3（蓝牙那一行没有断开按钮）的回归。"""

    @staticmethod
    def _api():
        from audiomixing.webui import AudioMixingApi

        cfg = _config_stub("本机", "node-netcheck")
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        return AudioMixingApi(node)

    @staticmethod
    def _run(stderr_ok=True):
        """假的 PowerShell：按脚本内容给不同的输出。"""
        def fake(args, **kwargs):
            script = " ".join(str(a) for a in (args or []))
            if "Get-NetConnectionProfile" in script:
                return types.SimpleNamespace(stdout="以太网|Private\n")
            if "Get-NetFirewallProfile" in script:
                return types.SimpleNamespace(stdout="Domain=True\nPrivate=True\nPublic=True\n")
            if "Get-NetFirewallRule" in script:
                program = r"C:\Users\x\Desktop\Audio Mixing v9.9.exe" if stderr_ok else r"C:\old\gone.exe"
                return types.SimpleNamespace(stdout=(
                    "Audio Mixing TCP|%s|TCP|Allow|True|Private\n"
                    "Audio Mixing UDP|%s|UDP|Allow|True|Private\n" % (program, program)))
            if "Get-Process" in script:
                # 旧写法就是靠这一句找"正在跑的 exe"，进程名对不上时永远写「读不到」。
                return types.SimpleNamespace(stdout="")
            return types.SimpleNamespace(stdout="")
        return fake

    def test_rules_are_found_by_name_not_by_a_stale_program_filter(self) -> None:
        """改名成 Audio Mixing 之后，旧的 `Program -like '*avelink*'` 一条规则都匹配不到，
        于是网络自检永远红着 —— 用户点多少次放行都不变。"""
        api = self._api()
        with mock.patch("subprocess.run", side_effect=self._run()), \
                mock.patch("audiomixing.tray._real_exe",
                           return_value=r"C:\Users\x\Desktop\Audio Mixing v9.9.exe"):
            result = api.netcheck()
        checks = {c["name"]: c for c in result["checks"]}
        row = checks["入站规则（当前程序）"]
        self.assertTrue(row["ok"], "当前程序路径有 TCP/UDP 规则就必须判通过")
        self.assertTrue(row["meta"]["covered"]["tcp"])
        self.assertTrue(row["meta"]["covered"]["udp"])
        self.assertTrue(result["ok"], "这几种情况下不该再有任何红叉")

    def test_current_program_path_comes_from_the_running_exe(self) -> None:
        """「当前程序路径：读不到」的根因是拿 `Get-Process -Name AudioMixing` 去找
        「Audio Mixing v1.27.exe」—— 进程名带空格带版本号，一个都匹配不上。"""
        api = self._api()
        with mock.patch("subprocess.run", side_effect=self._run()), \
                mock.patch("audiomixing.tray._real_exe",
                           return_value=r"C:\Users\x\Desktop\Audio Mixing v9.9.exe"):
            checks = {c["name"]: c for c in api.netcheck()["checks"]}
        self.assertTrue(checks["当前程序路径"]["ok"])
        self.assertIn("Audio Mixing v9.9.exe", checks["当前程序路径"]["detail"])

    def test_a_rule_bound_to_another_copy_is_not_counted_as_current(self) -> None:
        """只有旧版本规则时，主行绝不能显示“通过”；要说清点一次放行防火墙。"""
        api = self._api()
        with mock.patch("subprocess.run", side_effect=self._run(stderr_ok=False)), \
                mock.patch("audiomixing.tray._real_exe",
                           return_value=r"C:\Users\x\Desktop\Audio Mixing v9.9.exe"):
            checks = {c["name"]: c for c in api.netcheck()["checks"]}
        row = checks["入站规则（当前程序）"]
        self.assertFalse(row["ok"], "旧版本规则不能让当前程序判通过")
        self.assertIn("当前程序未放行", row["summary"])
        self.assertEqual(row["meta"]["history"]["total"], 2)
        self.assertIn("放行防火墙", row["detail"])

    def test_many_near_identical_versions_are_summarized_not_dumped(self) -> None:
        """★ 2026-10-27 用户：机器上大量近似版本时，旧规则不能把详情撑爆。

        120 条旧版本规则 + 当前程序规则：主行必须是短摘要，
        历史规则折叠成计数和最多 5 条示例；当前程序仍判通过。
        """
        api = self._api()
        current = r"C:\Users\x\Desktop\Audio Mixing v9.9.exe"

        def fake(args, **kwargs):
            script = " ".join(str(a) for a in (args or []))
            if "Get-NetConnectionProfile" in script:
                return types.SimpleNamespace(stdout="以太网|Private\n")
            if "Get-NetFirewallProfile" in script:
                return types.SimpleNamespace(stdout="Domain=True\nPrivate=True\nPublic=True\n")
            if "Get-NetFirewallRule" in script:
                lines = [
                    "Audio Mixing TCP|%s|TCP|Allow|True|Private" % current,
                    "Audio Mixing UDP|%s|UDP|Allow|True|Private" % current,
                ]
                for i in range(120):
                    lines.append(
                        "Audio Mixing - LAN audio casting|E:\\backup\\v%d\\audiomixing.exe|TCP|Allow|True|Private" % i)
                return types.SimpleNamespace(stdout="\n".join(lines))
            return types.SimpleNamespace(stdout="")

        with mock.patch("subprocess.run", side_effect=fake), \
                mock.patch("audiomixing.tray._real_exe", return_value=current):
            checks = {c["name"]: c for c in api.netcheck()["checks"]}
        row = checks["入站规则（当前程序）"]
        self.assertTrue(row["ok"])
        self.assertLess(len(row["detail"]), 260, "主行详情必须是摘要，不能铺 120 条路径")
        history = row["meta"]["history"]
        self.assertEqual(history["total"], 120)
        self.assertEqual(history["unique_paths"], 120)
        self.assertEqual(len(history["examples"]), 5)
        self.assertTrue(history["truncated"])
        self.assertNotIn("v119", row["detail"], "具体路径只能出现在折叠数据里")

    def test_a_rule_for_another_profile_is_not_counted_as_covering_this_network(self) -> None:
        """规则只放行 Domain、当前网络是 Private 时，不能判通过。"""
        api = self._api()
        current = r"C:\Users\x\Desktop\Audio Mixing v9.9.exe"

        def fake(args, **kwargs):
            script = " ".join(str(a) for a in (args or []))
            if "Get-NetConnectionProfile" in script:
                return types.SimpleNamespace(stdout="以太网|Private\n")
            if "Get-NetFirewallProfile" in script:
                return types.SimpleNamespace(stdout="Domain=True\nPrivate=True\nPublic=True\n")
            if "Get-NetFirewallRule" in script:
                return types.SimpleNamespace(
                    stdout="Audio Mixing TCP|%s|TCP|Allow|True|Domain\n"
                           "Audio Mixing UDP|%s|UDP|Allow|True|Domain\n" % (current, current))
            return types.SimpleNamespace(stdout="")

        with mock.patch("subprocess.run", side_effect=fake), \
                mock.patch("audiomixing.tray._real_exe", return_value=current):
            checks = {c["name"]: c for c in api.netcheck()["checks"]}
        row = checks["入站规则（当前程序）"]
        self.assertFalse(row["ok"])
        self.assertIn("没有覆盖当前网络类型", row["summary"])

    def test_the_bluetooth_row_can_be_disconnected_from_the_monitor(self) -> None:
        """图3：蓝牙那一行只有状态、没有断开按钮，用户想断只能进系统蓝牙设置。"""
        from unittest import mock

        from audiomixing import bluetooth as bt

        api = self._api()
        device = {"id": "\\\\?\\BTHENUM#dev#SNK", "name": "手机A", "link": "connected"}
        with mock.patch.object(bt, "cached_devices", return_value=[device]):
            flows = api.monitor()["flows"]
        row = [f for f in flows if f.get("channel") == "蓝牙"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["disconnect"], "bluetooth", "这一行必须给得出断开按钮")
        self.assertEqual(row[0]["route_id"], device["id"], "断开要按设备 id，不然断的是别人")

    def test_a_configured_but_idle_point_to_point_row_can_be_disconnected(self) -> None:
        """★ 2026-10-18 用户（图1）：「点对点没有"断开"。你忘加了。」

        图1 里那两条「已配置 · 当前没有数据」原来一个按钮都没有。它们和"正在发送/正在接收"
        是同一种东西（都是点对点路由），断开的方式也必须一致 —— **删掉这条路由**（不是静音：
        只静音的话路由还在，"断开"点了跟没点一样）。
        """
        from unittest import mock

        from audiomixing import bluetooth as bt
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        node = AudioMixingNode(_config_stub("本机", "node-monitor-idle"),
                               audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.set_routes([{"id": "r-idle", "from": ["peer-1"], "to": ["node-monitor-idle"],
                          "gain": 1.0, "mute": False}], persist=False)
        api = AudioMixingApi(node)
        with mock.patch.object(bt, "cached_devices", return_value=[]), \
                mock.patch.object(node.receiver, "stats",
                                  return_value={"streams": [], "clocks": []}):
            flows = api.monitor()["flows"]
        row = [f for f in flows if f.get("route_id") == "r-idle"]
        self.assertEqual(len(row), 1, "已配置、当前没数据的那条路由要显示出来")
        self.assertEqual(row[0]["disconnect"], "route", "它必须给得出断开按钮")
        self.assertEqual(row[0]["disconnect_id"], "r-idle")
        self.assertIn("删掉这条路由", row[0]["hint"])

        # 点下去要真的把这条路由删掉（不是静音）
        code, result = api.monitor_disconnect("route", "r-idle")
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual([r for r in node.routes if r["id"] == "r-idle"], [],
                         "「断开」= 删掉这条路由")

    def test_every_sound_row_in_the_monitor_has_a_way_out(self) -> None:
        """★ 2026-10-17 用户（图1）：「我们的每一个声音都应当有断开选项（WiFi/蓝牙/广播）」。

        收进来的三路原来只有数字、没有出口：手机经 Wi-Fi 送进来的声音、别的电脑送来的
        点对点流、以及广播流。这一条把三路都摆上，逐个查它给出的 kind 对不对。
        """
        from unittest import mock

        from audiomixing import bluetooth as bt
        from audiomixing.audio.backends import netmic as netmic_backend
        from audiomixing.webui import AudioMixingApi

        node = AudioMixingNode(_config_stub("本机", "node-monitor-out"),
                               audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        api = AudioMixingApi(node)

        class _Mic(object):
            """假的网络音源：monitor() 只读 name/bytes_in，inputs_state() 还要 describe()。"""

            def __init__(self) -> None:
                self.name = "手机A"
                self.bytes_in = 4096
                self.muted = False

            def describe(self) -> dict:
                return {"name": self.name, "live": True, "muted": self.muted, "gain": 1.0,
                        "median_gap_ms": 20.0, "max_gap_ms": 30.0,
                        "gaps_over_100ms": 0, "underruns": 0}

        with mock.patch.object(netmic_backend, "active", return_value=[_Mic()]), \
                mock.patch.object(bt, "cached_devices", return_value=[]), \
                mock.patch.object(node.receiver, "stats", return_value={
                    "streams": [
                        {"node_name": "电脑B", "route_id": "route-abc", "bytes": 100},
                        {"node_name": "电脑C", "route_id": "channel", "bytes": 200},
                    ],
                    "clocks": [],
                }):
            flows = api.monitor()["flows"]

        wifi = [f for f in flows if f.get("channel") == "Wi-Fi"]
        self.assertEqual(len(wifi), 1)
        self.assertEqual(wifi[0]["disconnect"], "mic", "手机经 Wi-Fi 送来的声音要能断")
        self.assertEqual(wifi[0]["disconnect_id"], "手机A",
                         "断的是这台手机；id 单独放 disconnect_id，不借 route_id 搅乱去重逻辑")

        # ★ 2026-10-20 用户：「我手机用的点对点，这边监控怎么是点对点/广播」——
        #   监控页现在按**真实归属**分行标：命中的是具名路由 = 点对点；只落进频道那条 = 广播。
        recv = [f for f in flows if f.get("channel") in ("点对点", "广播")]
        self.assertEqual(len(recv), 2)
        named = [f for f in recv if f.get("channel") == "点对点"]
        chan = [f for f in recv if f.get("channel") == "广播"]
        self.assertEqual(len(named), 1, "具名路由那条要标成「点对点」")
        self.assertEqual(len(chan), 1, "落在频道（通配）那条要标成「广播」")
        self.assertEqual(named[0]["disconnect"], "route", "点对点那一行给 route")
        self.assertEqual(named[0]["disconnect_id"], "route-abc", "断的必须是本机那条路由")
        self.assertEqual(chan[0]["disconnect"], "listen", "广播那一行给 listen（关掉「听广播」）")

    def test_the_disconnect_api_takes_the_new_kinds(self) -> None:
        """mic / listen 两种新断开的落点必须真的生效 —— 不能只是页面上多了个按钮。"""
        from unittest import mock

        from audiomixing.audio.backends import netmic as netmic_backend

        api = self._api()
        node = api.node

        # listen：真的把「听广播」关掉
        node.config.listen = True
        code, result = api.monitor_disconnect("listen")
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertFalse(node.config.listen, "点了「停止收听广播」，开关就得真的落下去")

        # mic：真的把那一台网络音源静音，而且静的是它、不是别人
        calls = []

        def fake_mute(name, muted):        # noqa: N802
            calls.append((name, muted))
            return True

        with mock.patch.object(netmic_backend, "set_muted", side_effect=fake_mute):
            code, result = api.monitor_disconnect("mic", "手机A")
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("手机A", True)])

        # 设备已经不在了：目标其实已经达成，如实说清楚，但别报成 HTTP 错
        with mock.patch.object(netmic_backend, "set_muted", return_value=False):
            code, result = api.monitor_disconnect("mic", "手机X")
        self.assertEqual(code, 200)
        self.assertFalse(result["ok"])
        self.assertIn("没有在送声音", result["note"])

        # 不认识的 kind 仍然是 400：页面上写错一个词不能静默"成功"
        code, _result = api.monitor_disconnect("nonsense")
        self.assertEqual(code, 400)

    def test_the_monitor_page_uses_the_dedicated_disconnect_id(self) -> None:
        """页面的 data-id 要优先取 disconnect_id —— 否则 Wi-Fi 那行会拿手机名去断路由。"""
        from audiomixing.webui import monitor_html

        html = monitor_html()
        self.assertIn("f.disconnect_id || f.route_id", html)

class Test20261103GhostsAndStats(unittest.TestCase):
    """★ 2026-11-03 用户三张截图：

    * 图1：`2cdffa7c-30ee-45c…` / `M332BF-999c` 这类历史设备一直挂在线/不可达；
    * 图3："正在接收 / 累计 9.2MB" 但 "速率 0 KB/s"、缓冲 60ms 而目标 25ms；
    * 图2：路由写着"送在跑"而卡片写"没有在送"、收没有在收。

    这里钉住三条口径：发现表按 TTL 淘汰但"见过"要留着（不误删真设备路由）；
    路由只 stale 不删除；收发状态/速率必须来自真实流，而不是另一个布尔开关。
    """

    def _node(self):
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    def test_a_forgotten_peer_leaves_the_list_but_stays_ever_seen(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "old-phone", "name": "M332BF-999c",
                                  "audio_port": 47831, "web_port": 0}, "192.168.0.100")
            node.registry.get("old-phone").last_seen = time.time() - 90.0
            removed = node.registry.prune(60.0)
            self.assertIn("old-phone", removed, "90 秒没有音讯的历史设备要从界面列表里消失")
            self.assertIsNone(node.registry.resolve("old-phone"))
            self.assertTrue(node.registry.ever_seen("old-phone"),
                            "‘见过一次’必须留下，否则关机中的真设备会被当假设备删路由")
        finally:
            node.stop()

    def test_a_manual_peer_is_never_pruned(self) -> None:
        node = self._node()
        try:
            node.registry.add_manual("192.168.0.102", 47831, "手动设备")
            peer = node.registry.resolve("192.168.0.102")
            peer.last_seen = time.time() - 600.0
            removed = node.registry.prune(60.0)
            self.assertNotIn(peer.node_id, removed, "手动添加是用户明确要求，TTL 不能删它")
        finally:
            node.stop()

    def test_a_route_to_a_vanished_peer_is_flagged_stale_not_deleted(self) -> None:
        node = self._node()
        try:
            node.set_routes([{"id": "r-old", "from": ["2cdffa7c-old"], "to": ["self"]}],
                            persist=False)
            node._refresh_route_staleness()
            self.assertFalse(node.status()["routes"][0]["stale"],
                             "刚发现找不到时先观察，不能立刻判死")
            node._unknown_route_since["r-old"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            self.assertTrue(node.status()["routes"][0]["stale"],
                            "持续找不到 60 秒后，界面不再画这个幽灵节点")
            self.assertEqual(len(node.routes), 1, "只是标记，不能删用户路由")
        finally:
            node.stop()

    def test_the_tick_loop_really_prunes_but_never_stales_a_real_device(self) -> None:
        """★ 2026-11-04（第 1 条回退）：tick 仍要真淘汰离线设备，但**不能**把见过面的真设备
        当"不存在"。这正是用户现场"虚线节点反而变多"的根因：以前只看"当前不在线"。

        tick 每半秒跑一次，是唯一稳定执行的地方；这条把"90s 淘汰 + 只有真 ghost 才 stale"
        钉在真正的调用链上（不是只测函数本身）。
        """
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "old-phone", "name": "M332BF-999c",
                                  "audio_port": 47831, "web_port": 0}, "192.168.0.100")
            node.registry.get("old-phone").last_seen = time.time() - 120.0
            node.set_routes([{"id": "r-ghost", "from": ["old-phone"], "to": ["self"]}],
                            persist=False)
            node._last_peer_prune = time.monotonic() - 30.0
            node._unknown_route_since["r-ghost"] = time.monotonic() - 90.0
            with mock.patch.object(node, "refresh_devices", lambda: None), \
                    mock.patch.object(node, "_apply_pushed_routes", lambda: None), \
                    mock.patch.object(node, "apply_routes", lambda: None), \
                    mock.patch.object(node, "_apply_multi_out", lambda: None), \
                    mock.patch.object(node, "_sweep_dead_senders", lambda: None), \
                    mock.patch.object(node, "_sweep_orphan_routes", lambda: None), \
                    mock.patch.object(node, "_sweep_unwanted_auto_routes", lambda: None):
                node.tick()
            self.assertNotIn("old-phone", [p["node_id"] for p in node.registry.snapshot()],
                             "tick 必须真的把 90 秒没音讯的设备从界面列表拿掉")
            item = node.status()["routes"][0]
            self.assertFalse(item["stale"],
                             "见过面的真设备只是关机/换网 → 实线（offline），不能当幽灵清掉")
            self.assertEqual(item["state"], "offline")
            self.assertEqual(len(node.routes), 1, "路由仍然保留（用户配置不能丢）")

            # 换成"本进程从没见过的 id"：这才是脏配置/旧 id，连续 60 秒解析不到必须标 missing。
            node.set_routes([{"id": "r-ghost", "from": ["b1fbc20c-b560-48"], "to": ["self"]}],
                            persist=False)
            node._unknown_route_since["r-ghost"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            ghost = node.status()["routes"][0]
            self.assertTrue(ghost["stale"], "真 ghost（从未见过）60 秒后才画虚线")
            self.assertEqual(ghost["state"], "missing")
        finally:
            node.stop()

    def test_the_frontend_draws_only_true_missing_as_dashed(self) -> None:
        """第 1/14 条（第四轮修正）：身份确实不存在的路由**根本不上图**（不再有幽灵卡片），
        状态只影响线型/透明度、不再换颜色；自环 u===v 不画。"""
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("if(r.stale) return;", html,
                         "旧的一刀切跳过会把 no_data 路由也藏掉")
        self.assertIn("if(String(r.state || '') === 'missing' || r.stale) return;", html,
                      "真 missing 路由只留在路由表，不上关系图")
        self.assertIn("if(l.state === 'missing') return cls + ' missing';", html,
                      "兜底分支保留（万一还有漏画的 missing 边）")
        self.assertIn("if(l.state === 'no_data' || !l.live)", html)
        self.assertIn("if(u === v) return;", html, "同一台设备（WiFi/蓝牙）不能画自环")

    def test_late_packet_wording_does_not_imply_dropped_audio(self) -> None:
        """★ 2026-11-03（Q2）：代码里 `late` 只计数、样本照播。

        「迟到」文案会被用户读成"丢音"，和「溢出」一起看就像同一次会话里自相矛盾。
        统一改成「晚到包」，并在诊断里写明不代表丢音。目标值**不是**下限：
        用户现场 116ms < 「最低 150ms」就是反例，文案必须是「目标（不是下限）」。
        """
        src = (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8")
        self.assertIn("晚到包", src)
        self.assertNotIn("迟到=", src, "不允许再出现会误导成丢音的旧文案")
        self.assertIn("不代表丢音", src, "必须写明晚到包不是丢音")
        # ★ 2026-11-04：现场 116ms < 「最低 150ms」证明 target 不是下限。文案改为
        #   「目标（不是下限）」，实际值允许短时低于它。
        self.assertIn("目标 %s 毫秒（不是下限）", src)
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("目标 ${x.target_ms}ms", html)
        self.assertNotIn("最低 ${x.target_ms}ms", html, "不能再把目标写成下限")



    def test_receiver_reports_a_live_rate_instead_of_always_zero(self) -> None:
        from audiomixing.net.rtpaudio import Receiver, StreamInfo

        recv = Receiver("self", 0)
        info = StreamInfo(ssrc=7, node_id="phone", bytes=1000, packets=10)
        recv.streams[7] = info
        first = recv.stats()["streams"][0]
        self.assertEqual(first["kbps"], 0.0, "第一次没有窗口，先给 0")
        time.sleep(0.25)
        info.bytes = 3000
        second = recv.stats()["streams"][0]
        self.assertGreater(second["kbps"], 1.0,
                           "累计字节涨了 2000，速率不能再是 0（图3 的根因）")

    def test_the_listening_gain_is_attenuated_by_the_phone_volume_only(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([{"id": "r-listen", "from": ["peer-1"], "to": ["self"],
                              "gain": 0.8, "mute": False}], persist=False)
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-1", "name": "手机", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(), "vol": 0.5})
            node._handle_peer_requests()
            self.assertAlmostEqual(node._peer_media_volume(node.routes[0]), 0.5)
            self.assertAlmostEqual(node.routes[0]["gain"], 0.8,
                                   "路由上的『我听它的』不能被手机音量改写")
            # ★ 2026-11-04（Q2）：验收必须能从 /api/status 直接看到手机报的音量。
            peer = {p["node_id"]: p for p in node.status()["peers"]}["peer-1"]
            self.assertEqual(peer["media_vol"], 0.5)
            self.assertIsNotNone(peer["media_vol_age_s"])
            # v2.26 起 vol 会存进多个身份键（node_id / device_id / alias / host）。
            # "不再上报"必须让**所有**键一起过期，否则 host 上的新值会继续生效。
            node._peer_volume.clear()
            self.assertAlmostEqual(node._peer_media_volume(node.routes[0]), 1.0,
                                   msg="手机不再报音量（老版本）时行为必须回到 1.0")
            peer = {p["node_id"]: p for p in node.status()["peers"]}["peer-1"]
            self.assertIsNone(peer["media_vol"], "过期后状态里必须显示未上报，而不是旧值")
        finally:
            node.stop()

    def test_phone_volume_scales_the_final_sink_gain(self) -> None:
        """★ 2026-11-04（Q1）：钉死「最终播放端增益 = 路由增益 × 手机媒体音量」。

        用户现场看到的 `sinks[0].gain=0.49` 恰好等于路由增益 0.49，只有两种可能：
        ① 手机上跑的版本没报 vol；② 报了但乘算没发生（键对不上/走了通配路由）。
        这条测试钉住②：只要 vol 到了电脑，`_apply_sink_gain` 必须乘进去。
        """
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([{"id": "r-listen", "from": ["peer-1"], "to": [node.config.node_id],
                              "gain": 0.8, "mute": False}], persist=False)
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-1", "name": "手机", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(), "vol": 0.5})
            node._handle_peer_requests()
            rule = next(r for r in node.routes if r["id"] == "r-listen")
            sink = SinkEngine(route_id="r-listen", device_selector="d")
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.4, msg="0.8 × 0.5 必须等于 0.4")
            self.assertEqual(sink.route_gain, 0.8)
            self.assertEqual(sink.media_vol, 0.5)
            self.assertEqual(sink.media_vol_source, "peer-1",
                             "诊断要能说出 vol 是从哪个键查到的")
            self.assertAlmostEqual(sink.describe()["gain"], 0.4,
                                   msg="面板/诊断读的就是 describe()，这里也必须是 0.4")
            # 同样：所有身份键一起清掉才算真正的"过期"，只陈旧 node_id 不够。
            node._peer_volume.clear()
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.8)
            self.assertEqual(sink.media_vol, 1.0)
        finally:
            node.stop()

    def test_unaccepted_point_to_point_is_not_attached_to_the_channel(self) -> None:
        """★ 2026-11-06（用户第 1 条）：勾「只听」不等于放行点对点。

        v2.28 为了修"只听下彻底没声"把准入改成一律放行，结果没开「收听」的手机
        也能把点对点流灌进频道通配路由 —— 声音能听到，但走的是给广播准备的混音
        路径，真机表现是"很卡"。现在恢复文档铁律（BUG-20261104-04）：
          * 「只听」只收**频道广播**；
          * 点对点必须在这台设备卡片上开「收听」（具名路由）才收；
          * 广播流仍然照常落进频道通配。
        """
        node = self._node()
        try:
            node.config.broadcast = False
            node.config.listen = True
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([], persist=False)
            self.assertEqual(node.routes_for_sender("peer-1"), {"channel"},
                             "还没声明点对点时，频道收听本来就该收它")
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-1", "name": "手机", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                 "send_mode": 1, "broadcast": False, "sending": True,
                 "want_listen": True})
            node._handle_peer_requests()
            self.assertEqual(node._peer_send_mode_for("peer-1"), (1, False))
            self.assertEqual(node.routes_for_sender("peer-1"), set(),
                             "★ 第 1 条：只听 ≠ 放行点对点，没开「收听」就一个播放端都不挂")
            self.assertFalse(node._stream_is_accepted("peer-1"),
                             "★ 第 1 条：接收准入必须把未接受的点对点挡在流表之外")
            node.config.listen = False
            self.assertEqual(node.routes_for_sender("peer-1"), set(),
                             "没开「只听」且没接受 → 一个播放端都不挂")
            self.assertFalse(node._stream_is_accepted("peer-1"))
            node.config.listen = True
            node.set_routes([{"id": "named", "from": ["peer-1"],
                              "to": [node.config.node_id]}], persist=False)
            self.assertEqual(node.routes_for_sender("peer-1"), {"named"},
                             "接受之后要立刻挂上具名路由（不用等重启）")
            self.assertTrue(node._stream_is_accepted("peer-1"))
        finally:
            node.stop()

    def test_a_broadcast_from_a_known_sender_still_reaches_the_channel(self) -> None:
        """★ 2026-11-04（Q2）：修准入不能把正常的广播→只听一起弄死。

        广播那条线路不报 want_listen（手机端「广播是和路由独立开的」），所以同一台
        已登记的手机在广播时仍必须落进频道那条。
        """
        node = self._node()
        try:
            node.config.broadcast = False
            node.config.listen = True
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([], persist=False)
            # ★ 2026-11-04（Q1 回退）：广播时手机可能因为残留的 sendIds 同时报
            #   want_listen=true —— 这一条正是回退的触发链，必须照样能落进频道。
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-1", "name": "手机", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                 "send_mode": 1, "broadcast": True, "sending": True,
                 "want_listen": True})
            node._handle_peer_requests()
            self.assertEqual(node._peer_send_mode_for("peer-1"), (1, True))
            self.assertFalse(node._peer_sends_point_to_point("peer-1"),
                             "显式 broadcast=true 永远不算点对点，哪怕 want_listen 误报")
            self.assertEqual(node.routes_for_sender("peer-1"), {"channel"})
            self.assertTrue(node._stream_is_accepted("peer-1"))
        finally:
            node.stop()

    def test_a_legacy_phone_that_never_reports_its_mode_is_still_heard(self) -> None:
        """★ 2026-11-04（Q1 回退）：2.10 及以前没有 send_mode / broadcast。

        退化行为必须是"能听"：不许因为字段缺失就把它当成点对点拒掉
（历史上广播一直是能听的，回退比漏音更不能接受）。
        """
        node = self._node()
        try:
            node.config.broadcast = False
            node.config.listen = True
            node.registry.update({"wl": 1, "id": "peer-old", "name": "老手机",
                                  "audio_port": 47831, "web_port": 0}, "192.168.0.102")
            node.set_routes([], persist=False)
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-old", "name": "老手机", "_type": "STATS",
                 "_from": "192.168.0.102:47831", "_at": time.monotonic(),
                 "want_listen": True})
            node._handle_peer_requests()
            self.assertIsNone(node._peer_send_mode_for("peer-old"),
                              "老版本没报模式，就不许凭空造一个")
            self.assertFalse(node._peer_sends_point_to_point("peer-old"))
            self.assertEqual(node.routes_for_sender("peer-old"), {"channel"})
            self.assertTrue(node._stream_is_accepted("peer-old"))
        finally:
            node.stop()

    def test_broadcast_listen_receives_real_audio_packets_end_to_end(self) -> None:
        """★ 2026-11-04（Q1）：端到端走一遍接收端准入 → 真的收下音频包。

        对照：同一条 stats 改成点对点（broadcast=false）且本机没接受 → 同一个
        接收端必须一个包都不收（`packets_denied` 增加）。
        """
        from audiomixing import proto

        def stats(node, cid, broadcast):
            node.receiver.control_messages.append(
                {"t": "stats", "node": cid, "name": "手机", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                 "send_mode": 1, "broadcast": broadcast, "sending": True,
                 "want_listen": True})
            node._handle_peer_requests()

        node = self._node()
        try:
            node.config.broadcast = False
            node.config.listen = True
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([], persist=False)
            stats(node, "peer-1", True)
            pkt = proto.Packet(type=proto.T_AUDIO, payload=b"\x01\x02" * 480,
                               codec=proto.CODEC_PCM_S16, seq=1, ssrc=0x5151,
                               samples=480, ts_ns=proto.now_ns())
            self.assertTrue(node.receiver._admitted(pkt, "198.51.100.10"),
                            "广播流必须过准入闸门")
            node.receiver._handle_audio(pkt, ("198.51.100.10", 47831))
            found = node.receiver.stats()["streams"]
            self.assertEqual(len(found), 1, "广播必须真的进流表")
            self.assertEqual(found[0]["packets"], 1)
        finally:
            node.stop()

        node2 = self._node()
        try:
            node2.config.broadcast = False
            node2.config.listen = True
            node2.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                   "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node2.set_routes([], persist=False)
            stats(node2, "peer-1", False)
            # ★ 2026-11-06（第 1 条）：只听只收广播，未接受的点对点必须被拦住。
            self.assertFalse(node2.receiver._admitted(pkt, "198.51.100.10"),
                            "★ 第 1 条：只听 ≠ 放行点对点，准入必须拒绝")
            self.assertEqual(node2.routes_for_sender("peer-1"), set(),
                            "★ 第 1 条：未接受的点对点一个播放端都不挂")
        finally:
            node2.stop()

    def test_phone_volume_is_not_multiplied_twice_when_the_capture_is_already_attenuated(
            self) -> None:
        """★ 2026-11-04（Q2）：手机采集点在音量**之后**（用户实测结论）。

        vol 1.0→0.5、收到峰值 0.80→0.40 同比例降 → 判定"采集端已衰减" → PC 不再乘 vol。
        最终 gain 必须等于路由增益本身，否则手机音量会被平方（调小一半变四分之一）。
        """
        from types import SimpleNamespace

        from audiomixing.net.rtpaudio import JitterBuffer, StreamInfo

        node = self._node()
        try:
            peer = "peer-1"
            node.registry.update({"wl": 1, "id": peer, "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node._peer_volume[peer] = (0.5, time.monotonic())
            node.receiver.streams[4242] = StreamInfo(ssrc=4242, node_id=peer)
            buf = JitterBuffer(name="q2")
            node.receiver.buffers[4242] = buf
            base = time.monotonic() - 60.0
            buf.peak = 0.8
            node._observe_peer_volume(peer, 1.0, base)
            buf.peak = 0.4
            node._observe_peer_volume(peer, 0.5, base + 12.0)
            self.assertTrue(node._peer_vol_verdict.get(peer),
                            "峰值随 vol 同比例变化 → 采集端已衰减")
            rule = {"gain": 0.8, "from": [peer], "to": [node.config.node_id]}
            sink = SimpleNamespace()
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.8, msg="判定已衰减时不许再乘 vol")
            self.assertAlmostEqual(sink.media_vol, 0.5, msg="上报值仍要显示出来")
            self.assertAlmostEqual(sink.volume_factor, 1.0)
            self.assertIn("不叠加", sink.volume_note)
            row = next(p for p in node._peers_with_media_volume() if p["node_id"] == peer)
            self.assertIn("vol=0.50", row["volume_note"], "诊断必须一眼看到 vol")
            self.assertIn("收到峰值=0.40", row["volume_note"])
            self.assertIn("不叠加", row["volume_note"])
            # 手动强制开：回到旧行为 0.8 × 0.5 = 0.40（对照，证明开关真的在起作用）
            node.config.phone_volume_mode = "on"
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.4)
            # 手动强制关：即使判定是"需要乘"也只乘路由增益
            node.config.phone_volume_mode = "off"
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.8)
        finally:
            node.stop()

    def test_phone_volume_is_still_applied_when_the_capture_precedes_the_volume(self) -> None:
        """★ 2026-11-04（Q2）：峰值**不随** vol 变 → 采集在音量之前 → 仍要乘一次。"""
        from types import SimpleNamespace

        from audiomixing.net.rtpaudio import JitterBuffer, StreamInfo

        node = self._node()
        try:
            peer = "peer-2"
            node._peer_volume[peer] = (0.5, time.monotonic())
            node.receiver.streams[4243] = StreamInfo(ssrc=4243, node_id=peer)
            buf = JitterBuffer(name="q2b")
            node.receiver.buffers[4243] = buf
            base = time.monotonic() - 60.0
            buf.peak = 0.8
            node._observe_peer_volume(peer, 1.0, base)
            buf.peak = 0.8
            node._observe_peer_volume(peer, 0.5, base + 12.0)
            self.assertFalse(node._peer_vol_verdict.get(peer),
                             "峰值没变 → 采集在音量之前，需要 PC 补上衰减")
            rule = {"gain": 0.8, "from": [peer], "to": [node.config.node_id]}
            sink = SimpleNamespace()
            node._apply_sink_gain(sink, rule)
            self.assertAlmostEqual(sink.gain, 0.4, msg="0.8 × 0.5")
            self.assertAlmostEqual(sink.volume_factor, 0.5)
            self.assertIn("叠加", sink.volume_note)
        finally:
            node.stop()

    def test_volume_decision_defaults_to_the_old_behaviour_without_samples(self) -> None:
        """★ 2026-11-04（Q2）：没有样本（老手机 / 还没调过音量）时按旧行为叠加，
        不许因为"判不出来"就把音量丢掉。"""
        node = self._node()
        try:
            node._peer_volume["peer-3"] = (0.5, time.monotonic())
            factor, note = node._volume_factor_for(
                0.5, node._volume_verdict_for(["peer-3"]))
            self.assertAlmostEqual(factor, 0.5)
            self.assertIn("样本不足", note)
        finally:
            node.stop()

    def test_the_receiver_drops_a_denied_source_before_it_enters_the_stats(self) -> None:
        """★ 2026-11-04（Q2）：准入在**计数之前**生效 —— 被拒绝的流不能出现在监控页。"""
        from types import SimpleNamespace

        from audiomixing import proto
        from audiomixing.net.rtpaudio import Receiver

        recv = Receiver("self", 0, accept_source=lambda nid: False,
                        resolve_source=lambda host: "peer-1")
        try:
            self.assertFalse(recv._admitted(SimpleNamespace(type=proto.T_AUDIO), "198.51.100.10"))
            self.assertTrue(recv._admitted(SimpleNamespace(type=proto.T_PING), "198.51.100.10"),
                            "控制包不受音频准入影响（时钟/统计还要能走）")
            self.assertIn("packets_denied", recv.stats())
        finally:
            recv.stop()
        recv2 = Receiver("self", 0, accept_source=lambda nid: False,
                         resolve_source=lambda host: "")
        try:
            self.assertTrue(recv2._admitted(SimpleNamespace(type=proto.T_AUDIO), "10.0.0.9"),
                            "认不出来源的流照旧放行（老行为：手动 IP / 信标丢了）")
        finally:
            recv2.stop()

    def test_a_synthetic_test_node_id_is_migrated_without_losing_routes(self) -> None:
        from audiomixing.config import Config, default_node_id

        path = _mkdtemp(prefix="am-cfg-mig-") / "config.json"
        path.write_text(json.dumps({
            "node_id": "self-node",
            "name": "本机",
            "routes": [{"id": "r1", "from": ["2cdffa7c"], "to": ["self-node"],
                        "origin": "self-node", "gain": 1.0}],
            "smart_link": ["peer-1", "real-phone"],
        }), encoding="utf-8")
        cfg = Config.load(path, create=True)
        self.assertNotEqual(cfg.node_id, "self-node", "测试身份必须换成真 UUID")
        self.assertEqual(len(cfg.node_id.split("-")), 5, "换成的是标准 uuid4")
        self.assertEqual(cfg.routes[0]["to"], [cfg.node_id], "指向旧身份的端点要原地改写")
        self.assertEqual(cfg.routes[0]["origin"], cfg.node_id)
        self.assertEqual(cfg.smart_link, ["real-phone"], "测试留下的 peer-1 不能继续自动连")
        self.assertEqual(len(cfg.routes), 1, "不能顺手删掉用户路由")
        # ★ 2026-11-03：迁移前必须留下原件，出问题能人工回退。
        backups = sorted(path.parent.glob(path.name + ".bak-*"))
        self.assertEqual(len(backups), 1, "迁移前应恰好生成一份 config.json.bak-<时间戳>")
        original = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(original["node_id"], "self-node", "备份必须是迁移前的原样内容")
        self.assertEqual(original["smart_link"], ["peer-1", "real-phone"])
        self.assertEqual(original["routes"][0]["to"], ["self-node"])


class Test20261104ClockAndJitter(unittest.TestCase):
    """★ 2026-11-04（Q1）：把"晚到包 595675 / 5960 秒 ≈ 100%"这个假数字钉死在测试里。

    诊断包证据：`clocks` 里 node_id 那条 samples=0；IP 那条 samples=8752、
    offset=-1173.839ms。音频流按 node_id 查 → offset=0 → 所有包都被判晚到。
    """

    def test_a_stream_uses_the_clock_learned_under_the_ip_address(self) -> None:
        from audiomixing.net.rtpaudio import Receiver, StreamInfo

        recv = Receiver("self", 0)
        cs = proto.ClockSync("192.168.0.100")
        cs.samples = 1
        cs.offset_ns = -1_173_839_000
        recv._clock["192.168.0.100"] = cs
        node_id = "605a4964-0000-0000-0000-000000000000"
        got = recv._clock_for(node_id, ("192.168.0.100", 47971))
        self.assertIs(got, cs, "node_id 名下没有样本时必须回退到 IP 上已同步的时钟")
        self.assertTrue(got.ready)
        self.assertAlmostEqual(got.offset_ns / 1e6, -1173.839, places=1)
        self.assertIs(recv._clock[node_id], cs, "回退成功后要登记到 node_id，stats 才能显示真实偏移")
        recv.streams[7] = StreamInfo(ssrc=7, node_id=node_id)
        stream = recv.stats()["streams"][0]
        self.assertTrue(stream["clock_ready"])
        self.assertAlmostEqual(stream["clock_ms"], -1173.8, places=1)
        self.assertEqual(stream["clock_samples"], 1)

    def test_unsynced_packets_are_not_counted_as_late(self) -> None:
        from audiomixing.net.rtpaudio import JitterBuffer

        buf = JitterBuffer()
        buf._have_timeline = True
        buf._primed = True
        buf._play_at = proto.now_ns()
        samples = make_tone(1000.0, BLOCK, 0.3, CHANNELS)
        # offset=0 时包时间戳不在本机时间轴上（手机墙钟比电脑慢 1.17 秒）。
        buf.write(samples, proto.now_ns() - 1_200_000_000, 0.0)
        self.assertEqual(buf.dropped_late, 1, "时钟已同步时该计就计（不变）")
        buf.dropped_late = 0
        buf.write(samples, proto.now_ns() - 1_200_000_000, 0.0, count_late=False)
        self.assertEqual(buf.dropped_late, 0, "时钟未同步时不允许把包算成晚到")

    def test_a_few_underruns_do_not_pin_the_target_at_the_max(self) -> None:
        from audiomixing.net.rtpaudio import JitterBuffer, _ms_to_frames

        buf = JitterBuffer()
        buf.target_frames = _ms_to_frames(55.0)
        now = proto.now_ns()
        buf._last_adjust_ns = now - 5_000_000_000
        buf.samples_written = 96_000
        buf._last_written = 0
        buf.underruns = 1
        buf._last_underruns = 0
        buf._reads = 200
        buf._last_reads = 0
        before = buf.target_frames
        buf._adapt(now)
        self.assertEqual(buf.target_frames, before,
                         "现场 0.5% 的欠载率不该把目标一路推到 150ms 上限")
        # 高欠载率（15%）才允许快速上调。
        buf.underruns = 31
        buf._last_underruns = 1
        buf._reads = 400
        buf._last_reads = 200
        buf._last_adjust_ns = now - 1_200_000_000
        buf._adapt(now)
        self.assertGreater(buf.target_frames, before, "欠载 >5% 必须一次涨 10ms")

    def test_the_target_shrinks_only_after_five_clean_seconds(self) -> None:
        from audiomixing.net.rtpaudio import JitterBuffer, _ms_to_frames

        buf = JitterBuffer()
        buf.target_frames = _ms_to_frames(60.0)
        now = proto.now_ns()
        buf.samples_written = 0
        for i in range(5):
            buf._last_adjust_ns = now - (6 - i) * 1_000_000_000
            buf._adapt(now)
        before = buf.target_frames
        buf._last_adjust_ns = now - 1_200_000_000
        buf._adapt(now)
        self.assertLess(buf.target_frames, before,
                         "连续 5 秒无欠载/晚到/溢出后才允许每秒收 2%（收要慢）")

    # ---- ★ 2026-11-04（审计 A2）：对端校时后 offset 必须跟得上 ----
    def test_a_peer_clock_step_is_followed_after_the_best_rtt_is_settled(self) -> None:
        """改前：offset 只在 `rtt <= 历史最小 RTT` 的样本上更新。

        `_best_rtt` 只减不增，采集几轮之后后续样本几乎永不命中 —— 对端墙钟被
        NTP 校正（或用户改时间）时 offset 永远停在旧值，播放头整体错位，
        表现为持续溢出/整段延迟直到重启。改后：250ms 以上的跳变两次 PONG 内重锚。
        """
        sync = proto.ClockSync("remote")

        def feed(offset_ns: float, rtt_us: int) -> None:
            t0 = proto.now_ns()
            ping = proto.Packet.control(proto.T_PING, proto.ping_body(t0))
            half = rtt_us * 1000 // 2
            t1 = t0 + offset_ns + half
            t2 = t1 + 300_000
            pong = proto.Packet.control(proto.T_PONG, proto.pong_body(ping, t1, t2))
            t3 = t2 - offset_ns + half
            sync.update(pong, t3)

        feed(0.0, 4000)
        self.assertAlmostEqual(sync.offset_ns, 0.0, delta=1_000_000)
        # 比历史最小值略大的正常样本：改前直接丢弃，改后允许小幅修正（窗口内）。
        feed(0.0, 5000)
        self.assertAlmostEqual(sync.offset_ns, 0.0, delta=3_000_000)
        # 对端校时：墙钟跳了 +500ms。
        feed(500_000_000.0, 5000)
        self.assertGreater(sync.offset_ns, 10_000_000,
                           "校时后的第一个样本就必须开始把 offset 拉过去（改前恒为 0）")
        feed(500_000_000.0, 5000)
        self.assertAlmostEqual(sync.offset_ns, 500_000_000.0, delta=5_000_000,
                               msg="两次 PONG 内必须重锚到新的墙钟（改前永远到不了）")

    # ---- ★ 2026-11-04（审计 A3）：乱序/重复包不许污染计数、不许重复入缓冲 ----
    def test_reordered_and_duplicate_packets_do_not_corrupt_the_counters(self) -> None:
        """改前：乱序旧包会把 `last_seq` 退回去，下一包被算成丢了几十个；
        重复包（同一 seq）照样写进抖动缓冲，10ms 音频被播第二遍。
        """
        from audiomixing.net.rtpaudio import Receiver

        recv = Receiver("self", 0)
        payload = b"\x00" * (480 * 2 * 2)   # 10ms 立体声 s16

        def pkt(seq: int) -> proto.Packet:
            return proto.Packet(type=proto.T_AUDIO, payload=payload,
                                codec=proto.CODEC_PCM_S16, seq=seq, ssrc=0x1234,
                                samples=480, ts_ns=proto.now_ns())

        for seq in (1, 2, 3):
            recv._handle_audio(pkt(seq), ("127.0.0.1", 5000))
        info = recv.streams[0x1234]
        self.assertEqual(info.lost, 0)
        self.assertEqual(info.last_seq, 3)
        recv._handle_audio(pkt(1), ("127.0.0.1", 5000))   # 乱序旧包
        recv._handle_audio(pkt(4), ("127.0.0.1", 5000))
        self.assertEqual(info.lost, 0, "一个乱序旧包不许把丢包数撑大")
        self.assertEqual(info.last_seq, 4, "旧包不许把游标退回去")
        before = recv.buffers[0x1234].samples_written
        recv._handle_audio(pkt(4), ("127.0.0.1", 5000))   # 重复包
        self.assertEqual(info.duplicates, 1)
        self.assertEqual(recv.buffers[0x1234].samples_written, before,
                         "重复包不许再写进抖动缓冲（否则同一段音频播两遍）")
        self.assertEqual(info.lost, 0, "重复包不算丢包")

    # ---- ★ 2026-11-04（审计 B2，第 3 轮）：同一 ssrc 换编码必须换解码器 ----
    def test_a_stream_that_switches_codec_rebuilds_its_decoder(self) -> None:
        """改前 `_decoders[ssrc]` 只在首包建一次。

        发送端用同一条路由重启并换了编码时 ssrc 不变（`stable_ssrc` 由 node_id|route_id
        派生），接收端却继续用旧解码器解新负载 —— 整条流噪声/无声，诊断里还显示旧编码。
        """
        from audiomixing.net.rtpaudio import Receiver

        recv = Receiver("self", 0)
        ssrc = 0x4321
        pcm_f32 = proto.Packet(
            type=proto.T_AUDIO, payload=b"\x00" * (480 * 2 * 4),
            codec=proto.CODEC_PCM_F32, seq=1, ssrc=ssrc, samples=480,
            ts_ns=proto.now_ns())
        recv._handle_audio(pcm_f32, ("127.0.0.1", 5000))
        info = recv.streams[ssrc]
        self.assertEqual(info.codec, proto.CODEC_PCM_F32)
        before = recv.buffers[ssrc].samples_written

        pcm_s16 = proto.Packet(
            type=proto.T_AUDIO, payload=b"\x00" * (480 * 2 * 2),
            codec=proto.CODEC_PCM_S16, seq=2, ssrc=ssrc, samples=480,
            ts_ns=proto.now_ns())
        recv._handle_audio(pcm_s16, ("127.0.0.1", 5000))
        self.assertEqual(info.codec, proto.CODEC_PCM_S16,
                         "编码变了，流的编码字段必须跟着变")
        self.assertEqual(recv.buffers[ssrc].samples_written - before, 480,
                         "必须换成 s16 解码器（旧 f32 解码器只会解出 120 帧 = 240 字节）")


class TestApkReleaseGate(unittest.TestCase):
    """★ 2026-11-03（Q3）：交付门槛必须自己长牙，不能靠人记得升版本号。

    真机事故的形态：源码写着 2.10、交付目录里还是 2.9 的包，脚本却报 PASS。
    """

    def _mod(self):
        import importlib.util

        path = ROOT / "tools" / "check_apk.py"
        spec = importlib.util.spec_from_file_location("check_apk_for_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_debug_apk_is_the_current_version_and_has_the_new_symbols(self) -> None:
        mod = self._mod()
        apk = mod.DEFAULT
        if not apk.is_file():
            self.skipTest("本机没有构建产物，跳过（打包机才会跑）")
        self.assertTrue(mod._manifest_has_version(apk, "2.20"), "交付版本必须是 2.20")
        self.assertFalse(mod._manifest_has_version(apk, "2.16"), "2.16 的旧包必须被拦下")
        self.assertFalse(mod._manifest_has_version(apk, "2.12"), "2.12 的包必须被拦下")
        self.assertFalse(mod._manifest_has_version(apk, "2.14"), "2.14 的旧包必须被拦下")
        import zipfile

        blob = b"".join(zipfile.ZipFile(apk).read(n)
                        for n in zipfile.ZipFile(apk).namelist() if n.endswith(".dex"))
        self.assertIn(b"mediaVolumeRatio", blob, "手机音量联动符号必须在 dex 里")
        self.assertIn(b"send_mode", blob, "送声音档位字段必须在 dex 里（Q1 准入判据靠它）")
        self.assertIn(b"originId", blob, "防自环扩展段字段必须在 dex 里（B 身份/防环）")
        self.assertIn(b"diagJson", blob, "2.17+：增益乘数/前后电平的取证字段必须在 dex 里")
        self.assertIn(b"bt_name", blob, "手机自报蓝牙身份字段必须在 dex 里（第 9 条合并证据）")
        self.assertIn(b"vol_in_capture", blob, "v2.27 的采集点声明必须在 dex 里（Bug-1）")
        self.assertIn(b"silent_vol_policy", blob, "2.20+：静音档乘数策略必须随统计包上报")
        self.assertIn(b"vol_policy", blob, "2.20+：电脑端读取静音档策略的字段必须在 dex 里")

    def test_delivery_copies_have_the_same_hash_as_the_installer_payload(self) -> None:
        mod = self._mod()
        if not mod.PAYLOAD.is_file():
            self.skipTest("本机没有 installer payload，跳过")
        want = mod._sha256(mod.PAYLOAD)
        for copy in mod.DELIVERY_COPIES:
            if not copy.is_file():
                self.skipTest("本机没有 %s，跳过" % copy)
            self.assertEqual(mod._sha256(copy), want,
                             "六处副本必须是同一颗 APK：%s" % copy)


class TestAuditFindsSyntheticIdentities(unittest.TestCase):
    """★ 2026-11-03（Q1）：审计工具必须把测试身份报出来，但不能替用户删。

    以前它只扫 manual_peers 与路由 id 前缀，`node_id="self-node"`、
    `smart_link=["peer-1"]` 明明在用户配置里，却打印 `[ok] no test peers/routes`。
    """

    def _identity_findings(self, cfg):
        import importlib.util

        path = ROOT / "tools" / "audit_leftovers.py"
        spec = importlib.util.spec_from_file_location("audit_leftovers_for_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.identity_findings(cfg)

    def test_synthetic_node_id_and_smart_link_are_reported(self) -> None:
        hits, routes = self._identity_findings({
            "node_id": "self-node",
            "smart_link": ["peer-1", "real-phone"],
            "routes": [{"id": "r1", "from": ["2cdffa7c-30ee-45cb-9862-46469ecf7ae8"],
                        "to": ["self-node"], "origin": "self-node"}],
        })
        self.assertTrue(any("node_id" in h for h in hits), "假 node_id 必须被 WARN 出来")
        self.assertTrue(any("peer-1" in h for h in hits), "smart_link 里的测试身份必须被 WARN 出来")
        self.assertTrue(any("origin" in h for h in routes), "路由 origin 指向假身份必须被 WARN 出来")
        self.assertTrue(any("to" in h for h in routes), "路由端点指向假身份必须被 WARN 出来")

    def test_real_uuid_and_real_devices_stay_quiet(self) -> None:
        hits, routes = self._identity_findings({
            "node_id": "a4c6d0f2-8b1e-4f7a-9c3d-2e5b6a7c8d9e",
            "smart_link": ["a4c6d0f2-8b1e-4f7a-9c3d-2e5b6a7c8d9e"],
            "routes": [{"id": "r-real", "from": ["b5d7e1a3-9c2f-4a8b-8d4e-3f6c7b8d9e0f"],
                        "to": ["a4c6d0f2-8b1e-4f7a-9c3d-2e5b6a7c8d9e"],
                        "origin": "a4c6d0f2-8b1e-4f7a-9c3d-2e5b6a7c8d9e"}],
        })
        self.assertEqual(hits, [], "真实 UUID 不能误报")
        self.assertEqual(routes, [], "真实设备路由不能误报")


# --------------------------------------------------------------------------
# ★ 2026-11-04（PC v2.22 / 手机 2.12）：A「智连」+ B「身份合并 / 防自环」
# --------------------------------------------------------------------------

class Test20261104SmartLinkAndIdentity(unittest.TestCase):
    """A「智连」= 接收方开了就自动接（发送方不需要开）：

    * 多台同时送 → 全接，不排队、不拒绝；
    * 「没声音」不挂断、数据流没了才挂（音频包静默 + 发现端口心跳消失，双判据）；
    * 自动打开的收听被用户手动关掉 → 智连按钮一起关，等下次人工打开；
    * 广播模式与智连互斥（不可用并给出原因）。

    B「身份合并 + 防自环」第一阶段：

    * 同一 deviceId 的多条链路（WiFi / 蓝牙）并成一个节点；
    * 同一 deviceId + 不同 instanceId = 两台真机，只提示、绝不自动合并；
    * 旧配置迁移：路由一条不少，旧 token 降级成 aliasIds，迁移前备份；
    * originId == 自己 → 丢帧并计数；hop 超限 → 丢帧并计数。
    """

    def _node(self, *, broadcast: bool = False, smart: list[str] | None = None):
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        cfg.broadcast = broadcast
        cfg.smart_link = list(smart or [])
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    def _peer(self, node, node_id: str = "peer-1", name: str = "手机A",
              host: str = "198.51.100.10", **extra):
        payload = {"wl": 1, "id": node_id, "name": name,
                   "audio_port": 47831, "web_port": 0}
        payload.update(extra)
        node.registry.update(payload, host)
        return node.registry.get(node_id)

    # ---- A1：接收方开了智连就自动接，发送方不需要也开 ----
    def test_smart_link_auto_accepts_on_the_receiver_side_only(self) -> None:
        node = self._node()
        try:
            self._peer(node)
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(node.routes, [], "接收方没开智连 → 不许自动接")
            self.assertFalse(node._auto_listen_granted)
            node.config.smart_link = ["手机A"]   # 只动接收方；发送方什么都不用开
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(len(node.routes), 1)
            rule = node.routes[0]
            self.assertEqual(rule["from"], ["peer-1"])
            self.assertEqual(rule["to"], [node.node_id])
            self.assertTrue(rule["auto"])
            self.assertTrue(node._auto_listen_granted["peer-1"]["created"])
        finally:
            node.stop()

    # ---- A2：多台同时送 → 全接，不排队、不拒绝 ----
    def test_two_sending_phones_are_both_answered_no_queue_no_refusal(self) -> None:
        node = self._node(smart=["手机A", "手机B"])
        try:
            self._peer(node, "peer-1", "手机A", "198.51.100.10")
            self._peer(node, "peer-2", "手机B", "192.168.0.102")
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            node._grant_listen_request("peer-2", "手机B", "192.168.0.102")
            self.assertEqual({r["from"][0] for r in node.routes}, {"peer-1", "peer-2"})
            self.assertEqual(len(node.auto_listen_state()["items"]), 2)
            self.assertEqual(node._pending_auto_listen, {}, "两边都要立刻接上，不能有排队")
        finally:
            node.stop()

    # ---- A3：静音不挂 / 断流才挂（双判据） ----
    def test_silent_audio_never_hangs_but_a_dead_stream_does(self) -> None:
        from audiomixing.engine import AUTO_LISTEN_HANG_S

        node = self._node(smart=["手机A"])
        try:
            peer = self._peer(node)
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(len(node.routes), 1)

            now = time.monotonic()
            peer.last_seen = time.time()
            node._auto_listen_granted["peer-1"]["last_audio"] = now
            node._sweep_auto_listen_hangs()
            self.assertEqual(len(node.routes), 1, "静音音频仍然有包 → 不许挂")
            self.assertFalse(node._auto_listen_hangs)

            # 只有音频静默、发现端口心跳还在 → 仍然不挂（双判据缺一不成立）。
            node._auto_listen_granted["peer-1"]["last_audio"] = now - 10.0
            peer.last_seen = time.time()
            node._sweep_auto_listen_hangs()
            self.assertEqual(len(node.routes), 1, "心跳还在 → 不挂")

            # 音频包和心跳都消失 → 这才是"数据流没了"。
            peer.last_seen = time.time() - 10.0
            node._sweep_auto_listen_hangs()
            self.assertEqual(node.routes, [])
            record = node._auto_listen_hangs["peer-1"]
            self.assertGreaterEqual(record["audio_silence_s"], AUTO_LISTEN_HANG_S)
            self.assertGreaterEqual(record["heartbeat_s"], AUTO_LISTEN_HANG_S)
            self.assertEqual(record["threshold_s"], AUTO_LISTEN_HANG_S,
                             "阈值要写成可观测字段，诊断页才解释得清")
            self.assertEqual(node._auto_listen_granted, {})
        finally:
            node.stop()

    # ---- A4：手动关自动收听 → 智连一起关；下次人工打开才恢复 ----
    def test_manual_listen_off_also_switches_the_smart_link_off(self) -> None:
        from audiomixing.webui import AudioMixingApi

        node = self._node(smart=["peer-1", "手机A"])
        try:
            self._peer(node)
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(len(node.routes), 1)
            # 走真实的删除入口（webui 会在同一个请求里先关智连、再删路由）。
            AudioMixingApi(node).set_routes({"routes": []})
            self.assertFalse(node.smart_link_allows("peer-1", "手机A"),
                             "手动关掉自动打开的收听 → 智连必须跟着关")
            self.assertEqual(node.config.smart_link, [])
            node._queue_auto_listen("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(node._pending_auto_listen, {},
                             "下一包音频不许把它又自动打开（你越关它越开）")
            # 等用户下次人工打开才恢复。
            node.set_smart_link("peer-1", "手机A", on=True)
            self.assertTrue(node.smart_link_allows("peer-1", "手机A"))
        finally:
            node.stop()

    # ---- A 硬规则：广播模式下智连不可用 ----
    def test_smart_link_is_unavailable_while_broadcasting(self) -> None:
        node = self._node(broadcast=True, smart=["手机A"])
        try:
            self.assertFalse(node.smart_link_allows("peer-1", "手机A"),
                             "广播开着时智连一律不可用")
            result = node.set_smart_link("peer-1", "手机A", on=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "broadcast")
            state = node.auto_listen_state()
            self.assertFalse(state["available"])
            self.assertIn("广播", state["reason"])
            self.assertFalse(node.status()["auto_listen"]["available"])
            self._peer(node)
            node._grant_listen_request("peer-1", "手机A", "198.51.100.10")
            self.assertEqual(node.routes, [], "广播模式下不许自动建收听路由")
        finally:
            node.stop()

    # ---- B1：同一 deviceId 的多条链路 = 一个节点 ----
    def test_multiple_links_with_one_device_id_become_one_node(self) -> None:
        reg = DiscoveryRegistry("self-node")
        base = {"wl": 1, "name": "手机", "audio_port": 47831, "web_port": 0,
                "device_id": "dev-1", "instance_id": "install-a", "ext": 1}
        reg.update(dict(base, id="phone-wifi"), "192.168.0.10")
        reg.update(dict(base, id="phone-bt"), "192.168.0.11")
        self.assertEqual(len(reg.all()), 1, "同一 deviceId 的 WiFi/蓝牙必须是一个节点")
        peer = reg.get("phone-wifi")
        self.assertEqual(peer.node_id, "phone-wifi", "主身份保留先到的那条")
        self.assertIn("phone-bt", peer.alias_ids)
        self.assertEqual(sorted(peer.hosts), ["192.168.0.10", "192.168.0.11"])
        self.assertTrue(peer.ext)
        self.assertIs(reg.get("phone-bt"), peer, "旧 id 必须仍能解析到同一个节点")
        self.assertIs(reg.resolve("phone-bt"), peer)
        self.assertEqual(reg.snapshot()[0]["device_id"], "dev-1")

    # ---- B1 反例：同一 deviceId + 不同 instanceId = 两台真机，不合并 ----
    def test_the_same_device_id_from_two_real_phones_is_never_auto_merged(self) -> None:
        reg = DiscoveryRegistry("self-node")
        base = {"wl": 1, "name": "手机", "audio_port": 47831, "web_port": 0,
                "device_id": "dev-shared"}
        reg.update(dict(base, id="phone-a", instance_id="install-a"), "192.168.0.20")
        reg.update(dict(base, id="phone-b", instance_id="install-b"), "192.168.0.21")
        self.assertEqual(len(reg.all()), 2,
                         "同一 deviceId 但 instanceId 不同 = 两台真机，绝不合并")
        self.assertTrue(reg.get("phone-a").identity_conflict)
        self.assertTrue(reg.get("phone-b").identity_conflict)

    # ---- B1 迁移：路由一条不少，旧 id 降级为 alias，迁移前备份 ----
    def test_old_config_migration_keeps_every_route_and_demotes_ids_to_aliases(self) -> None:
        folder = _mkdtemp(prefix="am-identity-")
        path = folder / "config.json"
        mine = "a4c6d0f2-8b1e-4f7a-9c3d-2e5b6a7c8d9e"
        raw = {
            "node_id": mine, "name": "本机",
            "routes": [
                {"id": "r-old-1", "from": ["legacy-node-a"], "to": [mine]},
                {"id": "r-old-2", "from": ["manual:192.168.0.9"], "to": ["legacy-node-b"]},
            ],
        }
        path.write_text(json.dumps(raw), encoding="utf-8")
        cfg = Config.load(path)
        self.assertEqual(len(cfg.routes), 2, "迁移不许删用户路由")
        self.assertEqual([r["id"] for r in cfg.routes], ["r-old-1", "r-old-2"])
        self.assertEqual(cfg.device_id, mine, "老配置没有 device_id 时取 node_id")
        self.assertTrue(cfg.instance_id)
        self.assertTrue(cfg.identity_migrated)
        self.assertEqual(cfg.routes[0]["aliasIds"], ["legacy-node-a"])
        self.assertEqual(set(cfg.routes[1]["aliasIds"]),
                         {"manual:192.168.0.9", "legacy-node-b"})
        self.assertEqual(len(list(folder.glob("config.json.bak-*"))), 1,
                         "迁移前必须留一份 config 备份")
        # 第二次加载不能再生成备份、也不能改路由。
        cfg2 = Config.load(path)
        self.assertEqual(len(list(folder.glob("config.json.bak-*"))), 1)
        self.assertEqual(len(cfg2.routes), 2)

    # ---- B2：originId == 自己 → 丢帧并计数（诊断可见） ----
    def test_frames_whose_origin_is_ourselves_are_dropped_and_counted(self) -> None:
        node = self._node()
        try:
            oid = node.origin_id
            self.assertTrue(oid)
            own = proto.Packet(type=proto.T_AUDIO, payload=b"\x00" * 1920,
                               codec=proto.CODEC_PCM_S16, seq=1, ssrc=1,
                               samples=480, ts_ns=proto.now_ns())
            own.origin_id = oid
            self.assertFalse(node.receiver._admitted(own, "198.51.100.10"))
            self.assertEqual(node.receiver.packets_echo_dropped, 1)
            self.assertEqual(node.status()["loop_guard"]["echo_dropped"], 1,
                             "诊断页必须显示「自回声丢弃 N」")
            # 老端没有扩展段（origin_id=0）→ 走老路径、照样收下，不报错、不拒流。
            legacy = proto.Packet(type=proto.T_AUDIO, payload=b"\x00" * 1920)
            self.assertTrue(node.receiver._admitted(legacy, "198.51.100.10"))
            self.assertEqual(node.receiver.packets_echo_dropped, 1)
            # 编码/解码：扩展段里的 originId + hop 全程跟着走；老包一个字节都不变。
            decoded = proto.decode(own.encode())
            self.assertEqual((decoded.origin_id, decoded.hop), (oid, 0))
            self.assertEqual(len(proto.Packet(type=proto.T_AUDIO, payload=b"abc").encode()),
                             proto.HEADER_SIZE + 3)
        finally:
            node.stop()

    # ---- B2：hop 超限 → 丢帧并计数（正好到上限仍允许） ----
    def test_frames_that_exceed_the_hop_limit_are_dropped_and_counted(self) -> None:
        recv = Receiver("self", 0, self_origin_id=proto.origin_id_of("other-device"))
        over = proto.Packet(type=proto.T_AUDIO, payload=b"\x00" * 1920,
                            origin_id=proto.origin_id_of("some-origin"),
                            hop=proto.MAX_HOP + 1)
        self.assertFalse(recv._admitted(over, "198.51.100.10"))
        self.assertEqual(recv.packets_hop_dropped, 1)
        edge = proto.Packet(type=proto.T_AUDIO, payload=b"\x00" * 1920,
                            origin_id=proto.origin_id_of("some-origin"),
                            hop=proto.MAX_HOP)
        self.assertTrue(recv._admitted(edge, "198.51.100.10"),
                        "正好到上限还允许（超过才丢）")
        self.assertEqual(recv.packets_hop_dropped, 1)


# --------------------------------------------------------------------------
# ★ 2026-11-04（PC v2.24 / 手机 2.14）：用户真机第二轮 11 条，逐条一条回归
# --------------------------------------------------------------------------

def _strip_markup(text: str) -> str:
    """去掉 HTML/JS/Java 注释后再查"残留"——注释里记录"已删除"不算残留。"""
    import re

    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


class TestRoundTwoRegressions(unittest.TestCase):
    """11 条真机问题，每一条都有独立断言；不合并成"顺手重构"。"""

    def _node(self, *, smart: list[str] | None = None):
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        cfg.smart_link = list(smart or [])
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    def _peer(self, node, node_id: str = "peer-1", name: str = "手机A",
              host: str = "198.51.100.10", **extra):
        payload = {"wl": 1, "id": node_id, "name": name,
                   "audio_port": 47831, "web_port": 0}
        payload.update(extra)
        node.registry.update(payload, host)
        return node.registry.get(node_id)

    def _api(self, node):
        from audiomixing.webui import AudioMixingApi

        return AudioMixingApi(node)

    # ---- 第 1 条：僵尸路由必须能标失效、能一键清理 ----
    def test_zombie_routes_are_marked_with_missing_seconds_and_purged(self) -> None:
        node = self._node()
        try:
            self._peer(node, "live-peer", "手机在线", "198.51.100.10")
            node.set_routes([
                {"id": "r-live", "from": ["live-peer"], "to": [node.node_id]},
                {"id": "r-zombie", "from": ["b1fbc20c-b560-48"], "to": [node.node_id]},
            ], persist=False)
            node._refresh_route_staleness()
            zombie = next(r for r in node.routes_json() if r["id"] == "r-zombie")
            self.assertFalse(zombie["stale"], "刚发现端点消失时先观察，不能立刻判死")
            node._unknown_route_since["r-zombie"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            zombie = next(r for r in node.routes_json() if r["id"] == "r-zombie")
            self.assertTrue(zombie["stale"], "持续找不到 60 秒 → 路由必须标失效")
            self.assertGreaterEqual(zombie["missing_s"], 90.0,
                                    "诊断要能看到它消失了多久，而不是只给一个布尔值")
            result = self._api(node).purge_stale_routes()
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["removed"], ["r-zombie"])
            self.assertEqual([r["id"] for r in node.routes_json()], ["r-live"],
                             "一键清理只删失效的，活的路线一条都不能动")
        finally:
            node.stop()

    # ---- 第 2 条（a）：离线设备卡片按超时回收，manual 不回收 ----
    def test_offline_device_cards_expire_after_thirty_seconds(self) -> None:
        node = self._node()
        try:
            live = self._peer(node, "live-peer", "手机在线", "198.51.100.10")
            old = self._peer(node, "old-peer", "手机离线", "192.168.0.100")
            old.last_seen = time.time() - 40.0
            node.registry.add_manual("192.168.0.9", 47831, "手动设备")
            manual = node.registry.resolve("192.168.0.9")
            manual.last_seen = time.time() - 600.0
            peers = self._api(node).peers()["peers"]
            ids = [p["node_id"] for p in peers]
            self.assertIn("live-peer", ids)
            self.assertNotIn("old-peer", ids, "40 秒没音讯的卡片必须在面板上消失")
            self.assertIn(manual.node_id, ids,
                          "用户明确添加的对端不参与超时回收")
        finally:
            node.stop()

    # ---- 第 2 条（b）：同 IP 重复卡片合并；不同名的真机不许误合并 ----
    def test_duplicate_cards_on_the_same_host_are_merged_but_two_phones_are_not(self) -> None:
        reg = DiscoveryRegistry("self-node")
        base = {"wl": 1, "audio_port": 47831, "web_port": 0, "platform": "android"}
        reg.update(dict(base, id="M332BF-da71", name="M332BF-da71"), "192.168.0.100")
        reg.get("M332BF-da71").last_seen = time.time() - 30.0   # 旧卡已经超时
        reg.update(dict(base, id="M332BF-b1fb", name="M332BF-b1fb"), "192.168.0.100")
        self.assertEqual(len(reg.all()), 1,
                         "同 IP + 同机型前缀、旧记录已超时 → 必须并成一张卡")
        self.assertTrue(reg.resolve("M332BF-da71") is not None,
                        "旧 id 仍要能解析到合并后的那条（路由不能失联）")

        reg2 = DiscoveryRegistry("self-node")
        reg2.update(dict(base, id="phone-a", name="小明的手机"), "192.168.0.101")
        reg2.update(dict(base, id="phone-b", name="小红的手机"), "192.168.0.101")
        self.assertEqual(len(reg2.all()), 2,
                         "两条都新鲜、名字不同 → 绝不静默合并两台真机")
        reg2.update(dict(base, id="phone-b", name="小红的手机",
                         device_id="dev-b", instance_id="inst-b"), "192.168.0.101")
        reg2.update(dict(base, id="phone-a", name="小明的手机",
                         device_id="dev-a", instance_id="inst-a"), "192.168.0.101")
        self.assertEqual(len(reg2.all()), 2, "deviceId 不同的两台真机同样不合并")

    # ---- 第 2 条（c）：对端在线时不能报成"关机 / 没消息" ----
    def test_an_offline_duplicate_id_sends_to_the_live_alias_instead_of_alarming(self) -> None:
        node = self._node()
        try:
            self._peer(node, "new-id", "M332BF-b1fb", "192.168.0.100")
            old = self._peer(node, "old-id", "M332BF-da71", "192.168.0.100")
            old.last_seen = time.time() - 29.0     # 现场那张"29 秒没有消息"的卡
            node.set_routes([{"id": "r-send", "from": [node.node_id],
                              "to": ["old-id"]}], persist=False)
            target = node._matching_nodes(["old-id"])
            self.assertEqual([t.node_id for t in target], ["new-id"],
                             "同一 IP 上有在线记录 → 必须换成活的，不能继续对着死 ID 发")
            key = "r-send|new-id"
            self.assertEqual([k for k, t, _r in node.desired_senders() if t.node_id == "new-id"],
                             [key])
            self.assertNotIn(key, node._senders_blocked,
                             "对端明明在线 → 不许产生「没消息」的告警")
            self.assertFalse(any("关机" in str(v.get("text") or "")
                                for v in node._senders_blocked.values()),
                             "告警文案不能把「换 ID」写成「关机」")
            sent = node.desired_senders()
            self.assertTrue(sent and sent[0][1].node_id == "new-id")
        finally:
            node.stop()

    # ---- 第 3 条：蓝牙提示按真实状态分支 ----
    def test_bluetooth_empty_reason_distinguishes_each_real_state(self) -> None:
        from audiomixing import bluetooth as bt

        cases = []
        with mock.patch.object(bt, "available", return_value=True), \
                mock.patch.object(bt, "radio_state",
                                  return_value={"present": False, "state": "unknown",
                                                "name": ""}):
            cases.append(bt.empty_reason())
        with mock.patch.object(bt, "available", return_value=True), \
                mock.patch.object(bt, "radio_state",
                                  return_value={"present": True, "state": "off",
                                                "name": "Intel Bluetooth"}):
            cases.append(bt.empty_reason())
        with mock.patch.object(bt, "available", return_value=True), \
                mock.patch.object(bt, "radio_state",
                                  return_value={"present": True, "state": "on",
                                                "name": "Intel Bluetooth"}):
            cases.append(bt.empty_reason())
        self.assertIn("没有检测到蓝牙适配器", cases[0])
        self.assertIn("蓝牙已关闭", cases[1])
        self.assertNotIn("配对", cases[1], "蓝牙没开时不能叫用户去配对")
        self.assertIn("还没有配对", cases[2])
        self.assertIn("Intel Bluetooth", cases[2], "开着时要能说出适配器型号")
        self.assertEqual(len(set(cases)), 3, "四种状态必须给四种不同的话")
        html = _strip_markup((ROOT / "audiomixing" / "ui" / "index.html")
                             .read_text(encoding="utf-8"))
        self.assertNotIn("请先在 Windows 设置里把手机配对", html,
                         "前端不许再写死「没配对」这条误导文案")

    # ---- 第 4 条：送声音改成循环键，第 2 档不做、不碰系统音量 ----
    def test_the_phone_send_button_cycles_and_never_touches_system_volume(self) -> None:
        src = (ROOT / "android" / "app" / "src" / "main" / "java" /
               "com" / "audiomixing" / "app" / "MainActivity.java").read_text(encoding="utf-8")
        code = _strip_markup(src)
        self.assertNotIn("showSendModeDialog", code, "三档弹窗必须删掉")
        self.assertIn("engine.setSendMode(0)", code,
                      "第 2 档（送声+手机静音）再按一下回到第 0 档")
        self.assertIn("engine.setSendMode(2)", code,
                      "第 1 档再按一下进入第 2 档（送声+手机静音）")
        self.assertIn("手机静音", code)
        self.assertIn("SEND_MODE_SILENT_NOTE", code,
                      "长按要把静音语义、还原路径与副作用说清")
        android = (ROOT / "android" / "app" / "src" / "main" / "java" /
                   "com" / "audiomixing" / "app")
        for path in android.glob("*.java"):
            live = _strip_markup(path.read_text(encoding="utf-8"))
            self.assertNotIn("setStreamMute(", live,
                             "用户硬约束：一处都不许改系统静默（%s）" % path.name)
            if path.name == "Engine.java":
                # ★ 2026-11-04 用户授权：第 2 档允许把 STREAM_MUSIC 临时降到 0，
                #   但只允许集中在 applySilentVolume 一处（便于审计）。
                self.assertEqual(live.count("am.setStreamVolume("), 2,
                                 "Engine 里只允许「置 0 + 还原」两处音量调用")
            else:
                self.assertNotIn("setStreamVolume(", live,
                                 "系统音量只允许第 2 档那一处（%s）" % path.name)

    # ---- 第 5 条：双判据挂断后状态回落待命，诊断可读两个数 ----
    def test_dual_criteria_hang_keeps_the_manual_route_and_returns_to_standby(self) -> None:
        node = self._node(smart=["手机A"])
        try:
            peer = self._peer(node)
            node.set_routes([{"id": "manual-listen", "from": ["peer-1"],
                              "to": [node.node_id]}], persist=False)
            node._auto_listen_granted["peer-1"] = {
                "node_id": "peer-1", "name": "手机A", "host": "198.51.100.10",
                "route_id": "manual-listen", "created": False,
                "at": time.time(), "last_audio": time.monotonic() - 10.0}
            peer.last_seen = time.time() - 10.0
            node._sweep_auto_listen_hangs()
            self.assertEqual([r["id"] for r in node.routes], ["manual-listen"],
                             "用户自己建的收听路由保留，只把状态收回到待命")
            self.assertEqual(node._auto_listen_granted, {}, "自动收听状态必须清空")
            hang = node._auto_listen_hangs["peer-1"]
            self.assertTrue(hang["route_kept"])
            self.assertGreaterEqual(hang["audio_silence_s"], 4.0)
            self.assertGreaterEqual(hang["heartbeat_s"], 4.0)
            card = next(p for p in self._api(node).peers()["peers"]
                        if p["node_id"] == "peer-1")
            self.assertIsNotNone(card["listen_hung"], "卡片要能看到「已挂断·待命」")
            self.assertGreaterEqual(float(card["listen_audio_s"]), 4.0,
                                    "诊断要显示音频静默秒数")
            self.assertGreaterEqual(float(card["listen_heartbeat_s"]), 4.0,
                                    "诊断要显示心跳秒数")
            self.assertFalse(card["smart_auto"], "智连状态要回落到「不是自动收听」")
            self.assertEqual(node.auto_listen_state()["items"], [])
        finally:
            node.stop()

    # ---- 第 6 条：APK 交付名一律带版本号 ----
    def test_apk_delivery_names_carry_the_phone_version_everywhere(self) -> None:
        from audiomixing import __phone_version__, webui

        self.assertEqual(__phone_version__, "2.20")
        self.assertEqual(webui.apk_display_name(), "AudioMixing-手机版-v2.20.apk")
        self.assertIn("AudioMixing-手机版-v2.20.apk", webui._APK_NAMES)
        init_py = (ROOT / "audiomixing" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('__phone_version__ = "2.20"', init_py)
        check = (ROOT / "tools" / "check_apk.py").read_text(encoding="utf-8")
        self.assertIn('REQUIRED_VERSION = "2.20"', check,
                      "check_apk --release 的版本门槛必须一起改成当前手机版本")
        self.assertIn("AudioMixing-手机版-v2.20.apk", check,
                      "交付副本与 installer payload 的路径必须带版本号")
        bat = (ROOT / "tools" / "installer_payload" / "install.bat").read_text(encoding="utf-8")
        self.assertIn("AudioMixing-手机版-v*.apk", bat,
                      "安装脚本要按带版本号的名字取包")
        app = (ROOT / "audiomixing" / "ui" / "app.html").read_text(encoding="utf-8")
        self.assertIn("AudioMixing-手机版-v版本号.apk", app,
                      "二维码说明页要写清文件名带版本号")

    # ---- 第 7 条：接收方开智连 → 对方一送就自动收听（端到端） ----
    def test_smart_link_starts_listening_on_the_first_send_end_to_end(self) -> None:
        node = self._node(smart=["手机A"])
        try:
            self._peer(node)
            node.set_routes([], persist=False)
            node.receiver.control_messages.append(
                {"t": "stats", "node": "peer-1", "name": "手机A", "_type": "STATS",
                 "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                 "send_mode": 1, "broadcast": False, "sending": True,
                 "want_listen": True})
            node._handle_peer_requests()
            auto = [r for r in node.routes if r.get("auto")]
            self.assertEqual(len(auto), 1, "接收方开了智连 → 对方一送就必须自动开始收听")
            self.assertEqual(auto[0]["from"], ["peer-1"])
            self.assertEqual(node._auto_listen_granted["peer-1"]["route_id"], auto[0]["id"])
            self.assertIn(auto[0]["id"], node.routes_for_sender("peer-1"),
                          "自动建的路由要立刻挂到这路流上，不用等重启")
            self.assertTrue(node._stream_is_accepted("peer-1"))

            engine_java = (ROOT / "android" / "app" / "src" / "main" / "java" /
                           "com" / "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
            start = engine_java.index("private boolean smartOnLocked")
            body = engine_java[start:engine_java.index("\n    }", start)]
            self.assertIn("smartHosts.contains(host)", body)
            self.assertIn("smartHosts.contains(idOf(p))", body, "手机侧不能只按 IP 判智连")
            self.assertIn("smartHosts.contains(name)", body, "设备名也要能命中")
        finally:
            node.stop()

    # ---- 第 8 条：「测连通」按钮与接口都下线 ----
    def test_the_ping_button_and_api_are_gone_from_every_page(self) -> None:
        from audiomixing import webui

        self.assertFalse(hasattr(webui.AudioMixingApi, "ping"))
        for name in ("index.html", "phone.html", "app.html"):
            text = _strip_markup((ROOT / "audiomixing" / "ui" / name).read_text(encoding="utf-8"))
            self.assertNotIn("测连通", text, "前端按钮必须删掉：%s" % name)
            self.assertNotIn("pingPeer", text)
        src = (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8")
        live = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("/api/ping", live, "服务端路由必须删掉，不留死接口")

    # ---- 第 9 条：一台手机两条链路并成一个节点，图上不自环 ----
    def test_one_phone_with_two_links_is_one_node_and_never_self_loops(self) -> None:
        reg = DiscoveryRegistry("self-node")
        base = {"wl": 1, "name": "手机", "audio_port": 47831, "web_port": 0,
                "device_id": "dev-1", "instance_id": "install-a"}
        reg.update(dict(base, id="M332BF-5e65"), "192.168.0.100")
        reg.update(dict(base, id="瘾控"), "192.168.0.100")
        self.assertEqual(len(reg.all()), 1, "同一 deviceId 的 WiFi/蓝牙链路必须是一个节点")
        self.assertIs(reg.get("M332BF-5e65"), reg.get("瘾控"))

        reg2 = DiscoveryRegistry("self-node")
        reg2.update(dict(base, id="phone-a", device_id="dev-a", instance_id="a"), "192.168.0.20")
        reg2.update(dict(base, id="phone-b", device_id="dev-b", instance_id="b"), "192.168.0.21")
        self.assertEqual(len(reg2.all()), 2, "两台真机绝不静默并成一个节点")

        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("identityCanonId", html, "图上必须先做身份归一")
        self.assertIn("observed_bt_mac", html, "蓝牙 MAC 是合并的强证据之一")
        self.assertIn("nameCount[nm] === 1", html,
                      "名字证据只允许「唯一一台」时使用，防止误合并")
        self.assertIn("if(!u || !v || u === v) return;", html,
                      "归一后同一台设备的边不算回环")
        self.assertIn("if(u === v) return;", html,
                      "归一后同一台设备不画自环")

    # ---- 第 10 条：超时后真 ghost **从图上消失**，只留在路由表并可一键清理 ----
    def test_a_stale_route_is_marked_missing_and_purgeable(self) -> None:
        """★ 2026-11-04（第四轮，用户截图）：missing 不能再画成红虚线幽灵卡片 ——
        它必须从关系图上消失，只剩路由表里的记录 + 「清理失效路由」入口。"""
        node = self._node()
        try:
            node.set_routes([{"id": "r-ghost", "from": ["b1fbc20c-b560-48"],
                              "to": [node.node_id]}], persist=False)
            node._unknown_route_since["r-ghost"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            row = node.routes_json()[0]
            self.assertTrue(row["stale"])
            self.assertEqual(row["state"], "missing")
            html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
            self.assertIn("if(String(r.state || '') === 'missing' || r.stale) return;", html,
                          "missing 路由不进关系图（用户截图里的幽灵卡片就是这么来的）")
            self.assertIn("清理失效路由", html, "必须给用户一键清理入口")
            self.assertIn("purgeStaleRoutes", html)
            self.assertIn("/api/routes/purge-stale",
                          (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8"))
        finally:
            node.stop()

    # ---- 第 11 条：「本机播放」按钮与接口彻底下线，兜底路径要写给用户 ----
    def test_the_local_play_button_is_gone_and_the_fallback_is_documented(self) -> None:
        from audiomixing import webui

        node = self._node()
        try:
            self.assertFalse(hasattr(node, "set_bluetooth_local_play"))
            self.assertFalse(hasattr(node, "bluetooth_local_playing"))
            self.assertFalse(hasattr(webui.AudioMixingApi, "bluetooth_local"))
        finally:
            node.stop()
        src = (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8")
        live = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("/api/bluetooth/local", live)
        self.assertIn("本机直通", src, "驱动不出声时的兜底路径要在诊断里告诉用户")
        index = _strip_markup((ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("本机播放", index)
        self.assertNotIn("btLocal(", index)
        self.assertIn("本机直通", index, "界面上要留着频道 → 音源 + 本机直通这条路")


class Test20261104V224(unittest.TestCase):
    """★ 2026-11-04（v2.24 / 手机 2.14）：本轮 4 项修复 + 第 2 档（用户已授权）。"""

    def _node(self):
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "self-v224"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        cfg.smart_link = []
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    # ---- 第 1 条回退：三态判定，只有真 missing 才是 stale ----
    def test_repeat_three_states_missing_no_data_offline(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "live-phone", "name": "live-phone",
                                  "audio_port": 47831, "web_port": 0}, "192.168.0.100")
            node.set_routes([{"id": "r-live", "from": ["live-phone"], "to": ["self"]}],
                            persist=False)
            with mock.patch.object(node, "_route_has_traffic", lambda rid: False):
                node._refresh_route_staleness()
            item = node.status()["routes"][0]
            self.assertEqual(item["state"], "no_data",
                             "两端在线但没有音频流 → no_data，必须保持实线")
            self.assertFalse(item["stale"], "no_data 绝不能反过来画成虚线")
            with mock.patch.object(node, "_route_has_traffic", lambda rid: True):
                node._refresh_route_staleness()
            self.assertEqual(node.status()["routes"][0]["state"], "ok")

            # 见过面的真设备掉线：offline，不是 missing
            node.registry.get("live-phone").last_seen = time.time() - 120.0
            node._refresh_route_staleness()
            item = node.status()["routes"][0]
            self.assertEqual(item["state"], "offline")
            self.assertFalse(item["stale"], "真设备关机/换网不能当幽灵清掉")

            # 从未见过的旧 id：60 秒后才是 missing
            node.set_routes([{"id": "r-old", "from": ["b1fbc20c-b560-48"], "to": ["self"]}],
                            persist=False)
            node._refresh_route_staleness()
            self.assertFalse(node.status()["routes"][0]["stale"], "先观察，不能立即判死")
            node._unknown_route_since["r-old"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            item = node.status()["routes"][0]
            self.assertTrue(item["stale"], "真 ghost 才画虚线并进入清理清单")
            self.assertEqual(item["state"], "missing")
        finally:
            node.stop()

    def test_purge_only_removes_true_missing(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "live-phone", "name": "live-phone",
                                  "audio_port": 47831, "web_port": 0}, "192.168.0.100")
            node.set_routes([
                {"id": "r-live", "from": ["live-phone"], "to": ["self"]},
                {"id": "r-old", "from": ["old-id-never-seen"], "to": ["self"]},
            ], persist=False)
            node._unknown_route_since["r-old"] = time.monotonic() - 90.0
            node._refresh_route_staleness()
            result = node.purge_stale_routes()
            ids = [r["id"] for r in node.routes]
            self.assertNotIn("r-old", ids, "真 missing 才可以清")
            self.assertIn("r-live", ids, "活路由一律不能动")
            self.assertEqual(result.get("removed"), ["r-old"])
        finally:
            node.stop()

    # ---- 第 8 条：挂断后卡片/诊断/实际秒数对齐 ----
    def test_hang_seconds_keep_ticking_and_clear_when_audio_returns(self) -> None:
        node = self._node()
        try:
            node._auto_listen_hangs["p-1"] = {
                "node_id": "p-1", "name": "phone", "audio_silence_s": 4.2,
                "heartbeat_s": 4.5, "threshold_s": 4.0, "at": time.time() - 26.0,
                "route_kept": True,
            }
            hang = node.auto_listen_state()["hangs"][0]
            self.assertGreaterEqual(hang["audio_silence_s"], 29.0,
                                    "截图过了 30 秒，诊断数字必须跟着涨，不能停在判定那一刻")
            self.assertIn("heartbeat_s", hang)
            # 新的音频包一到 → 旧的挂断记录必须立刻作废（否则一直显示"已挂断"）
            node.config.smart_link = ["p-1"]
            node._queue_auto_listen("p-1", "phone", "192.168.0.100")
            self.assertNotIn("p-1", node._auto_listen_hangs)
        finally:
            node.stop()

    def test_ui_listening_label_follows_actual_audio(self) -> None:
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("flows.in_receiving ? '✓ 收听中'", html,
                      "只有真的在收音频才显示「收听中」")
        self.assertIn("已连 · 等数据", html, "有路由但没数据要单独标注，不能冒充收听中")
        self.assertIn("已挂断 · 待命", html)

    # ---- 第 11 条：首个请求/首包即接，不等二十秒 ----
    def test_pc_pushes_listen_req_immediately_when_sender_starts(self) -> None:
        src = (ROOT / "audiomixing" / "engine.py").read_text(encoding="utf-8")
        i = src.index("self.sender.add(rid, session)")
        j = src.index("except Exception as exc:", i)
        self.assertIn("push_peer_stats_now()", src[i:j],
                      "建好发送会话就立刻推 listen_req，不能等 2 秒统计节流")
        self.assertIn("and not force", src, "强制推送要允许首包未发时就带 listen_req")

    def test_phone_applies_smart_immediately_on_toggle(self) -> None:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        self.assertIn("applySmartNow(host)", java,
                      "打开智连的瞬间要按缓存统计立即开收")
        self.assertIn("if (nowOn) {", java)

    # ---- 第 14 条：左进右出 + 颜色只跟链路种类 + 自环不画 ----
    def test_graph_is_left_to_right_and_state_coloured(self) -> None:
        html = (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")
        # ★ v2.28（用户第 4 条）：层级改为"以本机为参照的双向 BFS"（v2.27 的 Tarjan strong(v) 已删）。
        for token in ("relComputeRank", "let queue = [selfKey];",
                      "rank[v] = rank[u] - 1", "rank[v] = rank[u] + 1",
                      "marker-end=\"url(#' + relArrowId(l)", "if(u === v) return;"):
            self.assertIn(token, html, "缺少左进右出/自环/箭头：%s" % token)
        # ★ 第四轮：颜色 = 链路种类，状态只改变线型/透明度（不再覆盖 stroke）。
        self.assertIn("#relSvg .ln.wifi{stroke:#38bdf8}", html)
        self.assertIn("#relSvg .ln.bt{stroke:#34d399}", html)
        self.assertIn("#relSvg .ln.bc{stroke:#fbbf24}", html)
        self.assertIn("#relSvg .ln.nodata{opacity:.55}", html, "no_data 只压透明度，不改色")
        self.assertIn("#relSvg .ln.offline{opacity:.3}", html, "offline 只压透明度，不改色")
        self.assertNotIn("#relSvg .ln.nodata{stroke:", html)
        self.assertNotIn("#relSvg .ln.offline{stroke:", html)
        self.assertNotIn("#relSvg .ln.missing{stroke:", html)
        # 状态不再换箭头颜色：箭头只按链路种类 + 回环专用红色。
        self.assertNotIn("arr-nodata", html)
        self.assertNotIn("arr-offline", html)
        self.assertNotIn("arr-missing", html)
        self.assertIn("arr-cycle", html, "回环箭头仍为红色（红色是回环专用）")

    # ---- 第 2 档：手机静音但继续送（用户已授权，唯一允许动系统音量的一条路径）----
    def test_tier2_volume_zero_is_the_only_place_touching_stream_volume(self) -> None:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        self.assertNotIn("am.setStreamMute(", java, "静音必须用音量归零，不许锁 mute 标志")
        self.assertEqual(java.count("am.setStreamVolume("), 2,
                         "只允许 applySilentVolume 里两处真正的调用：置 0 + 还原原值")
        self.assertIn("AudioManager.STREAM_MUSIC, 0, 0", java)
        self.assertIn("AudioManager.STREAM_MUSIC, orig, 0", java)

    def test_tier2_stream_keeps_running(self) -> None:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        i = java.index("public void setSendMode(int mode)")
        body = java[i:java.index("public static final String SEND_MODE_SILENT_NOTE", i)]
        self.assertNotIn("stopSender()", body, "第 2 档绝不许停发送")
        self.assertNotIn("stopReceiver()", body)
        self.assertIn("syncSilentVolume();", body)

    def test_tier2_restore_on_stop_switch_and_destroy(self) -> None:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        i = java.index("public void stop()")
        body = java[i:java.index("\n    }", i) + 6]
        self.assertIn("syncSilentVolume();", body, "退出/服务销毁必须还原音量")
        self.assertIn("syncSilentVolume();", java[java.index("public void setSending"):],
                      "停止送声必须还原")

    def test_tier2_crash_marker_restores_on_next_launch(self) -> None:
        java = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        ctor = java[java.index("private Engine(Context ctx)"):]
        self.assertIn("restoreSilentVolumeIfNeeded();", ctor[:400],
                      "下次启动必须先还原残留标记")
        self.assertIn("PREF_SILENT_ACTIVE", java)
        self.assertIn(".commit();", java, "先同步落盘再静音，崩在下一行也能还原")

    def test_tier2_cycle_order_and_zero_mode_has_no_mute(self) -> None:
        act = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
               "audiomixing" / "app" / "MainActivity.java").read_text(encoding="utf-8")
        i = act.index("if (engine.isSending(p))")
        body = act[i:act.index("return;", act.index("engine.setSendMode(0)") + 10)]
        self.assertIn("engine.setSendMode(2)", body)
        self.assertIn("engine.setSendMode(0)", body)
        self.assertIn("手机静音", act)
        self.assertIn("SEND_MODE_SILENT_NOTE", act)
        engine = (ROOT / "android" / "app" / "src" / "main" / "java" / "com" /
                  "audiomixing" / "app" / "Engine.java").read_text(encoding="utf-8")
        zero = engine[engine.index("public void setSendMode(int mode)"):]
        zero = zero[:zero.index("public static final String SEND_MODE_SILENT_NOTE")]
        self.assertIn("if (m == 0) {", zero)
        self.assertIn("sendIds.clear();", zero, "第 0 档要清空送声目标（随后还原音量）")
        self.assertIn("syncSilentVolume();", zero, "第 0 档必须恢复音量，不残留静音")


class Test20261104V225(unittest.TestCase):
    """★ 2026-11-04（PC v2.25）：用户截图「先把这个最重要的、显而易见的修掉。这个颜色也不对呀」。

    截图里的现象：本机在左，右边挂着两张 `不在线 / 不可达` 的幽灵卡片，两条**红色虚线**
    从本机指过去。两件事都不对：
      1. `missing`（身份确实不存在）的路由根本不该出现在关系图上 —— 它只该留在路由表里，
         等用户点「清理失效路由」；
      2. 颜色被状态抢走了 —— 红虚线本来是"回环"的语义，Wi-Fi 链路却被画成红色。
         颜色必须只表示链路种类，状态只用线型/透明度表达。
    """

    def _html(self) -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def test_missing_routes_never_create_ghost_cards(self) -> None:
        """截图里那两张 `不在线 / 不可达` 是 keyForId 现造的；现在查不到端点就不画边。"""
        html = self._html()
        i = html.index("function relBuild(")
        body = html[i:html.index("function relComputeRank(", i)]
        self.assertIn("return keyOf[t] || '';", body, "查不到的端点不画边")
        self.assertNotIn("'不在线 / 不可达'", body, "不再现造幽灵卡片")
        self.assertNotIn("const key = 'x:' + t;", body)
        # 漏网越过 skip 的边也不会被画出来：类名是 missing（本色 + 虚线 + 压淡）。
        self.assertIn("if(l.state === 'missing') return cls + ' missing';", html)

    def test_missing_routes_do_not_fake_a_cycle_warning(self) -> None:
        """指向幽灵节点的边不能参与回环判定，否则顶部警告会凭空出现。"""
        html = self._html()
        i = html.index("function routeCycleInfo(")
        body = html[i:html.index("function identityDisplay(", i)]
        self.assertIn("if(String(r.state || '') === 'missing' || r.stale) return;", body,
                      "回环判定也必须跳过 missing 路由")

    def test_link_colour_is_stable_and_state_only_changes_opacity(self) -> None:
        """颜色 = 链路种类；状态类里不许再出现 `stroke:`。"""
        html = self._html()
        for line in ("#relSvg .ln.wifi{stroke:#38bdf8}",
                     "#relSvg .ln.bt{stroke:#34d399}",
                     "#relSvg .ln.bc{stroke:#fbbf24}"):
            self.assertIn(line, html, "链路颜色固定：%s" % line)
        i = html.index("#relSvg .ln.nodata")
        j = html.index("#relSvg .ln.cycle")
        state_css = html[i:j]
        for token in ("#relSvg .ln.nodata{opacity:.55}", "#relSvg .ln.offline{opacity:.3}",
                      "#relSvg .ln.missing{stroke-dasharray:7 6;opacity:.4}"):
            self.assertIn(token, state_css, "状态只用线型/透明度：%s" % token)
        self.assertNotIn("stroke:#", state_css, "状态类绝对不许抢链路颜色")
        # 红色是回环专用，其余状态不再出现红/琥珀/灰的 stroke 覆盖。
        self.assertNotIn("stroke:#f87171;stroke-dasharray:7 6", html)

    def test_arrow_colour_follows_link_kind_not_state(self) -> None:
        html = self._html()
        i = html.index("function relArrowId(")
        body = html[i:html.index("/** 箭头（", i)]
        self.assertIn("if(l.cycle) return 'arr-cycle';", body)
        self.assertIn("return l.kind === 'bt' ? 'arr-bt'", body)
        self.assertIn("(l.kind === 'bc' ? 'arr-bc' : 'arr-wifi')", body)
        for dead in ("arr-nodata", "arr-offline", "arr-missing"):
            self.assertNotIn(dead, html, "状态不再单独换箭头：%s" % dead)

    def test_sources_sit_on_the_left_and_targets_on_the_right(self) -> None:
        """左进右出的判定依据：`relComputeRank` 以本机为参照做双向 BFS（上游为负、下游为正），
        `relAutoPos` 用 `x = 22 + (rank - minRank) * span` 定列号（源在左、本机在右）；
        画线时按**层级**选端口（正向：源右边缘 → 目标左边缘，反向走返回弧）。"""
        html = self._html()
        auto = html[html.index("function relAutoPos("):html.index("function relLinkClass(")]
        self.assertIn("x: Math.round(22 + (r - minRank) * span)", auto,
                      "列号 = 层级（上游左、本机右、下游更右）")
        rank = html[html.index("function relComputeRank("):html.index("function relLinkClass(")]
        self.assertIn("rank[v] = rank[u] - 1", rank, "上游（送到本机）层级递减 → 源在左")
        self.assertIn("rank[v] = rank[u] + 1", rank, "下游（本机送出）层级递增 → 去向在右")
        self.assertIn("if(rank[v] === undefined)", rank, "同层/回边不覆盖已定层级")
        draw = html[html.index("function relDrawLinks("):html.index("function relNameOf(")]
        i = draw.index("else if(rb > ra){")
        self.assertIn("relPath(a.x + REL_W, y1, b.x, y2)", draw[i:i + 90],
                      "正向边从源右边缘画到目标左边缘")
        i = draw.index("} else {")
        self.assertIn("relReturnPath(a.x, y1, b.x + REL_W, y2)", draw[i:i + 110],
                      "反向边（回环）走返回弧，不从右侧钻进去")
        self.assertIn("marker-end=\"url(#' + relArrowId(l)", draw, "箭头指向接收方")

    def test_legend_states_the_colour_rule(self) -> None:
        html = self._html()
        self.assertIn("颜色 = 链路种类，同一条链路永远同色", html)
        self.assertIn("失效路由（身份不存在）不上图", html)
        self.assertIn("清理失效路由", html)




class Test20261104V226(unittest.TestCase):
    """★ 2026-11-04（PC v2.26）：真机 v2.25 诊断包里的 4 条。

    1) 监控页仍有两条指向旧身份的幽灵路由（b1fbc20c / 5e65addb），只空转；
    2) 手机 2.14 明明发 vol，PC 却显示"未上报"（键没对齐）；
    3) 蓝牙 4 条交叉命名，端点按子串匹配挂到别的设备名下；
    4) 回环提示是英文，用户不知道怎么处置。
    """

    def _node(self):
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode

        cfg = Config()
        cfg.node_id = "20e66011-94c5-4fd2-98ab-6a5c488ed993"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        cfg.smart_link = []
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    def _phone(self, node, first_id="b1fbc20c-b560-4885-8ed4-62296354d54a"):
        """先登记旧身份，再登记当前身份（同一 device_id / 同一 host）→ aliasIds 生效。"""
        node.registry.update({"wl": 1, "id": first_id, "name": "M332BF-e9b8",
                              "audio_port": 47831, "web_port": 0,
                              "device_id": "dev-m332bf"}, "192.168.0.100")
        node.registry.update({"wl": 1, "id": "5e65addb-2edb-47dd-94c8-1398a7a9843e",
                              "name": "M332BF-e9b8", "audio_port": 47831, "web_port": 0,
                              "device_id": "dev-m332bf"}, "192.168.0.100")
        node.registry.update({"wl": 1, "id": "e9b8cc51-f1a3-4aae-9bb9-b2a47bb0797a",
                              "name": "M332BF-e9b8", "audio_port": 47831, "web_port": 0,
                              "device_id": "dev-m332bf"}, "192.168.0.100")
        return node

    # ---- 第 1 条：旧身份路由自动改写 + 合并重复 + 监控页可操作 ----
    def test_ghost_routes_are_rewritten_to_live_identity_and_deduped(self) -> None:
        node = self._node()
        try:
            self._phone(node)
            node.set_routes([
                {"id": "68c622a3", "from": [node.node_id],
                 "to": ["b1fbc20c-b560-4885-8ed4-62296354d54a"]},
                {"id": "4eecefd4", "from": [node.node_id],
                 "to": ["5e65addb-2edb-47dd-94c8-1398a7a9843e"]},
            ], persist=False)
            result = node.normalize_route_identities()
            self.assertGreaterEqual(result["changed"], 1, "旧 id 必须被改写")
            self.assertEqual(len(node.routes), 1,
                             "改写后两条完全相同的路由必须只留一条，否则手机会收到两份")
            self.assertEqual(node.routes[0]["to"], ["dev-m332bf"],
                             "改写目标 = identity 归一的 device_id")
            log = node.status()["identity"]["route_rewrites"]
            self.assertTrue(any(item.get("old") in
                                ("b1fbc20c-b560-4885-8ed4-62296354d54a",
                                 "5e65addb-2edb-47dd-94c8-1398a7a9843e") for item in log),
                            "诊断里能查到被改写的旧 id")
        finally:
            node.stop()

    def test_missing_route_can_be_repaired_to_the_only_online_peer(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "e9b8cc51-f1a3-4aae-9bb9-b2a47bb0797a",
                                  "name": "M332BF-e9b8", "audio_port": 47831, "web_port": 0,
                                  "device_id": "dev-m332bf"}, "192.168.0.100")
            node.set_routes([{"id": "ghost", "from": [node.node_id],
                              "to": ["b1fbc20c-b560-4885-8ed4-62296354d54a"]}],
                            persist=False)
            result = node.repair_route_identity("ghost")
            self.assertTrue(result.get("ok"), "只有一台在线对端时必须允许一键修复")
            self.assertEqual(result.get("target"), "dev-m332bf", "改写目标是归一后的 device_id")
            self.assertEqual(node.routes[0]["to"], ["dev-m332bf"])
            self.assertTrue(any(item.get("field") == "manual-repair"
                                for item in node.status()["identity"]["route_rewrites"]),
                            "人工确认的修复也要记进诊断")
        finally:
            node.stop()

    def test_monitor_missing_route_is_actionable_not_a_bare_unreachable_row(self) -> None:
        from audiomixing.webui import AudioMixingApi

        node = self._node()
        try:
            node.set_routes([{"id": "ghost", "from": [node.node_id],
                              "to": ["dead-beef-0000-0000-000000000000"]}],
                            persist=False)
            # missing 要连续 60 秒解析不到才成立（见 _refresh_route_staleness 注释）。
            node._unknown_route_since["ghost"] = time.monotonic() - 61
            node._refresh_route_staleness()
            flows = AudioMixingApi(node).monitor().get("flows") or []
            ghost = [f for f in flows if str(f.get("route_id")) == "ghost"]
            self.assertTrue(ghost, "失效路由仍要出现在监控页（否则用户没法清）")
            row = ghost[0]
            self.assertNotIn("不可达", str(row.get("state")), "不允许裸的不可达行")
            self.assertIn("可清理", str(row.get("state")), "必须明确告诉用户能清理")
            self.assertEqual(row.get("action"), "清理失效路由", "按钮必须写清是清理")
            self.assertTrue(row.get("disconnect"), "必须有可点击的清理动作")
            self.assertIn("不会动 Windows 蓝牙配对", str(row.get("hint")),
                          "清理只删本机配置，表达要准确")
        finally:
            node.stop()

    # ---- 第 2 条：vol 在 identity 改名后仍能命中 ----
    def test_peer_volume_follows_alias_identity_and_reports_hit_key(self) -> None:
        node = self._node()
        try:
            self._phone(node)
            node._handled_stats_at = 0.0
            node._control_messages = lambda: [{
                "_type": "STATS", "_at": 1.0,
                "_from": "192.168.0.100:47831",
                # stats 用**当前** node id，而列表里的记录 node_id 仍是先登记的旧 id。
                "node": "e9b8cc51-f1a3-4aae-9bb9-b2a47bb0797a",
                "vol": 0.25, "name": "M332BF-e9b8",
            }]
            node._handle_peer_requests()
            peers = node.status()["peers"]
            phone = [p for p in peers if str(p.get("host")) == "192.168.0.100"][0]
            self.assertEqual(phone["media_vol"], 0.25,
                             "identity 改名后 vol 仍必须命中（不能显示未上报）")
            self.assertTrue(phone["media_vol_key"], "诊断必须写出命中的键")
            self.assertIn(phone["media_vol_key"], phone["media_vol_keys"],
                          "命中键必须包含在查过的键列表里")
            self.assertTrue(phone["media_vol_keys"], "诊断要列出查过的全部键")
            # host 兜底：只写 host 键也要能取到（stats 的 node 完全陌生时靠它）。
            node._peer_volume.clear()
            node._peer_volume[str(phone["host"])] = (0.3, time.monotonic())
            self.assertEqual(node._peer_media_volume_for_node(phone["node_id"]), 0.3,
                             "只按 host 存的 vol 也必须能查到")
            dbg = node.status()["peer_volume_debug"]
            self.assertEqual(dbg["last"]["vol"], 0.25, "最近一次原始上报要能查到")
            self.assertIn("192.168.0.100", dbg["last"]["keys"], "原始上报的存储键要能查到")
        finally:
            node.stop()

    # ---- 第 3 条：蓝牙同 MAC 合并、端点按词边界匹配、只清本程序记录 ----
    def test_bt_merge_same_address_and_keep_different_names_apart(self) -> None:
        from audiomixing.bluetooth import merge_paired_devices

        rows = [
            {"id": "old-1", "address": "AA:BB:CC:DD:EE:FF", "name": "瘾控",
             "state": "Closed", "held": False, "endpoint": False, "hfp": False, "link": "off"},
            {"id": "new-1", "address": "aa-bb-cc-dd-ee-ff", "name": "瘾控",
             "state": "Closed", "held": False, "endpoint": False, "hfp": False, "link": "off"},
            {"id": "pad", "address": "11:22:33:44:55:66", "name": "瘾控的Redmi Pad SE",
             "state": "Closed", "held": False, "endpoint": False, "hfp": False, "link": "off"},
        ]
        merged = merge_paired_devices(rows)
        self.assertEqual(len(merged), 2, "同 MAC 的两条必须合并；不同名字/地址不合并")
        group = [m for m in merged if m["name"] == "瘾控"][0]
        self.assertEqual(group["merged_count"], 2)
        self.assertEqual(sorted(group["ids"]), ["new-1", "old-1"])
        self.assertFalse(any(m["name"] == "瘾控的Redmi Pad SE" and m.get("merged_count", 1) > 1
                             for m in merged), "名字包含关系不是强证据，不能误合并")

    def test_bt_endpoint_matching_uses_a_word_boundary_not_substring(self) -> None:
        text = (ROOT / "audiomixing" / "bluetooth.py").read_text(encoding="utf-8")
        self.assertNotIn('-like ("*" + $d.Name + "*")', text,
                         "子串匹配会把 '瘾控的Redmi Pad SE A2DP SNK' 挂到 '瘾控' 名下")
        self.assertIn("$pattern = '(^|\\s|\\()'", text, "必须按词边界匹配端点名")
        self.assertIn('(\\s|\\)|$)', text)

    def test_bt_cleanup_is_honest_about_windows_pairings(self) -> None:
        import tempfile
        from pathlib import Path
        from audiomixing import bluetooth

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            # 助手状态文件名 = _key(device_id)（sha1 前 12 位），不是原始 id。
            (state / ("%s.keep" % bluetooth._key("old-1"))).write_text("", encoding="utf-8")
            (state / ("%s.status" % bluetooth._key("new-1"))).write_text("{}", encoding="utf-8")
            with mock.patch.object(bluetooth, "_STATE_DIR", state), \
                    mock.patch.object(bluetooth, "list_devices",
                                      return_value=[{"id": "new-1", "name": "瘾控"}]):
                result = bluetooth.cleanup_stale_records()
            self.assertEqual(result["removed"], 1, "只清当前列表里已不存在的旧记录")
            self.assertIn("Windows", result["note"], "必须写明 Windows 配对要用户自己去删")
            self.assertIn("设置", result["note"], "给出具体去处")

    # ---- 第 4 条：英文回环提示改中文 ----
    def test_echo_hint_is_chinese_with_an_action(self) -> None:
        text = (ROOT / "audiomixing" / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("pick a different output to avoid the echo", text,
                         "不能继续给用户英文提示")
        i = text.index("同时也是本机默认的采集设备")
        body = text[i:i + 240]
        self.assertIn("换一个输出设备", body, "要给处置建议")
        self.assertIn("没有开采集", body, "要说明可忽略的条件")

class TestV27Regressions(unittest.TestCase):
    """★ v2.27 回归：音量只乘一次、重复路由收敛、广播只走频道、关系合并/拆分、自启隔离。"""

    def _node(self):
        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        node = AudioMixingNode(cfg, audio_port=free_udp_port(),
                               discovery_port=free_udp_port(), simulate=True)
        node.devices = []
        return node

    # ---- Bug-1：手机声明「采集已在音量之后」→ 电脑不能再乘 vol ----
    def test_declared_capture_volume_is_not_multiplied_again(self) -> None:
        node = self._node()
        try:
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-1", "name": "手机", "_type": "STATS",
                "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                "vol": 0.5, "vol_in_capture": True,
            })
            node._handle_peer_requests()
            self.assertTrue(node._peer_vol_declared.get("phone-1"),
                            "手机 2.16 的声明必须落地")
            factor, note = node._volume_factor_for(
                0.5, node._volume_verdict_for(["phone-1"]))
            self.assertEqual(factor, 1.0, "采集端已带音量 → 电脑的乘数必须是 1.0（不再平方衰减）")
            self.assertIn("已衰减", note)
            # 对照组：没声明的老手机仍是 vol 乘一次（行为不变）
            self.assertEqual(node._volume_factor_for(0.5, None)[0], 0.5)
        finally:
            node.stop()

    # ---- 2026-11-08（用户方案）：外放档 / 静音档都由手机处理一次，电脑恒不叠乘 ----
    def test_external_and_silent_send_modes_are_never_multiplied_again(self) -> None:
        node = self._node()
        try:
            for source, in_capture in (("external", False), ("silent", True)):
                node.receiver.control_messages.append({
                    "t": "stats", "node": "phone-mode", "name": "手机", "_type": "STATS",
                    "_from": "198.51.100.12:47831", "_at": time.monotonic(),
                    "vol": 0.5, "vol_in_capture": in_capture,
                    "vol_mode": source, "vol_source": source,
                    "vol_policy": "internal_only",
                })
                node._handle_peer_requests()
                self.assertEqual(node._peer_vol_source.get("phone-mode"), source)
                self.assertTrue(node._peer_vol_declared.get("phone-mode"))
                factor, note = node._volume_factor_for(
                    0.5, node._volume_verdict_for(["phone-mode"]), source)
                self.assertEqual(factor, 1.0,
                                 "%s 档手机已经处理过一次，电脑必须恒为 1.0" % source)
                self.assertIn("不叠加", note)
            # 切档后声明和字段必须同步变化（模拟手机端下一轮统计包）
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-mode", "name": "手机", "_type": "STATS",
                "_from": "198.51.100.12:47831", "_at": time.monotonic(),
                "vol": 0.5, "vol_in_capture": False,
                "vol_mode": "external", "vol_source": "external",
                "vol_policy": "last_nonzero",
            })
            node._handle_peer_requests()
            self.assertEqual(node._peer_vol_source.get("phone-mode"), "external")
            self.assertEqual(node._peer_vol_policy.get("phone-mode"), "last_nonzero")
        finally:
            node.stop()

    def test_silent_volume_policy_setting_pushes_a_control_packet(self) -> None:
        node = self._node()
        try:
            self.assertEqual(node.config.phone_silent_vol_policy, "internal_only",
                             "默认必须是 internal_only（静音档 STREAM_MUSIC=0 无信息量）")
            self.assertEqual(node.set_phone_silent_vol_policy("last_nonzero"), "last_nonzero")
            self.assertEqual(node.config.phone_silent_vol_policy, "last_nonzero")
            self.assertGreaterEqual(node._silent_policy_seq, 1)
            self.assertEqual(node.set_phone_silent_vol_policy("bogus"), "internal_only")
        finally:
            node.stop()

    def test_peer_row_exposes_send_mode_and_multiplier_source(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "phone-row", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.13")
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-row", "name": "手机", "_type": "STATS",
                "_from": "198.51.100.13:47831", "_at": time.monotonic(),
                "vol": 0.25, "vol_in_capture": True,
                "vol_mode": "silent", "vol_source": "silent",
                "vol_policy": "internal_only",
            })
            node._handle_peer_requests()
            row = next(p for p in node._peers_with_media_volume()
                       if p["node_id"] == "phone-row")
            self.assertEqual(row["vol_source"], "silent")
            self.assertEqual(row["vol_mode"], "silent")
            self.assertEqual(row["vol_policy"], "internal_only")
            self.assertTrue(row["vol_in_capture"], "静音档必须报 true")
            self.assertEqual(row["volume_factor"], 1.0)
            self.assertIn("静音档", row["volume_note"])
        finally:
            node.stop()

    # ---- Bug-3.1：同端点重复路由收敛，删一次就干净 ----
    def test_duplicate_routes_collapse_and_one_delete_clears_them(self) -> None:
        node = self._node()
        try:
            node.set_routes([
                {"id": "old", "from": ["peer-1"], "to": ["self"]},
                {"id": "new", "from": ["peer-1"], "to": ["self"]},
            ], persist=False)
            self.assertEqual([r["id"] for r in node.routes], ["old"],
                             "语义相同的两条路由只能留一条")
            node.set_routes([], persist=False)
            self.assertEqual(node.routes, [], "取消收听点一次就该清干净")
        finally:
            node.stop()

    # ---- Bug-3.2：广播时只认频道「只听」，不被具名/自动路由绕过 ----
    def test_broadcast_peer_only_uses_the_channel_route(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "phone-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            node.set_routes([
                {"id": "chan", "from": ["*"], "to": ["self"]},
                {"id": "named", "from": ["phone-1"], "to": ["self"]},
            ], persist=False)
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-1", "_type": "STATS",
                "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                "send_mode": 1, "broadcast": True,
            })
            node._handle_peer_requests()
            self.assertEqual(node.routes_for_sender("phone-1"), {"chan"},
                             "广播时必须只走频道那条；残留具名路由不能再放一遍")
            # 没开「只听」时，广播流一条路由都不该命中（否则就是「没开只听也能听到」）
            node.set_routes([{"id": "named", "from": ["phone-1"], "to": ["self"]}],
                            persist=False)
            self.assertEqual(node.routes_for_sender("phone-1"), set(),
                             "广播没有频道路由 → 静音，不能借具名路由偷放")
        finally:
            node.stop()

    # ---- ★ 2026-11-06（Q2）：广播 → 只听，必须有明确证据，不能靠"猜" ----
    def test_broadcast_beacon_alone_is_accepted_by_a_listening_node(self) -> None:
        """手机的 STATS 可能只发给"它正在送的目标"，切换瞬间本机可能还拿着旧的
        "点对点"声明。信标里自报的 broadcast 必须能单独把准入打开。"""
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "phone-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0,
                                  "send_mode": 1, "broadcast": True}, "198.51.100.10")
            node.set_routes([{"id": "chan", "from": ["*"], "to": ["self"]}],
                            persist=False)
            # 5 秒前的旧控制消息说"点对点"，信标是刚收到的"广播" → 取信标
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-1", "_type": "STATS",
                "_from": "198.51.100.10:47831", "_at": time.monotonic() - 5.0,
                "send_mode": 1, "broadcast": False,
            })
            node._handle_peer_requests()
            self.assertTrue(node._stream_is_accepted("phone-1"),
                            "信标明确写着广播 → 只听必须放行（不能拿旧点对点声明拒收）")
            # 同一台设备改成点对点且没有具名路由 → 必须拒收（只听≠放行点对点）
            node.registry.update({"wl": 1, "id": "phone-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0,
                                  "send_mode": 1, "broadcast": False}, "198.51.100.10")
            self.assertFalse(node._stream_is_accepted("phone-1"),
                             "点对点没有具名路由时必须拦住")
        finally:
            node.stop()

    def test_send_mode_declaration_matches_through_identity_aliases(self) -> None:
        """现场做过身份归一（旧 id → 新 id）。声明按旧 id 存、音频包按新 id 查时，
        旧实现查不到就退回"按点对点拒收"。现在必须能通过别名互相命中。"""
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "phone-new", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            peer = node.registry.get("phone-new")
            node.registry._add_alias(peer, "phone-old")
            node.set_routes([{"id": "chan", "from": ["*"], "to": ["self"]}],
                            persist=False)
            node.receiver.control_messages.append({
                "t": "stats", "node": "phone-old", "_type": "STATS",
                "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                "send_mode": 1, "broadcast": True,
            })
            node._handle_peer_requests()
            self.assertTrue(node._stream_is_accepted("phone-new"),
                            "按新 id 查也必须看到旧 id 存下的广播声明")
            self.assertTrue(node._stream_is_accepted("phone-old"))
        finally:
            node.stop()

    # ---- ★ 2026-11-06（Q5）：蓝牙卡片"未到"与布尔 True 不能同时出现 ----
    def test_closed_bt_channel_never_reports_media_link_true(self) -> None:
        from audiomixing import bluetooth

        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            key = bluetooth._key("dev-1")
            (state / ("%s.keep" % key)).write_text("", encoding="utf-8")
            (state / ("%s.pid" % key)).write_text(str(os.getpid()), encoding="utf-8")
            (state / ("%s.status" % key)).write_text("open:Closed", encoding="utf-8")
            row = {"id": "dev-1", "name": "瘾控", "link": "off", "held": False,
                   "endpoint": True, "state": "Closed"}
            with mock.patch.object(bluetooth, "_STATE_DIR", state), \
                    mock.patch.object(bluetooth, "_endpoint_state_from_registry",
                                      return_value=False):
                out = bluetooth._apply_live_states([dict(row)])[0]
            self.assertEqual(out["link"], "waiting")
            self.assertFalse(out["endpoint"],
                             "文案说'媒体音频未到'时，手机媒体链路不能还是 True")
            self.assertTrue(out["held"], "辅助进程还在，这个布尔可以保留")
            with mock.patch.object(bluetooth, "_STATE_DIR", state), \
                    mock.patch.object(bluetooth, "_endpoint_state_from_registry",
                                      return_value=True):
                out = bluetooth._apply_live_states([dict(row)])[0]
            self.assertEqual(out["link"], "connected",
                             "注册表里端点 active → 媒体音频确实到了，必须报连接")
            self.assertTrue(out["endpoint"])

    # ---- 第 6 条：拖动合并 / 拆开 ----
    def test_relation_merge_and_split_round_trip(self) -> None:
        node = self._node()
        try:
            node.registry.update({"wl": 1, "id": "peer-1", "name": "手机",
                                  "audio_port": 47831, "web_port": 0}, "198.51.100.10")
            out = node.relation_merge("bt:手机蓝牙", "peer-1")
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(out.get("kind"), "relation")
            self.assertEqual(node.config.relation_merges[-1]["peer"], "peer-1")
            self.assertEqual(node.config.relation_merges[-1]["bt"], "手机蓝牙")
            self.assertIn("relation_merges", node.status().get("identity", {}),
                          "状态接口要把合并结果交给页面回填")
            out = node.relation_split("bt:手机蓝牙")
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(node.config.relation_merges, [])
        finally:
            node.stop()

    # ---- Bug-3.1：手动删掉收听路由后，智连必须一起关，下一包统计不能再建回来 ----
    def test_deleting_a_listen_route_turns_smart_link_off(self) -> None:
        from audiomixing import webui

        node = self._node()
        try:
            node.config.smart_link = ["peer-1"]
            node.set_routes([{"id": "mine", "from": ["peer-1"], "to": ["self"]}],
                            persist=False)
            api = webui.AudioMixingApi.__new__(webui.AudioMixingApi)
            api.node = node
            api._note_manual_auto_close(list(node.routes))
            self.assertNotIn("peer-1", node.config.smart_link,
                             "删掉收听路由必须同时关智连，否则 2 秒后又被自动建回来")
            node.set_routes([], persist=False)      # 页面上的删除动作
            node.receiver.control_messages.append({
                "t": "stats", "node": "peer-1", "_type": "STATS",
                "_from": "198.51.100.10:47831", "_at": time.monotonic(),
                "want_listen": True,
            })
            node._handle_peer_requests()
            self.assertEqual(node.routes, [],
                             "智连关掉后，对端继续请求也不能再偷偷打开收听")
        finally:
            node.stop()

    # ---- 新 UI：开机自启动默认关，开启才写 HKCU Run，关闭要删值 ----
    def test_autostart_only_touches_the_run_key_when_enabled(self) -> None:
        node = self._node()
        try:
            self.assertFalse(node.config.launch_at_login)
            self.assertFalse(node.config.to_json()["launch_at_login"])
        finally:
            node.stop()

        from audiomixing import webui

        written: dict[str, str] = {}

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _open_key(root, path, reserved=0, access=0):
            return _Key()

        # ★ 2026-11-06（Q4）：Windows 注册表值名**不区分大小写**。旧的 fake 用普通
        #   dict（区分大小写），于是 `DeleteValue("audiomixing")` 被当成没命中、
        #   真机上却会把刚写进去的 `AudioMixing` 一起删掉。这里按小写键模拟真机，
        #   旧实现会在这条用例上直接报 [WinError 2]。
        def _set_value_ex(key, name, reserved, typ, value):
            written[str(name).lower()] = value

        def _delete_value(key, name):
            k = str(name).lower()
            if k not in written:
                raise FileNotFoundError(name)
            del written[k]

        def _query_value_ex(key, name):
            k = str(name).lower()
            if k not in written:
                raise FileNotFoundError(name)
            return written[k], 1

        fake = types.SimpleNamespace(
            HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, KEY_QUERY_VALUE=4, REG_SZ=1,
            OpenKey=_open_key, SetValueEx=_set_value_ex, DeleteValue=_delete_value,
            QueryValueEx=_query_value_ex)
        with mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch.object(webui.sys, "platform", "win32"):
            ok, err = webui.set_windows_autostart(True)
            self.assertTrue(ok, err)
            self.assertIn("serve", written[webui._AUTOSTART_VALUE.lower()],
                          "登录后启动必须用 serve（后台就绪，不弹面板）")
            ok, err = webui.set_windows_autostart(False)
            self.assertTrue(ok, err)
            self.assertEqual(written, {}, "关闭自启动必须删掉 Run 值")

    # ---- ★ 2026-11-06（第 5 条）：自启动必须写外层 exe，不能写 onefile 临时目录 ----
    def test_autostart_uses_the_outer_exe_not_the_onefile_temp_python(self) -> None:
        """真机根因：Nuitka 不设 sys.frozen，旧代码走开发分支写进 onefile 临时目录。

        临时目录退出即删，所以注册表里那条 `...\\onefile_xxx\\python.exe ... launcher.py`
        下次开机必然不存在 —— 用户勾了自启动、开机不起。这里验证修复后写的是
        **外层 exe**，且带引号、带 `serve`。
        """
        from audiomixing import webui

        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "Audio Mixing v2.29.exe"
            exe.write_bytes(b"fake")
            ghost = pathlib.Path(tmp) / "onefile_123" / "python.exe"
            ghost.parent.mkdir()
            ghost.write_bytes(b"fake")

            class _Compiled:
                original_argv0 = str(exe)

            webui.__compiled__ = _Compiled()
            try:
                with mock.patch.object(sys, "executable", str(ghost)), \
                        mock.patch.object(sys, "argv", [str(exe), "serve"]), \
                        mock.patch.dict(os.environ, {"NUITKA_ONEFILE_PARENT": ""}):
                    self.assertEqual(webui._program_exe_path(), str(exe),
                                     "必须认出外层的 exe，而不是临时 python.exe")
                    cmd = webui._autostart_command()
                    self.assertEqual(cmd, '"%s" serve' % exe)
                    self.assertNotIn("onefile_", cmd)
                    self.assertNotIn("python.exe", cmd)
            finally:
                del webui.__compiled__

    # ---- ★ 2026-11-06（第 3 条）：采集"有数据但一直静音"要如实报出来 ----
    def test_a_device_that_streams_only_silence_is_reported_as_silent(self) -> None:
        """蓝牙端点"连上了、包在走、全是 0"以前显示健康，用户听到的是没声。"""
        from audiomixing.audio.capture import SILENT_AFTER_S

        node = self._node()
        try:
            state, reason, definite = node._capture_data_state({
                "name": "麦克风 (手机 A2DP SNK)",
                "blocks_read": 120, "last_data_age_s": 0.2, "alive_for_s": 60.0,
                "peak": 0.0, "silent_for_s": SILENT_AFTER_S + 5.0, "silent": True,
            })
            self.assertEqual(state, "silent")
            self.assertIn("没有电平", reason)
            self.assertFalse(definite, "用户可能只是暂停了播放，不能据此自动换设备")
            state2, _reason2, _d2 = node._capture_data_state({
                "name": "扬声器", "blocks_read": 120, "last_data_age_s": 0.2,
                "alive_for_s": 60.0, "peak": 0.4, "silent_for_s": 0.2, "silent": False,
            })
            self.assertEqual(state2, "ok")
        finally:
            node.stop()


# --------------------------------------------------------------------------
# ★ 2026-11-06（UI 残留）：把"肉眼能看出来的错误"变成门禁。
# --------------------------------------------------------------------------

def _load_ui_gate():
    path = ROOT / "tools" / "check_ui_residue.py"
    spec = importlib.util.spec_from_file_location("check_ui_residue", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestUiResidueGate(unittest.TestCase):
    def test_test_node_names_are_rejected(self) -> None:
        gate = _load_ui_gate()
        for bad in ("自启动复验", "test", "probe", "selftest", "peer-1",
                    "self-node", "demo", "example", "我的 test 设备"):
            self.assertIsNotNone(gate.bad_name(bad, set()), bad)
        for good in ("2024TSJ", "我的手机", "OPPO Enco Air4", "瘾控的Redmi Pad SE"):
            self.assertIsNone(gate.bad_name(good, set()), good)
        # 白名单是精确名字：用户明确允许的测试名可以放行（--allow）。
        self.assertIsNone(gate.bad_name("test-tone", {"test-tone"}))

    def test_shipped_ui_has_no_residue_and_hides_the_local_card(self) -> None:
        gate = _load_ui_gate()
        problems = gate.check_ui_source(set())
        self.assertEqual(problems, [], "页面源码里还有 UI 残留：%r" % problems)

    def test_every_self_identity_is_filtered_from_the_lan_list(self) -> None:
        """本机不能靠"只比 node_id"逃过过滤：device_id/别名/地址都要算。"""
        from audiomixing import webui

        keys = {"node-x", "dev-x", "inst-x", "alias-x", "192.168.0.105"}
        for item in ({"node_id": "node-x"}, {"device_id": "dev-x"},
                     {"instance_id": "inst-x"}, {"alias_ids": ["alias-x"]},
                     {"hosts": ["192.168.0.105"]}, {"host": "192.168.0.105"},
                     {"local": True}):
            self.assertTrue(webui._peer_is_self(item, keys), item)
        self.assertFalse(webui._peer_is_self({"node_id": "other", "host": "192.168.0.100"}, keys))

    def test_two_nodes_on_one_machine_are_not_hidden(self) -> None:
        """★ 双节点自检回归：同一台机器上的两个节点 host 相同，但不是本机。

        只按地址判会把 selftest 的 A/B 互相过滤，控制台 peers 直接变成 []。
        """
        from audiomixing import webui

        keys = {"self-node-id", "192.168.0.105", "127.0.0.1"}
        self.assertFalse(webui._peer_is_self(
            {"node_id": "peer-a", "host": "127.0.0.1"}, keys))
        self.assertFalse(webui._peer_is_self(
            {"node_id": "peer-b", "host": "192.168.0.105",
             "device_id": "peer-dev"}, keys))

    def test_self_identity_collects_config_aliases(self) -> None:
        from audiomixing import webui

        config = types.SimpleNamespace(device_id="dev-x", instance_id="inst-x",
                                       alias_ids=["alias-old"])
        node = types.SimpleNamespace(node_id="node-x", config=config)
        keys = webui._self_identity_keys(node)
        self.assertIn("node-x", keys)
        self.assertIn("dev-x", keys)
        self.assertIn("inst-x", keys)
        self.assertIn("alias-old", keys)
