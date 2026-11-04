from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.rules import RecordInput
from eval.stage1_regression import run_draft_regression, run_record_regression

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples" / "byd"


def _records() -> list[RecordInput]:
    return [
        RecordInput(sample_no="A", limit=3.5, scores=[3.5, 4.0, 4.0], reported_mean=3.8, test_date="2026-01-08", eval_date="2026-01-08", judgment="不符合"),
        RecordInput(sample_no="B", limit=3.5, scores=[3.5, 3.5, 4.0], reported_mean=3.7, test_date="2026-01-08", eval_date="2026-01-08", judgment="不符合"),
        RecordInput(sample_no="C", limit=3.5, scores=[4.0, 4.0, 4.5], reported_mean=4.2, test_date="2026-01-05", eval_date="2026-01-05", judgment="不符合"),
        RecordInput(sample_no="D", limit=3.3, scores=[3.5, 4.0, 4.0], reported_mean=3.8, test_date="2026-01-05", eval_date="2026-01-05", judgment="不符合"),
    ]


def test_record_regression_clean_and_16_mutants():
    result = run_record_regression(_records())

    assert result["clean_findings"] == 0
    assert result["mutants_total"] == 16
    assert result["mutants_detected"] == 16
    assert result["ok"] is True


def test_record_regression_is_deterministic():
    first = run_record_regression(_records())
    second = run_record_regression(_records())

    assert first == second


@pytest.mark.skipif(len(list(SAMPLES.glob("*.docx"))) != 12, reason="12 BYD drafts not mounted")
def test_draft_regression_reproduces_six_rules():
    result = run_draft_regression(SAMPLES)

    assert result["ok"] is True
    assert result["reproduced"] == 12
