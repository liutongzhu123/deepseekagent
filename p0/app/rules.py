"""Deterministic report-review rules for the P0 slice.

The rule set is deliberately small and explicit: every finding carries a
rule id, status, evidence and suggestion so the future review workbench can
show traceability instead of a black-box score.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Iterable

from . import rule_dsl, semantic_audit
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
    location: dict = field(default_factory=dict)
    layer: str = "deterministic"

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


DEFAULT_MEAN_TOLERANCE = 0.05
DEFAULT_EXPECTED_NOTICES = 8

_RULE_FUNCTIONS = {
    "R01": "r01_header",
    "R02": "r02_mean",
    "R03": "r03_fields",
    "R04": "r04_dates",
    "R05": "r05_device",
    "R06": "r06_judgment",
    "R07": "r07_pages",
    "R08": "r08_notes",
    "R09": "r09_markup",
    "R10": "r10_signature",
    "R11": "r11_template",
}


def _rule_meta(config: dict | None) -> tuple[dict[str, dict], float, int]:
    """Return (rule_id -> config, mean tolerance, expected notices)."""
    config = config or {}
    rules = {
        item.get("id"): item
        for item in (config.get("rules") or [])
        if isinstance(item, dict) and item.get("id")
    }
    tolerance = config.get("mean_tolerance", DEFAULT_MEAN_TOLERANCE)
    expected = config.get("expected_notices", DEFAULT_EXPECTED_NOTICES)
    try:
        tolerance = abs(float(tolerance))
    except (TypeError, ValueError):
        tolerance = DEFAULT_MEAN_TOLERANCE
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        expected = DEFAULT_EXPECTED_NOTICES
    return rules, tolerance, expected


def rules_catalog(config: dict | None = None) -> list[dict]:
    """Rule metadata for ``/api/v1/rules`` including admin overrides."""
    meta, _, _ = _rule_meta(config)
    catalog: list[dict] = []
    for rule_id, title, description in RULES:
        override = meta.get(rule_id, {})
        catalog.append(
            {
                "id": rule_id,
                "title": override.get("title") or title,
                "description": override.get("description", description),
                "enabled": override.get("enabled", True),
                "severity": override.get("severity", "medium"),
            }
        )
    return catalog


def _apply_rule_meta(finding: Finding, override: dict | None) -> Finding:
    if not override:
        return finding
    if override.get("enabled") is False:
        return Finding(
            finding.rule_id,
            override.get("title") or finding.title,
            "skip",
            "info",
            "该规则已由管理员在规则配置中停用",
            "",
            "",
        )
    if override.get("title"):
        finding.title = override["title"]
    if finding.status in {"fail", "warn"} and override.get("severity"):
        finding.severity = override["severity"]
    return finding


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
    if data.source_format in {"pdf", "ocr"}:
        if not data.cover_number:
            return _skip("R01", "页眉报告编号一致", f"{data.source_format.upper()} 未识别到报告编号，无法核对")
        if not data.header_numbers:
            return _skip("R01", "页眉报告编号一致", f"{data.source_format.upper()} 未识别到页眉编号，无法核对")
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


def r02_mean(data: ReportData, tolerance: float = DEFAULT_MEAN_TOLERANCE) -> Finding:
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
    if abs(computed - data.reported_mean) > tolerance:
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
    if data.source_format in {"pdf", "ocr"} and not any(
        [data.sample_name, data.client, data.sample_no, data.model]
    ):
        return _skip("R03", "报告内字段一致", f"{data.source_format.upper()} 未识别到摘要/样品字段，无法核对")
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
    toc_problems: list[str] = []
    last_page = 0
    for entry in data.toc_entries:
        page = int(entry.get("page") or 0)
        title = entry.get("title") or ""
        if page < 1 or page > total:
            toc_problems.append(f"「{title}」页码 {page} 超出 1..{total}")
        if page < last_page:
            toc_problems.append(f"「{title}」页码 {page} 小于前一条 {last_page}")
        last_page = max(last_page, page)
    if toc_problems:
        return _fail(
            "R07",
            "页码完整",
            "目录页码与正文不一致",
            "; ".join(toc_problems[:5]),
            "medium",
            "核对目录页码与正文实际页码，更新目录域",
        )
    suffix = f"；目录 {len(data.toc_entries)} 条页码在 1..{total} 内" if data.toc_entries else ""
    return _pass("R07", "页码完整", f"页码 1..{total} 连续{suffix}")


def r08_notes(data: ReportData, expected_notices: int = DEFAULT_EXPECTED_NOTICES) -> Finding:
    if expected_notices <= 0:
        return _skip("R08", "注意事项条数", "规则配置为不校验注意事项条数")
    if data.notices == 0:
        return _skip("R08", "注意事项条数", "未识别到注意事项段落")
    if data.notices != expected_notices:
        return _fail(
            "R08",
            "注意事项条数",
            f"中文报告注意事项应为 {expected_notices} 条，实际 {data.notices} 条",
            f"notices={data.notices}",
            "medium",
            "按模板补齐/删除注意事项",
        )
    return _pass("R08", "注意事项条数", f"注意事项 {expected_notices} 条")


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
        variant = data.template_variant or "变体未识别"
        return _pass("R11", "模板识别", f"识别为比亚迪气味报告模板（{variant}）", f"basis={data.basis[:80]}")
    return Finding("R11", "模板识别", "warn", "low", "未识别为已知模板", f"basis={data.basis[:80]}", "确认主机厂模板")


_PLACEHOLDERS = {"", "xx", "xxxx", "xxxxx", "----", "-", "/", "无", "n/a", "na"}


def _known(value: object) -> bool:
    text = re.sub(r"\s+", "", str(value or "")).strip()
    if text.lower() in _PLACEHOLDERS:
        return False
    return not (set(text) <= {"x"} or set(text) <= {"-"})


def _norm_compare(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _same(left: object, right: object) -> bool | None:
    """None when either side is empty/placeholder; otherwise exact after normalization."""
    if not _known(left) or not _known(right):
        return None
    return _norm_compare(left) == _norm_compare(right)


def _quantity_same(left: object, right: object) -> bool | None:
    if not _known(left) or not _known(right):
        return None
    left_nums = re.findall(r"\d+(?:\.\d+)?", str(left))
    right_nums = re.findall(r"\d+(?:\.\d+)?", str(right))
    if len(left_nums) == 1 and len(right_nums) == 1:
        try:
            return float(left_nums[0]) == float(right_nums[0])
        except ValueError:
            pass
    return _same(left, right)


def _crosscheck_task_order(data: ReportData, inputs: object) -> Finding:
    order: dict | None = None
    if data.cover_number:
        rows = inputs.find("task_orders", "report_no", data.cover_number)  # type: ignore[attr-defined]
        if rows:
            order = rows[0]
    if order is None and data.sample_no:
        rows = inputs.find("task_orders", "sample_no", data.sample_no)  # type: ignore[attr-defined]
        if rows:
            order = rows[0]
    if order is None:
        return _skip("X01", "报告—任务单一致", "未找到与报告编号/样品编号匹配的任务单")
    mismatches: list[str] = []
    compared = 0
    pairs = [
        ("报告编号", data.cover_number, order.get("report_no")),
        ("样品编号", data.sample_no, order.get("sample_no")),
        ("委托单位", data.client, order.get("customer")),
        ("样品名称", data.sample_name, order.get("sample_name")),
        ("检验项目", data.item, order.get("item")),
    ]
    for label, left, right in pairs:
        same = _same(left, right)
        if same is not None:
            compared += 1
        if same is False:
            mismatches.append(f"{label}: 报告={left or '空'} / 任务单={right or '空'}")
    if mismatches:
        return _fail(
            "X01", "报告—任务单一致", f"报告与任务单存在 {len(mismatches)} 处不一致",
            "; ".join(mismatches), "high", "核对任务单字段，统一报告与任务单口径",
        )
    if compared == 0:
        return _skip("X01", "报告—任务单一致", "报告与任务单的可比字段均为空或占位")
    return _pass("X01", "报告—任务单一致", "报告与任务单关键字段一致", f"任务单={order.get('order_no', '')}")


def _crosscheck_sample(data: ReportData, inputs: object) -> Finding:
    rows = []
    if data.sample_no:
        rows = inputs.find("samples", "sample_no", data.sample_no)  # type: ignore[attr-defined]
    if not rows and data.cover_number:
        rows = inputs.find("samples", "report_no", data.cover_number)  # type: ignore[attr-defined]
    if not rows:
        return _skip("X02", "报告—样品信息一致", "未找到与样品编号/报告编号匹配的样品记录")
    sample = rows[0]
    mismatches: list[str] = []
    compared = 0
    same_name = _same(data.sample_name, sample.get("sample_name"))
    if same_name is not None:
        compared += 1
    if same_name is False:
        mismatches.append(f"样品名称: 报告={data.sample_name or '空'} / 样品系统={sample.get('sample_name') or '空'}")
    same_qty = _quantity_same(data.sample_qty, sample.get("quantity"))
    if same_qty is not None:
        compared += 1
    if same_qty is False:
        mismatches.append(f"样品数量: 报告={data.sample_qty or '空'} / 样品系统={sample.get('quantity') or '空'}")
    if mismatches:
        return _fail(
            "X02", "报告—样品信息一致", f"报告与样品记录存在 {len(mismatches)} 处不一致",
            "; ".join(mismatches), "high", "核对样品编号、名称与数量",
        )
    if compared == 0:
        return _skip("X02", "报告—样品信息一致", "报告与样品记录的可比字段均为空或占位")
    return _pass("X02", "报告—样品信息一致", "报告与样品记录关键字段一致", f"样品={sample.get('sample_no', '')}")


def _crosscheck_record(data: ReportData, inputs: object) -> Finding:
    rows = []
    if data.cover_number:
        rows = inputs.find("original_records", "report_no", data.cover_number)  # type: ignore[attr-defined]
    if not rows and data.sample_no:
        rows = inputs.find("original_records", "sample_no", data.sample_no)  # type: ignore[attr-defined]
    if not rows:
        return _skip("X03", "报告—原始记录一致", "未找到与报告编号/样品编号匹配的原始记录")
    record = rows[0]
    mismatches: list[str] = []
    compared = 0
    same_sample = _same(data.sample_no, record.get("sample_no"))
    if same_sample is not None:
        compared += 1
    if same_sample is False:
        mismatches.append(f"样品编号: 报告={data.sample_no or '空'} / 原始记录={record.get('sample_no') or '空'}")
    record_date = parse_date(record.get("test_date", ""))
    start = parse_date(data.test_start)
    end = parse_date(data.test_end) or start
    if record_date and start and end:
        compared += 1
        if not (start <= record_date <= end):
            mismatches.append(f"检验日期 {record_date} 不在报告检验时间 {start}~{end} 内")
    if mismatches:
        return _fail(
            "X03", "报告—原始记录一致", f"报告与原始记录存在 {len(mismatches)} 处不一致",
            "; ".join(mismatches), "high", "核对原始记录日期与样品编号",
        )
    if compared == 0:
        return _skip("X03", "报告—原始记录一致", "报告与原始记录的可比字段均为空或占位")
    return _pass("X03", "报告—原始记录一致", "报告与原始记录关键字段一致", f"记录={record.get('record_no', '')}")


def _crosscheck_equipment(data: ReportData, inputs: object) -> Finding:
    devices = data.devices or []
    if not devices:
        return _skip("X04", "设备台账校验", "报告中未识别到设备信息")
    problems: list[str] = []
    missing = 0
    missing_ids: list[str] = []
    compared = 0
    test_end = parse_date(data.test_end) or parse_date(data.test_start)
    for item in devices:
        device_id = (item.get("device_id") or "").strip()
        if not device_id:
            missing += 1
            missing_ids.append("(无编号)")
            continue
        rows = inputs.find("equipment", "device_id", device_id)  # type: ignore[attr-defined]
        if not rows:
            missing += 1
            missing_ids.append(device_id)
            continue
        equipment = rows[0]
        compared += 1
        valid_until = parse_date(equipment.get("valid_until", ""))
        if valid_until and test_end and valid_until < test_end:
            problems.append(f"{device_id}: 台账有效期 {valid_until} 早于检验日期 {test_end}")
        status = (equipment.get("status") or "").strip()
        if any(word in status for word in ("停用", "维修", "检修", "报废", "待机")):
            problems.append(f"{device_id}: 台账状态={status}")
    if problems:
        return _fail(
            "X04", "设备台账校验", f"设备台账存在 {len(problems)} 处问题",
            "; ".join(problems), "high", "核对设备台账有效期与状态，或更换设备",
        )
    if compared == 0:
        return _skip("X04", "设备台账校验", "未找到与报告设备编号匹配的台账记录")
    if missing:
        return Finding(
            "X04", "设备台账校验", "warn", "medium",
            f"{compared} 台设备已核对，{missing} 台无匹配台账",
            "、".join(missing_ids),
            "补充设备台账或确认设备编号",
        )
    return _pass("X04", "设备台账校验", f"{compared} 台设备台账有效期/状态正常", "")


def crosscheck_report(data: ReportData, inputs: object) -> list[Finding]:
    """Data-driven checks appended only when a local inbox is configured."""
    return [
        _crosscheck_task_order(data, inputs),
        _crosscheck_sample(data, inputs),
        _crosscheck_record(data, inputs),
        _crosscheck_equipment(data, inputs),
    ]


_LOCATIONS = {
    "R01": {"page": 1, "section": "页眉", "cell": "报告编号"},
    "R02": {"page": 0, "section": "检验结果表", "cell": "检验均值"},
    "R03": {"page": 1, "section": "摘要页/样品情况表", "cell": "样品名称/数量/编号"},
    "R04": {"page": 1, "section": "日期栏", "cell": "生产/抽样/送样/检验/批准日期"},
    "R05": {"page": 0, "section": "设备信息表", "cell": "溯源有效期"},
    "R06": {"page": 0, "section": "检验结果表", "cell": "符合性判定"},
    "R07": {"page": 1, "section": "目录/页脚", "cell": "页码"},
    "R08": {"page": 0, "section": "注意事项", "cell": "条数"},
    "R09": {"page": 1, "section": "全文", "cell": "高亮/红色字体"},
    "R10": {"page": 0, "section": "签名栏", "cell": "批准/审核/主检/印章"},
    "R11": {"page": 1, "section": "检验依据", "cell": "模板"},
}


def _location_for(rule_id: str, data: ReportData) -> dict:
    location = dict(_LOCATIONS.get(rule_id, {"page": 1, "section": "全文", "cell": ""}))
    total = max(data.page_totals) if data.page_totals else 0
    if rule_id in {"R02", "R06"}:
        location["page"] = next(
            (entry["page"] for entry in data.toc_entries if "检验结果" in entry.get("title", "")),
            3,
        )
    elif rule_id in {"R05", "R08", "R10"}:
        location["page"] = total or 1
    return location


def report_text(data: ReportData) -> str:
    """Return the searchable report text (paragraphs plus table cells).

    The semantic layer receives text only; no file, image or path is sent.
    """
    parts = [text.strip() for text in data.paragraphs if text and text.strip()]
    for table in data.tables:
        for row in table:
            cells = [cell.text.strip() for cell in row if cell and cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _semantic_finding(status: str, severity: str, message: str, evidence: str = "", suggestion: str = "") -> Finding:
    return Finding(
        "SEM",
        "语义审核",
        status,
        severity,
        message,
        evidence,
        suggestion,
        {"page": 0, "section": "语义审核", "cell": ""},
        "semantic",
    )


def _finding_from_dict(item: dict) -> Finding:
    return Finding(
        str(item.get("rule_id") or ""),
        str(item.get("title") or ""),
        str(item.get("status") or "skip"),
        str(item.get("severity") or "info"),
        str(item.get("message") or ""),
        str(item.get("evidence") or ""),
        str(item.get("suggestion") or ""),
        dict(item.get("location") or {}),
        str(item.get("layer") or "deterministic"),
    )


def _stage2_findings(
    data: ReportData,
    inputs: object | None,
    semantic_config: object | None,
    semantic_post: object | None,
) -> tuple[list[Finding], dict]:
    """Run T201/T202/T203 layers and return their findings plus a status block."""
    from .partial_rules import evaluate_partial_rules  # local import: avoids a rules<->partial cycle

    findings: list[Finding] = []

    partial = evaluate_partial_rules(data, inputs)
    findings.extend(partial)
    partial_meta = {
        "count": len(partial),
        "statuses": {
            status: sum(1 for item in partial if item.status == status)
            for status in ("fail", "warn", "pass", "skip")
        },
    }

    pack = rule_dsl.load_pack()
    report_context = {
        "client": data.client,
        "sample_qty": data.sample_qty,
        "sample_no": data.sample_no,
        "sample_name": data.sample_name,
        "basis": data.basis,
        "item": data.item,
        "judgment": data.judgment,
        "conclusion": data.conclusion,
        "reported_mean": data.reported_mean,
        "limit": data.limit,
        "template": data.template_family,
        "toc_first_page": (data.toc_entries[0].get("page") if data.toc_entries else None),
    }
    context: dict = {"report": report_context}
    sources = {"report"}
    if inputs is not None:
        task_orders = inputs.find("task_orders", "report_no", data.cover_number)  # type: ignore[attr-defined]
        if not task_orders:
            task_orders = inputs.datasets.get("task_orders", [])  # type: ignore[attr-defined]
        if task_orders:
            context["inputs"] = dict(task_orders[0])
            sources.add("inputs")
    dsl_findings = [_finding_from_dict(item) for item in pack.evaluate(context, available_sources=sources)]
    findings.extend(dsl_findings)
    for rule_id in pack.skipped:
        rule = pack.by_id(rule_id)
        if rule is not None:
            findings.append(
                Finding(
                    rule.id,
                    rule.name,
                    "skip",
                    "info",
                    f"缺少 {rule.source} 数据，跳过规则 DSL 检查",
                    f"source={rule.source}",
                    "补齐对应数据后自动启用",
                    {"page": 1, "section": "规则 DSL", "cell": rule.field},
                    "dsl",
                )
            )
    dsl_meta = {
        "version": pack.version,
        "rules": len(pack.rules),
        "total": len(pack.rules),
        "hits": pack.stats()["hits"],
        "skipped": pack.stats()["skipped"],
    }

    semantic = semantic_audit.audit_semantics(
        report_text(data),
        config=semantic_config,  # type: ignore[arg-type]
        post=semantic_post,  # type: ignore[arg-type]
    )
    semantic_findings = semantic.get("findings") or []
    if semantic.get("status") == "ok":
        if semantic_findings:
            findings.extend(_finding_from_dict(item) for item in semantic_findings)
        else:
            findings.append(_semantic_finding("pass", "info", "语义审核完成，未发现明确问题"))
    else:
        reason = str(semantic.get("reason") or "semantic_unavailable")
        status = "skip" if semantic.get("status") == "skipped" else "warn"
        findings.append(
            _semantic_finding(
                status,
                "info" if status == "skip" else "medium",
                f"语义审核未完成：{reason}",
                suggestion="完成 D5 合规确认并配置 DeepSeek 后自动启用；当前结果不代表语义通过",
            )
        )
    semantic_meta = {
        "status": semantic.get("status", "skipped"),
        "reason": semantic.get("reason", ""),
        "model": semantic.get("model", ""),
        "findings": len(semantic_findings),
    }

    return findings, {
        "partial": partial_meta,
        "dsl": dsl_meta,
        "semantic": semantic_meta,
    }


def audit_report(
    data: ReportData,
    config: dict | None = None,
    inputs: object | None = None,
    *,
    stage2: bool = False,
    semantic_config: object | None = None,
    semantic_post: object | None = None,
) -> dict:
    """Run the enabled report rules with the configured thresholds/severity.

    When a local inbox snapshot is supplied, append the data-driven
    ``X01``–``X04`` cross-document checks.  ``stage2=True`` additionally runs
    the T202 partial rules, the T203 rule DSL and the T201 semantic layer;
    the default keeps the stage-1 result unchanged for regression baselines.
    """
    meta, tolerance, expected_notices = _rule_meta(config)
    findings = []
    for rule_id, function_name in _RULE_FUNCTIONS.items():
        if rule_id == "R02":
            finding = r02_mean(data, tolerance)
        elif rule_id == "R08":
            finding = r08_notes(data, expected_notices)
        else:
            finding = globals()[function_name](data)
        finding = _apply_rule_meta(finding, meta.get(rule_id))
        finding.location = _location_for(rule_id, data)
        findings.append(finding)
    if inputs is not None:
        for finding in crosscheck_report(data, inputs):
            finding.layer = "crosscheck"
            findings.append(finding)
    stage2_meta: dict | None = None
    if stage2:
        extra, stage2_meta = _stage2_findings(data, inputs, semantic_config, semantic_post)
        findings.extend(extra)
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
        "stage2": stage2_meta,
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


def validate_records(records: list[RecordInput], config: dict | None = None) -> dict:
    _, tolerance, _ = _rule_meta(config)
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
        elif abs(computed - record.reported_mean) > tolerance:
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
