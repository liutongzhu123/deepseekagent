"""P1-S1 identity, RBAC, project isolation and audit tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, main, standard_store


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "AUDIT_RETENTION_DAYS", 180)
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(auth, "_LAST_PRUNE_DATE", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")
    monkeypatch.setattr(standard_store, "INDEX_PATH", tmp_path / "kb_index.json")
    monkeypatch.setattr(main, "QUOTE_DIR", tmp_path / "quotes")
    return SimpleNamespace(tmp=tmp_path, client=TestClient(main.app))


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_password_policy(env):
    with pytest.raises(ValueError, match="至少 10 位"):
        auth.create_user("weak", "short1!")
    with pytest.raises(ValueError, match="至少两类"):
        auth.create_user("weak2", "abcdefghij")
    user = auth.create_user("strong", "abcdefgh12", role="reviewer")
    assert user["role"] == "reviewer"
    assert user["projects"] == []
    assert auth.authenticate("strong", "abcdefgh12")["username"] == "strong"


def test_role_permission_matrix(env):
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("viewer1", "viewer-pass-1", role="viewer")
    auth.create_user("sales1", "sales-pass-1", role="sales")
    auth.create_user("reviewer1", "review-pass-1", role="reviewer")

    viewer = TestClient(main.app)
    assert _login(viewer, "viewer1", "viewer-pass-1").status_code == 200
    assert viewer.get("/api/v1/rules").status_code == 200
    assert viewer.get("/api/v1/standards").status_code == 200
    assert viewer.get("/api/v1/auth/users").status_code == 403
    assert viewer.get("/api/v1/audit/events").status_code == 403
    assert viewer.post("/api/v1/reports/audit", files={"file": ("a.docx", b"x")}).status_code == 403
    assert viewer.post("/api/v1/standards/parse", files={"file": ("a.pdf", b"x")}).status_code == 403
    assert viewer.post("/api/v1/quotes/parse", files={"file": ("a.xlsx", b"x")}).status_code == 403

    sales = TestClient(main.app)
    assert _login(sales, "sales1", "sales-pass-1").status_code == 200
    assert sales.post("/api/v1/reports/audit", files={"file": ("a.docx", b"x")}).status_code == 403
    # Permission passed; the fake workbook fails parsing rather than authorization.
    assert sales.post("/api/v1/quotes/parse", files={"file": ("a.xlsx", b"x")}).status_code in {400, 422}

    reviewer = TestClient(main.app)
    assert _login(reviewer, "reviewer1", "review-pass-1").status_code == 200
    assert reviewer.post("/api/v1/quotes/parse", files={"file": ("a.xlsx", b"x")}).status_code == 403
    assert reviewer.get("/api/v1/audit/events").status_code == 403

    admin = TestClient(main.app)
    assert _login(admin, "root", "root-pass-1").status_code == 200
    assert admin.get("/api/v1/auth/users").status_code == 200
    assert admin.get("/api/v1/audit/events").status_code == 200


def test_account_lockout_and_reset(env):
    auth.create_user("lockme", "lock-pass-1", role="viewer")
    client = TestClient(main.app)
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert _login(client, "lockme", "wrong").status_code == 401
    locked = _login(client, "lockme", "lock-pass-1")
    assert locked.status_code == 423
    assert "锁定" in locked.json()["detail"]

    auth.create_user("root", "root-pass-1", role="admin")
    admin = TestClient(main.app)
    assert _login(admin, "root", "root-pass-1").status_code == 200
    reset = admin.post("/api/v1/auth/users/lockme/reset-password", json={})
    assert reset.status_code == 200
    password = reset.json()["password"]
    assert reset.json()["generated"] is True
    assert auth.authenticate("lockme", password)["username"] == "lockme"
    assert _login(TestClient(main.app), "lockme", password).status_code == 200


def test_admin_updates_user_and_guards_last_admin(env):
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("carol", "carol-pass-1", role="viewer", department="测试部")
    admin = TestClient(main.app)
    assert _login(admin, "root", "root-pass-1").status_code == 200

    updated = admin.patch(
        "/api/v1/auth/users/carol",
        json={"role": "reviewer", "projects": ["P-A"], "department": "质量部"},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["role"] == "reviewer"
    assert updated.json()["user"]["projects"] == ["P-A"]
    assert updated.json()["user"]["restricted"] is True

    assert admin.patch("/api/v1/auth/users/root", json={"active": False}).status_code == 409
    with pytest.raises(ValueError, match="至少保留一个启用的管理员"):
        auth.update_user("root", active=False)

    disabled = admin.patch("/api/v1/auth/users/carol", json={"active": False})
    assert disabled.status_code == 200
    assert disabled.json()["user"]["active"] is False
    assert _login(TestClient(main.app), "carol", "carol-pass-1").status_code == 401


def test_audit_query_and_export(env):
    auth.create_user("root", "root-pass-1", role="admin")
    admin = TestClient(main.app)
    assert _login(admin, "root", "root-pass-1").status_code == 200
    assert admin.get("/api/v1/rules").status_code == 200

    events = admin.get("/api/v1/audit/events", params={"user": "root", "limit": 50})
    assert events.status_code == 200
    payload = events.json()
    assert payload["total"] >= 2
    assert all(item["user"] == "root" for item in payload["events"])

    logins = admin.get("/api/v1/audit/events", params={"event": "login_success"})
    assert logins.status_code == 200
    assert logins.json()["total"] >= 1

    forbidden = admin.get("/api/v1/audit/events", params={"status": 403})
    assert forbidden.status_code == 200

    exported = admin.get("/api/v1/audit/export.csv")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=audit.csv" in exported.headers["content-disposition"]
    assert "login_success" in exported.text
    assert "'=cmd" in auth.audit_csv([{"user": "=cmd", "event": "probe"}])


def test_audit_retention_prunes_old_lines(env):
    now = datetime.now(timezone.utc)
    old = {"at": (now - timedelta(days=30)).isoformat(timespec="seconds"), "event": "old"}
    new = {"at": (now - timedelta(days=1)).isoformat(timespec="seconds"), "event": "new"}
    auth.AUDIT_PATH.parent.mkdir(parents=True)
    auth.AUDIT_PATH.write_text(
        json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8"
    )
    assert auth.prune_audit(7) == 1
    remaining = auth.AUDIT_PATH.read_text(encoding="utf-8")
    assert "old" not in remaining
    assert "new" in remaining


def test_project_isolation_for_standards_and_quotes(env):
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("alice", "alice-pass-1", role="reviewer", projects=["P-A"])
    auth.create_user("bob", "bob-pass-1", role="reviewer", projects=["P-B"])
    auth.create_user("carol", "carol-pass-1", role="sales", projects=["P-A"])
    auth.create_user("dave", "dave-pass-1", role="sales", projects=["P-B"])

    parsed_a = {
        "meta": {"standard_no": "Q/A 1-2026", "name": "A 标准", "pages": 1},
        "summary": {"items": 0},
        "items": [],
    }
    record_a = standard_store.create(parsed_a, b"pdf", "a.pdf", project="P-A", created_by="alice")
    record_b = standard_store.create(parsed_a, b"pdf", "b.pdf", project="P-B", created_by="bob")

    alice = TestClient(main.app)
    assert _login(alice, "alice", "alice-pass-1").status_code == 200
    listed = alice.get("/api/v1/standards").json()["standards"]
    assert [item["id"] for item in listed] == [record_a["id"]]
    assert alice.get(f"/api/v1/standards/{record_a['id']}").status_code == 200
    assert alice.get(f"/api/v1/standards/{record_b['id']}").status_code == 403

    admin = TestClient(main.app)
    assert _login(admin, "root", "root-pass-1").status_code == 200
    tagged = admin.patch(
        f"/api/v1/standards/{record_b['id']}/project",
        json={"project": "P-A", "created_by": "bob"},
    )
    assert tagged.status_code == 200
    assert tagged.json()["meta"]["project"] == "P-A"
    assert alice.get(f"/api/v1/standards/{record_b['id']}").status_code == 200

    missing_project = alice.post(
        "/api/v1/standards/parse", files={"file": ("a.pdf", b"x")}
    )
    assert missing_project.status_code == 400

    quotes = env.tmp / "quotes" / "q_20260101000000_abcdef"
    quotes.mkdir(parents=True)
    (quotes / "CRM报价草稿.xlsx").write_bytes(b"xlsx")
    (quotes / "meta.json").write_text(
        json.dumps({"project": "P-A", "created_by": "alice"}), encoding="utf-8"
    )
    carol = TestClient(main.app)
    assert _login(carol, "carol", "carol-pass-1").status_code == 200
    assert carol.get("/api/v1/quotes/q_20260101000000_abcdef/crm.xlsx").status_code == 200
    dave = TestClient(main.app)
    assert _login(dave, "dave", "dave-pass-1").status_code == 200
    assert dave.get("/api/v1/quotes/q_20260101000000_abcdef/crm.xlsx").status_code == 403


def test_password_change_invalidates_existing_sessions(env):
    auth.create_user("dana", "dana-pass-1", role="viewer")
    client = TestClient(main.app)
    assert _login(client, "dana", "dana-pass-1").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    changed = client.post(
        "/api/v1/auth/password",
        json={"old_password": "dana-pass-1", "new_password": "dana-pass-2"},
    )
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    assert _login(TestClient(main.app), "dana", "dana-pass-2").status_code == 200


def test_kb_stats_filters_projects(env):
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("alice", "alice-pass-1", role="reviewer", projects=["P-A"])
    parsed = {
        "meta": {"standard_no": "Q/A 1-2026", "name": "A 标准", "pages": 1},
        "summary": {"items": 1},
        "items": [{"seq": 1, "clause": "5.1", "name": "外观", "requirement": "无缺陷", "method_clause": "6.1", "method": "目测", "page": 1, "method_page": 1}],
    }
    record_a = standard_store.create(parsed, b"pdf", "a.pdf", project="P-A", created_by="alice")
    record_b = standard_store.create(parsed, b"pdf", "b.pdf", project="P-B", created_by="bob")
    # 未确认不入库：重建索引只收已复核的标准（SP-2.2）。
    for record in (record_a, record_b):
        standard_store.update_review(
            record["id"], [{"clause": "5.1", "reviewed": True}], reviewer="复核人"
        )
    standard_store.rebuild_index()

    alice = TestClient(main.app)
    assert _login(alice, "alice", "alice-pass-1").status_code == 200
    stats = alice.get("/api/v1/kb/stats").json()
    assert stats["standard_count"] == 1
    assert [item["id"] for item in stats["standards"]] == [record_a["id"]]


def test_provider_seam(env, monkeypatch):
    public = TestClient(main.app)
    data = public.get("/api/v1/auth/providers")
    assert data.status_code == 200
    assert data.json()["providers"][0]["id"] == "local"
    assert data.json()["oidc"]["configured"] is False
    monkeypatch.setenv("P0_OIDC_ISSUER", "https://sso.example.com")
    monkeypatch.setenv("P0_OIDC_CLIENT_ID", "cq-p1")
    assert public.get("/api/v1/auth/providers").json()["oidc"]["configured"] is True
