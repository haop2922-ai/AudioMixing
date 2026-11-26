"""独立调用入口：源码/开发时用；打包后的主程序用 ``AudioMixing.exe --mcp``。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audiomixing.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))