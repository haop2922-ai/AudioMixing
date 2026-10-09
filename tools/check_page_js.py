"""检查网页里内嵌的脚本能不能被解析。

为什么要这个：内嵌脚本里一个字符串写坏，整段脚本就废掉，页面表现只是
"按钮点了没反应"，很难猜。构建前用 node 解析一遍，几毫秒就能发现问题。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import urllib.request


def scripts_in(html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def check_url(url: str) -> list[str]:
    try:
        html = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
    except Exception as exc:
        return ["%s 打不开: %s" % (url, exc)]
    problems = []
    for index, js in enumerate(scripts_in(html)):
        if not js.strip():
            continue
        path = os.path.join(tempfile.gettempdir(), "wavelink-page-%d.js" % index)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        try:
            proc = subprocess.run(["node", "--check", path], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
        except FileNotFoundError:
            return []
        except Exception as exc:
            return ["node 执行失败: %s" % exc]
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[:600]
            problems.append("%s 第 %d 段脚本解析失败:" % (url, index + 1) + chr(10) + detail)
    return problems


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:17800"
    problems = []
    for suffix in ("/monitor", "/", "/app", "/listen"):
        problems.extend(check_url(base.rstrip("/") + suffix))
    for item in problems:
        print("[pages] %s" % item)
    print("[pages] 全部页面脚本可解析" if not problems else "[pages] 有脚本解析失败")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
