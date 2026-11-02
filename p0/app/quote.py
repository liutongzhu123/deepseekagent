"""P0 quote assistant: quote input -> CRM draft + sales quote form.

Design rules (P0 ADR-03):
  * unit price, quantity and unit come from the uploaded document;
  * the assistant only maps item names/departments and calculates totals;
  * no model-generated price, no CRM write-back, no approval workflow.

Input layouts come from the customer's real materials:

  * a DVP sheet with an ``试验项目`` header and ``单价/数量/单位`` columns
    (front-seat DVP layout, including section-level price headers);
  * a quote sheet with ``样品名称/检验标准/检验项目/单价/数量/单位`` columns
    (the PDF quote sheet sent to customers);
  * a hierarchical test plan with a ``测试项目``/``测试子过程`` header
    (non-standard test plan);
  * a customer test outline or inquiry table that only carries item names,
    standards and quantities (no price) — parsed rows are marked as needing a
    price instead of inventing one.

Excel, PDF and image inputs are normalised by :mod:`app.quote_import`; images
and scanned PDFs go through the local OCR path and carry review warnings.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from . import quote_import
from .admin_config import DEFAULT_QUOTE as _DEFAULT_QUOTE
from .xlsx_writer import write_xlsx

SUPPORTED_SUFFIXES = quote_import.SUPPORTED_SUFFIXES
SUPPORTED_INPUT_HINT = (
    "仅支持 Excel（.xlsx/.xlsm/.xls）、PDF 或图片（.png/.jpg/.jpeg/.webp/.bmp）报价输入"
)

CRM_HEADERS = [column["label"] for column in _DEFAULT_QUOTE["crm_columns"]]

_UNIT_MAP = dict(_DEFAULT_QUOTE["unit_map"])
_CRM_COLUMNS = [dict(column) for column in _DEFAULT_QUOTE["crm_columns"]]
_HEADER_ALIASES = {
    key: tuple(values) for key, values in _DEFAULT_QUOTE["headers"].items()
}


@dataclass
class QuoteLine:
    sample_name: str
    item_name: str
    standard: str
    department: str
    unit: str
    quantity: float
    unit_price: float
    discount: float = 60.0
    note: str = ""
    source_row: int = 0
    needs_price: bool = False
    confidence: float | None = None

    def crm_note(self) -> str:
        note = self.note
        if self.needs_price:
            note = f"{note}；待补单价" if note else "待补单价"
        return note

    def to_crm_row(self) -> list[Any]:
        return [
            self.sample_name,
            self.item_name,
            self.item_name,
            self.standard,
            self.department,
            self.unit,
            self.quantity,
            "" if self.needs_price else self.unit_price,
            "是",
            self.discount,
            self.crm_note(),
        ]


PRODUCT_TAX_RATE = _DEFAULT_QUOTE["product_tax_rate"]
SERVICE_TAX_RATE = _DEFAULT_QUOTE["service_tax_rate"]


class QuoteSelfCheckError(ValueError):
    """Raised when the generated 销售报价表 does not match the calculation."""

    def __init__(self, mismatches: list[dict]):
        self.mismatches = mismatches
        detail = "；".join(
            f"{item['cell']} {item['field']}：表内={item['actual']!r}，应为={item['expected']!r}"
            for item in mismatches
        )
        super().__init__(f"销售报价表自检失败：{detail}")


@dataclass
class QuoteResult:
    source_file: str
    sample_name: str
    lines: list[QuoteLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    discount: float = 60.0
    product_amount: float = 0.0
    incurred_cost: float = 0.0
    parse_method: str = ""
    parse_meta: dict[str, Any] = field(default_factory=dict)
    product_tax_rate: float = PRODUCT_TAX_RATE
    service_tax_rate: float = SERVICE_TAX_RATE

    @property
    def priced_lines(self) -> int:
        return sum(1 for line in self.lines if not line.needs_price)

    @property
    def unpriced_lines(self) -> int:
        return sum(1 for line in self.lines if line.needs_price)

    @property
    def all_priced(self) -> bool:
        return bool(self.lines) and self.unpriced_lines == 0

    @property
    def subtotal(self) -> float:
        return round(
            sum(
                line.quantity * line.unit_price
                for line in self.lines
                if not line.needs_price
            ),
            2,
        )

    @property
    def sales_amount(self) -> float:
        return round(self.subtotal * self.discount / 100.0, 2)

    @property
    def service_amount(self) -> float:
        """Service revenue = sales quote minus any product amount."""
        return round(self.sales_amount - self.product_amount, 2)

    @property
    def product_tax(self) -> float:
        return round(self.product_amount * self.product_tax_rate, 2)

    @property
    def service_tax(self) -> float:
        return round(self.service_amount * self.service_tax_rate, 2)

    @property
    def total_with_tax(self) -> float:
        return round(self.sales_amount + self.product_tax + self.service_tax, 2)

    @property
    def final_price(self) -> float:
        return round(self.total_with_tax + self.incurred_cost, 2)

    def summary(self) -> dict:
        return {
            "source_file": self.source_file,
            "sample_name": self.sample_name,
            "lines": len(self.lines),
            "priced_lines": self.priced_lines,
            "unpriced_lines": self.unpriced_lines,
            "all_priced": self.all_priced,
            "parse_method": self.parse_method,
            "subtotal": self.subtotal,
            "discount_percent": self.discount,
            "sales_amount": self.sales_amount,
            "product_amount": self.product_amount,
            "service_amount": self.service_amount,
            "product_tax_rate": self.product_tax_rate,
            "product_tax": self.product_tax,
            "service_tax_rate": self.service_tax_rate,
            "service_tax": self.service_tax,
            "total_with_tax": self.total_with_tax,
            "incurred_cost": self.incurred_cost,
            "final_price": self.final_price,
            "warnings": self.warnings,
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "parse_meta": self.parse_meta,
            "items": [asdict(line) for line in self.lines],
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean_item(value: Any) -> str:
    text = _text(value).replace("\n", " ")
    text = re.sub(r"^[\/／、]+", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    # PDF cell extraction often inserts a space at a line break inside a
    # Chinese item name; join those without leaving a visible gap.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[¥￥,\s元]", "", str(value))
    try:
        return float(text)
    except ValueError:
        return None


def _unit(value: Any, unit_map: dict[str, str] | None = None) -> str:
    text = _text(value)
    mapping = unit_map or _UNIT_MAP
    return mapping.get(text, text or "次")


def _header_aliases(headers: dict[str, list[str]] | None, key: str) -> tuple[str, ...]:
    if headers and headers.get(key):
        return tuple(headers[key])
    return _HEADER_ALIASES.get(key, ())


def _quote_rates(rule_config: dict | None) -> tuple[float, float]:
    config = rule_config or {}
    return (
        float(config.get("product_tax_rate", PRODUCT_TAX_RATE)),
        float(config.get("service_tax_rate", SERVICE_TAX_RATE)),
    )


def _quote_discount(rule_config: dict | None, discount: float | None) -> float:
    if discount is not None:
        return discount
    return float((rule_config or {}).get("default_discount", 60.0))


def _department(item: str, standard: str = "") -> str:
    if any(word in item for word in ("散发", "气味", "VOC", "voc", "雾化", "禁用物质", "禁限物质")):
        return "理化分析试验研究部"
    if "电磁" in item or "电气" in standard:
        return "智能电器试验研究部"
    return "车身附件试验研究部"


def _find_layout(
    ws, headers: dict[str, list[str]] | None = None
) -> tuple[int, int, int, int, int, int] | None:
    """Return (item_row, item_col, price_row, price_col, qty_col, unit_col)."""
    item_aliases = _header_aliases(headers, "item")
    price_aliases = _header_aliases(headers, "price")
    quantity_aliases = set(_header_aliases(headers, "quantity"))
    unit_aliases = set(_header_aliases(headers, "unit"))
    item_row = item_col = None
    # Alias order is priority: a sheet that has both 试验项目 and 测试项目
    # keeps using 试验项目, matching the pre-config behaviour.
    for alias in item_aliases:
        for row in range(1, min(ws.max_row, 30) + 1):
            for col in range(1, min(ws.max_column, 30) + 1):
                if alias in _text(ws.cell(row, col).value):
                    item_row, item_col = row, col
                    break
            if item_col:
                break
        if item_col:
            break
    if item_row is None or item_col is None:
        return None

    price_row = price_col = qty_col = unit_col = None
    for alias in price_aliases:
        for row in range(item_row, min(item_row + 6, ws.max_row) + 1):
            for col in range(1, min(ws.max_column, 30) + 1):
                if _text(ws.cell(row, col).value) == alias:
                    price_row, price_col = row, col
                    break
            if price_col:
                break
        if price_col:
            break
    if price_col is None:
        return None
    # Quantity/unit headers may appear once in the sample block (left of the
    # price columns) and again in the price block; only accept columns to the
    # right of 单价.
    for row in range(price_row or item_row, min((price_row or item_row) + 2, ws.max_row) + 1):
        for col in range(price_col + 1, min(ws.max_column, 40) + 1):
            value = _text(ws.cell(row, col).value)
            if value in quantity_aliases and qty_col is None:
                qty_col = col
            if value in unit_aliases and unit_col is None:
                unit_col = col
        if qty_col and unit_col:
            break
    qty_col = qty_col or price_col + 1
    unit_col = unit_col or price_col + 2
    return item_row, item_col, price_row or item_row, price_col, qty_col, unit_col


def parse_dvp(
    path: str | Path,
    discount: float | None = None,
    source_name: str | None = None,
    rule_config: dict | None = None,
) -> QuoteResult:
    rule_config = rule_config or {}
    headers = rule_config.get("headers")
    unit_map = rule_config.get("unit_map")
    discount = _quote_discount(rule_config, discount)
    product_rate, service_rate = _quote_rates(rule_config)
    path = Path(path)
    display_name = source_name or path.name
    workbook = load_workbook(path, data_only=True)
    # Prefer a DVP sheet; fall back to the first non-empty sheet.
    ws = next((sheet for sheet in workbook.worksheets if "座椅" in sheet.title or "DVP" in sheet.title), workbook.worksheets[0])

    layout = _find_layout(ws, headers)
    if layout is None:
        raise ValueError("未找到「试验项目 + 单价/数量/单位」表头，无法解析 DVP")
    item_row, item_col, price_row, price_col, qty_col, unit_col = layout

    sample_aliases = _header_aliases(headers, "sample")
    sample_name = ""
    for row in range(1, max(item_row - 1, 1) + 1):
        for col in range(1, min(ws.max_column, 20) + 1):
            value = _text(ws.cell(row, col).value)
            if any(alias in value for alias in sample_aliases):
                sample_name = value.split("：", 1)[-1].split(":", 1)[-1].strip()
                break
        if sample_name:
            break
    if not sample_name:
        sample_name = re.sub(r"^\d+[、.．]\s*", "", ws.title) or path.stem
    prefix = re.match(r"([A-Za-z]{2,}\d+)", Path(display_name).stem)
    if prefix and not re.match(r"[A-Za-z]", sample_name):
        sample_name = prefix.group(1) + sample_name

    result = QuoteResult(
        source_file=display_name,
        sample_name=sample_name,
        discount=discount,
        product_tax_rate=product_rate,
        service_tax_rate=service_rate,
    )
    last_item = ""
    for row in range(item_row + 2, min(item_row + 200, ws.max_row + 1)):
        item = _clean_item(ws.cell(row, item_col).value)
        if not item:
            continue
        if item == "总计":
            break
        if "备注" in item or item.startswith("注"):
            continue
        price = _number(ws.cell(row, price_col).value)
        qty = _number(ws.cell(row, qty_col).value)
        unit = _unit(ws.cell(row, unit_col).value, unit_map)
        if price is None or qty is None:
            result.warnings.append(f"第 {row} 行「{item}」缺少单价或数量，已跳过")
            continue
        standard_col = item_col - 1
        standard = _text(ws.cell(row, standard_col).value).replace("\n", " ").strip()
        if not standard:
            standard = f"{sample_name}DVP"
        # Keep the DVP name in the CRM standard column (matches the customer's
        # real CRM export); the underlying standard number is still in DVP.
        result.lines.append(
            QuoteLine(
                sample_name=sample_name,
                item_name=item,
                standard=f"{sample_name}DVP",
                department=_department(item, standard),
                unit=unit,
                quantity=qty,
                unit_price=price,
                discount=discount,
                note="",
                source_row=row,
            )
        )
        last_item = item

    if not result.lines:
        raise ValueError("DVP 中没有可报价的试验项目")
    if last_item and len(result.lines) < 2:
        result.warnings.append("只解析到 1 行，请检查表头/合并单元格")
    result.parse_method = "Excel 表格（DVP）"
    return result


_TOTAL_CELL_RE = re.compile(r"总计|合计|小计|含税总价|优惠后|价税合计")
_SECTION_HEADER_RE = re.compile(r"^[一二三四五六七八九十]+[、.．]")
_ITEM_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\s*[、.．]?|[（(]\d+[)）])\s*")
_SAMPLE_RE = re.compile(r"(?:零件/总成(?:名称)?|样品名称|产品名称|零部件名称)[：:]\s*(.+)")
_FILE_PREFIX_RE = re.compile(r"([A-Za-z]{2,}\d+)")


def _strip_item_prefix(item: str) -> str:
    text = _ITEM_PREFIX_RE.sub("", item, count=1).strip()
    return text or item


def _cell_text(row: list[Any], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return _text(row[index])


def _row_number(row: list[Any], index: int | None) -> float | None:
    return _number(_cell_text(row, index))


def _header_candidates(row: list[Any]) -> dict[str, list[tuple[int, int]]]:
    found: dict[str, list[tuple[int, int]]] = {}
    for index, cell in enumerate(row):
        text = _text(cell)
        if not text or len(text) > 28:
            continue
        kind = quote_import.header_kind(text)
        if kind:
            found.setdefault(kind, []).append((index, len(text)))
    return found


def _pick_columns(found: dict[str, list[tuple[int, int]]]) -> dict[str, int]:
    """Pick one column per semantic kind.

    Quantity/unit columns are preferred to the right of the price column, so a
    DVP's sample-count column on the left is not mistaken for the billing
    quantity.  Standard/sample columns choose the shortest header text to skip
    descriptive headers such as ``试验方法标准和试验方法描述``.
    """
    price = found["price"][0][0] if found.get("price") else None

    def pick(kind: str, preferred: bool = False) -> int | None:
        items = [column for column, _ in found.get(kind, [])]
        if not items:
            return None
        if preferred and price is not None:
            right = [column for column in items if column > price]
            if right:
                return min(right)
        if kind == "standard":
            return min(found[kind], key=lambda item: item[1])[0]
        return min(items)

    return {
        "item": pick("item"),
        "subprocess": pick("subprocess"),
        "standard": pick("standard"),
        "price": price,
        "qty": pick("qty", preferred=True),
        "unit": pick("unit", preferred=True),
        "sample": pick("sample"),
        "note": pick("note"),
    }


def _find_item_header(rows: list[list[Any]]) -> tuple[int, int] | None:
    best: tuple[tuple[int, int, int, int], int, int] | None = None
    for row_index, row in enumerate(rows[:40]):
        for column, cell in enumerate(row):
            text = _text(cell)
            if not text or len(text) > 40 or not quote_import.is_item_header(text):
                continue
            positions = [text.find(hint) for hint in quote_import.item_hints() if hint in text]
            position = min(positions) if positions else 99
            score = (position, len(text), row_index, column)
            if best is None or score < best[0]:
                best = (score, row_index, column)
    return (best[1], best[2]) if best else None


def _is_header_row(row: list[Any]) -> bool:
    found = _header_candidates(row)
    if not found:
        return False
    if "item" in found:
        return len(found) >= 2
    # A second header block (e.g. a DVP section that repeats 单价/数量/单位)
    # looks like a data row to the item check; treat it as a header so the
    # loop updates the column map instead of reading it as data or a total.
    return sum(1 for kind in ("price", "qty", "unit") if kind in found) >= 2


def _row_is_total(row: list[Any]) -> bool:
    for cell in row:
        text = _text(cell)
        if text and len(text) <= 16 and _TOTAL_CELL_RE.search(text):
            return True
    return False


def _sample_pattern(sample_aliases: list[str] | tuple[str, ...] | None):
    if not sample_aliases:
        return _SAMPLE_RE
    escaped = "|".join(re.escape(alias) for alias in sample_aliases)
    return re.compile(r"(?:" + escaped + r")[：:]\s*(.+)")


def _detect_sample(
    rows: list[list[Any]],
    header_row: int,
    sample_col: int | None,
    filename: str,
    sample_aliases: list[str] | tuple[str, ...] | None = None,
) -> str:
    if sample_col is not None:
        for row in rows[header_row + 1 : header_row + 4]:
            value = _clean_item(_cell_text(row, sample_col))
            if value:
                return value
    pattern = _sample_pattern(sample_aliases)
    for row in rows[: min(header_row + 1, len(rows))]:
        for cell in row:
            match = pattern.search(_text(cell))
            if not match:
                continue
            value = re.split(r"[\n（(]|\s+(?=[A-Za-z])", match.group(1), maxsplit=1)[0]
            value = _clean_item(value)
            if value:
                return value
    return _sample_from_filename(filename)


def _sample_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^(?:\d+[_\-、.．]*)+", "", stem).strip()
    return stem or Path(filename).stem


def _with_file_prefix(sample: str, filename: str) -> str:
    match = _FILE_PREFIX_RE.match(Path(filename).stem)
    if match and not re.match(r"[A-Za-z]", sample):
        return f"{match.group(1)}{sample}"
    return sample


def _parse_grid(
    rows: list[list[Any]],
    filename: str,
    discount: float,
    rule_config: dict | None = None,
) -> QuoteResult | None:
    quote_cfg = rule_config or {}
    headers = quote_cfg.get("headers") or {}
    unit_map = quote_cfg.get("unit_map")
    product_rate, service_rate = _quote_rates(quote_cfg)
    header = _find_item_header(rows)
    if header is None:
        return None
    header_row, item_col = header

    found: dict[str, list[tuple[int, int]]] = {}
    for row in rows[header_row : min(header_row + 4, len(rows))]:
        for kind, columns in _header_candidates(row).items():
            found.setdefault(kind, []).extend(columns)
    columns = _pick_columns(found)

    price_col = columns["price"]
    qty_col = columns["qty"]
    unit_col = columns["unit"]
    standard_col = columns["standard"]
    sample_col = columns["sample"]
    sub_col = columns["subprocess"]
    note_col = columns["note"]

    sample_name = _with_file_prefix(
        _detect_sample(
            rows, header_row, sample_col, filename, headers.get("sample")
        ),
        filename,
    )
    is_dvp = "DVP" in Path(filename).name.upper()

    result = QuoteResult(
        source_file=filename,
        sample_name=sample_name,
        discount=discount,
        product_tax_rate=product_rate,
        service_tax_rate=service_rate,
    )
    warnings: list[str] = []
    qty_defaulted = False
    last_line: QuoteLine | None = None

    for row_index in range(header_row + 1, min(len(rows), header_row + 301)):
        row = rows[row_index]
        if not any(_text(cell) for cell in row):
            continue
        if _is_header_row(row):
            # A repeated header block (page 2 of a PDF, or a DVP section that
            # repeats 单价/数量/单位 further down the sheet) refreshes the
            # column map and is never quoted as a row.
            late = _header_candidates(row)
            if sum(1 for kind in ("price", "qty", "unit") if kind in late) >= 2:
                late_columns = _pick_columns(late)
                price_col = late_columns["price"] if late_columns["price"] is not None else price_col
                qty_col = late_columns["qty"] if late_columns["qty"] is not None else qty_col
                unit_col = late_columns["unit"] if late_columns["unit"] is not None else unit_col
                standard_col = late_columns["standard"] if late_columns["standard"] is not None else standard_col
            continue
        if _row_is_total(row):
            break

        item = _clean_item(_cell_text(row, item_col))
        if sub_col is not None:
            subprocess = _clean_item(_cell_text(row, sub_col))
            if (
                subprocess
                and not quote_import.is_item_header(subprocess)
                and not _SECTION_HEADER_RE.match(subprocess)
                and not _TOTAL_CELL_RE.search(subprocess)
            ):
                item = subprocess
        if not item:
            # Price-only continuation row: a merged cell put the item on the
            # row above while 单价/数量/单位 landed on this row.
            if last_line is not None and last_line.needs_price:
                price = _row_number(row, price_col)
                quantity = _row_number(row, qty_col)
                if price is not None or quantity is not None:
                    if price is not None:
                        last_line.unit_price = price
                        last_line.needs_price = False
                    if quantity is not None:
                        last_line.quantity = quantity
                    unit_text = _unit(_cell_text(row, unit_col), unit_map) if unit_col is not None else ""
                    if unit_text:
                        last_line.unit = unit_text
                    note = _clean_item(_cell_text(row, note_col))
                    if note:
                        last_line.note = f"{last_line.note}；{note}" if last_line.note else note
            continue
        if _TOTAL_CELL_RE.search(item) or _SECTION_HEADER_RE.match(item):
            continue
        if item.startswith("备注") or item.startswith("注："):
            continue
        if item in {"序号", "试验项目", "测试项目", "检验项目"}:
            continue

        price = _row_number(row, price_col)
        quantity = _row_number(row, qty_col)
        unit_text = _unit(_cell_text(row, unit_col), unit_map) if unit_col is not None else ""

        sample = _clean_item(_cell_text(row, sample_col)) if sample_col is not None else ""
        if not sample:
            sample = sample_name
        if sample != sample_name:
            sample = _with_file_prefix(sample, filename)
        standard = _clean_item(_cell_text(row, standard_col)) if standard_col is not None else ""
        if is_dvp:
            # Match the customer's real CRM export: a DVP row carries the DVP
            # name in the CRM standard column, not the underlying standard no.
            standard = f"{sample}DVP"
        elif not standard:
            standard = "测试大纲"
        note = _clean_item(_cell_text(row, note_col)) if note_col is not None else ""
        if quantity is None:
            quantity = 1.0
            if not qty_defaulted:
                warnings.append("部分明细没有数量列或数量为空，按 1 计")
                qty_defaulted = True

        line = QuoteLine(
            sample_name=sample,
            item_name=item,
            standard=standard,
            department=_department(item, standard),
            unit=unit_text or "次",
            quantity=quantity,
            unit_price=price if price is not None else 0.0,
            discount=discount,
            note=note,
            source_row=row_index + 1,
            needs_price=price is None,
        )
        result.lines.append(line)
        last_line = line

    if not result.lines:
        return None
    result.warnings.extend(warnings)
    return result


def _result_from_raw(
    raw_rows: list[quote_import.RawQuoteRow],
    filename: str,
    discount: float,
    rule_config: dict | None = None,
) -> QuoteResult:
    quote_cfg = rule_config or {}
    unit_map = quote_cfg.get("unit_map")
    product_rate, service_rate = _quote_rates(quote_cfg)
    sample_name = _sample_from_filename(filename)
    result = QuoteResult(
        source_file=filename,
        sample_name=sample_name,
        discount=discount,
        product_tax_rate=product_rate,
        service_tax_rate=service_rate,
    )
    seen: set[tuple[str, str]] = set()
    low_confidence = 0
    for index, raw in enumerate(raw_rows, start=1):
        item = _strip_item_prefix(_clean_item(raw.item))
        if not item or _TOTAL_CELL_RE.search(item):
            continue
        standard = _clean_item(raw.standard) or "测试大纲"
        key = (item, standard)
        if key in seen:
            continue
        seen.add(key)
        sample = _clean_item(raw.sample) or sample_name
        if raw.confidence is not None and raw.confidence < 0.85:
            low_confidence += 1
        result.lines.append(
            QuoteLine(
                sample_name=sample,
                item_name=item,
                standard=standard,
                department=_department(item, standard),
                unit=_unit(raw.unit, unit_map) if raw.unit else "次",
                quantity=float(raw.quantity) if raw.quantity else 1.0,
                unit_price=float(raw.unit_price) if raw.unit_price is not None else 0.0,
                discount=discount,
                note=_clean_item(raw.note),
                source_row=index,
                needs_price=raw.unit_price is None,
                confidence=raw.confidence,
            )
        )
    if low_confidence:
        result.warnings.append(f"{low_confidence} 行 OCR 置信度偏低，请重点复核")
    if result.lines and result.unpriced_lines == len(result.lines):
        result.warnings.append("输入中没有单价列，全部明细需要补充单价后再生成销售报价表")
    return result


def _import_header_aliases(headers: dict[str, list[str]] | None) -> dict[str, tuple[str, ...]]:
    if not headers:
        return {}
    mapping: dict[str, tuple[str, ...]] = {}
    for key in ("item", "price", "quantity", "unit", "sample"):
        values = headers.get(key) or []
        if not values:
            continue
        mapping["qty" if key == "quantity" else key] = tuple(values)
    return mapping


def parse_quote_bytes(
    data: bytes,
    filename: str,
    discount: float | None = None,
    rule_config: dict | None = None,
) -> QuoteResult:
    """Parse any supported quote input (Excel / PDF / image) from raw bytes."""
    rule_config = rule_config or {}
    discount = _quote_discount(rule_config, discount)
    headers = rule_config.get("headers")
    with quote_import.header_aliases(_import_header_aliases(headers)):
        extraction = quote_import.extract_quote_source(data, filename)
        candidates: list[tuple[QuoteResult, str]] = []

        if extraction.kind == "pdf" and extraction.grids:
            combined: list[list[str]] = []
            for _, grid in extraction.grids:
                combined.extend(grid)
            parsed = _parse_grid(combined, filename, discount, rule_config)
            if parsed is not None:
                candidates.append((parsed, extraction.method_label))
        elif extraction.grids:
            for sheet, grid in extraction.grids:
                parsed = _parse_grid(grid, filename, discount, rule_config)
                if parsed is not None:
                    candidates.append((parsed, f"{extraction.method_label}（{sheet}）"))

        if extraction.raw_rows:
            parsed = _result_from_raw(
                extraction.raw_rows, filename, discount, rule_config
            )
            if parsed.lines:
                candidates.append((parsed, extraction.method_label))

    if not candidates:
        raise ValueError(
            "未识别到报价表：需要包含「试验项目/检验项目/测试项目」表头，"
            "以及单价、数量、单位或样品名称等列"
        )

    result, method_label = max(candidates, key=lambda item: len(item[0].lines))
    result.parse_method = method_label
    result.parse_meta = {
        **extraction.meta,
        "parse_method": method_label,
        "candidates": len(candidates),
    }
    result.warnings = extraction.warnings + result.warnings
    if result.unpriced_lines:
        result.warnings.append(
            f"{result.unpriced_lines}/{len(result.lines)} 行没有单价，CRM 草稿已标注「待补单价」"
        )
    return result


def parse_quote(
    path: str | Path,
    discount: float | None = None,
    source_name: str | None = None,
    rule_config: dict | None = None,
) -> QuoteResult:
    path = Path(path)
    data = path.read_bytes()
    return parse_quote_bytes(
        data,
        source_name or path.name,
        discount=discount,
        rule_config=rule_config,
    )


_CRM_WIDTHS = {
    "sample_name": 18,
    "item_name": 34,
    "standard": 24,
    "department": 22,
    "unit": 10,
    "quantity": 8,
    "unit_price": 12,
    "has_discount": 10,
    "discount": 8,
    "note": 26,
}


def _crm_value(line: QuoteLine, source: str) -> Any:
    if source == "has_discount":
        return "是"
    if source == "unit_price":
        return "" if line.needs_price else line.unit_price
    if source == "note":
        return line.crm_note()
    return getattr(line, source, "")


def write_crm_xlsx(
    path: str | Path, result: QuoteResult, columns: list[dict] | None = None
) -> Path:
    active = [
        column
        for column in (columns or _CRM_COLUMNS)
        if isinstance(column, dict) and column.get("enabled", True)
    ] or list(_CRM_COLUMNS)
    rows: list[list[Any]] = [[column["label"] for column in active]] + [
        [_crm_value(line, column["source"]) for column in active]
        for line in result.lines
    ]
    summary = [
        ["报价汇总", ""],
        ["样品名称", result.sample_name],
        ["明细行数", len(result.lines)],
        ["含单价行数", f"{result.priced_lines}/{len(result.lines)}"],
        ["解析方式", result.parse_method or "Excel"],
        ["DVP 合计（未折扣）", result.subtotal],
        ["折扣（%）", result.discount],
        ["销售报价（不含税）", result.sales_amount],
        ["服务税率", f"{result.service_tax_rate:.0%}"],
        ["税额", result.service_tax],
        ["税价合计", result.total_with_tax],
    ]
    return write_xlsx(
        path,
        [
            ("CRM报价草稿", rows, [_CRM_WIDTHS.get(column["source"], 16) for column in active]),
            ("报价汇总", summary, [28, 24]),
        ],
    )


SALES_QUOTE_CELLS = {
    "C7": ("解决方案部建议最低报价（不含税）", "sales_amount"),
    "C8": ("销售报价（不含税）", "sales_amount"),
    "C9": ("产品金额（不含税）", "product_amount"),
    "C10": ("服务金额（不含税）", "service_amount"),
    "C11": ("产品税额", "product_tax"),
    "C12": ("服务税额", "service_tax"),
    "C13": ("税价合计", "total_with_tax"),
    "C14": ("产生费用", "incurred_cost"),
    "C15": ("最终价格", "final_price"),
}

# (cell, label, expression, inputs) for the audit sheet.  The template's own
# formulas are listed as the provenance of each value.
SALES_QUOTE_FORMULAS = [
    ("C8", "销售报价（不含税）", "DVP 明细合计 × 折扣", "报价明细!H 列合计"),
    ("C9", "产品金额（不含税）", "—", "纯检测服务时为 0"),
    ("C10", "服务金额（不含税）", "C8-C9", "C8、C9"),
    ("C11", "产品税额", "C9*B11", "C9、B11=16%"),
    ("C12", "服务税额", "C10*B12", "C10、B12=6%"),
    ("C13", "税价合计", "C8+C11+C12", "C8、C11、C12"),
    ("C14", "产生费用", "—", "默认 0"),
    ("C15", "最终价格", "C13+C14", "C13、C14"),
]


def _write_value_sheet(workbook, original_formulas: dict[str, str], result: QuoteResult) -> None:
    """Add an audit sheet that keeps every amount traceable to its formula."""
    sheet = workbook.create_sheet("计算口径")
    sheet.append(["字段", "单元格", "模板原公式", "计算口径", "输入", "写入值"])
    for cell, (label, attr) in SALES_QUOTE_CELLS.items():
        formula = next((f for c, _, f, _ in SALES_QUOTE_FORMULAS if c == cell), "—")
        inputs = next((i for c, _, _, i in SALES_QUOTE_FORMULAS if c == cell), "—")
        sheet.append(
            [
                label,
                cell,
                original_formulas.get(cell, ""),
                formula,
                inputs,
                getattr(result, attr),
            ]
        )
    sheet.append([])
    sheet.append(["说明", "本表金额已写入实际单元格；模板原公式保留在本列，便于复核。"])
    # Keep the template formulas as text, not as live formulas in the audit
    # sheet (openpyxl otherwise treats a leading "=" as a formula).
    for row in sheet.iter_rows(min_row=2):
        formula_cell = row[2]
        if isinstance(formula_cell.value, str) and formula_cell.value.startswith("="):
            formula_cell.data_type = "s"


def fill_sales_quote(template: str | Path, result: QuoteResult, out: str | Path, meta: dict | None = None) -> Path:
    """Fill the customer 销售报价表 and write every amount into its real cell.

    The template's formulas are captured first, then replaced by the computed
    numeric values so any reader (including openpyxl ``data_only``) sees the
    amount without relying on Excel recalculation.  The original formulas stay
    visible on the 计算口径 sheet.
    """
    meta = meta or {}
    if result.unpriced_lines:
        raise ValueError(
            f"{result.unpriced_lines} 行缺少单价，不能生成含金额的销售报价表；请先补充单价"
        )
    workbook = load_workbook(template)
    ws = workbook.active
    original_formulas = {
        cell: str(ws[cell].value) if ws[cell].data_type == "f" else ""
        for cell in SALES_QUOTE_CELLS
    }

    ws["A2"] = meta.get("date") or "2026年11月02日"
    ws["B3"] = meta.get("applicant", "业务经理")
    ws["D3"] = meta.get("region", "华北")
    ws["B4"] = meta.get("project_name", f"{result.sample_name} DVP 试验报价")
    ws["B5"] = meta.get("customer", "客户名称")
    ws["B6"] = meta.get("contract", f"{result.sample_name} DVP（闭口合同）")
    for cell, (_, attr) in SALES_QUOTE_CELLS.items():
        ws[cell] = getattr(result, attr)

    detail = workbook.create_sheet("报价明细")
    headers = ["序号", "试验项目", "检验科室", "单位", "数量", "含税单价", "折扣(%)", "折后金额"]
    detail.append(headers)
    for index, line in enumerate(result.lines, start=1):
        detail.append(
            [
                index,
                line.item_name,
                line.department,
                line.unit,
                line.quantity,
                line.unit_price,
                line.discount,
                round(line.quantity * line.unit_price * line.discount / 100.0, 2),
            ]
        )
    detail.append(["", "合计", "", "", "", "", "", result.sales_amount])
    for column, width in zip("ABCDEFGH", [6, 42, 22, 10, 8, 12, 10, 14]):
        detail.column_dimensions[column].width = width

    _write_value_sheet(workbook, original_formulas, result)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.calculation.fullCalcOnLoad = True
    except AttributeError:  # pragma: no cover - openpyxl version guard
        pass
    workbook.save(out)
    return out


def _cell_number(ws, cell: str) -> float | None:
    value = ws[cell].value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def verify_sales_quote(path: str | Path, result: QuoteResult, tolerance: float = 0.01) -> dict:
    """Re-open the generated workbook and assert its numbers match the result.

    Raises :class:`QuoteSelfCheckError` when any amount cell is missing, still a
    formula, or differs from the computed value.
    """
    workbook = load_workbook(path, data_only=False)
    ws = workbook.active
    mismatches: list[dict] = []
    checks: list[dict] = []
    for cell, (label, attr) in SALES_QUOTE_CELLS.items():
        expected = getattr(result, attr)
        actual = _cell_number(ws, cell)
        ok = actual is not None and abs(actual - expected) <= tolerance
        checks.append(
            {
                "cell": cell,
                "field": label,
                "expected": expected,
                "actual": actual,
                "ok": ok,
            }
        )
        if not ok:
            mismatches.append(
                {
                    "cell": cell,
                    "field": label,
                    "expected": expected,
                    "actual": actual if actual is not None else ws[cell].value,
                    "reason": "单元格不是数值（可能仍是公式或为空）" if actual is None else "金额不一致",
                }
            )

    # Arithmetic consistency of the generated sheet itself.
    c8 = _cell_number(ws, "C8") or 0.0
    c9 = _cell_number(ws, "C9") or 0.0
    c10 = _cell_number(ws, "C10") or 0.0
    c11 = _cell_number(ws, "C11") or 0.0
    c12 = _cell_number(ws, "C12") or 0.0
    c13 = _cell_number(ws, "C13") or 0.0
    c14 = _cell_number(ws, "C14") or 0.0
    c15 = _cell_number(ws, "C15") or 0.0
    b11 = _cell_number(ws, "B11") or result.product_tax_rate
    b12 = _cell_number(ws, "B12") or result.service_tax_rate
    arithmetic = [
        ("服务金额 = 销售报价 - 产品金额", c10, c8 - c9, "C10 = C8-C9"),
        ("产品税额 = 产品金额 × 产品税率", c11, c9 * b11, "C11 = C9*B11"),
        ("服务税额 = 服务金额 × 服务税率", c12, c10 * b12, "C12 = C10*B12"),
        ("税价合计 = 销售报价 + 产品税 + 服务税", c13, c8 + c11 + c12, "C13 = C8+C11+C12"),
        ("最终价格 = 税价合计 + 产生费用", c15, c13 + c14, "C15 = C13+C14"),
    ]
    for label, actual, expected, formula in arithmetic:
        ok = abs(actual - expected) <= tolerance
        checks.append(
            {
                "cell": formula.split(" ")[0],
                "field": label,
                "expected": round(expected, 2),
                "actual": round(actual, 2),
                "ok": ok,
            }
        )
        if not ok:
            mismatches.append(
                {
                    "cell": formula.split(" ")[0],
                    "field": label,
                    "expected": round(expected, 2),
                    "actual": round(actual, 2),
                    "reason": f"表内算术不一致（{formula}）",
                }
            )

    if mismatches:
        raise QuoteSelfCheckError(mismatches)
    return {
        "ok": True,
        "file": str(path),
        "cells": {cell: getattr(result, attr) for cell, (_, attr) in SALES_QUOTE_CELLS.items()},
        "checks": checks,
    }


def sales_quote_preview(result: QuoteResult) -> dict:
    """Return the computed cells so a report/UI can show them without Excel."""
    return {
        "销售报价（不含税）": result.sales_amount,
        "产品金额（不含税）": result.product_amount,
        "服务金额（不含税）": result.service_amount,
        "产品税率": f"{result.product_tax_rate:.0%}",
        "产品税额": result.product_tax,
        "服务税率": f"{result.service_tax_rate:.0%}",
        "服务税额": result.service_tax,
        "税价合计": result.total_with_tax,
        "产生费用": result.incurred_cost,
        "最终价格": result.final_price,
    }
