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

try:  # optional in minimal deployments; defaults keep the BYD mapping working
    import yaml
except ImportError:  # pragma: no cover - requirements installs PyYAML
    yaml = None

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
    source_format: str = "docx"
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
    devices: list[dict] = field(default_factory=list)
    result_cells: list[list[str]] = field(default_factory=list)
    page_totals: list[int] = field(default_factory=list)
    page_numbers: list[int] = field(default_factory=list)
    footer_pages: list[int] = field(default_factory=list)
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
    template_variant: str = ""
    toc_entries: list[dict] = field(default_factory=list)
    font_sizes: list[float] = field(default_factory=list)
    font_names: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    tables: list[list[list[Cell]]] = field(default_factory=list)
    # Rendered-page split (explicit/lastRendered page breaks); used by R12.
    pages: list[dict] = field(default_factory=list)
    bookmarks: list[dict] = field(default_factory=list)
    bookmark_ends: list[str] = field(default_factory=list)
    bookmark_refs: list[dict] = field(default_factory=list)
    paragraph_styles: list[dict] = field(default_factory=list)


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


def _iter_pages(root: ET.Element) -> list[dict]:
    """Approximate rendered pages from w:br/page and lastRenderedPageBreak."""
    body = root.find(_q("body"))
    if body is None:
        return []
    pages: list[dict] = [{"index": 1, "chars": 0, "images": 0, "lines": []}]

    def walk(element: ET.Element) -> None:
        if element.tag in {_q("p"), _q("tbl")}:
            text = _text(element)
            page = pages[-1]
            if text:
                page["lines"].append(text)
                page["chars"] += len(re.sub(r"\s+", "", text))
            page["images"] += len(list(element.iter(_q("drawing")))) + len(
                list(element.iter(_q("pict")))
            )
            broken = any(
                (br.attrib.get(_q("type")) or "").lower() == "page"
                for br in element.iter(_q("br"))
            ) or any(True for _ in element.iter(_q("lastRenderedPageBreak")))
            if broken:
                pages.append({"index": len(pages) + 1, "chars": 0, "images": 0, "lines": []})
        elif element.tag == _q("sdt"):
            content = element.find(_q("sdtContent"))
            if content is not None:
                for child in list(content):
                    walk(child)

    for child in list(body):
        walk(child)
    while len(pages) > 1 and not pages[-1]["lines"] and not pages[-1]["images"]:
        pages.pop()
    return pages


def _parse_bookmarks(root: ET.Element) -> tuple[list[dict], list[str], list[dict]]:
    """Collect bookmark definitions, end ids and internal references with paragraph index."""
    starts: list[dict] = []
    ends: list[str] = []
    refs: list[dict] = []
    for index, paragraph in enumerate(root.iter(_q("p")), start=1):
        snippet = _text(paragraph)[:60]
        for start in paragraph.iter(_q("bookmarkStart")):
            name = (start.attrib.get(_q("name")) or "").strip()
            if name:
                starts.append({"name": name, "id": start.attrib.get(_q("id")), "paragraph": index, "text": snippet})
        for end in paragraph.iter(_q("bookmarkEnd")):
            ends.append(end.attrib.get(_q("id")) or "")
        for link in paragraph.iter(_q("hyperlink")):
            anchor = (link.attrib.get(_q("anchor")) or "").strip()
            if anchor:
                refs.append({"name": anchor, "paragraph": index, "text": snippet, "kind": "hyperlink"})
        for field in paragraph.iter(_q("fldSimple")):
            instr = field.attrib.get(_q("instr")) or ""
            for name in re.findall(r"\b(?:REF|PAGEREF)\s+([^\s\\]+)", instr):
                refs.append({"name": name, "paragraph": index, "text": snippet, "kind": "field"})
        instr_text = "".join(node.text or "" for node in paragraph.iter(_q("instrText")))
        for name in re.findall(r"\b(?:REF|PAGEREF)\s+([^\s\\]+)", instr_text):
            refs.append({"name": name, "paragraph": index, "text": snippet, "kind": "field"})
    return starts, ends, refs


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


def _expand_table(rows: list[list[Cell]]) -> list[list[str]]:
    """Expand gridSpan/vMerge cells into a rectangular ``cells[row][col]``."""
    grid: list[list[str]] = []
    for row in rows:
        expanded: list[str] = []
        column = 0
        for cell in row:
            text = cell.text.strip()
            span = max(1, cell.gridspan)
            for offset in range(span):
                value = text
                if cell.vmerge == "continue" and not text and grid:
                    # Continuation cells inherit the value directly above.
                    previous = grid[-1]
                    if column + offset < len(previous):
                        value = previous[column + offset]
                expanded.append(value)
            column += span
        grid.append(expanded)
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([""] * (width - len(row)))
    return grid


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


