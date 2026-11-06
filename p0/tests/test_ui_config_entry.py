"""Static regression checks for the module config entry -> admin config jump."""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_module_config_entries_and_admin_targets_exist():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for prefix in ("report", "std", "quote"):
        assert html.count(f'id="{prefix}ConfigCard"') == 1
    assert html.count('id="reportConfigEdit"') == 1
    assert html.count('id="stdConfigOpen"') == 1
    assert html.count('id="quoteConfigOpen"') == 1
    assert html.count('id="modalStdConfig"') == 1
    assert html.count('id="modalQuoteConfig"') == 1
    for section in ("report", "standard", "quote"):
        assert html.count(f'id="config-section-{section}"') == 1
        assert f'data-config-section="{section}"' in html
    report_entry = html[html.index('id="reportConfigEdit"') - 200: html.index('id="reportConfigEdit"') + 200]
    assert "修改配置" in report_entry
    assert "管理端配置 →" not in html


def test_report_config_entry_matches_module_bar_layout():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "ui.js").read_text(encoding="utf-8")
    assert html.count('class="card module-config-bar"') == 3
    assert html.count('class="btn config-open-btn') == 3
    for element in ("reportConfigCard", "reportConfigInline", "reportConfigRev", "reportConfigEdit"):
        assert f"id=\"{element}\"" in html
    for element in ("reportConfigInline", "reportConfigRev", "reportRules"):
        assert f"'{element}'" in script
    # 报告审核页的「修改配置」与标准解析 / 报价助手的弹窗按钮走同一个管理端跳转。
    assert "openAdminConfig" in script


def test_admin_jump_targets_are_mapped_in_ui_script():
    script = (STATIC_DIR / "ui.js").read_text(encoding="utf-8")
    for section in ("report", "standard", "quote"):
        assert f"id: 'config-section-{section}'" in script
    assert "scroll-margin-top" in (STATIC_DIR / "ui.css").read_text(encoding="utf-8")
