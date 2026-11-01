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
from io import BytesIO
from typing import Iterable

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


def parse_standard(
    pages: list[str], file_name: str = "", pdf_bytes: bytes | None = None
) -> dict:
    """Parse one digital standard PDF (already split into page texts).

    ``pdf_bytes`` is optional and only used for the table-structure fallback;
    text-only callers (and unit tests) keep the original behaviour.
    """
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
    source = ""
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

    # PP/PE material standards keep the item list in merged-cell
    # performance tables rather than in headings.  Prefer the table result
    # whenever it recovers more items than the heading heuristic.
    if pdf_bytes and method_sec is not None and source != "item-table":
        table_items = _parse_table_items(pdf_bytes, lines, headings, method_sec)
        if len(table_items) > len(items):
            items = table_items
            source = "table-parse"
            warnings = [
                warning
                for warning in warnings
                if "未找到完整检验项目表" not in warning
                and "未识别到「技术要求/试验方法」章节" not in warning
            ]
            warnings.append(
                "材料性能以多页合并表格给出，已按表格结构抽取项目和试验方法，需人工复核"
            )

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
        "mapping_source": source or "heading-match",
    }
