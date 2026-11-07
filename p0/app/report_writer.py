"""Render report-audit results as a review-ready Markdown/CSV deliverable.

The API returns JSON so the workbench can render its own table.  This module
adds the human-facing artefact required by the P0 demo: a review report with
every finding, evidence and suggested action, plus a batch summary.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .rules import RULES

_RULE_RANGE = f"R01–R{len(RULES):02d}"

STATUS_LABEL = {"fail": "不通过", "warn": "警告", "pass": "通过", "skip": "未检查"}
SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
LAYER_LABEL = {
    "deterministic": "确定性",
    "crosscheck": "跨文档",
    "partial": "部分规则",
    "dsl": "规则 DSL",
    "semantic": "语义",
}


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _location_text(location: object) -> str:
    if not isinstance(location, dict) or not location:
        return "未定位"
    page = location.get("page")
    parts = [f"p{page}" if page else "", str(location.get("section") or ""), str(location.get("cell") or "")]
    return " / ".join(part for part in parts if part)


def audit_markdown(result: dict, meta: dict | None = None, decisions: dict[str, str] | None = None) -> str:
    """Return a Markdown audit report for one parsed .docx report."""
    meta = meta or {}
    decisions = decisions or {}
    summary = result.get("summary", {})
    findings = result.get("findings", [])
    file_name = result.get("file", meta.get("file_name", ""))
    template = result.get("template", "")
    audit_mode = f"{_RULE_RANGE} 确定性规则 + X01–X04 跨文档"
    if result.get("stage2"):
        audit_mode += " + 部分规则 + 规则 DSL + 语义审核"

    lines = [
        f"# 报告审核报告：{file_name}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 审核方式：{audit_mode}（P0，不替代授权签字人）",
        f"- 模板识别：{template or '未识别'}",
        "",
        "## 1. 审核结论",
        "",
        f"共执行 {summary.get('total', len(findings))} 条规则："
        f"不通过 {summary.get('fail', 0)} 条、警告 {summary.get('warn', 0)} 条、"
        f"通过 {summary.get('pass', 0)} 条、未检查 {summary.get('skip', 0)} 条。",
        "",
    ]
    if summary.get("fail", 0):
        lines.append(
            f"**结论：建议退回修改。** 存在 {summary['fail']} 项不通过项，"
            "其中高风险项需在签发前整改并复核。"
        )
    elif summary.get("warn", 0):
        lines.append("**结论：有条件通过。** 无高风险不通过项，警告项需业务确认。")
    else:
        lines.append("**结论：规则层面通过。** 仍需授权签字人完成最终签核。")

    stage2 = result.get("stage2") or {}
    if isinstance(stage2, dict) and stage2:
        partial = stage2.get("partial") or {}
        dsl = stage2.get("dsl") or {}
        semantic = stage2.get("semantic") or {}
        partial_status = partial.get("statuses") or {}
        semantic_status = semantic.get("status") or "skipped"
        semantic_text = {
            "ok": "已运行",
            "skipped": f"未启用（{semantic.get('reason') or '未配置'}）",
            "error": f"调用失败，需人工复核（{semantic.get('reason') or '未知错误'}）",
        }.get(semantic_status, semantic_status)
        lines += [
            "",
            "### 阶段 2 检查状态",
            "",
            f"- 部分规则（P01–P08）：{partial.get('count', 0)} 条，"
            f"不通过 {partial_status.get('fail', 0)} / 警告 {partial_status.get('warn', 0)} / "
            f"通过 {partial_status.get('pass', 0)} / 未检查 {partial_status.get('skip', 0)}。",
            f"- 规则 DSL：v{dsl.get('version', 0)}，共 {dsl.get('rules', 0)} 条，"
            f"命中 {dsl.get('hits', {}) or '无'}。",
            f"- 语义审核：{semantic_text}；结论不得视为语义通过。",
        ]
    lines += [
        "",
        "## 2. 问题清单",
        "",
        "| 层 | 规则 | 定位 | 检查项 | 状态 | 严重级 | 问题 | 证据 | 建议 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for finding in findings:
        lines.append(
            "| {layer} | {rule} | {location} | {title} | {status} | {severity} | {message} | {evidence} | {suggestion} |".format(
                layer=_cell(LAYER_LABEL.get(finding.get("layer"), finding.get("layer") or "确定性")),
                rule=_cell(finding.get("rule_id")),
                location=_cell(_location_text(finding.get("location"))),
                title=_cell(finding.get("title")),
                status=STATUS_LABEL.get(finding.get("status"), finding.get("status")),
                severity=SEVERITY_LABEL.get(finding.get("severity"), finding.get("severity")),
                message=_cell(finding.get("message")),
                evidence=_cell(finding.get("evidence")),
                suggestion=_cell(finding.get("suggestion")),
            )
        )

    data = result.get("data", {})
    lines += [
        "",
        "## 3. 关键数据",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| 封面报告编号 | {_cell(data.get('cover_number'))} |",
        f"| 样品编号 | {_cell(data.get('sample_no'))} |",
        f"| 评价员评分 | {_cell(data.get('scores'))} |",
        f"| 计算均值 / 报告均值 | {_cell(data.get('computed_mean'))} / {_cell(data.get('reported_mean'))} |",
        f"| 限值 | {_cell(data.get('limit'))} |",
        f"| 符合性判定 | {_cell(data.get('judgment'))} |",
        "",
        "## 4. 人工复核记录",
        "",
        "| 规则 | 审核员意见 | 处理结果 |",
        "|---|---|---|",
    ]
    for finding in findings:
        if finding.get("status") in ("fail", "warn"):
            rule_id = finding.get("rule_id")
            decision = decisions.get(rule_id, "待确认")
            lines.append(f"| {_cell(rule_id)} | {_cell(decision)} | 待整改/驳回 |")

    lines += [
        "",
        "---",
        "",
        "> 本报告由 P0 演示系统生成。规则结论必须由授权审核员/签字人确认；"
        "扫描件、印章真伪、照片内容识别不在 P0 范围内。",
        "",
    ]
    return "\n".join(lines)


def batch_summary(results: Iterable[dict]) -> tuple[list[dict], dict[str, int]]:
    """Return per-file rows and per-rule fail counts for a draft batch."""
    rows: list[dict] = []
    rule_fail: dict[str, int] = {}
    for result in results:
        findings = result.get("findings", [])
        statuses = {finding["rule_id"]: finding["status"] for finding in findings}
        row = {
            "file": result.get("file", ""),
            "fail": result.get("summary", {}).get("fail", 0),
            "warn": result.get("summary", {}).get("warn", 0),
            "pass": result.get("summary", {}).get("pass", 0),
            "skip": result.get("summary", {}).get("skip", 0),
        }
        for rule_id, status in statuses.items():
            row[rule_id] = status
            if status == "fail":
                rule_fail[rule_id] = rule_fail.get(rule_id, 0) + 1
        rows.append(row)
    return rows, rule_fail


def batch_markdown(rows: list[dict], rule_fail: dict[str, int], rules: list[tuple[str, str, str]]) -> str:
    lines = [
        "# 气味报告草稿批量审核汇总（P0）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 样本：{len(rows)} 份 docx 草稿",
        f"- 规则：{_RULE_RANGE}，逐份输出问题/证据/建议；本汇总只做规则层统计",
        "",
        "## 1. 规则命中分布",
        "",
        "| 规则 | 检查项 | 不通过份数 |",
        "|---|---|---:|",
    ]
    for rule_id, title, _ in rules:
        lines.append(f"| {rule_id} | {_cell(title)} | {rule_fail.get(rule_id, 0)} |")
    lines += [
        "",
        "## 2. 逐份结果",
        "",
        "| 文件 | 不通过 | 警告 | 通过 | 未检查 | 主要问题 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        failed = [rule for rule, value in row.items() if rule.startswith("R") and value == "fail"]
        lines.append(
            f"| {_cell(row['file'])} | {row['fail']} | {row['warn']} | {row['pass']} | "
            f"{row['skip']} | {_cell('、'.join(failed))} |"
        )
    lines += [
        "",
        "## 3. 说明",
        "",
        "- R09/R10 对草稿中的高亮/外部图片链接按规则判为不通过，签发前需清除或嵌入。",
        "- R03 命中的是模板占位符未替换（XXXXX/XXXX），属于真实草稿缺陷。",
        "- 本汇总不替代人工审核结论；样本为同一样品的 12 份模板变体，指标只作 P0 基线。",
        "",
    ]
    return "\n".join(lines)
