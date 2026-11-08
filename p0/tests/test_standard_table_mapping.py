"""T2: table columns mapped to limits[]/conditions[] (mapping_confidence)."""
import os
from pathlib import Path

import pytest

from app.standard_extract.table_mapping import (
    _header_unit,
    _role,
    _segments,
    apply_table_mapping,
)
from app.standard_extract.tables import _clean_cell, _notes_below
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_TABLE_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)
_GENERAL = Path(os.getenv(
    "P0_STANDARD_GENERAL_SAMPLE",
    "/opt/dsh-agent/samples/standards/Q-JLY J7111029E-2024-汽车电气和电子零部件通用技术规范.pdf",
))
_EMC = Path(os.getenv(
    "P0_STANDARD_EMC_SAMPLE",
    "/opt/dsh-agent/samples/standards/Q-JLY J7110779E-2023-乘用车电气-电子零部件电磁兼容规范.pdf",
))
_NOTE = "注：1）限值单位为dB(μV)， “/”标识表示不适用。"


def _item(clause: str = "5.2") -> dict:
    return {"clause": clause, "limits": [], "conditions": [],
            "limits_status": "not_extracted", "conditions_status": "not_extracted"}


def test_columns_are_classified_by_header_keywords():
    assert _role("项目") == ("identifier", "high")
    assert _role("序号") == ("identifier", "high")
    assert _role("限值(N·m)") == ("limit", "high")
    assert _role("技术要求") == ("limit", "high")
    assert _role("试验温度") == ("condition", "high")
    assert _role("循环次数") == ("condition", "high")
    assert _role("试验方法")[0] is None
    assert _header_unit("工作温度范围℃") == "℃"
    assert _header_unit("时间 min") == "min"
    assert _header_unit("项目/Item") == ""


def test_rows_reuse_b1_b2_entry_shapes():
    item = _item()
    table = {"clause": "5.2", "page": 7, "parse_warning": None,
             "header": ["项目", "限值", "试验温度", "循环次数"],
             "rows": [["密封性", "≤3N·m", "-40℃～85℃", "100次"]]}
    summary = apply_table_mapping([item], [table])
    limit, = item["limits"]
    assert (limit["kind"], limit["upper"], limit["unit"]) == ("upper", 3, "N·m")
    assert limit["source"] == "table" and limit["table_page"] == 7
    assert limit["table_row"] == 0 and limit["column"] == "限值"
    assert limit["mapping_confidence"] == "high"
    kinds = {(entry["kind"], entry["value"], entry["value_max"])
             for entry in item["conditions"]}
    assert ("temperature", -40, 85) in kinds
    assert ("cycles", 100, None) in kinds
    assert summary["table_limits"] == 1 and summary["table_conditions"] == 2
    assert summary["limits"] == 1 and summary["conditions"] == 2


def test_bare_values_take_their_semantics_from_the_column():
    item = _item()
    table = {"clause": "5.2", "page": 8, "parse_warning": None,
             "header": ["名称", "限值(N·m)"],
             "rows": [["夹紧", "3"], ["释放", "4.5"]]}
    apply_table_mapping([item], [table])
    assert {(e["kind"], e["value"], e["unit"]) for e in item["limits"]} == {
        ("value", 3, "N·m"), ("value", 4.5, "N·m")}
    assert all(e["mapping_confidence"] == "high" for e in item["limits"])


def test_several_values_in_one_cell_stay_distinguishable():
    item = _item()
    cell = "≤3N·m / 8MPa 时 ≤4N·m"
    table = {"clause": "5.2", "page": 9, "parse_warning": None,
             "header": ["项目", "限值"], "rows": [["夹紧", cell]]}
    apply_table_mapping([item], [table])
    assert {entry["raw"] for entry in item["limits"]} == {"≤3N·m", "≤4N·m"}
    assert all(entry["cell_raw"] == cell for entry in item["limits"])


def test_low_confidence_rows_are_marked_and_raw_rows_kept():
    item = _item()
    rows = [["x", "按软件策略", "室温"]]
    table = {"clause": "5.2", "page": 10, "parse_warning": None,
             "header": ["项目", "要求", "条件"], "rows": rows[:]}
    summary = apply_table_mapping([item], [table])
    assert item["limits"] == [] and item["conditions"] == []
    assert table["rows"] == rows
    assert table["mapping"]["low_confidence_rows"][0]["row"] == 0
    assert summary["tables_mapping_low"] == 1


