"""APK 验收检查：清单节点、类是否都在、中文有没有被编码坑弄乱。

不依赖任何第三方库，也不启动任何 exe —— 直接读 zip 和 dex。
（aapt2 的清单 dump 由调用方另跑，这里补它看不到的东西：dex 里的类与中文字符串。）

    python tools/check_apk.py [apk路径]
"""

from __future__ import annotations

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

DEFAULT = pathlib.Path(__file__).resolve().parent.parent / "android/app/build/outputs/apk/debug/app-debug.apk"

# 每个交付的类都要在这里 —— 漏掉一个，那个类整个丢了脚本也照样报 PASS
# （Diagnosis.java 是 task-6 的主要交付物，之前就漏在清单外）。
CLASSES = ["Proto", "Discovery", "AudioSend", "AudioRecv", "Engine", "MixService",
           "MainActivity", "Diagnosis"]
WORDS = ["麦克风", "收听", "送声音", "详情", "设备", "正在", "局域网", "配对", "同一", "音量"]


def main() -> int:
    apk = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not apk.is_file():
        print("找不到 APK: %s" % apk)
        return 2
    print("检查 %s（%.0f KB）" % (apk.name, apk.stat().st_size / 1024))

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

    print("\n== 结论 ==")
    ok = missing == 0 and bad == 0 and not garbled
    print("   类: %s   中文: %s   乱码: %s" % (
        "全在 ✓" if missing == 0 else "缺 %d 个 ✗" % missing,
        "正常 ✓" if bad == 0 else "有 %d 个找不到 ✗" % bad,
        "无 ✓" if not garbled else "有 ✗"))
    print("   RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
