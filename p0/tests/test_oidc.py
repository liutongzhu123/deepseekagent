"""OIDC login: state handling, ID-token validation and callback flow."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import auth, main, oidc


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_s256(verifier: str) -> str:
    """Independent RFC 7636 S256 calculation for the synthetic IdP."""
    return _b64e(hashlib.sha256(verifier.encode("ascii")).digest())


def test_pkce_s256_matches_rfc7636_known_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert _pkce_s256(verifier) == expected
    assert oidc._challenge(verifier) == expected


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


def _install_synthetic_idp(monkeypatch, rsa_key, jwks):
    """Wire the real P0 OIDC callback to an in-memory, protocol-shaped IdP."""
    requests = []
    issued = {"id_token": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_document())
        if request.url.path.endswith("/token"):
            form = parse_qs(request.content.decode("ascii"))
            if form.get("grant_type") != ["authorization_code"]:
                return httpx.Response(400, json={"error": "unsupported_grant_type"})
            if (
                form.get("code") != ["synthetic-code"]
                or form.get("client_id") != ["p0-client"]
                or form.get("redirect_uri")
                != ["https://p0.example/p0/api/v1/auth/oidc/callback"]
            ):
                return httpx.Response(400, json={"error": "invalid_client_or_code"})
            verifier = (form.get("code_verifier") or [""])[0]
            if (
                issued["code_challenge_method"] != "S256"
                or not issued["code_challenge"]
                or _pkce_s256(verifier) != issued["code_challenge"]
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            if not issued["id_token"]:
                return httpx.Response(400, json={"error": "missing_synthetic_id_token"})
            return httpx.Response(
                200,
                json={"id_token": issued["id_token"], "access_token": "synthetic-access"},
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    monkeypatch.setattr(
        oidc,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    return requests, issued


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


def test_oidc_config_requires_https_issuer_and_fixed_https_callback(monkeypatch):
    monkeypatch.setenv("P0_OIDC_CLIENT_ID", "p0-client")
    monkeypatch.setenv("P0_OIDC_ISSUER", "http://idp.example")
    monkeypatch.setenv("P0_OIDC_REDIRECT_URI", "https://p0.example/callback")
    assert oidc.config() is None

    monkeypatch.setenv("P0_OIDC_ISSUER", "https://idp.example")
    monkeypatch.delenv("P0_OIDC_REDIRECT_URI", raising=False)
    assert oidc.config() is None

    monkeypatch.setenv("P0_OIDC_REDIRECT_URI", "https://p0.example:99999/callback")
    assert oidc.config() is None


def test_oidc_callback_uri_ignores_forwarded_headers(monkeypatch):
    from starlette.requests import Request

    monkeypatch.delenv("P0_OIDC_REDIRECT_URI", raising=False)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/auth/oidc/start",
        "raw_path": b"/api/v1/auth/oidc/start",
        "query_string": b"",
        "headers": [(b"x-forwarded-host", b"attacker.example"), (b"x-forwarded-proto", b"https")],
        "server": ("internal", 8080),
        "client": ("127.0.0.1", 1234),
    }
    assert oidc.redirect_uri(Request(scope)) == ""

    monkeypatch.setenv("P0_OIDC_REDIRECT_URI", "https://p0.example/callback")
    assert oidc.redirect_uri(Request(scope)) == "https://p0.example/callback"


def test_oidc_binding_rejects_http_issuer(oidc_env):
    with pytest.raises(ValueError, match="HTTPS"):
        auth.bind_oidc_identity("alice", "http://idp.example", "subject-2")
    with pytest.raises(ValueError, match="HTTPS"):
        auth.bind_oidc_identity("alice", "https://idp.example:99999", "subject-3")


@pytest.mark.parametrize("endpoint_key", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
@pytest.mark.parametrize("scheme", ["http", "https-invalid-port"])
def test_discovery_rejects_insecure_endpoints(oidc_env, endpoint_key, scheme):
    document = _document()
    endpoint = "http://idp.example/insecure" if scheme == "http" else "https://idp.example:99999/insecure"
    document[endpoint_key] = endpoint
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=document)
        )
    )

    with pytest.raises(oidc.OidcError, match=f"{endpoint_key} must use HTTPS"):
        oidc.discovery(oidc.config(), client=client)
    client.close()


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
    auth.create_user("alice", "alice-pass-1", role="technical")
    auth.bind_oidc_identity("alice", "https://idp.example", "subject-1")
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
    response = _login_flow(oidc_env.client, _claims(), monkeypatch)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/standards")
    me = oidc_env.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"


@pytest.mark.parametrize(
    ("subject", "expected_location", "pkce_mismatch"),
    [
        ("subject-1", "/standards", False),
        ("subject-unbound", "sso_error=account", False),
        ("subject-1", "sso_error=token", True),
    ],
)
def test_synthetic_idp_full_code_flow_uses_verified_subject_only(
    oidc_env, monkeypatch, rsa_key, jwks, subject, expected_location, pkce_mismatch
):
    requests, issued = _install_synthetic_idp(monkeypatch, rsa_key, jwks)
    start = oidc_env.client.get(
        "/api/v1/auth/oidc/start?next=/standards", follow_redirects=False
    )
    assert start.status_code == 303
    authorize = httpx.URL(start.headers["location"])
    authorize_params = authorize.params
    assert authorize_params["response_type"] == "code"
    assert authorize_params["client_id"] == "p0-client"
    assert authorize_params["redirect_uri"] == (
        "https://p0.example/p0/api/v1/auth/oidc/callback"
    )
    assert authorize_params["scope"] == "openid profile email"
    assert authorize_params["code_challenge_method"] == "S256"

    state_cookie = start.cookies[oidc.STATE_COOKIE]
    state_payload = oidc.read_state(state_cookie)
    assert state_payload is not None
    expected_challenge = _pkce_s256(state_payload["v"])
    assert authorize_params["state"] == state_payload["s"]
    assert authorize_params["nonce"] == state_payload["n"]
    assert authorize_params["code_challenge"] == expected_challenge
    issued["code_challenge"] = (
        "tampered-challenge" if pkce_mismatch else authorize_params["code_challenge"]
    )
    issued["code_challenge_method"] = authorize_params["code_challenge_method"]

    # The synthetic provider signs claims only after receiving the auth request,
    # so the ID token carries the nonce generated for this exact browser flow.
    issued["id_token"] = _token(
        rsa_key,
        _claims(sub=subject, nonce=state_payload["n"], preferred_username="alice"),
    )
    callback = oidc_env.client.get(
        f"/api/v1/auth/oidc/callback?code=synthetic-code&state={state_payload['s']}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert expected_location in callback.headers["location"]
    token_request = next(request for request in requests if request.url.path.endswith("/token"))
    token_form = parse_qs(token_request.content.decode("ascii"))
    assert token_form["code"] == ["synthetic-code"]
    assert token_form["client_id"] == ["p0-client"]
    assert token_form["code_verifier"] == [state_payload["v"]]
    assert token_form["redirect_uri"] == ["https://p0.example/p0/api/v1/auth/oidc/callback"]
    assert sum(request.url.path.endswith("/jwks") for request in requests) == (
        0 if pkce_mismatch else 1
    )

    if subject == "subject-1" and not pkce_mismatch:
        me = oidc_env.client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "alice"
    else:
        assert "p0_session" not in callback.cookies
        assert [user["username"] for user in auth.load_users()] == ["alice"]


def test_callback_rejects_unknown_user(oidc_env, monkeypatch):
    response = _login_flow(oidc_env.client, _claims(sub="subject-nobody", preferred_username="alice"), monkeypatch)
    assert response.status_code == 303
    assert "sso_error=account" in response.headers["location"]


def test_oidc_identity_binding_uses_stable_subject_and_rejects_duplicates(oidc_env):
    assert auth.find_user_by_oidc("https://idp.example/", "subject-1")["username"] == "alice"
    auth.create_user("bob", "bob-pass-1", role="business_manager")
    try:
        auth.bind_oidc_identity("bob", "https://idp.example", "subject-1")
    except ValueError as exc:
        assert "已绑定" in str(exc)
    else:
        raise AssertionError("a subject must not bind to two accounts")
    assert auth.find_user_by_oidc("https://idp.example", "subject-1")["username"] == "alice"


def test_oidc_identity_cannot_be_bound_to_legacy_or_customer_role(oidc_env):
    auth.save_users(auth.load_users() + [{
        "username": "legacy-reviewer", "role": "reviewer", "active": True,
        "projects": [], "permission_grants": [],
    }, {
        "username": "external-customer", "role": "customer", "active": True,
        "projects": [], "permission_grants": [],
    }])
    for username in ("legacy-reviewer", "external-customer"):
        try:
            auth.bind_oidc_identity(username, "https://idp.example", f"subject-{username}")
        except ValueError as exc:
            assert "已确认四角色" in str(exc)
        else:
            raise AssertionError("OIDC must only bind to an explicitly confirmed staff role")


def test_callback_rejects_bad_state(oidc_env, monkeypatch):
    _mock_exchange(monkeypatch, _claims())
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
