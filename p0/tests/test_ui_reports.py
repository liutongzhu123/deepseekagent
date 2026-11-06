"""Static checks for the report workbench UI (no browser required)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_report_panel_has_multi_upload_project_and_history():
    html = read("index.html")
    for element in ("reportProject", "reportHistoryCard", "reportHistory", "reportHistoryCount"):
        assert f'id="{element}"' in html
    assert 'id="reportFile"' in html
    assert 'id="reportFile"' in html and "multiple" in html.split('id="reportFile"')[1].split(">")[0]


def test_ui_loads_history_batch_and_download_actions():
    js = read("ui.js")
    for marker in (
        "api/v1/reports/batch",
        "function loadReports",
        "function loadReport",
        "function renderReportHistory",
        "data-report-export",
        "data-report-source",
        "data-batch-export",
        "reportHistory",
    ):
        assert marker in js


def test_report_workbench_styles_are_present():
    css = read("ui.css")
    for selector in (".report-actions", ".report-history", ".report-history-item", ".batch-table"):
        assert selector in css


def test_report_panel_has_data_readiness_card():
    html = read("index.html")
    for element in ("reportInputsCard", "reportInputsPill", "reportInputsBody"):
        assert f'id="{element}"' in html
    js = read("ui.js")
    for marker in ("function loadReportInputs", "api/v1/reports/inputs/status", "reportInputsPill", "reportInputsBody"):
        assert marker in js


def test_report_workbench_has_review_controls():
    js = read("ui.js")
    for marker in (
        "function reviewFinding",
        "findingSeverityFilter",
        "data-review-status",
        "data-review-rule",
        "review_coverage",
    ):
        assert marker in js


def test_report_history_has_search_and_pagination():
    html = read("index.html")
    for element in (
        "reportHistoryQuery",
        "reportHistorySearch",
        "reportHistoryPager",
        "reportHistoryPrev",
        "reportHistoryNext",
        "reportHistoryPage",
        "reportHistoryMeta",
    ):
        assert f'id="{element}"' in html
    js = read("ui.js")
    for marker in (
        "reportPage",
        "api/v1/reports?",
        "reportHistoryQuery",
        "reportHistorySearch",
        "reportHistoryPrev",
        "reportHistoryNext",
        "state.reportPage.offset = 0",
    ):
        assert marker in js
    assert ".report-history-tools" in read("ui.css")

