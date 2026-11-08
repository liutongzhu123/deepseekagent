"""PostgreSQL task-queue and async-worker integration tests."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main, standard_store, task_worker
from app.store import config, db, tasks

PG_URL = os.getenv("P0_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="P0_TEST_DATABASE_URL is not set (PostgreSQL integration test)"
)


def simple_pdf(text: str = "Q/TEST 001-2026 demo standard") -> bytes:
    """A one-page PDF with a real text layer (no external PDF library needed)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def task_env(monkeypatch, tmp_path):
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
    monkeypatch.setenv("P0_TASKS_ENABLED", "1")
    monkeypatch.setenv("P0_TASKS_PARSE_ASYNC", "1")
    monkeypatch.setenv("P0_TASKS_PUBLISH_ASYNC", "1")
    monkeypatch.setenv("P0_TASKS_ASYNC_MIN_MB", "0")
    monkeypatch.setenv("P0_TASKS_POLL_SECONDS", "0.05")
    monkeypatch.setenv("P0_TASKS_LEASE_SECONDS", "60")
    config.reset_caches()
    db.reset_schema()
    yield data
    task_worker.stop()
    config.reset_caches()


def wait_for(client: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/v1/tasks/{task_id}").json()
        if payload.get("status") in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"task did not finish: {payload if 'payload' in locals() else task_id}")


def test_async_parse_returns_202_and_health_stays_responsive(task_env):
    with TestClient(main.app) as client:
        response = client.post(
            "/api/v1/standards/parse",
            files={"file": ("demo.pdf", simple_pdf(), "application/pdf")},
            data={"project": "P1"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        started = time.perf_counter()
        health = client.get("/health")
        elapsed = time.perf_counter() - started
        assert health.status_code == 200
        assert elapsed < 0.5
        assert health.json()["tasks"] == "worker"
        finished = wait_for(client, task_id)
        assert finished["status"] == "succeeded", finished
        assert finished["file_name"] == "demo.pdf"
        assert finished["created_by"] == "test"
        standard_id = finished["result"]["standard_id"]
        assert client.get(f"/api/v1/standards/{standard_id}").status_code == 200
        loaded = standard_store.load(standard_id)
        assert loaded["meta"]["project"] == "P1"
        assert loaded["meta"]["created_by"] == "test"
        assert (task_env / "standards" / standard_id / "source.pdf").exists()


def test_async_publish_returns_task_and_updates_standard(task_env):
    parsed = {
        "meta": {"standard_no": "Q/TEST 002-2026", "name": "演示发布", "pages": 1},
        "summary": {"items": 1},
        "items": [
            {"clause": "5.1", "name": "外观", "requirement": "无缺陷", "method_clause": "6.1", "method": "目测", "page": 1, "method_page": 1}
        ],
    }
    record = standard_store.create(parsed, b"%PDF-1.4 demo", "demo.pdf", project="P1", created_by="admin")
    standard_store.update_review(
        record["id"], [{"clause": "5.1", "reviewed": True}], reviewer="复核人"
    )
    with TestClient(main.app) as client:
        response = client.post(f"/api/v1/standards/{record['id']}/publish")
        assert response.status_code == 202, response.text
        finished = wait_for(client, response.json()["task_id"])
        assert finished["status"] == "succeeded", finished
        assert finished["result"]["chunks"] >= 1
        assert standard_store.load(record["id"])["published"] is True


def test_cancel_retry_and_worker_restart_recovery(task_env):
    started = threading.Event()
    release = threading.Event()
    attempts = {"value": 0}

    def slow_handler(task, report):
        attempts["value"] += 1
        report("ocr", 20)
        started.set()
        release.wait(5)
        report("parse", 60)
        return {"ok": True}

    worker = task_worker.TaskWorker(handlers={"standard_parse": slow_handler}, concurrency=1)
    task = tasks.create("standard_parse", {"file_name": "fake.pdf"}, created_by="admin")
    worker.start()
    try:
        assert started.wait(5)
        cancelled = tasks.cancel(task["id"])
        assert cancelled["cancel_requested"] is True
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and tasks.get(task["id"])["status"] == "running":
            time.sleep(0.05)
        assert tasks.get(task["id"])["status"] == "cancelled"

        retried = tasks.retry(task["id"])
        assert retried["status"] == "queued"
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and tasks.get(task["id"])["status"] != "succeeded":
            time.sleep(0.05)
        assert tasks.get(task["id"])["status"] == "succeeded"
    finally:
        release.set()
        worker.stop()


def test_concurrency_claim_and_stale_recovery(task_env):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def handler(task, report):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.25)
        with lock:
            active["now"] -= 1
        return {"ok": True}

    worker = task_worker.TaskWorker(handlers={"standard_parse": handler}, concurrency=2)
    first = tasks.create("standard_parse", {"file_name": "a.pdf"}, created_by="admin")
    second = tasks.create("standard_parse", {"file_name": "b.pdf"}, created_by="admin")
    worker.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if tasks.get(first["id"])["status"] == tasks.get(second["id"])["status"] == "succeeded":
                break
            time.sleep(0.05)
        assert tasks.get(first["id"])["status"] == "succeeded"
        assert tasks.get(second["id"])["status"] == "succeeded"
        assert active["max"] == 2
    finally:
        worker.stop()

    stale = tasks.create("standard_parse", {"file_name": "stale.pdf"}, created_by="admin")
    claimed = tasks.claim_next("dead-worker")
    assert claimed and claimed["id"] == stale["id"]
    with db.engine().begin() as conn:
        from sqlalchemy import update

        conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == stale["id"])
            .values(lease_until=db.utcnow())
        )
    assert tasks.recover_stale() >= 1
    assert tasks.get(stale["id"])["status"] == "queued"


