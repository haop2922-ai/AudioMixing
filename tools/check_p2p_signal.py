"""手机直连（P2P）信令通道的自检。

做的事：起一个源码节点 → 用**真的 WebSocket**走一遍完整流程 → 报结果。

为什么自己写 WebSocket 客户端而不用现成库：
    如果用同一个库既当服务端又当客户端，两边的 bug 会互相抵消 ——
    "我跟我自己说话能懂"证明不了什么。这里用原生 socket 自己握手、自己组帧
    （客户端帧必须带掩码），验的是服务端真的按 RFC 6455 说话。

     python tools/check_p2p_signal.py
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 17800



def _url(path: str, **params: str) -> str:
    """拼一个 ASCII 安全的 URL（手机名字是中文，必须百分号编码）。"""
    return path + "?" + urllib.parse.urlencode(params)


class WsClient:
    """最小可用的 WebSocket 文本客户端（只支持本自检需要的那些帧类型）。"""

    def __init__(self, path: str, timeout: float = 8.0) -> None:
        self.sock = socket.create_connection((HOST, PORT), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, HOST, PORT, key)
        )
        self.sock.sendall(req.encode("ascii"))
        self.status = b""
        while b"\r\n\r\n" not in self.status:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.status += chunk
        self.headers, _, rest = self.status.partition(b"\r\n\r\n")
        self.buf = rest
        self.closed = False

    @property
    def upgraded(self) -> bool:
        return b"101" in self.headers.split(b"\r\n")[0]

    def _read(self, n: int) -> bytes | None:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str, opcode: int = 0x1) -> None:
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        mask = secrets.token_bytes(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> tuple[int, bytes] | None:
        head = self._read(2)
        if head is None:
            return None
        b0, b1 = head[0], head[1]
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            ext = self._read(2)
            if ext is None:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._read(8)
            if ext is None:
                return None
            length = struct.unpack(">Q", ext)[0]
        payload = self._read(length) if length else b""
        if payload is None:
            return None
        return opcode, payload

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        self.sock.settimeout(timeout)
        got = self.recv()
        if got is None:
            return None
        opcode, payload = got
        if opcode == 0x8:
            return {"t": "__closed__"}
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return {"t": "__bad__", "raw": payload[:80].decode("utf-8", "replace")}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def http_get(path: str) -> tuple[int, bytes]:
    s = socket.create_connection((HOST, PORT), timeout=6)
    try:
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, HOST)).encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        code = int(head.split(b" ")[1]) if len(head.split(b" ")) > 1 else 0
        return code, body
    finally:
        s.close()


def wait_node(seconds: float = 40.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            code, _ = http_get("/healthz")
            if code == 200:
                return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    results.append((bool(ok), label + (("　→ " + extra) if extra else "")))
    print("   [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("　→ " + extra) if extra else ""))


def main() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WAVELINK_HOME"] = os.path.join(os.environ.get("TEMP", "."), "am-p2p-selftest")
    print("== 起一个源码节点（沙箱配置，不动用户设置）==")
    proc = subprocess.Popen([sys.executable, "-m", "audiomixing", "serve"],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_node():
            print("   节点没起来，放弃自检")
            return 2
        print("   节点就绪 ✓")

        print("== 1. 页面入口 ==")
        code, body = http_get("/p2p")
        has_page = code == 200 and b"RTCPeerConnection" in body and b"ws/p2p" in body
        check(has_page, "/p2p 返回手机直连页", "HTTP %d，%d 字节" % (code, len(body)))

        code2, body2 = http_get("/p2p?room=1")
        check(code2 == 200, "/p2p 带参数也能打开", "HTTP %d" % code2)

        print("== 2. 房间号校验 ==")
        c = WsClient(_url("/ws/p2p", role="host", room="12", name="甲"))
        check(not c.upgraded, "3 位房间号被拒（要 4 位数字）", "HTTP 首行: " + c.headers.split(b"\r\n")[0].decode("latin1"))
        c.close()

        print("== 3. 两个客户端配对 ==")
        room = "%04d" % (int(time.time()) % 10000)
        host = WsClient(_url("/ws/p2p", role="host", room=room, name="甲"))
        check(host.upgraded, "host 握手成功（101）")
        first = host.recv_json(timeout=2.0)
        check(first is None, "host 先独自等待，不主动说话", "收到: %r" % (first,))

        guest = WsClient(_url("/ws/p2p", role="guest", room=room, name="乙"))
        check(guest.upgraded, "guest 握手成功（101）")
        h_peer = host.recv_json()
        g_peer = guest.recv_json()
        check(h_peer and h_peer.get("t") == "peer" and h_peer.get("name") == "乙",
              "host 收到 peer（对端名字=乙）", repr(h_peer))
        check(g_peer and g_peer.get("t") == "peer" and g_peer.get("name") == "甲",
              "guest 收到 peer（对端名字=甲）", repr(g_peer))

        print("== 4. 原样转发 ==")
        offer = {"t": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 192.168.1.5\r\n中文也要原样"}
        host.send(json.dumps(offer, ensure_ascii=False))
        got = guest.recv_json()
        check(got == offer, "offer 原样到达 guest（含中文与 \\r\\n）", repr(got)[:70])

        ice = {"t": "ice", "candidate": '{"candidate":"candidate:1 1 udp ..."}', "mid": "0", "mlin": 0}
        guest.send(json.dumps(ice, ensure_ascii=False))
        got_ice = host.recv_json()
        check(got_ice == ice, "ice 反向原样到达 host")

        print("== 4b. 完整协商序列（照页面真实顺序再走一遍）==")
        # 页面实际会做的事：host 发 offer → guest 回 answer → 双向各若干条 ICE。
        # 这里按同样顺序、同样 JSON 形状跑一遍，验证服务端不挑食、不截断长 SDP、
        # 也不打乱 ICE 的顺序 —— 这些是"信令通了但 WebRTC 连不上"的常见原因。
        long_sdp = (
            "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\ns=-\r\n"
            + "".join("a=candidate:842163049 %d 1 udp 1677729535 192.168.0.%d 50000 typ host\r\n"
                      % (i, i % 250 + 1) for i in range(60))
            + "a=ice-ufrag:Xk29\r\na=ice-pwd:Zq8vLm3nP0oR5sT7uW1yA4bC\r\n"
        )
        host.send(json.dumps({"t": "offer", "sdp": long_sdp}, ensure_ascii=False))
        got_offer = guest.recv_json(timeout=6.0)
        check(bool(got_offer and got_offer.get("sdp") == long_sdp),
              "长 SDP（含 60 条候选）原样到达，没有被截断",
              "%d 字符 → %d 字符" % (len(long_sdp), len((got_offer or {}).get("sdp") or "")))

        answer = {"t": "answer", "sdp": long_sdp.replace("a=ice-ufrag:Xk29", "a=ice-ufrag:Guest")}
        guest.send(json.dumps(answer, ensure_ascii=False))
        got_answer = host.recv_json(timeout=6.0)
        check(bool(got_answer and got_answer.get("sdp") == answer["sdp"]),
              "answer 原样回到 host")

        ok_ice = True
        for i in range(5):
            host.send(json.dumps({"t": "ice", "candidate": "host-%d" % i, "mid": "0", "mlin": 0}))
            guest.send(json.dumps({"t": "ice", "candidate": "guest-%d" % i, "mid": "0", "mlin": 0}))
        for i in range(5):
            a1 = host.recv_json(timeout=4.0)
            b1 = guest.recv_json(timeout=4.0)
            if (not a1 or not b1
                    or a1.get("candidate") != "guest-%d" % i
                    or b1.get("candidate") != "host-%d" % i):
                ok_ice = False
                break
        check(ok_ice, "双向各 5 条 ICE 全部按顺序到达，没有丢也没有串",
              "host 收到 guest-0..4 ／ guest 收到 host-0..4")

        print("== 5. 第三个人进不来 ==")
        third = WsClient(_url("/ws/p2p", role="guest", room=room, name="丙"), timeout=4.0)
        err = third.recv_json(timeout=4.0)
        check(err and err.get("t") == "error", "第三人收到 error", repr(err))
        third.close()

        print("== 6. 二进制（音频）必须被拒收 ==")
        host.send("", opcode=0x2)          # 空二进制帧也算二进制
        time.sleep(0.3)
        sneaky = b"\x00\x01" * 64          # 假装是一小段音频
        host.sock.sendall(_masked_binary(sneaky))
        time.sleep(0.4)
        guest.sock.settimeout(1.2)
        leaked = guest.recv()
        check(leaked is None or leaked[0] not in (0x1, 0x2),
              "二进制帧没有转发给对端（绝不转发音频）", "" if leaked is None else "收到 opcode=%s" % leaked[0])

        print("== 7. 断开通知对端 ==")
        host.close()
        bye = guest.recv_json(timeout=4.0)
        check(bye and bye.get("t") == "bye", "host 断开后 guest 收到 bye", repr(bye))
        guest.close()

        print("== 8. 房间回收（同号可再用）==")
        a = WsClient(_url("/ws/p2p", role="host", room=room, name="甲"))
        b = WsClient(_url("/ws/p2p", role="guest", room=room, name="乙"))
        pa, pb = a.recv_json(), b.recv_json()
        check(bool(pa and pa.get("t") == "peer") and bool(pb and pb.get("t") == "peer"),
              "房间号释放后可以重新配对")
        a.close(); b.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            pass

    passed = sum(1 for ok, _ in results if ok)
    print("=" * 60)
    print("共 %d 项，通过 %d" % (len(results), passed))
    for ok, label in results:
        if not ok:
            print("   失败: " + label)
    print("RESULT: " + ("PASS" if passed == len(results) else "FAIL"))
    return 0 if passed == len(results) else 1


def _masked_binary(payload: bytes) -> bytes:
    mask = secrets.token_bytes(4)
    n = len(payload)
    head = bytearray([0x82, 0x80 | n]) if n < 126 else bytearray([0x82, 0x80 | 126]) + struct.pack(">H", n)
    head += mask
    return bytes(head) + bytes(b ^ mask[i & 3] for i, b in enumerate(payload))


if __name__ == "__main__":
    raise SystemExit(main())
