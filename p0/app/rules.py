"""Deterministic report-review rules for the P0 slice.

The rule set is deliberately small and explicit: every finding carries a
rule id, status, evidence and suggestion so the future review workbench can
show traceability instead of a black-box score.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable

from .docx_parser import ReportData, fields_for_consistency, parse_date


@dataclass
class Finding:
    rule_id: str
    title: str
    status: str  # pass | fail | warn | skip
    severity: str  # high | medium | low | info
    message: str
    evidence: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecordInput:
    task_no: str = ""
    report_no: str = ""
    sample_no: str = ""
    limit: float | None = None
    scores: list[float] | None = None
    reported_mean: float | None = None
    test_date: str = ""
    eval_date: str = ""
    judgment: str = ""


RULES: list[tuple[str, str, str]] = [
    ("R01", "页眉报告编号一致", "封面与正文各页报告编号必须一致"),
    ("R02", "检验均值计算", "三次评分均值应正确、保留 1 位小数"),
    ("R03", "报告内字段一致", "摘要页与样品情况表字段必须一致"),
    ("R04", "时间顺序", "生产→抽样→送样→检验→批准顺序正确"),
    ("R05", "设备溯源有效", "检验日期在设备溯源有效期内"),
    ("R06", "符合性判定", "结论/判定与均值、限值一致"),
    ("R07", "页码完整", "共 N 页与第 1..N 页连续"),
    ("R08", "注意事项条数", "中文报告注意事项应为 8 条"),
    ("R09", "标记符号", "报告不得含黄色高亮/红色字体"),
    ("R10", "签名/印章完整", "签名姓名与印章图片应完整嵌入"),
    ("R11", "模板识别", "按检验依据/项目识别报告模板"),
]


def _mean(scores: Iterable[float]) -> float | None:
    values = [value for value in scores if isinstance(value, (int, float))]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _date(value: str) -> date | None:
    return parse_date(value)


def _pass(rule_id: str, title: str, message: str, evidence: str = "") -> Finding:
    return Finding(rule_id, title, "pass", "info", message, evidence)


def _fail(rule_id: str, title: str, message: str, evidence: str, severity: str = "high", suggestion: str = "") -> Finding:
    return Finding(rule_id, title, "fail", severity, message, evidence, suggestion)


def _skip(rule_id: str, title: str, message: str) -> Finding:
    return Finding(rule_id, title, "skip", "info", message)


def r01_header(data: ReportData) -> Finding:
    if not data.cover_number:
        return _fail("R01", "页眉报告编号一致", "封面未识别到报告编号", "封面报告编号为空", "high", "补齐封面报告编号")
    bad = [value for value in data.header_numbers if value != data.cover_number]
    if bad:
        blanks = sum(1 for value in data.header_numbers if not value)
        return _fail(
            "R01",
            "页眉报告编号一致",
            f"正文页眉报告编号与封面不一致（空白 {blanks} 页）",
            f"封面={data.cover_number}; 正文={data.header_numbers}",
            "high",
            "统一所有页眉报告编号，或由模板域自动填充",
        )
    return _pass("R01", "页眉报告编号一致", f"封面与正文均为 {data.cover_number}", f"页眉={data.header_numbers}")


def r02_mean(data: ReportData) -> Finding:
    computed = _mean(data.scores)
    if computed is None:
        return _skip("R02", "检验均值计算", "未识别到 3 名评价员的检验结果")
    if data.reported_mean is None:
        return _fail(
            "R02",
            "检验均值计算",
            f"检验均值缺失；按 3 次评分应为 {computed}",
            f"scores={data.scores}; 报告均值=空",
            "high",
            "在结果表中填写检验均值，或由模板公式自动计算",
        )
    if abs(computed - data.reported_mean) > 0.05:
        return _fail(
            "R02",
            "检验均值计算",
            f"检验均值错误：报告 {data.reported_mean}，计算 {computed}",
            f"scores={data.scores}; 报告均值={data.reported_mean}",
            "high",
            "按算术平均并四舍五入到 1 位小数重新计算",
        )
    return _pass("R02", "检验均值计算", f"均值 {computed} 正确", f"scores={data.scores}")


def r03_fields(data: ReportData) -> Finding:
    mismatches = fields_for_consistency(data)
    if mismatches:
        text = "; ".join(f"{label}: {left or '空'} vs {right or '空'}" for label, left, right in mismatches)
        return _fail(
            "R03",
            "报告内字段一致",
            f"摘要页与样品情况表存在 {len(mismatches)} 处字段不一致",
            text,
            "high",
            "统一字段口径，并确认占位符已替换",
        )
    return _pass("R03", "报告内字段一致", "摘要页与样品情况表字段一致")


def r04_dates(data: ReportData) -> Finding:
    production = _date(data.production_date)
    sampling = _date(data.sample_date)
    receive = _date(data.receive_date)
    test_start = _date(data.test_start)
    test_end = _date(data.test_end)
    approval = _date(data.approval_date)

    problems: list[str] = []
    if production and sampling and production > sampling:
        problems.append(f"生产 {production} 晚于抽样 {sampling}")
    if production and receive and production > receive:
        problems.append(f"生产 {production} 晚于送样 {receive}")
    if receive and test_start and receive > test_start:
        problems.append(f"送样 {receive} 晚于检验开始 {test_start}")
    if sampling and test_start and sampling > test_start:
        problems.append(f"抽样 {sampling} 晚于检验开始 {test_start}")
    if test_start and test_end and test_start > test_end:
        problems.append(f"检验开始 {test_start} 晚于检验结束 {test_end}")
    if test_end and approval and test_end > approval:
        problems.append(f"检验结束 {test_end} 晚于批准 {approval}")

    if problems:
        return _fail(
            "R04",
            "时间顺序",
            "时间顺序存在异常",
            "; ".join(problems),
            "high",
            "按生产→抽样→送样→检验→批准顺序核对日期",
        )
    if not any([production, sampling, receive, test_start, test_end]):
        return _skip("R04", "时间顺序", "未识别到足够日期")
    return _pass(
        "R04",
        "时间顺序",
        "日期顺序正确",
        f"生产={production}; 抽样={sampling}; 送样={receive}; 检验={test_start}~{test_end}; 批准={approval}",
    )


def r05_device(data: ReportData) -> Finding:
    valid = _date(data.device_valid)
    test_end = _date(data.test_end) or _date(data.test_start)
    if valid is None or test_end is None:
        return _skip("R05", "设备溯源有效", "未识别到设备有效期或检验日期")
    if valid < test_end:
        return _fail(
            "R05",
            "设备溯源有效",
            f"设备溯源有效期 {valid} 早于检验日期 {test_end}",
            f"设备溯源={valid}; 检验={test_end}",
            "high",
            "更换在有效期内的设备，或补充期间核查记录",
        )
    return _pass("R05", "设备溯源有效", f"设备有效期 {valid} 覆盖检验日期 {test_end}")


def r06_judgment(data: ReportData) -> Finding:
    computed = _mean(data.scores)
    if computed is None:
        return _skip("R06", "符合性判定", "未识别到检验结果")
    if data.limit is None:
        return _skip("R06", "符合性判定", "未识别到限值")
    expected = "不符合" if computed > data.limit else "符合"
    conclusion = data.conclusion or ""
    judgment = data.judgment or ""
    if expected == "不符合" and ("不符合" not in conclusion and "不符合" not in judgment):
        return _fail(
            "R06",
            "符合性判定",
            f"均值 {computed} 大于限值 {data.limit}，结论/判定应为不符合",
            f"结论={conclusion[:80]}; 判定={judgment}",
            "high",
            "同步修正结论页和结果表的符合性判定",
        )
    if expected == "符合" and ("不符合" in conclusion or "不符合" in judgment):
        return _fail(
            "R06",
            "符合性判定",
            f"均值 {computed} 未超过限值 {data.limit}，结论/判定不应为不符合",
            f"结论={conclusion[:80]}; 判定={judgment}",
            "high",
            "同步修正结论页和结果表的符合性判定",
        )
    return _pass("R06", "符合性判定", f"均值 {computed} / 限值 {data.limit} → {expected}", f"判定={judgment}")


def r07_pages(data: ReportData) -> Finding:
    if not data.page_totals or not data.page_numbers:
        return _skip("R07", "页码完整", "未识别到页脚页码")
    totals = set(data.page_totals)
    if len(totals) != 1:
        return _fail("R07", "页码完整", "各页总页数不一致", f"共 N 页={data.page_totals}", "medium", "统一总页数")
    total = next(iter(totals))
    expected = list(range(1, total + 1))
    if data.page_numbers != expected:
        return _fail(
            "R07",
            "页码完整",
            f"页码序列不连续，期望 1..{total}",
            f"实际={data.page_numbers}",
            "medium",
            "检查分页/页码域，补齐缺页或删除多余页",
        )
    return _pass("R07", "页码完整", f"页码 1..{total} 连续")


def r08_notes(data: ReportData) -> Finding:
    if data.notices == 0:
        return _skip("R08", "注意事项条数", "未识别到注意事项段落")
    if data.notices != 8:
        return _fail(
            "R08",
            "注意事项条数",
            f"中文报告注意事项应为 8 条，实际 {data.notices} 条",
            f"notices={data.notices}",
            "medium",
            "按模板补齐/删除注意事项",
        )
    return _pass("R08", "注意事项条数", "注意事项 8 条")


def r09_markup(data: ReportData) -> Finding:
    if data.highlights or data.red_runs:
        return _fail(
            "R09",
            "标记符号",
            f"报告含黄色高亮 {data.highlights} 处、红色字体 {data.red_runs} 处",
            f"highlights={data.highlights}; red_runs={data.red_runs}",
            "medium",
            "签发前清除高亮/红字批注；若为草稿标记，需业务确认",
        )
    return _pass("R09", "标记符号", "未发现高亮/红字")


def r10_signature(data: ReportData) -> Finding:
    problems: list[str] = []
    if data.external_images and not data.embedded_images:
        problems.append(f"外部图片链接 {data.external_images} 个，嵌入图片 {data.embedded_images} 个")
    # Flag blank signature labels and missing embedded seals/signatures.
    if not data.embedded_images:
        problems.append("docx 内无嵌入的签名/印章图片")
        names = re.findall(r"(?:批准|审核|主检)\s*[:：]\s*([^\s|]+)", data.signature_text)
        if not names:
            problems.append("批准/审核/主检姓名缺失")
    if problems:
        return _fail(
            "R10",
            "签名/印章完整",
            "签名/印章可能不完整",
            "; ".join(problems),
            "high",
            "确认批准/审核/主检签名姓名，并将签名/印章图片嵌入文档",
        )
    return _pass("R10", "签名/印章完整", "签名/印章图片已嵌入")


def r11_template(data: ReportData) -> Finding:
    if data.template_family == "BYD-odor":
        return _pass("R11", "模板识别", "识别为比亚迪气味报告模板", f"basis={data.basis[:80]}")
    return Finding("R11", "模板识别", "warn", "low", "未识别为已知模板", f"basis={data.basis[:80]}", "确认主机厂模板")


def audit_report(data: ReportData) -> dict:
    findings = [
        r01_header(data),
        r02_mean(data),
        r03_fields(data),
        r04_dates(data),
        r05_device(data),
        r06_judgment(data),
        r07_pages(data),
        r08_notes(data),
        r09_markup(data),
        r10_signature(data),
        r11_template(data),
    ]
    return {
        "file": data.file_name,
        "template": data.template_family,
        "summary": {
            "total": len(findings),
            "fail": sum(1 for f in findings if f.status == "fail"),
            "warn": sum(1 for f in findings if f.status == "warn"),
            "pass": sum(1 for f in findings if f.status == "pass"),
            "skip": sum(1 for f in findings if f.status == "skip"),
        },
        "findings": [f.to_dict() for f in findings],
        "data": {
            "cover_number": data.cover_number,
            "sample_no": data.sample_no,
            "scores": data.scores,
            "computed_mean": _mean(data.scores),
            "reported_mean": data.reported_mean,
            "limit": data.limit,
            "judgment": data.judgment,
            "template": data.template_family,
        },
    }


def _rmean(scores: list[float] | None) -> float | None:
    return _mean(scores or [])


def validate_records(records: list[RecordInput]) -> dict:
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for index, record in enumerate(records, start=1):
        sample = record.sample_no or f"row-{index}"
        seen[sample] = seen.get(sample, 0) + 1
        computed = _rmean(record.scores)
        if computed is None:
            findings.append(_fail("REC-01", "记录均值", f"{sample}: 缺评分", f"row={index}", "high", "补齐 3 次评分"))
        elif record.reported_mean is None:
            findings.append(_fail("REC-01", "记录均值", f"{sample}: 缺复核均值", f"computed={computed}", "high", "填写复核均值"))
        elif abs(computed - record.reported_mean) > 0.05:
            findings.append(
                _fail("REC-01", "记录均值", f"{sample}: 复核均值 {record.reported_mean} 与计算 {computed} 不一致", f"row={index}", "high", "修正复核均值")
            )
        test_date = _date(record.test_date)
        eval_date = _date(record.eval_date)
        if test_date and eval_date and eval_date < test_date:
            findings.append(_fail("REC-02", "记录日期", f"{sample}: 评价日期早于试验日期", f"{test_date} > {eval_date}", "high", "核对日期"))
        if record.limit is not None and computed is not None and record.judgment:
            expected = "不符合" if computed > record.limit else "符合"
            if record.judgment != expected:
                findings.append(
                    _fail("REC-03", "记录判定", f"{sample}: 判定 {record.judgment} 与计算 {expected} 不一致", f"mean={computed}, limit={record.limit}", "high", "修正判定")
                )
    for sample, count in seen.items():
        if count > 1:
            findings.append(_fail("REC-04", "样品编号唯一", f"{sample}: 重复 {count} 次", "", "medium", "核对样品编号"))
    return {
        "summary": {
            "total": len(records),
            "fail": sum(1 for f in findings if f.status == "fail"),
        },
        "findings": [f.to_dict() for f in findings],
    }