def test_unknown_column_is_not_guessed():
    item = _item()
    table = {"clause": "5.2", "page": 11, "parse_warning": None,
             "header": ["名称", "参数"], "rows": [["夹紧", "≤3N·m"]]}
    apply_table_mapping([item], [table])
    assert item["limits"] == []
    assert table["mapping"]["confidence"] == "none"
    assert table["rows"] == [["夹紧", "≤3N·m"]]


def test_cell_segmentation_keeps_parenthesised_values_together():
    assert _segments("≤3N·m / 8MPa 时 ≤4N·m") == ["≤3N·m", "8MPa 时 ≤4N·m"]
    assert _segments("（+4%, -4%）") == ["（+4%, -4%）"]


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_standard_rows_are_traceable_and_preserved():
    from app import watermark
    from app.pdf_text import extract_pages

    data = _SAMPLE.read_bytes()
    pages, _ = watermark.clean_pages(extract_pages(data))
    plain = parse_standard(pages, _SAMPLE.name)
    parsed = parse_standard(pages, _SAMPLE.name, pdf_bytes=data)
    assert [item["requirement"] for item in parsed["items"]] == [
        item["requirement"] for item in plain["items"]]
    for entry in parsed["tables"]:
        assert "mapping" in entry
        for row in entry["rows"]:
            assert isinstance(row, list)
    for item in parsed["items"]:
        for entry in item["limits"] + item["conditions"]:
            if entry.get("source") != "table":
                continue
            assert entry["clause"] == item["clause"] == entry["item_clause"]
            assert entry["table_page"] and entry["table_row"] is not None
            assert entry["column"] and entry["cell_raw"]
            assert entry["mapping_confidence"] in {"high", "low"}
    assert parsed["summary"]["limits"] == sum(
        len(item["limits"]) for item in parsed["items"])
    assert parsed["summary"]["conditions"] == sum(
        len(item["conditions"]) for item in parsed["items"])


@pytest.mark.skipif(not _GENERAL.is_file(), reason="通用标准样例未挂载")
def test_real_standard_general_table_limits_are_mapped_and_counted():
    from app import standard_parse

    result = standard_parse.parse_pdf_standard(
        _GENERAL.read_bytes(), _GENERAL.name, enable_ocr=False)
    summary = result["summary"]
    assert summary["table_limits"] > 0
    assert summary["limits"] >= summary["table_limits"]
    assert summary["table_limits_low_confidence"] <= summary["table_limits"]
    table_limits = [
        entry for item in result["items"] for entry in item["limits"]
        if entry.get("source") == "table"]
    assert len(table_limits) == summary["table_limits"]
    assert any(entry["kind"] == "value" for entry in table_limits)


def test_header_unit_keeps_dB_microvolt_denominator():
    assert _clean_cell("电场峰值限值 .dB（μV/m）") == "电场峰值限值 .dB（μV/m）"
    assert _header_unit("电场峰值限值 .dB（μV/m）") == "dB(μV/m)"
    assert _header_unit("限值 dB(uV)") == "dB(μV)"


def test_footnote_unit_raises_missing_unit_limits_to_high():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "限值"], "rows": [["辐射", "30"]],
             "footnotes": [_NOTE]}
    summary = apply_table_mapping([item], [table])
    limit, = item["limits"]
    assert (limit["unit"], limit["unit_source"]) == ("dB(μV)", "footnote")
    assert limit["mapping_confidence"] == "high"
    assert summary["table_limits_low_confidence"] == 0
    assert summary["table_limits_footnote_unit"] == 1


def test_header_watermark_note_does_not_erase_a_resolved_unit():
    item = _item()
    table = {"clause": "5.2", "page": 23,
             "parse_warning": "表头含水印/页眉残留，需人工复核",
             "header": ["项目", "限值"], "rows": [["辐射", "30"]],
             "footnotes": [_NOTE]}
    apply_table_mapping([item], [table])
    assert item["limits"][0]["unit"] == "dB(μV)"
    assert item["limits"][0]["mapping_confidence"] == "high"


def test_structural_parse_warning_keeps_mapping_low():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": "未识别到表头",
             "header": ["项目", "限值"], "rows": [["辐射", "30"]],
             "footnotes": [_NOTE]}
    apply_table_mapping([item], [table])
    assert item["limits"][0]["mapping_confidence"] == "low"


