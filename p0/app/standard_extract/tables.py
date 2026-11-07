"""T1: structured tables from a standard PDF (``tables[]``).

pdfplumber geometry keeps merged rows/columns aligned.  Each entry is
``{clause, clause_source, header, rows, page, raw_bbox, header_detected,
has_merges, parse_warning}``; table text is not copied into ``requirement``.
Ownership comes from the first-column clause or the nearest heading above;
unresolved tables are exported with ``parse_warning``, never guessed.
"""
from __future__ import annotations

import re
from io import BytesIO

from .. import watermark

#: Column labels that identify a header row; T2 does the semantic mapping.
_HEADER_KEYWORDS = (
    "项目", "条款", "要求", "限值", "条件", "方法", "判定", "序号", "名称",
    "参数", "单位", "备注", "技术", "试验", "检验", "性能", "符号", "分类",
    "范围", "指标", "规格", "内容", "代码", "类型", "位置", "模式", "试剂",
    "密封", "测试", "步骤", "结果", "样品", "状态", "时间", "次数", "频率",
    "强度", "功率", "距离", "等级", "序列", "目标", "类别", "波形", "速率",
)
_CLAUSE = r"(?:[A-Z](?:\.\d{1,3}){1,3}|[1-9]\d?(?:\.\d{1,3}){0,3})"
_CLAUSE_IN_CELL = re.compile(
    r"^\s*((?:[A-Z](?:\.\d{1,3}){1,3}|[1-9]\d?(?:\.\d{1,3}){1,3}))"
    r"(?=\s|$|[、,，])"
)
_CLAUSE_HEAD = re.compile(rf"^({_CLAUSE})\s+([^\d].{{0,80}})$")
_INDEX = re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|[1-9]\d?")
_TITLE = re.compile(r"^(?:续)?表\s*\d+(?:\s|$)")
_UNIT_VALUE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:V|A|mA|kV|MPa|N|mm|cm|℃|%|s|ms|min|h|kg|g|"
    r"Ω|MΩ|Hz|kHz|MHz|dB|r/min|km/h)"
)
_LETTER_AFTER = re.compile(
    r"(?<=[\u4e00-\u9fff])[A-Za-z](?=[\u4e00-\u9fff]|$|[^A-Za-z])"
)
_LETTER_BEFORE = re.compile(r"(?<![A-Za-z])[A-Za-z](?=[\u4e00-\u9fff])")
LARGE_TABLE_CHARS = 150

def _clean_cell(value: str | None) -> str:
    """Join wrapped lines, keep units and drop isolated watermark letters."""
    text = (value or "").replace("\r", "")
    text = re.sub(r"(?<=[A-Za-z])\s*\n\s*(?=[A-Za-z])", " ", text)
    text = re.sub(r"\s+", " ", text.replace("\n", "")).strip()
    text, _ = watermark.clean_text(text)
    if not re.search(r"\d", text):
        text = re.sub(r"(?<![A-Za-z])[a-z](?![A-Za-z])", "", text)
    for _ in range(3):
        cleaned = _LETTER_BEFORE.sub("", _LETTER_AFTER.sub("", text))
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"\s+", " ", text).strip()
def _clause_in(value: str) -> str:
    text = value or ""
    match = _CLAUSE_IN_CELL.match(text)
    if not match:
        return ""
    rest = text[match.end():].strip(" \t、,，")
    return match.group(1) if not rest or re.match(r"[\u4e00-\u9fff\d]", rest) else ""
def _line_clause(value: str) -> str:
    match = _CLAUSE_HEAD.match(re.sub(r"\s+", " ", value or "").strip())
    if not match or any(mark in match.group(2) for mark in "。；;"):
        return ""
    return match.group(1) if re.search(r"[\u4e00-\u9fff]", match.group(2)) else ""
def _is_value(cell: str) -> bool:
    return bool(re.search(r"[±≥≤<>~～]", cell) or _UNIT_VALUE.search(cell) or re.fullmatch(r"[\d.,，~～\-]+", cell))
def _trim(grid: list[list[str]]) -> list[list[str]]:
    """Drop empty border rows/columns produced by the PDF table detector."""
    while grid and (not any(grid[0]) or not any(grid[-1])):
        grid = grid[1:] if not any(grid[0]) else grid[:-1]
    if not grid:
        return grid
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    low, high = 0, width
    while low < high and all(not row[low] for row in grid):
        low += 1
    while high > low and all(not row[high - 1] for row in grid):
        high -= 1
    return [row[low:high] for row in grid]
