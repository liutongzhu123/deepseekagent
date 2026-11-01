"""Deterministic standard parser for the P0 standard-parsing increment.

Scope (P0 Sprint 2, first slice):
  * digital PDF standards with a "技术要求" section and a "试验方法" section;
  * an item/requirements/method table (like EPB table 3) when present is used
    as the authoritative item -> method mapping;
  * otherwise the method-section leaf clauses are matched to the
    requirements-section headings by Chinese name similarity;
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
from typing import Iterable

SCHEMA_VERSION = "standard-v0.1"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
HEAD_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+(\S.*)$")
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


def _clean_lines(pages: list[str], standard_no: str) -> list[_Line]:
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
        if not re.search(r"[A-Za-z]{2,}", rest):
            continue
        if re.search(r"[，。；：,;:]", rest):
            continue
        if len(rest) > 120:
            continue
        if re.match(r"^\d", rest):
            continue
        headings.append(
            _Heading(num=num, text=rest, page=line.page, line_idx=line.idx)
        )
    for heading in headings:
        heading.name = _chinese_name(heading.text)
    return headings


def _section_number(headings: list[_Heading], keyword: str) -> str | None:
    for heading in headings:
        if heading.num.count(".") == 0 and keyword in heading.name:
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
        text = re.sub(r"^\d+(?:\.\d+){1,4}\s+(?=[\u4e00-\u9fff])", "", raw).strip()
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
    return {
        "standard_no": standard_no,
        "name": name or re.sub(r"\.pdf$", "", file_name, flags=re.I),
        "issue_date": issue_date,
        "implementation_date": implementation_date,
        "confidential": confidential,
        "pages": len(pages),
        "file_name": file_name,
    }


def parse_standard(pages: list[str], file_name: str = "") -> dict:
    """Parse one digital standard PDF (already split into page texts)."""
    started = time.perf_counter()
    meta = _extract_meta(pages, file_name)
    warnings: list[str] = []

    total_chars = sum(len(page) for page in pages)
    if total_chars < 500:
        warnings.append(
            f"PDF 文本层仅 {total_chars} 字符，可能是扫描件；本增量不启用 OCR，无法抽取项目"
        )

    lines = _clean_lines(pages, meta["standard_no"])
    headings = _detect_headings(lines)
    tech_sec = _section_number(headings, "技术要求")
    method_sec = _section_number(headings, "试验方法")
    if tech_sec is None:
        tech_sec = "5" if any(h.num == "5" for h in headings) else None
    if method_sec is None:
        method_sec = "6" if any(h.num == "6" for h in headings) else None

    items: list[dict] = []
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
                    "reviewed": False,
                    "references": _find_references(requirement, method_text),
                }
            )
        if source == "heading-match":
            warnings.append("未找到完整检验项目表，项目与试验方法按标题名称匹配，需人工复核")
    else:
        warnings.append("未识别到「技术要求/试验方法」章节，无法抽取项目")

    if len(items) < 2 and total_chars >= 500:
        warnings.append("抽取到的项目少于 2 项，请检查标准结构或改用人工解析")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
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
        "parse_ms": elapsed_ms,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": meta,
        "items": items,
        "summary": summary,
        "warnings": warnings,
        "mapping_source": "item-table" if len(items) and items[0]["confidence"] == 1.0 else "heading-match",
    }
