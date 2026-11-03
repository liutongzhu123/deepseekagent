"""Static checks for the quote history UI (no browser required)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_quote_panel_has_history_card():
    html = read("index.html")
    for element in ("quoteHistoryCard", "quoteHistory", "quoteHistoryCount"):
        assert f'id="{element}"' in html


def test_ui_loads_quote_history_and_downloads():
    js = read("ui.js")
    for marker in (
        "api/v1/quotes?limit=20",
        "function renderQuoteHistory",
        "function loadQuotes",
        "data-quote-download",
        "quoteHistory",
    ):
        assert marker in js


def test_quote_history_styles_are_present():
    css = read("ui.css")
    for selector in (".quote-history", ".quote-history-item", ".quote-history-pills"):
        assert selector in css
