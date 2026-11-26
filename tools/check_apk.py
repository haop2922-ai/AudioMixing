"""APK 验收检查：清单节点、类是否都在、中文有没有被编码坑弄乱。

不依赖任何第三方库，也不启动任何 exe —— 直接读 zip 和 dex。
（aapt2 的清单 dump 由调用方另跑，这里补它看不到的东西：dex 里的类与中文字符串。）

    python tools/check_apk.py [apk路径]
    python tools/check_apk.py --release [apk路径]     # 交付前：版本/符号/各副本哈希

★ 2026-11-03（Q3）：以前只查 debug 构建，交付时把 2.9 的包交给用户也没人拦得住。
release 模式把三件事钉死：versionName == 2.16、dex 里有 mediaVolumeRatio（手机音量联动
电脑输出靠它）、send_mode（2026-11-04 Q3：送声音档位，电脑端准入靠它）和
originId（2026-11-04 B：音频扩展段防自环）、交付 APK 与 installer payload 哈希一致
（六处副本必须同一颗）。
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys
import zipfile

# 这台机器（以及很多中文 Windows）控制台是 GBK：报告里用了 ✓/✗ 这类字符，
# 不显式转 UTF-8 的话 print 会直接抛 UnicodeEncodeError，脚本 exit 1 ——
# 而验收条件写的是"跑 check_apk.py → RESULT: PASS"，那样根本跑不到结论。
# 这是个真实缺陷（由 android-ui 独立验收时发现），所以在这里堵死。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - Python < 3.7 或非常规 stdout
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "android/app/build/outputs/apk/debug/app-debug.apk"
RELEASE_DEFAULT = ROOT / "release/GOGO/AudioMixing-手机版-v2.31.7.apk"
REQUIRED_VERSION = "2.31.7"
# 2.10 的手机音量联动符号 + 2.11 的送声音档位字段（电脑端准入判据靠它）
# + 2.12 的防自环扩展段字段（音频帧 originId/hop）。
# 2.14 新增：bt_name / bt_mac（手机自报蓝牙身份，电脑端合并 WiFi/蓝牙链路的证据）。
# 2.16 新增：vol_in_capture（电脑端不再二次衰减）+ mediaVolumeRatioFor
#   （手机在发送前把系统媒体音量乘进 PCM —— 采集点在系统音量之前，不乘就"调不动"）。
# 2.17 新增：diagJson（把 vol_q14/gain_q14/total_q14/raw_peak/post_peak 随统计包上报，
#   真机上用来证明"音量乘法到底有没有生效"）。
# 2.18 新增：setSendState（把 send_mode/broadcast 写进信标）。
# 2.19 无新增符号（与本轮电脑端 2.31 配套，只升版本号；全部判据沿用 2.18）。
# 2.20 新增：silent_vol_policy / vol_policy（音量乘数绑到送声档位：外放档乘系统音量、
#   静音档默认只保留 App 内部音量；电脑端据此恒不叠乘，两端合计只乘一次）。
# 2.21 无新增符号（只把静音档策略改成只读，符号与 2.20 相同）。
# 2.22 同样无新增符号（电脑端 UI 重做，手机端仅顺延版本号）。
# 2.23 同样无新增符号（电脑端简洁档恢复音量/信号条，手机端仅顺延版本号）。
# 2.24 无新增符号（电脑端顶部自启动开关 + 启动文件夹方式，手机端仅顺延版本号）。
# 2.25 同样无新增符号（电脑端 serve 模式补托盘与启动日志，手机端仅顺延版本号）。
# 2.27 无新增符号（电脑端删配置卡/工具条挪位/蓝牙两键，手机端仅顺延版本号；判据沿用 2.26）。
# 2.28 新增：channelMode（广播专用接收器的独立准入；与点对点名单分开）。
# 2.29 新增：quality / quality_preset / setQuality（三档性能预算随信标公布并协商）
#            + setReservedPort（点对点/广播换口互不抢占）+ LegacyChannelJudge（旧版广播兼容）。
REQUIRED_SYMBOLS = ["mediaVolumeRatio", "mediaVolumeRatioFor", "send_mode", "originId",
                    "bt_name", "vol_in_capture", "diagJson", "total_q14", "setSendState",
                    "beaconSendMode", "silent_vol_policy", "vol_policy", "channelMode",
                    "quality", "setReservedPort", "LegacyChannelJudge", "concentus",
                    "HostGate", "storeChunks", "crossReceiverDup",
                    "localLinkTypes", "setLinkPreference",
                    # 2.31.5：9 键可缩放控制卡 + 设置对话框 + USB 网络共享自检
                    "control_collapsed", "showSettingsDialog", "usbTetherIp",
                    # 2.31.5（issue #1）：手动电脑 IP + 发现自检 + 附近的设备权限
                    "manual_peer_hosts", "discoverySummary", "setManualHosts",
                    "NEARBY_WIFI_DEVICES",
                    # 2.31.6（代码审查）：自检用尝试轮数
                    "beaconRounds", "discoveryAttempts",
                    # 2.31.7：按真实前缀发定向广播 + 手动 IP 覆盖全部发现端口
                    "localBroadcasts", "getInterfaceAddresses"]
PAYLOAD = ROOT / "tools/installer_payload/AudioMixing-手机版-v2.31.7.apk"
# 交付副本全部改为**带版本号**的文件名（用户实机装错过版本，第 6 条）。
# ★ v2.33（Q2 交付）：把桌面、面板 ui 目录、构建输出也纳入逐份哈希比对 ——
#   用户明确要求 APK 至少覆盖这六处，缺一处就可能在"扫码装 App"时发错包。
DESKTOP_APK = pathlib.Path.home() / "Desktop" / "AudioMixing-手机版-v2.31.7.apk"
DELIVERY_COPIES = [ROOT / "release/GOGO/AudioMixing-手机版-v2.31.7.apk",
                   ROOT / "release/AudioMixing-手机版-v2.31.7.apk",
                   PAYLOAD,
                   ROOT / "audiomixing/ui/AudioMixing-手机版-v2.31.7.apk",
                   DESKTOP_APK,
                   DEFAULT]

# 每个交付的类都要在这里 —— 漏掉一个，那个类整个丢了脚本也照样报 PASS
# （Diagnosis.java 是 task-6 的主要交付物，之前就漏在清单外）。
CLASSES = ["Proto", "Discovery", "AudioSend", "AudioRecv", "Engine", "MixService",
           "MainActivity", "Diagnosis", "OpusPcmDecoder", "HostGate"]
WORDS = ["麦克风", "收听", "送声音", "详情", "设备", "正在", "局域网", "配对", "同一", "音量"]


def _manifest_has_version(apk: pathlib.Path, version: str) -> bool:
    """APK 清单里的 versionName 是不是指定值。

    APK 里的 AndroidManifest.xml 是二进制 AXML，字符串池通常按 UTF-16LE 存；
    为了兼容 UTF-8 池，两种编码都找。这样不依赖 aapt2（它在别人的机器上不一定有）。
    """
    try:
        raw = zipfile.ZipFile(apk).read("AndroidManifest.xml")
    except (KeyError, zipfile.BadZipFile, OSError):
        return False
    return (version.encode("utf-16-le") in raw) or (version.encode("utf-8") in raw)


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    args = [a for a in sys.argv[1:]]
    release = "--release" in args
    paths = [a for a in args if not a.startswith("--")]
    apk = pathlib.Path(paths[0]) if paths else (RELEASE_DEFAULT if release else DEFAULT)
    if not apk.is_file():
        print("找不到 APK: %s" % apk)
        return 2
    print("检查 %s（%.0f KB）%s" % (apk.name, apk.stat().st_size / 1024,
                                  "  [release 模式]" if release else ""))

    z = zipfile.ZipFile(apk)
    dex_files = sorted(n for n in z.namelist() if n.endswith(".dex"))
    print("\n== dex 文件 ==")
    for n in dex_files:
        print("   %-16s %8d 字节" % (n, z.getinfo(n).file_size))

    # 所有 dex 拼起来找 —— 只查 classes.dex 是查不到东西的（真代码可能在 classes2.dex）
    blob = b"".join(z.read(n) for n in dex_files)
    text = blob.decode("utf-8", "replace")

    print("\n== 每个交付的类都在吗（清单见 CLASSES）==")
    missing = 0
    for cls in CLASSES:
        ok = ("Lcom/audiomixing/app/%s;" % cls) in text
        if not ok:
            missing += 1
        print("   %-14s %s" % (cls, "✓" if ok else "✗ 缺失"))

    print("\n== 中文有没有被 encoding 坑弄乱 ==")
    cn = re.findall(r"[\u4e00-\u9fff][\u4e00-\u9fff，。·、！？0-9A-Za-z]{1,40}", text)
    print("   dex 里的中文串共 %d 个" % len(cn))
    bad = 0
    for w in WORDS:
        ok = w in text
        if not ok:
            bad += 1
        print("   %-10s %s" % (w, "✓" if ok else "✗ 找不到"))
    garbled = bool(re.search(r"\?{3,}", text)) or bool(re.search(r"[\u00c0-\u00ff]{4,}", text))
    print("   乱码特征（连续问号 / 连续拉丁扩展字符）:", "有 ✗" if garbled else "没有 ✓")
    print("   举例:", "、".join(dict.fromkeys(cn))[:180] or "（一个中文串都没有）")

    release_ok = True
    if release:
        print("\n== 交付门槛（release 模式）==")
        version_ok = _manifest_has_version(apk, REQUIRED_VERSION)
        print("   versionName == %s：%s" % (REQUIRED_VERSION, "✓" if version_ok else "✗ 不是交付版本"))
        release_ok = release_ok and version_ok
        symbols_ok = True
        for sym in REQUIRED_SYMBOLS:
            ok = sym in text
            print("   dex 含 %s：%s" % (sym, "✓" if ok else "✗ 缺失"))
            symbols_ok = symbols_ok and ok
        release_ok = release_ok and symbols_ok
        if not PAYLOAD.is_file():
            print("   installer payload：✗ 找不到 %s" % PAYLOAD)
            release_ok = False
        else:
            want = _sha256(PAYLOAD)
            print("   基准（installer payload）sha256 = %s" % want)
            for copy in [apk, *DELIVERY_COPIES]:
                if copy.resolve() == apk.resolve():
                    continue  # 被检查的就是这一份，不用再和自己比
                if not copy.is_file():
                    print("   %s：✗ 不存在" % copy)
                    release_ok = False
                    continue
                got = _sha256(copy)
                same = got == want
                print("   %s：%s %s" % (copy, "✓ 一致" if same else "✗ 不一致", got))
                release_ok = release_ok and same

    print("\n== 结论 ==")
    ok = missing == 0 and bad == 0 and not garbled and release_ok
    print("   类: %s   中文: %s   乱码: %s%s" % (
        "全在 ✓" if missing == 0 else "缺 %d 个 ✗" % missing,
        "正常 ✓" if bad == 0 else "有 %d 个找不到 ✗" % bad,
        "无 ✓" if not garbled else "有 ✗",
        "" if not release else "   交付门槛: %s" % ("全过 ✓" if release_ok else "有未过项 ✗")))
    print("   RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
