"""Network microphone: audio pushed in from a phone, over the node's own web port.

A phone cannot join the UDP audio plane, and a *browser* cannot capture system audio at
all - but both can post PCM to an HTTP endpoint. Doing that turns the phone into one
more capture device on this machine, which means the whole existing pipeline works with
it unchanged: it can be the channel's source, feed the local pass-through, be broadcast
to the LAN, or be mixed with anything else.

Two sources of audio arrive this way:
  * the phone's **microphone** (any browser, or the app) - needs only a permission tap;
  * the phone's **system sound** (the native app only - Android 10+ MediaProjection).
"""

from __future__ import annotations

import threading
import time
from array import array
from collections import deque
from typing import Any

from ..base import AudioDevice, BackendBase, KIND_SOURCE, register
from ..format import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE, Resampler

# A mic that stops sending for this long is hidden from every device picker, so a phone
# that walks out of Wi-Fi range does not leave a dead entry behind. The object itself is
# kept (see open_mic): the capture side holds a reference to it, and swapping the object
# out from under that reference is how the audio went permanently silent after a hiccup.
# A phone that stops sending disappears from the list almost at once: an entry greyed
# out as "silent" is just clutter - you cannot tell it from a broken one.
LISTED_FOR_S = 3.0
STALE_S = 6.0
# A name only becomes a *device* once it has sent a quarter of a second: the app's
# connectivity check posts 100 ms of silence to test the link, and that must not turn
# into a phantom entry in every picker (nor get written into the config).
MIN_BYTES_TO_SHOW = 24000
# Only after this much silence is the slot actually forgotten.
IDLE_FORGET_S = 900.0
# Never buffer more than this much audio: a phone that outruns us must not add latency.
# A cushion of audio before playback starts. Ordinary LAN jitter (a POST occasionally
# taking longer than the 20 ms of audio it carries) is absorbed here instead of being
# heard; without it every late packet is a hole in the sound.
# Default cushion. Android coalesces timers, so a phone often sends in bursts rather
# than every 20 ms; 60 ms could not absorb that and the sound kept fading out. It is a
# setting because the right answer is a trade-off: more cushion means fewer dropouts and
# more delay (see jitter_ms in the panel).
DEFAULT_JITTER_MS = 150
JITTER_FRAMES = int(SAMPLE_RATE * DEFAULT_JITTER_MS / 1000.0)


def set_jitter_ms(ms: int) -> None:
    """Change the cushion for every phone (the panel exposes this)."""
    global JITTER_FRAMES
    ms = max(20, min(600, int(ms or DEFAULT_JITTER_MS)))
    JITTER_FRAMES = int(SAMPLE_RATE * ms / 1000.0)
# A real pause (no data at all) re-arms the cushion, so speech does not resume mid-stream.
RESYNC_AFTER_S = 0.5
# Never hold more than this: a listener wants to be current, and a big backlog is felt as
# delay. 250 ms is far above the jitter we actually see.
MAX_QUEUED_FRAMES = int(SAMPLE_RATE * 0.40)
# Trimming only down to a low watermark: trimming to exactly the cap means the next tiny
# drift trims again, and every trim is a click.
TRIM_TO_FRAMES = int(SAMPLE_RATE * 0.20)
# On an underrun the tail is faded out over this many blocks (~50 ms), not repeated
# verbatim. Repeating a block unchanged puts a step in the waveform at the block rate,
# which is heard as a 100 Hz buzz - the "electronic" sound this was reported as.
HOLD_FADE_BLOCKS = 5

MICS: dict[str, "NetMic"] = {}
_LOCK = threading.Lock()


def open_mic(name: str, rate: int = SAMPLE_RATE) -> "NetMic":
    """Get the slot for `name`, creating it once and reusing it forever after.

    The capture hub caches whatever `open_source` hands it, so this must return the
    *same* object every time: replacing it on a reconnect (or a rate change) left the
    reader holding a stopped mic, and the audio stayed silent until the configuration
    happened to be rebuilt.
    """
    name = (name or "").strip() or "手机"
    with _LOCK:
        mic = MICS.get(name)
        if mic is None:
            mic = NetMic(name, rate)
            MICS[name] = mic
        elif mic.device_rate != (int(rate) or SAMPLE_RATE):
            mic.set_rate(rate)
        return mic


