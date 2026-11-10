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
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import zipfile


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

def nuitka_args(onedir: bool) -> list[str]:
    """Nuitka 参数。onedir=True 出免解包文件夹（推荐分发），False 出单文件。

    ★ 2026-10-21：用户另一台电脑（中文账户）双击单文件版完全没反应，而本机用
      中文 TEMP / 中文 APPDATA / 中文 exe 路径复现时都能启动。单文件模式每次运行都要
      往 %TEMP% 里解压自己，这是最容易被安全软件和超长中文路径卡住的一步；
      改成 onedir（--standalone）后启动时不再解压，是这次分发的默认形态。
    """
    return [
        "-m", "nuitka",
        # ★ 2026-10-21：显式用 zig + baseline（配合 build_env() 的 -mcpu=baseline），
        #   避免在支持 AVX-512 的构建机上生成只有 AVX-512 CPU 能跑的产物。
        "--zig",
        "--target-arch=x86_64",
        # ★ 2026-10-22：Nuitka 的全局依赖缓存（%LOCALAPPDATA%\Nuitka\...\library_dependencies）
        #   被系统/安全软件拒绝写入（WinError 5，depends_legacy 建目录都失败）。
        #   这个缓存只是加速，关掉不影响产物；zig 的缓存仍由 build_env() 的独立目录管理。
        "--disable-cache=all",
        # ★ 2026-10-26：默认 16 路 zig 并发在这台机器上会卡在 C 编译阶段
        #   （16 组 zig 进程全部 0 CPU、四十分钟不动）。限制到 4 路后能稳定完成。
        #   构建慢一点可以接受，构建卡死一小时不能接受。
        "--jobs=4",
        "--standalone" if onedir else "--onefile",
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
    """给本次构建一套全新的 zig 编译器缓存目录，并强制 baseline CPU。

    ★ 2026-10-21 真机事故（0xc000001d 非法指令）：
      构建机是 Ryzen 9800X3D（支持 AVX-512），对方是 Intel Core Ultra 5 125H（不支持）。
      Nuitka 用 zig 编译时，**链接阶段**从 compiler-rt 选了一个 AVX-512 的 memcpy
      （反汇编是 `vmovups zmm0, [rdx]`），程序在进入 Python 之前就崩，所以双击“完全没反应”、
      连 launcher.log 都没有。v2.1 单文件版和 v2.2 免解包版都崩在同一条指令上。

      关键：只加 CFLAGS=-mcpu=baseline 不够（compiler-rt 在链接时选），
      必须同时给 **LDFLAGS=-mcpu=baseline**。实测：
        · 不加 LDFLAGS：.text 里 95 条 zmm（AVX-512）指令；
        · 加上 LDFLAGS：0 条。
      这组参数让产物跑在任何 x86-64 CPU 上，而不是只在编译机上能跑。
    """
    env = dict(os.environ)
    cache = tempfile.mkdtemp(prefix="nuitka-zig-cache-")
    env["ZIG_GLOBAL_CACHE_DIR"] = os.path.join(cache, "global")
    env["ZIG_LOCAL_CACHE_DIR"] = os.path.join(cache, "local")
    # ★ 2026-10-22：Nuitka 默认把模块/依赖缓存放在 %LOCALAPPDATA%\Nuitka\Nuitka\Cache，
    #   这台机器上被沙箱/安全软件拒绝写入（建 depends_legacy 目录都 WinError 5）。
    #   Nuitka 支持 NUITKA_CACHE_DIR，把整套缓存也挪到本次构建的临时目录里，
    #   既不和系统权限打架，也不会把上一次的旧缓存喂回来。
    env["NUITKA_CACHE_DIR"] = os.path.join(cache, "nuitka")
    env["CFLAGS"] = "-mcpu=baseline"
    env["CXXFLAGS"] = "-mcpu=baseline"
    env["LDFLAGS"] = "-mcpu=baseline"
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
    # ★ 2026-11-04（第 6 条）：编进包里的名字也带手机端版本号，
    #   面板下载时 `Content-Disposition` 与文件本体一致，用户能一眼看出装的是哪版。
    src_text = (ROOT / "audiomixing" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__phone_version__\s*=\s*"([^"]+)"', src_text)
    ver = match.group(1) if match else "0"
    target = "audiomixing/ui/AudioMixing-手机版-v%s.apk" % ver
    for cand in (ROOT / "release" / "GOGO" / ("AudioMixing-手机版-v%s.apk" % ver),
                 ROOT / "release" / ("AudioMixing-手机版-v%s.apk" % ver),
                 ROOT / "release" / "GOGO" / "AudioMixing-手机版.apk",
                 ROOT / "release" / "AudioMixing-mic.apk",
                 ROOT / "tools" / "installer_payload" / ("AudioMixing-手机版-v%s.apk" % ver),
                 ROOT / "tools" / "installer_payload" / "AudioMixing-mic.apk"):
        if cand.is_file():
            return ["--include-data-files=%s=%s" % (cand, target)]
    print("[build] 提示：没找到手机端 APK，本次不编进包里（安装页只能从 exe 旁边找）")
    return []


