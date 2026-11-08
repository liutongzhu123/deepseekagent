"""SP_MULTIFMT: standard uploads accept PDF, Word and images; others fail clearly."""
from __future__ import annotations

import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient

from app import main, standard_parse, standard_store
from app.ocr import OcrLine, OcrPage, OcrUnavailable

client = TestClient(main.app)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
COVER = ["Q/TEST 123-2024", "测试标准", "2024-01-01 发布", "2024-02-01 实施"]
BODY = ["5  技术要求 Technical requirements", "5.1  外观检查 Appearance inspection", "外观应无缺陷。", "5.2  密封性能 Tightness", "密封性应良好。"]
METHOD = ["6  试验方法 Test methods", "6.1  外观检查试验 Appearance test", "目测检查。", "6.2  密封性试验 Tightness test", "按方法进行。"]
TABLE = ["5.1 6.1", "5.2 6.2"]


@pytest.fixture(autouse=True)
def _isolated_standards(monkeypatch, tmp_path):
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")


def _docx(lines: list[str], breaks: set[int] | None = None) -> bytes:
    body = []
    for index, line in enumerate(lines):
        body.append(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>')
        if index in (breaks or set()):
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    document = f'<w:document xmlns:w="{W}"><w:body>{"".join(body)}<w:sectPr/></w:body></w:document>'
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _ocr_page(lines: list[str], confidence: float = 0.84) -> OcrPage:
    return OcrPage(page=1, lines=[OcrLine(text=t, confidence=confidence, top=i * 10, left=0) for i, t in enumerate(lines)], confidence=confidence)


def test_pdf_still_dispatches_to_the_existing_pdf_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(standard_parse, "parse_pdf_standard", lambda data, name, **kw: calls.append(name) or {"summary": {"pages": 1}})
    result = standard_parse.parse_standard_document(b"%PDF-1.4", "标准.pdf")
    assert result["summary"]["pages"] == 1 and calls == ["标准.pdf"]


def test_word_is_split_on_page_breaks_and_parsed():
    lines = COVER + BODY + METHOD + TABLE
    breaks = {len(COVER) - 1, len(COVER) + len(BODY) - 1, len(COVER) + len(BODY) + len(METHOD) - 1}
    result = standard_parse.parse_standard_document(_docx(lines, breaks), "测试标准.docx")
    assert result["meta"]["source_format"] == "docx"
    assert (result["meta"]["standard_no"], result["meta"]["name"]) == ("Q/TEST 123-2024", "测试标准")
    assert result["summary"]["pages"] == 4 and result["summary"]["items"] == 2
    assert any("页码为近似值" in warning for warning in result["warnings"])


def test_word_upload_is_parsed_through_the_api():
    lines = COVER + BODY + METHOD + TABLE
    response = client.post("/api/v1/standards/parse", files={"file": ("测试标准.docx", _docx(lines, {len(COVER) - 1}), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["items"] == 2


def test_image_is_parsed_with_ocr_confidence(monkeypatch):
    lines = COVER + BODY + METHOD + TABLE
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_image", lambda data: _ocr_page(lines))
    result = standard_parse.parse_standard_document(b"fake-image", "扫描标准.png")
    assert result["meta"]["source_format"] == "image"
    assert result["summary"]["items"] == 2 and result["summary"]["ocr_avg_confidence"] == 0.84
    assert any("OCR" in warning for warning in result["warnings"])


def test_image_upload_is_parsed_through_the_api(monkeypatch):
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_image", lambda data: _ocr_page(COVER + BODY + METHOD + TABLE))
    response = client.post("/api/v1/standards/parse", files={"file": ("扫描标准.jpg", b"fake-image", "image/jpeg")})
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["items"] == 2


def test_image_without_ocr_dependency_reports_the_capability_gap(monkeypatch):
    def unavailable(data):
        raise OcrUnavailable("未安装 OCR 依赖：pip install rapidocr_onnxruntime")
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_image", unavailable)
    with pytest.raises(ValueError, match="OCR"):
        standard_parse.parse_standard_document(b"fake-image", "扫描标准.png")


def test_image_without_recognised_text_reports_explicitly(monkeypatch):
    monkeypatch.setattr(standard_parse.ocr_service, "ocr_image", lambda data: OcrPage(page=1, lines=[], confidence=0.0))
    with pytest.raises(ValueError, match="未能从图片中识别出文字"):
        standard_parse.parse_standard_document(b"fake-image", "空白.png")


def test_image_with_ocr_disabled_reports_explicitly(monkeypatch):
    monkeypatch.setenv("P0_OCR_ENABLED", "0")
    with pytest.raises(ValueError, match="已关闭 OCR"):
        standard_parse.parse_standard_document(b"fake-image", "扫描标准.png")


def test_unreadable_word_reports_explicitly():
    with pytest.raises(ValueError, match="不是有效的 .docx"):
        standard_parse.parse_standard_document(b"not-a-zip", "坏文件.docx")
    with pytest.raises(ValueError, match="没有可解析的文字内容"):
        standard_parse.parse_standard_document(_docx([]), "空文档.docx")


def test_unsupported_format_is_rejected_with_supported_list():
    with pytest.raises(ValueError, match="支持 PDF、Word"):
        standard_parse.parse_standard_document(b"plain text", "标准.txt")


def test_unsupported_upload_returns_clear_400():
    response = client.post("/api/v1/standards/parse", files={"file": ("标准.txt", b"plain text", "text/plain")})
    assert response.status_code == 400
    assert ".txt" in response.json()["detail"] and "支持 PDF、Word" in response.json()["detail"]
