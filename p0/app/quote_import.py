"""Multi-format quote input extraction for the quote assistant.

Real customer inquiries reach the business manager as Excel workbooks, PDF
test outlines / quote sheets, and phone photos or screenshots of a test
requirement table.  This module turns those formats into either:

* a grid of rows (Excel sheets and PDF tables), consumed by the generic
  layout parser in :mod:`app.quote`; or
* :class:`RawQuoteRow` records when the source has no text layer and must go
  through the local RapidOCR path (images, scanned PDFs, images embedded in
  an Excel sheet).

Only deterministic extraction happens here: no price is invented, and every
OCR-derived row carries the OCR confidence plus review warnings.  Prices are
read from the document when present; otherwise the row is marked as needing a
price and the assistant never fabricates one.
"""
from __future__ import annotations

import re
from bisect import bisect_right as _bisect_right
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

SUPPORTED_SUFFIXES = {
    ".xlsx",
    ".xlsm",
    ".xls",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
}

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

METHOD_LABELS = {
    "excel": "Excel 表格",
    "excel-embedded-image": "Excel 内嵌图片（OCR）",
    "pdf-text": "PDF 文本表格",
    "pdf-ocr": "PDF 扫描件（OCR）",
    "image-ocr": "图片（OCR）",
}


@dataclass
class RawQuoteRow:
    """One quote line extracted from an OCR page before layout normalisation."""

    item: str
    standard: str = ""
    sample: str = ""
    unit: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    note: str = ""
    confidence: float | None = None


@dataclass
class Extraction:
    """Everything the parser needs from one uploaded file."""

    grids: list[tuple[str, list[list[str]]]] = field(default_factory=list)
    raw_rows: list[RawQuoteRow] = field(default_factory=list)
    method: str = ""
    kind: str = ""
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def method_label(self) -> str:
        return METHOD_LABELS.get(self.method, self.method)


# ---------------------------------------------------------------------------
# Header vocabulary (shared by the OCR band parser and the grid parser)
# ---------------------------------------------------------------------------
_HEADER_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("index", ("序号",)),
    ("sample", ("样品名称", "零件/总成名称", "零件/总成", "零件名称")),
    ("subprocess", ("测试子过程", "测试子项", "子过程")),
    ("item", ("试验项目", "测试项目", "检验项目", "实验项目", "检测项目")),
    ("standard", ("试验标准", "检验标准", "测试标准", "执行标准", "试验方法", "来源")),
    ("price", ("含税单价", "单价", "收费标准", "价格")),
    ("qty", ("试验材料数量", "样品数量", "测试次数", "试验数量", "数量", "Qty")),
    ("unit", ("单位",)),
    ("note", ("备注",)),
    ("total", ("小计", "总计", "合计")),
)

_HEADER_HINTS = tuple(keyword for _, keywords in _HEADER_KEYWORDS for keyword in keywords)
_ITEM_HINTS = ("试验项目", "测试项目", "检验项目", "实验项目", "检测项目")
ITEM_HINTS = _ITEM_HINTS

# Per-request admin overrides (rule configuration).  A ContextVar keeps the
# override scoped to one request/task instead of mutating the module globals.
_HEADER_ALIASES: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar(
    "quote_header_aliases", default=None
)


def _active_aliases() -> dict[str, tuple[str, ...]]:
    return _HEADER_ALIASES.get() or {}


def _header_keywords() -> tuple[tuple[str, tuple[str, ...]], ...]:
    overrides = _active_aliases()
    if not overrides:
        return _HEADER_KEYWORDS
    merged: list[tuple[str, tuple[str, ...]]] = []
    known: set[str] = set()
    for kind, keywords in _HEADER_KEYWORDS:
        extra = overrides.get(kind, ())
        merged.append((kind, tuple(dict.fromkeys((*keywords, *extra)))))
        known.add(kind)
    for kind, extra in overrides.items():
        if kind not in known and extra:
            merged.append((kind, tuple(extra)))
    return tuple(merged)


