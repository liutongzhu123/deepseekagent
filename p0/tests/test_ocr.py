"""Scanned-standard parsing tests (OCR-shaped text, no OCR engine needed)."""
from __future__ import annotations

from app import ocr, standard_parse
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


def _line(text: str) -> ocr.OcrLine:
    return ocr.OcrLine(text=text, confidence=0.9, top=0, left=0)


def test_missing_content_lines_detects_dropped_text():
    before = [
        _line("TO:user@example.com"),
        _line("Degrees of protection (IP-Code) - Protection of electrical equipment"),
        _line("@发布"),
        _line("技术要求正文"),
    ]
    after = [_line("EPB 制动钳总成技术条件"), _line("技术要求正文")]
    missing = ocr._missing_content_lines(before, after)
    assert missing == ["Degrees of protection (IP-Code) - Protection of electrical equipment"]


def test_watermark_mask_meta_lists_fallback_pages():
    result = ocr.OcrResult(
        pages=[
            ocr.OcrPage(page=1, lines=[], confidence=0.9, watermark_mask={"applied": True}),
            ocr.OcrPage(page=2, lines=[], confidence=0.9, watermark_mask={"fallback": True}),
            ocr.OcrPage(page=3, lines=[], confidence=0.9),
        ],
        engine="rapidocr_onnxruntime", dpi=220, elapsed_ms=1,
    )
    meta = result.meta()
    assert meta["watermark_mask_pages"] == [1]
    assert meta["watermark_mask_fallback_pages"] == [2]


def test_standard_parse_warns_on_mask_fallback(monkeypatch):
    monkeypatch.setenv("P0_OCR_ENABLED", "1")
    monkeypatch.setattr(standard_parse, "extract_pages", lambda data: ["短页"])
    def fake_ocr(data, **kwargs):
        return ocr.OcrResult(
            pages=[ocr.OcrPage(page=5, lines=[_line("正文")], confidence=0.9,
                               watermark_mask={"applied": False, "fallback": True})],
            engine="rapidocr_onnxruntime", dpi=220, elapsed_ms=1,
        )
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_pdf", fake_ocr)
    monkeypatch.setattr(standard_parse, "parse_standard", lambda *args, **kwargs: {"warnings": []})
    monkeypatch.setattr(standard_parse.admin_config, "standard_config", lambda: {})
    result = standard_parse.parse_pdf_standard(b"%PDF-1.4", "x.pdf", enable_ocr=True)
    assert any("第 5 页" in warning and "回退原图" in warning for warning in result["warnings"])


def test_watermark_line_filter_handles_ocr_variants():
    assert ocr._is_watermark_line("TO:user@example.com")
    assert ocr._is_watermark_line("T0:user@example.com")
    assert ocr._is_watermark_line("1o:user@example.com")
    assert ocr._is_watermark_line("A T:2022/11/10")
    assert not ocr._is_watermark_line("技术要求正文")


def test_missing_content_lines_tolerates_ocr_variants():
    before = [
        _line("Q/JLJ1ooo03汽车零部件永久性标识规定"),
        _line("vehicle parts 研究院"),
        _line("Fig. 3.The test evaluation indexes shall be filled in according to Table 1"),
    ]
    after = [
        _line("Q/JLJ100003汽车零部件永久性标识规定"),
        _line("vehicle parts"),
        _line("Fig. 3.The test evaluation indexes shall be filled in according to Table 1"),
    ]
    assert ocr._missing_content_lines(before, after) == []


def test_watermark_mask_meta_lists_review_pages():
    result = ocr.OcrResult(
        pages=[
            ocr.OcrPage(page=1, lines=[], confidence=0.9, watermark_mask={
                "applied": True, "before_content_lines": 10, "after_content_lines": 9,
                "before_chars": 100, "after_chars": 90,
            }),
            ocr.OcrPage(page=2, lines=[], confidence=0.9, watermark_mask={
                "applied": True, "before_content_lines": 5, "after_content_lines": 6,
                "before_chars": 50, "after_chars": 60,
            }),
        ],
        engine="rapidocr_onnxruntime", dpi=220, elapsed_ms=1,
    )
    assert result.meta()["watermark_mask_review_pages"] == [1]


def test_standard_parse_warns_on_mask_review(monkeypatch):
    monkeypatch.setenv("P0_OCR_ENABLED", "1")
    monkeypatch.setattr(standard_parse, "extract_pages", lambda data: ["短页"])
    def fake_ocr(data, **kwargs):
        return ocr.OcrResult(
            pages=[ocr.OcrPage(page=7, lines=[_line("正文")], confidence=0.9, watermark_mask={
                "applied": True, "before_content_lines": 10, "after_content_lines": 9,
            })],
            engine="rapidocr_onnxruntime", dpi=220, elapsed_ms=1,
        )
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_pdf", fake_ocr)
    monkeypatch.setattr(standard_parse, "parse_standard", lambda *args, **kwargs: {"warnings": []})
    monkeypatch.setattr(standard_parse.admin_config, "standard_config", lambda: {})
    result = standard_parse.parse_pdf_standard(b"%PDF-1.4", "x.pdf", enable_ocr=True)
    assert any("第 7 页" in warning and "行数/字符数减少" in warning for warning in result["warnings"])


def test_masked_page_falls_back_when_content_line_disappears():
    import numpy as np
    from PIL import Image

    image = Image.new("RGB", (40, 40), "white")
    for x in range(4):
        for y in range(4):
            image.putpixel((x, y), (252, 223, 226))

    def engine(arr):
        pink = arr[0, 0, 0] > 240 and arr[0, 0, 1] < 240
        if pink:
            raw = [
                ([[0, 0], [100, 0], [100, 10], [0, 10]],
                 "Degrees of protection (IP-Code) - Protection of electrical equipment", 0.9),
                ([[0, 20], [60, 20], [60, 30], [0, 30]], "TO:user@example.com", 0.9),
            ]
        else:
            raw = [([[0, 20], [60, 20], [60, 30], [0, 30]], "技术要求正文", 0.9)]
        return raw, 0

    raw, lines, meta = ocr._masked_page(image, engine, 0.35)
    assert meta["fallback"] is True
    assert meta["missing_lines"]
    assert len(lines) == 2  # the original result wins
