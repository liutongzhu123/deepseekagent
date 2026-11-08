"""Deterministic standard parser for the P0 standard-parsing increment.

Scope (P0 Sprint 2, first slice):
  * digital PDF standards with a "技术要求" section and a "试验方法" section;
  * an item/requirements/method table (like EPB table 3) when present is used
    as the authoritative item -> method mapping;
  * otherwise the method-section leaf clauses are matched to the
    requirements-section headings by Chinese name similarity;
  * headings may be pure Chinese and use a near-synonym section name; a
    standard with only one of the two sections yields review-only candidate
    items instead of zero results;
  * every item keeps its clause numbers and source pages so a reviewer can
    trace it back to the PDF.

It deliberately does not call an LLM: confidential standards stay local and
the result is reproducible.  Name/field corrections belong to the review API.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from typing import Iterable

from .standard_extract.classify import apply_classification
from .standard_extract.conditions import apply_conditions
from .standard_extract.formula import apply_formulas
from .standard_extract.items_list import apply as apply_items_list
from .standard_extract.limits import apply_limits
from .standard_extract.rounding import apply_rounding
from .standard_extract.samples import apply_samples
from .standard_extract.table_mapping import apply_table_mapping
from .standard_extract.tables import apply_tables

SCHEMA_VERSION = "standard-v0.1"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
HEAD_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s*([\u4e00-\u9fffA-Za-z].*)$")
PAGE_NO_RE = re.compile(r"^(?:[IVXLC]{1,6}|\d{1,3})$")
WATERMARK_RE = re.compile(r"^(?:TO|OF|BY|AT)\s*:")
DATE_RE = re.compile(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})")
GB_REF_RE = re.compile(r"GB/T\s*(\d+(?:\.\d+)?)(?!\s*[-—]\s*\d{4})")

# Verified 2026-08-31 in the materials' standard-parsing skill.  Used to
# annotate references that omit their year; the quoted original text is never
# rewritten, only reported separately.
KNOWN_GB_REFS = {
    "GB/T 231.1": "GB/T 231.1-2018",
    "GB/T 1348": "GB/T 1348-2019",
    "GB/T 9441": "GB/T 9441-2021",
    "GB/T 13384": "GB/T 13384-2008",
    "GB/T 30038": "GB/T 30038-2013",
}

_NOISE_LINES = {
    "试验项目",
    "技术要求",
    "试验方法",
    "出厂检验",
    "型式试验",
    "Test items",
    "Technical requirement",
    "Test method",
    "Delivery Inspection",
    "Type Test",
}


@dataclass
class _Line:
    text: str
    page: int
    idx: int


@dataclass
class _Heading:
    num: str
    text: str
    page: int
    line_idx: int
    name: str = ""


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _chinese_name(text: str) -> str:
    """Take the Chinese part of a bilingual heading, keeping inner Latin terms."""
    last = -1
    for i, ch in enumerate(text):
        if "\u4e00" <= ch <= "\u9fff":
            last = i
    if last < 0:
        return text.strip()
    end = last + 1
    while end < len(text) and text[end] in "）】」":
        end += 1
    return text[:end].strip()


def _normalise_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。；：、（）(])", "", text)
    text = re.sub(r"(?<=[，。；：、）)])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+([，。；：、）])", r"\1", text)
    text = re.sub(r"([（])\s+", r"\1", text)
    return text.strip()


def _clean_lines(
    pages: list[str], standard_no: str, keep_method_label: bool = False
) -> list[_Line]:
    no_pattern = re.escape(standard_no).replace(r"\ ", r"\s*")
    out: list[_Line] = []
    idx = 0
    for page_no, page_text in enumerate(pages, start=1):
        for raw in page_text.splitlines():
            text = raw.strip()
            if not text:
                continue
            if WATERMARK_RE.match(text):
                continue
            if standard_no and re.fullmatch(no_pattern, re.sub(r"\s+", " ", text)):
                continue
            if PAGE_NO_RE.fullmatch(text):
                continue
            if text in _NOISE_LINES:
                # OCR often puts a subheading label on its own line
                # (``5.5.9.2`` / ``试验方法``); the label must survive.
                if not (keep_method_label and text == "试验方法"):
                    continue
            out.append(_Line(text=text, page=page_no, idx=idx))
            idx += 1
    return out


def _detect_headings(lines: list[_Line]) -> list[_Heading]:
    headings: list[_Heading] = []
    for line in lines:
        match = HEAD_RE.match(line.text)
        if not match:
            continue
        num, rest = match.group(1), match.group(2).strip()
        if "...." in rest:
            continue
        if not _has_cjk(rest):
            continue
        if re.search(r"[，。；：,;:]", rest):
            continue
        if len(rest) > 120:
            continue
        if re.match(r"^\d", rest):
            continue
        if not re.search(r"[A-Za-z]{2,}", rest):
            # Scanned Chinese standards usually have no English subtitle.
            # Accept a pure-Chinese heading only when it is short and
            # label-like; otherwise a wrapped body line that happens to start
            # with a clause number would be promoted to a heading.
            if len(rest) < 2 or len(rest) > 30:
                continue
        headings.append(
            _Heading(num=num, text=rest, page=line.page, line_idx=line.idx)
        )
    for heading in headings:
        heading.name = _chinese_name(heading.text)
    return headings


_SECTION_ALIASES = {
    "技术要求": ("技术要求", "技术条件", "性能要求", "要求"),
    "试验方法": ("试验方法", "试验方法及要求", "检验方法", "测试方法"),
}


def _section_number(
    headings: list[_Heading], keyword: str, aliases: list[str] | tuple[str, ...] | None = None
) -> str | None:
    """Find a top-level section by name, accepting common near-synonyms.

    Scanned enterprise standards often omit the English subtitle and use a
    variant such as "技术条件" or "试验方法及要求"; only top-level headings are
    considered so a clause like "5.3 试验要求" cannot become the section.
    Administrators can override the alias list from the rule-config page.
    """
    alias_list = tuple(aliases) if aliases else _SECTION_ALIASES.get(keyword, (keyword,))
    for alias in alias_list:
        for heading in headings:
            if heading.num.count(".") != 0:
                continue
            if alias == "要求":
                # A bare "要求" is its own section; substring matching would
                # wrongly select "4 一般要求"/"4 通用要求".
                if heading.name.strip() != alias:
                    continue
            elif alias not in heading.name:
                continue
            return heading.num
    return None


def _is_leaf(heading: _Heading, section: list[_Heading]) -> bool:
    return not any(
        other is not heading and other.num.startswith(heading.num + ".")
        for other in section
    )


def _body_lines(
    lines: list[_Line], heading: _Heading, next_line_idx: int
) -> list[str]:
    body: list[str] = []
    for line in lines[heading.line_idx + 1 : next_line_idx]:
        if not _has_cjk(line.text):
            continue
        if re.fullmatch(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ\s√\-]+", line.text):
            continue
        body.append(line.text)
    return body


def _paragraphs(lines: list[_Line], heading: _Heading, next_line_idx: int) -> list[str]:
    paras: list[str] = []
    current = ""
    for raw in _body_lines(lines, heading, next_line_idx):
        text = re.sub(r"^\d+(?:\.\d+){1,4}\s*", "", raw).strip()
        if not text:
            continue
        if current:
            # A wrapped line continues the same sentence; keep Latin word
            # boundaries but never insert spaces between Chinese characters.
            if re.search(r"[A-Za-z0-9]$", current) and re.match(
                r"^[A-Za-z0-9]", text
            ):
                current += " "
            current += text
        else:
            current = text
        if re.search(r"[。；;]$", text):
            paras.append(current)
            current = ""
    if current:
        paras.append(current)
    return paras


def _text_for(
    lines: list[_Line], heading: _Heading, next_line_idx: int
) -> str:
    paras = [
        p.strip("；;。 ")
        for p in _paragraphs(lines, heading, next_line_idx)
        if p.strip("；;。 ")
    ]
    text = "；".join(paras)
    text = re.sub(r"[；;]{2,}", "；", text)
    return _normalise_text(text)


def _extract_pairs(
    lines: list[_Line], tech_sec: str, method_sec: str
) -> list[tuple[str, str]]:
    tech = re.escape(tech_sec)
    method = re.escape(method_sec)
    pattern = re.compile(
        rf"(?<![\d.])({tech}\.\d+(?:\.\d+)*)\s+({method}\.\d+(?:\.\d+)*)(?![\d.])"
    )
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        for match in pattern.finditer(line.text):
            tech_no, method_no = match.group(1), match.group(2)
            if tech_no in seen:
                continue
            seen.add(tech_no)
            pairs.append((tech_no, method_no))
    return pairs


def _base_name(tech: _Heading, method: _Heading) -> str:
    tech_name = _chinese_name(tech.text)
    method_name = re.sub(r"(试验|测试)$", "", _chinese_name(method.text))
    if tech_name == method_name:
        return tech_name
    if tech_name in method_name:
        return method_name
    if method_name in tech_name:
        return tech_name
    return tech_name


def _match_by_name(
    tech_section: list[_Heading], method_leaves: list[_Heading]
) -> dict[str, _Heading]:
    """One-to-one best-name matching used when no item table is present."""
    candidates = [h for h in tech_section if h.num.count(".") >= 1]
    scored: list[tuple[float, str, str]] = []
    for method in method_leaves:
        m_name = _chinese_name(method.text)
        for tech in candidates:
            score = SequenceMatcher(None, _chinese_name(tech.text), m_name).ratio()
            if score >= 0.55:
                scored.append((score, tech.num, method.num))
    scored.sort(reverse=True)
    result: dict[str, _Heading] = {}
    used_tech: set[str] = set()
    by_num = {h.num: h for h in tech_section}
    for _, tech_num, method_num in scored:
        if method_num in result or tech_num in used_tech:
            continue
        result[method_num] = by_num[tech_num]
        used_tech.add(tech_num)
    return result


def _item_name(tech: _Heading, method: _Heading, tech_by_num: dict[str, _Heading], tech_sec: str) -> tuple[str, str]:
    name = _base_name(tech, method)
    group = ""
    parent_num = tech.num.rsplit(".", 1)[0]
    parent = tech_by_num.get(parent_num)
    depth = tech.num.count(".")
    if parent is not None and depth >= 3 and parent.num != tech_sec:
        parent_name = re.sub(r"（.*?）", "", _chinese_name(parent.text)).strip()
        if parent_name and not parent_name.endswith("性能"):
            group = parent_name
            name = f"{parent_name}-{name}"
    return name, group


# OCR sometimes loses a chapter number but keeps the chapter title on its own
# line.  Such a line still marks the end of the previous section.
_SECTION_BOUNDARY_TITLES = {
    "范围",
    "规范性引用文件",
    "术语",
    "术语和定义",
    "试验原理",
    "试验设备",
    "试验方法",
    "结果表示",
    "试验报告",
    "技术要求",
    "检验规则",
    "标志、包装、运输和贮存",
    "附录",
}


def _is_section_boundary(text: str) -> bool:
    return re.sub(r"\s+", "", text) in _SECTION_BOUNDARY_TITLES


def _section_text(
    lines: list[_Line], headings: list[_Heading], heading: _Heading
) -> str:
    """Text of one heading including all of its nested subclauses."""
    depth = heading.num.count(".")
    next_idx = next(
        (
            other.line_idx
            for other in headings
            if other.line_idx > heading.line_idx and other.num.count(".") <= depth
        ),
        len(lines),
    )
    for line in lines[heading.line_idx + 1 :]:
        if line.idx >= next_idx:
            break
        if _is_section_boundary(line.text):
            next_idx = line.idx
            break
    return _text_for(lines, heading, next_idx)


def _parse_single_section_items(
    lines: list[_Line],
    headings: list[_Heading],
    section_num: str,
    kind: str,
) -> list[dict]:
    """Build review-only items when only one of the two sections exists.

    A test-method-only standard (a determination method, for example) has no
    "技术要求" section at all.  Instead of returning zero items, expose the
    method section's second-level clauses as candidate items with an empty
    requirement field; the review workbench fills in the business content.
    """
    section = [h for h in headings if h.num.split(".")[0] == section_num]
    candidates = [
        h for h in section if h.num != section_num and h.num.count(".") == 1
    ]
    leaves = [
        h for h in section if h.num != section_num and _is_leaf(h, section)
    ]
    if not candidates:
        candidates = leaves
    elif len(leaves) >= 10 and len(leaves) >= 3 * len(candidates):
        # Level-1 headings are grouping containers (5.1 一般条件 / 5.2 功能检测
        # / 5.3 环境试验); the real test clauses are the leaves.
        candidates = leaves
    label = "技术要求" if kind == "requirement" else "试验方法"
    populated = "requirement" if kind == "requirement" else "method"
    missing = "method" if kind == "requirement" else "requirement"
    items: list[dict] = []
    for seq, heading in enumerate(candidates, start=1):
        text = _section_text(lines, headings, heading)
        name = _chinese_name(heading.text).strip() or heading.num
        requirement = text if kind == "requirement" else ""
        method = text if kind == "method" else ""
        review_fields = [
            {
                "field": missing,
                "confidence": 0.0,
                "reason": (
                    "未识别到「试验方法」章节，需人工补充试验方法"
                    if kind == "requirement"
                    else "未识别到「技术要求」章节，需人工补充判定要求"
                ),
            },
            {
                "field": "name",
                "confidence": 0.6,
                "reason": f"项目名称取自「{label}」章节标题，需人工确认",
            },
        ]
        if not text:
            review_fields.append(
                {
                    "field": populated,
                    "confidence": 0.0,
                    "reason": f"「{label}」章节未提取到正文，需对照原 PDF 复核",
                }
            )
        items.append(
            {
                "seq": seq,
                "name": name,
                "name_auto": name,
                "group": label,
                "clause": heading.num,
                "requirement": requirement,
                "method_clause": heading.num if kind == "method" else "",
                "method": method,
                "page": heading.page,
                "method_page": heading.page,
                "confidence": 0.5,
                "review_required": True,
                "review_fields": review_fields,
                "reviewed": False,
                "references": _find_references(requirement, method),
            }
        )
    return items


_CHAPTER_TECH_NAMES = ("技术要求", "技术条件", "性能要求")
_CHAPTER_METHOD_NAMES = ("试验方法", "检验方法", "测试方法")


def _parse_chapter_items(
    lines: list[_Line], headings: list[_Heading]
) -> list[dict]:
    """Extract one item per chapter that has x.1 技术要求 / x.2 试验方法."""
    items: list[dict] = []
    seen: set[str] = set()
    for chapter in headings:
        if chapter.num.count(".") != 0 or chapter.num in {"1", "2", "3"}:
            continue
        if chapter.num in seen:
            # Table rows and figure legends repeat a chapter number.
            continue
        name = chapter.name.strip()
        if not name or re.match(r"^[A-Za-z0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]", name):
            continue
        if re.search(r"\d", re.sub(r"[（(][^）)]*[）)]", "", name)):
            # A bare numeric token outside the item code means a data row or
            # a figure legend, not a chapter heading.
            continue
        seen.add(chapter.num)
        children = [
            heading
            for heading in headings
            if heading.num.count(".") == 1
            and heading.num.split(".")[0] == chapter.num
        ]
        tech = next(
            (
                heading
                for heading in children
                if any(name in heading.name for name in _CHAPTER_TECH_NAMES)
            ),
            None,
        )
        method = next(
            (
                heading
                for heading in children
                if any(name in heading.name for name in _CHAPTER_METHOD_NAMES)
            ),
            None,
        )
        if tech is None or method is None or tech is method:
            continue
        requirement = _section_text(lines, headings, tech)
        method_text = _section_text(lines, headings, method)
        if not requirement and not method_text:
            continue
        name = _chinese_name(chapter.text).strip() or chapter.num
        items.append(
            {
                "seq": len(items) + 1,
                "name": name,
                "name_auto": name,
                "group": "",
                "clause": tech.num,
                "requirement": requirement,
                "method_clause": method.num,
                "method": method_text,
                "page": tech.page,
                "method_page": method.page,
                "confidence": 0.75,
                "review_required": True,
                "review_fields": [],
                "reviewed": False,
                "references": _find_references(requirement, method_text),
            }
        )
    return items


def _find_references(requirement: str, method: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for text in (requirement, method):
        for match in GB_REF_RE.finditer(text):
            ref = f"GB/T {match.group(1)}"
            if ref in seen:
                continue
            seen.add(ref)
            current = KNOWN_GB_REFS.get(ref)
            if current:
                refs.append({"ref": ref, "current": current})
    return refs


def _extract_meta(pages: list[str], file_name: str) -> dict:
    head = "\n".join(pages[:2])
    candidates = re.findall(
        r"(?<![A-Za-z0-9])((?:Q/[A-Z]{2,6}|QMAT|QSQR|QJD|SQR|VW)\s*[A-Z]{0,4}\s*\d+(?:[A-Z]\d*)?(?:-\d{2,4})*(?:-\d{4})?)",
        head,
    )
    counts: dict[str, int] = {}
    for candidate in candidates:
        key = re.sub(r"\s+", " ", candidate.strip())
        counts[key] = counts.get(key, 0) + 1
    standard_no = max(counts, key=counts.get) if counts else ""

    boilerplate = (
        "企业标准",
        "有限公司",
        "发布",
        "实施",
        "代替",
        "秘密",
        "Confidential",
        "Technical",
        "Issue",
        "Implementation",
        "Standard",
        "内部资料",
        "注意保存",
        "出版",
        "印发",
    )
    name = ""
    for raw in head.splitlines():
        text = raw.strip()
        if not (4 <= len(text) <= 60):
            continue
        if not _has_cjk(text):
            continue
        if any(word in text for word in boilerplate):
            continue
        if re.search(r"\d{4}\s*[-年/.]", text):
            continue
        name = _chinese_name(text)
        break

    dates = DATE_RE.findall(head)
    issue_date = "-".join(dates[0]) if dates else ""
    implementation_date = "-".join(dates[1]) if len(dates) > 1 else ""
    confidential = "秘密" in head or "Confidential" in head
    meta = {
        "standard_no": standard_no,
        "name": name or re.sub(r"\.pdf$", "", file_name, flags=re.I),
        "issue_date": issue_date,
        "implementation_date": implementation_date,
        "confidential": confidential,
        "pages": len(pages),
        "file_name": file_name,
    }
    apply_classification(meta)
    return meta


def _clean_table_text(value: str) -> str:
    """Remove PDF watermark fragments from a table cell, keep values."""
    text = (value or "").replace("\n", "/")
    text = re.sub(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", "", text)
    text = re.sub(r"/+", "；", text)
    text = re.sub(r"；+", "；", text)
    text = re.sub(r"\s+", "", text)
    return text.strip("； ")


def _readable_requirement(values: list[str]) -> str:
    """Keep table values that are meaningful for review/Q&A."""
    kept: list[str] = []
    for raw in values:
        for value in str(raw).split("；"):
            token = value.strip("；; ")
            token = re.sub(r"([≥≤＜＞<>])\s*\.\s*", r"\1", token)
            token = token.strip("；;，,。：: ")
            if len(token) < 2:
                continue
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", token))
            has_value = bool(re.search(r"[≥≤＜＞<>]?\s*\d", token))
            if not has_cjk and re.search(r"[A-Za-z]{2,}", token) and not has_value:
                continue
            if not has_cjk and not has_value:
                continue
            if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", token)) < 2:
                continue
            kept.append(token)
    return "；".join(dict.fromkeys(kept))


def _table_item_name(raw: str, method_no: str) -> str:
    text = (raw or "").replace("\n", "/")
    text = re.sub(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", "", text)
    text = text.replace("/", "")
    text = re.split(r"[,，]", text)[0]
    text = re.split(r"[A-Za-z]{2,}", text)[0]
    text = re.sub(r"[（(][^）)]*(?:针对|For|for|仅适用)[^）)]*[）)]", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^[0-9]+[:：]?", "", text)
    text = re.sub(r"^(?:驰|瑞|达|丰|津|天|股|份|公|司|有|限|@|：|:)+", "", text)
    text = re.sub(r"(?:驰|瑞|达|丰|津|天|股|份|公|司|有|限|@|：|:)+$", "", text)
    text = text.strip("：:；;（）() ")
    text = re.sub(r"[（(](\d{2})℃$", r"（\1℃）", text)
    text = re.sub(r"(?:℃|%)$", "", text)
    if text.endswith("要求") and len(text) > 3:
        text = text[: -len("要求")]
    if text.startswith("后收缩率"):
        return "收缩率"
    if text in ("透光率", "雾度"):
        return "透光率和雾度"
    if text.startswith("悬臂梁无缺口冲击强度"):
        return "悬臂梁冲击强度（23℃）"
    if text.startswith("悬臂梁缺口冲击强度"):
        if "-30" in raw or "－30" in raw:
            return "悬臂梁缺口冲击强度（-30℃）"
        return "悬臂梁缺口冲击强度（23℃）"
    if text in ("拉伸强度", "屈服伸长率"):
        return "拉伸强度/屈服伸长率"
    if text in ("弯曲强度", "弯曲模量"):
        return "弯曲强度/弯曲模量"
    if text == "燃烧性能":
        return "燃烧特性"
    return text or ""


def _method_clause_from_row(row: list[str]) -> str:
    for cell in reversed(row):
        match = re.fullmatch(r"\s*(6\.\d+(?:\.\d+)?)\s*", (cell or "").replace("\n", ""))
        if match:
            return match.group(1)
    return ""


def _parse_table_items(
    pdf_bytes: bytes,
    lines: list[_Line],
    headings: list[_Heading],
    method_sec: str,
) -> list[dict]:
    """Extract items from a standard's performance-requirement tables.

    Some PP/PE material standards keep the item list inside
    multi-page merged-cell tables instead of a clause/heading mapping.  This
    fallback uses pdfplumber's table geometry, cleans PDF watermark fragments,
    and maps each row to a method clause (or to the closest method heading
    when the merged cell leaves the method blank).
    """
    try:  # optional dependency: keep the text-only parser usable without it
        import pdfplumber
    except ImportError:  # pragma: no cover - exercised only without the extra
        return []

    method_section = [h for h in headings if h.num.split(".")[0] == method_sec]
    method_leaves = [h for h in method_section if _is_leaf(h, method_section)]
    by_num = {h.num: h for h in method_leaves}
    heading_lines = sorted(h.line_idx for h in headings)

    def method_text(heading: _Heading) -> str:
        next_idx = next(
            (idx for idx in heading_lines if idx > heading.line_idx), len(lines)
        )
        return _text_for(lines, heading, next_idx)

    def closest_method(name: str) -> _Heading | None:
        best: tuple[float, _Heading] | None = None
        for heading in method_leaves:
            ratio = SequenceMatcher(None, name, _chinese_name(heading.text)).ratio()
            if ratio >= 0.45 and (best is None or ratio > best[0]):
                best = (ratio, heading)
        return best[1] if best else None

    rows: list[tuple[str, str, str, int]] = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables()
            except Exception:  # noqa: BLE001 - a broken table must not kill the batch
                continue
            for table in tables:
                if not table:
                    continue
                header = " ".join(
                    (cell or "").replace("\n", "")
                    for row in table[:2]
                    for cell in row
                )
                if "试验项目" not in header or "试验方法" not in header:
                    continue
                if "出厂检验" in header or "型式检验" in header:
                    # An inspection schedule (试验项目/技术要求/试验方法/
                    # 出厂检验/型式检验) has a different column layout from the
                    # merged-cell performance tables this fallback targets.
                    continue
                for row in table[2:] if len(table) > 2 else table[1:]:
                    if not row:
                        continue
                    raw_name = row[0] or ""
                    if not _has_cjk(raw_name):
                        continue
                    method_no = _method_clause_from_row(row)
                    name = _table_item_name(raw_name, method_no)
                    if not name or len(name) > 40 or name in {"备注"}:
                        continue
                    values: list[str] = []
                    for cell in row[1:]:
                        cleaned = _clean_table_text(cell or "")
                        if not cleaned or re.fullmatch(r"6\.\d+(?:\.\d+)?", cleaned):
                            continue
                        if cleaned not in values:
                            values.append(cleaned)
                    requirement = _readable_requirement(values)
                    rows.append((name, requirement, method_no, page_no))

    merged: dict[str, dict] = {}
    for order, (name, requirement, method_no, page_no) in enumerate(rows):
        method = by_num.get(method_no) if method_no else None
        if method is None:
            method = closest_method(name)
        if method is None:
            continue
        key = name
        candidate = {
            "name": name,
            "requirement": requirement,
            "clause": method.num,  # table rows are requirements, not clauses
            "method_clause": method.num,
            "page": page_no,
            "method_page": method.page,
            "order": order,
        }
        previous = merged.get(key)
        if previous is None:
            merged[key] = candidate
        elif key in ("拉伸强度/屈服伸长率", "弯曲强度/弯曲模量"):
            # The customer's item list groups these two properties; keep the
            # first table's method/page but expose both requirement fragments.
            parts = [
                part
                for part in (previous["requirement"] + "；" + requirement).split("；")
                if part
            ]
            previous["requirement"] = "；".join(dict.fromkeys(parts))
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            tuple(int(part) for part in item["method_clause"].split(".")),
            item["order"],
        ),
    )
    items: list[dict] = []
    for seq, item in enumerate(ordered, start=1):
        method = by_num[item["method_clause"]]
        text = method_text(method)
        items.append(
            {
                "seq": seq,
                "name": item["name"],
                "name_auto": item["name"],
                "group": "材料性能",
                "clause": item["clause"],
                "requirement": item["requirement"],
                "method_clause": item["method_clause"],
                "method": text,
                "page": item["page"],
                "method_page": item["method_page"],
                "confidence": 0.7,
                "reviewed": False,
                "references": _find_references(item["requirement"], text),
            }
        )
    return items


_COMBINED_SECTION_TITLES = (
    "试验方法和性能要求",
    "试验方法和技术要求",
    "技术要求和试验方法",
    "试验方法和要求",
)
_FIELD_LABELS = (
    "试验目的",
    "试验方法",
    "试验步骤",
    "试验条件",
    "试验参数",
    "试验要求",
    "试验设备",
    "试验程序",
    "判定要求",
    "技术要求",
)
_METHOD_LABELS = ("试验方法", "试验步骤", "试验程序")
_REQUIREMENT_LABELS = ("试验要求", "判定要求", "技术要求")


def _field_label(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    for label in _FIELD_LABELS:
        if compact == label or compact.startswith(label):
            return label
    return ""


def _combined_section_start(lines: list[_Line]) -> tuple[int, str] | None:
    """Locate the body heading of a combined method/requirements chapter."""
    found: tuple[int, str] | None = None
    for index, line in enumerate(lines):
        text = line.text.strip()
        if "...." in text or "……" in text:
            continue
        for title in _COMBINED_SECTION_TITLES:
            position = text.find(title)
            if position < 0:
                continue
            number = text[:position].strip()
            if not re.fullmatch(r"\d{1,2}", number):
                continue
            found = (index, number)
            break
    return found


def _merge_split_ocr_headings(lines: list[_Line]) -> list[_Line]:
    """Join an OCR'd bare clause number with its heading on the next line."""
    merged: list[_Line] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        text = line.text.strip()
        if re.fullmatch(r"\d{1,2}(?:\.\d+){1,3}", text) and index + 1 < len(lines):
            following = lines[index + 1]
            candidate = following.text.strip()
            looks_like_name = (
                2 <= len(candidate) <= 40
                and _has_cjk(candidate)
                and not re.match(r"^\d", candidate)
                and not re.search(r"[。；;：:，,]", candidate)
            )
            if looks_like_name:
                merged.append(_Line(text=f"{text} {candidate}", page=line.page, idx=len(merged)))
                index += 2
                continue
        merged.append(_Line(text=text, page=line.page, idx=len(merged)))
        index += 1
    return merged


