"""Generic OIDC (authorization code + PKCE) login for the P1 console.

Default off: only active when ``P0_OIDC_ISSUER`` and ``P0_OIDC_CLIENT_ID`` are
set.  The flow binds an IdP identity to an **existing** active account by
``preferred_username`` (or a user's ``email`` when present); it never creates a
user or changes roles.  ID tokens are validated (RS256 + JWKS, issuer,
audience, expiry, nonce) before a local session is issued.

Security notes:

* the state/nonce/code-verifier payload is HMAC-signed with the session secret;
* ``next`` is restricted to a local path (no scheme/host);
* outgoing requests use short timeouts; discovery/JWKS are cached for one hour.

The unit tests use a generated RSA key and a mock transport; a real IdP still
has to be configured and accepted by the operator.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import auth

STATE_COOKIE = "p0_oidc_state"
_STATE_TTL_SECONDS = 600
_CACHE_TTL_SECONDS = 3600
_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_CACHE: dict[str, tuple[float, list[dict]]] = {}


class OidcError(RuntimeError):
    """Discovery/token/validation failure surfaced by the endpoints."""


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    redirect_uri: str


def config(redirect_uri: str = "") -> OidcConfig | None:
    issuer = os.getenv("P0_OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.getenv("P0_OIDC_CLIENT_ID", "").strip()
    if not issuer or not client_id:
        return None
    return OidcConfig(
        issuer=issuer,
        client_id=client_id,
        client_secret=os.getenv("P0_OIDC_CLIENT_SECRET", "").strip(),
        scopes=os.getenv("P0_OIDC_SCOPES", "openid profile email").strip() or "openid profile email",
        redirect_uri=redirect_uri,
    )


def enabled() -> bool:
    return config() is not None and os.getenv("P0_OIDC_ENABLED", "1").lower() not in {"0", "false", "off", "no"}


def redirect_uri(request) -> str:
    """Callback URL; ``P0_OIDC_REDIRECT_URI`` overrides proxy-derived values."""
    override = os.getenv("P0_OIDC_REDIRECT_URI", "").strip()
    if override:
        return override
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    root = os.getenv("P0_ROOT_PATH", "").rstrip("/")
    return f"{scheme}://{host}{root}/api/v1/auth/oidc/callback"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    digest = hmac.new(auth.state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{digest}"


def sign_state(payload: dict) -> str:
    return _sign(payload)


def read_state(token: str) -> dict | None:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(auth.state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        for key in ("s", "n", "v", "next"):
            if not payload.get(key):
                return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def safe_next(value: str | None) -> str:
    text = (value or "/").strip()
    if not text.startswith("/") or text.startswith("//") or "\\" in text or "://" in text:
        return "/"
    return text


def new_state(next_path: str = "/") -> dict:
    return {
        "s": secrets.token_urlsafe(24),
        "n": secrets.token_urlsafe(24),
        "v": secrets.token_urlsafe(48),
        "next": safe_next(next_path),
        "exp": int(time.time()) + _STATE_TTL_SECONDS,
    }


def _client() -> httpx.Client:
    return httpx.Client(timeout=float(os.getenv("P0_OIDC_TIMEOUT", "10")), follow_redirects=False)


def discovery(cfg: OidcConfig, client: httpx.Client | None = None) -> dict:
    cached = _DISCOVERY_CACHE.get(cfg.issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    owns = client is None
    client = client or _client()
    try:
        response = client.get(cfg.issuer + "/.well-known/openid-configuration")
        if response.status_code != 200:
            raise OidcError(f"discovery returned {response.status_code}")
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"discovery failed: {exc}") from exc
    finally:
        if owns:
            client.close()
    document_issuer = str(document.get("issuer", "")).rstrip("/")
    if document_issuer != cfg.issuer:
        raise OidcError("discovery issuer does not match P0_OIDC_ISSUER")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(key):
            raise OidcError(f"discovery missing {key}")
    _DISCOVERY_CACHE[cfg.issuer] = (time.time() + _CACHE_TTL_SECONDS, document)
    return document


def _challenge(verifier: str) -> str:
    return _b64e(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(cfg: OidcConfig, document: dict, state: dict) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state["s"],
        "nonce": state["n"],
        "code_challenge": _challenge(state["v"]),
        "code_challenge_method": "S256",
    }
    endpoint = str(document["authorization_endpoint"])
    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + urlencode(params)


def exchange_code(
    cfg: OidcConfig, document: dict, code: str, verifier: str, client: httpx.Client | None = None
) -> dict:
    owns = client is None
    client = client or _client()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": verifier,
        "client_id": cfg.client_id,
    }
    headers = {"Accept": "application/json"}
    if cfg.client_secret:
        if os.getenv("P0_OIDC_TOKEN_AUTH", "post").lower() == "basic":
            raw = f"{cfg.client_id}:{cfg.client_secret}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            data["client_secret"] = cfg.client_secret
    try:
        response = client.post(str(document["token_endpoint"]), data=data, headers=headers)
        if response.status_code != 200:
            raise OidcError(f"token endpoint returned {response.status_code}")
        tokens = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"token exchange failed: {exc}") from exc
    finally:
        if owns:
            client.close()
    if not tokens.get("id_token"):
        raise OidcError("token response has no id_token")
    return tokens


def _jwks(document: dict, client: httpx.Client | None = None) -> list[dict]:
    uri = str(document["jwks_uri"])
    cached = _JWKS_CACHE.get(uri)
    if cached and cached[0] > time.time():
        return cached[1]
    owns = client is None
    client = client or _client()
    try:
        response = client.get(uri)
        if response.status_code != 200:
            raise OidcError(f"jwks returned {response.status_code}")
        keys = response.json().get("keys") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"jwks fetch failed: {exc}") from exc
    finally:
        if owns:
            client.close()
    _JWKS_CACHE[uri] = (time.time() + _CACHE_TTL_SECONDS, keys)
    return keys


def _verify_signature(header: dict, signing_input: bytes, signature: bytes, document: dict, client=None) -> None:
    if header.get("alg") != "RS256":
        raise OidcError("only RS256 ID tokens are supported")
    keys = _jwks(document, client)
    kid = header.get("kid")
    candidates = [key for key in keys if key.get("kty") == "RSA" and (not kid or key.get("kid") == kid)]
    if kid and not candidates:
        _JWKS_CACHE.pop(str(document["jwks_uri"]), None)
        candidates = [key for key in _jwks(document, client) if key.get("kty") == "RSA" and key.get("kid") == kid]
    if not candidates:
        raise OidcError("no matching JWKS key")
    for key in candidates:
        try:
            public_key = rsa.RSAPublicNumbers(
                int.from_bytes(_b64d(str(key["e"])), "big"),
                int.from_bytes(_b64d(str(key["n"])), "big"),
            ).public_key()
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return
        except (ValueError, TypeError, Exception):  # noqa: BLE001 - try the next key
            continue
    raise OidcError("ID token signature is invalid")


def validate_id_token(
    cfg: OidcConfig, document: dict, token: str, nonce: str, client: httpx.Client | None = None
) -> dict:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(_b64d(header_segment))
        claims = json.loads(_b64d(payload_segment))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OidcError("ID token is not a valid JWT") from exc
    _verify_signature(
        header, f"{header_segment}.{payload_segment}".encode("ascii"), _b64d(signature_segment), document, client
    )
    now = int(time.time())
    if str(claims.get("iss", "")).rstrip("/") != cfg.issuer:
        raise OidcError("ID token issuer mismatch")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else list(audience or [])
    if cfg.client_id not in audiences:
        raise OidcError("ID token audience mismatch")
    if claims.get("azp") and claims["azp"] != cfg.client_id:
        raise OidcError("ID token authorized party mismatch")
    if int(claims.get("exp", 0)) < now - 60:
        raise OidcError("ID token expired")
    if int(claims.get("iat", now)) > now + 120:
        raise OidcError("ID token issued in the future")
    if claims.get("nonce") != nonce:
        raise OidcError("ID token nonce mismatch")
    return claims


def userinfo(cfg: OidcConfig, document: dict, access_token: str, client: httpx.Client | None = None) -> dict:
    endpoint = document.get("userinfo_endpoint")
    if not endpoint or not access_token:
        return {}
    owns = client is None
    client = client or _client()
    try:
        response = client.get(str(endpoint), headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}
    except (httpx.HTTPError, ValueError):
        return {}
    finally:
        if owns:
            client.close()


def identity_values(claims: dict, extra: dict | None = None) -> list[str]:
    """Candidate usernames/emails, preferred first; ``sub`` is not a username."""
    values: list[str] = []
    for source in (claims, extra or {}):
        for key in ("preferred_username", "email"):
            value = str(source.get(key, "")).strip()
            if value and value not in values:
                values.append(value)
    return values
