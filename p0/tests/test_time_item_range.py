"""RA-2.3: 单项检验时间落在汇总检验时间内，最早/最晚与区间端点一致。

The BYD drafts only carry the aggregate "检验于 A 至 B" sentence, so real
samples must report ``skip`` (never a false ``pass``).  Item tables are
exercised with synthetic fixtures covering F1-F8 of ``r19_item_times``.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.docx_parser import Cell, ReportData, parse_docx
from app.rules import audit_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
HEADER = ["序号", "检验项目", "检验日期"]


def _table(header: list[str], rows: list[list[str]]) -> list[list[Cell]]:
    return [[Cell(text) for text in header]] + [[Cell(text) for text in row] for row in rows]


def _data(tables: list | None = None, **overrides) -> ReportData:
    base = dict(file_name="time.docx", test_start="2025-03-02", test_end="2025-03-03", tables=tables or [])
    base.update(overrides)
    return ReportData(**base)


def _finding(data: ReportData) -> dict:
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R19")


def test_r19_is_in_the_catalog_with_a_location():
    finding = _finding(_data([_table(HEADER, [["1", "气味", "2025-03-02"]])]))

    assert finding["title"] == "单项检验时间"
    assert finding["location"]["page"] >= 1
    assert finding["location"]["section"] == "检验项目表"


def test_item_times_inside_range_with_matching_endpoints_pass():
    finding = _finding(_data([_table(HEADER, [["1", "气味", "2025-03-02"], ["2", "外观", "2025-03-03"]])]))

    assert finding["status"] == "pass"
    assert finding["evidence"] == "最早=2025-03-02; 最晚=2025-03-03"


def test_range_cell_uses_both_endpoints():
    finding = _finding(_data([_table(HEADER, [["1", "气味", "2025-03-02 至 2025-03-03"]])]))

    assert finding["status"] == "pass"


@pytest.mark.parametrize(
    "rows,overrides,status,fragment",
    [
        # F1 / F2: item time outside the summary range.
        ([["1", "气味", "2025-03-01"], ["2", "外观", "2025-03-03"]], {}, "fail", "早于汇总开始 2025-03-02"),
        ([["1", "气味", "2025-03-02"], ["2", "外观", "2025-03-04"]], {}, "fail", "晚于汇总结束 2025-03-03"),
        # F3 / F4: earliest/latest must equal the summary endpoints.
        ([["1", "气味", "2025-03-03"]], {}, "fail", "最早单项时间"),
        ([["1", "气味", "2025-03-02"]], {}, "fail", "最晚单项时间"),
        # F5 / F6: inverted item or summary range.
        ([["1", "气味", "2025-03-03 至 2025-03-02"]], {}, "fail", "单项起止倒置"),
        ([["1", "气味", "2025-03-03"]], {"test_start": "2025-03-04", "test_end": "2025-03-03"}, "fail", "汇总检验时间起止倒置"),
        # F7 / F8: missing data skips instead of passing.
        ([["1", "气味", "2025-03-02"]], {"test_start": "", "test_end": ""}, "skip", "未识别到汇总检验时间"),
        ([], {}, "skip", "未识别到单项检验时间"),
    ],
)
def test_failure_and_skip_scenarios(rows, overrides, status, fragment):
    finding = _finding(_data([_table(HEADER, rows)], **overrides))

    assert finding["status"] == status
    assert fragment in finding["evidence"] + finding["message"]


def test_production_and_sampling_dates_are_not_item_times():
    table = _table(["样品名称", "生产日期", "送样日期"], [["XXXX", "2024-11-25", "2025-03-16"]])

    assert _finding(_data([table]))["status"] == "skip"


def test_summary_row_alone_cannot_make_the_check_pass():
    finding = _finding(_data([_table(HEADER, [["合计", "2025-03-02 至 2025-03-03"]])]))

    assert finding["status"] == "skip"


def test_alternative_headers_and_duration_column():
    # 试验时间/h is a duration column and must not be read as a date column.
    header = ["序号", "检验项目", "试验日期", "试验时间/h"]
    table = _table(header, [["1", "气味", "2025-03-02", "2"], ["2", "外观", "2025-03-03", "2"]])

    assert _finding(_data([table]))["status"] == "pass"


def _make_docx(path: Path, paragraph: str, rows: list[list[str]]) -> None:
    def cell(text: str) -> str:
        return f"<w:tc><w:tcPr/><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

    table = "<w:tbl>" + "".join("<w:tr>" + "".join(cell(text) for text in row) + "</w:tr>" for row in rows) + "</w:tbl>"
    body = f'<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>'
    document = f'<w:document xmlns:w="{W}"><w:body>{body}{table}<w:sectPr/></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def test_parsed_docx_item_table_is_checked_end_to_end(tmp_path):
    path = tmp_path / "item-times.docx"
    _make_docx(
        path,
        "检验于2025年03月02日至2025年03月03日在中汽研汽车检验中心（天津）有限公司进行。",
        [HEADER, ["1", "气味", "2025-03-04"]],
    )

    finding = _finding(parse_docx(path))

    assert finding["status"] == "fail"
    assert "晚于汇总结束 2025-03-03" in finding["evidence"]
