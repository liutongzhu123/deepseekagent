"""Session authentication, RBAC, user store and full audit logging.

P1-S1 turns the P0 login gate into a multi-user identity base:

* four roles (admin / reviewer / sales / viewer) behind one permission matrix;
* account policy, per-account lockout, admin reset and activation control;
* project grants on users, used by the API layer for row-level isolation;
* an OIDC/SSO configuration seam (status only; local login stays the default);
* append-only JSONL audit with filtering, CSV export and retention pruning.

The dependency footprint deliberately stays at the Python standard library.  The
JSON user store remains the interim storage until the P1 PostgreSQL work lands;
``current_user``/``require_permission`` are the seams the API uses.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
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
AUDIT_RETENTION_DAYS = int(os.getenv("P0_AUDIT_RETENTION_DAYS", "180"))

COOKIE_NAME = "p0_session"
SESSION_TTL_SECONDS = int(os.getenv("P0_SESSION_TTL", str(8 * 3600)))
PBKDF2_ITERATIONS = 240_000
ROLES = ("admin", "reviewer", "sales", "viewer")
ROLE_LABELS = {
    "admin": "管理员",
    "reviewer": "复核人",
    "sales": "销售",
    "viewer": "只读",
}
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60

# action -> roles allowed.  ``admin`` is always allowed and is listed only for
# documentation.  Endpoints call ``require_permission`` with these action names.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "rules:read": ("admin", "reviewer", "sales", "viewer"),
    "report:audit": ("admin", "reviewer"),
    "standard:read": ("admin", "reviewer", "sales", "viewer"),
    "standard:parse": ("admin", "reviewer"),
    "standard:review": ("admin", "reviewer"),
    "standard:export": ("admin", "reviewer", "sales", "viewer"),
    "standard:project": ("admin",),
    "standard:publish": ("admin", "reviewer"),
    "standard:ask": ("admin", "reviewer", "sales", "viewer"),
    "quote:parse": ("admin", "sales"),
    "quote:generate": ("admin", "sales"),
    "quote:download": ("admin", "sales"),
    "kb:rebuild": ("admin",),
    "kb:stats": ("admin", "reviewer", "sales", "viewer"),
    "user:manage": ("admin",),
    "audit:read": ("admin",),
    "audit:export": ("admin",),
    "system:status": ("admin", "reviewer", "sales", "viewer"),
}

_LOCK = threading.RLock()
_SECRET: bytes | None = None
_RATE_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}
_AUDIT_LOCK = threading.RLock()
_LAST_PRUNE_DATE: str | None = None


class AccountLocked(Exception):
    """Raised when an account is temporarily locked after failed logins."""

    def __init__(self, until: str, seconds: int):
        super().__init__(f"账号已锁定至 {until}")
        self.until = until
        self.seconds = seconds


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
# Password policy
# ---------------------------------------------------------------------------
def validate_password(password: str) -> None:
    """Reject weak passwords before hashing or persisting them."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"密码不超过 {MAX_PASSWORD_LENGTH} 位")
    classes = sum(
        (
            any(char.isalpha() for char in password),
            any(char.isdigit() for char in password),
            any(not char.isalnum() for char in password),
        )
    )
    if classes < 2:
        raise ValueError("密码需包含字母、数字、符号中的至少两类")


def hash_password(password: str) -> str:
    validate_password(password)
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
# Roles, projects and permissions
# ---------------------------------------------------------------------------
def has_permission(user: dict | None, action: str) -> bool:
    if not user:
        return False
    role = user.get("role", "")
    if role == "admin":
        return True
    return role in PERMISSIONS.get(action, ())


