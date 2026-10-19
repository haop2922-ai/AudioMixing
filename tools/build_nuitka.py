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
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile


def _free_tcp_port() -> int:
    """让操作系统给一个当前空着的 TCP 端口（自检用，绝不碰用户正在跑的 17800）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _free_udp_port() -> int:
    """同上，UDP（音频端口）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "dist_nuitka"
NAME = "AudioMixing.exe"
ENTRY = "audiomixing_launcher.py"
BUILDMETA = ROOT / "audiomixing" / "_buildmeta.py"


def _padded_version() -> str:
    """把 audiomixing/__init__.py 的 __version__ 补成 Nuitka 要的四段数字。

    这里原来是写死的（1.22.0.0）—— 改版本号时极容易只改 __init__.py 而漏掉这里，
    结果 exe 右键属性里的版本和面板上的版本对不上，用户没法靠属性确认拿的是哪一版。
    直接从 __version__ 推就不会再漏。
    """
    src = (ROOT / "audiomixing" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    nums = [int(p) for p in re.findall(r"\d+", match.group(1))][:4] if match else []
    while len(nums) < 4:
        nums.append(0)
    return ".".join(str(n) for n in nums)

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
    # 版本号跟着 audiomixing/__init__.py 的 __version__ 走（v1.8 时写的是 1.1.0.0，
    # 结果文件属性里显示的版本和面板上的版本对不上，用户没法靠右键属性确认自己拿的是哪一版）。
    "--file-version=%s" % _padded_version(),
    "--product-version=%s" % _padded_version(),
    "--copyright=MIT License",
    ENTRY,
]


def buildmeta_args() -> list[str]:
    """把本次构建时间写进 audiomixing/_buildmeta.py，随包编进去。

    为什么：面板的「构建于」原本靠 exe 文件时间算，而 Nuitka onefile 下
    sys.executable 指向解包出来的临时 python.exe，取 mtime 失败 →
    面板显示「构建于 未知」（2026-10-14 实测）。写死一个时间常量就不依赖文件位置；
    exe 复制到桌面后文件时间会变，这个常量也不会跟着变。
    编完即删（.gitignore 已忽略），源码里不留构建痕迹。
    """
    import datetime

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    BUILDMETA.write_text(
        '"""由 tools/build_nuitka.py 生成，不要手改，也不进仓库。"""\n'
        'BUILD_TIME = "%s"\n' % stamp,
        encoding="utf-8",
    )
    return ["--include-module=audiomixing._buildmeta"]


def build_env() -> dict:
    """给本次构建一套全新的 zig 编译器缓存目录。

    2026-10-14 实测踩的坑：zig 的全局缓存会把**旧的目标文件**喂回来，
    结果源码是新的、生成的 C 也是新的，唯独编出来的 exe 里跑的是旧逻辑
    （面板仍显示「构建于 未知」、诊断报告仍写「源码运行」）。
    而 Nuitka 的 `--disable-cache=all` 管不到 zig 自己的缓存 ——
    只有换一个 ZIG_GLOBAL_CACHE_DIR / ZIG_LOCAL_CACHE_DIR 才有效。
    换成每次构建独立目录后，同一个探针立刻恢复正常。
    """
    env = dict(os.environ)
    cache = tempfile.mkdtemp(prefix="nuitka-zig-cache-")
    env["ZIG_GLOBAL_CACHE_DIR"] = os.path.join(cache, "global")
    env["ZIG_LOCAL_CACHE_DIR"] = os.path.join(cache, "local")
    return env


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


