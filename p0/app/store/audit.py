"""PostgreSQL audit-event repository.

Audit rows stay append-only.  The JSONL mirror is kept current while the file
store is still the rollback target; it is rewritten only by retention pruning.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select

from . import config, db


def _mirror_path() -> Path:
    override = os.getenv("P0_AUDIT_LOG", "").strip()
    if override:
        return Path(override)
    return config.data_dir() / "audit" / "audit.jsonl"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return db.utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mirror_append(record: dict) -> None:
    if not config.mirror_enabled():
        return
    path = _mirror_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def append(record: dict) -> None:
    db.ensure_schema()
    at = _as_datetime(record.get("at"))
    row = {
        "at": at,
        "user_name": str(record.get("user", "")),
        "role": str(record.get("role", "")),
        "event": str(record.get("event", "")),
        "method": str(record.get("method", "")),
        "path": str(record.get("path", "")),
        "status": record.get("status") if isinstance(record.get("status"), int) else None,
        "data": record,
    }
    with db.engine().begin() as conn:
        conn.execute(insert(db.audit_events).values(**row))
    _mirror_append(record)


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


def _conditions(
    *,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
) -> list:
    clauses = []
    if (user or "").strip():
        clauses.append(func.lower(db.audit_events.c.user_name) == user.strip().lower())
    if (role or "").strip():
        clauses.append(func.lower(db.audit_events.c.role) == role.strip().lower())
    if (event or "").strip():
        clauses.append(db.audit_events.c.event == event.strip())
    if (method or "").strip():
        clauses.append(func.upper(db.audit_events.c.method) == method.strip().upper())
    if (path or "").strip():
        clauses.append(db.audit_events.c.path.like(f"%{path.strip()}%"))
    if status is not None:
        clauses.append(db.audit_events.c.status == int(status))
    lower = _time_bound(date_from, end=False)
    if lower is not None:
        clauses.append(db.audit_events.c.at >= lower)
    upper = _time_bound(date_to, end=True)
    if upper is not None:
        clauses.append(db.audit_events.c.at <= upper)
    return clauses


def filter_entries(
    *,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    db.ensure_schema()
    statement = select(db.audit_events.c.data).order_by(
        db.audit_events.c.at.desc(), db.audit_events.c.id.desc()
    )
    clauses = _conditions(
        user=user, role=role, event=event, method=method, path=path,
        status=status, date_from=date_from, date_to=date_to,
    )
    if clauses:
        statement = statement.where(*clauses)
    if limit is not None:
        statement = statement.limit(int(limit)).offset(max(0, int(offset or 0)))
    with db.engine().connect() as conn:
        return [dict(row) for row in conn.execute(statement).scalars() if row]


def iter_entries(**filters):
    """Yield filtered events without loading the whole table (streaming CSV)."""
    db.ensure_schema()
    statement = select(db.audit_events.c.data).order_by(
        db.audit_events.c.at.desc(), db.audit_events.c.id.desc()
    )
    clauses = _conditions(
        user=filters.get("user", ""),
        role=filters.get("role", ""),
        event=filters.get("event", ""),
        method=filters.get("method", ""),
        path=filters.get("path", ""),
        status=filters.get("status"),
        date_from=filters.get("date_from", ""),
        date_to=filters.get("date_to", ""),
    )
    if clauses:
        statement = statement.where(*clauses)
    with db.engine().connect() as conn:
        result = conn.execution_options(stream_results=True).execute(statement)
        for row in result.scalars():
            if row:
                yield dict(row)


def count_entries(**filters) -> int:
    db.ensure_schema()
    statement = select(func.count()).select_from(db.audit_events)
    clauses = _conditions(
        user=filters.get("user", ""),
        role=filters.get("role", ""),
        event=filters.get("event", ""),
        method=filters.get("method", ""),
        path=filters.get("path", ""),
        status=filters.get("status"),
        date_from=filters.get("date_from", ""),
        date_to=filters.get("date_to", ""),
    )
    if clauses:
        statement = statement.where(*clauses)
    with db.engine().connect() as conn:
        return int(conn.execute(statement).scalar_one())


def query(**filters) -> dict:
    limit = max(1, min(int(filters.get("limit", 100)), 500))
    offset = max(0, int(filters.get("offset", 0)))
    base = {key: value for key, value in filters.items() if key not in {"limit", "offset"}}
    events = filter_entries(limit=limit, offset=offset, **base)
    return {
        "events": events,
        "total": count_entries(**base),
        "limit": limit,
        "offset": offset,
    }


def prune(retention_days: int) -> int:
    db.ensure_schema()
    if retention_days <= 0:
        return 0
    cutoff = db.utcnow() - timedelta(days=int(retention_days))
    with db.engine().begin() as conn:
        removed = int(
            conn.execute(
                delete(db.audit_events).where(db.audit_events.c.at < cutoff)
            ).rowcount
            or 0
        )
    _mirror_prune(cutoff)
    return removed


def _mirror_prune(cutoff: datetime) -> None:
    if not config.mirror_enabled():
        return
    path = _mirror_path()
    if not path.exists():
        return
    kept: list[str] = []
    removed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if _as_datetime(entry.get("at")) < cutoff:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        try:
            tmp.chmod(0o640)
        except OSError:
            pass
        tmp.replace(path)
    except OSError:
        pass


def import_entry(record: dict, source: str, source_seq: int) -> bool:
    """Insert a migrated JSONL event; provenance makes reruns idempotent."""
    db.ensure_schema()
    at = _as_datetime(record.get("at"))
    row = {
        "at": at,
        "user_name": str(record.get("user", "")),
        "role": str(record.get("role", "")),
        "event": str(record.get("event", "")),
        "method": str(record.get("method", "")),
        "path": str(record.get("path", "")),
        "status": record.get("status") if isinstance(record.get("status"), int) else None,
        "data": record,
        "source": source,
        "source_seq": int(source_seq),
    }
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with db.engine().begin() as conn:
        statement = pg_insert(db.audit_events).values(**row)
        statement = statement.on_conflict_do_nothing(
            index_elements=["source", "source_seq"]
        )
        result = conn.execute(statement)
        return bool(result.rowcount)


def migrated_count(source: str = "audit.jsonl") -> int:
    db.ensure_schema()
    with db.engine().connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(db.audit_events).where(
                    db.audit_events.c.source == source
                )
            ).scalar_one()
        )


def all_entries() -> list[dict]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.audit_events.c.data).order_by(
                db.audit_events.c.at, db.audit_events.c.id
            )
        ).scalars()
        return [dict(row) for row in rows if row]


def migrated_entries(source: str = "audit.jsonl") -> dict[int, dict]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.audit_events.c.source_seq, db.audit_events.c.data)
            .where(db.audit_events.c.source == source)
            .order_by(db.audit_events.c.source_seq)
        ).all()
    return {int(seq): dict(data) for seq, data in rows if seq is not None}
