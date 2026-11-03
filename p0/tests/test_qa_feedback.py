"""Q&A feedback: capture, admin summary and permission checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, feedback_store, main, standard_store


def _standard(project: str = "P1") -> dict:
    return standard_store.create(
        {
            "meta": {"standard_no": "Q/TEST 003-2026", "name": "问答演示标准", "pages": 1},
            "summary": {"items": 1},
            "items": [
                {
                    "clause": "5.1",
                    "name": "气味等级",
                    "requirement": "气味等级≤3.0级",
                    "method_clause": "6.1",
                    "method": "按方案2评价",
                    "page": 1,
                    "method_page": 1,
                }
            ],
            "warnings": [],
        },
        b"%PDF-1.4 demo",
        "demo.pdf",
        project=project,
        created_by="alice",
    )


@pytest.fixture()
def feedback_env(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback_store, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")
    return SimpleNamespace(client=TestClient(main.app), tmp=tmp_path)


@pytest.fixture()
def auth_env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(feedback_store, "FEEDBACK_DIR", tmp_path / "feedback")
    patch = SimpleNamespace(tmp=tmp_path)
    return patch


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def test_feedback_is_stored_and_summarised(feedback_env):
    standard = _standard()
    response = feedback_env.client.post(
        "/api/v1/standards/ask/feedback",
        json={
            "question": "气味等级要求是什么？",
            "answer": "【判定要求】第 1 页：气味等级≤3.0级",
            "rating": "helpful",
            "standard_id": standard["id"],
            "clause": "5.1",
            "retrieval_mode": "keyword-fallback",
            "citations": [{"standard_id": standard["id"], "clause": "5.1"}],
        },
    )
    assert response.status_code == 200
    created = response.json()
    assert created["id"].startswith("fb_")
    assert (feedback_env.tmp / "feedback" / "qa.jsonl").exists()

    feedback_env.client.post(
        "/api/v1/standards/ask/feedback",
        json={"rating": "not_helpful", "reason": "引用不对", "question": "另一问"},
    )
    summary = feedback_env.client.get("/api/v1/kb/feedback").json()
    assert summary["totals"] == {"total": 2, "helpful": 1, "not_helpful": 1}
    assert summary["reasons"][0]["reason"] == "引用不对"
    cited = next(item for item in summary["items"] if item["standard_id"] == standard["id"])
    assert cited["project"] == "P1"
    assert cited["citations"][0]["clause"] == "5.1"
    assert {item["rating"] for item in summary["items"]} == {"helpful", "not_helpful"}


def test_feedback_validates_rating(feedback_env):
    response = feedback_env.client.post(
        "/api/v1/standards/ask/feedback", json={"rating": "maybe"}
    )
    assert response.status_code == 422


def test_feedback_summary_is_admin_only(auth_env):
    auth.create_user("viewer1", "viewer-pass-1", role="viewer")
    auth.create_user("root", "root-pass-1", role="admin")
    viewer = TestClient(main.app)
    admin = TestClient(main.app)
    _login(viewer, "viewer1", "viewer-pass-1")
    _login(admin, "root", "root-pass-1")

    # Viewers may rate an answer (they can ask), but cannot read the summary.
    assert viewer.post(
        "/api/v1/standards/ask/feedback", json={"rating": "helpful"}
    ).status_code == 200
    assert viewer.get("/api/v1/kb/feedback").status_code == 403
    assert admin.get("/api/v1/kb/feedback").status_code == 200
