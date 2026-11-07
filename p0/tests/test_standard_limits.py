"""B2: structured technical limits (limits[] + limits_status)."""
import os
from pathlib import Path

import pytest

from app import standard_parse
from app.standard_extract.limits import extract_limits
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


def test_extracts_comparisons_ranges_and_tolerances():
    found = extract_limits(
        "漏气量≤3mL/min；硬度HBW（160～210）；夹紧力500N±50N；"
        "泄漏电流≤3mA；绝缘电阻≥50MΩ；噪音不超过70 dB(A)。"
    )
    entries = {
        (entry["kind"], entry["lower"], entry["upper"], entry["unit"])
        for entry in found
    }
    assert ("upper", None, 3, "mL/min") in entries
    assert ("range", 160, 210, "HBW") in entries
    assert ("tolerance", 450, 550, "N") in entries
    assert ("upper", None, 3, "mA") in entries
    assert ("lower", 50, None, "MΩ") in entries
    assert ("upper", None, 70, "dB(A)") in entries


def test_reference_only_clause_has_no_limit():
    text = "满足 Q/JLY J7110440E-2021中真空密封性规定的技术要求"
    assert extract_limits(text) == []


def _pages() -> list[str]:
    cover = "Q/TEST 123-2024\n测试标准\n2024-01-01 发布\n2024-02-01 实施"
    requirements = "\n".join([
        "5  技术要求 Technical requirements",
        "5.1  外观检查 Appearance inspection", "外观应无缺陷。",
        "5.2  密封性能 Tightness",
        "漏气量≤3mL/min；夹紧力500N±50N；硬度HBW（160～210）。",
    ])
    methods = "\n".join([
        "6  试验方法 Test methods",
        "6.1  外观检查试验 Appearance test", "目测检查。",
        "6.2  密封性试验 Tightness test", "按上述要求试验。",
    ])
    return [cover, requirements, methods, "5.1 6.1\n5.2 6.2"]


def test_parse_attaches_limits_and_not_extracted_status():
    result = parse_standard(_pages(), "测试标准.pdf")
    by_clause = {item["clause"]: item for item in result["items"]}
    sealed = by_clause["5.2"]
    entries = {
        (entry["kind"], entry["lower"], entry["upper"], entry["unit"])
        for entry in sealed["limits"]
    }
    assert ("upper", None, 3, "mL/min") in entries
    assert ("tolerance", 450, 550, "N") in entries
    assert ("range", 160, 210, "HBW") in entries
    assert sealed["limits_status"] == "extracted"
    assert by_clause["5.1"]["limits"] == []
    assert by_clause["5.1"]["limits_status"] == "not_extracted"
    assert result["summary"]["limits"] == 3
    assert result["summary"]["items_with_limits"] == 1
    assert result["summary"]["limits_not_extracted"] == 1
    assert all(entry["clause"] == "5.2" for entry in sealed["limits"])


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_watermarked_standard_limits():
    result = standard_parse.parse_pdf_standard(
        _SAMPLE.read_bytes(), _SAMPLE.name, enable_ocr=False
    )
    by_clause = {item["clause"]: item for item in result["items"]}

    drag = by_clause["5.3.3.2"]["limits"]
    assert any(
        entry["kind"] == "upper" and entry["upper"] == 3 and entry["unit"] == "N·m"
        for entry in drag
    )
    assert any(
        entry["kind"] == "upper" and entry["upper"] == 4 and entry["unit"] == "N·m"
        for entry in drag
    )

    water = by_clause["5.3.15.1"]["limits"]
    assert any(
        entry["kind"] == "upper" and entry["upper"] == 3 and entry["unit"] == "mA"
        for entry in water
    )
    assert any(
        entry["kind"] == "lower" and entry["lower"] == 50 and entry["unit"] == "MΩ"
        for entry in water
    )

    noise = by_clause["5.3.21"]["limits"]
    assert any(
        entry["kind"] == "upper" and entry["upper"] == 70
        and entry["unit"] == "dB(A)" for entry in noise
    )
    assert by_clause["5.3.1.1"]["limits_status"] == "not_extracted"