def _header_range(grid: list[list[str]]) -> tuple[int, int]:
    """Return ``(start, count)`` of header rows, skipping a merged title row."""
    start = 0
    while start < min(2, len(grid)):
        nonempty = [cell for cell in grid[start] if cell]
        if len(nonempty) == 1 and _TITLE.match(nonempty[0]):
            start += 1
            continue
        break
    if start >= len(grid):
        return start, 0
    count = 1
    for row in grid[start + 1:start + 3]:
        nonempty = [cell for cell in row if cell]
        if not nonempty:
            break
        values = sum(1 for cell in nonempty if _is_value(cell))
        value_ratio = values / len(nonempty)
        if not row[0]:
            if value_ratio > 0.4 and not all(_INDEX.fullmatch(cell) for cell in nonempty):
                break
            count += 1
            continue
        if (_clause_in(row[0]) or _INDEX.fullmatch(row[0])
                or value_ratio > 0.4):
            break
        if value_ratio <= 0.4 and any(
            word in row[0] for word in _HEADER_KEYWORDS
        ):
            count += 1
            continue
        break
    return start, count
def _combine_header(rows: list[list[str]]) -> list[str]:
    """Merge a one/two-row header, spreading merged parent cells rightwards."""
    width = max(len(row) for row in rows)
    filled: list[list[str]] = []
    for row in rows:
        values = list(row) + [""] * (width - len(row))
        previous = ""
        for index, value in enumerate(values):
            if value:
                previous = value
            else:
                values[index] = previous
        filled.append(values)
    header: list[str] = []
    for index in range(width):
        parts: list[str] = []
        for row in filled:
            value = row[index].strip()
            if value and value not in parts:
                parts.append(value)
        header.append(" / ".join(parts))
    return header
def _page_clauses(page, tables: list, current: str) -> tuple[dict[int, str], str]:
    """Map each table on one page to the nearest clause heading above it."""
    try:
        raw_lines = page.extract_text_lines()
    except Exception:  # noqa: BLE001 - a broken page must not stop the parse
        raw_lines = []
    blocked = [table.bbox for table in tables]
    events: list[tuple[float, int, object]] = []
    for line in raw_lines:
        text = _clean_cell(line.get("text"))
        centre = (line.get("top", 0) + line.get("bottom", 0)) / 2
        if text and not any(box[1] <= centre <= box[3] for box in blocked):
            events.append((line.get("top", 0), 0, text))
    events += [(table.bbox[1], 1, index) for index, table in enumerate(tables)]
    found: dict[int, str] = {}
    for _top, kind, payload in sorted(events, key=lambda item: (item[0], item[1])):
        if kind == 0:
            current = _line_clause(str(payload)) or current
        else:
            found[int(payload)] = current
    return found, current
def _first_column_clause(grid: list[list[str]]) -> str:
    return next((clause for row in grid[:200] if row and (clause := _clause_in(row[0]))), "")
def _entry(table, grid: list[list[str]], page: int, clause: str,
           clause_source: str) -> dict:
    start, count = _header_range(grid)
    header_rows = grid[start:start + count] if count else []
    keyword_header = any(
        word in cell for row in header_rows for cell in row for word in _HEADER_KEYWORDS
    )
    sentence_header = any("。" in cell for row in header_rows for cell in row)
    value_header = bool(header_rows) and not any(
        _is_value(cell) for cell in header_rows[0]
    ) and any(
        _is_value(cell) for cell in (grid[start + count] if start + count < len(grid) else [])
    )
    header_detected = bool(header_rows) and not sentence_header and (
        keyword_header or value_header
    )
    warnings: list[str] = []
    if not header_detected: warnings.append("未识别到表头")
    if not clause: warnings.append("未定位到所属条款")
    noisy = any(
        re.search(r"\d{4}/\d{1,2}/\d{1,2}|@|\.com", cell)
        for row in header_rows for cell in row
    )
    if noisy: warnings.append("表头含水印/页眉残留，需人工复核")
    merged = sum(1 for row in table.rows if any(cell is None for cell in row.cells))
    return {
        "clause": clause,
        "clause_source": clause_source,
        "header": _combine_header(header_rows) if header_detected else [],
        "rows": grid[start + count:] if header_detected else grid[start:],
        "page": page,
        "raw_bbox": [round(value, 1) for value in table.bbox],
        "header_rows": count,
        "header_detected": header_detected,
        "merged_cells": merged,
        "has_merges": bool(merged),
        "parse_warning": "；".join(warnings) or None,
    }
