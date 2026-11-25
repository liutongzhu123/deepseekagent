import json

from fastapi.testclient import TestClient
import pytest

from app import auth, main


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)


def test_password_hash_and_session_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    user = auth.create_user("alice", "correct-horse", role="technical", display_name="Alice")
    assert user["role"] == "technical"
    assert auth.authenticate("alice", "correct-horse")["username"] == "alice"
    assert auth.authenticate("alice", "wrong") is None
    token = auth.create_session(user)
    assert auth.read_session(token)["username"] == "alice"
    assert auth.read_session(token + "x") is None


def test_login_gate_and_audit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("bob", "secret-pass", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    assert client.get("/api/v1/rules").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    bad = client.post("/api/v1/auth/login", json={"username": "bob", "password": "nope"})
    assert bad.status_code == 401
    ok = client.post("/api/v1/auth/login", json={"username": "bob", "password": "secret-pass"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").json()["user"]["username"] == "bob"
    assert client.get("/api/v1/rules").status_code == 200
    assert client.get("/").status_code == 200
    audit_lines = auth.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line).get("event") == "login_success" for line in audit_lines)
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_admin_can_create_user(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    client.post("/api/v1/auth/login", json={"username": "root", "password": "root-pass-1"})
    created = client.post(
        "/api/v1/auth/users",
        json={"username": "carol", "password": "carol-pass-1", "role": "business_manager"},
    )
    assert created.status_code == 201
    assert auth.authenticate("carol", "carol-pass-1")["username"] == "carol"


def _login_as(client, username, password):
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def test_review_grants_are_account_specific_audited_and_removed_when_role_changes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("reviewer-one", "review-pass-1", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "root", "root-pass-1")

    granted = client.put(
        "/api/v1/auth/users/reviewer-one/review-permissions",
        json={
            "permissions": ["report:read", "report:audit", "standard:review"],
            "reason": "逐账号核实历史报告审核和标准复核职责",
        },
    )
    assert granted.status_code == 200
    assert granted.json()["user"]["review_permissions"] == [
        "report:audit", "report:read", "standard:review"
    ]
    assert auth.has_permission(auth.find_user("reviewer-one"), "report:audit")
    assert auth.has_permission(auth.find_user("reviewer-one"), "standard:review")
    session_user = auth.read_session(auth.create_session(auth.find_user("reviewer-one")))
    assert auth.has_permission(session_user, "report:audit")
    assert auth.has_permission(session_user, "standard:review")
    reviewer = TestClient(main.app)
    _login_as(reviewer, "reviewer-one", "review-pass-1")
    public_user = reviewer.get("/api/v1/auth/me").json()["user"]
    assert public_user["review_permissions"] == ["report:audit", "report:read", "standard:review"]
    assert "_approved_review_permissions" not in public_user
    assert "permission_grants" not in public_user
    assert not auth.has_permission(auth.find_user("reviewer-one"), "standard:publish")

    changed = auth.update_user("reviewer-one", role="business_manager")
    assert changed["review_permissions"] == []
    assert not auth.has_permission(auth.find_user("reviewer-one"), "report:audit")
    events = [json.loads(line) for line in auth.AUDIT_PATH.read_text().splitlines()]
    assert any(event.get("event") == "account_review_permissions_change_requested" for event in events)


def test_review_grant_fails_closed_when_audit_write_fails_or_admin_targets_self(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("staff", "staff-pass-1", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "root", "root-pass-1")
    payload = {"permissions": ["report:audit"], "reason": "逐账号已核实"}

    self_grant = client.put(
        "/api/v1/auth/users/root/review-permissions", json=payload
    )
    assert self_grant.status_code == 403

    monkeypatch.setattr(auth, "audit", lambda entry: False)
    failed = client.put(
        "/api/v1/auth/users/staff/review-permissions", json=payload
    )
    assert failed.status_code == 503
    assert auth.find_user("staff").get("permission_grants", []) == []


def test_external_customer_cannot_be_created_as_staff_login(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="微信小程序身份服务"):
        auth.create_user("customer-one", "customer-pass-1", role="customer")


def test_review_exceptions_cannot_be_granted_to_legacy_customer_identity(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    auth.save_users([{
        **auth.find_user("root"),
    }, {
        "username": "external-one", "role": "customer", "active": True,
        "projects": [], "permission_grants": [],
    }])
    with pytest.raises(ValueError, match="已确认四角色"):
        auth.set_review_permissions(
            "external-one", ["standard:review"],
            granted_by="admin", reason="should be rejected",
        )
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "root", "root-pass-1")
    response = client.put(
        "/api/v1/auth/users/external-one/review-permissions",
        json={"permissions": ["standard:review"], "reason": "should be rejected"},
    )
    assert response.status_code == 400
    events = [json.loads(line) for line in auth.AUDIT_PATH.read_text().splitlines()]
    assert not any(
        event.get("event") == "account_review_permissions_change_requested"
        and event.get("target") == "external-one"
        for event in events
    )


def test_direct_api_enforces_role_boundaries_for_crm_and_admin(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("manager", "manager-pass-1", role="business_manager")
    auth.create_user("technical", "technical-pass-1", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)

    _login_as(client, "manager", "manager-pass-1")
    assert client.get("/api/v1/auth/users").status_code == 403
    assert client.get("/api/v1/admin/config").status_code == 403
    assert client.post(
        "/api/v1/crm/base-data/qualifications", json={"name": "forbidden"}
    ).status_code == 403

    client.post("/api/v1/auth/logout")
    _login_as(client, "technical", "technical-pass-1")
    assert client.get("/api/v1/crm/base-data").status_code == 403
    assert client.post(
        "/api/v1/crm/base-data/prices", json={"project_name": "forbidden"}
    ).status_code == 403
