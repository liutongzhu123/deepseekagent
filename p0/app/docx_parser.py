"""Minimal OOXML report parser for the P0 report-review service.

This intentionally uses the Python standard library (zipfile + ElementTree)
instead of a high-level docx library so that the parser preserves the
table/merge/highlight/red-run/external-image information needed by the
deterministic review rules.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _q(tag: str) -> str:
    return W + tag


def _text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter(_q("t"))).strip()


@dataclass
class Cell:
    text: str
    vmerge: str | None = None
    gridspan: int = 1


@dataclass
class ReportData:
    file_name: str
    cover_number: str = ""
    header_numbers: list[str] = field(default_factory=list)
    sample_name: str = ""
    model: str = ""
    client: str = ""
    supplier: str = ""
    receive_date: str = ""
    production_date: str = ""
    sample_date: str = ""
    sample_qty: str = ""
    sample_no: str = ""
    basis: str = ""
    item: str = ""
    conclusion: str = ""
    scores: list[float] = field(default_factory=list)
    reported_mean: float | None = None
    limit: float | None = None
    judgment: str = ""
    device_valid: str = ""
    page_totals: list[int] = field(default_factory=list)
    page_numbers: list[int] = field(default_factory=list)
    notices: int = 0
    highlights: int = 0
    red_runs: int = 0
    external_images: int = 0
    embedded_images: int = 0
    signature_text: str = ""
    test_start: str = ""
    test_end: str = ""
    approval_date: str = ""
    template_family: str = ""
    paragraphs: list[str] = field(default_factory=list)
    tables: list[list[list[Cell]]] = field(default_factory=list)


DATE_RE = re.compile(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _cell_from_tc(tc: ET.Element) -> Cell:
    text = _text(tc)
    vmerge = None
    gridspan = 1
    tc_pr = tc.find(_q("tcPr"))
    if tc_pr is not None:
        vm = tc_pr.find(_q("vMerge"))
        if vm is not None:
            vmerge = vm.attrib.get(_q("val"), "continue") or "continue"
        gs = tc_pr.find(_q("gridSpan"))
        if gs is not None:
            try:
                gridspan = int(gs.attrib.get(_q("val"), "1"))
            except ValueError:
                gridspan = 1
    return Cell(text=text, vmerge=vmerge, gridspan=gridspan)


def _parse_table(tbl: ET.Element) -> list[list[Cell]]:
    rows: list[list[Cell]] = []
    for tr in tbl.findall(_q("tr")):
        rows.append([_cell_from_tc(tc) for tc in tr.findall(_q("tc"))])
    return rows


def _iter_tables(root: ET.Element) -> list[list[list[Cell]]]:
    return [_parse_table(tbl) for tbl in root.iter(_q("tbl"))]


def _iter_paragraphs(root: ET.Element) -> list[str]:
    out: list[str] = []
    for p in root.iter(_q("p")):
        text = _text(p)
        if text:
            out.append(text)
    return out


def _count_markup(root: ET.Element) -> tuple[int, int]:
    highlights = 0
    red_runs = 0
    red_colors = {"FF0000", "C00000", "E36C0A"}
    for run in root.iter(_q("r")):
        rpr = run.find(_q("rPr"))
        if rpr is None:
            continue
        hl = rpr.find(_q("highlight"))
        if hl is not None:
            val = (hl.attrib.get(_q("val")) or "").lower()
            if val and val != "none":
                highlights += 1
        color = rpr.find(_q("color"))
        if color is not None:
            val = (color.attrib.get(_q("val")) or "").upper()
            if val in red_colors:
                red_runs += 1
    return highlights, red_runs


def _pairs_from_table(rows: list[list[Cell]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in rows:
        for index, cell in enumerate(row):
            label = cell.text.strip()
            if not label:
                continue
            value = ""
            for nxt in row[index + 1 :]:
                if nxt.text.strip():
                    value = nxt.text.strip()
                    break
            pairs.append((label, value))
    return pairs


def _get(pairs: Iterable[tuple[str, str]], label: str, last: bool = False) -> str:
    values = [value for key, value in pairs if key == label]
    if not values:
        return ""
    return values[-1] if last else values[0]


def _find_result_table(tables: list[list[list[Cell]]]) -> list[list[Cell]] | None:
    for table in tables:
        if not table:
            continue
        header = [cell.text.strip() for cell in table[0]]
        if "评价员" in header and "检验结果" in header:
            return table
    return None


def _parse_result_rows(table: list[list[Cell]]) -> tuple[list[float], float | None, float | None, str]:
    header = [cell.text.strip() for cell in table[0]]
    try:
        idx_evaluator = header.index("评价员")
        idx_result = header.index("检验结果")
    except ValueError:
        return [], None, None, ""
    idx_mean = header.index("检验均值") if "检验均值" in header else None
    idx_limit = header.index("限值要求") if "限值要求" in header else None
    idx_judgment = header.index("符合性判定") if "符合性判定" in header else None

    scores: list[float] = []
    mean: float | None = None
    limit: float | None = None
    judgment = ""
    for row in table[1:]:
        if len(row) <= max(idx_evaluator, idx_result):
            continue
        evaluator = row[idx_evaluator].text.strip()
        result = row[idx_result].text.strip()
        if evaluator and result:
            try:
                scores.append(float(result))
            except ValueError:
                pass
        if idx_mean is not None and idx_mean < len(row) and mean is None:
            value = row[idx_mean].text.strip()
            if value:
                try:
                    mean = float(value)
                except ValueError:
                    pass
        if idx_limit is not None and idx_limit < len(row) and limit is None:
            match = re.search(r"[≤<]\s*(\d+(?:\.\d+)?)", row[idx_limit].text)
            if match:
                limit = float(match.group(1))
        if idx_judgment is not None and idx_judgment < len(row) and not judgment:
            judgment = row[idx_judgment].text.strip()
    return scores, mean, limit, judgment


def parse_docx(path: str | Path) -> ReportData:
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        xml_bytes = zf.read("word/document.xml")
        embedded_images = len([n for n in names if n.startswith("word/media/")])
    root = ET.fromstring(xml_bytes)
    paragraphs = _iter_paragraphs(root)
    tables = _iter_tables(root)
    body_text = "\n".join(paragraphs)
    xml_text = xml_bytes.decode("utf-8", errors="ignore")

    header_numbers: list[str] = []
    for line in paragraphs:
        if "报告编号" in line:
            match = re.search(r"报告编号\s*[:：]\s*([A-Za-z0-9\-]*)", line)
            header_numbers.append(match.group(1).strip() if match else "")
    # Some documents place the number on the line after the label; capture only
    # explicit non-empty values from the line itself to avoid false positives.
    cover_number = next((n for n in header_numbers if n), "")
    body_numbers = header_numbers[1:] if header_numbers else []

    summary_pairs: list[tuple[str, str]] = []
    sample_pairs: list[tuple[str, str]] = []
    for table in tables:
        labels = {cell.text.strip() for row in table for cell in row}
        if "样品名称" in labels and "检验依据" in labels:
            summary_pairs.extend(_pairs_from_table(table))
            break
    for table in tables:
        labels = {cell.text.strip() for row in table for cell in row}
        if "*样品名称" in labels or "*样品数量" in labels:
            sample_pairs = _pairs_from_table(table)
            break

    summary = {
        "样品名称": _get(summary_pairs, "样品名称"),
        "型号规格": _get(summary_pairs, "型号规格"),
        "生产企业": _get(summary_pairs, "生产企业"),
        "样品数量": _get(summary_pairs, "样品数量"),
        "生产日期": _get(summary_pairs, "生产日期"),
        "送样日期": _get(summary_pairs, "送样日期"),
        "检验依据": _get(summary_pairs, "检验依据"),
        "检验项目": _get(summary_pairs, "检验项目"),
        "检验结论": _get(summary_pairs, "检验结论"),
    }
    sample = {
        "*样品名称": _get(sample_pairs, "*样品名称"),
        "*规格型号": _get(sample_pairs, "*规格型号"),
        "*生产企业": _get(sample_pairs, "*生产企业"),
        "*样品数量": _get(sample_pairs, "*样品数量"),
        "*生产日期": _get(sample_pairs, "*生产日期"),
        "*抽样日期": _get(sample_pairs, "*抽样日期"),
        "送样日期": _get(sample_pairs, "送样日期", last=True),
        "样品编号": _get(sample_pairs, "样品编号"),
    }

    result_table = _find_result_table(tables)
    scores, reported_mean, limit, judgment = _parse_result_rows(result_table) if result_table else ([], None, None, "")

    device_valid = ""
    for table in tables:
        if not table:
            continue
        header = [cell.text.strip() for cell in table[0]]
        if "溯源有效期" not in header:
            continue
        idx = header.index("溯源有效期")
        for row in table[1:]:
            if idx < len(row) and row[idx].text.strip():
                device_valid = row[idx].text.strip()
                break
        break

    page_totals = [int(m.group(1)) for m in re.finditer(r"共\s*(\d+)\s*页", body_text)]
    page_numbers = [int(m.group(1)) for m in re.finditer(r"第\s*(\d+)\s*页", body_text)]

    notices = 0
    notice_start = next((i for i, line in enumerate(paragraphs) if "注  意  事  项" in line or "注意事项" in line), None)
    if notice_start is not None:
        for line in paragraphs[notice_start + 1 :]:
            if "检验单位地址电话" in line:
                break
            if re.match(r"^\d+[.．]", line.strip()):
                notices += 1

    match = re.search(r"检验于\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*至\s*(\d{4}年\d{1,2}月\d{1,2}日)", body_text)
    if not match:
        match = re.search(r"检验于\s*(\d{4}年\d{1,2}月\d{1,2}日)", body_text)
    test_start = match.group(1) if match else ""
    test_end = match.group(2) if match and match.lastindex and match.lastindex >= 2 else test_start

    approval_match = re.search(r"签发日期\s*[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日)", summary.get("检验结论", ""))
    approval_date = approval_match.group(1) if approval_match else ""

    signatures = [line for line in paragraphs if re.search(r"(批准|审核|主检)\s*[:：]", line)]
    signature_text = " | ".join(signatures)

    highlights, red_runs = _count_markup(root)
    external_images = xml_text.count("INCLUDEPICTURE")

    basis = summary.get("检验依据", "")
    item = summary.get("检验项目", "")
    template_family = "BYD-odor" if ("Q/BYDQ" in basis or "气味" in item) else "generic"

    return ReportData(
        file_name=path.name,
        cover_number=cover_number,
        header_numbers=body_numbers,
        sample_name=summary.get("样品名称", ""),
        model=summary.get("型号规格", ""),
        client=_get(summary_pairs, "委托单位"),
        supplier=summary.get("生产企业", ""),
        receive_date=summary.get("送样日期", ""),
        production_date=sample.get("*生产日期") or summary.get("生产日期", ""),
        sample_date=sample.get("*抽样日期", ""),
        sample_qty=summary.get("样品数量", ""),
        sample_no=sample.get("样品编号", ""),
        basis=basis,
        item=item,
        conclusion=summary.get("检验结论", ""),
        scores=scores,
        reported_mean=reported_mean,
        limit=limit,
        judgment=judgment,
        device_valid=device_valid,
        page_totals=page_totals,
        page_numbers=page_numbers,
        notices=notices,
        highlights=highlights,
        red_runs=red_runs,
        external_images=external_images,
        embedded_images=embedded_images,
        signature_text=signature_text,
        test_start=test_start,
        test_end=test_end,
        approval_date=approval_date,
        template_family=template_family,
        paragraphs=paragraphs,
        tables=tables,
    )


def fields_for_consistency(data: ReportData) -> list[tuple[str, str, str]]:
    """Return summary/sample mismatches for R03."""
    # Re-parse the summary/sample pairs using the same tables: keep this helper
    # small and explicit instead of mutating ReportData with parser internals.
    summary: dict[str, str] = {}
    sample: dict[str, str] = {}
    for table in data.tables:
        labels = {cell.text.strip() for row in table for cell in row}
        if "样品名称" in labels and "检验依据" in labels and not summary:
            summary = {key: value for key, value in _pairs_from_table(table)}
        if ("*样品名称" in labels or "*样品数量" in labels) and not sample:
            sample = {key: value for key, value in _pairs_from_table(table)}
    pairs = [
        ("样品名称", summary.get("样品名称", ""), sample.get("*样品名称", "")),
        ("规格型号", summary.get("型号规格", ""), sample.get("*规格型号", "")),
        ("生产企业", summary.get("生产企业", ""), sample.get("*生产企业", "")),
        ("样品数量", summary.get("样品数量", ""), sample.get("*样品数量", "")),
        ("生产日期", summary.get("生产日期", ""), sample.get("*生产日期", "")),
    ]
    return [(label, left, right) for label, left, right in pairs if left != right]
