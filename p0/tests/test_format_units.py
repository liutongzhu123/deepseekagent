"""R16 measurement units: whitelist case rules."""
from __future__ import annotations

import zipfile
from pathlib import Path

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_docx(path: Path, text: str) -> None:
    body = f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _r16(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R16")


def test_wrong_case_units_fail(tmp_path):
    path = tmp_path / "wrong.docx"
    _make_docx(path, "压力 100 mpa，温度 25 °c，扭矩 5 n·m，应力 2 kpa")

    finding = _r16(path)

    assert finding["status"] == "fail"
    assert "「mpa」应为「MPa」" in finding["message"]
    assert "「°c」应为「°C」" in finding["message"]
    assert "「n·m」应为「N·m」" in finding["message"]
    assert "「kpa」应为「kPa」" in finding["message"]


def test_canonical_units_pass(tmp_path):
    path = tmp_path / "clean.docx"
    _make_docx(path, "压力 100 MPa，温度 25 ℃，扭矩 5 N·m，尺寸 10 mm")

    finding = _r16(path)

    assert finding["status"] == "pass"


def test_no_numeric_unit_skips(tmp_path):
    path = tmp_path / "none.docx"
    _make_docx(path, "样品 5 个，结论符合要求")

    finding = _r16(path)

    assert finding["status"] == "skip"
    assert "未识别到数值后的计量单位" in finding["message"]


def test_unit_outside_whitelist_is_not_flagged(tmp_path):
    path = tmp_path / "bar.docx"
    _make_docx(path, "压力 2 bar")

    finding = _r16(path)

    assert finding["status"] == "pass"
    assert finding["message"].endswith("未发现白名单内的大小写错误")
