"""Static regression checks for the module config entry -> admin config jump."""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_module_config_entries_and_admin_targets_exist():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert html.count('id="stdConfigOpen"') == 1
    assert html.count('id="quoteConfigOpen"') == 1
    assert html.count('id="modalStdConfig"') == 1
    assert html.count('id="modalQuoteConfig"') == 1
    for section in ("report", "standard", "quote"):
        assert html.count(f'id="config-section-{section}"') == 1
    for section in ("report", "standard", "quote"):
        assert f'data-config-section="{section}"' in html


def test_admin_jump_targets_are_mapped_in_ui_script():
    script = (STATIC_DIR / "ui.js").read_text(encoding="utf-8")
    for section in ("report", "standard", "quote"):
        assert f"id: 'config-section-{section}'" in script
    assert "scroll-margin-top" in (STATIC_DIR / "ui.css").read_text(encoding="utf-8")
