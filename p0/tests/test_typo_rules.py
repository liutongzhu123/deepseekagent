"""R21 typo/病句 checks: whitelist suppression and paragraph location."""
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


def _r21(path: Path) -> list[dict]:
    return [item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R21"]


def test_typo_pair_is_located_to_paragraph(tmp_path):
    path = tmp_path / "typo.docx"
    _make_docx(path, ["检验报告", "经检验，该样品气味的检验结果符合要求。", "结论：检侧结果符合要求。"])
    findings = _r21(path)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "fail"
    assert "段落 3 发现错别字" in finding["message"]
    assert "「检侧」应为「检测」" in finding["message"]
    assert finding["location"]["cell"] == "段落 3"


def test_professional_term_whitelist_avoids_reversed_word_false_positive(tmp_path):
    path = tmp_path / "terms.docx"
    _make_docx(path, ["1.报告无“检验检测专用章”或“试验专用章”无效。", "检测检验机构出具的试验报告。"])
    finding = _r21(path)[0]
    assert finding["status"] == "pass"
    assert "未发现" in finding["message"]


def test_reversed_word_outside_whitelist_fails(tmp_path):
    path = tmp_path / "reversed.docx"
    _make_docx(path, ["出具的告报结论符合要求。"])
    finding = _r21(path)[0]
    assert finding["status"] == "fail"
    assert "「告报」应为「报告」" in finding["message"]


def test_repeated_word_char_and_punctuation_are_one_finding(tmp_path):
    path = tmp_path / "repeat.docx"
    _make_docx(path, ["检验检验结果符合要求，，报告的的数据准确。"])
    findings = _r21(path)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["status"] == "warn"
    assert "重复词「检验检验」" in finding["message"]
    assert "重复字「的的」" in finding["message"]
    assert "重复标点「，，」" in finding["message"]
    assert finding["location"]["cell"] == "段落 1"


def test_fixed_reduplications_are_whitelisted(tmp_path):
    path = tmp_path / "fixed.docx"
    _make_docx(path, ["为了了解样品性能，试验目的的确明确。"])
    assert _r21(path)[0]["status"] == "pass"


def test_signature_label_line_is_not_a_repeated_word(tmp_path):
    path = tmp_path / "signature.docx"
    _make_docx(path, ["批准批准审核审核主检主检"])
    assert _r21(path)[0]["status"] == "pass"


def test_clean_report_passes_and_skips_whitespace(tmp_path):
    path = tmp_path / "clean.docx"
    _make_docx(path, [
        "委托单位送样，样品数量 1 件。",
        "   ",
        "检验依据：Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2。",
        "气味等级≤3.3，检验结果符合要求。",
    ])
    finding = _r21(path)[0]
    assert finding["status"] == "pass"
    assert "扫描 3 段" in finding["message"]


def test_no_paragraphs_skips(tmp_path):
    path = tmp_path / "empty.docx"
    _make_docx(path, [])
    assert _r21(path)[0]["status"] == "skip"


def test_one_finding_per_affected_paragraph(tmp_path):
    path = tmp_path / "multi.docx"
    _make_docx(path, ["检侧结果符合要求。", "检验检验结果符合要求。"])
    findings = _r21(path)
    assert len(findings) == 2
    assert [item["location"]["cell"] for item in findings] == ["段落 1", "段落 2"]
