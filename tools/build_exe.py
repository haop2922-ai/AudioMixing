"""Build a single-file AudioMixing.exe that a non-technical user can double-click.

    python tools/build_exe.py

What the resulting exe does when double-clicked (no arguments):

    * starts the node (capture + discovery + control panel)
    * opens http://127.0.0.1:17800 in the default browser
    * keeps its console window open so errors stay visible
    * writes %APPDATA%\\WaveLink\\last-run.txt with the LAN URLs

Build notes:
    * ``--onefile`` keeps it to one file to copy around; start-up takes a few
      seconds because the bundle unpacks to a temp folder first.
    * PyInstaller now ships numpy's hook, so numpy (required by the mixer) is
      picked up automatically; we still add ``--collect-all numpy`` belt and braces.
    * The icon is generated at build time with Pillow when available, so the repo
      does not need to carry a binary blob.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICON = ROOT / "tools" / "wavelink.ico"
NAME = "AudioMixing"


# HWHHYN 用 DEEPSEEK 制作
def make_icon() -> pathlib.Path | None:
    """Draw a simple speaker/wave icon so the exe is recognisable in Explorer."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[build] Pillow 不可用，跳过图标（exe 仍可用）")
        return None

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        s = size / 32.0
        # rounded dark background
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=max(2, int(6 * s)), fill=(14, 17, 23, 255))
        # speaker body + cone
        d.polygon(
            [(6 * s, 12 * s), (11 * s, 12 * s), (16 * s, 7 * s), (16 * s, 25 * s), (11 * s, 20 * s), (6 * s, 20 * s)],
            fill=(63, 185, 80, 255),
        )
        # two sound arcs
        for i, r in enumerate((5.0, 8.5)):
            box = [16 * s - r * s, 16 * s - r * s, 16 * s + r * s, 16 * s + r * s]
            d.arc(box, start=-55, end=55, fill=(88, 166, 255, 255), width=max(1, int(1.6 * s)))
        images.append(img)

    images[0].save(ICON, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])
    print("[build] 图标已生成: %s" % ICON)
    return ICON