TOC_LINE_RE = re.compile(r"^(?P<title>.+?)\s*[…\.·]{2,}\s*\(?(\d+)\)?\s*$")


def _parse_toc(paragraphs: list[str]) -> list[dict]:
    marker = next(
        (index for index, line in enumerate(paragraphs) if re.sub(r"\s+", "", line) == "目录"),
        None,
    )
    if marker is None:
        return []
    entries: list[dict] = []
    for line in paragraphs[marker + 1 :]:
        match = TOC_LINE_RE.match(line.strip())
        if not match:
            if entries:
                break
            continue
        title = match.group("title").strip().rstrip(".．·… ")
        entries.append({"title": title, "page": int(match.group(2))})
    return entries


def _style_summary(root: ET.Element) -> tuple[list[float], list[str]]:
    sizes: set[float] = set()
    names: set[str] = set()
    for run in root.iter(_q("r")):
        rpr = run.find(_q("rPr"))
        if rpr is None:
            continue
        size = rpr.find(_q("sz"))
        if size is not None:
            try:
                sizes.add(int(size.attrib.get(_q("val"), "0")) / 2)
            except ValueError:
                pass
        fonts = rpr.find(_q("rFonts"))
        if fonts is not None:
            for key in ("ascii", "eastAsia", "hAnsi"):
                value = fonts.attrib.get(_q(key))
                if value:
                    names.add(value)
    return sorted(sizes), sorted(names)


def _paragraph_styles(root: ET.Element) -> list[dict]:
    """Dominant explicit font/size per paragraph; used by R17."""
    styles: list[dict] = []
    for index, paragraph in enumerate(root.iter(_q("p")), start=1):
        text = _text(paragraph)
        if not text:
            continue
        fonts: dict[str, int] = {}
        sizes: dict[float, int] = {}
        bold = False
        for run in paragraph.iter(_q("r")):
            rpr = run.find(_q("rPr"))
            if rpr is None:
                continue
            fonts_element = rpr.find(_q("rFonts"))
            if fonts_element is not None:
                for key in ("eastAsia", "ascii", "hAnsi"):
                    value = fonts_element.attrib.get(_q(key))
                    if value:
                        fonts[value] = fonts.get(value, 0) + 1
                        break
            size = rpr.find(_q("sz"))
            if size is not None:
                try:
                    points = int(size.attrib.get(_q("val"), "0")) / 2
                except ValueError:
                    points = 0
                if points:
                    sizes[points] = sizes.get(points, 0) + 1
            if rpr.find(_q("b")) is not None:
                bold = True
        styles.append(
            {
                "paragraph": index,
                "text": text[:80],
                "font": max(fonts, key=fonts.get) if fonts else "",
                "size": max(sizes, key=sizes.get) if sizes else None,
                "bold": bold,
            }
        )
    return styles


def _part_texts(zf: zipfile.ZipFile, prefix: str) -> list[str]:
    """Read header/footer parts in numeric order (header1, header2, ...)."""
    names = [name for name in zf.namelist() if name.startswith(prefix) and name.endswith(".xml")]
    names.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)) if re.search(r"(\d+)\.xml$", name) else 0)
    texts: list[str] = []
    for name in names:
        try:
            texts.append(_text(ET.fromstring(zf.read(name))))
        except ET.ParseError:
            continue
    return texts


def _header_number(text: str) -> str:
    match = re.search(r"报告编号\s*[:：]\s*([A-Za-z0-9\-]*)", text or "")
    return match.group(1).strip() if match else ""


def _footer_pages(text: str) -> tuple[list[int], list[int]]:
    pages = [int(value) for value in re.findall(r"第\s*(\d+)\s*页", text or "")]
    totals = [int(value) for value in re.findall(r"共\s*(\d+)\s*页", text or "")]
    return pages, totals


_TEMPLATE_CONFIG: dict | None = None


def _template_config() -> dict:
    global _TEMPLATE_CONFIG
    if _TEMPLATE_CONFIG is not None:
        return _TEMPLATE_CONFIG
    path = Path(__file__).resolve().parent.parent / "config" / "templates" / "byd_odor.yaml"
    if yaml is not None and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _TEMPLATE_CONFIG = loaded
                return _TEMPLATE_CONFIG
        except (OSError, yaml.YAMLError):
            pass
    _TEMPLATE_CONFIG = {
        "family": "BYD-odor",
        "match": {"basis_any": ["Q/BYDQ", "比亚迪"], "item_any": ["气味"]},
        "variants": {"OTS": {"label": "OTS", "page_totals": [5]}, "MOBAN": {"label": "摸底", "page_totals": [4]}},
    }
    return _TEMPLATE_CONFIG


