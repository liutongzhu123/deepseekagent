"""Durable per-session logout revocation for signed P0 session cookies."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .store import config, db

_LOCK = threading.RLock()
_flock = fcntl.flock


def _json_path() -> Path:
    data_dir = Path(os.getenv("P0_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return data_dir / "auth" / "session_revocations.json"


def _with_file_lock():
    """Open the stable lock inode used to serialize JSON updates across workers."""
    path = _json_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise RuntimeError("session lock file has unexpected owner")
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "a+")
    except Exception:
        os.close(descriptor)
        raise
    try:
        _flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        handle.close()
        raise
    return path, handle


def _read_file(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, int)
            for key, value in raw.items()
        ):
            raise ValueError("invalid revocation ledger")
        return raw
    except (OSError, ValueError, json.JSONDecodeError):
        # A damaged ledger must not silently re-enable logged-out credentials.
        raise RuntimeError("session revocation ledger unavailable") from None


def _write_file(path: Path, rows: dict[str, int]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def revoke(digest: str, expires_at: int) -> None:
    if config.postgres_enabled():
        db.ensure_schema()
        from .store import db as schema

        now = datetime.now(timezone.utc)
        expiry = datetime.fromtimestamp(expires_at, timezone.utc)
        with schema.engine().begin() as conn:
            conn.execute(delete(schema.session_revocations).where(
                schema.session_revocations.c.expires_at <= now
            ))
            table = schema.session_revocations
            values = {"token_digest": digest, "expires_at": expiry, "revoked_at": now}
            if schema.engine().dialect.name == "sqlite":
                statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(
                    index_elements=[table.c.token_digest]
                )
            else:
                statement = pg_insert(table).values(**values).on_conflict_do_nothing(
                    index_elements=[table.c.token_digest]
                )
            conn.execute(statement)
        return

    with _LOCK:
        path, handle = _with_file_lock()
        try:
            now = int(datetime.now(timezone.utc).timestamp())
            rows = {key: value for key, value in _read_file(path).items() if value > now}
            rows[digest] = expires_at
            _write_file(path, rows)
        finally:
            _flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def is_revoked(digest: str, now: int) -> bool:
    if config.postgres_enabled():
        db.ensure_schema()
        table = db.session_revocations
        instant = datetime.fromtimestamp(now, timezone.utc)
        with db.engine().connect() as conn:
            return conn.execute(
                select(table.c.token_digest).where(
                    table.c.token_digest == digest,
                    table.c.expires_at > instant,
                )
            ).first() is not None

    with _LOCK:
        path, handle = _with_file_lock()
        try:
            return _read_file(path).get(digest, 0) > now
        finally:
            _flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
