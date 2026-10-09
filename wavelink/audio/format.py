"""Sample-format helpers: interleaved float32 buffers, resampling, mixing.

All internal audio in WaveLink is **interleaved float32 at 48 kHz**. This module
has no I/O and no third-party dependencies, so it is fully unit-testable.

Recovered and rewritten after an editing accident removed several functions; the
tests in tests/test_wavelink.py (resampler vs an exact sinc reference, format
round-trips, tone/chirp generation, best_lag) are what keep this file honest.
"""

from __future__ import annotations

import math
from array import array
from typing import Sequence

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 mono samples per 10 ms frame


def frames_to_samples(frames: int, channels: int = CHANNELS) -> int:
    return frames * channels


def ms_to_frames(ms: float) -> int:
    return int(round(ms * SAMPLE_RATE / 1000.0))


def frames_to_ms(frames: int) -> float:
    return frames * 1000.0 / SAMPLE_RATE


def bytes_per_frame(codec: int) -> int:
    from .proto import CODEC_PCM_F32, CODEC_PCM_S16

    if codec == CODEC_PCM_F32:
        return 4 * CHANNELS
    if codec == CODEC_PCM_S16:
        return 2 * CHANNELS
    return 0  # variable-rate codecs


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def as_float_array(samples) -> array:
    """Normalise any sample container to ``array('f')``.

    The mixer produces numpy arrays while the rest of the code produces
    ``array('f')``. ``array('f', <ndarray>)`` does not do what it looks like (it
    iterates rows / numpy scalars), so numpy goes through ``tobytes()``; anything
    else falls back to ``tolist`` so no numpy scalar ever reaches ``array()``.
    """
    if isinstance(samples, array):
        return samples
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy is bundled
        np = None  # type: ignore[assignment]
    if np is not None and isinstance(samples, np.ndarray):
        flat = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        return array("f", flat.tobytes())
    if hasattr(samples, "tolist"):
        samples = samples.tolist()
    return array("f", samples)


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def peak(samples: Sequence[float]) -> float:
    """Largest absolute sample value."""
    p = 0.0
    for s in samples:
        a = s if s >= 0 else -s
        if a > p:
            p = a
    return p


def rms(samples: Sequence[float]) -> float:
    """Root mean square (average loudness) of a block."""
    if not len(samples):
        return 0.0
    acc = 0.0
    for s in samples:
        acc += s * s
    return math.sqrt(acc / len(samples))


def to_mono(samples: Sequence[float], channels: int = CHANNELS) -> array:
    if channels == 1:
        return as_float_array(samples)
    n = len(samples) // channels
    out = array("f", bytes(4 * n))
    for i in range(n):
        acc = 0.0
        for c in range(channels):
            acc += samples[i * channels + c]
        out[i] = acc / channels
    return out


def trim_leading_silence(samples: Sequence[float], channels: int = CHANNELS, threshold: float = 1e-4) -> int:
    """Return the frame index of the first non-silent frame (for tests/verification)."""
    n = len(samples) // channels
    for i in range(n):
        for c in range(channels):
            if abs(samples[i * channels + c]) > threshold:
                return i
    return n


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Normalised cross-correlation of two equal-length mono signals."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    sa = sb = sab = 0.0
    for i in range(n):
        sa += a[i] * a[i]
        sb += b[i] * b[i]
        sab += a[i] * b[i]
    d = math.sqrt(sa * sb)
    return 0.0 if d == 0.0 else sab / d


def best_lag(reference: Sequence[float], signal: Sequence[float], max_lag: int) -> tuple[int, float]:
    """Find how far `signal` is delayed relative to `reference`, in samples.

    Convention: a **positive** lag means ``signal`` arrives ``lag`` samples *late*,
    i.e. ``signal[i] ≈ reference[i - lag]``.

    IMPORTANT: this only gives a unique answer for a **broadband** signal (a chirp,
    noise, speech). A pure sine repeats every ``sample_rate / freq`` samples, so many
    different lags correlate equally well - with a 1 kHz tone at 48 kHz every 48th
    lag scores 1.0. Use a chirp when measuring latency; `wavelink doctor` does.
    """
    best = (0, -2.0)
    if len(reference) > len(signal):
        reference = reference[: len(signal)]
    elif len(signal) > len(reference):
        signal = signal[: len(reference)]
    n = len(reference) - 2 * max_lag
    if n <= 16:
        return best
    for lag in range(-max_lag, max_lag + 1):
        a = reference[max_lag : max_lag + n]
        b = signal[max_lag + lag : max_lag + lag + n]
        c = correlation(a, b)
        if c > best[1]:
            best = (lag, c)
    return best


