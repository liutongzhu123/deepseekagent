from pathlib import Path

from app.rule_dsl import DSLRule, RulePack, generate_judgment_rules, load_pack
from app.standard_extract.table_mapping import is_high_confidence, needs_confirmation


def test_pack_has_ten_examples():
    pack = load_pack()
    assert len(pack.rules) == 10
    assert pack.version == 1


def test_operators_and_hits():
    pack = RulePack(version=1, rules=[
        DSLRule("X1", "空值", "report", "client", "empty", None),
        DSLRule("X2", "包含", "report", "basis", "contains", "Q/BYDQ"),
        DSLRule("X3", "数值", "report", "mean", "gt", 4.0),
    ])
    findings = pack.evaluate({"report": {"basis": "Q/BYDQ", "mean": 4.2}})
    assert {item["rule_id"] for item in findings} == {"X1", "X2", "X3"}
    assert pack.stats()["hits"] == {"X1": 1, "X2": 1, "X3": 1}


def test_rollout_and_disabled_rules_are_skipped():
    pack = RulePack(version=1, rules=[
        DSLRule("OFF", "off", "report", "x", "empty", None, rollout=0),
        DSLRule("DIS", "dis", "report", "x", "empty", None, enabled=False),
    ])
    assert pack.evaluate({"report": {}}) == []


def test_regex_and_empty_do_not_crash_on_missing_context():
    pack = RulePack(version=1, rules=[DSLRule("R", "re", "report", "a", "regex", "^A")])
    assert pack.evaluate({}) == []


def test_shipped_pack_passes_validation():
    from app.rule_dsl import validate_pack

    assert validate_pack() == []


def test_missing_source_is_skipped_not_fired():
    pack = RulePack(version=1, rules=[DSLRule("X1", "空值", "inputs", "sample_no", "empty", None)])
    assert pack.evaluate({"report": {}}, available_sources={"report"}) == []
    assert pack.stats()["skipped"] == {"X1": 1}
    assert pack.skipped == {"X1": 1}


def test_rule_generation_takes_only_high_confidence_limits():
    result = generate_judgment_rules([
        {
            "clause": "5.2",
            "name": "密封性能",
            "page": 4,
            "limits": [
                {"kind": "upper", "upper": 3, "unit": "mL/min", "raw": "≤3mL/min",
                 "mapping_confidence": "high", "table_page": 4, "table_row": 1,
                 "column": "限值"},
                {"kind": "lower", "lower": 50, "unit": "MΩ", "raw": "≥50MΩ",
                 "mapping_confidence": "high"},
                {"kind": "range", "lower": 160, "upper": 210, "unit": "HBW",
                 "raw": "160～210", "mapping_confidence": "high"},
                {"kind": "upper", "upper": 90, "unit": "", "raw": "90",
                 "mapping_confidence": "low"},
            ],
        }
    ])
    assert [rule["op"] for rule in result["rules"]] == ["gt", "lt", "not_between"]
    assert [rule["value"] for rule in result["rules"]] == [3, 50, [160, 210]]
    assert [rule["id"] for rule in result["rules"]] == ["J001", "J002", "J003"]
    first = result["rules"][0]
    assert (first["clause"], first["raw"], first["page"]) == ("5.2", "≤3mL/min", 4)
    assert (first["table_row"], first["confidence"], first["status"]) == (1, "high", "draft")
    pending = result["pending"]
    assert len(pending) == 1
    assert pending[0]["raw"] == "90"
    assert "低置信" in pending[0]["reason"]


def test_text_limits_without_mapping_confidence_are_rule_eligible():
    item = {
        "clause": "5.1",
        "name": "外观",
        "page": 2,
        "limits": [{"kind": "upper", "upper": 10, "unit": "mm", "raw": "≤10mm"}],
    }
    result = generate_judgment_rules([item])
    assert len(result["rules"]) == 1
    assert result["pending"] == []
    assert result["rules"][0]["raw"] == "≤10mm"


def test_directionless_and_incomplete_limits_are_pending_not_rules():
    result = generate_judgment_rules([
        {
            "clause": "5.3",
            "name": "电气",
            "limits": [
                {"kind": "value", "value": 12, "unit": "V", "raw": "12V",
                 "mapping_confidence": "high"},
                {"kind": "range", "lower": None, "upper": 20, "unit": "℃",
                 "raw": "20", "mapping_confidence": "high"},
            ],
        }
    ])
    assert result["rules"] == []
    assert {entry["raw"] for entry in result["pending"]} == {"12V", "20"}
    assert any("比较方向" in entry["reason"] for entry in result["pending"])
    assert any("区间" in entry["reason"] for entry in result["pending"])


def test_missing_confidence_is_high_but_low_needs_confirmation():
    assert is_high_confidence({"kind": "upper"}) is True
    assert needs_confirmation({"mapping_confidence": "low"}) is True


def test_range_operators_evaluate_a_range():
    inside = RulePack(version=1, rules=[DSLRule("R", "区间", "report", "value", "between", [10, 20])])
    assert inside.evaluate({"report": {"value": 15}})
    assert inside.evaluate({"report": {"value": 10}})
    assert inside.evaluate({"report": {"value": 20}})
    assert inside.evaluate({"report": {"value": 25}}) == []

    outside = RulePack(version=1, rules=[
        DSLRule("R", "区间", "report", "value", "not_between", [10, 20])
    ])
    assert outside.evaluate({"report": {"value": 15}}) == []
    assert outside.evaluate({"report": {"value": 25}})


def test_invalid_pack_reports_actionable_errors():
    from app.rule_dsl import validate_pack_data

    errors = validate_pack_data({
        "version": 0,
        "rules": [
            {"id": "A", "name": "", "source": "unknown", "field": "", "op": "wat", "severity": "urgent", "rollout": 200},
            {"id": "A", "name": "duplicate", "source": "report", "field": "x", "op": "eq", "value": 1},
        ],
    })
    assert any("version" in item for item in errors)
    assert any("id 重复" in item for item in errors)
    assert any("source" in item for item in errors)
    assert any("op" in item for item in errors)
    assert any("severity" in item for item in errors)
    assert any("rollout" in item for item in errors)
