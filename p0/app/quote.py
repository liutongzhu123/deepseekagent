"""P0 quote assistant: DVP/test-plan Excel -> CRM draft + sales quote form.

Design rules (P0 ADR-03):
  * unit price, quantity and unit come from the uploaded DVP/price export;
  * the assistant only maps item names/departments and calculates totals;
  * no model-generated price, no CRM write-back, no approval workflow.

The parser deliberately accepts the two layouts that appear in the customer's
real materials:
  * a DVP sheet with a ``试验项目`` header and ``单价/数量/单位`` columns
    (front-seat DVP layout);
  * a hierarchical test-plan sheet with a ``测试项目``/``测试子过程`` header
    (non-standard test plan).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from .xlsx_writer import write_xlsx

CRM_HEADERS = [
    "样品名称",
    "项目名称",
    "检验项目",
    "标准",
    "检验科室",
    "单位",
    "数量",
    "含税单价",
    "是否有折扣",
    "折扣",
    "备注",
]

_UNIT_MAP = {
    "席位": "席位/次",
    "席位/次": "席位/次",
    "袋子": "次",
    "袋": "次",
    "套": "套",
    "件": "件",
    "批": "批",
    "次": "次",
    "小时": "小时",
    "h": "小时",
    "回路": "回路",
    "种": "种",
    "材料": "材料",
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

    def to_crm_row(self) -> list[Any]:
        return [
            self.sample_name,
            self.item_name,
            self.item_name,
            self.standard,
            self.department,
            self.unit,
            self.quantity,
            self.unit_price,
            "是",
            self.discount,
            self.note,
        ]


PRODUCT_TAX_RATE = 0.16
SERVICE_TAX_RATE = 0.06


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

    @property
    def subtotal(self) -> float:
        return round(sum(line.quantity * line.unit_price for line in self.lines), 2)

    @property
    def sales_amount(self) -> float:
        return round(self.subtotal * self.discount / 100.0, 2)

    @property
    def service_amount(self) -> float:
        """Service revenue = sales quote minus any product amount."""
        return round(self.sales_amount - self.product_amount, 2)

    @property
    def product_tax(self) -> float:
        return round(self.product_amount * PRODUCT_TAX_RATE, 2)

    @property
    def service_tax(self) -> float:
        return round(self.service_amount * SERVICE_TAX_RATE, 2)

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
            "subtotal": self.subtotal,
            "discount_percent": self.discount,
            "sales_amount": self.sales_amount,
            "product_amount": self.product_amount,
            "service_amount": self.service_amount,
            "product_tax_rate": PRODUCT_TAX_RATE,
            "product_tax": self.product_tax,
            "service_tax_rate": SERVICE_TAX_RATE,
            "service_tax": self.service_tax,
            "total_with_tax": self.total_with_tax,
            "incurred_cost": self.incurred_cost,
            "final_price": self.final_price,
            "warnings": self.warnings,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "items": [asdict(line) for line in self.lines]}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clean_item(value: Any) -> str:
    text = _text(value).replace("\n", " ")
    text = re.sub(r"^[\/／、]+", "", text).strip()
    text = re.sub(r"\s+", " ", text)
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


def _unit(value: Any) -> str:
    text = _text(value)
    return _UNIT_MAP.get(text, text or "次")


def _department(item: str, standard: str = "") -> str:
    if any(word in item for word in ("散发", "气味", "VOC", "voc", "雾化", "禁用物质", "禁限物质")):
        return "理化分析试验研究部"
    if "电磁" in item or "电气" in standard:
        return "智能电器试验研究部"
    return "车身附件试验研究部"


def _find_layout(ws) -> tuple[int, int, int, int, int, int] | None:
    """Return (item_row, item_col, price_row, price_col, qty_col, unit_col)."""
    item_row = item_col = None
    for row in range(1, min(ws.max_row, 30) + 1):
        for col in range(1, min(ws.max_column, 30) + 1):
            if "试验项目" in _text(ws.cell(row, col).value):
                item_row, item_col = row, col
                break
        if item_row:
            break
    if item_row is None or item_col is None:
        return None

    price_row = price_col = qty_col = unit_col = None
    for row in range(item_row, min(item_row + 6, ws.max_row) + 1):
        for col in range(1, min(ws.max_column, 30) + 1):
            value = _text(ws.cell(row, col).value)
            if value == "单价":
                price_row, price_col = row, col
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
            if value == "数量" and qty_col is None:
                qty_col = col
            if value == "单位" and unit_col is None:
                unit_col = col
        if qty_col and unit_col:
            break
    qty_col = qty_col or price_col + 1
    unit_col = unit_col or price_col + 2
    return item_row, item_col, price_row or item_row, price_col, qty_col, unit_col


def parse_dvp(path: str | Path, discount: float = 60.0, source_name: str | None = None) -> QuoteResult:
    path = Path(path)
    display_name = source_name or path.name
    workbook = load_workbook(path, data_only=True)
    # Prefer a DVP sheet; fall back to the first non-empty sheet.
    ws = next((sheet for sheet in workbook.worksheets if "座椅" in sheet.title or "DVP" in sheet.title), workbook.worksheets[0])

    layout = _find_layout(ws)
    if layout is None:
        raise ValueError("未找到「试验项目 + 单价/数量/单位」表头，无法解析 DVP")
    item_row, item_col, price_row, price_col, qty_col, unit_col = layout

    sample_name = ""
    for row in range(1, max(item_row - 1, 1) + 1):
        for col in range(1, min(ws.max_column, 20) + 1):
            value = _text(ws.cell(row, col).value)
            if "零件/总成" in value:
                sample_name = value.split("：", 1)[-1].split(":", 1)[-1].strip()
                break
        if sample_name:
            break
    if not sample_name:
        sample_name = re.sub(r"^\d+[、.．]\s*", "", ws.title) or path.stem
    prefix = re.match(r"([A-Za-z]{2,}\d+)", Path(display_name).stem)
    if prefix and not re.match(r"[A-Za-z]", sample_name):
        sample_name = prefix.group(1) + sample_name

    result = QuoteResult(source_file=display_name, sample_name=sample_name, discount=discount)
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
        unit = _unit(ws.cell(row, unit_col).value)
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
    return result


def write_crm_xlsx(path: str | Path, result: QuoteResult) -> Path:
    rows: list[list[Any]] = [CRM_HEADERS] + [line.to_crm_row() for line in result.lines]
    summary = [
        ["报价汇总", ""],
        ["样品名称", result.sample_name],
        ["明细行数", len(result.lines)],
        ["DVP 合计（未折扣）", result.subtotal],
        ["折扣（%）", result.discount],
        ["销售报价（不含税）", result.sales_amount],
        ["服务税率", "6%"],
        ["税额", result.service_tax],
        ["税价合计", result.total_with_tax],
    ]
    return write_xlsx(
        path,
        [
            ("CRM报价草稿", rows, [18, 34, 34, 24, 22, 10, 8, 12, 10, 8, 26]),
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
    b11 = _cell_number(ws, "B11") or PRODUCT_TAX_RATE
    b12 = _cell_number(ws, "B12") or SERVICE_TAX_RATE
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
        "产品税率": "16%",
        "产品税额": result.product_tax,
        "服务税率": "6%",
        "服务税额": result.service_tax,
        "税价合计": result.total_with_tax,
        "产生费用": result.incurred_cost,
        "最终价格": result.final_price,
    }