def _clean_ocr_section_lines(values: list[str]) -> tuple[list[str], bool]:
    """Keep prose from OCR sections; report whether table captions were seen."""
    kept: list[str] = []
    has_table = False
    for raw in values:
        text = raw.strip()
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        if not compact:
            continue
        if re.fullmatch(r"[\d\s.,，．±%≤≥<>=/()（）+\-~—:：°℃ΩmQkKgsA VmA]+", compact):
            continue
        if re.match(r"^[图表]\s*\d+", compact):
            has_table = True
            continue
        if re.fullmatch(r"Q/\s*SQR\s*SS8-46-2021", compact, flags=re.I):
            continue
        if len(compact) <= 3:
            continue
        if compact.startswith(("图例", "横坐标", "纵坐标", "单位：", "单位:")):
            continue
        if len(compact) <= 2 and not _has_cjk(compact):
            continue
        kept.append(_normalise_text(text))
    return list(dict.fromkeys(kept)), has_table


def _join_ocr_lines(values: list[str], limit: int = 1200) -> tuple[str, bool]:
    kept, _ = _clean_ocr_section_lines(values)
    text = "；".join(part.strip("；;。 ") for part in kept if part.strip("；;。 "))
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rstrip("；;，, ") + "…"
    return text, truncated


def _parse_toc_names(lines: list[_Line], section_num: str = "5") -> dict[str, str]:
    """Read item numbers from the table of contents (OCR pages 1-5)."""
    toc: dict[str, str] = {}
    pattern = re.compile(
        rf"^({re.escape(section_num)}(?:\.\d+){{2}})(?:\s*(.*))?$"
    )
    for index, line in enumerate(lines):
        if line.page > 5:
            continue
        match = pattern.match(line.text.strip())
        if not match:
            continue
        number, rest = match.group(1), (match.group(2) or "").strip()
        name = rest
        if not name:
            for candidate in lines[index + 1 : index + 3]:
                text = candidate.text.strip()
                compact = re.sub(r"\s+", "", text)
                if (
                    2 <= len(text) <= 40
                    and _has_cjk(text)
                    and not re.match(r"^\d", text)
                    and not _field_label(text)
                    and not re.search(r"[。；;：:，,]", text)
                ):
                    name = compact
                    break
        toc.setdefault(number, name.strip(" .．:："))
    return toc


