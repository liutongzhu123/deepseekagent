"""Static checks for the standard governance UI (no browser required)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_standard_detail_has_version_strip_and_unpublish_button():
    html = read("index.html")
    for element in ("unpublishBtn", "stdVersionStrip", "stdVersionList"):
        assert f'id="{element}"' in html


def test_ui_wires_unpublish_and_rollback():
    js = read("ui.js")
    for marker in (
        "/unpublish",
        "/rollback",
        "function renderStdVersions",
        "data-rollback",
        "standard_unpublished",
        "standard_rolled_back",
    ):
        assert marker in js


def test_version_strip_styles_are_present():
    css = read("ui.css")
    for selector in (".version-strip", ".version-list", ".version-chip"):
        assert selector in css


def test_standard_detail_marks_pending_low_confidence_limits():
    html = read("index.html")
    js = read("ui.js")
    css = read("ui.css")
    for marker in ("judgment-box", "pending-limit-list", "限值待确认", "待确认"):
        assert marker in js
    for marker in (".pending-limit-list", ".pending-limit", ".kpi-grid.six"):
        assert marker in css
    assert 'id="stdRules"' in html
    assert "renderJudgmentRules" in js
    assert "judgment_rules" in js
