from app import standard_parse, watermark


def test_repeated_single_letter_watermark_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text, removed = watermark.clean_text("外A观B应C无D缺E陷")
    assert text == "外观应无缺陷"
    assert removed == 5


def test_repeated_standard_number_watermark_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    monkeypatch.delenv("P0_WATERMARK_TOKEN_MIN", raising=False)
    token = "Q/JLY J7111074B-2021"
    text, removed = watermark.clean_text(
        f"技术要求 {token} 驻车基本性能 {token} 第 12 页 {token} 试验方法 {token}"
    )
    assert token not in text
    assert "技术要求" in text and "驻车基本性能" in text
    assert removed == 4


def test_repeated_hyphen_standard_number_watermark_is_removed(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    token = "Q-JLY J7111074B"
    text = " ".join([f"条款 A {token} 判定", token, token, token])
    cleaned, removed = watermark.clean_text(text)
    assert token not in cleaned
    assert removed == 4


def test_single_standard_number_is_kept(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    text = "依据 Q/JLY J7111074B-2021 第 5.3.11 条判定。"
    cleaned, removed = watermark.clean_text(text)
    assert cleaned == text
    assert removed == 0


def test_token_threshold_is_configurable(monkeypatch):
    monkeypatch.delenv("P0_WATERMARK_CLEAN", raising=False)
    monkeypatch.setenv("P0_WATERMARK_TOKEN_MIN", "2")
    token = "Q/JLY J7111074B-2021"
    cleaned, removed = watermark.clean_text(f"{token} 外观 {token}")
    assert token not in cleaned
    assert removed == 2


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
