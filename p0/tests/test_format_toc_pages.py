"""R18 TOC entry pages vs body heading pages."""
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


def _make_docx(path: Path, body: str, footers: list[str] | None = None) -> None:
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        for index, text in enumerate(footers or [], start=1):
            archive.writestr(
                f"word/footer{index}.xml",
                f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:ftr>',
            )


def _r18(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R18")


def test_toc_pages_match_body_heading_pages(tmp_path):
    path = tmp_path / "match.docx"
    body = (
        _p("报告编号: QA-001") + _p("目 录")
        + _p("1.任务来源及目的……(1)")
        + _p("2.1 方法依据……(2)")
        + _p("3.检验结果……(2)")
        + _br()
        + _p("（天津）有限公司 共 2 页 第 1 页")
        + _p("1.任务来源及目的") + _p("正文内容")
        + _p("（天津）有限公司 共 2 页 第 2 页")
        + _p("2.1方法依据") + _p("正文内容") + _p("3.检验结果")
        + _p("1.任务来源及目的")
    )
    _make_docx(path, body)

    finding = _r18(path)

    assert finding["status"] == "pass"
    assert "3 条中 3 条可定位" in finding["message"]


def test_toc_page_mismatch_is_located(tmp_path):
    path = tmp_path / "mismatch.docx"
    body = (
        _p("目 录")
        + _p("1.任务来源及目的……(1)")
        + _p("3.检验结果……(1)")
        + _br()
        + _p("（天津）有限公司 共 2 页 第 1 页")
        + _p("1.任务来源及目的")
        + _p("（天津）有限公司 共 2 页 第 2 页")
        + _p("3.检验结果")
    )
    _make_docx(path, body)

    finding = _r18(path)

    assert finding["status"] == "fail"
    assert "「3.检验结果」目录第 1 页、正文第 2 页" in finding["message"]
    assert finding["location"] == {"page": 2, "section": "目录/正文页码", "cell": "3.检验结果"}


def test_unlocatable_titles_are_reported_without_false_fail(tmp_path):
    path = tmp_path / "partial.docx"
    body = (
        _p("目 录")
        + _p("检验结论……(1)")
        + _p("1.任务来源及目的……(1)")
        + _br()
        + _p("（天津）有限公司 共 1 页 第 1 页")
        + _p("1.任务来源及目的")
    )
    _make_docx(path, body)

    finding = _r18(path)

    assert finding["status"] == "pass"
    assert "1 条可定位" in finding["message"]
    assert "未定位=1" in finding["evidence"]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        (_p("报告编号: QA-001") + _p("1.任务来源及目的"), "未识别到目录条目"),
        (_p("目 录") + _p("1.任务来源及目的……(1)") + _p("1.任务来源及目的"), "未识别到可用的正文页码"),
        (
            _p("目 录") + _p("9.不存在的章节……(1)") + _br()
            + _p("（天津）有限公司 共 1 页 第 1 页") + _p("1.任务来源及目的"),
            "均未定位",
        ),
    ],
)
def test_skip_cases(tmp_path, body, needle):
    path = tmp_path / "skip.docx"
    _make_docx(path, body)

    finding = _r18(path)

    assert finding["status"] == "skip"
    assert needle in finding["message"]


def test_footer_page_numbers_are_used_when_they_align(tmp_path):
    path = tmp_path / "footers.docx"
    body = (
        _p("目 录") + _p("1.任务来源及目的……(1)") + _p("2.检验依据……(2)")
        + _p("1.任务来源及目的")
        + _br()
        + _p("2.检验依据")
    )
    _make_docx(path, body, footers=["第 1 页 共 2 页", "第 2 页 共 2 页"])

    finding = _r18(path)

    assert finding["status"] == "pass"
    assert "2 条中 2 条可定位" in finding["message"]


def test_marker_after_content_assigns_whole_page(tmp_path):
    path = tmp_path / "bottom-marker.docx"
    body = (
        _p("目 录") + _p("1.任务来源及目的……(1)") + _p("2.检验依据……(2)")
        + _p("1.任务来源及目的") + _p("共 2 页 第 1 页")
        + _br()
        + _p("2.检验依据") + _p("共 2 页 第 2 页")
    )
    _make_docx(path, body)

    finding = _r18(path)

    assert finding["status"] == "pass"
    assert "2 条中 2 条可定位" in finding["message"]
