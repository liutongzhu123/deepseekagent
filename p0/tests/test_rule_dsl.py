from pathlib import Path

from app.rule_dsl import DSLRule, RulePack, load_pack


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
