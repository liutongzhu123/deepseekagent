"""Stage-2.2 partial rules.

These checks explicitly represent the POC rules that only become reliable when
customer data arrives.  Every rule returns ``skip`` with a reason when its
input is missing; visual checks are marked ``未自动审核`` and never ``pass``.
"""
from __future__ import annotations

from .docx_parser import ReportData, parse_date
from .rules import Finding

PARTIAL_RULES = (
    ("P01", "任务单编号一致"),
    ("P02", "目录页码完整"),
    ("P03", "单项检验时间"),
    ("P04", "数值单位"),
    ("P05", "平行样"),
    ("P06", "照片/设备标签/印章"),
    ("P07", "标记符号"),
    ("P08", "第二主机厂模板"),
)

PARTIAL_RULE_META = {
    "P01": {"description": "报告编号/样品编号与任务单一致", "requires": "任务单导出"},
    "P02": {"description": "目录页码落在 1..总页数内", "requires": "目录与页脚解析"},
    "P03": {"description": "单项检验时间在汇总检验时间内", "requires": "检验起止日期"},
    "P04": {"description": "检验结果数值单位与判定要求一致", "requires": "结构化单位列"},
    "P05": {"description": "平行样间偏差与小数位一致", "requires": "平行样编号/结果"},
    "P06": {"description": "照片/设备标签/印章完整性", "requires": "视觉模型（本期未接入）"},
    "P07": {"description": "报告不得含黄色高亮/红色字体", "requires": "报告样式解析"},
    "P08": {"description": "第二主机厂模板正确使用", "requires": "吉利/非吉利样例"},
}


def partial_rule_catalog() -> list[dict]:
    """Rule metadata for the API and the workbench stage-2 panel."""
    return [
        {
            "id": rule_id,
            "title": title,
            "description": PARTIAL_RULE_META.get(rule_id, {}).get("description", ""),
            "requires": PARTIAL_RULE_META.get(rule_id, {}).get("requires", ""),
            "enabled": True,
            "layer": "partial",
        }
        for rule_id, title in PARTIAL_RULES
    ]


def _finding(rule_id: str, title: str, status: str, severity: str, message: str, evidence: str = "", suggestion: str = "") -> Finding:
    return Finding(
        rule_id,
        title,
        status,
        severity,
        message,
        evidence,
        suggestion,
        {"page": 1, "section": "部分规则", "cell": rule_id},
        "partial",
    )


def evaluate_partial_rules(data: ReportData, inputs: object | None = None) -> list[Finding]:
    findings: list[Finding] = []

    # P01 task-order consistency (needs the local inbox).
    order = None
    if inputs is not None and data.cover_number:
        rows = inputs.find("task_orders", "report_no", data.cover_number)  # type: ignore[attr-defined]
        if rows:
            order = rows[0]
    if order is None:
        findings.append(_finding("P01", "任务单编号一致", "skip", "info", "未提供任务单数据，无法核对"))
    else:
        mismatch = []
        if data.cover_number and order.get("report_no") and data.cover_number != order["report_no"]:
            mismatch.append(f"报告编号 报告={data.cover_number} 任务单={order['report_no']}")
        if data.sample_no and order.get("sample_no") and data.sample_no != order["sample_no"]:
            mismatch.append(f"样品编号 报告={data.sample_no} 任务单={order['sample_no']}")
        findings.append(
            _finding("P01", "任务单编号一致", "fail" if mismatch else "pass", "high" if mismatch else "info", "存在不一致" if mismatch else "任务单关键字段一致", "; ".join(mismatch))
        )

    # P02 TOC pages against total pages.
    total = max(data.page_totals) if data.page_totals else 0
    if not data.toc_entries or not total:
        findings.append(_finding("P02", "目录页码完整", "skip", "info", "未识别到目录或总页数"))
    else:
        problems = []
        for entry in data.toc_entries:
            try:
                page = int(entry.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if not 1 <= page <= total:
                problems.append(f"{entry.get('title')}:{entry.get('page')}")
        findings.append(
            _finding("P02", "目录页码完整", "fail" if problems else "pass", "medium" if problems else "info", "目录页码越界" if problems else f"目录 {len(data.toc_entries)} 条页码在 1..{total} 内", "; ".join(problems))
        )

    # P03 single-item test time.
    start, end = parse_date(data.test_start), parse_date(data.test_end)
    if not start or not end:
        findings.append(_finding("P03", "单项检验时间", "skip", "info", "未识别到检验起止日期"))
    elif start > end:
        findings.append(_finding("P03", "单项检验时间", "fail", "high", "检验开始晚于结束", f"{start} > {end}"))
    else:
        findings.append(_finding("P03", "单项检验时间", "pass", "info", f"检验时间 {start}~{end}", ""))

    # P04 units require a structured unit column; not present in the POC export.
    findings.append(_finding("P04", "数值单位", "skip", "info", "报告未导出结构化单位列，待客户模板补充"))

    # P05 parallel samples require a second sample identifier.
    findings.append(_finding("P05", "平行样", "skip", "info", "未提供平行样编号，无法核对"))

    # P06 visual checks have no model in this phase.
    findings.append(_finding("P06", "照片/设备标签/印章", "skip", "medium", "未自动审核：视觉模型未接入，需人工复核"))

    # P07 markup is deterministic and already parsed from run styles.
    if data.highlights or data.red_runs:
        findings.append(_finding("P07", "标记符号", "fail", "medium", f"高亮 {data.highlights} 处、红字 {data.red_runs} 处", "highlights/red_runs"))
    else:
        findings.append(_finding("P07", "标记符号", "pass", "info", "未发现高亮/红字"))

    # P08 second OEM templates are not in the current sample set.
    if data.template_family != "BYD-odor":
        findings.append(_finding("P08", "第二主机厂模板", "skip", "info", "非 BYD 模板；第二主机厂样例未接入"))
    else:
        findings.append(_finding("P08", "第二主机厂模板", "skip", "info", "当前仅覆盖 BYD；吉利/非吉利待样例"))
    return findings
