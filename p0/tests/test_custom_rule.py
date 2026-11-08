"""Admin-defined report conditions (RA-5.1); failure cases F1-F6 in app/rules.py."""
from __future__ import annotations

import pytest

from app import admin_config
from app.docx_parser import ReportData
from app.rules import (audit_report, evaluate_custom_rules, rules_catalog,
                      validate_custom_rule_expression)

def _data(**overrides) -> ReportData:
    return ReportData(file_name="custom.docx", **overrides)

def _config(**overrides) -> dict:
    rule = {
        "id": "C01", "name": "检验日期距生产日期不超过 30 天",
        "expression": "days_between(production_date, test_start) > 30",
        "severity": "medium", "enabled": True,
        "message": "检验日期距生产日期超过 30 天", "suggestion": "核对日期",
    }
    rule.update(overrides)
    return {"custom_rules": [rule]}

@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_config, "CONFIG_PATH", tmp_path / "rules.json")
    admin_config.reset_cache()
    yield
    admin_config.reset_cache()

def test_expression_validation():
    assert validate_custom_rule_expression(
        "days_between(production_date, test_start) > 30") == ""
    for bad in ["", "reported_mean >", "foo > 1", "__import__('os')",
                "client.lower() == 'x'"]:
        assert validate_custom_rule_expression(bad)

def test_missing_or_bad_date_does_not_hit():
    for dates in [{}, {"production_date": "2025-07-01"}, {"test_start": "2025-08-20"},
                  {"production_date": "bad", "test_start": "2025-08-20"}]:
        assert evaluate_custom_rules(_data(**dates), _config()) == []

def test_missing_date_comparison_does_not_hit():
    for expression in ["approval_date <= test_end", "approval_date == test_end",
                       "approval_date != ''"]:
        for dates in [{}, {"approval_date": "2025-08-20"},
                      {"test_end": "2025-08-19"},
                      {"approval_date": "bad", "test_end": "2025-08-19"}]:
            assert evaluate_custom_rules(_data(**dates),
                                         _config(expression=expression)) == []

def test_date_fields_compare_as_dates():
    data = _data(approval_date="2025-09-01", test_end="2025-10-01")
    findings = evaluate_custom_rules(
        data, _config(expression="approval_date < test_end"))
    assert [f.rule_id for f in findings] == ["C01"]
    data = _data(approval_date="2025-09-01")
    findings = evaluate_custom_rules(
        data, _config(expression="approval_date == '2025-09-01'"))
    assert [f.rule_id for f in findings] == ["C01"]

def test_empty_disabled_and_not_met_do_not_hit():
    data = _data(production_date="2025-07-01", test_start="2025-08-20")
    assert evaluate_custom_rules(data, {"custom_rules": []}) == []
    assert evaluate_custom_rules(data, {}) == []
    assert evaluate_custom_rules(data, _config(enabled=False)) == []
    later = _data(production_date="2025-08-01", test_start="2025-08-20")
    assert evaluate_custom_rules(later, _config()) == []
    result = audit_report(data, {"custom_rules": []})
    assert not [f for f in result["findings"] if f["layer"] == "custom"]

@pytest.mark.parametrize("expression", ["reported_mean > 'abc'",
                                        "reported_mean > client"])
def test_bad_values_do_not_raise_or_hit(expression):
    data = _data(limit=3.3, reported_mean=0.0, client="")
    assert evaluate_custom_rules(data, _config(expression=expression)) == []

def test_hit_evidence_and_audit():
    data = _data(production_date="2025-07-01", test_start="2025-08-20")
    findings = evaluate_custom_rules(data, _config())
    assert [(f.rule_id, f.status, f.severity) for f in findings] == [
        ("C01", "fail", "medium")]
    evidence = findings[0].evidence
    assert "days_between(production_date, test_start) > 30" in evidence
    assert "production_date=2025-07-01" in evidence
    assert "test_start=2025-08-20" in evidence and "50 天" in evidence
    result = audit_report(data, _config())
    assert [f["rule_id"] for f in result["findings"] if f["layer"] == "custom"] == [
        "C01"]
    assert {r["id"]: r for r in rules_catalog(_config())}["C01"]["layer"] == "custom"

def test_hand_edited_bad_expression_is_skipped():
    findings = evaluate_custom_rules(_data(), _config(expression="__import__('os')"))
    assert [f.status for f in findings] == ["skip"]
    assert "表达式无效" in findings[0].message

def test_config_validation_and_persistence(config_path):
    section = admin_config.default_config()["report"]
    section["custom_rules"] = _config()["custom_rules"]
    assert admin_config.validate_section("report", section)["custom_rules"][0]["id"] == "C01"
    for key, value in [("expression", "__import__('os')"), ("id", "R01")]:
        bad = admin_config.default_config()["report"]
        bad["custom_rules"] = _config(**{key: value})["custom_rules"]
        with pytest.raises(admin_config.ConfigError):
            admin_config.validate_section("report", bad)
    admin_config.update_section("report", _config())
    saved = admin_config.update_section("report", {"expected_notices": 9})
    assert saved["report"]["custom_rules"][0]["id"] == "C01"
    assert admin_config.reset_section("report")["report"]["custom_rules"] == []
