"""P2P 信令转发层（audiomixing/net/p2p.py）的回归测试。

这里刻意**不开真 socket**：这一层要测的是"房间表"的逻辑（配对 / 转发 / 回收），
用假连接对象就能把每条规则钉死，而且整个文件跑完是毫秒级 —— 真开 socket 只会
让用例变慢、并给"安全软件拦了一下"这类偶发失败留门。

每个用例对应协议 docs/P2P协议.md 里的一条硬要求，或者接线时真会踩的一个坑：

    python -m unittest tests.test_p2p -v
"""

from __future__ import annotations

import pathlib
import sys
import threading
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audiomixing.net.p2p import P2PConn, P2PRooms  # noqa: E402

PEER_HOST = '{"t":"peer","name":"小红"}'   # guest 该收到的那条
PEER_GUEST = '{"t":"peer","name":"小蓝"}'  # host 该收到的那条
BYE = '{"t":"bye"}'
FULL = '{"t":"error","msg":"房间号已满"}'


class FakePeer:
    """假的对端连接：只把"发出去的文本"记在列表里。

    ``send_text`` / ``close`` 的签名和 webui 接线时要传进去的那两个回调完全一样，
    所以这里测出来的行为就是真连接上的行为。
    """

    def __init__(self, name: str = "手机") -> None:
        self.name = name
        self.sent: list[str] = []
        self.closed = False
        self.broken = False

    def send_text(self, text: str) -> None:
        if self.broken or self.closed:
            # 真 socket 在对端没了之后就是这个样子：写进去直接抛 OSError。
            raise OSError("对端已经没了")
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True

    def texts(self) -> list[str]:
        return list(self.sent)

    def clear(self) -> None:
        self.sent.clear()


def make_pair(rooms: P2PRooms, room: str = "1234"):
    """建一对连上的 host/guest，返回 (host, guest, host_conn, guest_conn)。"""
    host, guest = FakePeer("小红"), FakePeer("小蓝")
    host_conn = rooms.join(room, "host", "小红", host.send_text, host.close)
    guest_conn = rooms.join(room, "guest", "小蓝", guest.send_text, guest.close)
    return host, guest, host_conn, guest_conn


class PairingTests(unittest.TestCase):
    """配对：够两个人才通知，通知里只带对端的名字。"""

    def test_first_connection_waits_quietly_until_the_second_arrives(self) -> None:
        """只有一个人时不能发 peer —— 发了对面就会开始 createOffer，而没人接。"""
        rooms = P2PRooms()
        host = FakePeer("小红")
        self.assertIsNotNone(rooms.join("1234", "host", "小红", host.send_text, host.close))
        self.assertEqual(host.texts(), [], "对端还没来就发了 peer")

        guest = FakePeer("小蓝")
        self.assertIsNotNone(rooms.join("1234", "guest", "小蓝", guest.send_text, guest.close))
        self.assertEqual(host.texts(), [PEER_GUEST])
        self.assertEqual(guest.texts(), [PEER_HOST])

    def test_different_rooms_never_see_each_other(self) -> None:
        """两台手机输错一位房间号时，最坏的结果必须是"一直等待"，不是串音。"""
        rooms = P2PRooms()
        host, _guest, _hc, _gc = make_pair(rooms, "1234")
        other = FakePeer("别人")
        rooms.join("4321", "guest", "别人", other.send_text, other.close)
        self.assertEqual(host.texts(), [PEER_GUEST], "另一个房间的人插进了这个房间")