def measure_frequency(samples: Sequence[float], channels: int = CHANNELS) -> float:
    """Estimate the dominant frequency of a (roughly sinusoidal) tone by counting
    rising zero crossings, after trimming leading and trailing silence.

    Used by `wavelink doctor` and the hardware probe tools: it is deliberately
    simple and dependency-free, and it measures end-to-end correctness (if any
    part of the chain drops or duplicates samples, the number moves). Accepts plain
    lists or numpy arrays (the mixer hands us numpy).
    """
    n = len(samples) // channels
    if n < 64:
        return 0.0
    mono = [float(v) for v in to_mono(samples, channels)]
    p = peak(mono)
    threshold = max(1e-4, p * 0.15)
    start = 0
    while start < n and abs(mono[start]) < threshold:
        start += 1
    end = n - 1
    while end > start and abs(mono[end]) < threshold:
        end -= 1
    if end - start < 64:
        return 0.0
    # Skip 20 ms at each edge to avoid the fade-in/out region.
    margin = ms_to_frames(20)
    a = max(start, margin)
    b = min(end, n - 1 - margin)
    if b - a < 64:
        a, b = start, end
    rising = 0
    for i in range(a + 1, b + 1):
        if mono[i - 1] < 0 <= mono[i]:
            rising += 1
    seconds = (b - a) / float(SAMPLE_RATE)
    return rising / seconds if seconds > 0 else 0.0


def signal_report(samples: Sequence[float], channels: int = CHANNELS) -> dict[str, float]:
    n = len(samples) // channels
    mono = to_mono(samples, channels) if n else array("f")
    return {
        "frames": n,
        "seconds": round(n / float(SAMPLE_RATE), 3),
        "peak": round(float(peak(samples)), 5),
        "rms": round(float(rms(samples)), 5),
        "audio_rms": round(float(rms(mono)), 5),
        "frequency": round(measure_frequency(samples, channels), 2),
    }


# --------------------------------------------------------------------------
# Signal generation (tests, smoke checks, and the doctor's continuity probe)
# --------------------------------------------------------------------------

def make_tone(freq: float, frames: int, gain: float = 0.2, channels: int = CHANNELS) -> array:
    """A plain sine, phase starting at 0."""
    out = array("f", bytes(4 * frames * channels))
    w = 2.0 * math.pi * freq / SAMPLE_RATE
    for i in range(frames):
        v = gain * math.sin(w * i)
        for c in range(channels):
            out[i * channels + c] = v
    return out


class Chirp:
    """A phase-continuous linear sweep, generated incrementally.

    A steady sine is a poor end-to-end probe: dropping or duplicating whole cycles
    of a 1 kHz tone still looks like a 1 kHz tone, so the measurement cannot see
    the damage. A chirp is phase-continuous, so any discontinuity shows up as a
    level jump or a wrong frequency reading, and it is broadband so correlation can
    pin down a real delay (see best_lag).
    """

    def __init__(self, f_start: float = 300.0, f_end: float = 3000.0, seconds: float = 4.0, gain: float = 0.35) -> None:
        self.f_start = f_start
        self.f_end = f_end
        self.seconds = seconds
        self.gain = gain
        self._phase = 0.0
        self._t = 0.0

    def read(self, frames: int, channels: int = CHANNELS) -> array:
        out = array("f", bytes(4 * frames * channels))
        step = 1.0 / SAMPLE_RATE
        for i in range(frames):
            frac = min(1.0, self._t / self.seconds)
            freq = self.f_start + (self.f_end - self.f_start) * frac
            v = self.gain * math.sin(self._phase)
            self._phase += 2.0 * math.pi * freq * step
            if self._phase > 2.0 * math.pi:
                self._phase -= 2.0 * math.pi
            self._t += step
            for c in range(channels):
                out[i * channels + c] = v
        return out

    @property
    def elapsed(self) -> float:
        return self._t


# --------------------------------------------------------------------------
# Format conversion at the device boundary
# --------------------------------------------------------------------------

