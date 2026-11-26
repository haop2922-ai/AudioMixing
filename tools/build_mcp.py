"""编译独立的 AudioMixing-MCP.exe（控制台版，给 AI 客户端用 stdio 启动）。

和主程序分开编译的原因：主 exe 是 windows-console-mode=disable，
没有可靠的 stdin/stdout；MCP 必须是一个控制台进程。

用法：
    python tools/build_mcp.py
产物：
    dist_nuitka/AudioMixing-MCP.exe
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_nuitka  # noqa: E402

ROOT = build_nuitka.ROOT
NAME = "AudioMixing-MCP.exe"


def main() -> int:
    args = [
        "-m", "nuitka",
        "--onefile",
        "--windows-console-mode=force",
        "--assume-yes-for-downloads",
        "--zig",
        "--target-arch=x86_64",
        "--disable-cache=all",
        "--jobs=4",
        "--output-dir=dist_nuitka",
        "--output-filename=" + NAME,
        "tools/audiomixing_mcp.py",
    ]
    print("[mcp] Nuitka 编译中…", flush=True)
    subprocess.run([sys.executable, *args], cwd=str(ROOT),
                   env=build_nuitka.build_env(), check=True)
    out = ROOT / "dist_nuitka" / NAME
    if not out.is_file():
        print("[mcp] 没找到产物: %s" % out)
        return 1
    print("[mcp] 完成: %s (%d 字节)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
