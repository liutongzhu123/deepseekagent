"""Minimal session authentication, user storage and audit logging.

The P0 controlled launch needs a login gate in front of business data before it
is exposed on the public HTTPS entry.  This module deliberately keeps the
dependency footprint at the Python standard library:

* users live in ``data/auth/users.json`` (atomic writes, PBKDF2-SHA256 hashes);
* sessions are HMAC-signed cookies, so no server-side session store is needed;
* every authenticated API call is appended to ``data/audit/audit.jsonl``.

It is intentionally small: the P1 identity work (SSO/RBAC/tenant isolation) will
replace it behind the same ``current_user``/``require_role`` seam.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("P0_DATA_DIR", ROOT / "data"))
AUTH_DIR = DATA_DIR / "auth"
AUDIT_DIR = DATA_DIR / "audit"
USERS_PATH = Path(os.getenv("P0_AUTH_USERS", AUTH_DIR / "users.json"))
SECRET_PATH = Path(os.getenv("P0_SESSION_SECRET_FILE", AUTH_DIR / "session_secret"))
INITIAL_PASSWORD_PATH = Path(
    os.getenv("P0_INITIAL_PASSWORD_FILE", AUTH_DIR / "initial_admin_password.txt")
)
AUDIT_PATH = Path(os.getenv("P0_AUDIT_LOG", AUDIT_DIR / "audit.jsonl"))

COOKIE_NAME = "p0_session"
SESSION_TTL_SECONDS = int(os.getenv("P0_SESSION_TTL", str(8 * 3600)))
PBKDF2_ITERATIONS = 240_000
ROLES = ("admin", "reviewer", "viewer")

_LOCK = threading.RLock()
_SECRET: bytes | None = None
_RATE_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(mode)
    except OSError:
        pass
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def load_users() -> list[dict]:
    if not USERS_PATH.exists():
        return []
    try:
        payload = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    users = payload.get("users", []) if isinstance(payload, dict) else []
    return [user for user in users if isinstance(user, dict) and user.get("username")]


def save_users(users: list[dict]) -> None:
    _atomic_write(USERS_PATH, json.dumps({"users": users}, ensure_ascii=False, indent=1))


def find_user(username: str) -> dict | None:
    key = (username or "").strip().lower()
    for user in load_users():
        if user.get("username", "").lower() == key:
            return user
    return None


def create_user(
    username: str,
    password: str,
    role: str = "viewer",
    display_name: str = "",
) -> dict:
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValueError("用户名不能为空且不超过 64 位")
    if role not in ROLES:
        raise ValueError(f"角色必须是 {', '.join(ROLES)}")
    with _LOCK:
        users = load_users()
        if any(user.get("username", "").lower() == username.lower() for user in users):
            raise ValueError("用户已存在")
        user = {
            "username": username,
            "display_name": display_name or username,
            "role": role,
            "active": True,
            "password_hash": hash_password(password),
            "created_at": _now(),
        }
        users.append(user)
        save_users(users)
        return public_user(user)


def update_password(username: str, new_password: str) -> None:
    with _LOCK:
        users = load_users()
        for user in users:
            if user.get("username", "").lower() == username.lower():
                user["password_hash"] = hash_password(new_password)
                user["updated_at"] = _now()
                save_users(users)
                return
    raise KeyError(username)


def public_user(user: dict) -> dict:
    return {
        "username": user.get("username", ""),
        "display_name": user.get("display_name") or user.get("username", ""),
        "role": user.get("role", "viewer"),
        "active": bool(user.get("active", True)),
    }


def authenticate(username: str, password: str) -> dict | None:
    user = find_user(username)
    if not user or not user.get("active", True):
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return public_user(user)


def bootstrap() -> dict:
    """Ensure the auth store exists; return metadata about the bootstrap admin.

    An explicit ``P0_ADMIN_PASSWORD`` wins.  Otherwise a strong random password
    is generated once and written to a 0600 file so the operator can retrieve it
    without it appearing in process listings or the repository.
    """
    with _LOCK:
        users = load_users()
        created = False
        password = ""
        if not users:
            username = os.getenv("P0_ADMIN_USER", "admin").strip() or "admin"
            password = os.getenv("P0_ADMIN_PASSWORD", "")
            generated = not password
            if generated:
                password = secrets.token_urlsafe(18)
            create_user(username, password, role="admin", display_name="系统管理员")
            if generated:
                _atomic_write(INITIAL_PASSWORD_PATH, password + "\n")
            else:
                try:
                    INITIAL_PASSWORD_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
            users = load_users()
            created = True
        return {"created": created, "user_count": len(users)}


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
def _session_secret() -> bytes:
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    configured = os.getenv("P0_SESSION_SECRET", "").strip()
    if configured:
        _SECRET = configured.encode("utf-8")
        return _SECRET
    with _LOCK:
        if SECRET_PATH.exists():
            _SECRET = SECRET_PATH.read_text(encoding="ascii").strip().encode("ascii")
        else:
            _SECRET = secrets.token_urlsafe(48).encode("ascii")
            _atomic_write(SECRET_PATH, _SECRET.decode("ascii") + "\n")
    return _SECRET


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_session(user: dict) -> str:
    payload = {
        "u": user["username"],
        "r": user["role"],
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "n": secrets.token_hex(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(
        hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def read_session(token: str) -> dict | None:
    try:
        body, signature = token.split(".", 1)
        expected = _b64encode(
            hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        user = find_user(payload.get("u", ""))
        if not user or not user.get("active", True):
            return None
        return public_user(user)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Login throttling and audit
# ---------------------------------------------------------------------------
def throttled(key: str, limit: int = 8, window: int = 300) -> bool:
    now = time.time()
    with _RATE_LOCK:
        attempts = [stamp for stamp in _RATE.get(key, []) if now - stamp < window]
        _RATE[key] = attempts
        return len(attempts) >= limit


def record_attempt(key: str) -> None:
    with _RATE_LOCK:
        _RATE.setdefault(key, []).append(time.time())
        _RATE[key] = _RATE[key][-32:]


def audit(entry: dict) -> None:
    record = {"at": _now(), **entry}
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Auditing must never take the business endpoint down.
        pass


def recent_audit(limit: int = 100) -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines[-max(1, min(limit, 500)):]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
