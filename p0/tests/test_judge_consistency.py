"""R25 结论页无判定时结果表不得判定（RA-1.6）。

失败场景 F1–F11 见 ``p0/app/format_rules.py``；验收两条：
结论页无判定表述而结果表有判定 -> fail；两边都有判定 -> pass。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.admin_config import default_config
from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")
HEADER = ["序号", "检验项目", "评价员", "检验结果", "检验均值", "限值要求", "符合性判定"]


def _rows(*judgments):
    return [list(HEADER)] + [
        ["1", "气味", f"评价员{index}", "4.0", "", "≤3.3", value]
        for index, value in enumerate(judgments, start=1)
    ]


def _data(conclusion="", judgment="", rows=None, paragraphs=None):
    return ReportData(
        file_name="judge.docx",
        conclusion=conclusion,
        judgment=judgment,
        result_cells=_rows(judgment) if rows is None else rows,
        paragraphs=paragraphs or [],
    )


def _r25(data):
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R25")


def test_rule_is_in_catalog_and_admin_default():
    assert (
        "R25",
        "结论页无判定时结果表不得判定",
        "结论页无判定表述时，检验结果表的符合性判定不得出现判定表述；两边都有判定不报",
    ) in RULES
    rules = {item["id"]: item for item in default_config()["report"]["rules"]}
    assert rules["R25"]["enabled"] is True
    assert rules["R25"]["severity"] == "medium"


@pytest.mark.parametrize(
    "conclusion",
    ["经检验，该样品气味检验结果见检验结果表。", "该样品检验结果详见附表。", "详见结果表", "—", "/", "不适用"],
)
def test_conclusion_without_judgment_and_table_judgment_fails(conclusion):
    # 验收：结论页无判定表述时结果表有判定即报出。
    finding = _r25(_data(conclusion=conclusion, judgment="不符合"))

    assert finding["status"] == "fail"
    assert finding["severity"] == "medium"
    assert "结论页无判定表述" in finding["message"]
    assert "不符合" in finding["message"]
    assert "符合性判定=不符合" in finding["evidence"]
    assert finding["location"]["section"] == "检验结论/检验结果表"
    assert "不符合" in finding["location"]["cell"]


@pytest.mark.parametrize(
    ("conclusion", "judgment"),
    [
        ("经检验，该样品气味的检验结果符合委托单位提供限值的要求。", "符合"),
        ("经检验，该样品气味的检验结果不符合委托单位提供限值：气味等级≤3.3的要求。", "不符合"),
        ("检验结论：该样品经检验不合格", "不合格"),
        ("检验结论：该样品满足委托单位要求", "满足"),
        ("检验结论：该样品通过", "通过"),
    ],
)
def test_both_judgments_pass(conclusion, judgment):
    # 验收：两边都有判定不报。
    finding = _r25(_data(conclusion=conclusion, judgment=judgment))

    assert finding["status"] == "pass"
    assert "均有判定表述" in finding["message"]


@pytest.mark.parametrize(
    ("conclusion", "judgment", "needle"),
    [
        ("详见结果表", "", "均无判定表述"),
        ("详见结果表", "—", "均无判定表述"),
        ("详见结果表", "不适用", "均无判定表述"),
        ("详见结果表", "未判定", "均无判定表述"),
        ("经检验，该样品符合委托单位提供限值的要求。", "/", "结论页有判定表述"),
    ],
)
def test_other_combinations_pass(conclusion, judgment, needle):
    assert _r25(_data(conclusion=conclusion, judgment=judgment))["status"] == "pass"
    assert needle in _r25(_data(conclusion=conclusion, judgment=judgment))["message"]


def test_any_result_row_with_judgment_counts():
    # 第一行杠掉、后面行有判定，也算结果表出现判定表述。
    finding = _r25(_data(conclusion="详见结果表", judgment="—", rows=_rows("—", "符合")))

    assert finding["status"] == "fail"
    assert "符合" in finding["message"]
    assert "符合性判定=—、符合" in finding["evidence"]


def test_noun_forms_are_not_verdicts():
    for conclusion in ["检验结论：详见结果表符合性判定。", "比对合格证书编号见结果表。", "通过率统计见附表。"]:
        assert _r25(_data(conclusion=conclusion, judgment="符合"))["status"] == "fail"


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (ReportData(file_name="judge.docx", conclusion="符合"), "未识别到检验结果表"),
        (_data(conclusion="详见结果表", rows=[["序号", "评价员", "检验结果"], ["1", "甲", "4.0"]]), "符合性判定"),
        (_data(conclusion="", judgment="符合"), "检验结论"),
    ],
)
def test_undetectable_cases_skip(data, needle):
    # F1/F2/F5：看不到结果表、判定列或结论页时 skip，不把解析失败当无判定。
    finding = _r25(data)

    assert finding["status"] == "skip"
    assert needle in finding["message"]


def test_paragraph_conclusion_fallback():
    inline = _r25(_data(conclusion="", judgment="符合", paragraphs=["检验结论：经检验，该样品符合委托单位提供限值的要求。"]))
    heading = _r25(_data(conclusion="", judgment="不符合", paragraphs=["检验结论", "经检验，该样品气味检验结果不符合委托单位提供限值的要求。"]))
    blank = _r25(_data(conclusion="", judgment="符合", paragraphs=["检验结论", "签发日期：2025年03月20日"]))

    assert inline["status"] == "pass"
    assert heading["status"] == "pass"
    assert blank["status"] == "fail"  # 有「检验结论」标题但无结论正文 -> 无判定


@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="真实草稿样例未挂载")
def test_all_real_drafts_have_both_judgments():
    files = [path for path in sorted(SAMPLE_DIR.glob("*.docx")) if not path.name.startswith(".")]
    findings = [_r25(parse_docx(path)) for path in files]

    assert len(findings) == 12
    assert all(item["status"] == "pass" and "均有判定表述" in item["message"] for item in findings)
    assert "符合性判定=不符合" in findings[0]["evidence"]
