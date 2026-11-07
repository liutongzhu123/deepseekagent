"""R14 required table cells: empty vs placeholder vs explicit N/A."""
from __future__ import annotations

import zipfile
from pathlib import Path

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _tc(text: str) -> str:
    return f'<w:tc><w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc>'


def _table(rows: list[list[str]]) -> str:
    body = "".join("<w:tr>" + "".join(_tc(cell) for cell in row) + "</w:tr>" for row in rows)
    return f"<w:tbl>{body}</w:tbl>"


def _make_docx(path: Path, body: str) -> None:
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _r14(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R14")


def test_empty_required_value_fails(tmp_path):
    path = tmp_path / "empty.docx"
    _make_docx(path, _table([["样品编号", ""]]))

    finding = _r14(path)

    assert finding["status"] == "fail"
    assert "「样品编号」未填写" in finding["message"]
    assert finding["location"]["section"] == "表格必填项"


def test_underscore_placeholder_fails(tmp_path):
    path = tmp_path / "placeholder.docx"
    _make_docx(path, _table([["委托单位", "____"]]))

    finding = _r14(path)

    assert finding["status"] == "fail"
    assert "占位符未替换" in finding["message"]


def test_explicit_na_is_reasonable_blank(tmp_path):
    path = tmp_path / "na.docx"
    _make_docx(path, _table([["委托单位", "—"]]))

    finding = _r14(path)

    assert finding["status"] == "pass"
    assert "明确杠掉" in finding["message"]


def test_header_column_empty_data_fails(tmp_path):
    path = tmp_path / "header.docx"
    _make_docx(path, _table([["样品名称", "检验项目"], ["", "气味"]]))

    finding = _r14(path)

    assert finding["status"] == "fail"
    assert "「样品名称」未填写" in finding["message"]


def test_non_required_label_and_no_table_skip(tmp_path):
    optional = tmp_path / "optional.docx"
    _make_docx(optional, _table([["备注", ""]]))
    assert _r14(optional)["status"] == "skip"

    empty = tmp_path / "no-table.docx"
    _make_docx(empty, "<w:p><w:r><w:t>无表格</w:t></w:r></w:p>")
    assert _r14(empty)["status"] == "skip"