def stop_running() -> bool:
    """Stop a previous AudioMixing.exe. Returns True when one was actually running.

    PyInstaller cannot overwrite a running exe (and the onefile bootloader leaves a
    child process behind if it is only terminated, not killed with its tree), so this
    turns a confusing "Access is denied" into an actionable message.

    The caller relaunches afterwards: killing the node without starting it again is
    what left the control panel offline after every rebuild.
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AudioMixing.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    if "AudioMixing.exe" not in out:
        return False
    print("[build] 检测到 AudioMixing.exe 还在运行，先结束它（否则无法覆盖文件）")
    # No /T: the Bluetooth helpers are children of the node, and killing the whole tree
    # took every phone connection down with it - which is why audio "suddenly stopped"
    # after a rebuild. Both halves of a onefile build are called AudioMixing.exe, so the
    # image-name form is enough.
    subprocess.run(["taskkill", "/IM", "AudioMixing.exe", "/F"], capture_output=True, text=True, timeout=20)
    import time as _time

    _time.sleep(1.0)
    return True


def relaunch(exe: pathlib.Path) -> None:
    """Start the freshly built node again, detached, and wait for its panel."""
    import time as _time
    import urllib.request

    print("[build] 重新启动 %s" % exe.name)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(exe), "serve"], cwd=str(ROOT), creationflags=flags, close_fds=True)
    except OSError as exc:
        print("[build] 启动失败: %s" % exc)
        return
    # Poll the panel so the operator knows whether it actually came up. A onefile
    # bundle has to unpack ~26 MB and import numpy before it binds the port, which on
    # a busy machine can take a while - a short timeout here produces a false alarm.
    deadline = _time.time() + 90.0
    while _time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:17800/healthz", timeout=2) as resp:
                if resp.status == 200:
                    print("[build] 控制台已就绪: http://127.0.0.1:17800 （另一台设备用局域网地址访问）")
                    return
        except Exception:
            _time.sleep(1.0)
    print("[build] 面板未在 90 秒内响应，请双击 dist\\AudioMixing.exe，失败原因写在 %%APPDATA%%\\WaveLink\\launcher.log")


def _verify_imports() -> bool:
    """打包前先确认关键模块能导入。

    之前我把 import 插到 from __future__ 之前，整个 engine 模块加载失败，
    打出来的 exe 一启动就报 No module named 'wavelink.engine'。
    在打包前跑一次导入，这类错误立刻暴露。
    """
    import importlib

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for name in ("wavelink.engine", "wavelink.cli", "wavelink.webui", "wavelink.tray",
                 "wavelink.bluetooth"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            print("[build] 模块 %s 导入失败: %s" % (name, exc))
            return False
    return True

def _compile_all() -> bool:
    """打包前编译所有源码。有语法错误就直接停下。

    这一道是必须的：有一次补丁里的转义引号写坏了 webui.py，语法都不成立，
    我还是打了包，用户拿到手的程序直接报错。编译一遍只要一秒。
    """
    import py_compile
    from pathlib import Path

    bad = []
    for path in sorted(Path(ROOT).rglob("*.py")):
        if any(part in (".testappdata", "build", "dist", "release", "__pycache__", "dev")
               for part in path.parts):
            continue
        try:
            py_compile.compile(str(path), cfile=str(path) + ".check", doraise=True)
        except Exception as exc:
            bad.append("%s: %s" % (path.name, exc))
        finally:
            try:
                Path(str(path) + ".check").unlink()
            except OSError:
                pass
    for item in bad:
        print("[build] 语法错误 %s" % item)
    return not bad


def _run_tests() -> bool:
    """Run the unit tests. No exe is produced unless they all pass.

    This gate exists because of a real accident: a scripted edit deleted several
    methods from engine.py, the tests caught it immediately, and the exe was still
    built and handed over - so the delivered program could not start at all.
    Syntax and import checks do not catch "a method is missing"; the tests do.
    """
    print("[build] 先跑单元测试…")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    except Exception as exc:
        print("[build] 测试无法执行: %s" % exc)
        return False
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("[build]   %s" % line)
    if proc.returncode != 0:
        print("[build] 单元测试没有全绿，不产出 exe")
        return False
    print("[build] 单元测试全部通过")
    return True


def main() -> int:
    print("[build] 项目目录: %s" % ROOT)
    if not _compile_all():
        print("[build] 有源码语法错误，先修好再打包")
        return 1
    if not _run_tests():
        return 1
    if not _verify_imports():
        print("[build] 关键模块导入失败，先修代码再打包")
        return 1
    was_running = stop_running()
    icon = make_icon()

    # Clean previous artefacts so a stale exe can never be mistaken for a new one.
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # 窗口程序。双击打开图形界面，不再弹出黑色控制台窗口；带参数时仍走命令行。
           "--noconsole",
        "--name",
        NAME,
        "--paths",
        str(ROOT),
        "--collect-all",
        "numpy",
        # The control panel is a real file so the UI can be edited without touching
        # Python; it must travel inside the bundle.
        "--add-data",
        "%s%s%s" % (ROOT / "wavelink" / "ui", os.pathsep, "wavelink/ui"),
        "--hidden-import", "wavelink.tray",
           "--hidden-import", "wavelink.audio.opus",
           "--hidden-import", "opuslib_next",
           "--collect-all", "pyogg",
           "--hidden-import",
        "wavelink.audio.backends.wasapi",
        "--hidden-import",
        "wavelink.audio.backends.wasapi_com",
        "--hidden-import",
        "wavelink.audio.backends.file",
        "--hidden-import",
        "wavelink.audio.backends.null",
        "--hidden-import",
        "wavelink.net.discovery",
        "--hidden-import",
        "wavelink.net.rtpaudio",
        "--hidden-import",
        "wavelink.netcheck",
        "--hidden-import",
        "wavelink.webui",
    ]
    if icon is not None:
        cmd += ["--icon", str(icon)]
    cmd.append(str(ROOT / "wavelink_launcher.py"))

    print("[build] 开始打包（首次会慢一些）…")
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print("[build] PyInstaller 失败，返回码 %d" % proc.returncode)
        return proc.returncode

    exe = DIST / ("%s.exe" % NAME)
    if not exe.is_file():
        print("[build] 没有找到产物 %s" % exe)
        return 1
    size_mb = exe.stat().st_size / (1024 * 1024)
    print("[build] 完成: %s (%.1f MB)" % (exe, size_mb))
    # Always leave a working node behind. A rebuild used to stop the running one and
    # never start the replacement, so the control panel simply went offline.
    if was_running and "--no-relaunch" not in sys.argv:
        relaunch(exe)
    else:
        print("[build] 提示: 双击 dist\\AudioMixing.exe 即可启动（面板 http://127.0.0.1:17800）")
    print("[build] 验证命令: python tools/verify_exe.py --quick")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
