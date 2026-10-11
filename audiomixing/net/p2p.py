"""手机 ↔ 手机（P2P）信令的转发层：房间表 + 配对 + 文本转发。

设计说明
--------
* 这一层只做 `docs/P2P协议.md` 第二节写死的那三件事：按 room 把两个连接配对、
  把一方的**文本**消息原样转发给另一方、一方断开时通知另一方。不碰 HTTP，
  不认识 WebSocket 帧，也不需要知道 offer/answer/ice 长什么样。
* **音频永远不经过这里**。两端用 WebRTC 直连传声音，电脑只转发几十字节的 JSON，
  所以本模块既不接收也不缓存二进制：``relay()`` 只接受 ``str``，字节（二进制帧）
  一律拒收。这是协议第二条的那句"服务端绝不接收、缓存或转发任何音频数据"
  在代码里的落点 —— 不是靠"我们不会那么写"的自觉，而是让音频数据根本没有
  可以落脚的地方：一条连接对象上只有"发文本"和"关闭"两个回调，没有缓冲区。
* 不对消息做任何解析。``relay()`` 原样转发，所以将来协议加一种消息类型，
  服务端不用跟着改。
* 不缓存：对端不在（还没来 / 已经走）时消息直接丢弃，``relay()`` 返回 False。
  攒着等对端上线看着"贴心"，实际上是把过期的 SDP/ICE 喂给一个新对端，协商必失败。
* 线程安全：``audiomixing.webui`` 用的是 ``ThreadingHTTPServer``，同一个房间的
  两个连接跑在两个线程里，而且**对端线程会替你写你的 socket**（转发、bye 都是
  另一个线程发的）。所以房间表由一把可重入锁保护；锁内绝不调用 ``send_text``/
  ``close``（它们要写 socket，可能阻塞），一律先在锁内算清"该发给谁"，出锁再发。
  否则一台卡住的手机就能把整个房间表锁死。

对外接口
--------
    rooms = P2PRooms()
    conn = rooms.join(room, role, name, send_text, close)   # 被拒时返回 None
    rooms.relay(room, conn, text)                           # 原样转发给对端
    rooms.leave(room, conn)                                 # 连接断开时调用

``send_text``/``close`` 由调用方（webui.py）接到已有的 WebSocket 工具上
（``ws_frame`` 等），本模块只管"房间与转发"这一层。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

__all__ = ["P2PConn", "P2PRooms", "ROOM_FULL_MSG"]

# 一个房间两个人：一个 host（发起方）+ 一个 guest（加入方）。
# 协议里房间号是给"两台手机"用的，多出来的连接一律拒掉，免得配对关系变成三方。
MAX_PER_ROOM = 2

DEFAULT_NAME = "手机"
ROOM_FULL_MSG = "房间号已满"

BYE_TEXT = '{"t":"bye"}'


def _server_message(obj: dict[str, Any]) -> str:
    """服务端自己产生的消息（peer / bye / error）。

    手工指定紧凑分隔符，是为了让线上字节和协议文档里写的样子一模一样
    （``{"t":"peer","name":"小红"}``）：两端对着日志排查时，协议文档就是
    唯一的标准答案，多一个空格都是噪音。
    ``ensure_ascii=False`` 让中文名字按 UTF-8 原文出去（调用方负责编码）。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _peer_text(name: str) -> str:
    """配对成功时发给每一方的"对端已就位"。"""
    return _server_message({"t": "peer", "name": name})


# HWHHYN 用 DEEPSEEK 制作
class P2PConn:
    """一条已经进入房间的信令连接。

    刻意做成"只有一个出口"的薄壳：它记着房间号、角色、显示名和两个回调，
    没有任何存放消息内容的地方 —— 想在这层缓存音频也无处可放。
    """

    __slots__ = ("room", "role", "name", "send_text", "close", "joined_at")

    def __init__(
        self,
        room: str,
        role: str,
        name: str,
        send_text: Callable[[str], None],
        close: Callable[[], None],
    ) -> None:
        self.room = room
        self.role = role or "guest"
        self.name = (name or "").strip() or DEFAULT_NAME
        self.send_text = send_text
        self.close = close
        self.joined_at = time.time()

    def info(self) -> dict[str, Any]:
        """给面板/接口看的一行状态（不含任何消息内容）。"""
        return {"role": self.role, "name": self.name, "joined_at": round(self.joined_at, 3)}

    def __repr__(self) -> str:  # pragma: no cover - 只用于日志
        return "<P2PConn %s/%s %s>" % (self.room, self.role, self.name)