def test_health_stays_fast_while_slow_task_runs(task_env):
    started = threading.Event()
    release = threading.Event()

    def slow_handler(task, report):
        report("ocr", 20)
        started.set()
        release.wait(5)
        return {"ok": True}

    worker = task_worker.TaskWorker(handlers={"standard_parse": slow_handler}, concurrency=1)
    task = tasks.create("standard_parse", {"file_name": "slow.pdf"}, created_by="admin")
    worker.start()
    try:
        assert started.wait(5)
        client = TestClient(main.app)
        timings = []
        for _ in range(5):
            begin = time.perf_counter()
            response = client.get("/health")
            timings.append(time.perf_counter() - begin)
            assert response.status_code == 200
        assert max(timings) < 0.5, timings
        assert tasks.get(task["id"])["status"] == "running"
    finally:
        release.set()
        worker.stop()


def test_failed_task_can_be_retried(task_env):
    attempts = {"value": 0}

    def flaky_handler(task, report):
        attempts["value"] += 1
        if attempts["value"] == 1:
            raise RuntimeError("temporary OCR failure")
        return {"ok": True}

    worker = task_worker.TaskWorker(handlers={"standard_parse": flaky_handler}, concurrency=1)
    task = tasks.create("standard_parse", {"file_name": "flaky.pdf"}, created_by="admin")
    worker.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and tasks.get(task["id"])["status"] != "failed":
            time.sleep(0.05)
        failed = tasks.get(task["id"])
        assert failed["status"] == "failed"
        assert "temporary OCR failure" in failed["error"]
        tasks.retry(task["id"])
        deadline = time.time() + 5
        while time.time() < deadline and tasks.get(task["id"])["status"] != "succeeded":
            time.sleep(0.05)
        assert tasks.get(task["id"])["status"] == "succeeded"
    finally:
        worker.stop()


def test_graceful_stop_requeues_running_task(task_env):
    started = threading.Event()
    release = threading.Event()

    def handler(task, report):
        started.set()
        release.wait(5)
        report("parse", 60)
        return {"ok": True}

    worker = task_worker.TaskWorker(handlers={"standard_parse": handler}, concurrency=1)
    task = tasks.create("standard_parse", {"file_name": "stop.pdf"}, created_by="admin")
    worker.start()
    assert started.wait(5)
    stopper = threading.Thread(target=worker.stop, kwargs={"timeout": 5})
    stopper.start()
    time.sleep(0.2)
    release.set()
    stopper.join(timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline and tasks.get(task["id"])["status"] != "queued":
        time.sleep(0.05)
    assert tasks.get(task["id"])["status"] == "queued"
