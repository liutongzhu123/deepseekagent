"""OIDC login: state handling, ID-token validation and callback flow."""
from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import auth, main, oidc


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    numbers = rsa_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "kid-1",
                "alg": "RS256",
                "n": _b64e(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64e(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _token(rsa_key, claims: dict, alg: str = "RS256") -> str:
    header = {"alg": alg, "typ": "JWT", "kid": "kid-1"}
    signing = _b64e(json.dumps(header).encode()) + "." + _b64e(json.dumps(claims).encode())
    signature = rsa_key.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + _b64e(signature)


def _document() -> dict:
    return {
        "issuer": "https://idp.example",
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
    }


def _jwks_client(jwks: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example",
        "aud": "p0-client",
        "sub": "subject-1",
        "preferred_username": "alice",
        "exp": now + 300,
        "iat": now,
        "nonce": "nonce-1",
    }
    claims.update(overrides)
    return claims


def test_state_roundtrip_and_next_sanitising():
    state = oidc.new_state("/standards?x=1")
    assert state["next"] == "/standards?x=1"
    token = oidc.sign_state(state)
    assert oidc.read_state(token)["s"] == state["s"]
    assert oidc.read_state(token + "x") is None
    assert oidc.safe_next("https://evil.example/") == "/"
    assert oidc.safe_next("//evil.example") == "/"
    assert oidc.safe_next("/ok") == "/ok"


def test_authorize_url_has_pkce_and_nonce():
    cfg = oidc.OidcConfig("https://idp.example", "p0-client", "", "openid profile", "https://p0/cb")
    state = oidc.new_state("/")
    url = oidc.authorize_url(cfg, _document(), state)
    assert url.startswith("https://idp.example/authorize?")
    assert "code_challenge_method=S256" in url
    assert f"state={state['s']}" in url
    assert f"nonce={state['n']}" in url


def test_validate_id_token_accepts_valid_token(rsa_key, jwks):
    cfg = oidc.OidcConfig("https://idp.example", "p0-client", "", "openid", "https://p0/cb")
    token = _token(rsa_key, _claims())
    claims = oidc.validate_id_token(cfg, _document(), token, "nonce-1", client=_jwks_client(jwks))
    assert claims["preferred_username"] == "alice"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c.update(nonce="wrong"),
        lambda c: c.update(aud="other-client"),
        lambda c: c.update(exp=int(time.time()) - 3600),
        lambda c: c.update(iss="https://other.example"),
    ],
)
def test_validate_id_token_rejects_bad_claims(rsa_key, jwks, mutator):
    cfg = oidc.OidcConfig("https://idp.example", "p0-client", "", "openid", "https://p0/cb")
    claims = _claims()
    mutator(claims)
    token = _token(rsa_key, claims)
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(cfg, _document(), token, "nonce-1", client=_jwks_client(jwks))


def test_validate_id_token_rejects_bad_signature_and_alg(rsa_key, jwks, other_key):
    cfg = oidc.OidcConfig("https://idp.example", "p0-client", "", "openid", "https://p0/cb")
    token = _token(other_key, _claims())
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(cfg, _document(), token, "nonce-1", client=_jwks_client(jwks))
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(cfg, _document(), _token(rsa_key, _claims(), alg="HS256"), "nonce-1", client=_jwks_client(jwks))


@pytest.fixture()
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def oidc_env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setenv("P0_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("P0_OIDC_CLIENT_ID", "p0-client")
    monkeypatch.setenv("P0_OIDC_REDIRECT_URI", "https://p0.example/p0/api/v1/auth/oidc/callback")
    monkeypatch.setattr(oidc, "_DISCOVERY_CACHE", {})
    monkeypatch.setattr(oidc, "_JWKS_CACHE", {})
    auth.create_user("alice", "alice-pass-1", role="reviewer")
    return SimpleNamespace(client=TestClient(main.app), tmp=tmp_path)


def _mock_exchange(monkeypatch, claims: dict):
    monkeypatch.setattr(oidc, "discovery", lambda cfg, client=None: _document())
    monkeypatch.setattr(oidc, "exchange_code", lambda cfg, doc, code, verifier, client=None: {"id_token": "fake", "access_token": "access"})
    monkeypatch.setattr(oidc, "validate_id_token", lambda cfg, doc, token, nonce, client=None: claims)


def _login_flow(client: TestClient, claims: dict, monkeypatch) -> httpx.Response:
    _mock_exchange(monkeypatch, claims)
    start = client.get("/api/v1/auth/oidc/start?next=/standards", follow_redirects=False)
    assert start.status_code == 303
    state = httpx.URL(start.headers["location"]).params["state"]
    return client.get(f"/api/v1/auth/oidc/callback?code=abc&state={state}", follow_redirects=False)


def test_callback_creates_session_for_existing_user(oidc_env, monkeypatch):
    response = _login_flow(oidc_env.client, {"preferred_username": "alice"}, monkeypatch)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/standards")
    me = oidc_env.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"


def test_callback_rejects_unknown_user(oidc_env, monkeypatch):
    response = _login_flow(oidc_env.client, {"preferred_username": "nobody"}, monkeypatch)
    assert response.status_code == 303
    assert "sso_error=account" in response.headers["location"]


def test_callback_rejects_bad_state(oidc_env, monkeypatch):
    _mock_exchange(monkeypatch, {"preferred_username": "alice"})
    oidc_env.client.get("/api/v1/auth/oidc/start", follow_redirects=False)
    response = oidc_env.client.get(
        "/api/v1/auth/oidc/callback?code=abc&state=wrong", follow_redirects=False
    )
    assert response.status_code == 303
    assert "sso_error=state" in response.headers["location"]


def test_start_returns_404_when_not_configured(oidc_env, monkeypatch):
    monkeypatch.delenv("P0_OIDC_ISSUER", raising=False)
    response = oidc_env.client.get("/api/v1/auth/oidc/start")
    assert response.status_code == 404


def test_provider_status_reports_enabled_oidc(oidc_env):
    data = auth.provider_status()
    assert data["oidc"]["configured"] is True
    assert data["oidc"]["enabled"] is True
    assert data["oidc"]["status"] == "configured"
