"""Scanned-standard parsing tests (OCR-shaped text, no OCR engine needed)."""
from __future__ import annotations

from app.standard_parser import parse_standard


def scanned_pages() -> list[str]:
    cover = "\n".join(
        [
            "内部资料，注意保存",
            "Q/TEST 456-2024",
            "车用插接器",
            "2024-01-01 发布",
            "2024-02-01 实施",
        ]
    )
    toc = "\n".join(
        [
            "目录",
            "5 试验方法和性能要求",
            "5.1.1 外观检查",
            "5.2.1 绝缘电阻",
        ]
    )
    body = "\n".join(
        [
            "5 试验方法和性能要求",
            "5.1 常规试验",
            "5.1.1 外观检查",
            "5.1.1.1 试验条件",
            "正常光照下检查。",
            "5.1.1.2 试验要求",
            "样品不应有裂纹、划痕等缺陷。",
            "5.1.1.3 试验方法",
            "目测检查并记录。",
            "5.2 电气试验",
            "5.2.1 绝缘电阻",
            "5.2.1.1 试验目的",
            "验证绝缘性能。",
            "5.2.1.2 试验方法",
            "按 GB/T 1411 测量。",
            "5.2.1.3 试验要求",
            "绝缘电阻应大于 100MΩ。",
        ]
    )
    return [cover, toc, body]


def test_scanned_combined_section_parses_items_and_fields():
    result = parse_standard(
        scanned_pages(),
        "扫描件.pdf",
        ocr={
            "engine": "rapidocr_onnxruntime",
            "pages": 3,
            "avg_confidence": 0.94,
            "dpi": 220,
            "elapsed_ms": 100,
            "low_confidence_pages": [3],
            "line_count": 30,
        },
        page_confidence=[0.99, 0.97, 0.94],
    )
    assert result["mapping_source"] == "combined-sections"
    assert result["summary"]["items"] == 2
    items = {item["clause"]: item for item in result["items"]}
    assert items["5.1.1"]["name"] == "外观检查"
    assert "裂纹" in items["5.1.1"]["requirement"]
    assert items["5.1.1"]["method_clause"] == "5.1.1.3"
    assert "目测" in items["5.1.1"]["method"]
    assert items["5.2.1"]["method_clause"] == "5.2.1.2"
    assert items["5.2.1"]["page"] == 3
    assert "confidence_detail" in items["5.1.1"]
    assert result["meta"]["is_scanned"] is True
    assert result["meta"]["ocr"]["engine"] == "rapidocr_onnxruntime"
    assert result["summary"]["ocr_pages"] == 3


def test_low_confidence_page_and_numbers_are_flagged_for_review():
    result = parse_standard(
        scanned_pages(),
        "扫描件.pdf",
        ocr={
            "engine": "rapidocr_onnxruntime",
            "pages": 3,
            "avg_confidence": 0.86,
            "dpi": 220,
            "elapsed_ms": 100,
            "low_confidence_pages": [3],
            "line_count": 30,
        },
        page_confidence=[0.99, 0.97, 0.93],
    )
    item = next(item for item in result["items"] if item["clause"] == "5.2.1")
    assert item["review_required"] is True
    fields = {finding["field"] for finding in item["review_fields"]}
    assert "requirement" in fields
    # The requirement contains 100MΩ and the page OCR confidence is below
    # 0.95, so the numeric field must be surfaced for review.
    assert any("数值" in finding["reason"] for finding in item["review_fields"])


def test_missing_method_is_flagged_not_fabricated():
    pages = scanned_pages()
    pages[2] = pages[2].replace("5.2.1.2 试验方法", "5.2.1.2 说明")
    result = parse_standard(
        pages,
        "扫描件.pdf",
        ocr={
            "engine": "rapidocr_onnxruntime",
            "pages": 3,
            "avg_confidence": 0.94,
            "dpi": 220,
            "elapsed_ms": 100,
            "low_confidence_pages": [],
            "line_count": 30,
        },
        page_confidence=[0.99, 0.97, 0.94],
    )
    item = next(item for item in result["items"] if item["clause"] == "5.2.1")
    assert item["method_clause"] == ""
    assert any(finding["field"] == "method_clause" for finding in item["review_fields"])
