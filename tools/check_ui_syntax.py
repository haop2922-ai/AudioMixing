"""Extract the page's JavaScript and hand it to Node for a real syntax check.

Counting brackets finds nothing useful here because strings and template literals
legitimately contain unbalanced-looking text. Node's own parser is the authority.

    python tools/check_ui_syntax.py
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "audiomixing" / "ui" / "index.html"


def find_node() -> str | None:
    return shutil.which("node")


def main() -> int:
    if not HTML.is_file():
        print("找不到 %s" % HTML)
        return 1
    html = HTML.read_text(encoding="utf-8")

    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not scripts:
        print("页面里没有 <script> 段")
        return 1
    js = "\n;\n".join(scripts)
    print("页面: %s (%d 字节, %d 个 script 段, JS %d 字节)" % (HTML.name, len(html), len(scripts), len(js)))

    problems: list[str] = []

    # 1. self-contained check (must work on an isolated LAN)
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    if external:
        problems.append("引用了外部资源（局域网内打不开）: %s" % external)
    print("[%s] 无外部资源引用" % ("PASS" if not external else "FAIL"))

    # 2. every element id referenced by $('...') must exist in the markup
    ids_used = set(re.findall(r"\$\('([A-Za-z0-9_\-]+)'\)", js))
    ids_defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
    missing = sorted(i for i in ids_used if i not in ids_defined)
    # ids created dynamically by JS assignments are fine; report only obvious misses
    if missing:
        problems.append("JS 引用了不存在的元素 id: %s" % missing)
    print("[%s] 元素 id 引用完整%s" % ("PASS" if not missing else "FAIL", "" if not missing else " 缺失 " + ",".join(missing)))

    # 3. every API path the page calls must exist in webui.py
    py = (ROOT / "audiomixing" / "webui.py").read_text(encoding="utf-8")
    server_paths = set(re.findall(r'path == "(/api/[a-z\-/]+)"', py))
    ui_paths = set(re.findall(r"api\('(/api/[a-z\-/]+)'", js)) | set(re.findall(r"fetch\('(/api/[a-z\-/]+)'", js))
    unknown = sorted(p for p in ui_paths if p not in server_paths)
    if unknown:
        problems.append("页面调用了服务端不存在的接口: %s" % unknown)
    print("[%s] 页面调用的 %d 个接口服务端都有" % ("PASS" if not unknown else "FAIL", len(ui_paths)))

    # 4. real JS parse by Node
    node = find_node()
    if not node:
        print("[SKIP] 没找到 node，跳过真正的语法解析")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "page.js"
            path.write_text(js, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            print("[PASS] Node 语法解析通过 —— 页面脚本没有语法错误")
        else:
            err = (proc.stderr or "").strip()
            problems.append("Node 语法解析失败:\n%s" % err[:900])
            print("[FAIL] Node 语法解析失败")
            print(err[:900])

    print()
    if problems:
        for p in problems:
            print("问题: %s" % p)
    print("RESULT:", "PASS" if not problems else "FAIL")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