def test_watermarked_footnote_row_supplies_the_unit():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "限值"],
             "rows": [["辐射", "30"], ["注：1）限值单位为dTB( O μV)", "", ""]],
             "footnotes": []}
    apply_table_mapping([item], [table])
    limit, = item["limits"]
    assert limit["unit"] == "dB(μV)" and limit["unit_source"] == "footnote"
    assert limit["mapping_confidence"] == "high"
    assert table["mapping"]["low_confidence_rows"] == []
    assert table["mapping"]["mapped_rows"] == 1


def test_single_letter_watermark_unit_is_replaced_by_the_footnote():
    item = _item()
    table = {"clause": "5.2", "page": 31, "parse_warning": None,
             "header": ["项目", "限值"], "rows": [["辐射", "21g"]],
             "footnotes": ["注：1)限值单位为dB(μV/m)， “/”标识表示不适用。"]}
    apply_table_mapping([item], [table])
    limit, = item["limits"]
    assert (limit["unit"], limit["unit_source"]) == ("dB(μV/m)", "footnote")
    assert limit["mapping_confidence"] == "high"


def test_several_footnote_units_map_to_their_own_columns():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "峰值限值", "平均值限值"],
             "rows": [["辐射", "30", "20"]],
             "footnotes": ["注：峰值限值单位dB(μV)，平均值限值单位dB(μA)。"]}
    apply_table_mapping([item], [table])
    assert {(entry["unit"], entry["mapping_confidence"])
            for entry in item["limits"]} == {("dB(μV)", "high"), ("dB(μA)", "high")}


def test_ambiguous_footnote_units_are_not_guessed():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "限值"], "rows": [["辐射", "30"]],
             "footnotes": ["注：峰值限值单位dB(μV)，平均值限值单位dB(μA)。"]}
    apply_table_mapping([item], [table])
    assert item["limits"][0]["unit"] == ""
    assert item["limits"][0]["mapping_confidence"] == "low"
    assert "未应用" in table["mapping"]["footnote_warning"]


def test_several_units_in_one_statement_are_not_guessed():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "限值"], "rows": [["辐射", "30"]],
             "footnotes": ["注：单位均为dB(μV)或dB(μA)。"]}
    apply_table_mapping([item], [table])
    assert item["limits"][0]["unit"] == ""
    assert item["limits"][0]["mapping_confidence"] == "low"
    assert "多个单位" in table["mapping"]["footnote_warning"]


def test_footnote_unit_contradicting_a_temperature_column_stays_low():
    item = _item()
    table = {"clause": "5.2", "page": 23, "parse_warning": None,
             "header": ["项目", "要求", "温度"],
             "rows": [["辐射", "30", "40"]],
             "footnotes": ["注：单位均为dB(μV)。"]}
    apply_table_mapping([item], [table])
    assert all(entry["mapping_confidence"] == "low"
               for entry in item["limits"] + item["conditions"])
    assert "矛盾" in table["mapping"]["warning"]


def test_note_far_below_the_table_is_not_used():
    class Page:
        height = 842

        def __init__(self, top: float):
            self.top = top

        def extract_text_lines(self):
            return [{"text": _NOTE, "top": self.top}]

    assert _notes_below(Page(400), (0, 0, 100, 100), None) == []
    near = _notes_below(Page(140), (0, 0, 100, 100), None)
    assert len(near) == 1 and near[0].startswith("注：1）限值单位")


@pytest.mark.skipif(not _EMC.is_file(), reason="EMC 标准样例未挂载")
def test_real_emc_footnotes_raise_low_confidence_table_limits():
    from app import standard_parse

    result = standard_parse.parse_pdf_standard(
        _EMC.read_bytes(), _EMC.name, enable_ocr=False)
    summary = result["summary"]
    assert summary["table_limits"] >= 80
    assert summary["table_limits_footnote_unit"] > 0
    assert 0 < summary["table_limits_low_confidence"] < summary["table_limits"]
    footnote_limits = [
        entry for item in result["items"] for entry in item["limits"]
        if entry.get("source") == "table" and entry.get("unit_source") == "footnote"]
    assert footnote_limits and all(entry["unit"] for entry in footnote_limits)