def apk_args() -> list[str]:
    """把手机端安装包编进包里 —— 面板的「扫码装 App」页要能自己把 APK 发给手机。

    为什么要编进去：那个安装页是**电脑面板**提供的，用户扫码时手机上还什么都没有。
    如果只靠"exe 旁边放一个 apk"，exe 一旦被单独拷走，这个入口就废了 ——
    2026-10-15 用户现场就是这样：页面报「这台电脑上还没有手机端安装包」。
    （编进去之后 apk_path() 走 audiomixing/ui 那一条就能找到，旁边那份仍然优先。）
    """
    for cand in (ROOT / "release" / "GOGO" / "AudioMixing-手机版.apk",
                 ROOT / "release" / "AudioMixing-mic.apk",
                 ROOT / "tools" / "installer_payload" / "AudioMixing-mic.apk"):
        if cand.is_file():
            return ["--include-data-files=%s=audiomixing/ui/AudioMixing-手机版.apk" % cand]
    print("[build] 提示：没找到手机端 APK，本次不编进包里（安装页只能从 exe 旁边找）")
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
    extra = opus_dll_args() + icon_args() + apk_args()
    if extra:
        print("[build] 已附带 opus.dll、图标，以及手机端安装包（有的话）")
    extra = extra + buildmeta_args()
    print("[build] Nuitka 编译中（首次会下载 MinGW，约 100 MB，10-30 分钟）…")
    # ★ 2026-10-19 用户：「本次测试中发现各个链路的音频质量出现了下降，存在短小杂音」——
    #   时间上正好压在我连着编译的那几分钟里：Nuitka 会把所有核吃满，实时音频线程抢不到
    #   CPU 就是一声"咔"。编译本身不赶时间，所以整棵进程树都降到**低于正常**优先级：
    #   音频优先，编译慢一点无所谓。（Windows 子进程默认继承父进程的优先级类别，
    #   zig/clang 那些子进程也跟着降下来。）
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    try:
        rc = subprocess.call([sys.executable, *ARGS[:-1], *extra, ARGS[-1]],
                             cwd=ROOT, env=build_env(), creationflags=flags)
    finally:
        # 构建时间那个模块只在编译期存在（已编进产物），编完即删
        try:
            BUILDMETA.unlink()
        except OSError:
            pass
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

    # 3. 可选自检：不只看端口活没活，还要断言"编进去的确实是这一版"
    if args.test:
        import json
        import time
        import urllib.request

        # ★ 2026-10-16 修三件事（都是实测撞出来的）：
        #   ① 自检原来写死 17800 —— 用户**正开着面板**时，这里的 healthz 会打到**他那一个
        #      旧实例**上去，于是"面板已响应"是**假通过**（根本没验证到新 exe）；
        #      而且新 exe 一起就被"旧实例"拦住 / 或反过来把用户那个实例挤掉。
        #   ② 现在自检跑在一个**临时空端口**上：既碰不到用户的实例，也不可能对着别人的
        #      面板假通过（端口是我们刚探测确认没人听的）。
        #   ③ **自检绝不能写用户的配置**。改成临时端口之后踩到了这一条：exe 的 serve 会把
        #      web_port/audio_port 存进配置，于是自检那两个随机端口被写进了**用户真实配置**
        #      （%APPDATA%\AudioMixing\config.json）—— 用户下次双击，面板就跑到 57590
        #      这种端口上，手机和另一台电脑都找不到他。审计工具当场抓到。
        #      `AUDIOMIXING_HOME` 指向一次性目录，exe 的一切读写都落在里面，编完就删。
        #      （tools/selftest_twonode.py 早就这么做了，这里照抄它的做法。）
        test_port = _free_tcp_port()
        test_audio = _free_udp_port()
        sandbox = tempfile.mkdtemp(prefix="audiomixing-buildtest-")
        env = dict(os.environ)
        env["AUDIOMIXING_HOME"] = sandbox
        print("[build] 启动并等待面板（自检端口 %d，不碰 17800；配置落在临时目录 %s）…"
              % (test_port, sandbox))
        proc = subprocess.Popen(
            [str(made), "serve", "--host", "127.0.0.1",
             "--web-port", str(test_port), "--audio-port", str(test_audio)],
            cwd=str(OUT), env=env,
        )
        build = {}
        try:
            for _ in range(40):
                time.sleep(2)
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % test_port, timeout=3) as r:
                        if r.status == 200:
                            print("[build] 面板已响应 (ok)  http://127.0.0.1:%d" % test_port)
                            break
                except Exception:
                    continue
            else:
                print("[build] 面板 80 秒内没响应 —— 请手动双击确认")
                return 1
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % test_port, timeout=10) as r:
                    build = (json.loads(r.read().decode("utf-8")) or {}).get("build") or {}
            except Exception as exc:
                print("[build] 读 /api/status 失败: %s" % exc)
        finally:
            # onefile 的 exe 是个 stub，真正在跑面板的是它拉起来的子进程 ——
            # 只 terminate() 那个 stub 会留下一个**仍然占着自检端口的孤儿**，
            # 于是下一次自检会被"旧实例"拦住（2026-10-15 实测）。
            # 必须连整棵树一起收掉。
            try:
                subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                proc.terminate()
            # ★ 2026-10-18 补一道兜底（实测撞到两次）：onefile 的 stub 有时已经自己退出，
            #   这时 `taskkill /T` 再也找不到那棵"树"，它拉起来的**孤儿**子进程会活下来，
            #   一直占着构建目录里的 opus.dll —— 表现就是**下一次编译**直接
            #   `PermissionError: dist_nuitka\audiomixing_launcher.dist\opus.dll` 失败，
            #   而报错完全指不到"上一次自检留下的孤儿"这个真凶。
            #   所以按"可执行文件就在构建目录里"再扫一遍清干净；
            #   用户在桌面上跑的那颗 exe 不在这个目录里，不会被误伤。
            try:
                if os.name == "nt":
                    pat = str(OUT).replace("'", "''")
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         ("Get-Process -ErrorAction SilentlyContinue | "
                          "Where-Object { $_.Path -like '%s*' } | ForEach-Object { $_.Id }" % pat)],
                        capture_output=True, text=True, timeout=20).stdout or ""
                    for pid in [x.strip() for x in out.splitlines() if x.strip().isdigit()]:
                        subprocess.call(["taskkill", "/F", "/PID", pid],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            # 临时配置目录只服务这一次自检，编完就删（源码里不留构建痕迹）。
            import shutil

            shutil.rmtree(sandbox, ignore_errors=True)

        # 这条断言是 2026-10-14 那次事故的门禁：源码和 C 都是新的，
        # 却因为 zig 缓存把旧目标文件链接进来，交付了一颗跑旧逻辑的 exe。
        channel = build.get("channel")
        build_time = build.get("build_time") or ""
        ok = channel == "exe" and bool(build_time)
        print("[build] 自检: channel=%r build_time=%r => %s"
              % (channel, build_time, "OK" if ok else "FAIL"))
        if not ok:
            print("[build] 自检失败：产物里的版本信息不对，八成又是缓存/旧源码的问题，别交付。")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