class RelayTests(unittest.TestCase):
    """转发：原样、只给对方、不给自己。"""

    def test_messages_are_forwarded_verbatim_to_the_other_side_only(self) -> None:
        rooms = P2PRooms()
        host, guest, host_conn, guest_conn = make_pair(rooms)

        # 真实 SDP 里全是 \r\n，服务端但凡"顺手整理一下"就会毁掉协商。
        offer = '{"t":"offer","sdp":"v=0\\r\\no=- 1 2 IN IP4 127.0.0.1\\r\\n"}'
        self.assertTrue(rooms.relay("1234", host_conn, offer))
        self.assertEqual(guest.texts(), [PEER_HOST, offer], "offer 没有原样到达 guest")
        self.assertEqual(host.texts(), [PEER_GUEST], "服务端把消息回声给了发送方")

        # ICE 是多条、且两个方向都要走同一条路
        for i in range(3):
            cand = '{"t":"ice","candidate":"candidate:%d 1 udp 1 192.168.1.5 5000 typ host","mid":"0","mlin":0}' % i
            self.assertTrue(rooms.relay("1234", guest_conn, cand))
            self.assertEqual(host.texts()[-1], cand)

    def test_message_from_a_connection_that_never_joined_is_dropped(self) -> None:
        """被拒绝的连接如果还被继续读帧，不能借它把消息塞进房间。"""
        rooms = P2PRooms()
        host, guest, _hc, _gc = make_pair(rooms)
        stranger = FakePeer("路人")
        # 这正是"房间已满被拒"之后还会拿到的东西：一个没有进过房的句柄。
        stranger_conn = P2PConn("1234", "guest", "路人", stranger.send_text, stranger.close)
        self.assertFalse(rooms.relay("1234", stranger_conn, '{"t":"offer","sdp":"bad"}'))
        self.assertEqual(guest.texts(), [PEER_HOST])
        self.assertEqual(host.texts(), [PEER_GUEST])


class FullRoomTests(unittest.TestCase):
    """第三个连接：回 error、被关闭，而且不许打扰已经在房里的两个人。"""

    def test_third_connection_gets_error_and_is_closed(self) -> None:
        rooms = P2PRooms()
        host, guest, _hc, _gc = make_pair(rooms)

        third = FakePeer("第三台")
        self.assertIsNone(rooms.join("1234", "guest", "第三台", third.send_text, third.close))
        self.assertEqual(third.texts(), [FULL], "第三个连接没有收到 房间号已满")
        self.assertTrue(third.closed, "第三个连接收到 error 之后没有被关闭")
        self.assertFalse(host.closed)
        self.assertFalse(guest.closed)
        # 已经在房里的两个人不该看到任何变化（再发一条 peer 会让页面重启动协商）
        self.assertEqual(host.texts(), [PEER_GUEST])
        self.assertEqual(guest.texts(), [PEER_HOST])
        self.assertEqual(rooms.stats()["connections"], 2)

    def test_a_freed_slot_can_be_taken_again(self) -> None:
        """有人走了之后，房间必须重新接受新连接 —— 否则一次误连就废掉这个房间号。"""
        rooms = P2PRooms()
        host, guest, _host_conn, guest_conn = make_pair(rooms)
        rooms.leave("1234", guest_conn)

        newcomer = FakePeer("新来的")
        self.assertIsNotNone(rooms.join("1234", "guest", "新来的", newcomer.send_text, newcomer.close))
        self.assertEqual(newcomer.texts(), ['{"t":"peer","name":"小红"}'])
        self.assertEqual(host.texts()[-1], '{"t":"peer","name":"新来的"}', "host 不知道新对端是谁")
        self.assertEqual(guest.texts(), [PEER_HOST])  # 走了的那个不该再收到东西


