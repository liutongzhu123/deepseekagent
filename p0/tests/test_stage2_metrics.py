"""Stage-2 semantic metric math (no network)."""
from __future__ import annotations

from eval.stage2_metrics import compute_semantic_metrics


def _case(case_id: str, group: str, expected: list[dict]) -> dict:
    return {"id": case_id, "group": group, "text": "synthetic", "expected": expected}


def _response(*findings: dict) -> dict:
    return {"status": "ok", "findings": list(findings)}


def _finding(rule_id: str, message: str) -> dict:
    return {"rule_id": rule_id, "status": "fail", "severity": "high", "message": message, "evidence": ""}


def test_recall_and_false_positive_math():
    cases = [
        _case("s01-1", "S01", [{"rule_id": "S01", "keyword": "符和"}]),
        _case("s02-1", "S02", [{"rule_id": "S02", "keyword": "安全带"}]),
        _case("clean-1", "clean", []),
    ]
    responses = {
        "s01-1": _response(_finding("S01", "符和 是错别字")),
        "s02-1": _response(),
        "clean-1": _response(),
    }
    metrics = compute_semantic_metrics(cases, responses)

    assert metrics["tp"] == 1
    assert metrics["fn"] == 1
    assert metrics["fp"] == 0
    assert metrics["recall"] == 0.5
    assert metrics["fp_rate"] == 0.0
    assert metrics["gate_ok"] is False


def test_wrong_rule_but_matching_keyword_counts_as_detected_category_miss():
    cases = [_case("s04-1", "S04", [{"rule_id": "S04", "keyword": "弯曲强度"}])]
    responses = {"s04-1": _response(_finding("S02", "弯曲强度 与样品名称不一致"))}
    metrics = compute_semantic_metrics(cases, responses)

    assert metrics["tp"] == 1
    assert metrics["fn"] == 0
    assert metrics["fp"] == 0
    assert metrics["category_accuracy"] == 0.0


def test_clean_case_findings_are_false_positives():
    cases = [_case("clean-1", "clean", [])]
    responses = {"clean-1": _response(_finding("S01", "多余告警"))}
    metrics = compute_semantic_metrics(cases, responses)

    assert metrics["fp"] == 1
    assert metrics["fp_rate"] == 1.0
    assert metrics["clean_fp_rate"] == 1.0
    assert metrics["gate_ok"] is False
