from app import standard_parse, watermark


def test_repeated_single_letter_watermark_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text, removed = watermark.clean_text("外A观B应C无D缺E陷")
    assert text == "外观应无缺陷"
    assert removed == 5


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