def close_mic(name: str, mic: "NetMic | None" = None) -> None:
    """Drop a slot. Only used by tests and by an explicit disconnect."""
    with _LOCK:
        current = MICS.get((name or "").strip())
        if current is None:
            return
        if mic is not None and current is not mic:
            return
        MICS.pop(current.name, None)
        current.stop()


def reap() -> None:
    """Forget only slots that have been idle for a very long time."""
    now = time.monotonic()
    with _LOCK:
        for mic in [m for m in MICS.values() if now - m.last_push > IDLE_FORGET_S]:
            MICS.pop(mic.name, None)
            mic.stop()


def set_muted(name: str, muted: bool) -> bool:
    with _LOCK:
        mic = MICS.get((name or '').strip())
    if mic is None:
        return False
    mic.muted = bool(muted)
    return True


def set_gain(name: str, gain: float) -> bool:
    with _LOCK:
        mic = MICS.get((name or "").strip())
    if mic is None:
        return False
    mic.gain = max(0.0, min(4.0, float(gain)))
    return True


def active() -> list["NetMic"]:
    """Slots that are actually sending right now (what the pickers should show)."""
    now = time.monotonic()
    with _LOCK:
        return [m for m in MICS.values()
                if now - m.last_push <= LISTED_FOR_S and m.bytes_in >= MIN_BYTES_TO_SHOW]


