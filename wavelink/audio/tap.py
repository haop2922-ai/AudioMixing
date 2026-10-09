"""Fan-out of what a node is playing, for HTTP/WebSocket listeners.

A browser cannot join the UDP audio plane: it has no UDP socket, and no web page can
capture a phone's system audio at all. So the node the phone talks to does the work -
every block a local sink renders is copied here, and each connected listener drains
its own queue.

Subscribers are independent on purpose. A phone on a slow link, or one that is paused
in a background tab, must never be able to stall playback for everyone else, so its
queue is bounded and drops the *oldest* audio when it falls behind (the listener
wants to be current, not complete).
"""

from __future__ import annotations

import threading
from array import array
from collections import deque
from typing import Any

from .format import CHANNELS

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is bundled
    _np = None


def to_pcm16(samples) -> bytes:
    """Interleaved float32 -> signed 16-bit little-endian (what browsers prefer).

    Half the bytes of float32 for the same audio, and every browser can turn it back
    into floats with one pass.
    """
    if _np is not None:
        arr = _np.asarray(samples, dtype=_np.float32).reshape(-1)
        clipped = _np.clip(arr, -1.0, 1.0)
        return (_np.round(clipped * 32767.0).astype("<i2")).tobytes()
    out = array("h", bytes(2 * len(samples)))
    for i, s in enumerate(samples):
        v = int(round((s if s <= 1.0 else 1.0) * 32767.0))
        out[i] = -32768 if v < -32768 else (32767 if v > 32767 else v)
    return out.tobytes()


class AudioTap:
    """One publisher (the render loops), many independent listeners.

    The queue is deliberately short (8 blocks = 80 ms). Every block that waits here is
    latency the listener cannot get rid of, and a phone that starts far behind stays far
    behind - dropping is much better than arriving seven seconds late.
    """

    def __init__(self, max_blocks: int = 8, name: str = "") -> None:
        self.name = name
        self.max_blocks = max(1, int(max_blocks))
        self._lock = threading.Lock()
        self._subs: dict[int, deque[bytes]] = {}
        self._next_id = 0
        self.published_blocks = 0
        self.dropped_blocks = 0
        self.last_publish_ns = 0

    # -- publisher ---------------------------------------------------------
    def publish(self, samples, *, already_pcm: bytes | None = None) -> None:
        """Offer one block to every listener. Never blocks, never raises."""
        payload = already_pcm if already_pcm is not None else to_pcm16(samples)
        if not payload:
            return
        with self._lock:
            subs = list(self._subs.values())
            self.published_blocks += 1
        for queue in subs:
            # appendleft keeps the newest at the left, so trimming is cheap.
            if len(queue) >= self.max_blocks:
                self.dropped_blocks += 1
            queue.append(payload)

    # -- listeners ---------------------------------------------------------
    def subscribe(self) -> int:
        with self._lock:
            self._next_id += 1
            sid = self._next_id
            self._subs[sid] = deque(maxlen=self.max_blocks)
            return sid

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def drain(self, sid: int) -> bytes:
        """Take everything queued for `sid` as one chunk (empty when idle)."""
        with self._lock:
            queue = self._subs.get(sid)
            if not queue:
                return b""
            out = b"".join(queue)
            queue.clear()
            return out

    def stats(self) -> dict[str, Any]:
        with self._lock:
            listeners = len(self._subs)
            queued = sum(len(q) for q in self._subs.values())
        return {
            "name": self.name,
            "listeners": listeners,
            "queued_blocks": queued,
            "published_blocks": self.published_blocks,
            "dropped_blocks": self.dropped_blocks,
            "block_ms": round(1000.0 * 480 / 48000.0, 2),
            "channels": CHANNELS,
        }