class DisconnectTests(unittest.TestCase):
    """断开：留下的人必须立刻知道，而且消息绝不为离开的人攒着。"""

    def test_leaving_notifies_the_peer_with_bye(self) -> None:
        rooms = P2PRooms()
        host, guest, _host_conn, guest_conn = make_pair(rooms)
        host.clear()
        guest.clear()

        self.assertTrue(rooms.leave("1234", guest_conn))
        self.assertEqual(host.texts(), [BYE], "guest 断开后 host 没有收到 bye")
        self.assertEqual(guest.texts(), [], "bye 是发给留下那个人的，不该回给走的人")

        # 反方向同理：换一对新的连接（上一对里，留下的人再走时已经没人可通知了）
        host2, guest2 = FakePeer("甲"), FakePeer("乙")
        host2_conn = rooms.join("5678", "host", "甲", host2.send_text, host2.close)
        rooms.join("5678", "guest", "乙", guest2.send_text, guest2.close)
        host2.clear()
        guest2.clear()
        self.assertTrue(rooms.leave("5678", host2_conn))
        self.assertEqual(guest2.texts(), [BYE], "host 断开后 guest 没有收到 bye")
        self.assertEqual(host2.texts(), [], "bye 不该回给走的那个人")

    def test_leave_is_idempotent(self) -> None:
        """调用方常在 EOF 处理和 finally 里各叫一次 leave，重复不能变成两条 bye。"""
        rooms = P2PRooms()
        host, _guest, host_conn, guest_conn = make_pair(rooms)
        host.clear()
        self.assertTrue(rooms.leave("1234", guest_conn))
        self.assertFalse(rooms.leave("1234", guest_conn), "第二次 leave 应该什么也不做")
        self.assertFalse(rooms.leave("1234", guest_conn))
        self.assertEqual(host.texts(), [BYE], "重复 leave 发出了多条 bye")

    def test_message_after_the_peer_left_is_dropped_not_queued(self) -> None:
        """对端不在时的消息必须丢掉：替它攒着，等于把过期的协商信息喂给下一个对端。"""
        rooms = P2PRooms()
        host, _guest, host_conn, guest_conn = make_pair(rooms)
        rooms.leave("1234", guest_conn)
        host.clear()

        stale = '{"t":"offer","sdp":"这是给上一个对端的过期协商"}'
        self.assertFalse(rooms.relay("1234", host_conn, stale), "对端不在却报告转发成功")

        newcomer = FakePeer("新来的")
        rooms.join("1234", "guest", "新来的", newcomer.send_text, newcomer.close)
        self.assertEqual(newcomer.texts(), ['{"t":"peer","name":"小红"}'],
                         "服务端把离开前的那条 offer 补发给了新对端")


class RecycleTests(unittest.TestCase):
    """房间回收：房间里没人了就必须删掉，否则房间号会被慢慢耗光。"""

    def test_room_is_dropped_when_the_last_member_leaves(self) -> None:
        rooms = P2PRooms()
        _host, _guest, host_conn, guest_conn = make_pair(rooms)
        self.assertEqual(rooms.stats()["rooms"], 1)

        rooms.leave("1234", host_conn)
        self.assertEqual(rooms.stats()["rooms"], 1, "房里还有一个人，房间不能被删")

        rooms.leave("1234", guest_conn)
        self.assertEqual(rooms.stats()["rooms"], 0, "两个人都走了房间还留着 —— 房间号会被耗尽")
        self.assertEqual(rooms.stats()["connections"], 0)

        # 同一个房间号必须能立刻被新的一对使用
        first, second = FakePeer("甲"), FakePeer("乙")
        self.assertIsNotNone(rooms.join("1234", "host", "甲", first.send_text, first.close))
        self.assertIsNotNone(rooms.join("1234", "guest", "乙", second.send_text, second.close))
        self.assertEqual(first.texts(), ['{"t":"peer","name":"乙"}'])
        self.assertEqual(second.texts(), ['{"t":"peer","name":"甲"}'])

    def test_failed_send_does_not_break_the_room_table(self) -> None:
        """一个对端的 socket 坏了，不能连累房间表，也不能让另一个人的断开卡住。"""
        rooms = P2PRooms()
        host, _guest, host_conn, guest_conn = make_pair(rooms)
        host.broken = True  # host 的 socket 实际上已经断了

        # 转发失败只报 False；guest 自己的连接不该因此被关或抛异常
        self.assertFalse(rooms.relay("1234", guest_conn, '{"t":"ice","candidate":"x"}'))
        self.assertFalse(host.closed)
        # guest 断开时，给已经坏掉的 host 发 bye 会失败，但房间表必须照常更新
        self.assertTrue(rooms.leave("1234", guest_conn))
        self.assertEqual(rooms.stats()["connections"], 1, "发 bye 失败把 guest 留在了表里")
        self.assertTrue(rooms.leave("1234", host_conn))
        self.assertEqual(rooms.stats()["rooms"], 0, "发 bye 失败把房间条目留在了表里")


