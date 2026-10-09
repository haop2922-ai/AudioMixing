"""Capture fan-out.

One physical capture device has a single read cursor. When two routes need the
same source (for example "my laptop's sound to my headset *and* to my partner's
headset") both sessions must see the *same* samples, not alternate blocks.

``CaptureHub`` solves that: it owns one reader thread per capture device and gives
every consumer an independent cursor over a small rolling buffer, so consumers
never block each other and the device is opened exactly once.
"""

from __future__ import annotations

import threading
import time
from array import array
from collections import deque
from typing import Any, Callable

# A capture that has read nothing for this long, while a consumer waits, is stuck:
# its reader thread is parked inside a source whose read() never returns.
WEDGED_AFTER_S = 6.0

from .format import CHANNELS, SAMPLE_RATE, as_float_array

TAP_BUFFER_BLOCKS = 64


class _Capture:
    def __init__(self, spec: dict[str, Any], source: Any, factory: Callable[[dict], Any]) -> None:
        self.spec = spec
        self.source = source
        self.factory = factory
        self.blocks: deque[Any] = deque(maxlen=TAP_BUFFER_BLOCKS)
        self.lock = threading.Condition()
        self.next_index = 0
        self.consumers = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error = ""
        self.blocks_read = 0
        self.frames_read = 0
        self.name = getattr(source, "name", str(spec.get("device", "")))
        self.closed = False

    def endpoint_name(self) -> str:
        """The real capture endpoint's name, once the device has been opened.

        `_Capture.name` is only a label: it is computed before `start()`, so for the
        Windows default endpoint it is literally "@default". Anything that needs to
        know *which* device is being captured - the echo guard, for one - has to ask
        here, because the answer only exists after the source is running.
        """
        return str(getattr(self.source, "name", "") or self.name)

    def start_reader(self) -> None:
        self.created_at = time.monotonic()
        self.thread = threading.Thread(target=self._reader, name="wavelink-capture-%s" % self.name[:16], daemon=True)
        self.thread.start()

    def wedged(self, now: float | None = None) -> bool:
        """Has this capture produced nothing at all while somebody waits for it?

        A source whose read() never returns leaves the reader thread parked forever:
        no error, not closed, just silence - which the panel could only report as "the
        pass-through is ready" while not a single block moved. That is impossible to
        diagnose from the outside, so it is detected and rebuilt instead.
        """
        if self.consumers <= 0 or self.blocks_read > 0 or self.closed:
            return False
        return ((now if now is not None else time.monotonic()) - self.created_at) > WEDGED_AFTER_S

    def _reader(self) -> None:
        try:
            self.source.start()
        except Exception as exc:
            self.error = "start failed: %s" % exc
            return
        while not self.stop_event.is_set():
            try:
                block = self.source.read()
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                break
            if not block:
                if getattr(self.source, "eof", False):
                    break
                time.sleep(0.002)
                continue
            with self.lock:
                self.blocks.append((self.next_index, block))
                self.next_index += 1
                self.blocks_read += 1
                self.frames_read += len(block) // CHANNELS
                self.lock.notify_all()
        with self.lock:
            self.closed = True
            self.lock.notify_all()
        try:
            self.source.stop()
        except Exception:
            pass

    def close_reader(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.lock.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=1.5)
            self.thread = None

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.endpoint_name(),
            "spec": dict(self.spec),
            "consumers": self.consumers,
            "blocks_read": self.blocks_read,
            "frames_read": self.frames_read,
            "error": self.error,
            "closed": self.closed,
        }


class TapReader:
    """One consumer's cursor over a shared capture."""

    def __init__(self, capture: _Capture, name: str = "") -> None:
        self.capture = capture
        self.name = name or capture.name
        self.sample_rate = getattr(capture.source, "sample_rate", SAMPLE_RATE)
        self.channels = getattr(capture.source, "channels", CHANNELS)
        self._next = 0
        self.started = False
        self.dropped_blocks = 0

    @property
    def eof(self) -> bool:
        return self.capture.closed and not self.capture.blocks

    @property
    def endpoint_name(self) -> str:
        """Who this tap really reads from (see _Capture.endpoint_name)."""
        return self.capture.endpoint_name()

    def start(self) -> None:
        if self.started:
            return
        with self.capture.lock:
            self.capture.consumers += 1
            # Start from the newest block so a late consumer does not replay history.
            self._next = self.capture.next_index
        self.started = True
        if self.capture.thread is None:
            self.capture.start_reader()

    def read(self):
        cap = self.capture
        with cap.lock:
            while True:
                if cap.blocks:
                    if cap.blocks[-1][0] < self._next:
                        # We are ahead of the writer: nothing new yet.
                        pass
                    else:
                        for index, block in cap.blocks:
                            if index >= self._next:
                                self._next = index + 1
                                return block
                        # The buffer rolled past us while we were descheduled.
                        newest_index = cap.blocks[-1][0]
                        self.dropped_blocks += max(0, newest_index + 1 - self._next)
                        self._next = newest_index + 1
                if cap.closed:
                    return None
                cap.lock.wait(timeout=0.2)

    def drain_nowait(self) -> array:
        """Every block that arrived since the last call, concatenated (empty if none).

        The mixer uses this so one silent device can never block the others.
        """
        cap = self.capture
        out = array("f")
        with cap.lock:
            for index, block in cap.blocks:
                if index >= self._next:
                    self._next = index + 1
                    out.extend(as_float_array(block))
        return out

    def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        with self.capture.lock:
            self.capture.consumers -= 1

    def describe(self) -> dict[str, Any]:
        d = {}
        try:
            d = self.capture.source.describe()
        except Exception:
            pass
        d.update({"consumers": self.capture.consumers, "dropped_blocks": self.dropped_blocks})
        return d