class NetMic:
    """One incoming stream. `push()` is called from the HTTP handler's thread."""

    def __init__(self, name: str, rate: int = SAMPLE_RATE, origin: str = "phone") -> None:
        self.name = name
        self.origin = origin
        self.sample_rate = SAMPLE_RATE  # what we hand the engine
        self.channels = CHANNELS
        self.device_rate = int(rate) or SAMPLE_RATE
        # A Condition, not a plain Lock: the reader waits for the phone here instead
        # of fading the audio out the moment the queue looks empty.
        self._lock = threading.Condition()
        self._mono: deque[float] = deque()
        self._queued = 0
        self._resampler = Resampler(self.device_rate, SAMPLE_RATE, 1)
        self._stop = threading.Event()
        self._next_ns = 0
        self._epoch_ns = 0
        self._blocks_out = 0
        self._mix_gain = 1.0
        self.started = False
        self.bytes_in = 0
        self.frames_emitted = 0
        self.dropped_frames = 0
        self.last_push = time.monotonic()
        self.muted = False
        # Per-phone volume, so one loud phone can be tamed without touching the rest.
        self.gain = 1.0
        self._primed = False
        self._last_out = None
        self.underruns = 0
        self.starved = 0
        self._last_chunk = None
        self._hold = 0
        self._hold_prev = 1.0
        self._gaps = deque()
        self._gap_times = deque(maxlen=500)
        self._last_push_at = 0.0
        self._last_size = 0
        self._max_gap_ms = 0.0
        self.connected_at = time.time()

    def set_rate(self, rate: int) -> None:
        """Follow a phone that reconnects at a different sample rate.

        The object stays the same on purpose - only the converter and the queue are
        rebuilt - so whoever is reading it keeps reading the right thing.
        """
        with self._lock:
            self.device_rate = int(rate) or SAMPLE_RATE
            self._resampler = Resampler(self.device_rate, SAMPLE_RATE, 1)
            self._mono.clear()
            self._queued = 0

    # -- from the network --------------------------------------------------
    def push(self, pcm16: bytes) -> int:
        """Append interleaved PCM16 mono from the phone."""
        if not pcm16:
            return 0
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        samples = array("h")
        samples.frombytes(pcm16)
        if not len(samples):
            return 0
        floats = array("f", [s / 32768.0 for s in samples])
        with self._lock:
            self.bytes_in += len(pcm16)
            # What the phone actually sends. The block size identifies the app build
            # (100 ms before, 20 ms now), and the gaps are where the sound is lost - so
            # "is it the app?" and "where does it stall?" are answerable from here
            # instead of guessed at.
            now_at = time.monotonic()
            if self._last_push_at and (now_at - self._last_push_at) > 10.0:
                self._gap_times.clear()
            if self._last_push_at:
                gap_ms = (now_at - self._last_push_at) * 1000.0
                self._gaps.append(gap_ms)
                if len(self._gaps) > 400:
                    self._gaps.popleft()
                # Windowed, not cumulative: a maximum kept since startup reported 88
                # seconds after the phone had simply been idle, and pinned the score to
                # zero for a link that was actually fine.
                self._gap_times.append(gap_ms)
                while len(self._gap_times) > 500:
                    self._gap_times.popleft()
                if gap_ms > self._max_gap_ms:
                    self._max_gap_ms = gap_ms
            self._last_push_at = now_at
            self._last_size = len(pcm16)
            self.last_push = time.monotonic()
            block = floats
            if self._resampler.enabled:
                block = array("f", self._resampler.process(floats))
            self._mono.extend(block)
            self._queued += len(block)
            self._lock.notify_all()
            excess = self._queued - MAX_QUEUED_FRAMES
            if excess > 0:
                excess = self._queued - TRIM_TO_FRAMES
                # Drop the oldest: a listener wants to be current, not complete.
                for _ in range(excess):
                    if self._mono:
                        self._mono.popleft()
                self._queued = len(self._mono)
                self.dropped_frames += excess
        return len(samples)

    # -- AudioSource contract ---------------------------------------------
    def start(self) -> None:
        # An absolute epoch plus a block counter, not an accumulating deadline: adding a
        # block to "now" every time let the reader run about 1% fast, which slowly ate
        # the whole cushion and then produced a burst of dropouts every few seconds.
        self._epoch_ns = time.monotonic_ns()
        self._blocks_out = 0
        self.started = True

    def depth(self) -> int:
        """How many samples are waiting - the measure the mixer's rate servo needs."""
        with self._lock:
            return len(self._mono)

    def take(self, frames: int) -> list[float]:
        """One chunk for the mixer, using this phone's own cushion.

        Taking straight from the queue looked harmless and was not: it bypassed the
        cushion entirely, so ordinary arrival jitter turned into holes in the mix - heard
        as stuttering, exactly like the dropouts this cushion exists to prevent. Here the
        phone primes, waits for its own cushion, and fades its last chunk on an underrun
        instead of punching a gap.
        """
        with self._lock:
            if (time.monotonic() - self.last_push) > RESYNC_AFTER_S:
                self._primed = False
            if not self._primed:
                if len(self._mono) < JITTER_FRAMES:
                    self.starved += 1
                    return [0.0] * frames
                self._primed = True
            got = min(frames, len(self._mono))
            chunk = [self._mono.popleft() for _ in range(got)]
            self._queued = len(self._mono)
            self.frames_emitted += got
            if got == frames:
                self._last_chunk = chunk
                self._hold = 0
                return chunk
            self.underruns += 1
            self.starved += 1
            self._hold += 1
            held = self._last_chunk
            if not held or self._hold > HOLD_FADE_BLOCKS:
                return [0.0] * frames
            factor = 1.0 - self._hold / float(HOLD_FADE_BLOCKS + 1)
            head = [v * factor for v in held[:got]]
            head.extend([0.0] * (frames - got))
            return head

    def read(self) -> array:
        """One block of stereo float32, paced like a real capture device."""
        if self._stop.is_set():
            return array("f")
        block_ns = int(FRAME_SAMPLES * 1e9 / SAMPLE_RATE)
        if not self._blocks_out:
            self._epoch_ns = time.monotonic_ns()
        target_ns = self._epoch_ns + self._blocks_out * block_ns
        self._blocks_out += 1
        wait_ns = target_ns - time.monotonic_ns()
        if wait_ns > 0:
            time.sleep(wait_ns / 1e9)
        with self._lock:
            if (time.monotonic() - self.last_push) > RESYNC_AFTER_S:
                self._primed = False
            if not self._primed:
                if len(self._mono) < JITTER_FRAMES:
                    # Still filling the cushion: silence, but nothing was playing yet.
                    self.frames_emitted += FRAME_SAMPLES
                    return array("f", bytes(4 * FRAME_SAMPLES * CHANNELS))
                self._primed = True
            # Patient, not prompt: if the queue is empty the phone is merely late, and
            # the sink's own buffer can absorb a short wait. Fading straight away turned
            # every scheduling hiccup into audible loss.
            deadline = time.monotonic() + 0.025
            while not self._mono and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._lock.wait(left)
            take = min(FRAME_SAMPLES, len(self._mono))
            if take < FRAME_SAMPLES:
                self.underruns += 1
            mono = [self._mono.popleft() for _ in range(take)]
            self._queued = len(self._mono)
        out = array("f", bytes(4 * FRAME_SAMPLES * CHANNELS))
        if self.muted:
            # Muted at the source, so it is silent locally and on the LAN too.
            self.frames_emitted += FRAME_SAMPLES
            return out
        gain = self.gain
        for i, value in enumerate(mono):
            scaled = value * gain if gain != 1.0 else value
            out[i * CHANNELS] = scaled
            out[i * CHANNELS + 1] = scaled  # a phone mic is mono; feed both ears
        if len(mono) < FRAME_SAMPLES:
            # The tail is missing (the phone is late, not gone). Fade from the last real
            # sample instead of discarding what did arrive and repeating an older block:
            # discarding real audio is what made the sound stutter, and repeating it
            # verbatim is what buzzed. Only the gap is faded, over at most 20 ms.
            tail = min(FRAME_SAMPLES - len(mono), int(SAMPLE_RATE * 0.02))
            last = mono[-1] if mono else 0.0
            step = last / float(tail + 1)
            for k in range(tail):
                value = last - step * (k + 1)
                index = len(mono) + k
                out[index * CHANNELS] = value
                out[index * CHANNELS + 1] = value
        self.frames_emitted += FRAME_SAMPLES
        self._last_out = out
        self._hold = 0
        self._hold_prev = 1.0
        return out

    def stop(self) -> None:
        self._stop.set()

    def describe(self) -> dict[str, Any]:
        stale = time.monotonic() - self.last_push
        return {
            "backend": "netmic",
            "device": self.name,
            "name": self.name,
            "kind": "network",
            "sample_rate": self.sample_rate,
            "device_rate": self.device_rate,
            "channels": self.channels,
            "bytes_in": self.bytes_in,
            "frames_emitted": self.frames_emitted,
            "dropped_frames": self.dropped_frames,
            "queued_frames": self._queued,
            # Freshness, not queue depth: a healthy mic is drained as fast as it
            # arrives, so asking whether anything is queued reported a working phone
            # as silent.
            "live": stale < 1.0,
            "muted": self.muted,
            "gain": round(self.gain, 2),
            "underruns": self.underruns,
            "starved": self.starved,
            "last_size": self._last_size,
            "median_gap_ms": round(sorted(self._gaps)[len(self._gaps) // 2], 1) if self._gaps else 0.0,
            "max_gap_ms": round(max(self._gap_times) if self._gap_times else 0.0, 1),
            "gaps_over_100ms": sum(1 for g in self._gaps if g > 100),
            "idle_s": round(stale, 2),
        }


@register
class NetMicBackend(BackendBase):
    """Presents every connected phone as an ordinary capture device."""

    name = "netmic"
    platform = "any"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def list_devices(cls) -> list[AudioDevice]:
        return [
            AudioDevice(
                # A prefixed id, so a phone called "2" cannot collide with the numeric
                # endpoint ids the Windows backend uses. The name stays as typed.
                id="netmic:" + mic.name,
                name=mic.name,
                kind=KIND_SOURCE,
                backend="netmic",
                kind_detail="network",
                channels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                is_default=False,
                extra={"origin": mic.origin, "live": mic.describe()["live"]},
            )
            for mic in active()
        ]

    @classmethod
    def open_source(cls, spec: dict[str, Any]) -> NetMic:
        name = str(spec.get("device") or "")
        with _LOCK:
            mic = MICS.get(name)
        if mic is None:
            raise RuntimeError("网络麦克风「%s」还没有开始发送" % name)
        return mic

    @classmethod
    def open_sink(cls, spec: dict[str, Any]):
        raise RuntimeError("netmic 只能当音源，不能当输出")

class NetMicMixer:
    """Several phones as one capture source, without the capture hub.

    The hub mixes by reading each device in turn, which only works for sources that
    return immediately; a phone's read is paced and blocking, so the hub's writer parked
    forever on it (that was the silence bug). Here every input is drained without
    waiting, missing samples become silence, and the clock is the mixer's own - so one
    quiet phone can never hold up the others.
    """

    def __init__(self, mics: list["NetMic"]) -> None:
        self.mics = [m for m in mics if m is not None]
        self.name = " + ".join(m.name for m in self.mics) or "手机混音"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.started = False
        self.eof = False
        self._epoch_ns = 0
        self._blocks_out = 0
        # Start a little below unity when several phones are mixed: the limiter then has
        # headroom and the servo has no reason to move at all.
        self._mix_gain = 1.0 if len(self.mics) < 2 else 0.7

    def start(self) -> None:
        for mic in self.mics:
            if not mic.started:
                mic.start()
        self._epoch_ns = time.monotonic_ns()
        self._blocks_out = 0
        self.started = True

    def _servo_frames(self, mic: "NetMic") -> int:
        """How many samples to take from one phone this block.

        A phone whose clock runs fast fills the queue until the cap trims it - and every
        trim is an audible jump. Taking a hair more (or fewer) samples than the block
        holds, in proportion to how full the queue is, drains or fills that difference
        smoothly instead. The result is resampled back to one block, so nothing is lost.
        """
        depth = mic.depth()
        error = depth - (JITTER_FRAMES + FRAME_SAMPLES)
        swing = int(FRAME_SAMPLES * 0.10)          # at most 10% per block
        adjust = max(-swing, min(swing, int(error / 24)))
        return max(int(FRAME_SAMPLES * 0.5), FRAME_SAMPLES + adjust)

    def take_block(self) -> array:
        """One stereo block, every enabled phone summed together.

        The sum can exceed full scale with two loud phones, and clamping that is heard as
        harsh distortion. Instead the mix is turned down as a whole, smoothly, and only
        while it needs to be - so nothing clips and nothing pumps.
        """
        mono = [0.0] * FRAME_SAMPLES
        for mic in self.mics:
            if mic.muted:
                continue
            # Exactly one block, untouched. An earlier version took a few samples more or
            # fewer and resampled them back to a block to soak up a phone whose clock runs
            # fast - which is a permanent pitch shift, and was heard as a cheap voice
            # changer. Rate differences are handled where they belong: the sender paces
            # itself against the wall clock, and the queue's cap trims any residual drift.
            chunk = mic.take(FRAME_SAMPLES)
            gain = mic.gain
            for i, value in enumerate(chunk):
                mono[i] += value * gain
        peak = max((abs(v) for v in mono), default=0.0) * self._mix_gain
        target = 1.0 if peak <= 0.98 else 0.98 / max(peak, 1e-6)
        target = max(0.05, min(4.0, target))
        # Slow in both directions. An earlier version pulled the gain down by a quarter
        # within a single 10 ms block, which is a 100 Hz volume modulation - heard as the
        # sound breaking up, and it did it to the built-in test tone too, since that is
        # mixed here as well. Spikes are handled by the soft limiter below instead.
        step = 0.02 if target < self._mix_gain else 0.01
        self._mix_gain += (target - self._mix_gain) * step
        out = array("f", bytes(4 * FRAME_SAMPLES * CHANNELS))
        gain = self._mix_gain
        for i, value in enumerate(mono):
            scaled = value * gain
            if scaled > 0.6 or scaled < -0.6:
                # Soft knee towards the ceiling: rounds peaks off instead of slicing them,
                # which keeps a loud moment from turning into a click.
                sign = 1.0 if scaled > 0 else -1.0
                over = abs(scaled) - 0.6
                scaled = sign * (0.6 + over / (1.0 + over * 2.5))
            out[i * CHANNELS] = scaled
            out[i * CHANNELS + 1] = scaled
        return out

    def read(self) -> array:
        block_ns = int(FRAME_SAMPLES * 1e9 / SAMPLE_RATE)
        if not self._blocks_out:
            self._epoch_ns = time.monotonic_ns()
        target = self._epoch_ns + self._blocks_out * block_ns
        self._blocks_out += 1
        wait_ns = target - time.monotonic_ns()
        if wait_ns > 0:
            time.sleep(wait_ns / 1e9)
        return self.take_block()

    def stop(self) -> None:
        for mic in self.mics:
            mic.stop()

    def describe(self) -> dict:
        return {"name": self.name, "mics": [m.describe() for m in self.mics]}


def _resample(samples: list[float], frames: int) -> list[float]:
    """Linear resampling of one block: `len(samples)` in, exactly `frames` out."""
    n = len(samples)
    if n == frames or n == 0:
        return list(samples) + [0.0] * (frames - n)
    out = [0.0] * frames
    step = (n - 1) / float(frames - 1) if frames > 1 else 0.0
    for i in range(frames):
        pos = i * step
        left = int(pos)
        right = min(left + 1, n - 1)
        frac = pos - left
        out[i] = samples[left] * (1.0 - frac) + samples[right] * frac
    return out
