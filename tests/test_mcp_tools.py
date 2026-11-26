"""MCP 工具层的纯逻辑测试：不启动 HTTP，也不改真实配置。"""

from __future__ import annotations

import unittest

from audiomixing import mcp_server as mcp


class TestRouteMatching(unittest.TestCase):
    def test_direction_and_device_fields(self):
        route = {
            "to": ["peer-1"],
            "from": ["peer-2"],
            "sink_device": "sink-1",
            "source_device": "src-1",
        }
        self.assertTrue(mcp._route_matches(route, ["peer-1"], "to"))
        self.assertFalse(mcp._route_matches(route, ["peer-1"], "from"))
        self.assertTrue(mcp._route_matches(route, ["peer-2"], "from"))
        self.assertTrue(mcp._route_matches(route, ["sink-1"], "to"))
        self.assertTrue(mcp._route_matches(route, ["src-1"], "from"))

    def test_wildcard_matches(self):
        self.assertTrue(mcp._route_matches({"to": ["*"]}, ["anything"], "to"))
        self.assertFalse(mcp._route_matches({"to": ["*"]}, ["anything"], "from"))


class TestDeviceResolve(unittest.TestCase):
    def test_same_name_on_sink_and_source_is_ambiguous(self):
        peers = [{"id": "peer-1", "name": "设备A"}]
        devices = [("sinks", {"id": "sink-1", "name": "设备A"}),
                   ("sources", {"id": "src-1", "name": "设备A"})]
        matches = mcp._resolve("设备A", peers, devices)
        self.assertEqual({m["kind"] for m in matches}, {"peer", "sink", "source"})

    def test_id_exact_match(self):
        peers = []
        devices = [("sinks", {"id": "sink-1", "name": "耳机"})]
        matches = mcp._resolve("sink-1", peers, devices)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["kind"], "sink")


if __name__ == "__main__":
    unittest.main()