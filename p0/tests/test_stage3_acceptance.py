"""Stage-3 acceptance harness: manifest validation, metric math and gates.

These tests never need customer samples: files are simulated with tmp_path and
the audit result is injected through ``Outcome``.  The real 20–30 reports and
their gold standard stay outside the repository (D3).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.docx_parser import ReportData
from eval import stage3_acceptance as s3

HEADER = list(s3.MANIFEST_COLUMNS) + ["pages", "redacted"]


def _write_manifest(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in HEADER})
    return path


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "data_id": "D3-001",
        "dataset": "D3",
        "class": "A",
        "masked_id": "S3-OTS-001",
        "file_name": "ots-001.docx",
        "expected_judgment": "不符合",
        "expected_fail_rules": "R01;R02",
        "case_kind": "defect",
        "pages": "5",
        "redacted": "yes",
    }
    base.update(overrides)
    return base


def _case(**overrides: object) -> s3.Case:
    base = {
        "data_id": "D3-001",
        "report_class": "A",
        "masked_id": "S3-OTS-001",
        "file_name": "ots-001.docx",
        "expected_judgment": "不符合",
        "expected_fail": frozenset({"R01", "R02"}),
        "case_kind": "defect",
    }
    base.update(overrides)
    return s3.Case(**base)  # type: ignore[arg-type]


def _outcome(
    case: s3.Case,
    actual: set[str] | frozenset[str] | None = None,
    *,
    status: str = "ok",
    template: str = "BYD-odor",
    variant: str = "OTS",
    gaps: tuple[str, ...] = (),
    seconds: float = 1.0,
) -> s3.Outcome:
    return s3.Outcome(
        case=case,
        status=status,
        actual_fail=frozenset(actual if actual is not None else case.expected_fail),
        template=template,
        variant=variant,
        judgment=case.expected_judgment,
        seconds=seconds,
        gaps=gaps,
    )


def _twenty_cases() -> list[s3.Case]:
    """20 acceptance samples: 15 defects + 5 clean controls across A/B/C."""
    cases: list[s3.Case] = []
    for index in range(8):
        cases.append(_case(data_id=f"A-{index}", report_class="A", masked_id=f"S3-OTS-{index:03d}",
                           file_name=f"a-{index}.docx",
                           expected_fail=frozenset() if index < 2 else frozenset({"R01", "R02"}),
                           case_kind="clean" if index < 2 else "defect"))
    for index in range(7):
        cases.append(_case(data_id=f"B-{index}", report_class="B", masked_id=f"S3-MOBAN-{index:03d}",
                           file_name=f"b-{index}.docx",
                           expected_fail=frozenset() if index < 2 else frozenset({"R02"}),
                           case_kind="clean" if index < 2 else "defect"))
    for index in range(5):
        cases.append(_case(data_id=f"C-{index}", report_class="C", masked_id=f"S3-XG-{index:03d}",
                           file_name=f"c-{index}.docx",
                           expected_fail=frozenset() if index < 1 else frozenset({"R01"}),
                           case_kind="clean" if index < 1 else "defect"))
    return cases


def _outcomes_for(cases: list[s3.Case]) -> list[s3.Outcome]:
    outcomes = []
    for case in cases:
        if case.report_class == "C":
            outcomes.append(_outcome(case, template="XG", variant=""))
        else:
            outcomes.append(_outcome(case, variant="OTS" if case.report_class == "A" else "摸底"))
    return outcomes


def test_load_manifest_keeps_only_d3_and_parses_rules(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "manifest.csv",
        [
            _row(data_id="D1-001", dataset="D1", **{"class": "C"}, case_kind="template", expected_fail_rules=""),
            _row(data_id="D3-001", expected_fail_rules="R01；R02"),
            _row(data_id="D3-002", masked_id="S3-OTS-002", file_name="ots-002.docx",
                 expected_fail_rules="", case_kind="clean"),
        ],
    )
    cases = s3.load_manifest(manifest)

    assert [case.data_id for case in cases] == ["D3-001", "D3-002"]
    assert cases[0].expected_fail == frozenset({"R01", "R02"})
    assert cases[0].pages == 5
    assert cases[1].case_kind == "clean"
    assert cases[1].expected_fail == frozenset()


def test_load_manifest_rejects_clean_with_expected_fail(tmp_path: Path):
    manifest = _write_manifest(tmp_path / "manifest.csv", [_row(case_kind="clean")])

    try:
        s3.load_manifest(manifest)
    except s3.ManifestError as exc:
        assert "clean" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure path
        raise AssertionError("clean row with expected fail rules must be rejected")


def test_load_manifest_rejects_missing_column(tmp_path: Path):
    path = tmp_path / "manifest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["data_id", "dataset"])
        writer.writerow(["D3-001", "D3"])

    try:
        s3.load_manifest(path)
    except s3.ManifestError as exc:
        assert "缺少列" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure path
        raise AssertionError("a manifest without gold-standard columns must be rejected")


def test_metrics_and_gate_pass_at_twenty_samples():
    cases = _twenty_cases()
    metrics = s3.compute_metrics(cases, _outcomes_for(cases))
    gate = s3.evaluate_gate(metrics)

    assert metrics["samples"] == 20
    assert metrics["correct"] == 20
    assert metrics["clean_fp"] == 0
    assert metrics["template_ok"] == 20
    assert gate["verdict"] == "通过"


def test_clean_report_false_positive_fails_gate():
    cases = [_case(data_id="clean-1", file_name="clean.docx", expected_fail=frozenset(), case_kind="clean")]
    outcome = _outcome(cases[0], {"R09"})
    metrics = s3.compute_metrics(cases, [outcome])
    gate = s3.evaluate_gate(metrics)

    assert metrics["clean_fp"] == 1
    assert metrics["fp"] == 1
    assert gate["verdict"] == "blocked"  # too few samples, but the FP is recorded


def test_too_few_samples_is_blocked():
    cases = _twenty_cases()[:10]
    metrics = s3.compute_metrics(cases, _outcomes_for(cases))
    gate = s3.evaluate_gate(metrics)

    assert metrics["samples"] == 10
    assert gate["verdict"] == "blocked"


def test_missing_file_is_blocked_and_listed(tmp_path: Path):
    case = _case()
    outcome = s3.run_case(case, tmp_path)
    metrics = s3.compute_metrics([case], [outcome])
    gate = s3.evaluate_gate(metrics)

    assert outcome.status == "missing"
    assert metrics["missing"] == ["D3-001"]
    assert metrics["missing_files"][0]["file_name"] == "ots-001.docx"
    assert gate["verdict"] == "blocked"


def test_callable_retry_and_gap_detection():
    data = ReportData(file_name="a.docx", cover_number="", judgment="")
    gaps = s3._data_gaps(data, {"R01": "pass", "R04": "pass", "R06": "pass", "P06": "pass"})

    assert any("缺数据沉默通过" in item for item in gaps)
    assert any("P06" in item for item in gaps)


def test_serialisation_round_trip(tmp_path: Path):
    cases = _twenty_cases()
    outcomes = _outcomes_for(cases)
    metrics = s3.compute_metrics(cases, outcomes)
    gate = s3.evaluate_gate(metrics)
    payload = {"manifest": "m.csv", **metrics, **gate}
    path = tmp_path / "out.json"

    s3._write_json(path, payload)
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert loaded["verdict"] == "通过"
    assert loaded["samples"] == 20


def test_render_markdown_marks_blocked():
    cases = _twenty_cases()[:3]
    outcomes = _outcomes_for(cases)
    metrics = s3.compute_metrics(cases, outcomes)
    gate = s3.evaluate_gate(metrics)
    markdown = s3.render_markdown(
        metrics, gate, cases, outcomes, manifest="m.csv", samples_dir="samples", now="2026-11-20 10:00"
    )

    assert "blocked" in markdown
    assert "缺文件" in markdown
    assert "语义" in markdown
    assert "D1（XG 样例）" in markdown
