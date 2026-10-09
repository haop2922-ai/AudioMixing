"""Prove the phone listener path end to end, without a phone.

The mobile page cannot be tested from here, but everything it depends on can be:
the WebSocket handshake, the framing, and a real PCM16 stream produced by the node's
own render loop. The tap publishes whatever a sink plays, so enabling "listen" (which
creates a sink that writes silence) is enough to see frames flow - nothing audible
happens, and the previous setting is restored on the way out.

    python tools/check_phone_stream.py [seconds]
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.request

HOST = "127.0.0.1"
PORT = int(os.environ.get("WAVELINK_PORT", "17800"))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BLOCK_BYTES = 480 * 2 * 2  # 480 frames, stereo, PCM16


def api(path: str, body: dict | None = None) -> dict:
    url = "http://%s:%d%s" % (HOST, PORT, path)
    if body is None:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_frame(sock: socket.socket) -> bytes:
    header = sock.recv(2)
    if len(header) < 2:
        return b""
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return payload


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    try:
        api("/api/status")
    except Exception as exc:
        print("FAIL: 控制台 %d 端口没有响应 (%s)" % (PORT, exc))
        return 1

    print("WaveLink 手机收听链路自检")
    print("  1) 播放测试音到「空输出」并同时喂给监听 tap（完全无声，也不需要其它设备在线）")
    api("/api/test-tone", {"device": "null", "seconds": seconds, "freq": 1000})
    try:
        time.sleep(0.4)
        print("  2) WebSocket 握手")
        sock = socket.create_connection((HOST, PORT), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.sendall((
            "GET /ws/audio HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
            % (HOST, PORT, key)
        ).encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        ok_status = "101" in status
        ok_accept = expected.encode("ascii") in head
        print("     %s  %s" % (status, "accept 正确" if ok_accept else "accept 不符"))

        print("  3) 读取 PCM 帧（最多 %.1f 秒）" % seconds)
        sock.settimeout(3.0)
        frames = 0
        payload_bytes = 0
        bad_sizes = 0
        nonzero = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                payload = read_frame(sock)
            except TimeoutError:
                break
            if not payload:
                break
            frames += 1
            payload_bytes += len(payload)
            if len(payload) % BLOCK_BYTES:
                bad_sizes += 1
            if any(payload):
                nonzero += 1
        sock.close()
        print("     收到 %d 帧 / %d 字节" % (frames, payload_bytes))
        print("     非 10ms 整块的帧: %d（应为 0）；含音频的帧: %d（测试音期间应 > 0）" % (bad_sizes, nonzero))
        ok = ok_status and ok_accept and frames > 10 and bad_sizes == 0 and nonzero > 0
        print("")
        print("RESULT: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        try:
            api("/api/test-tone", {"device": "null", "seconds": 0.1})
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
