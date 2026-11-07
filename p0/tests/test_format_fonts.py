"""R17 paragraph font/size consistency."""
from __future__ import annotations

import zipfile
from pathlib import Path

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY = "这是正文段落内容用于字体字号一致性检测示例。"


def _p(text: str, font: str | None = None, size: float | None = None, bold: bool = False) -> str:
    props = ""
    if font:
        props += f'<w:rFonts w:eastAsia="{font}"/>'
    if size:
        props += f'<w:sz w:val="{int(size * 2)}"/>'
    if bold:
        props += "<w:b/>"
    run_props = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _make_docx(path: Path, body: str) -> None:
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _r17(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R17")


def test_consistent_body_fonts_pass(tmp_path):
    path = tmp_path / "clean.docx"
    _make_docx(path, "".join(_p(BODY, "宋体", 12) for _ in range(6)))

    finding = _r17(path)

    assert finding["status"] == "pass"
    assert "宋体 12pt" in finding["message"]


def test_deviating_font_and_size_warn(tmp_path):
    path = tmp_path / "deviate.docx"
    body = "".join(_p(BODY, "宋体", 12) for _ in range(6))
    body += _p(BODY, "黑体", 12) + _p(BODY, "宋体", 14)
    _make_docx(path, body)

    finding = _r17(path)

    assert finding["status"] == "warn"
    assert "黑体" in finding["message"]
    assert "14pt" in finding["message"]
    assert finding["location"]["section"] == "字体/字号"


def test_heading_and_bold_are_excluded(tmp_path):
    path = tmp_path / "heading.docx"
    body = _p("5. 检验结果", "黑体", 18, bold=True) + "".join(_p(BODY, "宋体", 12) for _ in range(6))
    _make_docx(path, body)

    finding = _r17(path)

    assert finding["status"] == "pass"


def test_insufficient_body_samples_skip(tmp_path):
    path = tmp_path / "few.docx"
    _make_docx(path, "".join(_p(BODY, "宋体", 12) for _ in range(4)))

    finding = _r17(path)

    assert finding["status"] == "skip"
    assert "样本不足" in finding["message"]
    assert parse_docx(path).paragraph_styles[0]["font"] == "宋体"
