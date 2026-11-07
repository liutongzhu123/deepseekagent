"""R15 section/item numbering: gaps and duplicates."""
from __future__ import annotations

import zipfile
from pathlib import Path

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_docx(path: Path, lines: list[str]) -> None:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{line}</w:t></w:r></w:p>' for line in lines)
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _r15(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R15")


def test_clean_section_and_item_numbers_pass(tmp_path):
    path = tmp_path / "clean.docx"
    _make_docx(path, ["1. 任务来源", "2. 检验依据", "5.1 外观", "5.2 密封", "（1）准备", "（2）测量"])

    finding = _r15(path)

    assert finding["status"] == "pass"


def test_single_level_gap_fails(tmp_path):
    path = tmp_path / "single-gap.docx"
    _make_docx(path, ["1. 任务来源", "2. 检验依据", "4. 检验项目"])

    finding = _r15(path)

    assert finding["status"] == "fail"
    assert "编号断档：2→4" in finding["message"]


def test_section_gap_and_duplicate_fail(tmp_path):
    path = tmp_path / "section-gap.docx"
    _make_docx(path, ["5.1 外观", "5.2 密封", "5.4 噪声", "5.4 噪声复核"])

    finding = _r15(path)

    assert finding["status"] == "fail"
    assert "章节号断档：缺 5.3" in finding["message"]
    assert "章节号重复：5.4" in finding["message"]


def test_paren_item_gap_fails(tmp_path):
    path = tmp_path / "paren.docx"
    _make_docx(path, ["（1）准备", "（2）测量", "（4）记录"])

    finding = _r15(path)

    assert finding["status"] == "fail"
    assert "括号项目编号断档：2→4" in finding["message"]


def test_measurement_is_not_a_heading(tmp_path):
    path = tmp_path / "measure.docx"
    _make_docx(path, ["3.5 符合"])

    finding = _r15(path)

    assert finding["status"] == "skip"
