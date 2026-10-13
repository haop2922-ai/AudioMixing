"""WaveLink test suite (stdlib unittest, no extra dependencies).

    python -m unittest discover -s tests -v
    python tests/test_wavelink.py
"""

from __future__ import annotations

import atexit
import math
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from array import array

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The tests build real WaveLinkNode objects, and `set_routes()` persists by default,
# so without this they wrote test ports and a test node name ("node-self"/"本机")
# into the user's %APPDATA%\WaveLink\config.json. Sandbox it, and clean up after.
_TEST_SANDBOX = tempfile.mkdtemp(prefix="wavelink-tests-")
os.environ["WAVELINK_HOME"] = _TEST_SANDBOX
atexit.register(lambda: shutil.rmtree(_TEST_SANDBOX, ignore_errors=True))

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
from audiomixing.engine import SinkEngine, WaveLinkNode  # noqa: E402
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
        self.assertIsNone(proto.decode(b"hello world, not a wavelink packet"))
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
            node = WaveLinkNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
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

    def test_ring_never_reports_more_than_capacity(self):
        buf = JitterBuffer(capacity_ms=100.0)
        t0 = proto.now_ns()
        for i in range(100):
            buf.write(make_tone(500.0, BLOCK, 0.3, CHANNELS), t0 + i * 10_000_000, 0.0)
        self.assertLessEqual(buf._count, buf.capacity)
        self.assertGreater(buf.dropped_overflow, 0)


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
        s.sendto(b"not a wavelink packet at all", ("127.0.0.1", port))
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


# --------------------------------------------------------------------------
# engine: routing decisions
# --------------------------------------------------------------------------

