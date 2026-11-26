import json
import stat

from fastapi.testclient import TestClient
import pytest

from app import auth, main, session_store
from app.store import config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_STORE", "json")
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(
        session_store,
        "_json_path",
        lambda: tmp_path / "auth" / "session_revocations.json",
    )


def test_password_hash_and_session_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    user = auth.create_user("alice", "correct-horse", role="technical", display_name="Alice")
    assert user["role"] == "technical"
    assert auth.authenticate("alice", "correct-horse")["username"] == "alice"
    assert auth.authenticate("alice", "wrong") is None
    token = auth.create_session(user)
    assert len(auth._decode_session_payload(token)["n"]) == 32
    assert auth.read_session(token)["username"] == "alice"
    assert auth.read_session(token + "x") is None


def test_weknora_admin_link_is_https_and_returned_only_to_admin(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("tech", "tech-pass-1", role="technical")
    auth.create_user("manager", "manager-pass-1", role="business_manager")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setenv(
        "P0_WEKNORA_ADMIN_URL", "https://kb.example.test/api/v1/auth/oidc/start"
    )
    client = TestClient(main.app)

    _login_as(client, "root", "root-pass-1")
    admin_status = client.get("/api/v1/system/status")
    assert admin_status.status_code == 200
    assert admin_status.json()["weknora"]["admin_console_url"] == (
        "https://kb.example.test/api/v1/auth/oidc/start"
    )

    for username, password in (("tech", "tech-pass-1"), ("manager", "manager-pass-1")):
        _login_as(client, username, password)
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        assert response.json()["weknora"]["admin_console_url"] == ""

    # Customer identities are managed by the mini-program, never employee login.
    # Exercise the role gate directly without inventing a customer employee account.
    monkeypatch.setattr(main, "_require_permission", lambda *_: {"role": "customer"})
    assert main.system_status(None)["weknora"]["admin_console_url"] == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://kb.example.test/api/v1/auth/oidc/start",
        "https://user@kb.example.test/api/v1/auth/oidc/start",
        "https://kb.example.test/api/v1/auth/oidc/start?next=https://evil.test",
        "https://kb.example.test/api/v1/auth/oidc/start?",
        "https://kb.example.test/api/v1/auth/oidc/start#external",
        "https://kb.example.test/api/v1/auth/oidc/start#",
        "https://kb.example.test/admin",
        "https://[invalid/api/v1/auth/oidc/start",
    ],
)
def test_weknora_admin_link_rejects_unsafe_or_unexpected_url(monkeypatch, tmp_path, url):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setenv("P0_WEKNORA_ADMIN_URL", url)
    client = TestClient(main.app)
    _login_as(client, "root", "root-pass-1")

    response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json()["weknora"]["admin_console_url"] == ""


