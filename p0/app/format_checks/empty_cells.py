"""R14 required-field completeness checks for table cells."""
from __future__ import annotations

from ..docx_parser import _expand_table
from ._shared import ReportData, _NA_VALUES, _finding, _norm_cell

_REQUIRED_LABELS = {
    "样品名称", "样品编号", "规格型号", "型号规格", "生产企业", "委托单位", "样品数量",
    "生产日期", "抽样日期", "送样日期", "检验依据", "检验项目", "检验结论", "签发日期",
    "检验日期", "试验日期", "设备编号", "设备名称", "溯源有效期", "报告编号",
}
_TODO_VALUES = {"___", "____", "_____", "______", "…", "...", "待填", "待定", "未填", "□", "☐", "xx", "xxx", "xxxx", "xxxxx"}


def _cell_issue(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "未填写"
    norm = _norm_cell(raw)
    if norm.lower() in _NA_VALUES:
        return ""
    if norm.lower() in _TODO_VALUES or (len(norm) >= 3 and set(norm) <= {"_"}):
        return "占位符未替换"
    return ""


def r14_empty_cells(data: ReportData):
    title = "空缺项未填写"
    if not data.tables:
        return _finding("R14", title, "skip", "info", "未识别到表格，无法检查空缺项")
    problems: list[str] = []
    targets: list[str] = []
    scanned = 0
    na_count = 0
    for table_index, table in enumerate(data.tables, start=1):
        grid = _expand_table(table)
        for row_index, row in enumerate(grid):
            for column_index, cell in enumerate(row):
                label = _norm_cell(cell)
                if label not in _REQUIRED_LABELS:
                    continue
                scanned += 1
                checks: list[tuple[int, str]] = []
                header_row = (
                    row_index == 0
                    and len(grid) > 1
                    and len(row) >= 2
                    and all(not cell.strip() or _norm_cell(cell) in _REQUIRED_LABELS for cell in row)
                )
                if header_row:
                    checks.extend(
                        (rj + 1, grid[rj][column_index])
                        for rj in range(1, len(grid))
                        if column_index < len(grid[rj])
                    )
                elif column_index + 1 < len(row):
                    checks.append((row_index + 1, row[column_index + 1]))
                for row_no, value in checks:
                    raw = (value or "").strip()
                    if raw and _norm_cell(raw).lower() in _NA_VALUES:
                        na_count += 1
                        continue
                    issue = _cell_issue(value)
                    if issue:
                        problems.append(f"表{table_index} 第{row_no}行「{label}」{issue}")
                        targets.append(label)
    if problems:
        return _finding(
            "R14", title, "fail", "medium",
            f"必填项空缺 {len(problems)} 处：" + "；".join(problems[:6]),
            f"必填项={scanned}; 合理留空={na_count}; 问题={len(problems)}",
            "补齐必填项；确属不适用的单元格用 /、— 或“不适用”明确杠掉",
            section="表格必填项", cell="、".join(dict.fromkeys(targets))[:80],
        )
    if scanned == 0:
        return _finding("R14", title, "skip", "info", "未识别到必填项标签或表头，无法检查空缺项")
    return _finding(
        "R14", title, "pass", "info",
        f"扫描 {scanned} 个必填项，均已填写或明确杠掉",
        f"合理留空={na_count}",
        section="表格必填项", cell="必填项",
    )
