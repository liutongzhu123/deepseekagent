"""T1: PDF table structure extraction (tables[])."""
import os
from pathlib import Path

import pytest

from app.standard_extract.tables import (
    _clean_cell,
    _combine_header,
    _first_column_clause,
    _header_range,
    _line_clause,
    _trim,
    extract_tables,
)
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_TABLE_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


def test_cell_cleanup_keeps_units_and_removes_watermark_letters():
    assert _clean_cell("F\n名O称 g\nn") == "名称"
    assert _clean_cell("峰值\nPeak detector") == "峰值Peak detector"
    assert _clean_cell("（5.5～6.6）V") == "（5.5～6.6）V"
    assert _clean_cell("A") == "A"


def test_header_range_finds_two_row_merged_header():
    grid = [
        ["试验项目", "技术要求", "试验方法", "出厂检验", "型式检验", "", ""],
        ["", "", "", "", "Ⅰ", "Ⅱ", "Ⅲ"],
        ["外观检查", "5.1.2", "6.1", "√", "√", "", ""],
    ]
    start, count = _header_range(grid)
    assert (start, count) == (0, 2)
    assert _combine_header(grid[:2])[-3:] == ["型式检验 / Ⅰ", "型式检验 / Ⅱ", "型式检验 / Ⅲ"]


def test_header_range_keeps_units_in_a_second_header_row():
    grid = [
        ["轴", "", "轴", "", "轴", ""],
        ["频率Hz", "PSD (m/s2)2/Hz", "频率Hz", "PSD (m/s2)2/Hz", "频率Hz", "PSD (m/s2)2/Hz"],
        ["10", "10", "10", "5", "10", "10"],
    ]
    assert _header_range(grid) == (0, 2)


def test_header_range_ignores_a_data_table_without_header():
    grid = [
        ["防盗报警系统", "车辆运行状态时不工作。", "车辆停放状态监控。"],
        ["制动控制系统", "车辆运行状态的制动循环。", "车辆停放状态监控。"],
    ]
    assert _header_range(grid) == (0, 1)


def test_clause_comes_from_first_column_or_annex_heading():
    assert _first_column_clause([["1", "外观"], ["5.1.2", "外观"]]) == "5.1.2"
    assert _first_column_clause([["0.15 to 4.77", "限值"]]) == ""
    assert _line_clause("5.1.2 外观检查 Appearance") == "5.1.2"
    assert _line_clause("C.1.2 测试天线位置") == "C.1.2"
    assert _line_clause("0.15 to 4.77") == ""


def test_trim_drops_empty_border_rows_and_columns():
    assert _trim([["", "名称", "值", ""], ["", "外观", "√", ""], ["", "", "", ""]]) == [
        ["名称", "值"],
        ["外观", "√"],
    ]


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_standard_exports_tables_without_polluting_requirement():
    from app import watermark
    from app.pdf_text import extract_pages

    data = _SAMPLE.read_bytes()
    pages, _ = watermark.clean_pages(extract_pages(data))
    plain = parse_standard(pages, _SAMPLE.name)
    parsed = parse_standard(pages, _SAMPLE.name, pdf_bytes=data)
    assert parsed["tables"]
    assert all(entry["clause"] for entry in parsed["tables"])
    assert any(entry["header_detected"] for entry in parsed["tables"])
    assert any(entry["has_merges"] for entry in parsed["tables"])
    assert all(entry["page"] >= 1 and len(entry["raw_bbox"]) == 4 for entry in parsed["tables"])
    assert [item["requirement"] for item in parsed["items"]] == [
        item["requirement"] for item in plain["items"]
    ]
    assert all("tables" in item for item in parsed["items"])
