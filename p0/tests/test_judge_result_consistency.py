"""R30 compares the overall conclusion with result-table verdicts (RA-1.5)."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from app.admin_config import default_config
from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report


HEADER = ["序号", "检验项目", "评价员", "检验结果", "检验均值", "限值要求", "符合性判定"]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _tc(text: str) -> str:
    return f'<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'


def _table(rows: list[list[str]]) -> str:
    body = "".join("<w:tr>" + "".join(_tc(cell) for cell in row) + "</w:tr>" for row in rows)
    return f"<w:tbl>{body}</w:tbl>"


def _data(conclusion: str, *judgments: str) -> ReportData:
    rows = [list(HEADER)] + [
        [str(index), "气味", f"评价员{index}", "4.0", "", "≤3.3", judgment]
        for index, judgment in enumerate(judgments, start=1)
    ]
    return ReportData(
        file_name="decision-consistency.docx",
        conclusion=conclusion,
        result_cells=rows,
        judgment=judgments[0] if judgments else "",
    )


def _r30(data: ReportData, config: dict | None = None) -> dict:
    return next(
        item for item in audit_report(data, config=config)["findings"]
        if item["rule_id"] == "R30"
    )


def test_rule_is_in_catalog_and_enabled_by_default():
    assert any(rule[0] == "R30" for rule in RULES)
    rule = next(item for item in default_config()["report"]["rules"] if item["id"] == "R30")
    assert rule["enabled"] is True
    assert rule["severity"] == "medium"
    findings = audit_report(_data("检验结论：符合要求。", "符合"))["findings"]
    assert sum(item["rule_id"] == "R30" for item in findings) == 1


def test_matching_positive_and_negative_verdicts_pass():
    assert _r30(_data("经检验，该样品符合委托单位要求。", "合格"))["status"] == "pass"
    assert _r30(_data("检验结论：该样品不符合要求。", "不合格"))["status"] == "pass"


def test_opposite_verdicts_fail_with_both_sides_in_evidence():
    finding = _r30(_data("经检验，该样品符合委托单位要求。", "不符合"))

    assert finding["status"] == "fail"
    assert "结论判定=positive" in finding["evidence"]
    assert "结果表总体判定=negative" in finding["evidence"]
    assert finding["location"]["section"] == "检验结论/检验结果表"


def test_mixed_table_verdicts_skip_until_aggregation_policy_is_confirmed():
    finding = _r30(_data("检验结论：均不符合要求。", "符合", "不符合"))

    assert finding["status"] == "skip"
    assert "缺少经确认的总体归并口径" in finding["message"]


def test_unrecognized_or_ambiguous_outcomes_skip_instead_of_pass():
    assert _r30(_data("未发现不符合项。", "符合"))["status"] == "skip"
    assert _r30(_data("尚无明显不符合项。", "符合"))["status"] == "skip"
    assert _r30(_data("未达到合格标准。", "符合"))["status"] == "skip"
    assert _r30(_data("不得判定为合格。", "符合"))["status"] == "skip"
    assert _r30(_data("符合且不符合。", "符合"))["status"] == "skip"
    assert _r30(_data("检验结论：符合。", "待复核"))["status"] == "skip"


def test_missing_conclusion_or_result_verdict_skips():
    no_conclusion = _r30(_data("", "符合"))
    no_result = _r30(_data("检验结论：符合。"))
    missing_column = ReportData(
        file_name="missing-judgment-column.docx",
        conclusion="检验结论：符合。",
        result_cells=[["序号", "检验项目"], ["1", "气味"]],
    )

    assert no_conclusion["status"] == "skip"
    assert no_result["status"] == "skip"
    assert _r30(missing_column)["status"] == "skip"


def test_parser_to_rule_end_to_end_on_synthetic_docx():
    summary = _table([
        ["样品名称", "样品A"],
        ["检验依据", "GB/T 1234"],
        ["检验项目", "气味"],
        ["检验结论", "该样品符合委托单位要求。"],
    ])
    result = _table([HEADER, ["1", "气味", "评价员1", "4.0", "", "≤3.3", "不符合"]])
    document = f'<w:document xmlns:w="{W}"><w:body>{summary}{result}<w:sectPr/></w:body></w:document>'
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "decision-consistency.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)
        data = parse_docx(path)

    finding = _r30(data)
    assert data.conclusion == "该样品符合委托单位要求。"
    assert data.result_cells[1][-1] == "不符合"
    assert finding["status"] == "fail"
