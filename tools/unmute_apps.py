"""把被 v2.40 误静音的 App 音频会话一键恢复（取消静音）。

背景（2026-11-16 致命 bug）：v2.40 的 `ISimpleAudioVolume` vtable 顺序按字母序写反，
`GetMute` 实际调到 `SetMute`，面板一打开就把每个正在出声的 App 会话静音了。
Windows 会把每个 App 的静音状态记进音频策略，**进程退出也不会自动恢复**，
所以需要这个脚本显式恢复。

用法：
    python tools/unmute_apps.py            # 直接取消所有会话静音
    python tools/unmute_apps.py --check    # 只看当前哪些会话是静音的，不改
"""

import os
import sys

# 脚本在 tools/ 下，仓库根是上一层；不写死绝对路径（换目录会静默失效）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from audiomixing.audio import apps as app_mod
    from audiomixing.audio.backends import wasapi as wasapi_backend
    from audiomixing.audio.backends import wasapi_com as wc

    if not sys.platform.startswith("win"):
        print("只在 Windows 上有音频会话，这个脚本没有可做的事。")
        return 1
    try:
        wasapi_backend._ensure_com()
        before = wc.session_mute_states()
    except Exception as exc:
        print("读不到音频会话：%s" % exc)
        return 1

    muted_pids = sorted({row["pid"] for row in before if row["muted"]})
    print("当前活动会话 %d 个，其中静音 %d 个%s"
          % (len(before), len(muted_pids),
             ("（PID: %s）" % ", ".join(str(p) for p in muted_pids)) if muted_pids else ""))
    if "--check" in argv:
        return 0
    if not muted_pids:
        print("没有需要恢复的会话。")
        return 0

    result = app_mod.unmute_all_sessions()
    if not result.get("ok"):
        print("恢复失败：%s" % result.get("reason"))
        return 1
    print("已恢复：取消静音 %d 个（扫描到 %d 个会话）。"
          % (result.get("changed"), result.get("total")))
    after = wc.session_mute_states()
    still = sorted({row["pid"] for row in after if row["muted"]})
    if still:
        print("仍有静音会话（可能已被独占或刚重新静音）：PID %s"
              % ", ".join(str(p) for p in still))
        return 1
    print("现在所有活动会话都不静音了。若某个 App 还没声，请在它自己的音量合成器里确认音量没被拉到 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
