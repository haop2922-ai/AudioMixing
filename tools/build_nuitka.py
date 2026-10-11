"""用 Nuitka 打包出单文件 AudioMixing.exe。

为什么不用 PyInstaller（2026-10-11 实测结论，详见 docs/排查实录-改名事故.md 第 3c 节）：
    PyInstaller 的单文件模式启动时会创建临时目录并把自己解压进去，
    这个动作会命中部分安全软件（火绒 HIPS / 游戏加加注入）的规则，
    典型报错：  Could not create temporary directory!
    换参数、换目录、清网络标记、改配置目录 —— 全都无效。
    Nuitka 的启动 stub 与 PyInstaller 的 bootloader 完全不同，
    换过去一次通过，产物体积还更小（20.3 MB vs 34 MB）。

前置条件：
    * 必须用 python.org 的官方 Python
      微软商店版 Python 不提供链接用库文件，Nuitka 会报：
          error: unable to find dynamic system library 'python313'
    * pip install nuitka numpy pyogg opuslib_next qrcode pillow
      （首次编译时 Nuitka 会自动下载 MinGW 编译器，约 100 MB）

用法：
    python tools/build_nuitka.py            # 编译
    python tools/build_nuitka.py --test     # 编译后跑一次自我检查

产物：
    dist_nuitka/AudioMixing.exe            单文件，可直接复制给别人
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "dist_nuitka"
NAME = "AudioMixing.exe"
ENTRY = "audiomixing_launcher.py"

ARGS = [
    "-m", "nuitka",
    "--onefile",
    "--assume-yes-for-downloads",          # 首次自动下载 MinGW 编译器
    "--enable-plugin=numpy",
    "--windows-console-mode=disable",      # 无控制台窗口（等同 PyInstaller --windowed）
    "--include-package=audiomixing",
    "--include-package-data=pyogg",        # opus.dll 随包走
    "--include-package-data=opuslib_next",
    "--include-package-data=qrcode",
    "--include-data-dir=audiomixing/ui=audiomixing/ui",
    "--output-dir=dist_nuitka",
    "--output-filename=" + NAME,
    # 完整的版本信息 + 图标。不只是好看：
    # 未签名、又完全没有版本信息的单文件 exe，最容易被杀软的机器学习启发式
    # 判成 Trojan:Win32/Bearfoos.B!ml 之类的误报（2026-10-11 实测遇到）。
    # 正规软件都会带这些字段，补上能明显降低误报率。
    # 注意：Nuitka 要求这几个版本字段【成套出现】，只给其中一个会直接报错。
    "--company-name=Audio Mixing Project",
    "--product-name=Audio Mixing",
    "--file-description=Audio Mixing - LAN audio casting",
    "--file-version=1.1.0.0",
    "--product-version=1.1.0.0",
    "--copyright=MIT License",
    ENTRY,
]


def icon_args() -> list[str]:
    """带上图标（有则用，没有就跳过）。"""
    ico = ROOT / "tools" / "audiomixing.ico"
    if ico.is_file():
        return ["--windows-icon-from-ico=%s" % ico]
    print("[build] 提示：没找到 tools/audiomixing.ico，本次不带图标")
    return []


def opus_dll_args() -> list[str]:
    """把 opus.dll 明确放进包里 + 放到 exe 旁边（双保险）。

    实测（2026-10-11）：Nuitka 打包版界面报
        RuntimeError: Opus 不可用: Could not find Opus library.
    原因：打包后模块的 __file__ 指向源码路径，而且 Nuitka 不设
    sys.frozen / sys._MEIPASS，原来那几个搜索分支全部落空。
    这里除了带上包数据，还把 dll 复制到 exe 同级（onefile 解压根目录），
    配合 audiomixing/audio/opus.py 的多路径查找一定能命中。
    """
    try:
        import pyogg

        dll = pathlib.Path(pyogg.__file__).resolve().parent / "opus.dll"
        if dll.is_file():
            return ["--include-data-files=%s=opus.dll" % dll]
        print("[build] 警告：没找到 pyogg/opus.dll，打包后可能没有 Opus 压缩")
    except Exception as exc:
        print("[build] 警告：定位 pyogg 失败（%s），继续打包" % exc)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="编译后启动一次，检查端口 17800")
    args = ap.parse_args()

    # 1. 官方 Python 检查：商店版没有链接库，直接给出可读的错误
    exe = pathlib.Path(sys.executable)
    if "WindowsApps" in str(exe):
        print("[build] 当前是微软商店版 Python，Nuitka 无法编译。")
        print("[build] 请装 python.org 官方版后重试：")
        print("[build]   https://www.python.org/downloads/windows/")
        print("[build]   （安装时勾 InstallAllUsers=0，商店版可以共存）")
        return 2

    # 2. 编译
    extra = opus_dll_args() + icon_args()
    if extra:
        print("[build] 已附带 opus.dll 和图标")
    print("[build] Nuitka 编译中（首次会下载 MinGW，约 100 MB，10-30 分钟）…")
    rc = subprocess.call([sys.executable, *ARGS[:-1], *extra, ARGS[-1]], cwd=ROOT)
    if rc != 0:
        print("[build] 失败，退出码 %d" % rc)
        return rc

    made = OUT / NAME
    if not made.is_file():
        print("[build] 没有产出 %s" % made)
        return 1
    print("[build] 完成: %s  %.1f MB" % (made, made.stat().st_size / 1024 / 1024))
    print("[build] 提示: 这个文件可以单独复制给别人（第一次运行需在 SmartScreen 点「仍要运行」）")
    print("[build] 提示: 不要放在反复编译的目录里运行 —— 那个目录可能已被安全软件按路径拦截")

    # 3. 可选自检
    if args.test:
        import json
        import time
        import urllib.request

        print("[build] 启动并等待面板…")
        proc = subprocess.Popen([str(made)], cwd=str(OUT))
        try:
            for _ in range(40):
                time.sleep(2)
                try:
                    with urllib.request.urlopen("http://127.0.0.1:17800/healthz", timeout=3) as r:
                        if r.status == 200:
                            print("[build] 面板已响应 (ok)  http://127.0.0.1:17800")
                            break
                except Exception:
                    continue
            else:
                print("[build] 面板 80 秒内没响应 —— 请手动双击确认")
        finally:
            proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
