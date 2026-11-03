import os
import zipfile
from pathlib import Path

import pytest

from app.docx_parser import Cell, _expand_table, parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_docx(path: Path, body: str, headers: list[str] | None = None, footers: list[str] | None = None) -> None:
    def part(tag: str, text: str) -> str:
        return f'<w:{tag} xmlns:w="{W}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:{tag}>'

    paragraphs = "".join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>' for line in body.split("\n"))
    document = f'<w:document xmlns:w="{W}"><w:body>{paragraphs}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        for index, text in enumerate(headers or [], start=1):
            archive.writestr(f"word/header{index}.xml", part("hdr", text))
        for index, text in enumerate(footers or [], start=1):
            archive.writestr(f"word/footer{index}.xml", part("ftr", text))


def sample_dir() -> Path:
    return Path(os.getenv("P0_SAMPLE_DIR", "/data"))


@pytest.mark.skipif(not (sample_dir() / "ots袋子-100L.docx").exists(), reason="real sample not mounted")
def test_parse_real_ots_report():
    data = parse_docx(sample_dir() / "ots袋子-100L.docx")
    assert data.cover_number == "QA17LX1LDT861"
    assert data.sample_no == "TS24LXJ8A-01"
    assert data.scores == [4.0, 4.0, 3.5]
    assert data.reported_mean is None
    assert data.limit == 3.3
    assert data.notices == 8
    assert [item["device_id"] for item in data.devices] == ["NJ.18.8.2431", "NJ.18.91.662"]
    assert data.devices[1]["device_name"] == "袋式法环境舱"
    assert data.embedded_images == 0
    assert data.external_images > 0
    assert data.highlights > 0
    assert data.red_runs > 0


@pytest.mark.skipif(not (sample_dir() / "ots袋子-100L.docx").exists(), reason="real sample not mounted")
def test_audit_real_ots_report():
    result = audit_report(parse_docx(sample_dir() / "ots袋子-100L.docx"))
    statuses = {item["rule_id"]: item["status"] for item in result["findings"]}
    assert statuses["R01"] == "fail"
    assert statuses["R02"] == "fail"
    assert statuses["R03"] == "fail"
    assert statuses["R04"] == "fail"
    assert statuses["R09"] == "fail"
    assert statuses["R10"] == "fail"
    assert statuses["R08"] == "pass"
    assert statuses["R07"] == "pass"


def test_header_and_footer_are_parsed(tmp_path):
    path = tmp_path / "normal.docx"
    _make_docx(
        path,
        "报告编号: QA-001",
        headers=["报告编号: QA-001"],
        footers=["第 1 页 共 1 页"],
    )
    data = parse_docx(path)

    assert data.cover_number == "QA-001"
    assert data.header_numbers == ["QA-001"]
    assert data.footer_pages == [1]
    assert audit_report(data)["findings"][0]["status"] == "pass"


def test_blank_header_fails_r01(tmp_path):
    path = tmp_path / "blank-header.docx"
    _make_docx(
        path,
        "报告编号: QA-001",
        headers=["报告编号："],
        footers=["第 1 页 共 1 页"],
    )
    data = parse_docx(path)

    assert data.header_numbers == [""]
    assert audit_report(data)["findings"][0]["status"] == "fail"


def test_multi_page_header_mismatch_fails_r01(tmp_path):
    path = tmp_path / "mismatch.docx"
    _make_docx(
        path,
        "报告编号: QA-001",
        headers=["报告编号: QA-001", "报告编号: QA-002"],
        footers=["第 1 页 共 2 页", "第 2 页 共 2 页"],
    )
    data = parse_docx(path)

    assert data.header_numbers == ["QA-001", "QA-002"]
    assert data.footer_pages == [1, 2]
    assert audit_report(data)["findings"][0]["status"] == "fail"


def test_toc_entries_and_pages(tmp_path):
    path = tmp_path / "toc.docx"
    _make_docx(
        path,
        "报告编号: QA-001\n目 录\n检验结论……(1)\n1.任务来源及目的……(2)",
        headers=["报告编号: QA-001"],
        footers=["第 1 页 共 2 页", "第 2 页 共 2 页"],
    )
    data = parse_docx(path)

    assert data.toc_entries == [
        {"title": "检验结论", "page": 1},
        {"title": "1.任务来源及目的", "page": 2},
    ]
    assert audit_report(data)["findings"][6]["status"] == "pass"  # R07


def test_toc_page_out_of_range_fails_r07(tmp_path):
    path = tmp_path / "toc-bad.docx"
    _make_docx(
        path,
        "报告编号: QA-001\n目 录\n附录A……(3)",
        headers=["报告编号: QA-001"],
        footers=["第 1 页 共 2 页", "第 2 页 共 2 页"],
    )
    data = parse_docx(path)
    result = audit_report(data)
    finding = next(item for item in result["findings"] if item["rule_id"] == "R07")

    assert finding["status"] == "fail"
    assert "超出" in finding["evidence"]


def test_r09_uses_run_styles_not_plain_text(tmp_path):
    path = tmp_path / "styled.docx"
    document = (
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:rPr>'
        f'<w:highlight w:val="yellow"/><w:color w:val="FF0000"/><w:sz w:val="24"/>'
        f'</w:rPr><w:t>红色</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    data = parse_docx(path)

    assert data.highlights == 1
    assert data.red_runs == 1
    assert data.font_sizes == [12.0]
    finding = next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R09")
    assert finding["status"] == "fail"


def test_expand_table_handles_gridspan_and_vmerge():
    table = [
        [Cell("评价员", gridspan=2), Cell("检验结果")],
        [Cell("1"), Cell("2"), Cell("3")],
        [Cell("甲", vmerge="restart"), Cell("4"), Cell("5")],
        [Cell("", vmerge="continue"), Cell("6"), Cell("7")],
    ]
    grid = _expand_table(table)

    assert grid[0] == ["评价员", "评价员", "检验结果"]
    assert grid[2] == ["甲", "4", "5"]
    assert grid[3][0] == "甲"
    assert all(len(row) == 3 for row in grid)


def test_expand_table_pads_empty_cells():
    grid = _expand_table([[Cell("A")], [Cell("B"), Cell("C")]])

    assert grid == [["A", ""], ["B", "C"]]


def test_expand_table_keeps_rows_across_page_boundary():
    # Word keeps a split table in one XML table; a blank boundary row must not
    # drop the rows that continue after the page break.
    grid = _expand_table([
        [Cell("评价员"), Cell("检验结果")],
        [Cell("1"), Cell("4")],
        [Cell(""), Cell("")],
        [Cell("2"), Cell("5")],
    ])

    assert len(grid) == 4
    assert grid[3] == ["2", "5"]
