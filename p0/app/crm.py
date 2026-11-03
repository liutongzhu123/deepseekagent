"""P1-5 CRM read-only adapter with an Excel/CSV bridge and a reserved API seam.

The customer CRM is not available for integration yet.  This module keeps the
business contract stable:

* ``P0_CRM_MODE=off`` (default) — no CRM data; quote behaviour is unchanged;
* ``P0_CRM_MODE=excel|csv`` — read a local export/template, reconcile quote
  lines against projects/prices, and expose read-only lookups;
* ``P0_CRM_MODE=api`` — the interface is reserved but intentionally not called;
  status/lookup return a clear configuration error instead of silent fallback.

Everything is read-only: no CRM write-back, no price generation.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

MODES = ("off", "excel", "csv", "api")


class CrmUnavailable(RuntimeError):
    """CRM data source is missing, unreadable, or the API seam is not wired."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | None:
    text = _text(value).replace(",", "").replace("￥", "").replace("¥", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None


def _int(value: Any, default: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else default


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u3000（）()、，,。；;：:·\-_/]+", "", _text(value)).lower()


def _norm_standard(value: Any) -> str:
    text = _norm(value)
    if "大纲" in text:
        return "大纲"
    return text


@dataclass
class CrmProject:
    project_id: str
    name: str
    customer: str = ""
    status: str = ""
    owner: str = ""
    updated_at: str = ""


@dataclass
class CrmPrice:
    project_id: str
    project_name: str
    sample_name: str
    item_name: str
    standard: str
    department: str
    unit: str
    quantity: float
    unit_price: float
    discount: float = 60.0
    valid_until: str = ""


@dataclass
class CrmQualification:
    name: str
    certificate_no: str
    scope: str = ""
    valid_until: str = ""
    status: str = ""


@dataclass
class CrmSchedule:
    project_id: str
    project_name: str
    start_date: str = ""
    end_date: str = ""
    status: str = ""
    note: str = ""


@dataclass
class CrmTaskOrder:
    order_no: str
    project_id: str = ""
    customer: str = ""
    sample_name: str = ""
    status: str = ""
    commissioned_at: str = ""


@dataclass
class CrmSample:
    sample_no: str
    project_id: str = ""
    sample_name: str = ""
    model: str = ""
    quantity: float = 0.0
    status: str = ""
    received_at: str = ""


@dataclass
class CrmSnapshot:
    mode: str
    source: str
    projects: list[CrmProject] = field(default_factory=list)
    prices: list[CrmPrice] = field(default_factory=list)
    qualifications: list[CrmQualification] = field(default_factory=list)
    schedules: list[CrmSchedule] = field(default_factory=list)
    task_orders: list[CrmTaskOrder] = field(default_factory=list)
    samples: list[CrmSample] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "projects": len(self.projects),
            "prices": len(self.prices),
            "qualifications": len(self.qualifications),
            "schedules": len(self.schedules),
            "task_orders": len(self.task_orders),
            "samples": len(self.samples),
        }


_SHEET_ALIASES = {
    "projects": {"项目", "项目清单", "projects"},
    "prices": {"价格", "价格表", "报价", "报价表", "prices"},
    "qualifications": {"资质", "资质证书", "qualifications"},
    "schedules": {"排期", "试验排期", "schedules"},
    "task_orders": {"任务单", "任务", "task_orders"},
    "samples": {"样品", "samples"},
}

