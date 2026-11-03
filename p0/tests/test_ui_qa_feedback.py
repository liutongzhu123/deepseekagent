"""Static checks for Q&A feedback and citation jump wiring."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_knowledge_page_has_feedback_entrypoints():
    html = read("index.html")
    for element in ("answerFeedback", "feedbackStatsCard", "feedbackStats", "feedbackTotal"):
        assert f'id="{element}"' in html


def test_ui_posts_feedback_and_jumps_to_citations():
    js = read("ui.js")
    for marker in (
        "api/v1/standards/ask/feedback",
        "api/v1/kb/feedback",
        "data-answer-feedback",
        "data-citation-standard",
        "function loadFeedbackStats",
        "standard_ask_feedback",
    ):
        assert marker in js


def test_feedback_styles_are_present():
    css = read("ui.css")
    for selector in (".answer-feedback", ".feedback-stat", ".citation-jump"):
        assert selector in css
