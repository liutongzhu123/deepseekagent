"""B3: sample quantity, size and pretreatment (samples[] + samples_status)."""
import os
from pathlib import Path

import pytest

from app import standard_parse
from app.standard_extract.samples import extract_samples
from app.standard_parser import parse_standard

_SAMPLE = Path(
    os.getenv("P0_STANDARD_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


def _kinds(entries):
    return {(entry["kind"], entry["value"], entry["value_max"], entry["unit"]) for entry in entries}


def test_extracts_quantity_size_and_pretreatment():
    lines = [
        ("5.2  密封性能 Tightness", 1),
        ("样品数量：3 件；试样尺寸 100mm×50mm×2mm；"
         "试验前在 23℃±2℃下放置 24h。", 1),
    ]
    entries = extract_samples(lines, [{"clause": "5.2", "method_clause": "6.2"}])
    assert ("quantity", 3, None, "件") in _kinds(entries)
    size = next(entry for entry in entries if entry["kind"] == "size")
    assert size["text"] == "100mm×50mm×2mm"
    pretreatment = next(entry for entry in entries if entry["kind"] == "pretreatment")
    assert "试验前" in pretreatment["text"]
    assert ("temperature", 23, "℃") in {
        (item["kind"], item["value"], item["unit"])
        for item in pretreatment["conditions"]
    }
    assert all(entry["item_clause"] == "5.2" for entry in entries)


def test_quantity_range_and_group_count():
    lines = [
        ("7.2.2  型式检验 Type inspection", 1),
        ("样品应从合格产品中随机抽取，均分成 12 组，每组数量（2～3）套。", 1),
    ]
    entries = extract_samples(lines, [])
    assert ("quantity", 2, 3, "套") in _kinds(entries)
    assert ("quantity", 12, None, "组") in _kinds(entries)
    assert all(entry["item_clause"] == "" for entry in entries)


def test_no_explicit_statement_is_not_guessed():
    lines = [("7.2.3  样品应从合格产品中随机抽取。", 1)]
    assert extract_samples(lines, []) == []


def _pages() -> list[str]:
    cover = "Q/TEST 123-2024\n测试标准\n2024-01-01 发布\n2024-02-01 实施"
    requirements = "\n".join([
        "5  技术要求 Technical requirements",
        "5.1  外观检查 Appearance inspection", "外观应无缺陷。",
        "5.2  密封性能 Tightness",
        "样品数量：3 件；试样尺寸 100mm×50mm×2mm；试验前在 23℃±2℃下放置 24h。",
    ])
    methods = "\n".join([
        "6  试验方法 Test methods",
        "6.1  外观检查试验 Appearance test", "目测检查。",
        "6.2  密封性试验 Tightness test", "按上述要求试验。",
    ])
    return [cover, requirements, methods, "5.1 6.1\n5.2 6.2"]


def test_parse_attaches_samples_and_not_extracted_status():
    result = parse_standard(_pages(), "测试标准.pdf")
    by_clause = {item["clause"]: item for item in result["items"]}
    assert ("quantity", 3, None, "件") in _kinds(result["samples"])
    assert by_clause["5.2"]["samples_status"] == "extracted"
    assert by_clause["5.1"]["samples"] == []
    assert by_clause["5.1"]["samples_status"] == "not_extracted"
    assert result["summary"]["samples"] == 3
    assert result["summary"]["items_with_samples"] == 1
    assert result["summary"]["samples_not_extracted"] == 1


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="真实标准样例未挂载")
def test_real_standard_samples_are_traceable():
    result = standard_parse.parse_pdf_standard(
        _SAMPLE.read_bytes(), _SAMPLE.name, enable_ocr=False
    )
    entries = result["samples"]
    assert result["summary"]["sample_quantities"] >= 1
    assert result["summary"]["sample_pretreatments"] >= 1
    clauses = {item["clause"] for item in result["items"]}
    for entry in entries:
        assert entry["kind"] in {"quantity", "size", "pretreatment"}
        assert entry["clause"]
        assert entry["item_clause"] in clauses or entry["item_clause"] == ""
    for item in result["items"]:
        assert item["samples_status"] in {"extracted", "not_extracted"}