_HEADER_ALIASES = {
    "project_id": ("项目编号", "项目号", "项目id", "project_id"),
    "project_name": ("项目名称", "项目", "project_name"),
    "customer": ("客户", "客户名称", "委托单位", "customer"),
    "status": ("状态", "status"),
    "owner": ("负责人", "owner"),
    "updated_at": ("更新时间", "updated_at"),
    "sample_name": ("样品名称", "样品", "sample_name"),
    "item_name": ("检验项目", "测试项目", "试验项目", "项目名称", "item_name"),
    "standard": ("标准", "检验标准", "试验标准", "standard"),
    "department": ("科室", "检验科室", "部门", "department"),
    "unit": ("单位", "unit"),
    "quantity": ("数量", "quantity"),
    "unit_price": ("含税单价", "单价", "价格", "unit_price", "price"),
    "discount": ("折扣", "discount"),
    "valid_until": ("有效期至", "有效期", "valid_until"),
    "name": ("资质名称", "名称", "name"),
    "certificate_no": ("证书编号", "证书号", "certificate_no"),
    "scope": ("适用范围", "scope"),
    "start_date": ("计划开始", "开始日期", "start_date"),
    "end_date": ("计划结束", "结束日期", "end_date"),
    "note": ("备注", "note"),
    "order_no": ("任务单号", "任务单编号", "order_no"),
    "commissioned_at": ("委托日期", "commissioned_at"),
    "sample_no": ("样品编号", "sample_no"),
    "model": ("型号", "规格型号", "model"),
    "received_at": ("接收日期", "received_at"),
}


def _header_index(row: list[Any]) -> dict[str, int]:
    index: dict[str, int] = {}
    used: set[int] = set()
    normalized = [_norm(cell) for cell in row]
    # Exact header names win over substring aliases, otherwise a short alias
    # such as 样品 would steal 样品编号 and break the sample sheet.
    for key, aliases in _HEADER_ALIASES.items():
        for position, text in enumerate(normalized):
            if not text or position in used:
                continue
            if any(_norm(alias) == text for alias in aliases):
                index[key] = position
                used.add(position)
                break
    for key, aliases in _HEADER_ALIASES.items():
        if key in index:
            continue
        for position, text in enumerate(normalized):
            if not text or position in used:
                continue
            if any(_norm(alias) in text for alias in aliases):
                index[key] = position
                used.add(position)
                break
    return index


def _find_header(rows: list[list[Any]], required: set[str]) -> tuple[int, dict[str, int]]:
    for position, row in enumerate(rows[:20]):
        index = _header_index(row)
        if required.issubset(index):
            return position, index
    return -1, {}


def _project_from_prices(prices: list[CrmPrice]) -> list[CrmProject]:
    seen: dict[str, CrmProject] = {}
    for price in prices:
        key = price.project_id or price.project_name
        if not key:
            continue
        seen.setdefault(
            key,
            CrmProject(project_id=price.project_id, name=price.project_name or price.project_id),
        )
    return list(seen.values())


class _PriceListMixin:
    def _price_rows(
        self, rows: list[list[Any]], fallback_project: str, fallback_only: bool = False
    ) -> list[CrmPrice]:
        header_position, index = _find_header(rows, {"item_name"})
        if header_position < 0:
            return []
        prices: list[CrmPrice] = []
        for row in rows[header_position + 1 :]:
            if not row or not _text(row[index.get("item_name", 0)]):
                continue
            sample_name = _text(row[index.get("sample_name", -1)]) if "sample_name" in index else ""
            if fallback_only:
                # A CRM batch-import template has no project column; its
                # "项目名称" is the test item, so group by sample/file instead.
                project_name = sample_name or fallback_project
                project_id = project_name
            else:
                project_id = _text(row[index.get("project_id", -1)]) if "project_id" in index else ""
                project_name = (
                    _text(row[index.get("project_name", -1)])
                    if "project_name" in index
                    else fallback_project
                ) or fallback_project
            price = _number(row[index["unit_price"]]) if "unit_price" in index else None
            prices.append(
                CrmPrice(
                    project_id=project_id or project_name,
                    project_name=project_name or project_id,
                    sample_name=sample_name,
                    item_name=_text(row[index["item_name"]]),
                    standard=_text(row[index.get("standard", -1)]) if "standard" in index else "",
                    department=_text(row[index.get("department", -1)]) if "department" in index else "",
                    unit=_text(row[index.get("unit", -1)]) if "unit" in index else "",
                    quantity=_number(row[index.get("quantity", -1)]) or 1.0 if "quantity" in index else 1.0,
                    unit_price=price or 0.0,
                    discount=_number(row[index.get("discount", -1)]) or 60.0 if "discount" in index else 60.0,
                    valid_until=_text(row[index.get("valid_until", -1)]) if "valid_until" in index else "",
                )
            )
        return prices


