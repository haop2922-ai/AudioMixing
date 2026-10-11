"""Regression tests for the multi-machine routing bugs fixed on 2026-10-11/12.

每个用例都对应一次真实故障（两台电脑实测复现），目的是让它们不会再回来。
这些 bug 的共同特征是：单机测试全绿，但两台电脑一起跑就出问题 ——
所以这里刻意用【两个节点对象 + 显式 author_id】来模拟"两台电脑共享一张路由表"。

    python -m unittest tests.test_route_regressions -v
"""

from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试会构造真实的 WaveLinkNode，而 set_routes 默认会落盘 ——
# 必须沙箱化，否则会往用户的 %APPDATA% 写测试端口和测试节点名。
_TEST_SANDBOX = tempfile.mkdtemp(prefix="audiomixing-regress-")
os.environ["WAVELINK_HOME"] = _TEST_SANDBOX
atexit.register(lambda: shutil.rmtree(_TEST_SANDBOX, ignore_errors=True))

from audiomixing.config import Config          # noqa: E402
from audiomixing.engine import WaveLinkNode    # noqa: E402


def free_udp_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def config_stub(name: str = "本机", node_id: str = "node-self") -> Config:
    cfg = Config()
    cfg.name = name
    cfg.node_id = node_id
    cfg.autostart = False
    cfg.local = False
    cfg.broadcast = False
    cfg.listen = False
    return cfg


def make_node(name: str = "本机", node_id: str = "node-self") -> WaveLinkNode:
    return WaveLinkNode(config_stub(name, node_id),
                        audio_port=free_udp_port(),
                        discovery_port=free_udp_port(),
                        simulate=True)


def route(rid: str, src: str = "node-a", dst: str = "node-b") -> dict:
    return {"id": rid, "from": [src], "to": [dst], "gain": 1.0}


