"""R12 page-level format checks: blank page / page-number gap / mojibake."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _br() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _make_docx(path: Path, body: str, footers: list[str]) -> None:
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        for index, text in enumerate(footers, start=1):
            archive.writestr(
                f"word/footer{index}.xml",
                f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:ftr>',
            )


def _r12(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R12")


def test_blank_page_fails(tmp_path):
    path = tmp_path / "blank.docx"
    body = _p("第一页正文内容") + _br() + _p("") + _br() + _p("第三页正文内容")
    _make_docx(path, body, ["第 1 页 共 3 页", "第 2 页 共 3 页", "第 3 页 共 3 页"])

    finding = _r12(path)

    assert len(parse_docx(path).pages) == 3
    assert finding["status"] == "fail"
    assert "空白页：2" in finding["message"]
    assert finding["location"]["page"] == 2


def test_page_number_gap_fails(tmp_path):
    path = tmp_path / "gap.docx"
    body = _p("第一页内容") + _br() + _p("第二页内容") + _br() + _p("第四页内容")
    _make_docx(path, body, ["第 1 页 共 4 页", "第 2 页 共 4 页", "第 4 页 共 4 页"])

    finding = _r12(path)

    assert finding["status"] == "fail"
    assert "页码断档：缺 3" in finding["message"]
    assert finding["location"]["page"] == 3


@pytest.mark.parametrize(
    ("body", "footers", "status", "needle"),
    [
        (
            _p("正常内容\ufffd\ue000\ufffd"),
            ["第 1 页 共 1 页"],
            "fail",
            "乱码/不可打印字符 3 个",
        ),
        (
            _p("第一页正文内容") + _br() + _p("第二页正文内容"),
            ["第 1 页 共 2 页", "第 2 页 共 2 页"],
            "pass",
            "页码连续",
        ),
        (
            _p("整篇没有分页符"),
            ["第 1 页 共 5 页"],
            "skip",
            "未识别到分页符",
        ),
    ],
)
def test_r12_other_outcomes(tmp_path, body, footers, status, needle):
    path = tmp_path / "case.docx"
    _make_docx(path, body, footers)

    finding = _r12(path)

    assert finding["status"] == status
    assert needle in finding["message"]