class MixReader:
    """Mixes several capture devices into one 48 kHz stereo stream.

    The LAN channel needs this: several phones (each on its own Bluetooth adapter), or
    a phone plus this machine's own sound, have to become *one* stream before they are
    sent, so a listener still receives a single mixed block every 10 ms instead of one
    stream per source. Mixing here rather than on the receiver also means the wire
    format does not change at all.

    Each input keeps its own queue and is padded with silence when it has nothing, so
    a device that has gone quiet cannot stall the others.
    """

    def __init__(self, readers: list[Any], name: str = "") -> None:
        self.readers = list(readers)
        self.name = name or "混音"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.frames_per_block = 480
        self._pending = [array("f") for _ in self.readers]
        self._lock = threading.Lock()
        self._deadline = time.monotonic()
        self.started = False
        self.blocks_read = 0

    @property
    def endpoint_name(self) -> str:
        """Who this mixes, so the echo guard can see every input at once."""
        return " + ".join(r.endpoint_name for r in self.readers)

    def start(self) -> None:
        if self.started:
            return
        for reader in self.readers:
            reader.start()
        self.started = True
        self._deadline = time.monotonic()

    def read(self) -> array:
        """Exactly one 10 ms block, mixed, then paced like a real device."""
        need = self.frames_per_block * CHANNELS
        mix = [0.0] * need
        with self._lock:
            for i, reader in enumerate(self.readers):
                fresh = reader.drain_nowait()
                if fresh:
                    self._pending[i].extend(fresh)
                queue = self._pending[i]
                # A device that runs fast must not keep adding latency.
                if len(queue) > need * 8:
                    del queue[: len(queue) - need * 8]
                take = min(need, len(queue))
                for k in range(take):
                    mix[k] += queue[k]
                if take:
                    del queue[:take]
            for k in range(need):
                v = mix[k]
                mix[k] = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
            self.blocks_read += 1
        self._deadline += self.frames_per_block / float(SAMPLE_RATE)
        sleep_for = self._deadline - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        elif sleep_for < -0.2:
            self._deadline = time.monotonic()
        return array("f", mix)

    def stop(self) -> None:
        for reader in self.readers:
            try:
                reader.stop()
            except Exception:
                pass
        self.started = False

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "mix",
            "device": self.name,
            "name": self.name,
            "kind": "mix",
            "sources": [r.endpoint_name for r in self.readers],
            "blocks_read": self.blocks_read,
        }


class CaptureHub:
    """Opens each capture device once and hands out fan-out readers."""

    def __init__(self) -> None:
        self._captures: dict[tuple[str, str], _Capture] = {}
        self._lock = threading.RLock()
        self._reader_factory: Callable[[dict], Any] | None = None

    def configure(self, factory: Callable[[dict], Any]) -> None:
        self._reader_factory = factory

    def _open(self, spec: dict[str, Any]) -> Any:
        if self._reader_factory is not None:
            return self._reader_factory(spec)
        from . import base as audio_base

        return audio_base.open_source(spec)

    def reader(self, spec: dict[str, Any], name: str = "") -> TapReader:
        key = (str(spec.get("backend")), str(spec.get("device")))
        with self._lock:
            cap = self._captures.get(key)
            if cap is None or cap.closed:
                source = self._open(spec)
                cap = _Capture(dict(spec), source, self._open)
                self._captures[key] = cap
            reader = TapReader(cap, name)
        reader.start()
        return reader

    def health(self) -> list[str]:
        """Drop captures whose reader thread never produced a block.

        Rebuilding is safe: the next reader() re-opens the device, and a source that
        was merely slow has time to prove itself first (WEDGED_AFTER_S).
        """
        now = time.monotonic()
        dropped: list[str] = []
        with self._lock:
            for key, cap in list(self._captures.items()):
                if cap.wedged(now):
                    self._captures.pop(key, None)
                    cap.close_reader()
                    dropped.append(key[1])
        return dropped

    def mixer(self, specs: list[dict[str, Any]], name: str = "") -> MixReader:
        """One reader per device, mixed into a single stream (see MixReader)."""
        readers = [self.reader(spec, name=str(spec.get("device") or "")) for spec in specs]
        return MixReader(readers, name=name)

    def reap(self) -> None:
        with self._lock:
            for key, cap in list(self._captures.items()):
                if cap.consumers <= 0:
                    cap.close_reader()
                    del self._captures[key]

    def stats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [cap.stats() for cap in self._captures.values()]

    def stop_all(self) -> None:
        with self._lock:
            captures = list(self._captures.values())
            self._captures.clear()
        for cap in captures:
            cap.close_reader()
