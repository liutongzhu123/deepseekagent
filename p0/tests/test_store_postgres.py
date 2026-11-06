"""PostgreSQL/object-store integration tests for the P1 data layer.

These run only when ``P0_TEST_DATABASE_URL`` points at a disposable PostgreSQL
database (the CI job starts one as a service).  File mode remains the default
and is covered by the rest of the suite.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main, quote_store, report_store, standard_store, task_worker
from app.store import config, db, migration, objects, tasks

PG_URL = os.getenv("P0_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="P0_TEST_DATABASE_URL is not set (PostgreSQL integration test)"
)

STANDARD_ID = "std_20261102230000_abcdef"
QUOTE_ID = "q_20261102230000_abcdef"


def parsed_standard() -> dict:
    return {
        "meta": {"standard_no": "Q/TEST 001-2026", "name": "演示标准", "pages": 2},
        "summary": {"items": 1},
        "items": [
            {
                "clause": "5.1",
                "name": "外观",
                "requirement": "外观无缺陷",
                "method_clause": "6.1",
                "method": "目测",
                "page": 1,
                "method_page": 2,
            }
        ],
        "warnings": [],
    }


@pytest.fixture
def pg(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("P0_STORE", "postgres")
    monkeypatch.setenv("P0_DATABASE_URL", PG_URL)
    monkeypatch.setenv("P0_OBJECT_STORE", "local")
    monkeypatch.setenv("P0_OBJECT_DIR", str(data / "objects"))
    monkeypatch.setenv("P0_DATA_DIR", str(data))
    monkeypatch.setenv("P0_AUTH_USERS", str(data / "auth" / "users.json"))
    monkeypatch.setenv("P0_AUDIT_LOG", str(data / "audit" / "audit.jsonl"))
    monkeypatch.setenv("P0_STANDARD_DIR", str(data / "standards"))
    monkeypatch.setenv("P0_QUOTE_DIR", str(data / "quotes"))
    monkeypatch.setenv("P0_WEKNORA_REQUIRED", "0")
    monkeypatch.delenv("P0_WEKNORA_BASE_URL", raising=False)
    monkeypatch.delenv("P0_WEKNORA_API_KEY", raising=False)
    monkeypatch.delenv("P0_WEKNORA_KB_ID", raising=False)
    # JSON fallback paths used by the rollback assertions.
    monkeypatch.setattr(auth, "USERS_PATH", data / "auth" / "users.json")
    monkeypatch.setattr(auth, "AUDIT_PATH", data / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "AUDIT_DIR", data / "audit")
    monkeypatch.setattr(standard_store, "DATA_DIR", data / "standards")
    monkeypatch.setattr(quote_store, "QUOTE_DIR", data / "quotes")
    monkeypatch.setattr(report_store, "REPORT_DIR", data / "reports")
    monkeypatch.setattr(main, "QUOTE_DIR", data / "quotes")
    config.reset_caches()
    db.reset_schema()
    yield data
    config.reset_caches()


def test_users_and_audit_roundtrip(pg):
    user = auth.create_user("alice", "correct-horse-1", role="reviewer", projects=["P1"])
    assert user["role"] == "reviewer"
    assert auth.authenticate("alice", "correct-horse-1")["username"] == "alice"
    assert [item["username"] for item in auth.load_users()] == ["alice"]
    assert (pg / "auth" / "users.json").exists()

    auth.audit({"user": "alice", "role": "reviewer", "event": "login_success", "method": "POST", "path": "/api/v1/auth/login", "status": 200})
    auth.audit({"user": "alice", "role": "reviewer", "event": "standard_parsed", "method": "POST", "path": "/api/v1/standards/parse", "status": 200})
    assert len(auth.filter_audit(event="login_success")) == 1
    page = auth.query_audit(limit=1, offset=1)
    assert page["total"] == 2
    assert page["events"][0]["event"] == "login_success"
    assert (pg / "audit" / "audit.jsonl").read_text(encoding="utf-8").count("\n") == 2

    from app.store import audit

    audit.import_entry({"at": "2020-01-01T00:00:00+00:00", "event": "ancient"}, "seed", 1)
    assert auth.prune_audit(7) == 1
    assert all(entry.get("event") != "ancient" for entry in auth.filter_audit())


def test_standard_roundtrip_and_objects(pg):
    from app.store import standards

    record = standard_store.create(
        parsed_standard(), b"%PDF-1.4 demo standard", "demo.pdf", project="P1", created_by="alice"
    )
    assert record["id"].startswith("std_")
    assert standard_store.load(record["id"])["meta"]["standard_no"] == "Q/TEST 001-2026"
    assert standard_store.list_standards()[0]["project"] == "P1"
    assert (pg / "standards" / record["id"] / "source.pdf").read_bytes().startswith(b"%PDF")

    standard_store.set_project(record["id"], "P2")
    assert standard_store.load(record["id"])["meta"]["project"] == "P2"
    updated = standard_store.update_review(
        record["id"], [{"clause": "5.1", "requirement": "复核后的要求"}], reviewer="alice"
    )
    assert updated["revision"] == 1
    assert standards.items_for(record["id"], 1)[0]["requirement"] == "复核后的要求"

    published = standard_store.publish(record["id"])
    assert published["published"] is True
    assert standard_store.published_kbs(record["id"])[0]["chunks"]

    data = standard_store.export_bytes(record["id"], [("实检项目", [["a", "b"]], [16, 16])])
    assert data.startswith(b"PK")
    assert standards.read_export(record["id"]) == data
    assert (pg / "standards" / record["id"] / "export.xlsx").read_bytes() == data


def test_quote_roundtrip_and_download(pg):
    from app.store import quotes

    meta = {
        "quote_id": QUOTE_ID,
        "project": "P1",
        "created_by": "alice",
        "created_at": "2026-11-02T23:00:00+00:00",
        "source_name": "dvp.xlsx",
        "parse_method": "xlsx",
        "lines": 1,
        "priced_lines": 1,
    }
    quote_store.create(meta, {"CRM报价草稿.xlsx": b"crm-bytes"}, base_dir=pg / "quotes")
    assert quote_store.load(QUOTE_ID, base_dir=pg / "quotes")["project"] == "P1"
    assert quote_store.read_file(QUOTE_ID, "CRM报价草稿.xlsx", base_dir=pg / "quotes") == b"crm-bytes"
    assert quotes.file_names(QUOTE_ID) == ["CRM报价草稿.xlsx"]

    client = TestClient(main.app)
    response = client.get(f"/api/v1/quotes/{QUOTE_ID}/crm.xlsx")
    assert response.status_code == 200
    assert response.content == b"crm-bytes"
    assert "crm" in response.headers["content-disposition"].lower()
    assert client.get("/health").json()["store"] == "postgres"


def test_migration_reconciliation_and_file_rollback(pg):
    data = pg
    (data / "auth").mkdir(parents=True)
    users = [
        {
            "username": "legacy",
            "display_name": "历史用户",
            "role": "admin",
            "active": True,
            "department": "",
            "projects": ["*"],
            "auth_provider": "local",
            "session_epoch": 0,
            "failed_logins": 0,
            "locked_until": "",
            "last_login_at": "",
            "password_hash": auth.hash_password("legacy-pass-1"),
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    (data / "auth" / "users.json").write_text(
        json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8"
    )
    (data / "audit").mkdir(parents=True)
    (data / "audit" / "audit.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"at": "2026-01-01T00:00:00+00:00", "user": "legacy", "event": "login_success"}, ensure_ascii=False),
                json.dumps({"at": "2026-01-02T00:00:00+00:00", "user": "legacy", "event": "standard_parsed"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record = {
        "id": STANDARD_ID,
        "created_at": "2026-01-03T00:00:00+00:00",
        "updated_at": "2026-01-03T00:00:00+00:00",
        "revision": 0,
        "published": True,
        "published_at": "2026-01-03T00:00:00+00:00",
        "reviewer": "",
        "review_history": [],
        "source": {"file_name": "legacy.pdf", "bytes": 9},
        "meta": {"standard_no": "Q/LEGACY 1-2026", "name": "历史标准", "pages": 1, "project": "P1", "created_by": "legacy"},
        "summary": {"items": 1},
        "items": [{"clause": "5.1", "name": "外观", "requirement": "无缺陷", "method": "目测", "page": 1}],
    }
    directory = data / "standards" / STANDARD_ID
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    (directory / "source.pdf").write_bytes(b"%PDF-old")
    (directory / "kb.json").write_text(json.dumps({"standard_id": STANDARD_ID, "chunks": [{"id": "x"}]}, ensure_ascii=False), encoding="utf-8")
    (directory / "export.xlsx").write_bytes(b"old-export")
    quote_meta = {
        "quote_id": QUOTE_ID,
        "project": "P1",
        "created_by": "legacy",
        "created_at": "2026-01-04T00:00:00+00:00",
        "source_name": "old.xlsx",
        "parse_method": "xlsx",
        "lines": 1,
        "priced_lines": 1,
    }
    quote_dir = data / "quotes" / QUOTE_ID
    quote_dir.mkdir(parents=True)
    (quote_dir / "meta.json").write_text(json.dumps(quote_meta, ensure_ascii=False), encoding="utf-8")
    (quote_dir / "CRM报价草稿.xlsx").write_bytes(b"old-crm")

    report = migration.migrate(data)
    assert report["imported"]["errors"] == []
    assert report["imported"]["users"] == 1
    assert report["imported"]["standards"] == 1
    assert report["imported"]["quotes"] == 1
    assert report["imported"]["audit_events"] == 2
    verification = migration.verify(data)
    assert verification["ok"], verification["diffs"]

    # Live writes after the migration are mirrored, so a rollback is complete.
    auth.create_user("after-migration", "after-pass-1", role="viewer")
    standard_store.set_project(STANDARD_ID, "P2")
    exported = migration.export_to_files(data / "rollback")
    assert exported["users"] == 2
    assert exported["standards"] == 1
    exported_names = {u["username"] for u in json.loads((data / "rollback" / "auth" / "users.json").read_text(encoding="utf-8"))["users"]}
    assert exported_names == {"legacy", "after-migration"}

    # File mode still reads the exported tree.
    import app.standard_store as ss
    from app import quote_store as qs

    old_store = os.environ.get("P0_STORE")
    os.environ["P0_STORE"] = "json"
    try:
        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        mp.setattr(auth, "USERS_PATH", data / "rollback" / "auth" / "users.json")
        mp.setattr(auth, "AUDIT_PATH", data / "rollback" / "audit" / "audit.jsonl")
        mp.setattr(ss, "DATA_DIR", data / "rollback" / "standards")
        mp.setattr(qs, "QUOTE_DIR", data / "rollback" / "quotes")
        assert auth.find_user("after-migration")["role"] == "viewer"
        assert ss.load(STANDARD_ID)["meta"]["project"] == "P2"
        assert qs.load(QUOTE_ID, base_dir=data / "rollback" / "quotes")["project"] == "P1"
        assert auth.filter_audit(event="login_success")[0]["user"] == "legacy"
    finally:
        mp.undo()
        if old_store is None:
            os.environ.pop("P0_STORE", None)
        else:
            os.environ["P0_STORE"] = old_store
        config.reset_caches()


def test_report_audit_roundtrip(pg):
    report_id = "rp_20261103010101_abcdef"
    batch_id = "rb_20261103010101_abcdef"
    result = {
        "file": "demo.docx",
        "template": "BYD-odor",
        "summary": {"total": 2, "pass": 1, "warn": 0, "fail": 1, "skip": 0},
        "findings": [
            {"rule_id": "R01", "title": "编号一致性", "status": "pass", "severity": "high", "message": "ok"},
            {"rule_id": "R02", "title": "均值一致", "status": "fail", "severity": "high", "message": "均值不符"},
        ],
    }
    record = {
        "id": report_id,
        "batch_id": batch_id,
        "project": "P1",
        "created_by": "alice",
        "created_at": "2026-11-03T01:01:01+00:00",
        "file_name": "demo.docx",
        "file_size": 9,
        "file_sha256": "0" * 64,
        "template": "BYD-odor",
        "summary": dict(result["summary"]),
    }
    report_store.create(record, result, b"fake-docx")

    loaded = report_store.load(report_id)
    assert loaded["result"]["findings"] == result["findings"]
    assert loaded["project"] == "P1"
    assert report_store.read_source(report_id) == b"fake-docx"

    rows = report_store.list_reports()
    assert [row["id"] for row in rows] == [report_id]
    assert rows[0]["batch_id"] == batch_id
    assert rows[0]["summary"]["fail"] == 1
    assert (pg / "reports" / report_id / "source.docx").read_bytes() == b"fake-docx"


def test_report_prune_removes_old_audits(pg):
    old_id = "rp_20200101010101_abcdef"
    new_id = "rp_20261103010101_abcdef"
    result = {"file": "demo.docx", "template": "BYD-odor", "findings": []}
    for report_id, created_at in (
        (old_id, "2020-01-01T00:00:00+00:00"),
        (new_id, "2026-11-03T01:01:01+00:00"),
    ):
        report_store.create(
            {
                "id": report_id,
                "batch_id": "",
                "project": "P1",
                "created_by": "alice",
                "created_at": created_at,
                "file_name": "demo.docx",
                "file_size": 9,
                "file_sha256": "0" * 64,
                "template": "BYD-odor",
                "summary": {},
            },
            result,
            b"fake-docx",
        )

    cutoff = datetime(2021, 1, 1, tzinfo=timezone.utc)
    assert report_store.prunable_ids(cutoff) == [old_id]
    assert report_store.prune_before(cutoff) == 1

    with pytest.raises(KeyError):
        report_store.load(old_id)
    assert report_store.read_source(new_id) == b"fake-docx"
    assert not (pg / "reports" / old_id).exists()
    assert (pg / "reports" / new_id).exists()


def test_report_batch_queue_roundtrip(pg, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("P0_TASKS_ENABLED", "1")
    batch_id = "rb_20261106100000_abcdef"
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
        method="POST",
        url=SimpleNamespace(path="/api/v1/reports/batch"),
    )

    response = main._enqueue_report_batch(
        request,
        {"username": "alice"},
        entries=[("a.docx", b"docx-a"), ("b.docx", b"docx-b")],
        project="P1",
        batch_id=batch_id,
    )

    assert response.status_code == 202
    task_id = json.loads(response.body)["task_id"]
    queued = tasks.get(task_id)
    assert queued["kind"] == "report_batch"
    assert queued["status"] == "queued"
    first_file = queued["payload"]["files"][1]["file_id"]
    with db.engine().connect() as conn:
        assert objects.read_file(conn, first_file)[1] == b"docx-b"

    monkeypatch.setattr(
        main,
        "_audit_report_bytes",
        lambda data, name, **kwargs: {
            "report_id": f"rp_{name}",
            "summary": {"total": 1, "pass": 1, "warn": 0, "fail": 0, "skip": 0},
        },
    )
    monkeypatch.setattr(task_worker.auth, "audit", lambda entry: None)
    result = task_worker._handle_report_batch(queued, lambda stage, value: None)

    assert result["batch_id"] == batch_id
    assert result["summary"]["stored"] == 2
    assert result["summary"]["pass"] == 2


def test_standard_versions_unpublish_and_rollback(pg):
    record = standard_store.create(
        {
            "meta": {"standard_no": "Q/TEST 002-2026", "name": "治理演示标准", "pages": 1},
            "summary": {"items": 1},
            "items": [
                {
                    "clause": "5.1",
                    "name": "外观",
                    "requirement": "外观无缺陷",
                    "method_clause": "6.1",
                    "method": "目测",
                    "page": 1,
                    "method_page": 1,
                }
            ],
            "warnings": [],
        },
        b"%PDF-1.4 demo",
        "demo.pdf",
        project="P1",
        created_by="alice",
    )
    standard_store.update_review(
        record["id"],
        [{"clause": "5.1", "requirement": "回滚前的新要求", "method": "目测"}],
        reviewer="bob",
    )
    versions = standard_store.list_versions(record["id"])
    assert [item["version"] for item in versions] == [1, 0]
    assert versions[0]["current"] is True

    rolled = standard_store.rollback(record["id"], 0, "bob")
    assert rolled["revision"] == 2
    assert rolled["items"][0]["requirement"] == "外观无缺陷"

    standard_store.publish(record["id"])
    assert standard_store.load(record["id"])["published"] is True
    standard_store.unpublish(record["id"], "bob")
    assert standard_store.load(record["id"])["published"] is False
    assert standard_store.published_kbs(record["id"]) == []


def test_quote_history_roundtrip(pg):
    quote_id = "q_20261103020202_abcdef"
    meta = {
        "quote_id": quote_id,
        "project": "P1",
        "created_by": "alice",
        "created_at": "2026-11-03T02:02:02+00:00",
        "source_name": "demo.xlsx",
        "parse_method": "Excel 表格",
        "lines": 2,
        "priced_lines": 2,
        "has_sales_quote": False,
        "crm": {"enabled": False, "mode": "off", "reconciled": True, "matched": 0},
    }
    quote_store.create(meta, {"CRM报价草稿.xlsx": b"PK"})
    rows = quote_store.list_quotes()
    assert [row["quote_id"] for row in rows] == [quote_id]
    assert rows[0]["project"] == "P1"
    assert rows[0]["has_sales_quote"] is False


def test_qa_feedback_roundtrip(pg):
    from app import feedback_store

    record = {
        "id": "fb_20261103030303_abcdef",
        "at": "2026-11-03T03:03:03+00:00",
        "user": "alice",
        "role": "reviewer",
        "rating": "not_helpful",
        "reason": "引用不对",
        "comment": "条款页码对不上",
        "question": "气味等级要求？",
        "answer_excerpt": "气味等级≤3.0级",
        "standard_id": STANDARD_ID,
        "clause": "5.1",
        "retrieval_mode": "keyword-fallback",
        "citations": [{"standard_id": STANDARD_ID, "clause": "5.1"}],
        "project": "P1",
    }
    feedback_store.create(record)
    rows = feedback_store.list_feedback()
    assert [row["id"] for row in rows] == [record["id"]]
    assert rows[0]["rating"] == "not_helpful"
    assert rows[0]["citations"][0]["clause"] == "5.1"


def test_task_prune_removes_only_old_finished(pg):
    from datetime import timedelta

    from sqlalchemy import update

    from app.store import tasks

    old_task = tasks.create("standard_parse", {"file_name": "old.pdf"}, created_by="alice", project="")
    tasks.finish(old_task["id"], "succeeded", result={"items": 1})
    new_task = tasks.create("standard_parse", {"file_name": "new.pdf"}, created_by="alice", project="")
    tasks.finish(new_task["id"], "succeeded", result={"items": 1})
    with db.engine().begin() as conn:
        conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == old_task["id"])
            .values(finished_at=db.utcnow() - timedelta(days=40))
        )

    assert tasks.count_prunable(days=30) == 1
    assert tasks.prune(days=30) == 1
    with pytest.raises(KeyError):
        tasks.get(old_task["id"])
    assert tasks.get(new_task["id"])["status"] == "succeeded"
    assert tasks.prune(days=30, statuses=("queued",)) == 0