def identify_template(basis: str, item: str, page_totals: list[int]) -> tuple[str, str]:
    """Return (family, variant label) from the configurable BYD mapping."""
    config = _template_config()
    match = config.get("match") or {}
    basis_any = [str(value) for value in match.get("basis_any") or []]
    item_any = [str(value) for value in match.get("item_any") or []]
    if not (any(value and value in (basis or "") for value in basis_any) or any(value and value in (item or "") for value in item_any)):
        return "generic", ""
    totals = set(page_totals or [])
    for spec in (config.get("variants") or {}).values():
        expected = set(spec.get("page_totals") or [])
        if expected and totals and totals <= expected:
            return str(config.get("family") or "BYD-odor"), str(spec.get("label") or "")
    return str(config.get("family") or "BYD-odor"), ""


def parse_docx(path: str | Path) -> ReportData:
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        xml_bytes = zf.read("word/document.xml")
        embedded_images = len([n for n in names if n.startswith("word/media/")])
        header_texts = _part_texts(zf, "word/header")
        footer_texts = _part_texts(zf, "word/footer")
    root = ET.fromstring(xml_bytes)
    paragraphs = _iter_paragraphs(root)
    tables = _iter_tables(root)
    body_text = "\n".join(paragraphs)
    xml_text = xml_bytes.decode("utf-8", errors="ignore")

    body_header_numbers: list[str] = []
    for line in paragraphs:
        if "报告编号" in line:
            match = re.search(r"报告编号\s*[:：]\s*([A-Za-z0-9\-]*)", line)
            body_header_numbers.append(match.group(1).strip() if match else "")
    # Prefer real header parts; fall back to body occurrences for drafts/templates
    # that put the number in the body instead of a Word header.
    if header_texts:
        header_numbers = [_header_number(text) for text in header_texts]
        cover_number = next((n for n in body_header_numbers if n), "") or next((n for n in header_numbers if n), "")
        body_numbers = header_numbers
    else:
        header_numbers = body_header_numbers
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
    result_cells = _expand_table(result_table) if result_table else []

    devices: list[dict] = []
    device_valid = ""
    for table in tables:
        if not table:
            continue
        header = [cell.text.strip() for cell in table[0]]
        if "溯源有效期" not in header:
            continue
        index = {name: position for position, name in enumerate(header)}

        def cell_value(row: list[Cell], name: str) -> str:
            position = index.get(name)
            if position is None or position >= len(row):
                return ""
            return row[position].text.strip()

        for row in table[1:]:
            item = {
                "device_id": cell_value(row, "设备编号"),
                "device_name": cell_value(row, "设备名称"),
                "device_model": cell_value(row, "设备型号"),
                "valid_until": cell_value(row, "溯源有效期"),
            }
            if any(item.values()):
                devices.append(item)
        if devices:
            device_valid = devices[0]["valid_until"]
        break

    page_totals = [int(m.group(1)) for m in re.finditer(r"共\s*(\d+)\s*页", body_text)]
    page_numbers = [int(m.group(1)) for m in re.finditer(r"第\s*(\d+)\s*页", body_text)]
    footer_page_numbers: list[int] = []
    footer_page_totals: list[int] = []
    for text in footer_texts:
        pages, totals = _footer_pages(text)
        footer_page_numbers.extend(pages)
        footer_page_totals.extend(totals)
    if footer_page_numbers:
        page_numbers = footer_page_numbers
    if footer_page_totals:
        page_totals = footer_page_totals

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
    toc_entries = _parse_toc(paragraphs)
    font_sizes, font_names = _style_summary(root)
    paragraph_styles = _paragraph_styles(root)
    pages = _iter_pages(root)
    bookmarks, bookmark_ends, bookmark_refs = _parse_bookmarks(root)
    external_images = xml_text.count("INCLUDEPICTURE")

    basis = summary.get("检验依据", "")
    item = summary.get("检验项目", "")
    template_family, template_variant = identify_template(basis, item, page_totals)

    return ReportData(
        file_name=path.name,
        source_format="docx",
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
        devices=devices,
        result_cells=result_cells,
        page_totals=page_totals,
        page_numbers=page_numbers,
        footer_pages=page_numbers,
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
        template_variant=template_variant,
        toc_entries=toc_entries,
        font_sizes=font_sizes,
        font_names=font_names,
        paragraphs=paragraphs,
        tables=tables,
        pages=pages,
        bookmarks=bookmarks,
        bookmark_ends=bookmark_ends,
        bookmark_refs=bookmark_refs,
        paragraph_styles=paragraph_styles,
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