def _ocr_confidence(page_confidence: list[float] | None, page: int) -> float:
    if page_confidence and 1 <= page <= len(page_confidence):
        return float(page_confidence[page - 1])
    return 0.9


def _parse_combined_items(
    lines: list[_Line],
    page_confidence: list[float] | None,
    ocr_meta: dict | None,
) -> list[dict]:
    """Parse 5 试验方法和性能要求 / 5 试验方法和技术要求 combined chapters."""
    start = _combined_section_start(lines)
    if start is None:
        return []
    start_index, section_num = start
    clause_pattern = re.compile(
        rf"^({re.escape(section_num)}(?:\.\d+){{1,4}})\s*(.*)$"
    )

    toc = _parse_toc_names(lines, section_num)
    items: list[dict] = []
    current: dict | None = None
    parent = ""
    parent_heading: _Heading | None = None
    label = "body"

    def create_item(number: str, name: str, source: str, page: int) -> dict:
        item = {
            "num": number,
            "name": name,
            "name_source": source,
            "page": page,
            "parent": parent,
            "sections": {},
            "method_clause": "",
            "method_page": None,
        }
        items.append(item)
        return item

    for line in lines[start_index + 1 :]:
        text = line.text.strip()
        if not text:
            continue
        if re.match(r"^附\s*录", text):
            break
        match = clause_pattern.match(text)
        if match:
            number, rest = match.group(1), match.group(2).strip()
            depth = len(number.split("."))
            field = _field_label(rest)
            if depth <= 2:
                if not rest:  # bare number in a table, not a heading
                    continue
                parent = _chinese_name(rest).strip() or rest
                parent_heading = _Heading(
                    num=number, text=rest, page=line.page, line_idx=line.idx
                )
                label = "body"
                current = None
                continue
            if field:
                if current is None and depth == 3 and parent_heading is not None:
                    # 5.4/5.5/5.7 place 试验目的… directly under the group;
                    # the group heading itself is the item.
                    current = create_item(
                        parent_heading.num,
                        _chinese_name(parent_heading.text).strip()
                        or parent_heading.num,
                        "heading",
                        parent_heading.page,
                    )
                label = field
                if current is not None:
                    if field in _METHOD_LABELS and not current["method_clause"]:
                        current["method_clause"] = number
                        current["method_page"] = line.page
                continue
            if depth == 3:
                if not rest and number not in toc:  # table row, not heading
                    continue
                if toc and number not in toc:
                    # A depth-3 clause outside the TOC is usually a table row
                    # or a mis-numbered subheading; keep it as body text.
                    if current is not None:
                        current["sections"].setdefault(label, []).append(text)
                    continue
                name = rest or toc.get(number, "")
                source = "heading" if rest else "toc"
                if not name:
                    name = number
                    source = "fallback"
                current = create_item(number, name, source, line.page)
                label = "body"
                continue
            if depth >= 4:
                label = "body"
                if current is not None:
                    current["sections"].setdefault("body", []).append(rest)
                continue
        if current is None:
            continue
        if not _has_cjk(text) and not re.search(r"[≥≤＜＞<>]", text):
            continue
        current["sections"].setdefault(label or "body", []).append(text)

    parsed: list[dict] = []
    for seq, item in enumerate(items, start=1):
        sections = item["sections"]
        method_values = [
            value
            for key in _METHOD_LABELS
            for value in sections.get(key, [])
        ]
        method_from_condition = False
        if not method_values and sections.get("试验条件"):
            method_values = sections["试验条件"]
            method_from_condition = True
        method_from_body = False
        if not method_values and sections.get("body"):
            method_values = sections["body"]
            method_from_body = True
        requirement_values = [
            value
            for key in _REQUIREMENT_LABELS
            for value in sections.get(key, [])
        ]
        requirement_from_body = False
        if not requirement_values and sections.get("body"):
            joined = "".join(sections["body"])
            if any(word in joined for word in ("应", "要求", "不得", "符合", "≤", "≥", "<", ">")):
                requirement_values = sections["body"]
                requirement_from_body = True

        method, method_truncated = _join_ocr_lines(method_values)
        requirement, requirement_truncated = _join_ocr_lines(requirement_values)
        _, has_table = _clean_ocr_section_lines(
            [value for values in sections.values() for value in values]
        )
        page_conf = _ocr_confidence(page_confidence, item["page"])
        method_page = item["method_page"] or item["page"]
        method_page_conf = _ocr_confidence(page_confidence, method_page)
        field_confidence = {
            "clause": round(page_conf, 3),
            "name": round(min(page_conf, 0.98 if item["name_source"] == "heading" else 0.9), 3),
            "requirement": round(page_conf if requirement else 0.0, 3),
            "method": round(method_page_conf if method else 0.0, 3),
            "method_clause": round(method_page_conf if item["method_clause"] else 0.0, 3),
        }
        review_fields: list[dict] = []
        field_values = {
            "clause": item["num"],
            "name": item["name"],
            "requirement": requirement,
            "method": method,
            "method_clause": item["method_clause"],
        }
        for field, confidence in field_confidence.items():
            if not field_values.get(field):
                review_fields.append(
                    {
                        "field": field,
                        "confidence": confidence,
                        "reason": "OCR/结构解析未提取到该字段",
                    }
                )
            elif confidence < 0.9:
                review_fields.append(
                    {
                        "field": field,
                        "confidence": confidence,
                        "reason": "OCR 置信度低于 0.90",
                    }
                )
            elif field in ("requirement", "method") and re.search(r"\d", field_values[field]):
                if page_conf < 0.95:
                    review_fields.append(
                        {
                            "field": field,
                            "confidence": confidence,
                            "reason": "字段含数值且所在页 OCR 置信度低于 0.95",
                        }
                    )
        if item["name_source"] != "heading":
            review_fields.append(
                {
                    "field": "name",
                    "confidence": field_confidence["name"],
                    "reason": "正文项目标题未完整识别，名称取自目录或编号",
                }
            )
        if method_from_condition:
            review_fields.append(
                {
                    "field": "method",
                    "confidence": field_confidence["method"],
                    "reason": "未找到试验方法小节，已回退到试验条件",
                }
            )
        if method_from_body:
            review_fields.append(
                {
                    "field": "method",
                    "confidence": field_confidence["method"],
                    "reason": "未找到试验方法小节，已回退到正文",
                }
            )
        if requirement_from_body:
            review_fields.append(
                {
                    "field": "requirement",
                    "confidence": field_confidence["requirement"],
                    "reason": "未找到试验要求小节，已回退到正文",
                }
            )
        if has_table or method_truncated or requirement_truncated:
            review_fields.append(
                {
                    "field": "requirement" if requirement_truncated else "method",
                    "confidence": field_confidence["requirement" if requirement_truncated else "method"],
                    "reason": "章节含表格或文本被截断，需对照原 PDF 复核",
                }
            )
        confidence = round(min(field_confidence.values()), 3)
        parsed.append(
            {
                "seq": seq,
                "name": item["name"],
                "name_auto": item["name"],
                "group": item["parent"],
                "clause": item["num"],
                "requirement": requirement,
                "method_clause": item["method_clause"],
                "method": method,
                "page": item["page"],
                "method_page": method_page,
                "confidence": confidence,
                "confidence_detail": field_confidence,
                "review_required": bool(review_fields),
                "review_fields": review_fields,
                "reviewed": False,
                "references": _find_references(requirement, method),
            }
        )
    return parsed


