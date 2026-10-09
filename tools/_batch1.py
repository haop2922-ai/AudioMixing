"""第一批：1 关键路径记异常、2 删死代码、4 界面提示广播规则、5 网络位置误报。"""

import ast
import io
import re

# ---------- 2) 删掉没人调用的 _pick_live_capture ----------
E = r"E:\AI\deep blue\wavelink\wavelink\engine.py"
src = io.open(E, encoding="utf-8").read()
start = src.find("    def _pick_live_capture(self)")
if start > 0:
    end = src.find("\n    def ", start + 10)
    if end > start:
        src = src[:start] + src[end + 1:]
        print("2) 已删除死代码 _pick_live_capture（%d 字符）" % (end - start))
else:
    print("2) 已经不存在")

# ---------- 1) 调和循环：出错必须留痕，不能静默 ----------
old_loop = """    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():"""
new_loop = """    def _maintenance_loop(self) -> None:
        # 每一轮都单独包起来。以前这里任何一处抛异常，整条维护线程就静悄悄地死了，
        # 表现是"采集不刷新、发送端不清理、日志也不再更新"，却完全看不到原因。
        while not self._stop.is_set():
            try:
                self._maintenance_once()
            except Exception:
                self._note_once("维护循环出错（已跳过这一轮）：%s"
                                % traceback.format_exc().strip().splitlines()[-1])

    def _maintenance_once(self) -> None:
        while not self._stop.is_set():"""
if old_loop in src and "_maintenance_once" not in src:
    src = src.replace(old_loop, new_loop, 1)
    print("1) 维护循环已加异常留痕")
else:
    print("1) 维护循环锚点未匹配或已改")

if "import traceback" not in src:
    lines = src.split("\n")
    idx = next(i for i, l in enumerate(lines) if l.startswith("import ") or l.startswith("from "))
    lines[idx:idx] = ["import traceback"]
    src = "\n".join(lines)
    print("1) 已加 import traceback")

io.open(E, "w", encoding="utf-8", newline="\n").write(src)
ast.parse(io.open(E, encoding="utf-8").read())
print("engine 语法 OK")