def to_float32(raw: bytes, fmt: str) -> array:
    """Convert raw device bytes to interleaved float32.

    Accepts bytes (the normal case). If something else is passed it is treated as
    float32 samples and re-encoded, rather than silently producing garbage from a
    2-D numpy block.
    """
    out = array("f")
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
    else:
        data = as_float_array(raw).tobytes()
    if fmt in ("f32", "float"):
        out.frombytes(data[: len(data) // 4 * 4])
        return out
    if fmt == "s16":
        a = array("h")
        a.frombytes(data[: len(data) // 2 * 2])
        out.extend(v / 32768.0 for v in a)
        return out
    if fmt == "s32":
        a = array("i")
        a.frombytes(data[: len(data) // 4 * 4])
        out.extend(v / 2147483648.0 for v in a)
        return out
    if fmt == "s24":
        n = len(data) // 3 * 3
        for i in range(0, n, 3):
            v = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            out.append(v / 8388608.0)
        return out
    if fmt == "u8":
        out.extend((b - 128) / 128.0 for b in data)
        return out
    raise ValueError("unsupported sample format %r" % (fmt,))


def from_float32_f32(samples: Sequence[float]) -> bytes:
    return as_float_array(samples).tobytes()


def from_float32_s16(samples: Sequence[float]) -> bytes:
    """float32 -> signed 16-bit, with clipping."""
    a = array("h", (max(-32768, min(32767, int(round(s * 32767.0)))) for s in samples))
    return a.tobytes()


def from_float32_fmt(samples: Sequence[float], fmt: str) -> bytes:
    """float32 -> the device's sample format."""
    if fmt in ("f32", "float"):
        return from_float32_f32(samples)
    if fmt == "s16":
        return from_float32_s16(samples)
    if fmt == "s32":
        a = array("i", (max(-2147483648, min(2147483647, int(round(s * 2147483647.0)))) for s in samples))
        return a.tobytes()
    raise ValueError("unsupported output sample format %r" % (fmt,))


def channel_map(samples: Sequence[float], src_ch: int, dst_ch: int) -> array:
    """Cheap interleaved channel conversion (mono<->stereo by duplicate/average)."""
    if src_ch == dst_ch:
        return as_float_array(samples)
    out = array("f")
    n_frames = len(samples) // src_ch
    if src_ch == 1 and dst_ch == 2:
        for i in range(n_frames):
            v = samples[i]
            out.append(v)
            out.append(v)
        return out
    if src_ch == 2 and dst_ch == 1:
        for i in range(n_frames):
            out.append((samples[2 * i] + samples[2 * i + 1]) * 0.5)
        return out
    for f in range(n_frames):
        base = f * src_ch
        for c in range(dst_ch):
            out.append(samples[base + (c if c < src_ch else src_ch - 1)])
    return out


# --------------------------------------------------------------------------
# Resampling: streaming windowed-sinc with a precomputed tap table
# --------------------------------------------------------------------------

_TABLE_BITS = 12
_TABLE_SIZE = 1 << _TABLE_BITS
_ZERO_CROSSINGS = 16


def _sinc(x: float) -> float:
    if x == 0.0:
        return 1.0
    px = math.pi * x
    return math.sin(px) / px


def _blackman(x: float) -> float:
    # x in [-1, 1]
    t = (x + 1.0) * 0.5
    return 0.42 - 0.5 * math.cos(2 * math.pi * t) + 0.08 * math.cos(4 * math.pi * t)


def _build_tap_table() -> list[float]:
    """Windowed-sinc table: row ``p`` holds taps for phase ``p / _TABLE_SIZE``.

    Layout: ``table[p * 2Z + (k + Z)]`` is the weight for tap ``k`` in ``[-Z, Z)``.
    Precomputed once at import; this is what makes the streaming resampler fast
    enough (a few percent of one core for 48 kHz stereo).
    """
    z = _ZERO_CROSSINGS
    span = 2 * z
    table = [0.0] * ((_TABLE_SIZE + 1) * span)  # +1 row so index _TABLE_SIZE is valid
    for p in range(_TABLE_SIZE + 1):
        frac = p / float(_TABLE_SIZE)
        base = p * span
        for k in range(-z, z):
            x = k - frac
            table[base + k + z] = _sinc(x) * _blackman(x / z)
    return table


_TAPS = _build_tap_table()


class Resampler:
    """Streaming sample-rate converter for interleaved audio (windowed sinc).

    Keeps a history buffer so it can be fed arbitrary block sizes, which is what a
    device callback does. Quality is far beyond what is audible for speech and
    music at 44.1<->48 kHz; accuracy is asserted against an exact sinc reference in
    tests/test_wavelink.py.
    """

    def __init__(self, src_rate: int, dst_rate: int, channels: int = CHANNELS) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.channels = int(channels)
        self.ratio = self.src_rate / float(self.dst_rate)
        self._z = _ZERO_CROSSINGS
        self._span = 2 * self._z
        # Pending input, starting with z silent frames of history. `_pos` is the
        # fractional position of the next output frame within `_in`.
        self._in = [0.0] * (self._z * self.channels)
        self._pos = float(self._z)
        self.enabled = self.src_rate != self.dst_rate

    def reset(self) -> None:
        self._in = [0.0] * (self._z * self.channels)
        self._pos = float(self._z)

    def process(self, frames: Sequence[float]) -> array:
        """Interleaved float in [-1,1] -> interleaved float32 at the target rate.

        An output frame is only produced once its entire window exists - z input
        frames before *and* after it. Waiting for the frames after it costs z input
        frames of lookahead (a third of a millisecond at 48 kHz) and is what keeps
        block boundaries clean: running the filter on a truncated window every 10 ms
        is heard as a rough, buzzy edge, and it was measurably the largest error in
        the whole converter. Input that is not consumed yet stays buffered for the
        next call, so arbitrary block sizes are safe.
        """
        if not self.enabled:
            return as_float_array(frames)
        if frames:
            self._in.extend(as_float_array(frames))
        ch = self.channels
        z = self._z
        span = self._span
        taps = _TAPS
        n_in = len(self._in) // ch
        out = array("f")
        pos = self._pos
        ratio = self.ratio
        while True:
            i = int(pos)
            if i + z > n_in:  # the window's right edge does not exist yet
                break
            frac = pos - i
            pi = int(frac * _TABLE_SIZE)
            if pi > _TABLE_SIZE:  # paranoia against float rounding
                pi = _TABLE_SIZE
            base = pi * span
            start = (i - z) * ch
            for c in range(ch):
                idx = start + c
                acc = 0.0
                for k in range(span):
                    acc += self._in[idx] * taps[base + k]
                    idx += ch
                out.append(-1.0 if acc < -1.0 else (1.0 if acc > 1.0 else acc))
            pos += ratio
        # Everything further back than the next window is dead weight.
        drop = int(pos) - z
        if drop > 0:
            del self._in[: drop * ch]
            pos -= drop
        self._pos = pos
        return out

    def flush(self) -> array:
        """Emit the tail that the lookahead is still holding back.

        A real-time stream never needs this - holding z input frames is the price of
        never running the filter on a truncated window. It exists so that one pass
        over a whole signal returns exactly the samples the maths says it should:
        the missing future is treated as silence, which is what a zero-padded
        reference computation does.
        """
        if not self.enabled:
            return array("f")
        out = self.process([0.0] * (self._z * self.channels))
        self.reset()
        return out


def resample_linear(frames: Sequence[float], src_rate: int, dst_rate: int, channels: int = CHANNELS) -> array:
    """Block-based linear resampler, kept as the reference implementation.

    `Resampler` (windowed sinc) is what the audio paths use; this simpler, obviously
    correct version exists so tests can sanity-check it and so a reader can see what
    the resampling is supposed to do before reading the optimised one.
    """
    if src_rate == dst_rate:
        return as_float_array(frames)
    n_in = len(frames) // channels
    n_out = int(n_in * dst_rate / src_rate)
    out = array("f", bytes(4 * n_out * channels))
    ratio = (n_in - 1) / max(1, n_out - 1) if n_out > 1 else 0.0
    for j in range(n_out):
        pos = j * ratio
        i0 = int(pos)
        i1 = min(n_in - 1, i0 + 1)
        f = pos - i0
        for c in range(channels):
            a = frames[i0 * channels + c]
            b = frames[i1 * channels + c]
            out[j * channels + c] = a + (b - a) * f
    return out


# --------------------------------------------------------------------------
# Mixing
# --------------------------------------------------------------------------

def saturating_mix_into(dst: array, src: Sequence[float], gain: float = 1.0) -> None:
    """dst += src * gain with saturation, in place (both interleaved float32)."""
    n = min(len(dst), len(src))
    if gain == 1.0:
        for i in range(n):
            v = dst[i] + src[i]
            dst[i] = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
    else:
        for i in range(n):
            v = dst[i] + src[i] * gain
            dst[i] = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)


def apply_gain(samples: Sequence[float], gain: float, *, limit: bool = True) -> array:
    out = array("f", bytes(4 * len(samples)))
    if gain == 1.0 and not limit:
        out[:] = as_float_array(samples)
        return out
    for i, s in enumerate(samples):
        v = s * gain
        out[i] = clamp(v) if limit else v
    return out
