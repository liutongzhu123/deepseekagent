"""B1: structured test conditions (conditions[] + conditions_status)."""
import os
from pathlib import Path

import pytest

from app import standard_parse
from app.standard_extract.conditions import extract_conditions
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


def test_extracts_all_seven_condition_kinds():
    found = extract_conditions(
        "常温20℃±5℃，湿度65%±20%RH，保持24h，循环5次；"
        "电压9V～16V，电流0.5A，盐雾浓度5%。"
    )
    tuples = {(entry["kind"], entry["value"], entry["unit"]) for entry in found}
    for expected in (
        ("temperature", 20, "℃"), ("humidity", 65, "%"), ("duration", 24, "h"),
        ("cycles", 5, "次"), ("voltage", 9, "V"), ("current", 0.5, "A"),
        ("salt_fog", 5, "%"),
    ):
        assert expected in tuples
    by_kind = {entry["kind"]: entry for entry in found}
    assert by_kind["temperature"]["tolerance"] == 5
    assert by_kind["humidity"]["tolerance"] == 20
    assert by_kind["voltage"]["value_max"] == 16


def test_reference_only_clause_is_never_guessed():
    text = "满足 Q/JLY J7110440E-2021中真空密封性规定的技术要求"
    assert extract_conditions(text) == []


def _pages() -> list[str]:
    cover = "Q/TEST 123-2024\n测试标准\n2024-01-01 发布\n2024-02-01 实施"
    requirements = "\n".join([
        "5  技术要求 Technical requirements",
        "5.1  外观检查 Appearance inspection", "外观应无缺陷。",
        "5.2  密封性能 Tightness",
        "在（23±2）℃、相对湿度50%±5%下，12V电压、0.5A电流保持24h，循环5次；"
        "漏气量≤3mL/min。",
    ])
    methods = "\n".join([
        "6  试验方法 Test methods",
        "6.1  外观检查试验 Appearance test", "目测检查。",
        "6.2  密封性试验 Tightness test", "继续试验30min。",
    ])
    return [cover, requirements, methods, "5.1 6.1\n5.2 6.2"]


def test_parse_attaches_conditions_and_not_extracted_status():
    result = parse_standard(_pages(), "测试标准.pdf")
    by_clause = {item["clause"]: item for item in result["items"]}
    sealed = by_clause["5.2"]
    tuples = {
        (entry["kind"], entry["value"], entry["unit"])
        for entry in sealed["conditions"]
    }
    for expected in (
        ("temperature", 23, "℃"), ("humidity", 50, "%"), ("duration", 24, "h"),
        ("duration", 30, "min"), ("cycles", 5, "次"), ("voltage", 12, "V"),
        ("current", 0.5, "A"),
    ):
        assert expected in tuples
    assert sealed["conditions_status"] == "extracted"
    assert by_clause["5.1"]["conditions_status"] == "not_extracted"
    assert by_clause["5.1"]["conditions"] == []
    assert result["summary"]["conditions"] == 7
    assert result["summary"]["items_with_conditions"] == 1
    assert result["summary"]["conditions_not_extracted"] == 1
    hold = next(
        entry for entry in sealed["conditions"]
        if entry["kind"] == "duration" and entry["value"] == 24
    )
    method = next(
        entry for entry in sealed["conditions"]
        if entry["kind"] == "duration" and entry["value"] == 30
    )
    assert (hold["clause"], hold["source"]) == ("5.2", "requirement")
    assert (method["clause"], method["source"]) == ("6.2", "method")


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_standard_conditions_are_traceable():
    """Real standard: extracted conditions are traceable; references stay unparsed."""
    result = standard_parse.parse_pdf_standard(
        _SAMPLE.read_bytes(), _SAMPLE.name, enable_ocr=False
    )
    assert result["summary"]["conditions"] > 0
    kinds = {
        "temperature", "humidity", "duration", "cycles",
        "voltage", "current", "salt_fog",
    }
    for item in result["items"]:
        assert item["conditions_status"] in {"extracted", "not_extracted"}
        for entry in item["conditions"]:
            assert entry["kind"] in kinds
            assert entry["unit"]
            assert entry["clause"] in {item["clause"], item["method_clause"]}
    assert any(item["conditions_status"] == "not_extracted" for item in result["items"])
