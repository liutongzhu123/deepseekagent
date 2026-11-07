"""XG numbering config is present, disabled by default and overridable."""
from __future__ import annotations

from pathlib import Path

from app import xg_rules


def test_default_config_is_disabled():
    config = xg_rules.load_config()

    assert config["enabled"] is False
    assert config["rules"]["report_number_offset_from_voided"] == 1
    assert config["rules"]["require_void_note"] is True


def test_custom_config_can_enable_rule(tmp_path):
    path = tmp_path / "xg.yaml"
    path.write_text(
        "enabled: true\nrules:\n  report_number_offset_from_voided: 2\n  require_void_note: false\n",
        encoding="utf-8",
    )

    config = xg_rules.load_config(path)

    assert config["enabled"] is True
    assert config["rules"]["report_number_offset_from_voided"] == 2
    assert config["rules"]["require_void_note"] is False
    assert config["rules"]["void_note_keyword"] == "原报告作废"


def test_missing_or_broken_config_falls_back_to_disabled(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("enabled: [", encoding="utf-8")

    assert xg_rules.load_config(tmp_path / "missing.yaml")["enabled"] is False
    assert xg_rules.load_config(broken)["enabled"] is False
