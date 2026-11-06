"""Report review retention: prune old audits (T303).

File-mode tests create report records with controlled ``created_at`` values so
the cutoff behaviour is deterministic; the CLI test checks dry-run first and
that ``--apply`` only removes the old record.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import auth, report_store
from scripts import prune_reports

OLD_ID = "rp_20200101010101_abcdef"
NEW_ID = "rp_20261105010101_abcdef"


def _record(report_id: str, created_at: str) -> dict:
    return {
        "id": report_id,
        "batch_id": "",
        "project": "P1",
        "created_by": "alice",
        "created_at": created_at,
        "file_name": f"{report_id}.docx",
        "file_size": 4,
        "file_sha256": "0" * 64,
        "template": "BYD-odor",
        "summary": {"total": 1, "fail": 1, "pass": 0, "warn": 0, "skip": 0},
    }


def _create(base: Path, report_id: str, created_at: str) -> None:
    report_store.create(_record(report_id, created_at), {"findings": []}, b"docx", base_dir=base)


def _cutoff() -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_prunable_ids_respects_cutoff_and_keeps_unknown_timestamps(tmp_path: Path):
    _create(tmp_path, OLD_ID, "2026-01-01T00:00:00+00:00")
    _create(tmp_path, NEW_ID, "2026-11-05T00:00:00+00:00")
    unknown = tmp_path / "rp_20260101010101_aaaaaa"
    unknown.mkdir()
    (unknown / "meta.json").write_text('{"id": "rp_20260101010101_aaaaaa"}', encoding="utf-8")

    assert report_store.prunable_ids(_cutoff(), tmp_path) == [OLD_ID]


def test_prune_before_removes_old_record_and_source(tmp_path: Path):
    _create(tmp_path, OLD_ID, "2026-01-01T00:00:00+00:00")
    _create(tmp_path, NEW_ID, "2026-11-05T00:00:00+00:00")

    removed = report_store.prune_before(_cutoff(), tmp_path)

    assert removed == 1
    assert not (tmp_path / OLD_ID).exists()
    assert (tmp_path / NEW_ID / "source.docx").read_bytes() == b"docx"
    assert [row["id"] for row in report_store.list_reports(tmp_path)] == [NEW_ID]


def test_prune_reports_script_dry_run_then_apply(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(report_store, "REPORT_DIR", tmp_path / "reports")
    now = datetime.now(timezone.utc)
    _create(tmp_path / "reports", OLD_ID, (now - timedelta(days=90)).isoformat(timespec="seconds"))
    _create(tmp_path / "reports", NEW_ID, (now - timedelta(days=1)).isoformat(timespec="seconds"))
    events: list[dict] = []
    monkeypatch.setattr(auth, "audit", events.append)

    assert prune_reports.main(["--days", "30"]) == 0
    assert "dry-run: 1 report(s)" in capsys.readouterr().out
    assert (tmp_path / "reports" / OLD_ID).exists()

    assert prune_reports.main(["--days", "30", "--apply"]) == 0
    assert "removed 1 report(s)" in capsys.readouterr().out
    assert not (tmp_path / "reports" / OLD_ID).exists()
    assert (tmp_path / "reports" / NEW_ID / "source.docx").exists()
    assert [event["event"] for event in events] == ["reports_pruned"]
    assert events[0]["rows"] == 1
    assert events[0]["days"] == 30


def test_prune_reports_script_rejects_non_positive_days(capsys):
    assert prune_reports.main(["--days", "0", "--apply"]) == 2
    assert "--days must be >= 1" in capsys.readouterr().err
