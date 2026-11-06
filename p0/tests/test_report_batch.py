"""Report batch aggregation and the async worker handler (no customer data)."""
from __future__ import annotations

from fastapi import HTTPException

from app import auth, main, report_batch, task_worker
from app.store import tasks


def _summary(fail: int = 1, passed: int = 2) -> dict:
    return {"total": fail + passed, "fail": fail, "pass": passed, "warn": 0, "skip": 0}


def test_run_batch_aggregates_good_and_failed_files():
    def fake_audit(data, name, *, user, project, batch_id, inputs=None):
        if name == "bad.docx":
            raise ValueError("坏文件")
        return {"report_id": f"rp_{name}", "summary": _summary()}

    result = report_batch.run_batch(
        [("ok.docx", b"x"), ("bad.docx", b"y")],
        user={"username": "alice"},
        project="P1",
        batch_id="rb_1",
        audit=fake_audit,
    )

    assert [item["ok"] for item in result["items"]] == [True, False]
    assert result["summary"] == {
        "total": 2, "stored": 1, "failed": 1, "pass": 2, "warn": 0, "fail": 1, "skip": 0,
    }
    assert "坏文件" in result["items"][1]["error"]
def test_run_batch_reports_read_failure_with_http_detail():
    result = report_batch.run_batch(
        [("bad.docx", HTTPException(status_code=400, detail="only .docx"))],
        user={},
        project="",
        batch_id="rb_1",
        audit=lambda *args, **kwargs: {},
    )

    assert result["items"][0]["error"] == "only .docx"
    assert result["summary"]["failed"] == 1
def test_worker_handler_reads_files_and_reports_progress(monkeypatch):
    monkeypatch.setattr(
        main,
        "_audit_report_bytes",
        lambda data, name, **kwargs: {"report_id": f"rp_{name}", "summary": _summary(fail=0, passed=1)},
    )
    events: list[dict] = []
    monkeypatch.setattr(auth, "audit", events.append)
    task = {
        "id": "task_20261106100000_abcdef",
        "kind": "report_batch",
        "created_by": "alice",
        "payload": {
            "batch_id": "rb_20261106100000_abcdef",
            "project": "P1",
            "created_by": "alice",
            "files": [
                {"file_id": "f1", "file_name": "a.docx"},
                {"file_id": "f2", "file_name": "b.docx"},
            ],
        },
    }
    progress: list[tuple[str, int]] = []

    result = task_worker._handle_report_batch(
        task,
        lambda stage, value: progress.append((stage, value)),
        read=lambda file_id, name: (name, b"data"),
    )

    assert result["summary"]["stored"] == 2
    assert [item["file"] for item in result["items"]] == ["a.docx", "b.docx"]
    assert progress[0] == ("load", 5)
    assert progress[-1] == ("save", 95)
    assert events[0]["event"] == "report_batch_audited"
    assert events[0]["stored"] == 2
def test_report_batch_kind_is_registered():
    assert "report_batch" in tasks.KINDS
    assert "report_batch" in task_worker.DEFAULT_HANDLERS
