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
