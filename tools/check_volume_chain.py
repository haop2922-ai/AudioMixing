"""电脑侧验证「送声音」音量链路：乘数、诊断判定、"不重复衰减"。

为什么单独做这个：
  2026-11-07 用户把手机连上电脑要求端到端验、但 adb 一直 unauthorized。
  A 项里"拿真机调媒体音量看读数"必须手机参与，电脑侧替不了；
  但手机上报的那五个数（vol_q14 / gain_q14 / total_q14 / raw_peak / post_peak）
  在电脑侧的处理与判定是**可以完整验证**的 —— 构造手机"应该发的那几个数"，
  喂进真实入口 `_handle_peer_requests()`，再用真实的 `AudioMixingApi.diagnose()`
  渲染出用户看到的那段中文，逐条断言。

被测的是电脑端，不是重写手机算法：
  `total_q14` 按手机端 AudioSend.java 的口径生成（gain*media/GAIN_ONE，上限 2.0），
  只是为了造输入样本；`post_peak/raw_peak` 按"增益是否真的生效"来造。

    python tools/check_volume_chain.py
退出码 0 = 全部通过，1 = 有断言失败。
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# 诊断报告会写到 %APPDATA%\Audio Mixing\诊断报告.txt；自测不许写用户的 AppData。
_SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="am-volchain-"))
os.environ["APPDATA"] = str(_SANDBOX)
os.environ["AUDIOMIXING_HOME"] = str(_SANDBOX)

GAIN_ONE = 16384
LEVELS = (1.0, 0.75, 0.5, 0.25, 0.1)


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _total_q14(gain: float, media: float) -> int:
    """照抄手机端：total = gain * media / 16384，上限 2.0（AudioSend.java）。"""
    g = int(round(gain * GAIN_ONE))
    m = int(round(media * GAIN_ONE))
    return min(GAIN_ONE * 2, (g * m) // GAIN_ONE)


class Harness:
    def __init__(self) -> None:
        from audiomixing.config import Config
        from audiomixing.engine import AudioMixingNode
        from audiomixing.webui import AudioMixingApi

        cfg = Config()
        cfg.node_id = "self-node"
        cfg.name = "本机"
        cfg.routes = []
        cfg.codec = "pcm_s16"
        cfg.phone_volume_mode = "auto"
        self.node = AudioMixingNode(cfg, audio_port=_free_udp_port(),
                                    discovery_port=_free_udp_port(), simulate=True)
        self.node.devices = []
        self.node.registry.update({"wl": 1, "id": "phone-a", "name": "手机 A",
                                   "audio_port": 47831, "web_port": 0}, "127.0.0.1")
        self.api = AudioMixingApi(self.node)
        self._at = 0.0

    def feed(self, media: float, gain: float, raw: float,
             *, declared_scaled: bool = True, source: str | None = None,
             policy: str = "internal_only", send_mode: int = 1) -> int:
        """把"手机这一帧的统计包"送进真实解析入口，返回 total_q14。"""
        total = _total_q14(gain, media)
        # 增益没被抹平时 post = raw * total/16384；被抹平时 post == raw。
        post = round(raw * (total / float(GAIN_ONE)), 4) if declared_scaled else round(raw, 4)
        # `_handle_peer_requests()` 把已处理水位写成 time.monotonic()，
        # 而这里构造的 `_at` 是从 0 开始的小数；不重置水位的话只有第一条会被处理。
        self._at = time.monotonic() + 0.2
        self.node.receiver.control_messages.clear()
        self.node._handled_stats_at = 0.0
        msg = {
            "_type": "STATS", "_at": self._at, "_from": "127.0.0.1:47999",
            "t": "stats", "node": "phone-a", "name": "手机 A",
            "vol": round(media, 3), "vol_in_capture": True,
            "send_mode": send_mode, "broadcast": False,
            "vol_q14": int(round(media * GAIN_ONE)), "gain_q14": int(round(gain * GAIN_ONE)),
            "total_q14": total, "raw_peak": raw, "post_peak": post,
        }
        if source:
            # ★ 2026-11-08：外放档 false / 静音档 true，按档位真实取值。
            msg["vol_mode"] = source
            msg["vol_source"] = source
            msg["vol_policy"] = policy
            msg["vol_in_capture"] = (source == "silent")
        self.node.receiver.control_messages.append(msg)
        self.node._handle_peer_requests()
        return total

    def diagnose_line(self) -> str:
        text = self.api.diagnose().get("text") or ""
        for line in text.splitlines():
            if "手机采集层" in line:
                return line
        return ""

    def series_line(self) -> str:
        text = self.api.diagnose().get("text") or ""
        for line in text.splitlines():
            if line.startswith("vol 序列"):
                return line
        return ""

    def pc_factor(self) -> float:
        from types import SimpleNamespace
        rule = {"gain": 1.0, "from": ["phone-a"], "to": ["self-node"]}
        sink = SimpleNamespace()
        self.node._apply_sink_gain(sink, rule)
        return float(getattr(sink, "volume_factor", 1.0))

    def close(self) -> None:
        try:
            self.node.stop()
        except Exception:
            pass


def main() -> int:
    h = Harness()
    failures: list[str] = []
    try:
        print("=" * 96)
        print("A. 「送声音」音量链路（电脑侧：解析 + 判定；手机侧只负责按这个口径上报）")
        print("=" * 96)

        for group, label in ((1.0, "第 1 档（手机外放不静音）"), (0.35, "第 2 档（静音档：STREAM_MUSIC 钉 0，乘数取用户最后设定值）")):
            print("\n%s" % label)
            print("  %-6s %-9s %-9s %-10s %-8s %-8s %-8s %-9s %-10s %s" % (
                "音量", "vol_q14", "gain_q14", "total_q14", "raw", "post", "post/raw",
                "应≈", "PC 系数", "诊断判定"))
            for media in LEVELS:
                raw = 0.80
                total = h.feed(media, group, raw)
                line = h.diagnose_line()
                ratio = (round(raw * total / GAIN_ONE, 4) / raw)
                expect = total / float(GAIN_ONE)
                ok_math = abs(ratio - expect) <= 0.005
                ok_verdict = "乘法生效 ✓" in line
                factor = h.pc_factor()
                ok_factor = abs(factor - 1.0) < 1e-6
                print("  %-6.2f %-9d %-9d %-10d %-8.2f %-8.2f %-8.3f %-9.3f %-10.2f %s" % (
                    media, int(round(media * GAIN_ONE)), int(round(group * GAIN_ONE)),
                    total, raw, round(raw * expect, 4), ratio, expect, factor,
                    "✓" if (ok_math and ok_verdict and ok_factor) else "✗"))
                if not ok_math:
                    failures.append("%s vol=%.2f：post/raw 与 total/16384 不符" % (label, media))
                if not ok_verdict:
                    failures.append("%s vol=%.2f：诊断没判「乘法生效」→ %s" % (label, media, line))
                if not ok_factor:
                    failures.append("%s vol=%.2f：PC 又乘了一次（系数 %.2f，应 1.0）"
                                    % (label, media, factor))
                if media == LEVELS[-1]:
                    print("  诊断原样：%s" % line)

        series = h.series_line()
        print("\n  诊断里的 vol 序列：%s" % (series or "（没有）"))
        if "有变化 ✓" not in series:
            failures.append("音量逐档变化后，诊断的 vol 序列没有标「有变化」")

        print("\nB. 反向对照：增益被后续环节抹平（post == raw）时必须判「没生效 ✗」")
        h.feed(0.25, 1.0, 0.80, declared_scaled=False)
        line = h.diagnose_line()
        print("  诊断原样：%s" % line)
        if "乘法没生效 ✗" not in line:
            failures.append("post==raw 时诊断没有判「乘法没生效」，判定可能是写死的")

        print("\nC. 强制档位（phone_volume_mode）仍然优先，且静音档 0 不会让整条链路哑掉")
        h.node.config.phone_volume_mode = "off"
        off = h.pc_factor()
        h.node.config.phone_volume_mode = "on"
        on = h.pc_factor()
        h.node.config.phone_volume_mode = "auto"
        auto = h.pc_factor()
        print("  强制关=%.2f　强制开=%.2f　自动=%.2f（手机已声明 vol_in_capture → 自动不叠加）"
              % (off, on, auto))
        if abs(off - 1.0) > 1e-6:
            failures.append("强制关应当不叠加")
        if abs(auto - 1.0) > 1e-6:
            failures.append("手机声明了 vol_in_capture，自动模式仍叠乘了 vol")

        print("\nD. 2026-11-08 用户方案：两档的声明与电脑端系数（合计只乘一次）")
        print("  %-10s %-10s %-14s %-10s %s" % ("档位", "vol_in_capture", "策略", "PC 系数", "判定"))
        for source, cap in (("external", False), ("silent", True)):
            for policy in ("internal_only", "last_nonzero"):
                if source == "external" and policy != "internal_only":
                    continue        # 策略只影响静音档
                h.node.config.phone_volume_mode = "auto"
                h.feed(0.25 if source == "external" else 1.0, 0.6, 0.8,
                       source=source, policy=policy, send_mode=1 if source == "external" else 2)
                factor = h.pc_factor()
                note = h.diagnose_line()
                ok = abs(factor - 1.0) < 1e-6 and (
                    ("外放档" in note) if source == "external" else ("静音档" in note))
                print("  %-10s %-10s %-14s %-10.2f %s" % (source, cap, policy, factor,
                                                          "✓" if ok else "✗"))
                if not ok:
                    failures.append("%s/%s：PC 系数应 1.0 且诊断写清档位 → %.2f %s"
                                    % (source, policy, factor, note))
    finally:
        h.close()

    print("\n" + "=" * 96)
    if failures:
        print("RESULT: FAIL（%d 项）" % len(failures))
        for item in failures:
            print("  ✗ %s" % item)
        return 1
    print("RESULT: PASS —— 电脑侧的乘数换算、诊断判定、「手机已衰减就不再乘」全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