class ExcelCrmProvider(_PriceListMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mode = "excel"

    def snapshot(self) -> CrmSnapshot:
        if not self.path.exists():
            raise CrmUnavailable(f"CRM Excel 文件不存在：{self.path}")
        try:
            workbook = load_workbook(self.path, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - optional file boundary
            raise CrmUnavailable(f"无法读取 CRM Excel：{exc}") from exc
        sheets = {name: list(workbook[name].iter_rows(values_only=True)) for name in workbook.sheetnames}
        snapshot = CrmSnapshot(mode="excel", source=str(self.path))
        for key, aliases in _SHEET_ALIASES.items():
            name = next((candidate for candidate in sheets if _text(candidate) in aliases), "")
            if not name:
                continue
            rows = [list(row) for row in sheets[name]]
            self._load_sheet(snapshot, key, rows)
        if not snapshot.prices:
            # CRM 批量导入模板没有标准 sheet 名；第一张表按价格清单回退解析。
            first = next(iter(sheets.values()), [])
            fallback = self.path.stem
            snapshot.prices = self._price_rows([list(row) for row in first], fallback, fallback_only=True)
            if snapshot.prices:
                snapshot.projects = _project_from_prices(snapshot.prices)
                snapshot.warnings.append("未找到「价格」sheet，已按第一张表的价格清单解析")
        if not snapshot.prices and not snapshot.projects:
            raise CrmUnavailable("CRM Excel 中没有识别到项目或价格表")
        return snapshot

    def _load_sheet(self, snapshot: CrmSnapshot, key: str, rows: list[list[Any]]) -> None:
        if key == "prices":
            snapshot.prices = self._price_rows(rows, self.path.stem)
            return
        required = {
            "projects": {"project_name"},
            "qualifications": {"name"},
            "schedules": {"project_name"},
            "task_orders": {"order_no"},
            "samples": {"sample_no"},
        }[key]
        header_position, index = _find_header(rows, required)
        if header_position < 0:
            snapshot.warnings.append(f"{key} sheet 缺少必要表头，已跳过")
            return
        for row in rows[header_position + 1 :]:
            if not row:
                continue
            def get(field_name: str, default: Any = "") -> Any:
                return row[index[field_name]] if field_name in index and index[field_name] < len(row) else default

            if key == "projects" and _text(get("project_name")):
                snapshot.projects.append(
                    CrmProject(
                        project_id=_text(get("project_id")) or _text(get("project_name")),
                        name=_text(get("project_name")),
                        customer=_text(get("customer")),
                        status=_text(get("status")),
                        owner=_text(get("owner")),
                        updated_at=_text(get("updated_at")),
                    )
                )
            elif key == "qualifications" and _text(get("name")):
                snapshot.qualifications.append(
                    CrmQualification(
                        name=_text(get("name")),
                        certificate_no=_text(get("certificate_no")),
                        scope=_text(get("scope")),
                        valid_until=_text(get("valid_until")),
                        status=_text(get("status")),
                    )
                )
            elif key == "schedules" and _text(get("project_name")):
                snapshot.schedules.append(
                    CrmSchedule(
                        project_id=_text(get("project_id")) or _text(get("project_name")),
                        project_name=_text(get("project_name")),
                        start_date=_text(get("start_date")),
                        end_date=_text(get("end_date")),
                        status=_text(get("status")),
                        note=_text(get("note")),
                    )
                )
            elif key == "task_orders" and _text(get("order_no")):
                snapshot.task_orders.append(
                    CrmTaskOrder(
                        order_no=_text(get("order_no")),
                        project_id=_text(get("project_id")),
                        customer=_text(get("customer")),
                        sample_name=_text(get("sample_name")),
                        status=_text(get("status")),
                        commissioned_at=_text(get("commissioned_at")),
                    )
                )
            elif key == "samples" and _text(get("sample_no")):
                snapshot.samples.append(
                    CrmSample(
                        sample_no=_text(get("sample_no")),
                        project_id=_text(get("project_id")),
                        sample_name=_text(get("sample_name")),
                        model=_text(get("model")),
                        quantity=_number(get("quantity")) or 0.0,
                        status=_text(get("status")),
                        received_at=_text(get("received_at")),
                    )
                )


class CsvCrmProvider(_PriceListMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mode = "csv"

    def snapshot(self) -> CrmSnapshot:
        if not self.path.exists():
            raise CrmUnavailable(f"CRM CSV 文件不存在：{self.path}")
        try:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
        except OSError as exc:
            raise CrmUnavailable(f"无法读取 CRM CSV：{exc}") from exc
        prices = self._price_rows(rows, self.path.stem)
        if not prices:
            raise CrmUnavailable("CRM CSV 中没有识别到价格表")
        return CrmSnapshot(
            mode="csv", source=str(self.path), prices=prices, projects=_project_from_prices(prices)
        )


class ApiCrmProvider:
    """Reserved CRM HTTP seam.  It deliberately does not make a request yet."""

    def __init__(self):
        self.base_url = os.getenv("P0_CRM_API_BASE_URL", "").strip()
        self.api_key = os.getenv("P0_CRM_API_KEY", "").strip()

    def snapshot(self) -> CrmSnapshot:
        if not self.base_url:
            raise CrmUnavailable("P0_CRM_MODE=api 需要 P0_CRM_API_BASE_URL；接口尚未接入")
        raise CrmUnavailable(
            "CRM API 接口已预留但尚未接入：请先用 P0_CRM_MODE=excel/csv 的导出适配，"
            "或提供接口账号与字段契约后实现 ApiCrmProvider"
        )


class NullCrmProvider:
    mode = "off"

    def snapshot(self) -> CrmSnapshot:
        return CrmSnapshot(mode="off", source="")


def provider() -> Any:
    mode = os.getenv("P0_CRM_MODE", "off").strip().lower() or "off"
    if mode not in MODES:
        raise CrmUnavailable(f"P0_CRM_MODE 必须是 {MODES}，当前 {mode!r}")
    if mode == "off":
        return NullCrmProvider()
    if mode == "api":
        return ApiCrmProvider()
    path_text = os.getenv("P0_CRM_PATH", "").strip()
    if not path_text:
        raise CrmUnavailable(f"P0_CRM_MODE={mode} 需要 P0_CRM_PATH")
    path = Path(path_text).expanduser()
    return ExcelCrmProvider(path) if mode == "excel" else CsvCrmProvider(path)


def status() -> dict:
    try:
        source = provider()
        snapshot = source.snapshot()
    except CrmUnavailable as exc:
        return {"configured": False, "mode": os.getenv("P0_CRM_MODE", "off"), "error": str(exc), "counts": {}}
    return {
        "configured": True,
        "mode": snapshot.mode,
        "source": snapshot.source,
        "counts": snapshot.counts(),
        "warnings": snapshot.warnings,
    }


def read_projects(snapshot: CrmSnapshot) -> list[dict]:
    return [project.__dict__ for project in snapshot.projects]


def read_prices(snapshot: CrmSnapshot, project: str = "", query: str = "") -> list[dict]:
    wanted_project = _norm(project)
    wanted_query = _norm(query)
    rows = []
    for price in snapshot.prices:
        if wanted_project and wanted_project not in {_norm(price.project_id), _norm(price.project_name)}:
            continue
        if wanted_query and wanted_query not in _norm(
            price.sample_name + price.item_name + price.standard + price.department
        ):
            continue
        rows.append(price.__dict__)
    return rows


def read_qualifications(snapshot: CrmSnapshot) -> list[dict]:
    return [item.__dict__ for item in snapshot.qualifications]


def read_schedules(snapshot: CrmSnapshot) -> list[dict]:
    return [item.__dict__ for item in snapshot.schedules]


def lookup_task_order(snapshot: CrmSnapshot, order_no: str) -> dict | None:
    wanted = _norm(order_no)
    for item in snapshot.task_orders:
        if _norm(item.order_no) == wanted:
            return item.__dict__
    return None


def lookup_sample(snapshot: CrmSnapshot, sample_no: str) -> dict | None:
    wanted = _norm(sample_no)
    for item in snapshot.samples:
        if _norm(item.sample_no) == wanted:
            return item.__dict__
    return None


def reconcile_quote(result: Any) -> dict:
    """Compare parsed quote lines against the CRM price snapshot (read-only)."""
    mode = os.getenv("P0_CRM_MODE", "off").strip().lower() or "off"
    if mode == "off":
        required = os.getenv("P0_CRM_REQUIRED", "0").lower() in {"1", "true", "yes"}
        return {
            "enabled": False,
            "mode": "off",
            "reconciled": not required,
            "error": "P0_CRM_REQUIRED=1 但 P0_CRM_MODE=off" if required else "",
            "total": len(result.lines),
        }
    try:
        snapshot = provider().snapshot()
    except CrmUnavailable as exc:
        return {
            "enabled": True,
            "mode": mode,
            "reconciled": False,
            "error": str(exc),
            "total": len(result.lines),
            "matched": 0,
            "missing": [],
            "price_mismatch": [],
        }
    index: dict[tuple[str, str, str], CrmPrice] = {}
    loose: dict[tuple[str, str], CrmPrice] = {}
    for price in snapshot.prices:
        index[(_norm(price.sample_name), _norm(price.item_name), _norm_standard(price.standard))] = price
        loose.setdefault((_norm(price.item_name), _norm_standard(price.standard)), price)
    matched = 0
    missing: list[dict] = []
    mismatches: list[dict] = []
    price_available: list[dict] = []
    for line in result.lines:
        key = (_norm(line.sample_name), _norm(line.item_name), _norm_standard(line.standard))
        price = index.get(key) or loose.get((_norm(line.item_name), _norm_standard(line.standard)))
        if price is None:
            missing.append(
                {
                    "sample_name": line.sample_name,
                    "item_name": line.item_name,
                    "standard": line.standard,
                    "source_row": line.source_row,
                }
            )
            continue
        matched += 1
        if line.needs_price or line.unit_price <= 0:
            # A zero price on both sides is an agreed "to be priced" line; it
            # is not a reconciliation failure.  Only a CRM price the quote did
            # not pick up is surfaced for manual completion.
            if price.unit_price > 0:
                price_available.append(
                    {
                        "sample_name": line.sample_name,
                        "item_name": line.item_name,
                        "crm_unit_price": price.unit_price,
                        "source_row": line.source_row,
                    }
                )
            continue
        if abs(float(line.unit_price) - float(price.unit_price)) > 0.01:
            mismatches.append(
                {
                    "sample_name": line.sample_name,
                    "item_name": line.item_name,
                    "quote_unit_price": line.unit_price,
                    "crm_unit_price": price.unit_price,
                    "source_row": line.source_row,
                }
            )
    total = len(result.lines)
    reconciled = total > 0 and matched == total and not mismatches and not price_available
    return {
        "enabled": True,
        "mode": snapshot.mode,
        "source": snapshot.source,
        "reconciled": reconciled,
        "total": total,
        "matched": matched,
        "missing": missing[:50],
        "price_mismatch": mismatches[:50],
        "price_available": price_available[:50],
        "crm_counts": snapshot.counts(),
    }