def normalize_projects(value: object) -> list[str]:
    """Normalize a project grant list from JSON, a comma string or a UI field."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = re.split(r"[,，\s]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ValueError("项目授权必须是字符串列表")
    out: list[str] = []
    for item in items:
        project = str(item).strip()
        if not project:
            continue
        if len(project) > 64:
            raise ValueError("项目标识不能超过 64 位")
        if project not in out:
            out.append(project)
    if len(out) > 50:
        raise ValueError("项目授权最多 50 个")
    return out


def _grants(user: dict) -> list[str]:
    raw = user.get("projects") or []
    if isinstance(raw, str):
        raw = normalize_projects(raw)
    return [str(item).strip() for item in raw if str(item).strip()]


def is_restricted(user: dict) -> bool:
    """A user with explicit project grants (and not ``*``) is project-scoped."""
    if user.get("role") == "admin":
        return False
    grants = _grants(user)
    return bool(grants) and "*" not in grants


def can_access_project(user: dict, project: str, created_by: str = "") -> bool:
    """Row-level project check shared by standards and quotes.

    Users without project grants keep P0's unrestricted behaviour so existing
    installs do not lose access on upgrade.  An unassigned resource is visible
    to its creator, and to unrestricted users, but not to other restricted users.
    """
    if user.get("role") == "admin":
        return True
    grants = _grants(user)
    if not grants or "*" in grants:
        return True
    project = (project or "").strip()
    if project:
        return project in grants
    return bool(created_by) and created_by == user.get("username")


def can_create_in_project(user: dict, project: str) -> bool:
    """Restricted users must create records inside a project they can access."""
    if user.get("role") == "admin":
        return True
    grants = _grants(user)
    if not grants or "*" in grants:
        return True
    project = (project or "").strip()
    return bool(project) and project in grants


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
    department: str = "",
    projects: object = None,
) -> dict:
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValueError("用户名不能为空且不超过 64 位")
    if role not in ROLES:
        raise ValueError(f"角色必须是 {', '.join(ROLES)}")
    normalized = normalize_projects(projects)
    with _LOCK:
        users = load_users()
        if any(user.get("username", "").lower() == username.lower() for user in users):
            raise ValueError("用户已存在")
        user = {
            "username": username,
            "display_name": display_name or username,
            "role": role,
            "active": True,
            "department": (department or "").strip()[:128],
            "projects": normalized,
            "auth_provider": "local",
            "session_epoch": 0,
            "failed_logins": 0,
            "locked_until": "",
            "last_login_at": "",
            "password_hash": hash_password(password),
            "created_at": _now(),
            "updated_at": _now(),
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
                user["session_epoch"] = int(user.get("session_epoch", 0)) + 1
                user["failed_logins"] = 0
                user["locked_until"] = ""
                user["updated_at"] = _now()
                save_users(users)
                return
    raise KeyError(username)


def reset_password(username: str, new_password: str | None = None) -> dict:
    """Reset an account; generate a temporary password when none is supplied."""
    generated = not new_password
    # Guarantee at least three character classes so the temporary password
    # always satisfies the policy (token_urlsafe can occasionally be letters only).
    password = new_password or ("Aa1-" + secrets.token_urlsafe(12))
    with _LOCK:
        users = load_users()
        for user in users:
            if user.get("username", "").lower() == username.lower():
                user["password_hash"] = hash_password(password)
                user["session_epoch"] = int(user.get("session_epoch", 0)) + 1
                user["failed_logins"] = 0
                user["locked_until"] = ""
                user["updated_at"] = _now()
                save_users(users)
                return {
                    "username": user["username"],
                    "password": password,
                    "generated": generated,
                }
    raise KeyError(username)


def _count_active_admins(users: list[dict]) -> int:
    return sum(
        1
        for user in users
        if user.get("role") == "admin" and user.get("active", True)
    )


def update_user(
    username: str,
    *,
    role: str | None = None,
    active: bool | None = None,
    display_name: str | None = None,
    department: str | None = None,
    projects: object = None,
) -> dict:
    if role is not None and role not in ROLES:
        raise ValueError(f"角色必须是 {', '.join(ROLES)}")
    normalized = normalize_projects(projects) if projects is not None else None
    with _LOCK:
        users = load_users()
        target = None
        for user in users:
            if user.get("username", "").lower() == username.lower():
                target = user
                break
        if target is None:
            raise KeyError(username)
        losing_admin = (
            target.get("role") == "admin"
            and target.get("active", True)
            and ((role is not None and role != "admin") or active is False)
        )
        if losing_admin and _count_active_admins(users) <= 1:
            raise ValueError("至少保留一个启用的管理员")
        if role is not None:
            target["role"] = role
        if active is not None:
            target["active"] = bool(active)
        if display_name is not None:
            target["display_name"] = display_name or target["username"]
        if department is not None:
            target["department"] = department.strip()[:128]
        if normalized is not None:
            target["projects"] = normalized
        target["updated_at"] = _now()
        save_users(users)
        return public_user(target)


def public_user(user: dict) -> dict:
    return {
        "username": user.get("username", ""),
        "display_name": user.get("display_name") or user.get("username", ""),
        "role": user.get("role", "viewer"),
        "active": bool(user.get("active", True)),
        "department": user.get("department", ""),
        "projects": list(user.get("projects") or []),
        "restricted": is_restricted(user),
        "auth_provider": user.get("auth_provider", "local"),
        "last_login_at": user.get("last_login_at", ""),
        "created_at": user.get("created_at", ""),
        "updated_at": user.get("updated_at", ""),
        "locked": lock_remaining_seconds(user) > 0,
    }


# ---------------------------------------------------------------------------
# Login throttling, per-account lockout and SSO seam
# ---------------------------------------------------------------------------
def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def lock_remaining_seconds(user: dict) -> int:
    until = _parse_time(user.get("locked_until", ""))
    if until is None:
        return 0
    remaining = int((until - datetime.now(timezone.utc)).total_seconds())
    return max(0, remaining)


def _record_failure(username: str) -> None:
    with _LOCK:
        users = load_users()
        for user in users:
            if user.get("username", "").lower() != username.lower():
                continue
            failures = int(user.get("failed_logins", 0)) + 1
            user["failed_logins"] = failures
            if failures >= MAX_FAILED_LOGINS:
                until = datetime.now(timezone.utc) + timedelta(seconds=LOCKOUT_SECONDS)
                user["locked_until"] = until.isoformat(timespec="seconds")
            user["updated_at"] = _now()
            save_users(users)
            return


def _record_success(username: str) -> None:
    with _LOCK:
        users = load_users()
        for user in users:
            if user.get("username", "").lower() != username.lower():
                continue
            user["failed_logins"] = 0
            user["locked_until"] = ""
            user["last_login_at"] = _now()
            save_users(users)
            return


def authenticate(username: str, password: str) -> dict | None:
    user = find_user(username)
    if not user or not user.get("active", True):
        return None
    remaining = lock_remaining_seconds(user)
    if remaining > 0:
        raise AccountLocked(user.get("locked_until", ""), remaining)
    if not verify_password(password, user.get("password_hash", "")):
        _record_failure(user["username"])
        return None
    _record_success(user["username"])
    refreshed = find_user(user["username"]) or user
    return public_user(refreshed)


def provider_status() -> dict:
    """Describe available identity providers without exposing secrets."""
    issuer = os.getenv("P0_OIDC_ISSUER", "").strip()
    client_id = os.getenv("P0_OIDC_CLIENT_ID", "").strip()
    return {
        "providers": [{"id": "local", "label": "账号密码", "enabled": True}],
        "oidc": {
            "configured": bool(issuer and client_id),
            "status": "reserved",
            "issuer_configured": bool(issuer),
        },
    }


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
    raw = find_user(user["username"]) or user
    payload = {
        "u": user["username"],
        "r": user["role"],
        "v": int(raw.get("session_epoch", 0)),
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
        if int(payload.get("v", 0)) != int(user.get("session_epoch", 0)):
            return None
        if lock_remaining_seconds(user) > 0:
            return None
        return public_user(user)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Audit log: append, prune, query, export
# ---------------------------------------------------------------------------
def _read_audit() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def prune_audit(retention_days: int | None = None) -> int:
    """Drop audit lines older than the retention window; return removed count."""
    days = AUDIT_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days <= 0 or not AUDIT_PATH.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[str] = []
    removed = 0
    with _AUDIT_LOCK:
        try:
            lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            at = _parse_time(str(entry.get("at", "")))
            if at is not None and at < cutoff:
                removed += 1
            else:
                kept.append(line)
        if removed:
            try:
                _atomic_write(
                    AUDIT_PATH,
                    "\n".join(kept) + ("\n" if kept else ""),
                    mode=0o640,
                )
            except OSError:
                return 0
    return removed


def _maybe_prune() -> None:
    global _LAST_PRUNE_DATE
    today = datetime.now(timezone.utc).date().isoformat()
    if _LAST_PRUNE_DATE == today:
        return
    _LAST_PRUNE_DATE = today
    prune_audit()


def audit(entry: dict) -> None:
    record = {"at": _now(), **entry}
    try:
        with _AUDIT_LOCK:
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            _maybe_prune()
    except OSError:
        # Auditing must never take the business endpoint down.
        pass


def _time_bound(value: str, end: bool) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            parsed = datetime.fromisoformat(value)
            if end:
                parsed = parsed + timedelta(days=1) - timedelta(seconds=1)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def filter_audit(
    *,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    lower = _time_bound(date_from, end=False)
    upper = _time_bound(date_to, end=True)
    wanted_user = (user or "").strip().lower()
    wanted_role = (role or "").strip().lower()
    wanted_event = (event or "").strip()
    wanted_method = (method or "").strip().upper()
    wanted_path = (path or "").strip()

    matched: list[dict] = []
    for entry in reversed(_read_audit()):
        if wanted_user and str(entry.get("user", "")).lower() != wanted_user:
            continue
        if wanted_role and str(entry.get("role", "")).lower() != wanted_role:
            continue
        if wanted_event and str(entry.get("event", "")) != wanted_event:
            continue
        if wanted_method and str(entry.get("method", "")).upper() != wanted_method:
            continue
        if wanted_path and wanted_path not in str(entry.get("path", "")):
            continue
        if status is not None and entry.get("status") != status:
            continue
        at = _parse_time(str(entry.get("at", "")))
        if lower is not None and (at is None or at < lower):
            continue
        if upper is not None and (at is None or at > upper):
            continue
        matched.append(entry)
    return matched


def query_audit(
    *,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    matched = filter_audit(
        user=user,
        role=role,
        event=event,
        method=method,
        path=path,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    total = len(matched)
    return {
        "events": matched[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def recent_audit(limit: int = 100) -> list[dict]:
    return query_audit(limit=limit)["events"]


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    # Prevent spreadsheet formula injection from user-controlled audit fields.
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def audit_csv(entries: list[dict]) -> str:
    fields = ("at", "user", "role", "event", "action", "method", "path", "status", "ms", "ip")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for entry in entries:
        writer.writerow({field: _csv_cell(entry.get(field, "")) for field in fields})
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Login throttling (per IP+username, in memory)
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


def bootstrap() -> dict:
    """Ensure the auth store exists; return metadata about the bootstrap admin.

    An explicit ``P0_ADMIN_PASSWORD`` wins and must satisfy the P1 password
    policy.  Otherwise a strong random password is generated once and written to
    a 0600 file so the operator can retrieve it without it appearing in process
    listings or the repository.
    """
    _maybe_prune()
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
            create_user(
                username,
                password,
                role="admin",
                display_name="系统管理员",
                projects=["*"],
            )
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
