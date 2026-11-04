"""Stage-1 regression: 12 BYD drafts + 4 real records + 16 mutants.

The customer samples are not committed.  Run locally with::

    python -m eval.stage1_regression \
      --samples ../samples/byd \
      --records "/path/to/(散发四项报告 比亚迪)草本出具及审核视图.xlsx" \
      --out eval/stage1-report.json

Records and mutants do not contain customer text in the report; only rule ids,
counts and sample hashes are written.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.docx_parser import parse_docx  # noqa: E402
from app.rules import RecordInput, audit_report, validate_records  # noqa: E402

DRAFT_FAIL_RULES = ("R01", "R02", "R03", "R04", "R09", "R10")
MUTANT_RULES = ("mean", "judgment", "date", "duplicate")


def _date(value: object) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value or "")[:10]


def _number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("≤", "").replace("<", "").strip())
    except ValueError:
        return None


def load_records(path: str | Path) -> list[RecordInput]:
    """Read the four POC records from the customer review workbook."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(workbook.active.iter_rows(values_only=True))
    finally:
        workbook.close()
    records: list[RecordInput] = []
    for row in rows[1:5]:
        scores = [_number(row[22]), _number(row[23]), _number(row[24])]
        if any(score is None for score in scores):
            raise ValueError("record row has missing scores")
        records.append(
            RecordInput(
                task_no=str(row[0] or "").strip(),
                report_no=str(row[1] or "").strip(),
                sample_no=str(row[4] or "").strip(),
                limit=_number(row[6]),
                scores=[float(score) for score in scores if score is not None],
                reported_mean=_number(row[21]),
                test_date=_date(row[14]),
                eval_date=_date(row[20]),
                judgment="不符合",
            )
        )
    if len(records) != 4:
        raise ValueError(f"expected 4 records, got {len(records)}")
    return records


def _codes(result: dict) -> set[str]:
    return {str(item.get("rule_id")) for item in result.get("findings", [])}


def run_record_regression(records: list[RecordInput]) -> dict:
    """Clean records must have 0 findings; 4 mutations x 4 records = 16/16."""
    clean = validate_records(records)
    cases: list[tuple[str, str, list[RecordInput], str]] = []
    for record in records:
        mean = dataclasses.replace(record, reported_mean=round((record.reported_mean or 0) + 0.1, 1))
        cases.append(("mean", record.sample_no, [mean], "REC-01"))
        flipped = "符合" if record.judgment != "符合" else "不符合"
        cases.append(("judgment", record.sample_no, [dataclasses.replace(record, judgment=flipped)], "REC-03"))
        test_day = date.fromisoformat(record.test_date)
        cases.append(
            ("date", record.sample_no, [dataclasses.replace(record, eval_date=(test_day - timedelta(days=1)).isoformat())], "REC-02")
        )
        cases.append(("duplicate", record.sample_no, [*records, record], "REC-04"))
    detected = 0
    misses: list[dict] = []
    for kind, sample, batch, expected in cases:
        codes = _codes(validate_records(batch))
        if expected in codes:
            detected += 1
        else:
            misses.append({"kind": kind, "sample": hashlib.sha256(sample.encode()).hexdigest()[:12], "expected": expected})
    return {
        "clean_findings": clean["summary"]["fail"],
        "mutants_total": len(cases),
        "mutants_detected": detected,
        "ok": clean["summary"]["fail"] == 0 and detected == len(cases),
        "misses": misses,
    }


def run_draft_regression(samples_dir: str | Path) -> dict:
    files = sorted(Path(samples_dir).glob("*.docx"))
    if len(files) != 12:
        return {"files": len(files), "ok": False, "reason": "expected 12 BYD drafts", "per_file": []}
    per_file: list[dict] = []
    reproduced = 0
    for path in files:
        findings = audit_report(parse_docx(path))["findings"]
        statuses = {item["rule_id"]: item["status"] for item in findings}
        reproduced_rules = [rule for rule in DRAFT_FAIL_RULES if statuses.get(rule) == "fail"]
        if len(reproduced_rules) == len(DRAFT_FAIL_RULES):
            reproduced += 1
        per_file.append({"file": hashlib.sha256(path.name.encode()).hexdigest()[:12], "rules_failed": reproduced_rules})
    return {"files": len(files), "reproduced": reproduced, "ok": reproduced == 12, "per_file": per_file}


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 回归：12 草稿 + 4 记录 + 16 变异")
    parser.add_argument("--samples", default=os.getenv("P0_REPORT_SAMPLES", str(ROOT.parent / "samples" / "byd")))
    parser.add_argument("--records", required=True)
    parser.add_argument("--out", default=str(ROOT / "eval" / "stage1-report.json"))
    args = parser.parse_args()

    drafts = run_draft_regression(args.samples)
    records = run_record_regression(load_records(args.records))
    report = {
        "drafts": {key: value for key, value in drafts.items() if key != "per_file"},
        "records": records,
        "draft_details": drafts.get("per_file", []),
        "ok": drafts["ok"] and records["ok"],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("drafts", "records", "ok")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
