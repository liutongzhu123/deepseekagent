"""R13 bookmark integrity: pairing and internal references."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.docx_parser import parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _bookmark(name: str, ident: int, closed: bool = True) -> str:
    end = f'<w:bookmarkEnd w:id="{ident}"/>' if closed else ""
    return f'<w:p><w:bookmarkStart w:id="{ident}" w:name="{name}"/><w:r><w:t>正文</w:t></w:r>{end}</w:p>'


def _link(anchor: str) -> str:
    return f'<w:p><w:hyperlink w:anchor="{anchor}"><w:r><w:t>跳转</w:t></w:r></w:hyperlink></w:p>'


def _field(name: str) -> str:
    return f'<w:p><w:fldSimple w:instr=" REF {name} \\h "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'


def _make_docx(path: Path, body: str) -> None:
    document = f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _r13(path: Path) -> dict:
    return next(item for item in audit_report(parse_docx(path))["findings"] if item["rule_id"] == "R13")


def test_valid_bookmarks_and_links_pass(tmp_path):
    path = tmp_path / "ok.docx"
    _make_docx(path, _bookmark("sec1", 1) + _link("sec1") + _field("sec1"))

    finding = _r13(path)

    assert finding["status"] == "pass"
    assert finding["location"]["section"] == "书签/目录"


def test_missing_link_target_fails(tmp_path):
    path = tmp_path / "missing.docx"
    _make_docx(path, _bookmark("sec1", 1) + _link("sec2") + _field("sec3"))

    finding = _r13(path)

    assert finding["status"] == "fail"
    assert "引用失效：sec2（段落 2）" in finding["message"]
    assert "引用失效：sec3（段落 3）" in finding["message"]


def test_unclosed_bookmark_fails(tmp_path):
    path = tmp_path / "unclosed.docx"
    _make_docx(path, _bookmark("sec1", 1, closed=False))

    finding = _r13(path)

    assert finding["status"] == "fail"
    assert "书签未闭合：sec1（段落 1）" in finding["message"]


def test_duplicate_bookmark_name_fails(tmp_path):
    path = tmp_path / "duplicate.docx"
    _make_docx(path, _bookmark("sec1", 1) + _bookmark("sec1", 2))

    finding = _r13(path)

    assert finding["status"] == "fail"
    assert "书签重名：sec1" in finding["message"]


def test_no_bookmarks_skips(tmp_path):
    path = tmp_path / "none.docx"
    _make_docx(path, "<w:p><w:r><w:t>无书签</w:t></w:r></w:p>")

    finding = _r13(path)

    assert finding["status"] == "skip"
    assert "未识别到书签" in finding["message"]


SAMPLE = Path("/opt/dsh-agent/samples/byd/ots-250717(1)__ots-250717__袋子-100L.docx")


def test_bookmark_end_outside_paragraph_is_collected(tmp_path):
    path = tmp_path / "outside.docx"
    body = (
        '<w:p><w:bookmarkStart w:id="1" w:name="OLE_LINK1"/><w:r><w:t>正文</w:t></w:r></w:p>'
        '<w:bookmarkEnd w:id="1"/>'
    )
    _make_docx(path, body)

    data = parse_docx(path)
    finding = _r13(path)

    assert data.bookmark_ends == ["1"]
    assert finding["status"] == "pass"


@pytest.mark.skipif(not SAMPLE.exists(), reason="real draft not mounted")
def test_real_draft_bookmark_ends_outside_paragraphs():
    data = parse_docx(SAMPLE)
    finding = _r13(SAMPLE)

    assert len(data.bookmarks) == 31
    assert len(data.bookmark_ends) == 31
    assert "未闭合" not in finding["message"]