class BinaryTests(unittest.TestCase):
    """协议红线：服务端绝不接收、缓存或转发任何音频/二进制数据。"""

    def test_binary_frames_are_refused_and_leave_no_trace(self) -> None:
        rooms = P2PRooms()
        host, guest, host_conn, _guest_conn = make_pair(rooms)
        host.clear()
        guest.clear()

        blob = bytes(range(256)) * 8  # 假装有人想借这条路传音频
        self.assertFalse(rooms.relay("1234", host_conn, blob), "二进制被当成消息转发了")
        self.assertEqual(guest.texts(), [], "二进制数据到达了对端")
        self.assertEqual(rooms.stats()["rejected_binary"], 1)

        # 结构上的保证：房间表对象上没有任何字节缓冲区可以存住它
        for name, value in vars(rooms).items():
            self.assertNotIsInstance(value, (bytes, bytearray, memoryview),
                                     "房间表上出现了字节缓冲区 %s —— 协议不允许服务端持有二进制" % name)

        # 拒掉一条越界消息之后，这条连接还得能正常转发文本
        self.assertTrue(rooms.relay("1234", host_conn, '{"t":"ice","candidate":"ok"}'))
        self.assertEqual(guest.texts(), ['{"t":"ice","candidate":"ok"}'])


class ConcurrencyTests(unittest.TestCase):
    """webui 是 ThreadingHTTPServer：同一房间的两个连接在两个线程里跑。

    而且"替你写 socket"是常态（转发和 bye 都是对端的线程发的），所以房间表
    必须在并发下也是对的 —— 单线程测试全绿、两台手机一起连就串房间，
    正是这类代码最容易出的问题。
    """

    def test_concurrent_joins_pair_exactly_two(self) -> None:
        rooms = P2PRooms()
        peers = [FakePeer("手机%d" % i) for i in range(16)]
        barrier = threading.Barrier(len(peers))
        results: list[Any] = [None] * len(peers)

        def worker(i: int) -> None:
            barrier.wait()
            results[i] = rooms.join("1234", "host" if i % 2 else "guest", peers[i].name,
                                    peers[i].send_text, peers[i].close)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(peers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 2, "16 个连接同时进同一个房间，配上的不是 2 个")
        self.assertEqual(rooms.stats()["connections"], 2)
        self.assertEqual(rooms.stats()["refused"], len(peers) - 2)
        self.assertEqual(sum(1 for p in peers if p.closed), len(peers) - 2, "被拒的连接没有全部关闭")

    def test_concurrent_relay_never_loses_or_duplicates_a_message(self) -> None:
        rooms = P2PRooms()
        host, guest, host_conn, guest_conn = make_pair(rooms)
        host.clear()
        guest.clear()
        per_side = 100
        results: list[bool] = []

        def pump(conn, side: str) -> None:
            for i in range(per_side):
                results.append(rooms.relay("1234", conn, '{"t":"ice","side":"%s","n":%d}' % (side, i)))

        threads = [
            threading.Thread(target=pump, args=(host_conn, "host")),
            threading.Thread(target=pump, args=(guest_conn, "guest")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertTrue(all(results), "并发转发时有消息没送出去")
        self.assertEqual(len(host.texts()), per_side, "host 收到的条数不对")
        self.assertEqual(len(guest.texts()), per_side, "guest 收到的条数不对")
        self.assertEqual(rooms.stats()["relayed"], per_side * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
