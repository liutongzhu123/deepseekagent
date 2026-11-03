"""Static checks for the P1-2 task progress UI (no browser required)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_standard_panel_renders_parse_and_publish_task_progress():
    html = read("index.html")
    for element in ("stdTaskBox", "stdTaskBar", "stdTaskStage", "stdTaskCancel", "stdTaskError"):
        assert f'id="{element}"' in html
    for element in ("publishTaskBox", "publishTaskBar", "publishTaskStage", "publishTaskCancel", "publishTaskError"):
        assert f'id="{element}"' in html


def test_ui_polls_task_status_and_supports_cancel_resume():
    js = read("ui.js")
    assert "api/v1/tasks/" in js
    assert "function pollTask" in js
    assert "function cancelTask" in js
    assert "function resumeLatestTask" in js
    assert "data.task_id" in js
    assert "publishTaskCancel" in js
    assert "stdTaskCancel" in js


def test_task_styles_are_present():
    css = read("ui.css")
    assert ".task-box" in css
    assert ".task-box.is-succeeded" in css
