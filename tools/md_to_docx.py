"""把 docs/视频拍摄脚本.md 转成排版好的 Word（.docx）。

为什么不用通用转换器：这份脚本的结构是固定的（标题 / 台词 / 步骤 / 表格 / 代码块），
自己按行解析比装 pandoc 更可控，也能把"台词"单独做成灰底斜体，方便拍摄时照着念。

用法：
    python tools/md_to_docx.py docs/视频拍摄脚本.md 视频拍摄脚本.docx
"""

from __future__ import annotations

import pathlib
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

CJK = "微软雅黑"
MONO = "Consolas"


def set_cjk(run, name: str = CJK) -> None:
    """让中文也用指定字体（Word 需要单独设 eastAsia）。"""
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), name)
    fonts.set(qn("w:ascii"), name)
    fonts.set(qn("w:hAnsi"), name)


def shade(paragraph, color: str) -> None:
    pr = paragraph._p.get_or_add_pPr()
    el = OxmlElement("w:shd")
    el.set(qn("w:val"), "clear")
    el.set(qn("w:fill"), color)
    pr.append(el)


def add_code_block(doc, lines: list[str]) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Pt(14)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)
    shade(p, "F2F2F2")
    for i, line in enumerate(lines):
        if i:
            p.add_run().add_break()
        r = p.add_run(line)
        r.font.size = Pt(10)
        set_cjk(r, MONO)


def add_quote(doc, lines: list[str]) -> None:
    for line in lines:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(18)
        p.paragraph_format.space_before = Pt(3)
        p.paragraph_format.space_after = Pt(3)
        shade(p, "FFF6E5")
        r = p.add_run(line)
        r.italic = True
        r.font.size = Pt(12)
        set_cjk(r)


def add_table(doc, rows: list[list[str]]) -> None:
    if not rows:
        return
    cols = max(len(r) for r in rows)
    t = doc.add_table(rows=0, cols=cols)
    t.style = "Table Grid"
    for i, row in enumerate(rows):
        cells = t.add_row().cells
        for j in range(cols):
            text = row[j] if j < len(row) else ""
            cell = cells[j]
            cell.text = ""
            p = cell.paragraphs[0]
            r = p.add_run(text)
            r.font.size = Pt(10)
            set_cjk(r)
            if i == 0:
                r.bold = True
                shade(p, "E8E8E8")
    doc.add_paragraph()


def inline_runs(p, text: str) -> None:
    """处理 **加粗** 和 `代码`。"""
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            r = p.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`"):
            r = p.add_run(part[1:-1])
            set_cjk(r, MONO)
            r.font.size = Pt(10.5)
            continue
        else:
            r = p.add_run(part)
        set_cjk(r)
        r.font.size = Pt(11)


def main() -> int:
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs/视频拍摄脚本.md")
    dst = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "视频拍摄脚本.docx")
    lines = src.read_text(encoding="utf-8").splitlines()

    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(11)
    style.font.name = CJK
    style.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)

    in_code = False
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        buf = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # 代码块
        if line.startswith("```"):
            if in_code:
                add_code_block(doc, buf)
                in_code = False
                flush()
            else:
                in_code = True
                flush()
            i += 1
            continue
        if in_code:
            buf.append(raw)
            i += 1
            continue

        # 表格
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            add_table(doc, rows)
            continue

        # 引用块（本脚本里就是"台词"）
        if line.startswith(">"):
            q: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(lines[i].strip().lstrip(">").strip())
                i += 1
            add_quote(doc, [x for x in q if x])
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if text.startswith("【第") or text.startswith("【0"):
                doc.add_page_break()
            if level == 1:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = p.add_run(text)
                r.bold = True
                r.font.size = Pt(24)
                set_cjk(r)
                p.paragraph_format.space_after = Pt(18)
            else:
                p = doc.add_paragraph()
                r = p.add_run(text)
                r.bold = True
                r.font.size = Pt({2: 17, 3: 14, 4: 12}.get(level, 12))
                set_cjk(r)
                p.paragraph_format.space_before = Pt(14 if level == 2 else 10)
                p.paragraph_format.space_after = Pt(6)
                if level == 2:
                    r.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
                elif level == 3:
                    r.font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)
            i += 1
            continue

        # 分隔线 → 忽略（靠分页区分段落）
        if re.fullmatch(r"-{3,}", line.strip()):
            i += 1
            continue

        # 列表
        m = re.match(r"^(\s*)([-*+]|\d+\.|□|⚠️)\s+(.*)$", line)
        if m:
            indent, marker, text = m.group(1), m.group(2), m.group(3)
            ordered = marker.endswith(".")
            p = doc.add_paragraph(style="List Number" if ordered else "List Bullet")
            p.paragraph_format.left_indent = Pt(18 + len(indent) * 6)
            p.paragraph_format.space_after = Pt(2)
            inline_runs(p, text)
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        inline_runs(p, line)
        i += 1

    doc.save(str(dst))
    print("已生成: %s  (%.1f KB)" % (dst, dst.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