def _apply_field_rules(items: list[dict], fields: list[dict] | None) -> int:
    """Mark items whose administrator-required fields were not extracted.

    Returns the number of newly requested review fields.  Field visibility
    (``export``) is applied by the Excel export endpoint, not here, so the
    stored result keeps every extracted value for audit.
    """
    required = [
        field
        for field in (fields or [])
        if isinstance(field, dict) and field.get("required")
    ]
    added = 0
    for item in items:
        review_fields = item.setdefault("review_fields", [])
        for field in required:
            key = field.get("key", "")
            value = item.get(key)
            missing = value is None or (isinstance(value, str) and not value.strip())
            if not missing:
                continue
            if any(entry.get("field") == key for entry in review_fields):
                continue
            review_fields.append(
                {
                    "field": key,
                    "confidence": 0.0,
                    "reason": f"字段「{field.get('label') or key}」按规则必填但未提取到",
                }
            )
            item["review_required"] = True
            added += 1
    return added


def parse_standard(
    pages: list[str], file_name: str = "", pdf_bytes: bytes | None = None,
    page_confidence: list[float] | None = None, ocr: dict | None = None,
    rule_config: dict | None = None,
) -> dict:
    """Parse one standard PDF (already split into page texts).

    ``pdf_bytes`` is optional and only used for the table-structure fallback;
    ``page_confidence``/``ocr`` are supplied by the OCR pipeline for scanned
    PDFs and drive the per-field review flags.  ``rule_config`` is the
    administrator-managed ``standard`` section (section aliases and field
    requirements); omitted means the documented defaults.
    """
    started = time.perf_counter()
    rule_config = rule_config or {}
    section_aliases = rule_config.get("section_aliases") or {}
    meta = _extract_meta(pages, file_name)
    warnings: list[str] = []

    total_chars = sum(len(page) for page in pages)
    if total_chars < 500 and not ocr:
        warnings.append(
            f"文本层仅 {total_chars} 字符，内容较少；若为扫描件请启用 OCR 后再解析"
        )
    if ocr:
        warnings.append(
            "已通过 OCR 抽取，平均置信度 "
            f"{ocr.get('avg_confidence', 0):.3f}，所有字段需按置信度人工复核"
        )

    lines = _clean_lines(pages, meta["standard_no"], keep_method_label=bool(ocr))
    if ocr:
        lines = _merge_split_ocr_headings(lines)
    headings = _detect_headings(lines)
    items: list[dict] = []
    source = ""

    combined_items = _parse_combined_items(lines, page_confidence, ocr)
    if combined_items:
        items = combined_items
        source = "combined-sections"
        warnings.append(
            "使用「试验方法和性能要求/技术要求」合并章节抽取：项目=5.x.y，"
            "试验方法/技术要求=5.x.y.z，缺失的字段已标出"
        )

    tech_sec = _section_number(headings, "技术要求", section_aliases.get("技术要求"))
    method_sec = _section_number(headings, "试验方法", section_aliases.get("试验方法"))
    if tech_sec is not None and tech_sec == method_sec:
        # A single combined "技术要求和试验方法" section cannot be split into
        # two clause streams; treat it as a method section for human review.
        if not items:
            warnings.append(
                "「技术要求」与「试验方法」指向同一章节，已按试验方法章节抽取待复核项目"
            )
        tech_sec = None

    if not items and tech_sec is None and method_sec is None:
        chapter_items = _parse_chapter_items(lines, headings)
        if chapter_items:
            items = chapter_items
            source = "chapter-items"
            warnings.append(
                "未识别到统一的「技术要求/试验方法」章节，已按各检测章的"
                "「技术要求/试验方法」子条款抽取项目，需人工复核名称与限值"
            )

    if not items:
        if tech_sec is not None and method_sec is not None:
            tech_section = [
                h for h in headings if h.num.split(".")[0] == tech_sec
            ]
            method_section = [
                h for h in headings if h.num.split(".")[0] == method_sec
            ]
            tech_by_num = {h.num: h for h in tech_section}
            method_leaves = [h for h in method_section if _is_leaf(h, method_section)]

            pairs = _extract_pairs(lines, tech_sec, method_sec)
            pairs = [
                (tech_no, method_no)
                for tech_no, method_no in pairs
                if tech_no in tech_by_num and method_no in {h.num for h in method_leaves}
            ]
            if len(pairs) >= 2:
                mapping: list[tuple[_Heading, _Heading, float]] = [
                    (tech_by_num[tech_no], {h.num: h for h in method_leaves}[method_no], 1.0)
                    for tech_no, method_no in pairs
                ]
                source = "item-table"
            else:
                if pairs:
                    warnings.append("检验项目表不完整，改用标题名称匹配")
                by_method = _match_by_name(tech_section, method_leaves)
                mapping = []
                for method in method_leaves:
                    tech = by_method.get(method.num)
                    if tech is not None:
                        mapping.append((tech, method, 0.75))
                source = "heading-match"

            ordered = sorted(mapping, key=lambda pair: pair[1].line_idx)
            # next heading line (any section) for body slicing
            heading_lines = sorted(h.line_idx for h in headings)
            for seq, (tech, method, confidence) in enumerate(ordered, start=1):
                next_idx = next(
                    (idx for idx in heading_lines if idx > tech.line_idx), len(lines)
                )
                method_next_idx = next(
                    (idx for idx in heading_lines if idx > method.line_idx), len(lines)
                )
                name, group = _item_name(tech, method, tech_by_num, tech_sec)
                requirement = _text_for(lines, tech, next_idx)
                method_text = _text_for(lines, method, method_next_idx)
                if not requirement:
                    warnings.append(f"{tech.num} 未提取到技术要求正文")
                if not method_text:
                    warnings.append(f"{method.num} 未提取到试验方法正文")
                items.append(
                    {
                        "seq": seq,
                        "name": name,
                        "name_auto": name,
                        "group": group,
                        "clause": tech.num,
                        "requirement": requirement,
                        "method_clause": method.num,
                        "method": method_text,
                        "page": tech.page,
                        "method_page": method.page,
                        "confidence": confidence,
                        "review_required": source == "heading-match",
                        "review_fields": (
                            [
                                {
                                    "field": "name",
                                    "confidence": confidence,
                                    "reason": "项目与试验方法按标题名称相似度匹配，需人工复核",
                                }
                            ]
                            if source == "heading-match"
                            else []
                        ),
                        "reviewed": False,
                        "references": _find_references(requirement, method_text),
                    }
                )
            if source == "heading-match":
                warnings.append("未找到完整检验项目表，项目与试验方法按标题名称匹配，需人工复核")
        elif method_sec is not None:
            items = _parse_single_section_items(lines, headings, method_sec, "method")
            if items:
                source = "method-only"
                warnings.append(
                    "未识别到「技术要求」章节，已按「试验方法」章节标题生成待复核项目，"
                    "判定要求需人工补充"
                )
            else:
                warnings.append("未识别到可抽取的「试验方法」条款，无法抽取项目")
        elif tech_sec is not None:
            items = _parse_single_section_items(lines, headings, tech_sec, "requirement")
            if items:
                source = "requirement-only"
                warnings.append(
                    "未识别到「试验方法」章节，已按「技术要求」章节标题生成待复核项目，"
                    "试验方法需人工补充"
                )
            else:
                warnings.append("未识别到可抽取的「技术要求」条款，无法抽取项目")
        else:
            warnings.append("未识别到「技术要求/试验方法」章节，无法抽取项目")

    # PP/PE material standards keep the item list in merged-cell
    # performance tables rather than in headings.  Prefer the table result
    # whenever it recovers more items than the heading heuristic.
    if pdf_bytes and method_sec is not None and source not in {
        "item-table",
        "combined-sections",
        "chapter-items",
    }:
        table_items = _parse_table_items(pdf_bytes, lines, headings, method_sec)
        if len(table_items) > len(items):
            items = table_items
            source = "table-parse"
            warnings = [
                warning
                for warning in warnings
                if "未找到完整检验项目表" not in warning
                and "未识别到「技术要求/试验方法」章节" not in warning
                and "未识别到「技术要求」章节" not in warning
                and "未识别到「试验方法」章节" not in warning
                and "按「试验方法」章节标题生成" not in warning
                and "按「技术要求」章节标题生成" not in warning
            ]
            warnings.append(
                "材料性能以多页合并表格给出，已按表格结构抽取项目和试验方法，需人工复核"
            )

    if not items and method_sec is not None:
        # Table-only standards often have a "技术要求" section whose rows are
        # not clause headings, so the normal pair/item-table paths find
        # nothing.  Expose the method clauses as review-only candidates rather
        # than returning an empty parse; the workbench fills the requirement.
        fallback_items = _parse_single_section_items(lines, headings, method_sec, "method")
        if fallback_items:
            items = fallback_items
            source = "method-only-fallback"
            warnings = [
                warning
                for warning in warnings
                if "无法抽取项目" not in warning
                and "未找到完整检验项目表" not in warning
            ]
            warnings.append(
                "「技术要求」未形成条款条目（可能为表格型标准），已按「试验方法」章节生成待复核项目；"
                "判定要求需人工补充"
            )

    conditions_summary = apply_conditions(items)
    limits_summary = apply_limits(items)
    line_meta = [(line.text, line.page) for line in lines]
    formula_entries, formulas_summary = apply_formulas(items, line_meta)
    sample_entries, samples_summary = apply_samples(items, line_meta)
    rounding_entries, roundings_summary = apply_rounding(items, line_meta)
    table_entries, tables_summary = apply_tables(pdf_bytes, pages, items)
    mapping_summary = apply_table_mapping(items, table_entries)
    items_list, items_list_summary = apply_items_list(meta, items, line_meta)
    missing_parts = (
        len(items) - items_list_summary["items_with_applicable_parts"]
    )
    if missing_parts:
        warnings.append(
            f"{missing_parts} 个检测项目未提取到适用零部件，已加入人工复核清单"
        )

    if len(items) < 2:
        warnings.append("抽取到的项目少于 2 项，请检查标准结构或改用人工解析")

    if ocr:
        meta["is_scanned"] = True
        meta["ocr"] = ocr

    field_rule_hits = _apply_field_rules(items, rule_config.get("fields"))
    if field_rule_hits:
        warnings.append(
            f"按规则配置有 {field_rule_hits} 个必填字段未提取到，已加入人工复核清单"
        )
    if rule_config:
        meta["rule_config"] = {
            "section_aliases": {
                key: list(value)
                for key, value in section_aliases.items()
                if isinstance(value, (list, tuple))
            },
            "required_fields": [
                field.get("key")
                for field in rule_config.get("fields", [])
                if isinstance(field, dict) and field.get("required")
            ],
        }

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    review_required = sum(1 for item in items if item.get("review_required"))
    summary = {
        "items": len(items),
        "items_with_requirement": sum(1 for item in items if item["requirement"]),
        "items_with_method": sum(1 for item in items if item["method"]),
        "clauses_traced": sum(
            1
            for item in items
            if item["clause"] and item["method_clause"] and item["page"] and item["method_page"]
        ),
        "pages": len(pages),
        "chars": total_chars,
        "reviewed_items": 0,
        "items_with_applicable_parts": items_list_summary[
            "items_with_applicable_parts"
        ],
        "review_required_items": review_required,
        "review_fields": sum(len(item.get("review_fields", [])) for item in items),
        "parse_ms": elapsed_ms,
        **conditions_summary,
        **limits_summary,
        **samples_summary,
        **formulas_summary,
        **roundings_summary,
        **tables_summary,
        **mapping_summary,
    }
    if ocr:
        summary["ocr_pages"] = ocr.get("pages", len(pages))
        summary["ocr_avg_confidence"] = ocr.get("avg_confidence", 0.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": meta,
        "items": items,
        "items_list": items_list,
        "samples": sample_entries,
        "formulas": formula_entries,
        "roundings": rounding_entries,
        "tables": table_entries,
        "summary": summary,
        "warnings": warnings,
        "mapping_source": source or "heading-match",
    }
