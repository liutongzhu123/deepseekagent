"""RA-2.7: 重算表格中的平均值/最大值/变化率并与报告值比对。

BYD 12 份草稿的结果表只有评价员评分列、检验均值列为空，没有最大值/变化率列，
因此真实样例应返回 skip，不冒充 pass；本次用合成表覆盖 F1-F11。
"""
from __future__ import annotations

import pytest

from app.docx_parser import Cell, ReportData
from app.rules import RULES, audit_report


def _table(header: list[str], rows: list[list[str]]) -> list[list[Cell]]:
    return [[Cell(text) for text in header]] + [[Cell(text) for text in row] for row in rows]


def _finding(table: list[list[Cell]]) -> dict:
    data = ReportData(file_name="calc.docx", tables=[table])
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R20")


def test_r20_is_in_the_catalog_with_a_location():
    finding = _finding(_table(["样品", "测量1", "测量2", "平均值"], [["A", "1", "3", "2"]]))

    assert ("R20", "计算结果正确性", "重算平均值/最大值/变化率并与报告值比对，偏差超过容差报出") in RULES
    assert finding["location"] == {"page": 3, "section": "检验结果表", "cell": "平均值/最大值/变化率"}


@pytest.mark.parametrize(
    "header,row",
    [
        (["样品", "测量1", "测量2", "测量3", "平均值", "最大值"], ["A", "1.5", "2.5", "3.5", "2.5", "3.5"]),
        (["样品", "试验前", "试验后", "变化率"], ["A", "100", "120", "20"]),
        (["样品", "试验前", "试验后", "变化率"], ["A", "80", "60", "-25"]),
    ],
)
def test_matching_aggregates_pass(header, row):
    finding = _finding(_table(header, [row]))

    assert finding["status"] == "pass"
    assert "计算结果与报告值一致" in finding["message"]


@pytest.mark.parametrize(
    "header,row,fragment",
    [
        (["样品", "测量1", "测量2", "平均值"], ["A", "1", "3", "2.5"], "平均值"),
        (["样品", "测量1", "测量2", "最大值"], ["A", "1", "3", "2"], "最大值"),
        (["样品", "试验前", "试验后", "变化率"], ["A", "100", "120", "25"], "变化率"),
    ],
)
def test_deviation_over_tolerance_fails(header, row, fragment):
    finding = _finding(_table(header, [row]))

    assert finding["status"] == "fail"
    assert fragment in finding["evidence"]
    assert "偏差" in finding["evidence"]


def test_deviations_within_tolerance_pass():
    mean = _finding(_table(["样品", "测量1", "测量2", "平均值"], [["A", "1", "3", "2.04"]]))
    rate = _finding(_table(["样品", "试验前", "试验后", "变化率"], [["A", "100", "120", "20.05"]]))

    assert mean["status"] == "pass"
    assert rate["status"] == "pass"


@pytest.mark.parametrize(
    "table",
    [
        _table(["样品", "测量1", "测量2"], [["A", "1", "2"]]),  # F5 无聚合列
        _table(["样品", "测量1", "平均值"], [["A", "1", "1"]]),  # F6 只有一个测量值
        _table(["样品", "试验前", "变化率"], [["A", "100", "20"]]),  # F7 缺后值
        _table(["样品", "试验前", "试验后", "变化率"], [["A", "0", "20", "20"]]),  # F7 前值为 0
        _table(["样品", "测量1", "测量2", "平均值"], [["A", "1", "3", "—"]]),  # F8 占位符
    ],
)
def test_incomplete_or_unmatched_rows_skip(table):
    assert _finding(table)["status"] == "skip"


def test_non_measurement_columns_are_excluded():
    table = _table(
        ["序号", "样品编号", "检验日期", "限值", "测量1", "测量2", "平均值"],
        [["1", "20241125", "2025-03-02", "3.3", "1", "3", "2"]],
    )

    assert _finding(table)["status"] == "pass"


def test_partial_comparisons_only_report_the_bad_aggregate():
    table = _table(["样品", "测量1", "测量2", "平均值", "最大值"], [["A", "1", "3", "2", "4"]])
    finding = _finding(table)

    assert finding["status"] == "fail"
    assert "最大值" in finding["evidence"]
    assert "平均值" not in finding["evidence"]


def test_result_table_max_is_recomputed_down_the_evaluator_column():
    header = ["序号", "检验项目", "评价员", "检验结果", "最大值"]
    rows = [["1", "气味", "甲", "4.0", "3.5"], ["", "", "乙", "3.5", ""], ["", "", "丙", "3.0", ""]]
    finding = _finding(_table(header, rows))

    assert finding["status"] == "fail"
    assert "结果表 最大值" in finding["evidence"]


def test_result_table_max_pass():
    header = ["序号", "检验项目", "评价员", "检验结果", "最大值"]
    rows = [["1", "气味", "甲", "4.0", "4.0"], ["", "", "乙", "3.5", ""], ["", "", "丙", "3.0", ""]]

    assert _finding(_table(header, rows))["status"] == "pass"


def test_inspection_mean_header_is_recognized():
    finding = _finding(_table(["样品", "测量1", "测量2", "检验均值"], [["A", "1", "3", "3"]]))

    assert finding["status"] == "fail"
    assert "平均值" in finding["evidence"]


def test_generic_sample_table_max_is_not_grouped():
    table = _table(["样品", "检验结果", "最大值"], [["A", "1", "1"], ["B", "3", "3"]])

    assert _finding(table)["status"] == "skip"


def test_configured_tolerance_is_applied():
    table = _table(["样品", "测量1", "测量2", "平均值"], [["A", "1", "3", "2.04"]])
    data = ReportData(file_name="calc.docx", tables=[table])
    finding = next(
        item for item in audit_report(data, {"mean_tolerance": 0.01})["findings"] if item["rule_id"] == "R20"
    )

    assert finding["status"] == "fail"
    assert "0.01" in finding["evidence"]