class P2PRooms:
    """所有 P2P 房间。整个 webui 共用一个实例，所有连接共用一张房间表。

    多线程使用是默认场景（每条 WebSocket 连接一个线程），所以方法全部可以
    从任意线程调用。回调也全部在锁外执行。
    """

    def __init__(self, max_per_room: int = MAX_PER_ROOM) -> None:
        self.max_per_room = max(2, int(max_per_room))
        self._rooms: dict[str, list[P2PConn]] = {}
        # 一把可重入锁足够：临界区里只动房间表，不做 IO。
        self._lock = threading.RLock()
        # 下面这些计数只为排查问题（日志、面板），不参与任何判断。
        self.joined = 0
        self.refused = 0
        self.relayed = 0
        self.dropped = 0
        self.rejected_binary = 0
        self.bye_sent = 0

    # -- 配对 ---------------------------------------------------------------
    def join(
        self,
        room: str,
        role: str,
        name: str,
        send_text: Callable[[str], None],
        close: Callable[[], None],
    ) -> P2PConn | None:
        """把一个连接放进房间，够两个人就配对。

        * 返回 ``P2PConn``：已进房。若房间里本来就有一个人，双方刚刚各收到一条
          ``{"t":"peer","name":...}``，接下来由 host 先发 offer。
        * 返回 ``None``：房间已满。error 已经回给这个连接、它也已经关掉了，
          调用方什么都不用做，直接 return。

        ``send_text(text)`` 必须可以从**任意线程**调用并保证整帧写出去不交错：
        转发和 bye 是对端的线程发的，不是自己的线程（见模块头"线程安全"）。
        ``close()`` 只在"房间已满"这一条路上由本模块调用，同样在锁外。
        """
        key = str(room or "").strip()
        conn = P2PConn(key, role, name, send_text, close)
        with self._lock:
            members = self._rooms.setdefault(key, [])
            if len(members) >= self.max_per_room:
                # 满了：先不碰这个连接，出锁再回话 —— 回 error 要写 socket，
                # 拿着房间表的锁做这件事，就是让一台慢手机拖住所有房间。
                full = True
                peer: P2PConn | None = None
            else:
                full = False
                members.append(conn)
                self.joined += 1
                # 第一个人进房只是等着（host 等 guest，guest 也可能先到等 host），
                # 第二个人进来才算配对。
                peer = members[0] if len(members) > 1 else None
        if full:
            self.refused += 1
            self._send(conn, _server_message({"t": "error", "msg": ROOM_FULL_MSG}))
            self._close(conn)
            return None
        if peer is not None:
            # 两边各发一条，各自负责各自的失败：一边的 socket 坏了，不该连累
            # 另一边收不到通知 —— 收不到 peer 的那个客户端只会显示"一直在等待"，
            # 这种问题在真机上最难查。
            self._send(peer, _peer_text(conn.name))
            self._send(conn, _peer_text(peer.name))
        return conn

    # -- 转发 ---------------------------------------------------------------
    def relay(self, room: str, conn: P2PConn, text: str) -> bool:
        """把一条消息**原样**转给同房间的对端，返回是否真的送出去了。

        没有对端、连接已经不在房间里，或者传进来的是字节（二进制帧）时返回
        ``False`` 并丢弃。不缓存、不改写、不解析：服务端是根管子，不是中间人。
        """
        if not isinstance(text, str):
            # 浏览器可以发二进制帧过来。协议的红线是"绝不接收、缓存或转发
            # 任何音频数据"，所以这里直接拒收 —— 丢掉一条越界消息，
            # 也不给它任何被转发或被记住的可能。
            self.rejected_binary += 1
            return False
        key = str(room or "").strip()
        with self._lock:
            members = self._rooms.get(key)
            if not members or conn not in members:
                # 已经离开（或从来没进来）：静静丢掉。调用方通常正在收尾，
                # 这时抛异常只会把一个正常的断开变成一个难看的报错。
                self.dropped += 1
                return False
            peer = next((m for m in members if m is not conn), None)
            if peer is None:
                # 对端还没来或已经走了。不缓存：见模块头。
                self.dropped += 1
                return False
            self.relayed += 1
        # 出锁再写 socket：一次转发卡住，只能卡住这两条连接。
        return self._send(peer, text)

    # -- 断开 ---------------------------------------------------------------
    def leave(self, room: str, conn: P2PConn) -> bool:
        """把连接从房间摘掉，并通知对端 ``{"t":"bye"}``。

        幂等：重复调用不再发第二条 bye（调用方常在 EOF 处理和 finally 里
        各叫一次，重复的 bye 会让对端以为又断了一次）。
        最后一个人走了就删掉房间条目 —— 房间号必须能回收，否则一天下来
        房间表会攒下一堆空壳，房间号也就慢慢"用完"了。
        """
        key = str(room or "").strip()
        with self._lock:
            members = self._rooms.get(key)
            if not members or conn not in members:
                return False
            members.remove(conn)
            peer = members[0] if members else None
            if not members:
                self._rooms.pop(key, None)
        if peer is not None:
            if self._send(peer, BYE_TEXT):
                self.bye_sent += 1
        return True

    # -- 状态（给面板/接口看，不参与逻辑） -----------------------------------
    def rooms(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"room": key, "count": len(members), "members": [m.info() for m in members]}
                for key, members in self._rooms.items()
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            room_count = len(self._rooms)
            conn_count = sum(len(m) for m in self._rooms.values())
        return {
            "rooms": room_count,
            "connections": conn_count,
            "joined": self.joined,
            "refused": self.refused,
            "relayed": self.relayed,
            "dropped": self.dropped,
            "rejected_binary": self.rejected_binary,
            "bye_sent": self.bye_sent,
        }

    # -- 内部：回调一律不抛 ---------------------------------------------------
    @staticmethod
    def _send(conn: P2PConn, text: str) -> bool:
        """发一条文本。失败只记 False，不抛。

        对端掉线是这条链路上的常态（手机锁屏、走进电梯），一次发送失败
        不该把房间表和另一个人的连接一起带下去。
        """
        try:
            conn.send_text(text)
            return True
        except Exception:
            return False

    @staticmethod
    def _close(conn: P2PConn) -> None:
        try:
            conn.close()
        except Exception:
            pass
