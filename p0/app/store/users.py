"""PostgreSQL user repository.

The P0 user document is kept intact in ``users.data`` (JSONB); the indexed
columns exist for listing, lookups and future admin queries.  Saves replace the
whole small user list inside one transaction, matching the existing
``save_users(list)`` contract used by the authentication module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from . import config, db

MIRROR_MODE = 0o600


def _mirror_path() -> Path:
    override = os.getenv("P0_AUTH_USERS", "").strip()
    if override:
        return Path(override)
    return config.data_dir() / "auth" / "users.json"


def _mirror(users: list[dict]) -> None:
    if not config.mirror_enabled():
        return
    path = _mirror_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"users": users}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        try:
            tmp.chmod(MIRROR_MODE)
        except OSError:
            pass
        tmp.replace(path)
    except OSError:
        # The database is authoritative; a stale mirror only delays rollback.
        pass


def load_users() -> list[dict]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.users.c.data).order_by(db.users.c.username)).scalars()
        return [dict(row) for row in rows if row]


def save_users(users: list[dict]) -> None:
    db.ensure_schema()
    rows = [
        {
            "username": user["username"],
            "role": user.get("role", ""),
            "active": bool(user.get("active", True)),
            "data": user,
            "password_hash": user.get("password_hash", ""),
            "updated_at": db.utcnow(),
        }
        for user in users
        if user.get("username")
    ]
    identity_rows = []
    for user in users:
        for identity in user.get("oidc_identities") or []:
            if isinstance(identity, dict):
                identity_rows.append({
                    "username": user["username"],
                    "issuer": str(identity.get("issuer", "")),
                    "subject": str(identity.get("subject", "")),
                })
    try:
        with db.engine().begin() as conn:
            conn.execute(delete(db.oidc_identities))
            conn.execute(delete(db.users))
            if rows:
                conn.execute(insert(db.users), rows)
            if identity_rows:
                conn.execute(insert(db.oidc_identities), identity_rows)
    except IntegrityError as exc:
        raise ValueError("OIDC 身份绑定重复，未保存账号变更") from exc
    _mirror(users)


def count_users() -> int:
    db.ensure_schema()
    with db.engine().connect() as conn:
        return len(conn.execute(select(db.users.c.username)).all())
