"""Generate an explicitly internal workbook from an audited CRM preview."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


HEADERS = [
    "项目", "询价项目", "CRM匹配项目", "标准/条款", "样品", "数量", "单位",
    "价格状态", "只读参考单价", "币种", "含税口径", "样品匹配级别",
    "排期状态", "开始日期", "结束日期", "资质状态", "资质范围", "资质标准",
    "解析来源行", "解析置信度", "来源记录", "待核实事项", "建议动作",
]


def _text(value: Any) -> str:
    """Keep all exported untrusted text literal in spreadsheet cells."""
    if value is None:
        return ""
    result = ILLEGAL_CHARACTERS_RE.sub("", str(value))
    if result.lstrip().startswith(("=", "+", "-", "@")):
        result = "'" + result
    return result[:32000]


def build_internal_review_workbook(
    project_name: str, review_id: str, preview: dict[str, Any]
) -> bytes:
    """Build a review-only workbook; it deliberately contains no quote totals."""
    if preview.get("environment") != "internal" or preview.get("input_origin") != "parsed_quote":
        raise ValueError("只允许从当前 CRM 解析复核预览生成内部草稿")
    if preview.get("demo_only") is not False:
        raise ValueError("合成数据不能导出为 CRM 内部草稿")
    mode = preview.get("source_mode")
    revision = preview.get("source_revision")
    if mode not in {"excel", "csv", "base"} or not revision:
        raise ValueError("CRM 来源模式或版本缺失")
    lines = preview.get("lines")
    if not isinstance(lines, list) or not lines or len(lines) > 200:
        raise ValueError("CRM 预览明细为空或超过导出上限")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "内部复核草稿"
    sheet.append(["内部 CRM 复核草稿 · 非正式报价 · 不得直接发送客户或回写 CRM"])
    sheet.append([f"项目：{_text(project_name)}", f"复核编号：{_text(review_id)}"])
    sheet.append([f"CRM 来源：{_text(mode)}", f"数据版本：{_text(revision)}"])
    sheet.append([f"预览编号：{_text(preview.get('preview_id'))}", "生成时间：" + datetime.now(timezone.utc).isoformat(timespec="seconds")])
    sheet.append(HEADERS)
    for line in lines:
        request = line.get("request") if isinstance(line.get("request"), dict) else {}
        price = line.get("price_preview") if isinstance(line.get("price_preview"), dict) else {}
        schedule = line.get("schedule_preview") if isinstance(line.get("schedule_preview"), dict) else {}
        qualification = line.get("qualification_preview") if isinstance(line.get("qualification_preview"), dict) else {}
        # Ambiguous, expired, unavailable, or missing prices must never appear
        # as a selected unit price in the export.
        unit_price = price.get("unit_price") if line.get("price_status") == "matched" else None
        refs = line.get("source_refs") if isinstance(line.get("source_refs"), list) else []
        row = [
            request.get("project_name"), request.get("item_name"), line.get("matched_item_name"),
            request.get("standard"), request.get("sample_name"), request.get("quantity"),
            request.get("unit"), line.get("price_status"), unit_price, price.get("currency"),
            price.get("tax_inclusive"), price.get("sample_match_level"),
            line.get("schedule_status"), schedule.get("start_date"), schedule.get("end_date"),
            line.get("qualification_status"), qualification.get("scope_level"),
            qualification.get("standard"),
            (line.get("input_evidence") or {}).get("source_row"),
            (line.get("input_evidence") or {}).get("parse_confidence"),
            "; ".join(
                f"{_text(ref.get('source_type'))}:{_text(ref.get('record_id'))}@{_text(ref.get('revision'))}"
                for ref in refs if isinstance(ref, dict)
            ),
            "；".join(_text(item) for item in (line.get("missing_fields") or [])),
            line.get("action"),
        ]
        sheet.append([value if isinstance(value, (int, float, bool)) else _text(value) for value in row])

    sheet.freeze_panes = "A6"
    sheet.auto_filter.ref = f"A5:{get_column_letter(len(HEADERS))}{sheet.max_row}"
    for cell in sheet[5]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for col_index, header in enumerate(HEADERS, start=1):
        width = min(32, max(12, len(header) * 2 + 2))
        sheet.column_dimensions[get_column_letter(col_index)].width = width
    for row in sheet.iter_rows(min_row=1, max_row=4):
        for cell in row:
            cell.font = Font(bold=cell.row == 1, color="9C0006" if cell.row == 1 else "404040")

    instructions = workbook.create_sheet("使用说明")
    instructions.append(["用途", "仅供内部人员核对当前配置 CRM 只读快照与已复核询价明细。"])
    instructions.append(["限制", "非正式报价；不得直接发送客户；未写回 CRM；不包含折扣、总价或报价承诺。"])
    instructions.append(["价格", "仅在当前快照唯一匹配时列出参考单价；歧义、过期、缺失或不可用均留空并保留状态。"])
    instructions.append(["资质", "CRM 适配器缺少可核验的项目/标准范围时保持未知，不构成资质承诺。"])
    instructions.append(["来源", f"复核编号：{_text(review_id)}；CRM 模式：{_text(mode)}；数据版本：{_text(revision)}"])
    instructions.column_dimensions["A"].width = 16
    instructions.column_dimensions["B"].width = 100

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
