"""把要删的东西先搬进回收目录，而不是直接删。

为什么需要它：这个项目今天因为批量改代码误删过好几个方法（_best_loopback_selector、
_pick_live_capture 等），当时没有任何备份，只能靠记忆重写，花了很久还出过错。
规则很简单：**不删，只搬**。要用的时候一条命令就能拿回来。

用法
    python tools/trash.py put <文件或目录> [原因]     把它搬进回收目录
    python tools/trash.py list                        看回收目录里有什么
    python tools/trash.py restore <名字> [目标路径]    取回来
    python tools/trash.py snapshot <文件> [说明]      只在回收目录留一份副本（原文件不动）
"""

from __future__ import annotations

import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRASH = os.path.join(ROOT, "回收站")
INDEX = os.path.join(TRASH, "索引.txt")


# HWHHYN 用 DEEPSEEK 制作
def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _note(line: str) -> None:
    os.makedirs(TRASH, exist_ok=True)
    with open(INDEX, "a", encoding="utf-8") as fh:
        fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))


def put(target: str, reason: str = "") -> int:
    if not os.path.exists(target):
        print("找不到: %s" % target)
        return 1
    os.makedirs(TRASH, exist_ok=True)
    base = os.path.basename(target.rstrip("\\/"))
    dest = os.path.join(TRASH, "%s__%s" % (_stamp(), base))
    shutil.move(target, dest)
    _note("搬入 %s  （原因：%s）" % (dest, reason or "未说明"))
    print("已搬入回收目录: %s" % dest)
    return 0


def snapshot(target: str, reason: str = "") -> int:
    """原文件不动，只在回收目录留一份带时间戳的副本。

    改代码前先跑这个，改坏了从这里取回来即可。
    """
    if not os.path.exists(target):
        print("找不到: %s" % target)
        return 1
    os.makedirs(TRASH, exist_ok=True)
    base = os.path.basename(target.rstrip("\\/"))
    dest = os.path.join(TRASH, "%s__%s" % (_stamp(), base))
    if os.path.isdir(target):
        shutil.copytree(target, dest)
    else:
        shutil.copy2(target, dest)
    _note("留副本 %s  （说明：%s）" % (dest, reason or "未说明"))
    print("已留副本: %s" % dest)
    return 0


def list_items() -> int:
    if not os.path.isdir(TRASH):
        print("回收目录还不存在")
        return 0
    items = sorted(x for x in os.listdir(TRASH) if x != "索引.txt")
    if not items:
        print("回收目录是空的")
    for name in items:
        path = os.path.join(TRASH, name)
        size = os.path.getsize(path) if os.path.isfile(path) else -1
        print("   %-58s %s" % (name, ("%d 字节" % size) if size >= 0 else "目录"))
    print("")
    print("取回: python tools/trash.py restore <名字> [目标路径]")
    return 0


def restore(name: str, target: str = "") -> int:
    src = os.path.join(TRASH, name)
    if not os.path.exists(src):
        matches = [x for x in os.listdir(TRASH) if name in x]
        if len(matches) != 1:
            print("找不到或匹配到多个: %s" % (matches or name))
            return 1
        src = os.path.join(TRASH, matches[0])
    dest = target or os.path.join(ROOT, name.split("__", 1)[-1])
    if os.path.exists(dest):
        print("目标已存在，先改成别的名字或删掉它: %s" % dest)
        return 1
    shutil.move(src, dest)
    _note("取回 %s 到 %s" % (src, dest))
    print("已取回: %s" % dest)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    action = argv[0]
    if action == "put":
        return put(argv[1], argv[2] if len(argv) > 2 else "") if len(argv) > 1 else 2
    if action == "snapshot":
        return snapshot(argv[1], argv[2] if len(argv) > 2 else "") if len(argv) > 1 else 2
    if action == "list":
        return list_items()
    if action == "restore":
        return restore(argv[1], argv[2] if len(argv) > 2 else "") if len(argv) > 1 else 2
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