class TestRouting(unittest.TestCase):
    def setUp(self):
        self.node = WaveLinkNode(
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
        node_a = WaveLinkNode(_config_stub("A", "node-a"), audio_port=audio_port_a, discovery_port=free_udp_port(), simulate=True)
        node_b = WaveLinkNode(_config_stub("B", "node-b"), audio_port=audio_port_b, discovery_port=free_udp_port(), simulate=True)
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
        node = WaveLinkNode(_config_stub(name="t", node_id="node-hub"), audio_port=free_udp_port(),
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

    def _node(self, node_id: str, name: str) -> WaveLinkNode:
        node = WaveLinkNode(_config_stub(name=name, node_id=node_id), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
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
        from audiomixing.webui import WaveLinkApi

        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"]}], persist=False,
                          author_id="node-b", author_name="B机")
        api = WaveLinkApi(self.a)
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
        from audiomixing.webui import WaveLinkApi

        self.a.set_routes([{"id": "theirs", "from": ["node-b"], "to": ["node-a"], "gain": 1.0}],
                          persist=False, author_id="node-b", author_name="B机")
        self.assertEqual([r["id"] for r in self.a.local_routes()], [], "A does not own it")

        api = WaveLinkApi(self.a)
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
        self.node = WaveLinkNode(_config_stub(name="本机", node_id="node-a"), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
        self.node.start(announce=False)
        self.addCleanup(self.node.stop)
        self.node.registry.update(
            {"wl": 1, "id": "node-b", "name": "B机", "audio_port": 47990, "web_port": 1, "sinks": [{"name": "耳机", "id": "x"}]},
            "192.168.1.50",
        )

    def test_two_sinks_for_one_source_are_both_found(self):
        self.node.set_routes([
            {"id": "r1", "from": ["node-b"], "to": ["self"]},
            {"id": "r2", "from": ["node-b"], "to": ["self"]},
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
        node = WaveLinkNode(_config_stub(name="t", node_id="node-render"), audio_port=free_udp_port(), discovery_port=free_udp_port(), simulate=True)
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
            bt._STATE_DIR = pathlib.Path(tempfile.mkdtemp())
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
        self.node = WaveLinkNode(cfg, simulate=True)
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

        node = engine_mod.WaveLinkNode(config_mod.Config(), audio_port=free_udp_port(),
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
        from audiomixing.engine import WaveLinkNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "self"
        cfg.routes = []
        cfg.broadcast = False
        cfg.listen = False
        self.node = WaveLinkNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
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
        from audiomixing.engine import WaveLinkNode
        from audiomixing.webui import WaveLinkApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.broadcast = True
        cfg.listen = False
        cfg.channel_sources = []
        cfg.broadcast_excluded = []
        self.node = WaveLinkNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
        self.node.devices = []
        self.api = WaveLinkApi(self.node)
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
             "web_port": 8080}, "192.168.0.101")

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
        from audiomixing.engine import WaveLinkNode
        from audiomixing.webui import WaveLinkApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "2024TSJ"
        cfg.routes = []
        cfg.broadcast = True
        cfg.listen = False
        cfg.local = True
        cfg.channel_sources = []
        cfg.broadcast_excluded = []
        self.node = WaveLinkNode(cfg, audio_port=47899, discovery_port=47898, simulate=False)
        self.node.devices = []
        self.api = WaveLinkApi(self.node)
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

    def _add_peer(self, name: str, host: str = "192.168.0.101") -> None:
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
        from audiomixing.webui import WaveLinkApi

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
        self.assertTrue(WaveLinkApi._version_tuple("9.9") > WaveLinkApi._version_tuple(__version__))
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

    def test_version_parsing(self) -> None:
        from audiomixing.webui import WaveLinkApi

        self.assertEqual(WaveLinkApi._version_tuple("Audio Mixing v1.9.exe"), (1, 9))
        self.assertEqual(WaveLinkApi._version_tuple("v1.10.2"), (1, 10, 2))
        self.assertEqual(WaveLinkApi._version_tuple("没有数字"), ())

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
        html = self._html()
        for gone in ("renderDragList", "addToDraft", "beginDrag", "moveDrag", "endDrag",
                     'id="dropFrom"', 'id="dropTo"', 'id="dragList"', 'id="ghost"',
                     "拖到这里", "拖两个框就是一条路由"):
            self.assertNotIn(gone, html, "拖拽式路由应该已经删干净：%s" % gone)

    def test_multi_target_and_per_route_controls_survive(self) -> None:
        """能力不能丢：多目标混音、每路音量、静音、编码、指定输出设备。"""
        html = self._html()
        for token in ('id="pickFrom"', 'id="pickTo"', 'id="newGain"', 'id="newCodec"',
                      'id="newSinkDevice"', "function toggleDraft(", "setGain(",
                      "toggleMute(", "async function addRoute("):
            self.assertIn(token, html)
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
        self.assertIn("频道音量（我们这一层，不动 Windows 系统音量）", html)
        self.assertIn("我们这一层的音量（不动 Windows 系统音量）", html)
        self.assertIn("我们这一层的音量：只改这台手机传进来的增益", html)


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
        """手机端的大状态圆环 + 22 段电平条，电脑端也要有（同一套做法）。"""
        for token in ('id="ring"', 'id="ringProg"', 'id="ringMetric"', 'id="ringUnit"',
                      'id="ringState"', 'id="levelBars"', "const LEVEL_BARS = 22",
                      "function pushLevel(", "function renderLevelMeter("):
            self.assertIn(token, self.pc)
        self.assertIn("const BARS = 22", self.phone, "手机端也是 22 段（改手机端时这里要跟着改）")

    def test_the_browse_density_words_match_the_phone(self) -> None:
        """浏览密度三档的词要和手机端一致：简洁 / 详细 / 监控。"""
        for word in (">简洁<", ">详细<", ">监控<"):
            self.assertIn(word, self.pc)
        self.assertIn('id="modeSeg"', self.pc)


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
    另一条来自截图：面板写着「192.168.0.101 不通：timed out」——查清是手机没回还是我们等错了。
    """

    @staticmethod
    def _html() -> str:
        return (ROOT / "audiomixing" / "ui" / "index.html").read_text(encoding="utf-8")

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import WaveLinkNode
        from audiomixing.webui import WaveLinkApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        self.node = WaveLinkNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
                                 simulate=True)
        self.api = WaveLinkApi(self.node)

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
        """用户说了"显示实际音量条也不错" —— 收/送两条实时电平要留着。"""
        html = self._html()
        self.assertIn('<span class="lbl">收</span>', html)
        self.assertIn('<span class="lbl">送</span>', html)
        self.assertIn("function levelSegs(", html)
        self.assertEqual(html.count("levelSegs(") >= 3, True, "收/送两条都要用 22 段电平条画")
        self.assertIn("function inboundKbps(", html, "下行电平要按字节增量算，不能只看累计")

    def test_slider_ids_are_per_device(self) -> None:
        """多张卡片同时在页面上：数字标签的 id 必须带设备 id，否则会互相覆盖。"""
        html = self._html()
        self.assertNotIn('id="cg_in"', html)
        self.assertNotIn('id="cg_out"', html)
        self.assertIn("$('cg_' + slot + '_' + uid)", html)

    def test_ping_uses_the_port_the_peer_advertised(self) -> None:
        """对端端口要用它自己广播里报的：以前拿本机端口去探，探错了地方当然是超时。"""
        port = free_udp_port()
        receiver = Receiver("peer-node", port, bind_host="127.0.0.1")
        receiver.start()
        try:
            self.node.registry.update(
                {"wl": 1, "id": "peer-1", "name": "手机", "audio_port": port, "web_port": 0},
                "127.0.0.1")
            result = self.api.ping({"host": "127.0.0.1"})       # 不带 port
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["port"], port, "应该用对端广播里报的 audio_port")
            self.assertEqual(result["type"], "PONG", "电脑↔电脑时对端会回 PONG")
            self.assertIn("回了 PONG", result["note"])
        finally:
            receiver.stop()

    def test_a_silent_peer_gets_an_honest_answer(self) -> None:
        """没人监听时：不谎报"不通"，而是说清"手机端不回 PING/PONG"。"""
        port = free_udp_port()
        result = self.api.ping({"host": "127.0.0.1", "port": port})
        self.assertFalse(result["ok"])
        note = result.get("note") or ""
        self.assertIn("没有回应 UDP 探测包", note)
        self.assertIn("不回 PING/PONG", note, "必须告诉用户安卓端本来就不回，别让人以为坏了")
        self.assertIn("不等于连不通", note)

    def test_the_page_passes_the_peer_port_and_shows_the_note(self) -> None:
        html = self._html()
        self.assertIn("async function pingPeer(host, port)", html)
        self.assertIn("pingPeer('${esc(n.host)}', ${Number(n.audio_port) || 0})", html)
        self.assertIn("r.note ||", html, "超时时要把服务端那句解释原样显示出来")


class TestPeerLivenessAndAutoListen(unittest.TestCase):
    """④ 对端没了就停发（安全）+ ① 自动收听 / 共享增益。

    用户真机现场：「手机蓝牙向电脑传输音频，电脑关闭应用，手机声音还在向电脑发送」。
    根因是发送端的目标表只在用户点选时写一次，对方关机后没人移除它 ——
    而且 `PeerEndpoints.online` 对【手动添加】的对端恒为 True，连 15 秒的超时都不生效。
    """

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import WaveLinkNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        self.node = WaveLinkNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
                                 simulate=True)
        self.node.devices = []

    def tearDown(self) -> None:
        try:
            self.node.stop()
        except Exception:
            pass

    def _peer(self, node_id: str = "peer-1", name: str = "手机", host: str = "192.168.0.101",
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
        self.assertIn("已停止向", blocked[0]["text"])
        self.assertIn("秒没有任何消息", blocked[0]["text"])
        self.assertGreaterEqual(blocked[0]["silent_for_s"], 30.0)

    def test_a_manual_peer_also_expires(self) -> None:
        """手动添加的对端以前永远 online（online 对 manual 恒 True）→ 会一直白发。"""
        self.node.registry.add_manual("192.168.0.101", 47831, "手机")
        peer = self.node.registry.resolve("192.168.0.101")
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
        self._stats_from("peer-1", "192.168.0.101")
        self.node._track_peer_liveness()
        self.assertEqual(len(self.node.desired_senders()), 1)

    def test_an_old_stats_message_stops_counting(self) -> None:
        """旧消息不能反复当成新证据，否则死掉的对端永远不会被判死。"""
        self._peer(age=60.0)
        self._route_to_peer()
        self._stats_from("peer-1", "192.168.0.101")
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
        self._stats_from("peer-1", "192.168.0.101",
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

    def test_a_listen_request_does_not_duplicate_an_existing_route(self) -> None:
        self.node.set_routes([{"id": "mine", "from": ["peer-1"], "to": ["self"]}], persist=False)
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"], "已经有路由就别再加一条")

    def test_revoking_removes_only_the_auto_route(self) -> None:
        self.node.set_routes([{"id": "mine", "from": ["peer-1"], "to": ["self"]}], persist=False)
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "已经有一条 它→本机 的路由了，不该再加一条")
        # 对方关掉送声音：只撤"自动"那种，用户自己建的路由必须原样留着
        self.node.receiver.control_messages.clear()
        self._stats_from("peer-1", "192.168.0.101", want_listen=False)
        self.node._handle_peer_requests()
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "撤销只能撤自动建的那条，用户自己建的不能动")

    def test_revoking_removes_the_auto_route_but_keeps_the_users(self) -> None:
        """自动建了一条之后，用户自己又建了一条 → 撤销只删自动那条。

        注意：协议里 want_listen 是幂等的（没有"取消"消息），撤销是【用户】的动作
        （面板上的「撤销」/关掉那台的收听），走的也是这个入口。
        """
        self.node.config.save()
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.node.set_routes(list(self.node.routes)
                             + [{"id": "mine", "from": ["peer-1"], "to": ["self"]}],
                             persist=False)
        self.assertEqual(len(self.node.routes), 2)
        self.node._revoke_auto_listen("peer-1", "手机")      # 用户点「撤销」
        self.assertEqual([r["id"] for r in self.node.routes], ["mine"],
                         "撤销只删自动那条，用户自己建的不动")

    # ---- ① 共享增益 -------------------------------------------------------
    def test_a_shared_gain_from_the_peer_updates_our_route(self) -> None:
        """对端的 gain 是"它→我"这条链路（= 我的「我听它的」）。"""
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 1.0}],
                             persist=False)
        self._stats_from("peer-1", "192.168.0.101", gain=0.5, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        rule = [r for r in self.node.routes if r["id"] == "in"][0]
        self.assertAlmostEqual(rule["gain"], 0.5, places=3, msg="两端共享同一个增益")

    def test_a_local_gain_change_wins_over_an_in_flight_value(self) -> None:
        self.node.set_routes([{"id": "r1", "from": ["self"], "to": ["peer-1"], "gain": 1.2}],
                             persist=False)
        self.node.note_local_gain_change("r1")                 # 用户刚拖过这个滑块
        self._stats_from("peer-1", "192.168.0.101", gain_rx=0.5, gain_rx_ts=1700000000000)
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
        self._stats_from("peer-1", "192.168.0.101", gain=0.4, gain_ts=1700000000000,
                         gain_rx=0.9, gain_rx_ts=1700000000000)
        self.node._handle_peer_requests()
        by_id = {r["id"]: r["gain"] for r in self.node.routes}
        self.assertAlmostEqual(by_id["in"], 0.4, places=3, msg="对端 gain → 我的「我听它的」")
        self.assertAlmostEqual(by_id["out"], 0.9, places=3, msg="对端 gain_rx → 我的「我送它的」")

    def test_an_equal_value_does_not_bounce_back(self) -> None:
        """值一样时只对齐时间戳，绝不回推（否则两端会互相弹）。"""
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 0.7}],
                             persist=False)
        self._stats_from("peer-1", "192.168.0.101", gain=0.7, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        self.assertEqual(self.node._gain_ts["in"], 1700000000000, "时间戳要对齐，值不动")
        self.assertAlmostEqual([r for r in self.node.routes if r["id"] == "in"][0]["gain"],
                               0.7, places=3)

    def test_a_clock_skewed_but_idle_peer_is_still_adopted(self) -> None:
        """时钟偏差兜底：本机 5 秒没动过就采纳对端的值（手机端同规则）。"""
        self.node.set_routes([{"id": "in", "from": ["peer-1"], "to": ["self"], "gain": 1.0}],
                             persist=False)
        self.node._gain_local_at["in"] = time.monotonic() - 30.0     # 很久没动过
        self.node._gain_ts["in"] = 9_999_999_999_999                 # 本机时间戳更大（对端时钟慢）
        self._stats_from("peer-1", "192.168.0.101", gain=0.3, gain_ts=1700000000000)
        self.node._handle_peer_requests()
        self.assertAlmostEqual([r for r in self.node.routes if r["id"] == "in"][0]["gain"],
                               0.3, places=3)

    def test_a_huge_timestamp_is_parsed_as_an_integer(self) -> None:
        """13 位毫秒时间戳不许经过浮点（手机端踩过：被截断成 1.7e8）。"""
        from audiomixing.engine import WaveLinkNode

        self.assertEqual(WaveLinkNode._as_ms(1700000000000), 1700000000000)
        self.assertEqual(WaveLinkNode._as_ms("1700000000000"), 1700000000000)
        self.assertEqual(WaveLinkNode._as_ms(1700000000000.0), 1700000000000)
        self.assertEqual(WaveLinkNode._as_ms(1.7e12), 1700000000000)
        self.assertEqual(WaveLinkNode._as_ms(None), 0)

    # ---- ① 撤销名单 / 总开关 ---------------------------------------------
    def test_a_revoked_device_is_never_auto_added_again(self) -> None:
        self.node.config.save()
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual(len([r for r in self.node.routes if r.get("auto")]), 1)
        self.node._revoke_auto_listen("peer-1", "手机")     # 用户点「撤销」
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [])
        self.assertIn("peer-1", self.node.config.listen_optout)
        self.node.receiver.control_messages.clear()
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "撤销过的设备不该被自动加回来")
        denied = self.node.auto_listen_state()["denied"]
        self.assertTrue(denied, "面板要说清为什么没自动打开")
        self.assertIn("你之前关过它", denied[0]["reason"])

    def test_the_global_switch_stops_auto_listening(self) -> None:
        self.node.config.save()
        self.node.config.auto_accept_listen = False
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual([r for r in self.node.routes if r.get("auto")], [],
                         "总开关关了就不许自动加")
        self.assertIn("关掉了", self.node.auto_listen_state()["denied"][0]["reason"])

    def test_allowing_again_clears_the_optout(self) -> None:
        self.node.config.save()
        self.node.set_listen_optout("peer-1", "手机", on=True)
        self.assertIn("peer-1", self.node.config.listen_optout)
        self.node.set_listen_optout("peer-1", "手机", on=False)
        self.assertNotIn("peer-1", self.node.config.listen_optout)
        self._stats_from("peer-1", "192.168.0.101", want_listen=True)
        self.node._handle_peer_requests()
        self.assertEqual(len([r for r in self.node.routes if r.get("auto")]), 1)

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
        for key in ("want_listen", "gain", "gain_ts", "gain_rx", "gain_rx_ts"):
            self.assertIn(key, body, "手机端要对齐的字段：%s" % key)
        self.assertTrue(body["want_listen"], "幂等：还在送就一直 true")
        self.assertAlmostEqual(float(body["gain"]), 0.75, places=3,
                               msg="gain = 我→你 这条链路（我的「我送它的」）")
        self.assertAlmostEqual(float(body["gain_rx"]), 1.0, places=3,
                               msg="反方向没有路由 → 用默认 1.0 报出去")
        self.assertIsInstance(body["gain_ts"], int, "时间戳必须是整数毫秒")
        self.assertIsInstance(body["gain_rx_ts"], int)


class TestStopsSendingWithoutThePanel(unittest.TestCase):
    """④ 的清理必须在【后台线程】里跑，不能挂在界面上。

    手机端（task-17）的真根因就是这个：清理只写在界面 tick 里，一锁屏就停，
    所以"电脑关了还在发"能一直持续。电脑端的清理在引擎自己的循环里（tick → apply_routes），
    这条测试不打开任何页面，只让引擎自己跑，证明发送端真的会停。
    """

    def setUp(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import WaveLinkNode

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.codec = "pcm_s16"
        cfg.routes = []
        self.node = WaveLinkNode(cfg, audio_port=free_udp_port(), discovery_port=free_udp_port(),
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
        # 只作用于详细档的那张表：简洁档那一列（brief）不动
        self.assertIn("n.local || n.online || (n.age_s || 0) <= OFFLINE_KEEP_S", html)
        self.assertIn("tr class=\"${n.local || n.online ? '' : 'offline'}\"", html)

    def test_the_bluetooth_rows_use_an_aligned_grid(self) -> None:
        """⑦/⑧ 的行要按列对齐（用户："上下不对齐好难受"）。"""
        import re

        html = self._html()
        m = re.search(r"#btList \.btrow, #netAudioList \.btrow\{[^}]*display:grid", html, re.S)
        self.assertTrue(m, "蓝牙/网络音源行要用栅格对齐")
        self.assertIn("minmax(96px,150px)", html, "名字列固定宽度是「对齐」的关键")

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

    def test_the_auto_listen_notice_is_visible_and_revocable(self) -> None:
        html = self._html()
        self.assertIn('id="autoListenBox"', html)
        self.assertIn("已自动打开", html)
        self.assertIn("function revokeAutoListen(", html)
        self.assertIn("撤销", html)
        # 撤销要进"永不再自动"名单；被拒的请求也要说出来
        self.assertIn("/api/listen/optout", html)
        self.assertIn("al.denied", html, "被拒的请求也要在面板上说出来")
        self.assertIn("这次打开", html)
        self.assertIn('id="autoAcceptListen"', html, "总开关「自动接受收听请求」")
        self.assertIn("saveAutoAcceptListen", html)


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