def _chars(entry: dict) -> int:
    cells = list(entry["header"]) + [c for row in entry["rows"] for c in row]
    return sum(len(cell) for cell in cells)
def _drop_repeated_furniture(entries: list[dict]) -> list[dict]:
    """Drop small tables whose exact content repeats on several pages."""
    groups: dict[tuple, list[int]] = {}
    for index, entry in enumerate(entries):
        if _chars(entry) < LARGE_TABLE_CHARS:
            signature = tuple(entry["header"]) + tuple(map(tuple, entry["rows"]))
            groups.setdefault(signature, []).append(index)
    drop = {i for pages in groups.values() if len(pages) >= 2 for i in pages}
    return [entry for index, entry in enumerate(entries) if index not in drop]
def extract_tables(pdf_bytes: bytes | None, pages: list[str]) -> list[dict]:
    """Extract every usable table as a structured ``tables[]`` entry."""
    del pages  # page text is only used for numbering by the caller
    if not pdf_bytes:
        return []
    try:
        import pdfplumber
        pdf = pdfplumber.open(BytesIO(pdf_bytes))
    except Exception:  # pragma: no cover - optional dependency / broken PDF
        return []
    entries: list[dict] = []
    current = ""
    with pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                found = page.find_tables()
            except Exception:  # noqa: BLE001 - one bad page must not kill the batch
                continue
            tables: list[tuple[object, list[list[str]]]] = []
            for table in found:
                try:
                    raw_rows = table.extract()
                except Exception:  # noqa: BLE001 - skip the broken table only
                    continue
                grid = [[_clean_cell(cell) for cell in row] for row in raw_rows if row]
                if not grid:
                    continue
                width = max(len(row) for row in grid)
                grid = _trim([row + [""] * (width - len(row)) for row in grid])
                if not grid or len(grid[0]) < 2 or sum(len(cell) for row in grid for cell in row) < 4:
                    continue
                tables.append((table, grid))
            tables.sort(key=lambda item: item[0].bbox[1])
            page_clauses, current = _page_clauses(
                page, [table for table, _ in tables], current
            )
            for index, (table, grid) in enumerate(tables):
                column = _first_column_clause(grid)
                clause = column or page_clauses.get(index, "")
                source = "first-column" if column else ("heading" if clause else "")
                entries.append(_entry(table, grid, page_no, clause, source))
    return _drop_repeated_furniture(entries)
def apply_tables(
    pdf_bytes: bytes | None, pages: list[str], items: list[dict]
) -> tuple[list[dict], dict]:
    """Return ``(tables, summary)`` and attach each table to its item clause."""
    entries = extract_tables(pdf_bytes, pages)
    page_items = sorted({
        (page, item["clause"])
        for item in items if item.get("clause")
        for page in (item.get("page"), item.get("method_page")) if page
    })
    for entry in entries:
        current = entry["clause"]
        candidates = [
            clause for page, clause in page_items
            if page <= entry["page"]
            and (not current or "." not in current or clause == current
                 or clause.startswith(current + "."))
        ]
        if candidates and (
            not current or candidates[-1].count(".") > current.count(".")
        ):
            inferred = not current
            entry["clause_source"] = "item-fallback" if inferred else "item-page"
            entry["clause"] = candidates[-1]
            if inferred:
                entry["parse_warning"] = (
                    entry["parse_warning"] or "条款按邻近条目推断，需人工复核"
                )
    by_clause: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["clause"]:
            by_clause.setdefault(entry["clause"], []).append(entry)
    for item in items:
        tables = by_clause.get(item.get("clause", ""), [])
        item["tables"] = tables
        item["tables_status"] = "extracted" if tables else "not_extracted"
    return entries, {
        "tables": len(entries),
        "tables_large": sum(1 for entry in entries if _chars(entry) >= LARGE_TABLE_CHARS),
        "tables_with_header": sum(1 for entry in entries if entry["header_detected"]),
        "tables_with_clause": sum(1 for entry in entries if entry["clause"]),
        "tables_with_merges": sum(1 for entry in entries if entry["has_merges"]),
        "tables_with_warnings": sum(1 for entry in entries if entry["parse_warning"]),
        "items_with_tables": sum(
            1 for item in items if item.get("tables_status") == "extracted"
        ),
    }
