#!/usr/bin/env python3
"""PDF（书籍）→ Markdown CLI.

依赖: 仓库根目录运行 ./install.sh（PyMuPDF）；无文字层的扫描件另需 ocrmypdf（./install.sh --system）

用法:
  ./misctools pdf2md book.pdf
  ./misctools pdf2md book.pdf -o book.md
  ./misctools pdf2md book.pdf --structure contents
  ./misctools pdf2md book.pdf --analyze        # 只输出判断结果，不转换

流程:
  1. 判断 PDF 类型：原生文字 / 扫描件带 OCR 文字层（如 Google Books）/ 扫描件无文字层（先跑 ocrmypdf）
  2. 选择章节结构来源（--structure auto 时按可靠程度自动选择）:
       outline   PDF 书签（原生 PDF 通常可靠）
       contents  书前印刷目录（老书扫描件：CHAPTER + 标题 + 小节列表 + 页码）
       fonts     字号 / 粗体 / 斜体 / 居中（原生 PDF 无书签时；ocrmypdf 的 OCR 结果也可用，Google Books 不用）
       pattern   正文中的「CHAPTER I」「PART II」章首标记（兜底）
  3. 去页眉页脚、页码、印张标记、插图页与图注；重建段落、合并断词；脚注转为 Markdown 脚注；OCR 文字做常见纠错
  4. 标题最多 4 级（# 至 ####）
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import pymupdf
except ImportError:
    # 推迟到参数解析之后再报错，保证未安装依赖时 --help 仍可用
    pymupdf = None


MAX_LEVEL = 4
SKIP_SECTION_TITLES = {"index", "索引"}
OCR_PRODUCER_HINTS = ("google", "abbyy", "tesseract", "ocrmypdf", "finereader", "omnipage")

# OCR 常把拉丁字母识别成形近的希腊/西里尔字母，例如目录里的「(ΤΟ 1510)」
HOMOGLYPHS = str.maketrans(
    "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧαοριυνАВЕКМНОРСТХаеорсуухіји",
    "ABEZHIKMNOPTYXaopiuvABEKMHOPCTXaeopcyyxiju",
)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    centered: bool = False

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Page:
    index: int
    width: float
    height: float
    lines: List[Line]
    figures: List[Tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class Heading:
    level: int
    title: str
    page: Optional[int] = None  # 起始的 PDF 页（0 起）
    subtitle: str = ""
    label: str = ""  # 如「Chapter I」
    topics: List[str] = field(default_factory=list)


@dataclass
class Analysis:
    kind: str  # digital | ocr | scanned
    google_books: bool
    producer: str
    pages: int
    notes: List[str] = field(default_factory=list)


@dataclass
class Item:
    """输出流中的一项：标题，或带「是否新段落」标记的一行正文。"""

    kind: str  # heading | line | break
    text: str = ""
    new_para: bool = False
    heading: Optional[Heading] = None


# ---------------------------------------------------------------- 文本工具


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.translate(HOMOGLYPHS).lower()).strip()


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def is_mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) > 0.8


SMALL_WORDS = {
    "a", "an", "the", "and", "but", "or", "nor", "for", "of", "in", "on", "at",
    "to", "by", "up", "as", "with", "from", "into", "under", "over",
}


def title_case(text: str) -> str:
    words = text.lower().split()
    out = []
    for i, w in enumerate(words):
        if i and w in SMALL_WORDS:
            out.append(w)
        else:
            out.append(re.sub(r"(^|-)([^a-z]*)([a-z])", lambda m: m.group(1) + m.group(2) + m.group(3).upper(), w))
    return " ".join(out)


ROMAN_RE = r"[IVXLC]+"


def pair_quotes(text: str) -> str:
    """OCR 常把直引号两侧的空格放错（`" Freedom "`、`"Anti-Convict "Agitation`）。
    引号成对时按开/合交替重排空格；单数说明引文跨段，不动。"""
    if text.count('"') % 2:
        return text
    parts = text.split('"')
    out = parts[0]
    for i, seg in enumerate(parts[1:]):
        if i % 2 == 0:  # 开引号
            out = out.rstrip(" ")
            if out and not out.endswith(("(", "[", "—", "-", "'")):
                out += " "
            out += '"' + seg.lstrip(" ")
        else:  # 合引号
            out = out.rstrip(" ") + '"'
            seg = seg.lstrip(" ")
            if seg and (seg[0].isalnum() or seg[0] in "(“"):
                seg = " " + seg
            out += seg
    return out


LATIN_SCRIPT = True  # convert() 按全文字母分布设置；希腊 / 西里尔文书籍不能做形近字替换


def fix_homoglyphs(text: str) -> str:
    return text.translate(HOMOGLYPHS) if LATIN_SCRIPT else text


def clean_ocr_text(text: str) -> str:
    text = fix_homoglyphs(text)
    text = re.sub(r"\s+([.,;:!?])(?=\s|$|[\"'”’)\]—.,;:-])", r"\1", text)
    text = re.sub(r":-(?=\s*\S)", ":—", text)
    text = re.sub(r"(?<!\S)ود(?!\S)", "〃", text)
    text = re.sub(r'(^|[\s(\[—])"\s+(?=\w)', r'\1"', text)
    text = re.sub(r'(?<=[\w.,;!?])\s+"(?=[\s.,;:!?)\]]|$)', '"', text)
    text = re.sub(r"\s*--+\s*", "—", text)
    text = re.sub(r"\.-(?=[A-Z\"“])", ".—", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return pair_quotes(text).strip()


# ---------------------------------------------------------------- 读取页面


def spans_of(page_dict) -> Iterable[dict]:
    for block in page_dict["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip():
                    yield span


def span_is_bold(span: dict) -> bool:
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()


def span_is_italic(span: dict) -> bool:
    font = span["font"].lower()
    return bool(span["flags"] & 2) or "italic" in font or "oblique" in font


def join_spans(spans: List[dict]) -> Line:
    spans = sorted(spans, key=lambda s: s["bbox"][0])
    text = ""
    prev = None
    for s in spans:
        t = s["text"]
        if prev is not None:
            gap = s["bbox"][0] - prev["bbox"][2]
            if gap > s["size"] * 0.15 and not text.endswith(" ") and not t.startswith(" "):
                text += " "
        text += t
        prev = s
    chars = Counter()
    for s in spans:
        chars[round(s["size"], 1)] += len(s["text"].strip())
    return Line(
        text=re.sub(r"\s+", " ", text).strip(),
        x0=min(s["bbox"][0] for s in spans),
        y0=min(s["bbox"][1] for s in spans),
        x1=max(s["bbox"][2] for s in spans),
        y1=max(s["bbox"][3] for s in spans),
        size=chars.most_common(1)[0][0],
        bold=all(span_is_bold(s) for s in spans),
        italic=all(span_is_italic(s) for s in spans),
    )


def lines_by_geometry(spans: List[dict]) -> List[Line]:
    """OCR 文字层：同一视觉行的碎片 y 坐标会相差数点，按行中心重新归并。"""
    if not spans:
        return []
    heights = [s["bbox"][3] - s["bbox"][1] for s in spans]
    tol = 0.45 * median(heights)
    spans = sorted(spans, key=lambda s: (s["bbox"][1] + s["bbox"][3]) / 2)
    groups: List[List[dict]] = []
    centers: List[float] = []
    for s in spans:
        cy = (s["bbox"][1] + s["bbox"][3]) / 2
        if groups and cy - centers[-1] <= tol:
            groups[-1].append(s)
            centers[-1] = sum((g["bbox"][1] + g["bbox"][3]) / 2 for g in groups[-1]) / len(groups[-1])
        else:
            groups.append([s])
            centers.append(cy)
    return [join_spans(g) for g in groups]


def lines_by_pdf_order(page_dict) -> List[Line]:
    out = []
    for block in page_dict["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                out.append(join_spans(spans))
    return out


def mark_centered(lines: List[Line], page_width: float) -> None:
    """相对本页正文块（而非纸张，书页左右边距常不对称）判断是否居中。"""
    if len(lines) < 3:
        return
    left = sorted(l.x0 for l in lines)[len(lines) // 10]
    right = sorted(l.x1 for l in lines)[(len(lines) * 9) // 10]
    tol = 0.04 * page_width
    for l in lines:
        l.centered = l.x0 - left > tol and abs((l.x0 - left) - (right - l.x1)) < tol


def extract_pages(doc, kind: str) -> List[Page]:
    pages = []
    for page in doc:
        # 只取文字：默认的 dict 会带上整页扫描图的数据，慢两个数量级
        d = page.get_text("dict", sort=True, flags=pymupdf.TEXTFLAGS_TEXT)
        if kind == "digital":
            lines = lines_by_pdf_order(d)
        else:
            lines = lines_by_geometry(list(spans_of(d)))
        mark_centered(lines, page.rect.width)
        area = page.rect.width * page.rect.height
        figures = []
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            # 扫描件整页底图不算插图；Google Books 会把插图区域另存一张图
            if 0.02 * area < (x1 - x0) * (y1 - y0) < 0.9 * area:
                figures.append((x0, y0, x1, y1))
        pages.append(Page(page.number, page.rect.width, page.rect.height, lines, figures))
    return pages


def drop_figure_text(page: Page, lines: List[Line], body_height: float) -> List[Line]:
    """去掉插图内部的文字以及紧挨插图、字号偏小且居中的图注 / 出处行。"""
    if not page.figures:
        return lines
    out = []
    for l in lines:
        drop = False
        mid = (l.x0 + l.x1) / 2
        for x0, y0, x1, y1 in page.figures:
            if x0 - 2 <= mid <= x1 + 2 and y0 - 2 <= l.cy <= y1 + 2:
                drop = True
            elif (
                l.height < 0.85 * body_height
                and abs(mid - (x0 + x1) / 2) < 0.12 * page.width
                and (y1 <= l.cy <= y1 + 40 or y0 - 30 <= l.cy <= y0)
            ):
                drop = True
        if not drop:
            out.append(l)
    return out


FOOTNOTE_RE = re.compile(r"^(\d{1,2}|[*†‡])\s+[A-Z\"“(]")
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def footnote_ref(marker: str) -> re.Pattern:
    """正文中的脚注角标：紧贴在词或标点后的数字 / 符号，或上标数字。"""
    sup = marker.translate(SUPERSCRIPT)
    alts = re.escape(marker) + (f"|{re.escape(sup)}" if sup != marker else "")
    return re.compile(r"(?<=[^\s\d⁰¹²³⁴⁵⁶⁷⁸⁹])(?:" + alts + r")(?=[\s.,;:)]|$)")


def split_footnotes(page: Page, lines: List[Line], body_height: float, pitch: float) -> Tuple[List[Line], List[str]]:
    """页面下部以「1 」「* 」开头、字号偏小或与正文隔开的行视为脚注，连同其后各行一起取出。"""
    for i in range(1, len(lines)):
        l = lines[i]
        if l.cy < 0.6 * page.height or not FOOTNOTE_RE.match(l.text):
            continue
        ref = footnote_ref(l.text.split(None, 1)[0])
        referenced = any(ref.search(p.text) for p in lines[:i])
        if referenced or l.height < 0.9 * body_height or l.cy - lines[i - 1].cy > 1.4 * pitch:
            notes: List[str] = []
            for n in lines[i:]:
                if FOOTNOTE_RE.match(n.text) or not notes:
                    notes.append(n.text)
                else:
                    notes[-1] += "\n" + n.text
            return lines[:i], notes
    return lines, []


# ---------------------------------------------------------------- 类型判断


def analyze(doc) -> Analysis:
    producer = (doc.metadata or {}).get("producer", "") or ""
    n = len(doc)
    sample = sorted(set(range(0, n, max(1, n // 40))))
    char_counts = []
    full_image_pages = 0
    glyphless = False
    for i in sample:
        page = doc[i]
        char_counts.append(len(page.get_text().strip()))
        area = page.rect.width * page.rect.height
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            if (x1 - x0) * (y1 - y0) > 0.8 * area:
                full_image_pages += 1
                break
        if any("glyphless" in f[3].lower() for f in page.get_fonts()):
            glyphless = True

    google = "google" in producer.lower()
    notes = []
    text_pages = sum(c > 100 for c in char_counts)
    if text_pages < 0.2 * len(sample):
        kind = "scanned"
        notes.append("几乎没有文字层，需要先 OCR")
    elif glyphless or full_image_pages > 0.6 * len(sample) or any(
        h in producer.lower() for h in OCR_PRODUCER_HINTS
    ):
        kind = "ocr"
        notes.append("扫描页面 + 隐形 OCR 文字层；字号不可靠，按版面位置与印刷目录确定结构")
    else:
        kind = "digital"
        notes.append("原生文字 PDF；可使用书签与字号信息")
    return Analysis(kind, google, producer, n, notes)


def run_ocr(src: Path, lang: str, force: bool) -> Path:
    if not shutil.which("ocrmypdf"):
        raise SystemExit("错误: 需要 ocrmypdf 才能处理扫描件。请运行 ./install.sh --system")
    out = Path(tempfile.mkdtemp(prefix="pdf2md.")) / "ocr.pdf"
    cmd = ["ocrmypdf", "--force-ocr" if force else "--skip-text", "-l", lang, str(src), str(out)]
    print(f"正在 OCR（ocrmypdf {cmd[1]}，语言 {lang}）…", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
        raise SystemExit(f"错误: ocrmypdf 失败（退出码 {result.returncode}）")
    return out


# ---------------------------------------------------------------- 页眉页脚与噪声


def line_pitch(lines: List[Line]) -> float:
    gaps = [b.cy - a.cy for a, b in zip(lines, lines[1:]) if b.cy - a.cy > 0]
    return median(gaps) if gaps else 12.0


def strip_header_footer(page: Page, doc_pitch: Optional[float] = None) -> List[Line]:
    lines = sorted(page.lines, key=lambda l: l.cy)
    if len(lines) < 3:
        return lines
    pitch = line_pitch(lines)
    if doc_pitch:
        # 插图页正文行少，页内行距会被插图拉大
        pitch = min(pitch, doc_pitch)
    # 页眉：位于页面顶部 12% 内，且与下一行的间距明显大于行距
    while len(lines) >= 2 and lines[0].cy < 0.12 * page.height and lines[1].cy - lines[0].cy > 1.5 * pitch:
        lines = lines[1:]
    # 页脚：位于底部 12% 内、文字很短（页码、印张标记「B」「C 2」）
    while (
        len(lines) >= 2
        and lines[-1].cy > 0.88 * page.height
        and lines[-1].cy - lines[-2].cy > 1.4 * pitch
        and len(lines[-1].text) <= 40
    ):
        lines = lines[:-1]
    # 印张标记（signature mark）紧贴最后一行，没有明显间距，只能按样式识别
    if len(lines) >= 2 and lines[-1].cy > 0.85 * page.height and SIGNATURE_RE.match(lines[-1].text.strip()):
        lines = lines[:-1]
    return lines


SIGNATURE_RE = re.compile(r"^[A-Z]{1,2}(\s?\d{1,2})?\*?$|^\d{1,2}\*?$|^\d{1,2}\s?[-—*]\s?\d{1,2}$")


def repeated_margin_texts(pages: List[Page]) -> set:
    """原生 PDF：多页在同一位置重复出现的页眉页脚文字（去掉数字后比较）。"""
    counter: Counter = Counter()
    for p in pages:
        if not p.lines:
            continue
        ordered = sorted(p.lines, key=lambda l: l.cy)
        for l in ordered[:2] + ordered[-2:]:
            key = re.sub(r"\d+", "#", norm(l.text))
            if key:
                counter[key] += 1
    threshold = max(3, len(pages) // 5)
    return {k for k, c in counter.items() if c >= threshold}


def is_noise_line(line: Line, body_width: float) -> bool:
    t = line.text.strip()
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", t):
        return True
    if re.match(r"^\[?\s*Photo\s*[:.]", t, re.IGNORECASE):
        return True
    # 图注：全大写、明显短于版心宽度
    if is_mostly_upper(t) and (line.x1 - line.x0) < 0.75 * body_width and not re.match(
        r"^(CHAPTER|PART|BOOK)\b", t
    ):
        return True
    return False


# ---------------------------------------------------------------- 印刷目录


CONTENTS_TITLES = {"contents", "table of contents", "目录", "目 录"}


def find_contents_pages(pages: List[Page]) -> List[int]:
    limit = max(10, len(pages) // 6)
    start = None
    for p in pages[:limit]:
        tops = [norm(l.text) for l in sorted(p.lines, key=lambda l: l.cy)[:3]]
        if any(t in CONTENTS_TITLES for t in tops):
            start = p.index
            break
    if start is None:
        return []
    result = [start]
    for p in pages[start + 1 : limit + 10]:
        tops = sorted(p.lines, key=lambda l: l.cy)[:2]
        head = " ".join(norm(l.text) for l in tops)
        has_chapter = any(re.match(r"^(chapter|part)\b", norm(l.text)) for l in p.lines)
        if "contents" in head or "目录" in head or has_chapter:
            result.append(p.index)
        else:
            break
    return result


def split_topics(lines: List[str]) -> List[str]:
    text = ""
    for ln in lines:
        ln = ln.strip()
        if not text:
            text = ln
        elif re.search(r"[a-z]-$", text) and re.match(r"[a-z]", ln):
            text = text[:-1] + ln  # 跨行断词
        elif text.endswith("-"):
            text += ln
        else:
            text += " " + ln
    text = fix_homoglyphs(text)
    parts = re.split(r"\s*[-–—]{1,2}\s*(?=[A-Z0-9\"“'(])|\s+[-–—]\s+", text)
    out = []
    for p in parts:
        p = clean_ocr_text(p).strip(" .-")
        if not p:
            continue
        if p.count('"') % 2:
            # 分隔符吞掉了收尾引号：`Wreck of the "Haarlem"-Next` → `Wreck of the "Haarlem`
            p = p[:-1].rstrip() if p.endswith('"') else p + '"'
        out.append(p)
    return out


def parse_contents(pages: List[Page], contents_pages: List[int]) -> List[Heading]:
    entries: List[Heading] = []
    flat: List[Heading] = []
    current: Optional[Heading] = None
    topic_lines: List[str] = []
    has_parts = False

    def finish():
        nonlocal topic_lines
        if current is not None and topic_lines:
            current.topics = split_topics(topic_lines)
        topic_lines = []

    for idx in contents_pages:
        for line in sorted(pages[idx].lines, key=lambda l: l.cy):
            s = line.text.translate(HOMOGLYPHS).strip()
            if re.fullmatch(r"(?i)[ivxlc\d]*\s*(contents|table of contents|目录)\s*[ivxlc\d]*", s):
                continue
            if s.upper() in {"PAGE", "PAGES"}:
                continue
            m = re.match(r"(?i)^(chapter|part|book)\s+(" + ROMAN_RE + r"|\d+)\b\.?\s*(.*)$", s)
            if m:
                finish()
                kind = m.group(1).lower()
                has_parts = has_parts or kind in {"part", "book"}
                current = Heading(
                    level=1 if kind in {"part", "book"} else 2,
                    title="",
                    label=f"{m.group(1).title()} {m.group(2).upper()}",
                )
                rest = m.group(3).strip()
                pm = re.search(r"\s(\d{1,4})\s*$", rest)
                if pm:
                    current.page = int(pm.group(1))
                    rest = rest[: pm.start()].strip(" .")
                if rest:
                    current.title = rest
                entries.append(current)
                continue
            if current is None:
                fm = re.match(r"^(.+?)[\s.·…]{2,}(\d{1,4})$", s)
                if fm:
                    flat.append(Heading(level=1, title=fm.group(1).strip(), page=int(fm.group(2))))
                continue
            if re.fullmatch(r"\(.+\)", s) and not current.subtitle and not topic_lines:
                current.subtitle = s
                continue
            if not topic_lines and is_mostly_upper(s) and current.page is None:
                current.title = (current.title + " " + s).strip()
                continue
            pm = re.search(r"\s(\d{1,4})\s*$", s)
            if pm and current.page is None:
                current.page = int(pm.group(1))
                s = s[: pm.start()].rstrip(" .")
            if s:
                topic_lines.append(s)
    finish()

    if not entries:
        return flat
    if not has_parts:
        for e in entries:
            e.level = 1
    for e in entries:
        e.title = clean_ocr_text(e.title)
        if is_mostly_upper(e.title):
            e.title = title_case(e.title)
        e.subtitle = re.sub(r"^\(TO\b", "(To", clean_ocr_text(e.subtitle))
    return entries


def page_offset(pages: List[Page], start: int) -> Optional[int]:
    """PDF 页下标与印刷页码之差：取页眉/页脚中纯数字的众数。"""
    votes: Counter = Counter()
    for p in pages[start:]:
        ordered = sorted(p.lines, key=lambda l: l.cy)
        for l in ordered[:2] + ordered[-1:]:
            for tok in re.findall(r"(?<![\w.])(\d{1,4})(?![\w.])", l.text):
                votes[p.index - int(tok)] += 1
    if not votes:
        return None
    offset, count = votes.most_common(1)[0]
    return offset if count >= 5 else None


CHAPTER_MARKER_RE = re.compile(
    r"^(CHAPTER|Chapter|PART|Part)(?:\s+(" + ROMAN_RE + r"|\d+)\b\.?(?:\s+(.*))?)?\s*$"
)


def chapter_marker(line: Line) -> bool:
    t = line.text.translate(HOMOGLYPHS).strip()
    return len(t) < 60 and bool(CHAPTER_MARKER_RE.match(t))


def marker_numeral(page: Page) -> Tuple[bool, Optional[str]]:
    """页面上半部是否有章标记，以及标记里（或紧随其后一行）的章号。"""
    ordered = sorted(page.lines, key=lambda l: l.cy)
    for i, l in enumerate(ordered):
        if l.cy >= 0.5 * page.height:
            break
        if not chapter_marker(l):
            continue
        m = CHAPTER_MARKER_RE.match(l.text.translate(HOMOGLYPHS).strip())
        if m.group(2):
            return True, m.group(2).upper()
        if i + 1 < len(ordered):
            nxt = ordered[i + 1].text.translate(HOMOGLYPHS).strip().rstrip(".")
            if re.fullmatch(ROMAN_RE + r"|\d{1,3}", nxt):
                return True, nxt.upper()
        return True, None
    return False, None


def label_numeral(label: str) -> Optional[str]:
    m = re.search(r"\s(\S+)$", label or "")
    return m.group(1).upper() if m else None


def locate_chapters(pages: List[Page], entries: List[Heading], after: int) -> None:
    offset = page_offset(pages, after)
    marker_pages: List[int] = []
    by_numeral: Dict[str, int] = {}
    for p in pages[after:]:
        found, numeral = marker_numeral(p)
        if found:
            marker_pages.append(p.index)
            if numeral and numeral not in by_numeral:
                by_numeral[numeral] = p.index

    prev = after
    for e in entries:
        numeral = label_numeral(e.label)
        if numeral in by_numeral and by_numeral[numeral] > prev:
            e.page = prev = by_numeral[numeral]
            continue
        expected = e.page + offset if (e.page is not None and offset is not None) else None
        found = None
        if expected is not None:
            near = [m for m in marker_pages if expected - 2 <= m <= expected + 3 and m > prev]
            found = min(near, key=lambda m: abs(m - expected)) if near else expected
        else:
            later = [m for m in marker_pages if m > prev]
            found = later[0] if later else None
        e.page = found
        if found is not None:
            prev = found

    # 目录里漏掉的章（扫描页被裁切、OCR 漏行）：正文里有章标记但没有对应条目，就从正文补上
    assigned = {e.page for e in entries}
    level = entries[0].level if entries else 1
    known = [e.page for e in entries if e.page is not None]
    for m in marker_pages:
        if m not in assigned and known and m > min(known):
            entries.append(Heading(level=level, title="", page=m))
    entries.sort(key=lambda e: (e.page is None, e.page or 0))


def pattern_headings(pages: List[Page]) -> List[Heading]:
    """没有书签也没有书前目录时：页面上半部出现 CHAPTER / PART 标记的页即章首页。
    章号、标题、副标题留给 strip_chapter_lines 从正文补齐。"""
    skip = set(find_contents_pages(pages))
    found: List[Tuple[int, str]] = []
    for p in pages:
        if p.index in skip:
            continue
        has_marker, _ = marker_numeral(p)
        if not has_marker:
            continue
        word = next(
            CHAPTER_MARKER_RE.match(l.text.translate(HOMOGLYPHS).strip()).group(1).lower()
            for l in sorted(p.lines, key=lambda l: l.cy)
            if chapter_marker(l)
        )
        found.append((p.index, word))
    has_parts = any(w == "part" for _, w in found) and any(w == "chapter" for _, w in found)
    return [Heading(level=2 if has_parts and w == "chapter" else 1, title="", page=i) for i, w in found]


def strip_chapter_lines(lines: List[Line], heading: Heading) -> Tuple[List[Line], Optional[str], Optional[str]]:
    """去掉章首页顶部的「CHAPTER / I / (To 1510) / 标题」，返回正文行与正文中的标题、副标题写法。

    目录里没有这一章时（heading.title 为空），同时从这些行补出章号与标题。"""
    body_title = body_sub = None
    i = 0
    while i < len(lines) and i < 8:
        t = lines[i].text.translate(HOMOGLYPHS).strip()
        m = CHAPTER_MARKER_RE.match(t)
        if m and len(t) < 60:
            if not heading.label:
                heading.label = m.group(1).title() + (f" {m.group(2)}" if m.group(2) else "")
            i += 1
            continue
        if re.fullmatch(ROMAN_RE + r"\.?", t) or re.fullmatch(r"\d{1,3}", t):
            if heading.label and not re.search(r"\s\S+$", heading.label):
                heading.label += " " + t.rstrip(".")
            i += 1
            continue
        if re.fullmatch(r"\(.+\)", t):
            body_sub = t
            i += 1
            continue
        if heading.title and similar(t, heading.title) > 0.6:
            body_title = clean_ocr_text(t)
            i += 1
            continue
        if not heading.title and len(t) < 80 and not t.endswith((".", ",", ";")):
            heading.title = clean_ocr_text(t)
            if is_mostly_upper(heading.title):
                heading.title = title_case(heading.title)
            body_title = heading.title
            i += 1
            continue
        if is_mostly_upper(t) and len(t) < 80:
            i += 1
            continue
        break
    return lines[i:], body_title, body_sub


# ---------------------------------------------------------------- 结构：书签 / 字号 / 文字规律


def outline_headings(doc) -> List[Heading]:
    out = []
    for level, title, page in doc.get_toc(simple=True):
        if page < 1:
            continue
        out.append(Heading(level=min(level, MAX_LEVEL), title=title.strip(), page=page - 1))
    return out


StyleKey = Tuple[float, bool, bool, bool]


def heading_candidate(line: Line) -> bool:
    t = line.text.strip()
    return len(t) < 100 and bool(re.search(r"[A-Za-z\u4e00-\u9fff]", t)) and not t.endswith((".", ",", ";", ":"))


class FontLevels:
    """标题样式 → 级别。

    原生 PDF：样式 = 字号 + 粗体 + 斜体 + 居中，比正文大或与正文同号但加粗才算候选。
    OCR（ocrmypdf / tesseract）：只有字号（由行高推算）可信且有抖动，按比例聚类，并要求明显大于正文。
    """

    # tesseract 推算的同一样式字号会抖动约 ±10%，正文行最高约到 1.08 倍
    OCR_MIN_RATIO = 1.18
    OCR_CLUSTER_RATIO = 1.12

    def __init__(self, pages: List[Page], ocr: bool = False):
        self.ocr = ocr
        self.clusters: List[float] = []
        self.levels: Dict[StyleKey, int] = {}
        sizes: Counter = Counter()
        for p in pages:
            for l in p.lines:
                sizes[round(l.size * 2) / 2] += len(l.text)
        if not sizes:
            self.body = 0.0
            return
        self.body = sizes.most_common(1)[0][0]
        total = sum(sizes.values())
        candidates = [l for p in pages for l in p.lines if heading_candidate(l)]
        if ocr:
            for s in sorted({l.size for l in candidates if l.size >= self.OCR_MIN_RATIO * self.body}, reverse=True):
                if not self.clusters or self.clusters[-1] / s > self.OCR_CLUSTER_RATIO:
                    self.clusters.append(s)
        styles: Counter = Counter()
        for l in candidates:
            key = self.key(l)
            if key:
                styles[key] += len(l.text)
        # 大量出现的样式是正文的一部分（如整段粗体的引文），不是标题
        keys = [k for k, chars in styles.items() if chars < 0.15 * total]
        # 字号大者在上；同号时 粗体 > 居中 > 非斜体
        keys.sort(key=lambda k: (-k[0], not k[1], not k[3], k[2]))
        self.levels = {k: min(i + 1, MAX_LEVEL) for i, k in enumerate(keys)}

    def __len__(self) -> int:
        return len(self.levels)

    def key(self, line: Line) -> Optional[StyleKey]:
        if self.ocr:
            if not self.clusters or line.size < self.OCR_MIN_RATIO * self.body:
                return None
            rep = min(self.clusters, key=lambda c: abs(c - line.size))
            return (rep, False, False, line.centered)
        size = round(line.size * 2) / 2
        if line.size - self.body >= 1.0:
            return (size, line.bold, line.italic, line.centered)
        if abs(line.size - self.body) < 0.6 and line.bold:
            return (size, True, line.italic, line.centered)
        return None

    def level(self, line: Line) -> Optional[int]:
        if not heading_candidate(line):
            return None
        key = self.key(line)
        return self.levels.get(key) if key else None


# ---------------------------------------------------------------- 正文流


class Builder:
    def __init__(self, ocr: bool):
        self.ocr = ocr
        self.items: List[Item] = []
        self.prev: Optional[Line] = None
        self.prev_right = 0.0

    def heading(self, h: Heading):
        self.items.append(Item("heading", heading=h))
        self.prev = None

    def footnote(self, text: str):
        self.items.append(Item("footnote", text=text))

    def page_lines(self, lines: List[Line], gap_breaks: bool):
        if not lines:
            return
        body_lines = [l for l in lines if len(l.text) > 25] or lines
        left = sorted(l.x0 for l in body_lines)[len(body_lines) // 10]
        right = sorted(l.x1 for l in body_lines)[(len(body_lines) * 9) // 10]
        width = max(right - left, 1.0)
        pitch = line_pitch(lines)
        for line in lines:
            if is_noise_line(line, width):
                continue
            indent = line.x0 - left
            new_para = 0.03 * width < indent < 0.45 * width
            if self.prev is not None:
                short = self.prev.x1 < self.prev_right - 0.12 * width
                if short and re.search(r"[.?!:\"”’)]$", self.prev.text):
                    new_para = True
                if gap_breaks and line.cy - self.prev.cy > 1.6 * pitch and self.prev in lines:
                    new_para = True
            if line.x0 - left > 0.45 * width and len(line.text) < 0.5 * len(self.prev.text if self.prev else "x" * 99):
                new_para = True
            self.items.append(Item("line", text=line.text, new_para=new_para))
            self.prev = line
            self.prev_right = right


def word_counts(pages: List[Page]) -> Counter:
    counts: Counter = Counter()
    for p in pages:
        for l in p.lines:
            counts.update(w.lower() for w in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", l.text))
    return counts


def join_lines(lines: List[str], counts: Counter) -> str:
    text = ""
    for ln in lines:
        if not text:
            text = ln
            continue
        m = re.search(r"([A-Za-z]+)-$", text)
        n = re.match(r"([A-Za-z]+)", ln)
        if m and n:
            a, b = m.group(1), n.group(1)
            hyphenated = counts[f"{a}-{b}".lower()]
            joined = counts[(a + b).lower()]
            if b[0].isupper() or hyphenated > joined:
                text = text + ln  # 保留连字符，如 sea-route、Anglo-Boer
            else:
                text = text[:-1] + ln
        elif text.endswith(("-", "—")):
            text += ln
        else:
            text += " " + ln
    return text


def is_garbage_page(lines: List[Line], counts: Counter) -> bool:
    words = [w.lower() for l in lines for w in re.findall(r"[A-Za-z]{3,}", l.text)]
    if len(words) < 8:
        return False
    known = sum(counts[w] >= 3 for w in words)
    return known / len(words) < 0.5


# ---------------------------------------------------------------- 输出


def run_in_heading(para: str, chapter: Heading, state: Dict[str, int]) -> Optional[Tuple[str, str]]:
    """段首小标题（如「Decline of the Moslem.—But while…」）与印刷目录的小节列表对照确认。"""
    if not chapter.topics:
        return None
    joined = " " + " ".join(norm(t) for t in chapter.topics) + " "
    for m in re.finditer(r"[.!?][\"”]?(?=\s|—|-|$)", para[:120]):
        head = para[: m.start() + 1].strip()
        key = norm(head)
        if not key or (len(key) < 6 and " " not in key) or len(key.split()) > 14:
            continue
        if not re.match(r"^[\"“]?[A-Z]", head):
            return None
        pos = joined.find(" " + key + " ", max(0, state.get("pos", 0) - 60))
        if pos >= 0:
            state["pos"] = pos + len(key)
            rest = para[m.end() :].lstrip(" —-")
            title = re.sub(r'\.([\"”])$', r"\1", para[: m.end()].strip()).rstrip(".!?").strip()
            title = re.sub(r'"\s*([^"]*?)\s*"', r'"\1"', title)
            if title.count('"') % 2:
                title = title.strip('" ')
            return title, rest
    return None


def render(items: List[Item], counts: Counter, ocr: bool, meta: Dict[str, str]) -> str:
    out: List[str] = []
    if meta.get("title") or meta.get("author"):
        out.append("---")
        for k in ("title", "author"):
            if meta.get(k):
                out.append(f"{k}: {meta[k]}")
        out.append("---")
        out.append("")

    chapter: Optional[Heading] = None
    state: Dict[str, int] = {}
    para: List[str] = []
    skipping_level: Optional[int] = None

    pending_notes: List[str] = []
    note_count = 0

    def attach_notes(text: str) -> Tuple[str, List[str]]:
        """把延后的脚注接到段落后：正文里找得到角标就转成 Markdown 脚注，否则作为引用块。"""
        nonlocal note_count
        defs = []
        for raw in pending_notes:
            note = join_lines(raw.split("\n"), counts)
            if ocr:
                note = clean_ocr_text(note)
            marker, _, body = note.partition(" ")
            ref = footnote_ref(marker)
            if text and ref.search(text):
                note_count += 1
                text = ref.sub(f"[^{note_count}]", text, count=1)
                defs.extend([f"[^{note_count}]: {body}", ""])
            else:
                defs.extend([f"> {note}", ""])
        pending_notes.clear()
        return text, defs

    def place_note(raw: str) -> None:
        marker = raw.split(None, 1)[0]
        ref = footnote_ref(marker)
        if not any(ref.search(p) for p in para):
            for i in range(len(out) - 1, max(len(out) - 60, -1), -1):
                if out[i] and not out[i].startswith(("#", "[^", ">")) and ref.search(out[i]):
                    pending_notes.append(raw)
                    out[i], defs = attach_notes(out[i])
                    out[i + 1 : i + 1] = [""] + defs[:-1]
                    return
        pending_notes.append(raw)

    def flush():
        nonlocal para
        if not para:
            return
        text = join_lines(para, counts)
        para = []
        if ocr:
            text = clean_ocr_text(text)
        if chapter is not None:
            found = run_in_heading(text, chapter, state)
            if found:
                title, text = found
                level = min(chapter.level + 1, MAX_LEVEL)
                out.extend([f"{'#' * level} {title}", ""])
        defs: List[str] = []
        if pending_notes:
            text, defs = attach_notes(text)
        if text:
            out.extend([text, ""])
        out.extend(defs)

    for item in items:
        if item.kind == "heading":
            flush()
            if pending_notes:
                out.extend(attach_notes("")[1])
            h = item.heading
            if skipping_level is not None and h.level > skipping_level:
                continue
            skipping_level = None
            if norm(h.title) in SKIP_SECTION_TITLES:
                skipping_level = h.level
                continue
            title = h.title
            if h.label:
                title = f"{h.label}: {title}" if title else h.label
            out.extend([f"{'#' * min(h.level, MAX_LEVEL)} {title}", ""])
            if h.subtitle:
                out.extend([f"*{h.subtitle}*", ""])
            chapter = h
            state = {}
            continue
        if skipping_level is not None:
            continue
        if item.kind == "footnote":
            place_note(item.text)
            continue
        if item.kind == "line":
            if item.new_para:
                flush()
            para.append(item.text)
    flush()
    if pending_notes:
        out.extend(attach_notes("")[1])
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- 主流程


def find_back_matter(pages: List[Page], after: int) -> int:
    for p in pages[after + 1 :]:
        tops = [norm(l.text) for l in sorted(p.lines, key=lambda l: l.cy)[:3]]
        if any(t in SKIP_SECTION_TITLES for t in tops):
            return p.index
    return len(pages)


def choose_structure(analysis: Analysis, doc, pages: List[Page], requested: str):
    """返回 (策略名, 标题列表, 说明)。"""
    tried = []
    order = [requested] if requested != "auto" else (
        ["outline", "contents", "fonts", "pattern"]
        if analysis.kind == "digital"
        # Google Books 的 OCR 层字号与版面无关；ocrmypdf 的字号按行高推算，还能用
        else ["contents", "pattern"] if analysis.google_books
        else ["contents", "fonts", "pattern"]
    )
    for strategy in order:
        if strategy == "outline":
            hs = outline_headings(doc)
            if len(hs) >= 3 or requested == "outline":
                return "outline", hs, f"PDF 书签（{len(hs)} 条）"
            tried.append("书签不足 3 条")
        elif strategy == "contents":
            cps = find_contents_pages(pages)
            hs = parse_contents(pages, cps) if cps else []
            if len(hs) >= 3 or requested == "contents":
                if cps:
                    locate_chapters(pages, hs, cps[-1] + 1)
                hs = [h for h in hs if h.page is not None]
                topics = sum(len(h.topics) for h in hs)
                where = f"PDF 第 {cps[0] + 1}–{cps[-1] + 1} 页" if cps else "未找到"
                return "contents", hs, f"书前目录（{where}；{len(hs)} 章，{topics} 个小节条目）"
            tried.append("未找到可解析的书前目录")
        elif strategy == "fonts":
            levels = FontLevels(pages, ocr=analysis.kind != "digital")
            if levels or requested == "fonts":
                return "fonts", [], f"字号与字形（{len(levels)} 种标题样式）"
            tried.append("没有区别于正文的标题样式")
        elif strategy == "pattern":
            hs = pattern_headings(pages)
            if hs or requested == "pattern":
                return "pattern", hs, f"正文中的 CHAPTER / PART 标记（{len(hs)} 章）"
            tried.append("正文中没有 CHAPTER / PART 标记")
    raise SystemExit("错误: 无法确定章节结构（" + "；".join(tried) + "）")


GOOGLE_NOTICE_RE = re.compile(r"(?i)digitized\s+by\s+google|books\.google\.com|google\s+book\s+search")

JUNK_META_RE = re.compile(
    r"(?i)^(untitled|unknown|user|admin(istrator)?|owner|author|python-docx|microsoft (office )?(word|user)"
    r"|.*\.(docx?|pdf|indd|tex|rtf|odt|qxd))$"
)


def useful_meta(value: str) -> str:
    """丢弃生成工具写入的无意义元数据（作者 python-docx、标题为文件名等）。"""
    value = (value or "").strip()
    return "" if not value or JUNK_META_RE.match(value) else value


def convert(pdf_path: Path, out_path: Optional[Path], args) -> None:
    doc = pymupdf.open(str(pdf_path))
    analysis = analyze(doc)
    kind_label = {"digital": "原生文字 PDF", "ocr": "扫描件（已有 OCR 文字层）", "scanned": "扫描件（无文字层）"}
    print(f"类型: {kind_label[analysis.kind]}{'，Google Books' if analysis.google_books else ''}；{analysis.pages} 页", file=sys.stderr)
    for n in analysis.notes:
        print(f"  {n}", file=sys.stderr)

    if analysis.kind == "scanned" or args.force_ocr:
        if args.analyze:
            return
        doc = pymupdf.open(str(run_ocr(pdf_path, args.ocr_lang, force=args.force_ocr)))
        analysis.kind = "ocr"

    pages = extract_pages(doc, analysis.kind)
    global LATIN_SCRIPT
    letters = Counter(
        "latin" if c.isascii() else "other" for p in pages for l in p.lines for c in l.text if c.isalpha()
    )
    LATIN_SCRIPT = letters["latin"] >= 0.9 * max(sum(letters.values()), 1)
    strategy, headings, desc = choose_structure(analysis, doc, pages, args.structure)
    print(f"结构来源: {desc}", file=sys.stderr)
    if args.analyze:
        for h in headings[:60]:
            label = f"{h.label}: " if h.label else ""
            print(f"  {'  ' * (h.level - 1)}{label}{h.title}  [PDF p.{(h.page or 0) + 1}，{len(h.topics)} 小节]", file=sys.stderr)
        return

    ocr = analysis.kind == "ocr"
    counts = word_counts(pages)
    repeated = repeated_margin_texts(pages) if not ocr else set()
    by_page: Dict[int, List[Heading]] = {}
    for h in headings:
        by_page.setdefault(h.page, []).append(h)

    if strategy in {"contents", "pattern"} and headings:
        start = min(h.page for h in headings)
        end = find_back_matter(pages, max(h.page for h in headings))
    else:
        start, end = 0, len(pages)

    font_levels = FontLevels(pages, ocr=ocr) if strategy == "fonts" else None

    dense = [p for p in pages if len(p.lines) >= 15]
    doc_pitch = median(line_pitch(sorted(p.lines, key=lambda l: l.cy)) for p in dense) if dense else None
    heights = [l.height for p in dense for l in p.lines]
    body_height = median(heights) if heights else 10.0

    builder = Builder(ocr)
    dropped = 0
    for page in pages[start:end]:
        lines = strip_header_footer(page, doc_pitch)
        if ocr:
            lines = drop_figure_text(page, lines, body_height)
        if repeated:
            lines = [l for l in lines if re.sub(r"\d+", "#", norm(l.text)) not in repeated]
        if ocr and is_garbage_page(lines, counts):
            dropped += 1
            continue
        if GOOGLE_NOTICE_RE.search(" ".join(l.text for l in lines[:12])):
            continue
        lines, notes = split_footnotes(page, lines, body_height, doc_pitch or line_pitch(lines))
        for h in by_page.get(page.index, []):
            if strategy in {"contents", "pattern"}:
                lines, body_title, body_sub = strip_chapter_lines(lines, h)
                if body_title and similar(body_title, h.title) > 0.6:
                    h.title = body_title
                if body_sub and not h.subtitle:
                    h.subtitle = clean_ocr_text(body_sub)
                builder.heading(h)
            elif strategy == "outline":
                lines = place_outline_heading(builder, lines, h)
        if strategy == "fonts":
            emit_font_headings(builder, lines, font_levels)
        else:
            builder.page_lines(lines, gap_breaks=not ocr)
        for note in notes:
            builder.footnote(note)
    if dropped:
        print(f"  跳过插图/乱码页 {dropped} 页", file=sys.stderr)

    meta = {k: useful_meta((doc.metadata or {}).get(k, "")) for k in ("title", "author")}
    md = render(builder.items, counts, ocr, meta)
    out_path = out_path or pdf_path.with_suffix(".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    n_heads = sum(1 for ln in md.splitlines() if ln.startswith("#"))
    print(f"完成: {out_path}（{n_heads} 个标题，{len(md):,} 字符）", file=sys.stderr)


def place_outline_heading(builder: Builder, lines: List[Line], h: Heading) -> List[Line]:
    """书签标题：在该页找到对应的行（允许跨两行），先输出该行之前的正文，再输出标题。"""
    best, best_score, span = None, 0.0, 1
    for i in range(len(lines)):
        for k in (1, 2):
            if i + k > len(lines):
                continue
            text = " ".join(l.text for l in lines[i : i + k])
            score = similar(text, h.title)
            if score > best_score:
                best, best_score, span = i, score, k
    if best is not None and best_score >= 0.8:
        builder.page_lines(lines[:best], gap_breaks=True)
        builder.heading(h)
        return lines[best + span :]
    builder.heading(h)
    return lines


def emit_font_headings(builder: Builder, lines: List[Line], font_levels: FontLevels) -> None:
    """按标题样式切分本页：连续同样式的行合并为一个（跨行的）标题。"""
    gap_breaks = not builder.ocr
    chunk: List[Line] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        level = font_levels.level(line)
        if level is None:
            chunk.append(line)
            i += 1
            continue
        title = line.text.strip()
        i += 1
        while i < len(lines) and font_levels.level(lines[i]) == level and font_levels.key(lines[i]) == font_levels.key(line):
            title += " " + lines[i].text.strip()
            i += 1
        builder.page_lines(chunk, gap_breaks=gap_breaks)
        chunk = []
        if builder.ocr:
            title = clean_ocr_text(title)
        builder.heading(Heading(level=level, title=title))
    builder.page_lines(chunk, gap_breaks=gap_breaks)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_to_md",
        description="将书籍 PDF 转为 Markdown：自动判断 PDF 类型并选择章节结构来源，标题最多 4 级。",
    )
    p.add_argument("input", type=Path, help="输入 PDF")
    p.add_argument("-o", "--output", type=Path, default=None, help="输出 .md（默认与输入同目录同名）")
    p.add_argument(
        "--structure",
        choices=["auto", "outline", "contents", "fonts", "pattern"],
        default="auto",
        help="章节结构来源（默认 auto：按 PDF 类型自动选择）",
    )
    p.add_argument("--analyze", action="store_true", help="只输出类型判断与识别到的章节，不转换")
    p.add_argument("--force-ocr", action="store_true", help="忽略现有文字层，用 ocrmypdf 重新 OCR")
    p.add_argument("--ocr-lang", default="eng", help="OCR 语言（tesseract 语言代码，如 eng、chi_sim+eng）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if pymupdf is None:
        print("错误: 缺少 Python 依赖 PyMuPDF。请在 miscTools 根目录执行: ./install.sh", file=sys.stderr)
        return 1
    pdf_path = args.input.expanduser().resolve()
    if not pdf_path.is_file():
        print(f"错误: 找不到文件: {pdf_path}", file=sys.stderr)
        return 1
    out = args.output.expanduser().resolve() if args.output else None
    convert(pdf_path, out, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
