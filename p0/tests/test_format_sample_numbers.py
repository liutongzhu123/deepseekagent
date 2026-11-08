"""R24 样品编号连续性与重复性（RA-5.4 / RA-1.8）。

F1-F12 见 ``p0/app/format_rules.py``；12 份 BYD 草稿各仅 1 个编号，真实样例只证明
“单编号 -> skip”，断号/重复/范围/数量由合成数据覆盖。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.docx_parser import Cell, ReportData, parse_docx
from app.rules import RULES, audit_report

SAMPLE = Path("/opt/dsh-agent/samples/byd/ots-250717(1)__ots-250717__袋子-100L.docx")


def _table(rows):
    return [[Cell(text=value) for value in row] for row in rows]


CROSS_TABLE = [_table([["序号", "样品编号"], ["1", "TS24LXJ8A-01"]])]
ITEM_TABLE = [_table([["序号", "检验项目", "样品编号"], ["1", "气味", "TS24LXJ8A-01"],
                      ["2", "外观", "TS24LXJ8A-01"], ["3", "拉伸", "TS24LXJ8A-02"]])]
CELL_DUP_TABLE = [_table([["序号", "检验项目", "样品编号"], ["1", "气味", "TS24LXJ8A-01、TS24LXJ8A-01"]])]
ROW_DUP_TABLE = [_table([["序号", "样品编号"], ["1", "TS24LXJ8A-01"],
                         ["2", "TS24LXJ8A-02"], ["3", "TS24LXJ8A-02"]])]
LONG_TABLE = [_table([["序号", "样品编号"], ["1", "20241125"], ["2", "20241127"]])]
# 验证#154 反例 2：编号前缀分隔符写全角「－」时，与字段里的 ASCII 写法应归一为同一编号。
FULLWIDTH_PREFIX_TABLE = [_table([["序号", "样品编号"],
                                  ["1", "TS24LXJ8A－01"],
                                  ["2", "TS24LXJ8A－02"]])]


def _data(sample_no="", qty="", tables=None):
    return ReportData(file_name="sample.docx", sample_no=sample_no, sample_qty=qty, tables=tables or [])


def _r24(data):
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R24")


def test_rule_is_in_catalog():
    assert ("R24", "样品编号连续性与重复性", "样品编号按前缀分组检测断号/重复；样品数量可解析时核对编号个数") in RULES


@pytest.mark.parametrize(
    ("sample_no", "qty", "needle"),
    [
        ("TS24LXJ8A-01、TS24LXJ8A-02、TS24LXJ8A-04", "", "编号断档：TS24LXJ8A- 缺 TS24LXJ8A-03"),
        ("TS24LXJ8A-01、TS24LXJ8A-01、TS24LXJ8A-02", "", "编号重复：TS24LXJ8A-01（样品编号字段，2 次）"),
        ("TS24LXJ8A-01、TS24LXJ8A-02", "3件", "样品数量 3 与样品编号 2 个不对应"),
        # 验证#154 反例 3：同一编号的全角/半角前缀分隔符写法必须判重。
        ("TS24LXJ8A-01、TS24LXJ8A－01", "1", "编号重复：TS24LXJ8A-01（样品编号字段，2 次）"),
    ],
)
def test_fail_cases(sample_no, qty, needle):
    finding = _r24(_data(sample_no, qty))

    assert finding["status"] == "fail"
    assert finding["severity"] == "medium"
    assert needle in finding["message"]
    assert finding["location"]["section"] == "样品信息"


@pytest.mark.parametrize(
    ("sample_no", "qty", "tables", "needle"),
    [
        ("TS24LXJ8A-01、TS24LXJ8A-02、TS24LXJ8A-03", "3件", None, "样品数量=3"),
        ("TS24LXJ8A-01、TS24LXJ8A-02、QA17LX1LDT-01、QA17LX1LDT-02", "", None, "2 组前缀"),
        ("TS24LXJ8A-01～03", "3", None, "样品编号=3"),
        ("TS24LXJ8A-01、TS24LXJ8A-02", "2", CROSS_TABLE, "未发现断号或重复"),
        # 验证#154 反例 1：全角「－」是前缀分隔符，后面的「～」才是范围符。
        ("TS24LXJ8A－01～03", "3", None, "样品编号=3"),
        # 验证#154 反例 2：字段 ASCII + 表格全角前缀，应归一为同样 2 个编号而非 4 个。
        ("TS24LXJ8A-01、TS24LXJ8A-02", "2", FULLWIDTH_PREFIX_TABLE, "未发现断号或重复"),
        ("", "2", ITEM_TABLE, "未发现断号或重复"),
        ("TS24LXJ8A-01、TS24LXJ8A-02", "XX", None, "未发现断号或重复"),
    ],
)
def test_pass_cases(sample_no, qty, tables, needle):
    finding = _r24(_data(sample_no, qty, tables))

    assert finding["status"] == "pass"
    assert needle in finding["message"] + finding["evidence"]
    if qty == "XX":
        assert "样品数量" not in finding["evidence"]


@pytest.mark.parametrize(
    "sample_no",
    [
        "TS24LXJ8A-01－03",  # 全角「－」范围符（验证#154 第 1 轮反例）
        "TS24LXJ8A－01－03",  # 全角前缀分隔符 + 全角范围符
        "01－03",  # 无前缀范围
        "TS24LXJ8A-01－TS24LXJ8A-03",
        "TS24LXJ8A-01—03",
        "TS24LXJ8A-01–03",
        "TS24LXJ8A-01～03",
        "TS24LXJ8A-01至03",
    ],
)
def test_range_dash_variants_expand(sample_no):  # F11 / F12
    finding = _r24(_data(sample_no, "3"))

    assert finding["status"] == "pass"
    assert "样品编号=3" in finding["evidence"]
    assert "样品数量 3 与样品编号" not in finding["message"]


def test_fullwidth_prefix_dedupes_across_field_and_table():
    finding = _r24(_data("TS24LXJ8A-01、TS24LXJ8A-02", "2", FULLWIDTH_PREFIX_TABLE))

    assert finding["status"] == "pass"
    assert "样品编号=2" in finding["evidence"]
    assert "编号出现=4" in finding["evidence"]


def test_fullwidth_prefix_participates_in_gap_and_dup():
    gap = _r24(_data("TS24LXJ8A－01、TS24LXJ8A－02、TS24LXJ8A－04"))
    assert gap["status"] == "fail"
    assert "编号断档：TS24LXJ8A- 缺 TS24LXJ8A-03" in gap["message"]

    mixed_dup = [_table([["序号", "样品编号"],
                         ["1", "TS24LXJ8A-01"],
                         ["2", "TS24LXJ8A－01"]])]
    dup = _r24(_data("", "", mixed_dup))
    assert dup["status"] == "fail"
    assert "编号重复：TS24LXJ8A-01（表1样品编号列，2 次）" in dup["message"]


@pytest.mark.parametrize(
    ("sample_no", "tables", "needle"),
    [
        ("TS24LXJ8A-01", None, "仅识别到 1 个样品编号"),
        # F12：前缀本身以数字结尾时，单个「－」按前缀分隔符处理，不展开成 24..27。
        ("TS24－27", None, "仅识别到 1 个样品编号"),
        ("TS24－01", None, "仅识别到 1 个样品编号"),
        ("", None, "未识别到可判定的样品编号"),
        ("", LONG_TABLE, "未识别到可判定的样品编号"),
    ],
)
def test_skip_cases(sample_no, tables, needle):
    finding = _r24(_data(sample_no, tables=tables))

    assert finding["status"] == "skip"
    assert needle in finding["message"]


@pytest.mark.parametrize(
    ("tables", "needle"),
    [
        (CELL_DUP_TABLE, "编号重复：TS24LXJ8A-01（表1样品编号列，2 次）"),
        (ROW_DUP_TABLE, "表1样品编号列"),
    ],
)
def test_duplicate_in_table(tables, needle):
    finding = _r24(_data(tables=tables))

    assert finding["status"] == "fail"
    assert needle in finding["message"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="customer sample not mounted")
def test_real_draft_single_number_skips():
    finding = _r24(parse_docx(SAMPLE))

    assert finding["status"] == "skip"
    assert "仅识别到 1 个样品编号" in finding["message"]
