"""B4: formula recognition (formulas[] + formula_status)."""
import os
from pathlib import Path

import pytest

from app import standard_parse
from app.standard_extract.formula import extract_text_formulas
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


def test_mean_with_variable_definitions():
    item = {
        "clause": "5.1",
        "method_clause": "6.1",
        "requirement": "",
        "method": "取三次试验结果的算术平均值作为试验结果。"
                  "式中：x——每次试验结果；n——试验次数。",
    }
    entries = extract_text_formulas(item["method"], "6.1", "5.1")
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["kind"], entry["status"], entry["clause"]) == (
        "mean", "extracted", "6.1"
    )
    assert [variable["symbol"] for variable in entry["variables"]] == ["x", "n"]
    assert entry["variables"][0]["definition"] == "每次试验结果"


def test_weighted_mean_range_stddev_and_percentage():
    item = {
        "clause": "5.2",
        "method_clause": "6.2",
        "requirement": (
            "试验结果按加权平均值计算；极差为最大值与最小值之差；"
            "标准差按式（1）计算；变化率按式（2）计算，变化率=（A-B）/B×100%。"
            "式中：A——实测值；B——基准值。"
        ),
        "method": "",
    }
    kinds = {
        entry["kind"] for entry in extract_text_formulas(item["requirement"], "5.2", "5.2")
        if entry["status"] == "extracted"
    }
    assert kinds == {"weighted_mean", "range", "std_dev", "percentage"}


def test_formula_reference_without_type_is_kept_not_guessed():
    item = {
        "clause": "5.3",
        "method_clause": "6.3",
        "requirement": "试验结果按式（3）计算。",
        "method": "",
    }
    entries = extract_text_formulas(item["requirement"], "5.3", "5.3")
    assert len(entries) == 1
    assert entries[0]["status"] == "not_extracted"
    assert entries[0]["kind"] is None
    assert entries[0]["raw"] == "试验结果按式（3）计算"


def test_prose_average_is_not_a_formula():
    item = {
        "clause": "5.4",
        "method_clause": "6.4",
        "requirement": (
            "在40km/h的平均车速下，试验持续24h；"
            "平均值检波器用于测量；温度变化率要求见5.3.6。"
        ),
        "method": "",
    }
    assert extract_text_formulas(item["requirement"], "5.4", "5.4") == []


def test_percentage_sampling_rule_is_not_a_formula():
    text = "对变化率进行取值时，取电流和电压峰值的20%至80%进行计算"
    assert extract_text_formulas(text, "17.2", "") == []


def _pages() -> list[str]:
    cover = "Q/TEST 123-2024\n测试标准\n2024-01-01 发布\n2024-02-01 实施"
    requirements = "\n".join([
        "5  技术要求 Technical requirements",
        "5.1  外观检查 Appearance inspection", "外观应无缺陷。",
        "5.2  密封性能 Tightness",
        "取三次试验结果的算术平均值作为试验结果。",
    ])
    methods = "\n".join([
        "6  试验方法 Test methods",
        "6.1  外观检查试验 Appearance test", "目测检查。",
        "6.2  密封性试验 Tightness test", "按上述要求试验。",
    ])
    return [cover, requirements, methods, "5.1 6.1\n5.2 6.2"]


def test_parse_attaches_formulas_and_counts():
    result = parse_standard(_pages(), "测试标准.pdf")
    by_clause = {item["clause"]: item for item in result["items"]}
    assert by_clause["5.2"]["formula_status"] == "extracted"
    assert [entry["kind"] for entry in by_clause["5.2"]["formulas"]] == ["mean"]
    assert by_clause["5.1"]["formula_status"] == "not_extracted"
    assert result["summary"]["formulas"] == 1
    assert result["summary"]["formulas_extracted"] == 1
    assert result["summary"]["formulas_not_extracted"] == 0
    assert result["summary"]["formula_means"] == 1
    assert result["summary"]["items_with_formulas"] == 1


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_standard_has_no_invented_formulas():
    result = standard_parse.parse_pdf_standard(
        _SAMPLE.read_bytes(), _SAMPLE.name, enable_ocr=False
    )
    assert result["summary"]["formulas_extracted"] == 0
    assert result["summary"]["formulas"] == 0
    assert all(
        item["formula_status"] == "not_extracted" for item in result["items"]
    )
