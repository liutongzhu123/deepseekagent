import os
from pathlib import Path

import pytest

from app import standard_parse, watermark


def test_repeated_single_letter_watermark_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text, removed = watermark.clean_text("外A观B应C无D缺E陷")
    assert text == "外观应无缺陷"
    assert removed == 5


def test_repeated_cited_standard_numbers_are_preserved(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    cited = "Q/JLY J7110440E-2021"
    text = "\n".join(
        [f"满足 {cited} 中真空密封性规定的技术要求"] * 4
    )
    cleaned, removed = watermark.clean_text(text)
    assert cleaned == text
    assert removed == 0


def test_clean_pages_keeps_cited_standard_numbers(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    cited = "Q/JLY J7110440E-2021"
    page = "\n".join([f"满足 {cited} 中真空密封性规定的技术要求"] * 4)
    pages, removed = watermark.clean_pages([page])
    assert pages == [page]
    assert removed == 0


def test_recipient_block_removal_does_not_touch_cited_standard_numbers(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    cited = "Q/JLY J7110440E-2021"
    text = (
        "\n".join([_recipient_block()] * 4)
        + "\n"
        + "\n".join([f"满足 {cited} 中真空密封性规定的技术要求"] * 4)
    )
    cleaned, removed = watermark.clean_text(text)
    assert "TO:" not in cleaned
    assert cleaned.count(cited) == 4
    assert removed == 4


def test_one_or_two_letters_are_treated_as_real_text(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text, removed = watermark.clean_text("分为A类，另有B级")
    assert text == "分为A类，另有B级"
    assert removed == 0


def test_configured_terms_are_removed(monkeypatch):
    monkeypatch.setenv("P0_WATERMARK_TERMS", "YUNAN,云安水印")
    text, removed = watermark.clean_text("YUNAN 外观 云安水印 合格")
    assert "YUNAN" not in text and "云安水印" not in text
    assert removed == 2


def test_disabled_keeps_the_page_untouched(monkeypatch):
    monkeypatch.setenv("P0_WATERMARK_CLEAN", "0")
    text, removed = watermark.clean_text("外A观B应C无D缺E陷")
    assert (text, removed) == ("外A观B应C无D缺E陷", 0)


def test_clean_pages_cleans_each_page_and_counts(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    pages, removed = watermark.clean_pages(["外A观B应C无D缺E陷", "外观应无缺陷"])
    assert pages == ["外观应无缺陷", "外观应无缺陷"]
    assert removed == 5


def test_standard_parse_reports_watermark_cleanup(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    monkeypatch.setattr(standard_parse, "extract_pages", lambda data: ["外A观B应C无D缺E陷"])
    monkeypatch.setattr(standard_parse, "parse_standard", lambda *args, **kwargs: {"warnings": []})
    monkeypatch.setattr(standard_parse.admin_config, "standard_config", lambda: {})
    result = standard_parse.parse_pdf_standard(b"%PDF-1.4", "x.pdf", enable_ocr=False)
    assert "已清洗水印碎片 5 处" in result["warnings"]


def _recipient_block(to: str = "a@b", date: str = "2026/01/01", colon: str = ":") -> str:
    return f"TO{colon}{to}\nOF{colon}单位\nBY{colon}研究院\nAT{colon}{date}"


def test_repeated_recipient_block_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    monkeypatch.delenv("P0_WATERMARK_BLOCK_MIN", raising=False)
    block = _recipient_block()
    text = "\n".join([block] * 4) + "\n技术要求正文"
    cleaned, removed = watermark.clean_text(text)
    assert "TO:" not in cleaned and "AT:" not in cleaned
    assert "技术要求正文" in cleaned
    assert removed == 4


def test_single_recipient_block_is_kept(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text = _recipient_block() + "\n技术要求正文"
    cleaned, removed = watermark.clean_text(text)
    assert cleaned == text
    assert removed == 0


def test_different_recipient_blocks_are_not_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text = "\n".join(_recipient_block(f"user{i}@b") for i in range(4)) + "\n技术要求"
    cleaned, removed = watermark.clean_text(text)
    assert cleaned == text
    assert removed == 0


def test_recipient_block_threshold_is_configurable(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    monkeypatch.setenv("P0_WATERMARK_BLOCK_MIN", "2")
    text = "\n".join([_recipient_block()] * 2) + "\n正文"
    cleaned, removed = watermark.clean_text(text)
    assert "TO:" not in cleaned and "正文" in cleaned
    assert removed == 2


def test_recipient_block_handles_crlf_and_fullwidth_colon(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    block = _recipient_block(colon="：").replace("\n", "\r\n")
    text = "\r\n".join([block] * 4) + "\r\n技术要求"
    cleaned, removed = watermark.clean_text(text)
    assert "TO：" not in cleaned and "技术要求" in cleaned
    assert removed == 4


def test_mask_light_pink_removes_pink_but_keeps_dark_text(monkeypatch):
    from PIL import Image

    monkeypatch.delenv("P0_OCR_WATERMARK_MIN_RATIO", raising=False)
    image = Image.new("RGB", (40, 40), "white")
    for x in range(10, 20):
        for y in range(10, 20):
            image.putpixel((x, y), (252, 223, 226))
    for x in range(5, 35):
        for y in range(25, 30):
            image.putpixel((x, y), (0, 0, 0))
    masked, stats = watermark.mask_light_pink(image)
    assert stats["applied"] is True
    assert stats["masked_pixels"] == 100
    assert masked.getpixel((15, 15)) == (255, 255, 255)
    assert masked.getpixel((20, 27)) == (0, 0, 0)


def test_mask_light_pink_skips_clean_page(monkeypatch):
    from PIL import Image

    monkeypatch.delenv("P0_OCR_WATERMARK_MIN_RATIO", raising=False)
    masked, stats = watermark.mask_light_pink(Image.new("RGB", (40, 40), "white"))
    assert masked is None
    assert stats["applied"] is False


_SAMPLE = Path(
    os.getenv("P0_WATERMARK_SAMPLE", "/opt/dsh-agent/samples/watermark-sample.pdf")
)


@pytest.mark.skipif(
    not _SAMPLE.is_file(),
    reason="带水印样例未挂载（设置 P0_WATERMARK_SAMPLE 指定路径）",
)
def test_watermark_sample_keeps_cited_standard_numbers(monkeypatch):
    """Same 33-page watermarked standard: cleaning on/off must yield 44 identical clauses."""
    data = _SAMPLE.read_bytes()
    monkeypatch.setenv("P0_WATERMARK_CLEAN", "0")
    before = standard_parse.parse_pdf_standard(data, _SAMPLE.name, enable_ocr=False)
    monkeypatch.setenv("P0_WATERMARK_CLEAN", "1")
    after = standard_parse.parse_pdf_standard(data, _SAMPLE.name, enable_ocr=False)

    before_req = {item["clause"]: item["requirement"] for item in before["items"]}
    after_req = {item["clause"]: item["requirement"] for item in after["items"]}
    assert len(after_req) == 44
    assert after_req == before_req
    assert "Q/JLY J7110440E-2021" in after_req["5.3.1.1"]
    assert [clause for clause, text in after_req.items() if "满足中" in text] == []