class RouteOwnershipTests(unittest.TestCase):
    """归属决定"谁的推送有资格替换它"，被改写就会误删别人的路由。"""

    def setUp(self) -> None:
        self.node = make_node("本机", "node-self")
        self.addCleanup(self.node.stop)

    def test_panel_style_push_does_not_steal_ownership(self) -> None:
        """面板提交【整张表】时，不能把别人建的路由过户到自己名下。

        现场故障：甲建的路由被乙的一次保存改成"归属乙"，乙之后推送自己的表
        （里面没有它），这条路由就被当成"作者已撤回"删掉 —— 监控里显示"残留"、
        字节数 0、对端永远不响。
        """
        self.node.set_routes([route("r-owner", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        before = {r["id"]: r["origin"] for r in self.node.routes}
        self.assertEqual(before.get("r-owner"), "node-a")

        # 乙（node-self）把整张表原样提交回来 —— 这就是面板的日常保存
        whole_table = [dict(r) for r in self.node.routes]
        self.node.set_routes(whole_table, persist=False, author_id="node-self", author_name="本机")

        after = {r["id"]: r["origin"] for r in self.node.routes}
        self.assertEqual(after.get("r-owner"), "node-a",
                         "归属被面板保存改写了：原作者下一次推送会把这条路由误删")

    def test_author_push_replaces_only_its_own_share(self) -> None:
        """甲推送自己的表，只能替换甲名下的路由，不能动乙的。"""
        self.node.set_routes([route("r-a", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        self.node.set_routes([route("r-b", "node-b", "node-self")],
                             persist=False, author_id="node-b", author_name="乙")
        ids = {r["id"] for r in self.node.routes}
        self.assertEqual(ids, {"r-a", "r-b"})

        # 甲重新推送，且只带自己的一条 —— 乙那条必须留着
        self.node.set_routes([route("r-a2", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        ids = {r["id"] for r in self.node.routes}
        self.assertIn("r-b", ids, "甲的推送删掉了乙的路由")
        self.assertIn("r-a2", ids)
        self.assertNotIn("r-a", ids, "甲撤回了自己的旧路由，应该被替换掉")


class DeletedRouteTests(unittest.TestCase):
    """被显式删除的路由，短期内任何入口都不许复活；但也不能永久拉黑。"""

    def setUp(self) -> None:
        self.node = make_node("本机", "node-self")
        self.addCleanup(self.node.stop)

    def test_explicit_delete_cannot_be_undone_by_a_stale_panel(self) -> None:
        """浏览器里那份旧表 PUT 回来时，删除结果必须保住。

        现场故障：删掉一条路由后，面板下一次自动保存又把它 PUT 回来 ——
        两边来回删/加，界面显示"残留"，音频链路反复重建。
        """
        self.node.set_routes([route("r-del", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        self.assertEqual(len(self.node.routes), 1)

        # 删除：本机把 id 记进删除表，并通知对端
        self.node.set_routes([], persist=False, drop=["r-del"])
        self.assertEqual([r["id"] for r in self.node.routes], [])

        # 面板（或对端）拿着旧表把它推回来 —— 不许复活
        self.node.set_routes([route("r-del", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        self.assertEqual([r["id"] for r in self.node.routes], [],
                         "已删除的路由被旧表复活了")

    def test_dropped_route_comes_back_after_the_ttl(self) -> None:
        """删除记录必须有有效期，否则对端重建同名路由也永远回不来。"""
        self.node.set_routes([route("r-ttl", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        self.node.set_routes([], persist=False, drop=["r-ttl"])
        self.assertEqual([r["id"] for r in self.node.routes], [])

        # 把删除时间往前拨，模拟过了有效期
        import time as _time

        self.node._dropped_ids["r-ttl"] = _time.time() - (WaveLinkNode.DROPPED_TTL + 1.0)

        self.node.set_routes([route("r-ttl", "node-a", "node-b")],
                             persist=False, author_id="node-a", author_name="甲")
        self.assertEqual([r["id"] for r in self.node.routes], ["r-ttl"],
                         "过了有效期之后同名路由仍然回不来 —— 删除记录成了永久拉黑")


class StaleSourceDeviceTests(unittest.TestCase):
    """配置里记着的采集设备消失后，不能一直拿旧名字去开。"""

    def setUp(self) -> None:
        self.node = make_node("本机", "node-self")
        self.addCleanup(self.node.stop)

    def _fake_devices(self, names: list[str]) -> None:
        # simulate=True 时 default_source_spec() 会返回测试音，这里要测的是真实选择逻辑
        self.node.simulate = False
        items = []
        for i, name in enumerate(names):
            items.append(types.SimpleNamespace(
                name=name, kind="sink", kind_detail="render",
                backend="wasapi", is_default=(i == 0)))
        self.node.devices = items

    def test_missing_configured_device_falls_back_to_a_live_one(self) -> None:
        """设备拔掉/断开之后，采集源要自动退回当前可用的设备。

        现场故障：配置里记着"耳机 (某型号)"，设备断开后采集端每次都
        start failed，一个字节都产不出来 —— 对端永远听不到声音。
        """
        self._fake_devices(["扬声器 (可用的)", "耳机 (还在的)"])
        self.node.config.source_device = "耳机 (已经拔掉的型号)"

        spec = self.node.default_source_spec()
        self.assertNotEqual(spec.get("device"), "耳机 (已经拔掉的型号)",
                            "仍然拿着一个不存在的设备名去采集")
        self.assertIn(spec.get("device"), ["扬声器 (可用的)", "耳机 (还在的)"])

    def test_existing_configured_device_is_kept(self) -> None:
        """设备还在的时候，必须尊重用户的选择，不能自作主张换掉。"""
        self._fake_devices(["扬声器 (可用的)", "耳机 (还在的)"])
        self.node.config.source_device = "耳机 (还在的)"

        spec = self.node.default_source_spec()
        self.assertEqual(spec.get("device"), "耳机 (还在的)",
                         "用户选定的采集设备还在，却被换掉了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
