from pathlib import Path

import pytest

from app.rules import RecordInput
from eval import report_files
from eval.stage1_metrics import KEY_RULES, compute_metrics, render_markdown
from eval.stage1_regression import load_records

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples" / "byd"


def _records() -> list[RecordInput]:
    return [
        RecordInput(sample_no="A", limit=3.5, scores=[3.5, 4.0, 4.0], reported_mean=3.8, test_date="2026-01-08", eval_date="2026-01-08", judgment="不符合"),
        RecordInput(sample_no="B", limit=3.5, scores=[3.5, 3.5, 4.0], reported_mean=3.7, test_date="2026-01-08", eval_date="2026-01-08", judgment="不符合"),
        RecordInput(sample_no="C", limit=3.5, scores=[4.0, 4.0, 4.5], reported_mean=4.2, test_date="2026-01-05", eval_date="2026-01-05", judgment="不符合"),
        RecordInput(sample_no="D", limit=3.3, scores=[3.5, 4.0, 4.0], reported_mean=3.8, test_date="2026-01-05", eval_date="2026-01-05", judgment="不符合"),
    ]


@pytest.mark.skipif(len(report_files(SAMPLES)) != 12, reason="12 BYD drafts not mounted")
def test_metrics_key_rules_have_full_recall():
    metrics = compute_metrics(SAMPLES, _records())

    assert metrics["gate_ok"] is True
    assert all(value == 1.0 for value in metrics["key_recall"].values())
    assert metrics["overall"]["precision"] == 1.0
    assert metrics["overall"]["recall"] == 1.0


def test_markdown_states_baseline_not_acceptance():
    markdown = render_markdown({"per_rule": {}, "overall": {"precision": 1, "recall": 1, "accuracy": 1}, "key_recall": {rule: 1.0 for rule in KEY_RULES}, "gate_ok": True, "draft_cases": 12, "record_cases": 20}, "records.xlsx")

    assert "不能替代 20–30 份正式报告金标准" in markdown
    assert "模板变体" in markdown