def test_new_non_admin_scope_fails_closed_but_legacy_rows_remain_compatible(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    manager = auth.create_user("manager", "manager-pass", role="business_manager")
    assert manager["restricted"] is True
    assert manager["project_scope_enforced"] is True
    assert not auth.can_access_project(auth.find_user("manager"), "P-A")

    scoped = auth.create_user("tech", "technical-pass", role="technical", projects=["P-A"])
    assert scoped["restricted"] is True
    assert auth.can_access_project(auth.find_user("tech"), "P-A")
    assert not auth.can_access_project(auth.find_user("tech"), "P-B")

    global_manager = auth.create_user("global", "global-pass-1", role="business_manager", projects=["*"])
    assert global_manager["restricted"] is False
    assert auth.can_access_project(auth.find_user("global"), "P-B")

    legacy = {
        "username": "legacy", "role": "business_manager", "active": True,
        "projects": [], "session_epoch": 0,
    }
    auth.save_users([*auth.load_users(), legacy])
    assert auth.can_access_project(auth.find_user("legacy"), "P-B")
    legacy_token = auth.create_session(auth.find_user("legacy"))
    migrated = auth.update_user("legacy", role="technical")
    assert migrated["project_scope_enforced"] is True
    assert migrated["restricted"] is True
    assert not auth.can_access_project(auth.find_user("legacy"), "P-B")
    assert auth.read_session(legacy_token) is None


def test_security_scope_changes_revoke_existing_sessions(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("staff", "staff-pass", role="technical", projects=["P-A"])
    token = auth.create_session(auth.find_user("staff"))
    assert auth.read_session(token)

    auth.update_user("staff", projects=["P-B"])
    assert auth.read_session(token) is None
    token = auth.create_session(auth.find_user("staff"))
    assert auth.read_session(token)

    auth.set_review_permissions(
        "staff", ["standard:review"], granted_by="root", reason="逐账号授权"
    )
    review_token = auth.create_session(auth.find_user("staff"))
    assert auth.has_permission(auth.read_session(review_token), "standard:review")
    auth.set_review_permissions("staff", [], granted_by="root", reason="撤销授权")
    assert auth.read_session(review_token) is None


def test_access_change_audit_failure_prevents_scope_update(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("root", "root-pass-1", role="admin")
    auth.create_user("staff", "staff-pass-1", role="technical", projects=["P-A"])
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "root", "root-pass-1")
    monkeypatch.setattr(auth, "audit", lambda entry: False)

    response = client.patch("/api/v1/auth/users/staff", json={"projects": ["P-B"]})

    assert response.status_code == 503
    assert auth.find_user("staff")["projects"] == ["P-A"]


def test_standard_ask_filters_published_weknora_knowledge_by_project(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("manager", "manager-pass-1", role="business_manager", projects=["P-A"])
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "manager", "manager-pass-1")
    published = [
        {"standard_no": "Q/A", "project": "P-A", "weknora": {"knowledge_id": "kb-a"}},
        {"standard_no": "Q/B", "project": "P-B", "weknora": {"knowledge_id": "kb-b"}},
    ]
    received = []
    monkeypatch.setattr(main.standard_store, "published_kbs", lambda standard_id=None: published)
    monkeypatch.setattr(main.standard_store, "load", lambda standard_id: {"meta": {"project": "P-B"}})
    monkeypatch.setattr(
        main.standard_qa,
        "answer",
        lambda question, kbs, **kwargs: received.append(kbs) or {"found": False},
    )

    response = client.post("/api/v1/standards/ask", json={"question": "测试问题"})

    assert response.status_code == 200
    assert [kb["standard_no"] for kb in received[0]] == ["Q/A"]
    assert client.post(
        "/api/v1/standards/ask", json={"question": "测试问题", "standard_id": "standard-b"}
    ).status_code == 403


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


def test_logout_revokes_only_the_current_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("staff", "staff-pass-1", role="technical")
    first = auth.create_session(auth.find_user("staff"))
    second = auth.create_session(auth.find_user("staff"))
    assert auth.read_session(first)
    assert auth.read_session(second)

    assert auth.revoke_session(first)
    assert auth.read_session(first) is None
    assert auth.read_session(second)


def test_logout_endpoint_revokes_a_copied_cookie(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("staff", "staff-pass-1", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "staff", "staff-pass-1")
    copied_token = client.cookies.get(auth.COOKIE_NAME)

    assert client.post("/api/v1/auth/logout").status_code == 200
    replay = TestClient(main.app, cookies={auth.COOKIE_NAME: copied_token})
    assert replay.get("/api/v1/auth/me").status_code == 401


def test_logout_storage_failure_returns_503_without_revoking_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("staff", "staff-pass-1", role="technical")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    client = TestClient(main.app)
    _login_as(client, "staff", "staff-pass-1")

    def fail_revoke(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(
        session_store,
        "revoke",
        fail_revoke,
    )

    assert client.post("/api/v1/auth/logout").status_code == 503
    assert client.get("/api/v1/auth/me").status_code == 200


def test_corrupt_revocation_ledger_fails_closed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    user = auth.create_user("staff", "staff-pass-1", role="technical")
    token = auth.create_session(user)
    session_store._json_path().parent.mkdir(parents=True, exist_ok=True)
    session_store._json_path().write_text("not-json", encoding="utf-8")

    assert auth.read_session(token) is None


def test_json_revocation_storage_uses_private_permissions(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    session_store.is_revoked("a" * 64, 2**31)
    auth_dir = tmp_path / "auth"
    lock_file = auth_dir / "session_revocations.json.lock"

    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


def test_postgres_store_revocation_table_roundtrip_on_sqlite(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_STORE", "postgres")
    monkeypatch.setenv("P0_DATABASE_URL", f"sqlite:///{tmp_path / 'sessions.db'}")
    config.reset_caches()
    try:
        digest = "b" * 64
        expires_at = 2**31
        session_store.revoke(digest, expires_at)
        assert session_store.is_revoked(digest, 1_700_000_000)
        assert not session_store.is_revoked(digest, expires_at + 1)
    finally:
        config.reset_caches()


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