def _version_text() -> str:
    """面板上显示的版本号（例如 2.2），用来给 zip 命名。"""
    src = (ROOT / "audiomixing" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    return match.group(1) if match else "0"


def _find_built_exe(onedir: bool) -> pathlib.Path | None:
    """Nuitka 的产物位置：单文件在 OUT/NAME，免解包在 OUT/<name>.dist/NAME。"""
    if not onedir:
        direct = OUT / NAME
        return direct if direct.is_file() else None
    candidates = [p for p in OUT.rglob(NAME) if p.parent != OUT]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _zip_onedir(exe: pathlib.Path) -> pathlib.Path:
    """把 <name>.dist 整个文件夹压成 zip，顶层目录固定叫 AudioMixing。

    这样对方解压出来就是一个完整的 AudioMixing 文件夹，双击里面的 AudioMixing.exe
    即可，不需要每次启动再往 %TEMP% 解压自己。
    """
    root = exe.parent
    out = ROOT / "release" / ("AudioMixing-v%s-免解包版.zip" % _version_text())
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            zf.write(path, arcname=str(pathlib.Path("AudioMixing") / path.relative_to(root)))
            total += path.stat().st_size
        # ★ 2026-10-21：把一键诊断也放进去，和 exe 同级。对方解压后直接双击
        #   AudioMixing-诊断.bat 就能把启动环境/拦截情况带回来，不用再教他找路径。
        diag = ROOT / "release" / "诊断工具"
        for name in ("AudioMixing-诊断.bat", "diagnose.ps1"):
            path = diag / name
            if path.is_file():
                zf.write(path, arcname=str(pathlib.Path("AudioMixing") / name))
                total += path.stat().st_size
    print("[build] 已打包: %s  %.1f MB（解压后 %.1f MB）"
          % (out, out.stat().st_size / 1024 / 1024, total / 1024 / 1024))
    return out


def _pe_text_bytes(exe: pathlib.Path) -> bytes:
    """取 PE 的 .text 段（单文件取 stub，免解包取主程序）。"""
    data = exe.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    num_sec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    sec_off = pe + 24 + opt_size
    for i in range(num_sec):
        base = sec_off + i * 40
        name = data[base:base + 8].rstrip(b"\0")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
        if name == b".text":
            return data[rawptr:rawptr + rawsize]
    return b""


def check_portable_isa(exe: pathlib.Path) -> bool:
    """发布前的 CPU 兼容门禁：产物里不能有 AVX-512（zmm）指令。

    ★ 2026-10-21 真机事故：构建机 Ryzen 9800X3D 有 AVX-512，zig 在链接时选了
      AVX-512 的 memcpy；对方 Intel Ultra 5 125H 没有 AVX-512，启动第一条
      `vmovups zmm0` 就 0xc000001d（非法指令），双击完全没反应。
      这扇门就是防止再交付一颗“只有编译机能跑”的 exe。
    """
    text = _pe_text_bytes(exe)
    if not text:
        print("[build] 可移植性检查: 没找到 .text 段，跳过")
        return True
    # 已知的 AVX-512 memcpy 特征（不依赖 capstone 也能抓到这次事故）
    if b"\x62\xf1\x7c\x48" in text:
        print("[build] 可移植性检查: 发现 AVX-512 memcpy 特征 => FAIL")
        return False
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    except Exception:
        print("[build] 可移植性检查: 未安装 capstone，只做了特征字节检查 => PASS（建议装 capstone 做全量扫描）")
        return True
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    zmm = 0
    pos = text.find(b"\x62")
    while pos >= 0:
        for ins in md.disasm(text[pos:pos + 16], pos):
            if "zmm" in (ins.mnemonic + " " + ins.op_str):
                zmm += 1
            break
        pos = text.find(b"\x62", pos + 1)
    print("[build] 可移植性检查: AVX-512(zmm) 指令 %d 条 => %s" % (zmm, "PASS" if zmm == 0 else "FAIL"))
    return zmm == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="编译后启动一次，检查面板是否响应")
    ap.add_argument("--onedir", action="store_true",
                    help="打包成免解包文件夹（--standalone），并 zip 到 release/")
    ap.add_argument("--no-zip", action="store_true", help="onedir 时不生成 zip")
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
    cmd = nuitka_args(args.onedir)
    try:
        rc = subprocess.call([sys.executable, *cmd[:-1], *extra, cmd[-1]],
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

    made = _find_built_exe(args.onedir)
    if made is None:
        print("[build] 没有找到产物 %s（onedir=%r）" % (OUT, args.onedir))
        return 1
    print("[build] 完成: %s  %.1f MB" % (made, made.stat().st_size / 1024 / 1024))
    if not check_portable_isa(made):
        print("[build] 产物包含目标 CPU 不支持的 AVX-512 指令，拒绝交付")
        return 1
    if args.onedir and not args.no_zip:
        _zip_onedir(made)
        print("[build] 分发方式：把 zip 整个发给对方，解压后运行 AudioMixing.exe")
    else:
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
        # ★ 2026-10-21：自检必须覆盖“中文账户”的真实形态 —— 中文 TEMP（单文件解压路径）、
        #   中文配置目录、中文 exe 所在目录。只在本机 ASCII 路径下通过，不能算过。
        cn_root = tempfile.mkdtemp(prefix="AudioMixing-构建自检-中文-")
        cn_temp = os.path.join(cn_root, "临时目录")
        sandbox = os.path.join(cn_root, "配置目录")
        os.makedirs(cn_temp, exist_ok=True)
        env = dict(os.environ)
        env["TEMP"] = cn_temp
        env["TMP"] = cn_temp
        env["AUDIOMIXING_HOME"] = sandbox
        print("[build] 启动并等待面板（自检端口 %d，不碰 17800；中文临时目录 %s）…"
              % (test_port, cn_root))
        proc = subprocess.Popen(
            [str(made), "serve", "--host", "127.0.0.1",
             "--web-port", str(test_port), "--audio-port", str(test_audio)],
            cwd=str(made.parent), env=env,
        )
        build = {}
        cfg_ok = False
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
            # 配置必须真的落到中文目录里（读一次，确认不是空文件/编码坏）
            cfg_file = os.path.join(sandbox, "config.json")
            cfg_ok = os.path.isfile(cfg_file)
            if cfg_ok:
                try:
                    json.loads(pathlib.Path(cfg_file).read_text(encoding="utf-8"))
                except Exception as exc:
                    print("[build] 中文配置读取失败: %s" % exc)
                    cfg_ok = False
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
            shutil.rmtree(cn_root, ignore_errors=True)

        # 这条断言是 2026-10-14 那次事故的门禁：源码和 C 都是新的，
        # 却因为 zig 缓存把旧目标文件链接进来，交付了一颗跑旧逻辑的 exe。
        # 2026-10-21 再加一条：版本号必须等于源码里的 __version__（防止又交付旧产物）。
        channel = build.get("channel")
        build_time = build.get("build_time") or ""
        version = build.get("version") or ""
        ok = channel == "exe" and bool(build_time) and version == _version_text() and cfg_ok
        print("[build] 自检: channel=%r version=%r build_time=%r 中文配置落盘=%r => %s"
              % (channel, version, build_time, cfg_ok, "OK" if ok else "FAIL"))
        if not ok:
            print("[build] 自检失败：产物里的版本/配置不对，别交付。")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