def _header_hints() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(hint for _, keywords in _header_keywords() for hint in keywords)
    )


def _item_hints() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys((*_ITEM_HINTS, *(_active_aliases().get("item") or ())))
    )


@contextmanager
def header_aliases(aliases: dict[str, list[str]] | None):
    """Apply admin-configured header aliases for this request only."""
    token = _HEADER_ALIASES.set(
        {key: tuple(values) for key, values in (aliases or {}).items() if values}
    )
    try:
        yield
    finally:
        _HEADER_ALIASES.reset(token)
_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+[、.．]")
_TOTAL_RE = re.compile(r"总计|合计|小计|含税总价|优惠后|价税合计")
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_UNIT_HINTS = ("套", "次", "件", "批", "小时", "h", "天", "人天", "人月", "材料", "种", "组", "台", "个", "份", "项", "席位", "袋", "样", "块", "支", "根", "米", "kg")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean(value: Any) -> str:
    text = _text(value).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean(value)
    if text in {"/", "-", "—", "无", "N/A", "n/a"}:
        return None
    match = _NUMBER_RE.search(text.replace("¥", "").replace("￥", "").replace("元", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _is_header_cell(text: str) -> bool:
    cleaned = _clean(text)
    if not cleaned or len(cleaned) > 28:
        return False
    return any(hint in cleaned for hint in _header_hints())


def _header_kind(text: str) -> str | None:
    """Return the semantic kind of a short header cell, if any.

    Only cells that *start* with a header keyword qualify: OCR line text such
    as ``能测试标准及要求》`` contains ``测试标准`` but is a data-cell
    continuation, not a header, and must not push the header band downward.
    """
    cleaned = _clean(text)
    if not cleaned or len(cleaned) > 40:
        return None
    # OCR sometimes prefixes a merged header cell with its 序号 ("1测试项目").
    probe = re.sub(r"^\d+[、.．]?\s*", "", cleaned)
    for kind, keywords in _header_keywords():
        for keyword in keywords:
            if probe.startswith(keyword):
                return kind
    return None


def _is_item_header(text: str) -> bool:
    cleaned = _clean(text)
    if not cleaned or len(cleaned) > 40:
        return False
    return any(hint in cleaned for hint in _item_hints())


def header_kind(text: str) -> str | None:
    """Public header classifier used by the generic grid parser."""
    return _header_kind(text)


def is_item_header(text: str) -> bool:
    """Public item-column header check used by the generic grid parser."""
    return _is_item_header(text)


def item_hints() -> tuple[str, ...]:
    """Active item-header hints, including admin-configured aliases."""
    return _item_hints()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def extract_quote_source(data: bytes, filename: str) -> Extraction:
    suffix = Path(filename).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return _extract_excel(data, filename)
    if suffix == ".pdf":
        return _extract_pdf(data, filename)
    if suffix in IMAGE_SUFFIXES:
        return _extract_image(data, filename)
    raise ValueError(
        "仅支持 Excel（.xlsx/.xlsm/.xls）、PDF 或图片（.png/.jpg/.jpeg/.webp/.bmp）报价输入"
    )


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def _xlsx_frames(data: bytes) -> tuple[list[tuple[str, list[list[str]]]], list[bytes]]:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(data), data_only=True)
    grids: list[tuple[str, list[list[str]]]] = []
    for sheet in workbook.worksheets[:8]:
        rows = _sheet_rows(sheet, max_rows=500, max_cols=40)
        grids.append((sheet.title, rows))
    return grids, _xlsx_media(data)


def _xls_frames(data: bytes) -> list[tuple[str, list[list[str]]]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    grids: list[tuple[str, list[list[str]]]] = []
    for index in range(min(book.nsheets, 8)):
        sheet = book.sheet_by_index(index)
        rows: list[list[str]] = []
        for row_index in range(min(sheet.nrows, 500)):
            row: list[str] = []
            for column in range(min(sheet.ncols, 40)):
                row.append(_cell_text(sheet.cell_value(row_index, column)))
            rows.append(row)
        grids.append((sheet.name, rows))
    return grids


def _sheet_rows(sheet: Any, max_rows: int, max_cols: int) -> list[list[str]]:
    rows: list[list[str]] = []
    max_row = min(sheet.max_row or 0, max_rows)
    max_col = min(sheet.max_column or 0, max_cols)
    for row in sheet.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        rows.append([_cell_text(cell.value) for cell in row])
    return rows


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _clean(value)


def _xlsx_media(data: bytes) -> list[bytes]:
    images: list[bytes] = []
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("xl/media/")
                and name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
            )
            for name in names:
                images.append(archive.read(name))
    except Exception:  # noqa: BLE001 - not every workbook is a valid zip
        return []
    return images


def _filtered_images(images: list[bytes], limit: int = 2) -> list[bytes]:
    """Keep table-sized images; drop logos/QR codes by pixel size."""
    kept: list[bytes] = []
    for raw in images:
        try:
            from PIL import Image

            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
        except Exception:  # noqa: BLE001 - optional image dependency
            continue
        if min(width, height) >= 200 and width * height >= 120_000:
            kept.append(raw)
        if len(kept) >= limit:
            break
    return kept


def _extract_excel(data: bytes, filename: str) -> Extraction:
    extraction = Extraction(method="excel", kind="excel", meta={"source": filename})
    try:
        if Path(filename).suffix.lower() == ".xls":
            grids = _xls_frames(data)
            media: list[bytes] = []
        else:
            grids, media = _xlsx_frames(data)
    except Exception as exc:  # noqa: BLE001 - parser boundary
        raise ValueError(f"无法读取 Excel：{exc}") from exc

    populated = [
        (name, rows) for name, rows in grids if sum(1 for row in rows if any(row)) >= 2
    ]
    extraction.grids = populated
    extraction.meta["sheets"] = [name for name, _ in grids]
    if len(populated) > 1:
        extraction.warnings.append(
            "工作簿含多个有内容的 sheet，已选择报价行最多的一张：" + "、".join(name for name, _ in populated)
        )

    if not populated:
        if not media:
            raise ValueError("Excel 中没有可解析的内容")
        return _ocr_embedded_images(extraction, media, filename)
    if media and not _grid_has_item_header(populated):
        # Some real inquiries keep the quote table as an embedded screenshot
        # while the sheet only holds a title and a note.
        try:
            return _ocr_embedded_images(extraction, media, filename)
        except ValueError:
            return extraction
    return extraction


def _ocr_embedded_images(
    extraction: Extraction, media: list[bytes], filename: str
) -> Extraction:
    images = _filtered_images(media)
    if not images:
        raise ValueError("Excel 只有图片且没有可识别的大图（疑似 logo 或二维码）")
    rows: list[RawQuoteRow] = []
    confidences: list[float] = []
    try:
        from . import ocr as ocr_service
    except Exception as exc:  # pragma: no cover - import boundary
        raise ValueError("OCR 依赖不可用") from exc
    for image in images:
        page = ocr_service.ocr_image(image)
        confidences.append(page.confidence)
        rows.extend(_rows_from_tokens(_tokens_from_ocr_lines(page.lines)))
    extraction.raw_rows = _dedupe_raw(rows)
    extraction.method = "excel-embedded-image"
    extraction.meta["images"] = len(images)
    extraction.meta["ocr_confidence"] = round(
        sum(confidences) / len(confidences), 4
    ) if confidences else None
    if not extraction.raw_rows:
        raise ValueError("Excel 内嵌图片未识别出报价行")
    extraction.warnings.append("报价表来自 Excel 内嵌图片，OCR 结果请复核")
    return extraction


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _extract_pdf(data: bytes, filename: str) -> Extraction:
    extraction = Extraction(method="pdf-text", kind="pdf", meta={"source": filename})
    try:
        grids, page_count = _pdf_table_grids(data)
    except Exception as exc:  # noqa: BLE001 - parser boundary
        raise ValueError(f"无法读取 PDF：{exc}") from exc

    extraction.meta["pages"] = page_count
    extraction.grids = grids
    if grids and _grid_has_item_header(grids):
        return extraction

    # No usable text table: render the first pages and run local OCR.
    try:
        from . import ocr as ocr_service

        ocr_result = ocr_service.ocr_pdf(data, max_pages=3)
    except Exception as exc:  # noqa: BLE001 - optional OCR boundary
        raise ValueError(f"PDF 没有可解析的表格，OCR 也不可用：{exc}") from exc
    rows: list[RawQuoteRow] = []
    for page in ocr_result.pages:
        rows.extend(_rows_from_tokens(_tokens_from_ocr_lines(page.lines)))
    extraction.raw_rows = _dedupe_raw(rows)
    extraction.method = "pdf-ocr"
    extraction.meta["ocr_confidence"] = ocr_result.avg_confidence
    extraction.meta["ocr_pages"] = len(ocr_result.pages)
    if page_count > len(ocr_result.pages):
        extraction.warnings.append(
            f"PDF 共 {page_count} 页，OCR 只处理前 {len(ocr_result.pages)} 页"
        )
    if not extraction.raw_rows:
        raise ValueError("PDF 未识别出报价表；请提供更清晰的表格或 Excel 版")
    extraction.warnings.append("PDF 为扫描件，报价行来自 OCR，请复核")
    return extraction


def _pdf_table_grids(data: bytes) -> tuple[list[tuple[str, list[list[str]]]], int]:
    import pdfplumber

    grids: list[tuple[str, list[list[str]]]] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for index, page in enumerate(pdf.pages[:8]):
            rows: list[list[str]] = []
            for table in page.extract_tables() or []:
                rows.extend([[_cell_text(cell) for cell in row] for row in table])
            if rows:
                grids.append((f"第 {index + 1} 页", rows))
    return grids, page_count


def _grid_has_item_header(grids: list[tuple[str, list[list[str]]]]) -> bool:
    for _, rows in grids:
        for row in rows[:40]:
            if any(_is_item_header(cell) for cell in row):
                return True
    return False


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def _extract_image(data: bytes, filename: str) -> Extraction:
    extraction = Extraction(method="image-ocr", kind="image", meta={"source": filename})
    try:
        from . import ocr as ocr_service

        page = ocr_service.ocr_image(data)
    except Exception as exc:  # noqa: BLE001 - optional OCR boundary
        raise ValueError(f"无法识别图片：{exc}") from exc
    extraction.raw_rows = _rows_from_tokens(_tokens_from_ocr_lines(page.lines))
    extraction.meta["ocr_confidence"] = page.confidence
    if not extraction.raw_rows:
        raise ValueError("图片中未识别出报价表；请确认图片包含表头与试验项目")
    extraction.warnings.append("图片报价行来自本地 OCR，请复核识别结果")
    return extraction


# ---------------------------------------------------------------------------
# OCR tokens -> quote rows (header-anchored band parser)
# ---------------------------------------------------------------------------
@dataclass
class _Token:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    confidence: float | None = None
    kind: str | None = None

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


def _split_mixed_script(token: _Token) -> list[_Token]:
    """Split an OCR box that merged an English label and a Chinese item name.

    Bilingual test outlines put e.g. ``Chemistry气味`` in one OCR box; the
    Chinese item name starts to the right of the English category and belongs
    to the next table column.  Proportional character widths recover the two
    sub-spans so the item column can be read.
    """
    text = token.text
    if not re.search(r"[\u4e00-\u9fff]", text) or not re.search(r"[A-Za-z]", text):
        return [token]
    runs: list[tuple[int, int, bool]] = []
    for index, char in enumerate(text):
        is_cjk = bool(re.match(r"[\u4e00-\u9fff]", char))
        if runs and runs[-1][2] == is_cjk and char.strip():
            runs[-1] = (runs[-1][0], index + 1, is_cjk)
        elif char.strip():
            runs.append((index, index + 1, is_cjk))
    if len(runs) < 2:
        return [token]
    total = max(len(text), 1)
    width = token.x1 - token.x0
    parts: list[_Token] = []
    for start, end, _is_cjk in runs:
        if end <= start:
            continue
        parts.append(
            _Token(
                text=text[start:end],
                x0=token.x0 + width * start / total,
                x1=token.x0 + width * end / total,
                y0=token.y0,
                y1=token.y1,
                confidence=token.confidence,
            )
        )
    return parts or [token]


def _tokens_from_ocr_lines(lines: list[Any]) -> list[_Token]:
    tokens: list[_Token] = []
    for line in lines:
        bbox = getattr(line, "bbox", None) or []
        if bbox:
            xs = [point[0] for point in bbox]
            ys = [point[1] for point in bbox]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
        else:  # pragma: no cover - OcrLine always carries a bbox
            x0 = float(getattr(line, "left", 0.0))
            x1 = x0 + 10.0
            y0 = float(getattr(line, "top", 0.0))
            y1 = y0 + 10.0
        token = _Token(
            text=_clean(getattr(line, "text", "")),
            x0=float(x0),
            x1=float(x1),
            y0=float(y0),
            y1=float(y1),
            confidence=float(getattr(line, "confidence", 0.0) or 0.0),
        )
        tokens.extend(_split_mixed_script(token))
    return [token for token in tokens if token.text]


def _split_merged_header(token: _Token) -> list[_Token]:
    """Split an OCR line that merged several header cells into sub-tokens."""
    found: list[tuple[int, int, str]] = []
    for kind, keywords in _header_keywords():
        for keyword in keywords:
            start = 0
            while True:
                index = token.text.find(keyword, start)
                if index < 0:
                    break
                found.append((index, index + len(keyword), kind))
                start = index + len(keyword)
    if not found:
        return [token]
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    chosen: list[tuple[int, int, str]] = []
    last_end = -1
    for match in found:
        if match[0] >= last_end:
            chosen.append(match)
            last_end = match[1]
    if len({kind for _, _, kind in chosen}) <= 1:
        single = found[0][2]
        return [_Token(token.text, token.x0, token.x1, token.y0, token.y1, token.confidence, single)]
    if len(chosen) < 2:
        return [token]
    total = max(len(token.text), 1)
    width = token.x1 - token.x0
    result: list[_Token] = []
    for index, (start, _end, kind) in enumerate(chosen):
        next_start = chosen[index + 1][0] if index + 1 < len(chosen) else total
        if next_start <= start:
            continue
        x0 = token.x0 + width * (start / total)
        x1 = token.x0 + width * (next_start / total)
        text = token.text[start:next_start].strip()
        result.append(_Token(text, x0, x1, token.y0, token.y1, token.confidence, kind))
    return result or [token]


def _visual_lines(tokens: list[_Token]) -> list[list[_Token]]:
    """Group OCR boxes into visual table lines (top to bottom)."""
    ordered = sorted(tokens, key=lambda token: (token.y0, token.x0))
    lines: list[list[_Token]] = []
    for token in ordered:
        center = (token.y0 + token.y1) / 2
        if lines:
            line_center = sum((item.y0 + item.y1) / 2 for item in lines[-1]) / len(lines[-1])
            if abs(center - line_center) <= max(6.0, token.height * 0.55):
                lines[-1].append(token)
                continue
        lines.append([token])
    for line in lines:
        line.sort(key=lambda item: item.x0)
    return lines


def _column_edges(lines: list[list[_Token]]) -> list[float]:
    """Infer column boundaries from the vertical gaps between table cells.

    Header text is often centred inside its column, so its left edge is not a
    reliable column anchor.  The gaps that repeat across rows are: a gap much
    wider than the median text height marks a cell boundary.
    """
    heights = [token.height for line in lines for token in line]
    if not heights:
        return []
    line_height = sorted(heights)[len(heights) // 2]
    minimum_gap = max(10.0, line_height * 0.7)
    midpoints: list[float] = []
    for line in lines:
        for left, right in zip(line, line[1:]):
            gap = right.x0 - left.x1
            if gap >= minimum_gap:
                midpoints.append((left.x1 + right.x0) / 2)
    midpoints.sort()
    clusters: list[list[float]] = []
    for midpoint in midpoints:
        if clusters and midpoint - clusters[-1][-1] <= 12:
            clusters[-1].append(midpoint)
        else:
            clusters.append([midpoint])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _column_index(x: float, edges: list[float]) -> int:
    return _bisect_right(edges, x)


def _plausible_item(text: str) -> bool:
    if not text or len(text) < 2:
        return False
    if _is_header_cell(text) or _is_skip_row(text):
        return False
    if _NUMBER_RE.fullmatch(text):
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]{2}", text):
        return False
    if text in {"序号", "试验类别", "测试项目", "检验项目", "实验项目", "测试子过程"}:
        return False
    if text.startswith("-") and len(text) < 8:
        return False
    return True


def _rows_from_tokens(tokens: list[_Token]) -> list[RawQuoteRow]:
    lines = _visual_lines(tokens)
    edges = _column_edges(lines)
    if not edges:
        return []

    # Header cells: split merged OCR boxes first, then map each cell to the
    # column that contains its centre.
    header_rows: list[list[_Token]] = []
    kinds_by_column: dict[int, list[str]] = {}
    spans_by_kind: dict[str, list[tuple[float, float]]] = {}
    for line in lines[:40]:
        line_has_header = False
        for token in line:
            if _header_kind(token.text) is None:
                continue
            for sub in _split_merged_header(token):
                if not sub.kind or not sub.text:
                    continue
                column = _column_index((sub.x0 + sub.x1) / 2, edges)
                kinds_by_column.setdefault(column, []).append(sub.kind)
                spans_by_kind.setdefault(sub.kind, []).append((sub.x0, sub.x1))
                line_has_header = True
        if line_has_header:
            header_rows.append(line)
    if not header_rows:
        return []

    def column_for(kind: str) -> int | None:
        matches = [column for column, kinds in kinds_by_column.items() if kind in kinds]
        return min(matches) if matches else None

    item_column = column_for("item")
    if item_column is None:
        item_column = column_for("subprocess")
    if item_column is None:
        return []

    item_spans = spans_by_kind.get("item") or spans_by_kind.get("subprocess") or []
    item_span = (
        (min(span[0] for span in item_spans), max(span[1] for span in item_spans))
        if item_spans
        else None
    )

    def is_item_token(token: _Token) -> bool:
        if item_span is None:
            return _column_index((token.x0 + token.x1) / 2, edges) == item_column
        # Use the token centre, not its left edge: a long item name can start
        # left of its column when it is centred in a wide cell, while an
        # adjacent category label can bleed into the column's left edge.
        centre = (token.x0 + token.x1) / 2
        return item_span[0] - 8 <= centre <= item_span[1] + 25

    def x_range_for(kind: str, left_margin: float = 100.0) -> tuple[float, float] | None:
        spans = spans_by_kind.get(kind)
        if not spans:
            return None
        span = min(spans, key=lambda item: item[0])
        start = span[0] - left_margin
        later = [other[0] for values in spans_by_kind.values() for other in values if other[0] > span[1]]
        end = min(later) - 2 if later else float("inf")
        return start, end

    standard_range = x_range_for("standard")

    def standard_of(line: list[_Token]) -> str:
        if standard_range is None:
            return ""
        start, end = standard_range
        selected = [
            token
            for token in line
            if not is_item_token(token) and start <= (token.x0 + token.x1) / 2 < end
        ]
        return _clean(_join_cell(selected))[:160]

    header_bottom = max(token.y1 for line in header_rows for token in line)

    rows: list[RawQuoteRow] = []
    previous_cells: dict[int, str] | None = None
    for line in lines:
        if line[0].y0 <= header_bottom:
            continue
        item_tokens = [token for token in line if is_item_token(token)]
        cells: dict[int, list[_Token]] = {}
        for token in line:
            if token in item_tokens:
                continue
            cells.setdefault(_column_index((token.x0 + token.x1) / 2, edges), []).append(token)
        cell_texts = {column: _join_cell(tokens) for column, tokens in cells.items()}
        item = _clean_item_label(_join_cell(item_tokens))
        if not _plausible_item(item):
            if previous_cells is not None:
                # Continuation of a multi-line cell (standard/requirement/note).
                for column, text in cell_texts.items():
                    if not text:
                        continue
                    previous_cells[column] = (
                        f"{previous_cells[column]} {text}".strip()
                        if previous_cells.get(column)
                        else text
                    )
            continue
        if previous_cells is not None and previous_cells.get(item_column) == item:
            # Same item repeated by a wrapped OCR line: merge the new cells.
            for column, text in cell_texts.items():
                if not text:
                    continue
                if not previous_cells.get(column):
                    previous_cells[column] = text
            continue
        row_cells = dict(cell_texts)
        row = RawQuoteRow(
            item=item,
            standard=standard_of(line),
            sample=_clean(row_cells.get(column_for("sample") or -1, ""))[:80],
            unit=_first_text_unit(row_cells.get(column_for("unit") or -1, "")),
            quantity=_first_number_text(row_cells.get(column_for("qty") or -1, "")),
            unit_price=_first_number_text(row_cells.get(column_for("price") or -1, "")),
            note=_clean(row_cells.get(column_for("note") or -1, ""))[:160],
            confidence=_avg_confidence(item_tokens),
        )
        rows.append(row)
        previous_cells = dict(cell_texts)
        previous_cells[item_column] = item
    return rows


def _first_number_text(text: str) -> float | None:
    if not text:
        return None
    return _number(text)


def _first_text_unit(text: str) -> str:
    if not text:
        return ""
    for part in text.replace("/", " ").split():
        if _number(part) is not None:
            continue
        if any(hint in part for hint in _UNIT_HINTS):
            return part[:12]
    return ""


def _join_cell(tokens: list[_Token] | None) -> str:
    if not tokens:
        return ""
    ordered = sorted(tokens, key=lambda token: (token.y0, token.x0))
    parts: list[str] = []
    for token in ordered:
        if parts and token.text in parts[-1]:
            continue
        parts.append(token.text)
    return _clean(" ".join(parts))


_ITEM_ASCII_LABEL_RE = re.compile(
    r"^(?:chemistry|category|test\s*type|test\s*item|standard|judgement|requirements)\b[\s|｜]*",
    re.IGNORECASE,
)
_ITEM_ASCII_TAIL_RE = re.compile(
    r"\s+(?:chemistry|category|test\s*type|test\s*item|standard|judgement|requirements)$",
    re.IGNORECASE,
)


def _clean_item_label(text: str) -> str:
    """Strip bilingual category labels that OCR merged into the item cell."""
    cleaned = _ITEM_ASCII_LABEL_RE.sub("", text)
    cleaned = _ITEM_ASCII_TAIL_RE.sub("", cleaned)
    return _clean(cleaned)


def _dedupe_raw(rows: list[RawQuoteRow]) -> list[RawQuoteRow]:
    seen: set[tuple[str, str, str]] = set()
    result: list[RawQuoteRow] = []
    for row in rows:
        key = (row.item, row.standard, row.unit)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _avg_confidence(tokens: list[_Token]) -> float | None:
    values = [token.confidence for token in tokens if token.confidence]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _is_skip_row(item: str) -> bool:
    if not item:
        return True
    if _TOTAL_RE.search(item):
        return True
    if _SECTION_RE.match(item):
        return True
    if item.startswith("注") and len(item) <= 30:
        return True
    return False
