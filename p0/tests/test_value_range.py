"""R26 数值合理性、单位一致性与小数位数（RA-2.4 / RA-2.5）。

失败场景 F1–F12 见 ``p0/app/format_rules.py``；验收三条：
数值超出判定要求范围能报出、单位与判定要求不一致能报出、平行样小数位数不一致能报出。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_config import default_config
from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")
HEADER = ["序号", "检验项目", "样品编号", "评价员", "检验结果", "单位", "检验均值", "限值要求", "符合性判定"]


def _find(data):
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R26")


def _r26(*specs):
    rows = [list(HEADER)] + [
        [str(index), item, sample, "评价员", result, unit, mean, limit, judgment]
        for index, (item, sample, result, unit, mean, limit, judgment) in enumerate(specs, start=1)
    ]
    return _find(ReportData(file_name="value.docx", result_cells=rows))


def test_rule_is_in_catalog_and_admin_default():
    assert ("R26", "数值合理性与单位/小数位", "按限值/判定要求核对数值范围与单位一致性，并按平行样组核对小数位数") in RULES
    rule = {item["id"]: item for item in default_config()["report"]["rules"]}["R26"]
    assert rule["enabled"] is True and rule["severity"] == "medium"


@pytest.mark.parametrize(
    ("limit", "result", "judgment", "status"),
    [
        ("≤3.3", "4.0", "符合", "fail"),
        ("≥0.5", "0.3", "符合", "fail"),
        ("5~10", "11", "符合", "fail"),
        ("0.5±0.2", "0.8", "符合", "fail"),
        ("≤3.3", "4.0", "", "fail"),
        ("≤3.3", "3.3", "符合", "pass"),
        ("5～10", "5", "符合", "pass"),
        ("-5～5", "-3", "符合", "pass"),
        ("≤3.3", "4.0", "不符合", "pass"),
    ],
)
def test_value_range(limit, result, judgment, status):
    # F3/F4/F5/F10/F11：上限/下限/区间/公差/全角；已判不符合的超限值不重复报。
    finding = _r26(("气味", "S1", result, "", "", limit, judgment))
    assert finding["status"] == status
    if status == "fail":
        assert "超出判定要求" in finding["message"]
        assert finding["severity"] == "medium"
        assert finding["location"]["section"] == "检验结果表"


def test_mean_is_checked_too():
    finding = _r26(("压力", "S1", "9.5", "", "10.5", "≤10", "符合"))
    assert finding["status"] == "fail"
    assert "检验均值" in finding["message"]


@pytest.mark.parametrize(
    ("result", "unit", "limit", "status"),
    [
        ("9.5", "N·m", "≤10 MPa", "fail"),
        ("9.5 N·m", "", "≤10 MPa", "fail"),
        ("9.5 MPa", "N·m", "≤10 MPa", "fail"),
        ("9500 kPa", "", "≤10 MPa", "fail"),
        ("9.5MPa", "", "≤10 MPa", "pass"),
        ("9.5", "℃", "≤10 °C", "pass"),
        ("9.5", "bar", "≤10 MPa", "pass"),
        ("9.5", "", "≤10 MPa", "pass"),
    ],
)
def test_unit_consistency(result, unit, limit, status):
    # F6/F7：数值、单位列、表头与判定要求单位对比；只出现一侧或未知单位时保守跳过。
    finding = _r26(("压力", "S1", result, unit, "", limit, "符合"))
    assert finding["status"] == status
    if status == "fail":
        assert "单位" in finding["message"] and "不一致" in finding["message"]


@pytest.mark.parametrize(("second", "status"), [("3.50", "fail"), ("3.5", "pass")])
def test_parallel_group_decimals(second, status):
    # F8：同一检验项目+样品编号（平行样）的小数位数必须一致；已判不符合不影响小数位检查。
    finding = _r26(("气味", "S1", "4.0", "", "", "≤3.3", "不符合"), ("气味", "S1", second, "", "", "≤3.3", "不符合"))
    assert finding["status"] == status
    if status == "fail":
        assert "平行样小数位数不一致" in finding["message"]
        assert "4.0（1 位）" in finding["message"]


def test_different_items_are_not_parallel_groups():
    finding = _r26(("气味", "S1", "4.0", "", "", "≤3.3", "不符合"), ("拉伸", "S1", "3.50", "", "", "≤3.3", "不符合"))
    assert finding["status"] == "pass"


@pytest.mark.parametrize(
    ("rows", "needle"),
    [
        ([], "未识别到检验结果表"),
        ([["序号", "检验项目", "评价员", "检验均值"], ["1", "气味", "甲", "4.0"]], "没有「检验结果」列"),
        ([list(HEADER), ["1", "气味", "S1", "甲", "符合", "", "", "", "符合"]], "未识别到判定要求"),
    ],
)
def test_undetectable_cases_skip(rows, needle):
    # F1/F2/F10：看不到结果表、结果列或可解析的判定要求时 skip，不冒充 pass。
    finding = _find(ReportData(file_name="value.docx", result_cells=rows))
    assert finding["status"] == "skip"
    assert needle in finding["message"]


@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="真实草稿样例未挂载")
def test_all_real_drafts_pass():
    # F12：12 份 BYD 草稿均为“结果超限但判定不符合”的真实不合格结果，0 误报。
    files = [path for path in sorted(SAMPLE_DIR.glob("*.docx")) if not path.name.startswith(".")]
    findings = [_find(parse_docx(path)) for path in files]
    assert len(findings) == 12
    assert all(item["status"] == "pass" and "单位比对=0" in item["evidence"] for item in findings)
