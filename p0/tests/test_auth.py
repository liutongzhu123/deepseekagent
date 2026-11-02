import json

from fastapi.testclient import TestClient

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
    user = auth.create_user("alice", "correct-horse", role="reviewer", display_name="Alice")
    assert user["role"] == "reviewer"
    assert auth.authenticate("alice", "correct-horse")["username"] == "alice"
    assert auth.authenticate("alice", "wrong") is None
    token = auth.create_session(user)
    assert auth.read_session(token)["username"] == "alice"
    assert auth.read_session(token + "x") is None


def test_login_gate_and_audit(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    auth.create_user("bob", "secret-pass", role="viewer")
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
        json={"username": "carol", "password": "carol-pass-1", "role": "viewer"},
    )
    assert created.status_code == 201
    assert auth.authenticate("carol", "carol-pass-1")["username"] == "carol"
