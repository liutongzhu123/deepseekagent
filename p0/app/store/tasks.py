"""PostgreSQL-backed background task queue.

The queue deliberately reuses the P1 data layer instead of adding Redis: the
``tasks`` table holds the state, and workers claim rows with
``SELECT ... FOR UPDATE SKIP LOCKED``.  Every worker has a lease, so a process
that dies mid-task is recovered on the next start instead of losing the job.

Stages are coarse (extract / ocr / parse / publish) because OCR itself is a
single blocking call; progress is 0–100 and always paired with a stage label.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from . import config, db

KINDS = {"standard_parse", "standard_publish"}
STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
_ID_RE = re.compile(r"^task_[0-9]{14}_[0-9a-f]{8}$")


def enabled() -> bool:
    value = os.getenv("P0_TASKS_ENABLED", "0").strip().lower()
    return config.postgres_enabled() and value not in {"0", "false", "off", "no", ""}


def concurrency() -> int:
    try:
        value = int(os.getenv("P0_TASKS_CONCURRENCY", "1"))
    except ValueError:
        value = 1
    return max(1, min(value, 4))


def lease_seconds() -> int:
    try:
        value = int(os.getenv("P0_TASKS_LEASE_SECONDS", "900"))
    except ValueError:
        value = 900
    return max(30, value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_id(task_id: str) -> str:
    if not _ID_RE.fullmatch(task_id or ""):
        raise KeyError(task_id)
    return task_id


def create(
    kind: str,
    payload: dict,
    *,
    created_by: str = "",
    project: str = "",
    max_attempts: int = 2,
) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unsupported task kind: {kind}")
    db.ensure_schema()
    now = _now()
    task_id = "task_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(4)
    row = {
        "id": task_id,
        "kind": kind,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "payload": payload,
        "result": None,
        "error": "",
        "attempts": 0,
        "max_attempts": max(1, int(max_attempts)),
        "cancel_requested": False,
        "created_by": created_by,
        "project": project,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "worker_id": "",
        "lease_until": None,
    }
    with db.engine().begin() as conn:
        from sqlalchemy import insert

        conn.execute(insert(db.tasks).values(**row))
    return public(row)


def set_payload(task_id: str, payload: dict) -> dict:
    _check_id(task_id)
    with db.engine().begin() as conn:
        result = conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == task_id)
            .values(payload=payload, updated_at=_now())
        )
        if not result.rowcount:
            raise KeyError(task_id)
    return get(task_id)


def get(task_id: str) -> dict:
    _check_id(task_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(select(db.tasks).where(db.tasks.c.id == task_id)).mappings().first()
    if row is None:
        raise KeyError(task_id)
    return dict(row)


def public(task_or_row: dict) -> dict:
    row = dict(task_or_row)
    payload = row.get("payload") or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "stage": row.get("stage", ""),
        "progress": int(row.get("progress", 0)),
        "result": row.get("result"),
        "error": row.get("error") or "",
        "attempts": int(row.get("attempts", 0)),
        "max_attempts": int(row.get("max_attempts", 1)),
        "cancel_requested": bool(row.get("cancel_requested", False)),
        "created_by": row.get("created_by", ""),
        "project": row.get("project", ""),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "file_name": str(payload.get("file_name", "")),
        "standard_id": str(payload.get("standard_id", "")),
    }


def _iso(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


def list_recent(*, created_by: str = "", limit: int = 10) -> list[dict]:
    db.ensure_schema()
    statement = select(db.tasks).order_by(db.tasks.c.created_at.desc()).limit(max(1, min(int(limit), 50)))
    if created_by:
        statement = statement.where(db.tasks.c.created_by == created_by)
    with db.engine().connect() as conn:
        return [public(dict(row)) for row in conn.execute(statement).mappings()]


def claim_next(worker_id: str) -> dict | None:
    db.ensure_schema()
    now = _now()
    lease = now + timedelta(seconds=lease_seconds())
    with db.engine().begin() as conn:
        row = conn.execute(
            select(db.tasks)
            .where(
                db.tasks.c.status == "queued",
                db.tasks.c.cancel_requested.is_(False),
            )
            .order_by(db.tasks.c.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).mappings().first()
        if row is None:
            return None
        conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == row["id"])
            .values(
                status="running",
                stage="starting",
                progress=0,
                attempts=int(row["attempts"]) + 1,
                started_at=now,
                updated_at=now,
                worker_id=worker_id,
                lease_until=lease,
                error="",
            )
        )
        claimed = dict(row)
    claimed.update(
        {
            "status": "running",
            "stage": "starting",
            "progress": 0,
            "attempts": int(row["attempts"]) + 1,
            "started_at": now,
            "updated_at": now,
            "worker_id": worker_id,
            "lease_until": lease,
            "error": "",
        }
    )
    return claimed


def heartbeat(task_id: str, stage: str, progress: int) -> None:
    _check_id(task_id)
    now = _now()
    with db.engine().begin() as conn:
        conn.execute(
            update(db.tasks)
            .where(
                db.tasks.c.id == task_id,
                db.tasks.c.status == "running",
            )
            .values(
                stage=stage[:64],
                progress=max(0, min(int(progress), 100)),
                updated_at=now,
                lease_until=now + timedelta(seconds=lease_seconds()),
            )
        )


def cancel_requested(task_id: str) -> bool:
    _check_id(task_id)
    with db.engine().connect() as conn:
        value = conn.execute(
            select(db.tasks.c.cancel_requested).where(db.tasks.c.id == task_id)
        ).scalar_one_or_none()
    return bool(value)


def finish(task_id: str, status: str, *, result: dict | None = None, error: str = "") -> dict:
    if status not in STATUSES - {"queued", "running"}:
        raise ValueError(f"unsupported final status: {status}")
    _check_id(task_id)
    now = _now()
    with db.engine().begin() as conn:
        conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == task_id)
            .values(
                status=status,
                stage=status,
                progress=100 if status == "succeeded" else 0,
                result=result,
                error=(error or "")[:8000],
                updated_at=now,
                finished_at=now,
                lease_until=None,
            )
        )
    return public(get(task_id))


def cancel(task_id: str) -> dict:
    _check_id(task_id)
    task = get(task_id)
    if task["status"] in {"succeeded", "failed", "cancelled"}:
        return public(task)
    now = _now()
    with db.engine().begin() as conn:
        if task["status"] == "queued":
            conn.execute(
                update(db.tasks)
                .where(db.tasks.c.id == task_id)
                .values(
                    status="cancelled",
                    cancel_requested=True,
                    stage="cancelled",
                    finished_at=now,
                    updated_at=now,
                    lease_until=None,
                )
            )
        else:
            conn.execute(
                update(db.tasks)
                .where(db.tasks.c.id == task_id)
                .values(cancel_requested=True, updated_at=now)
            )
    return public(get(task_id))


def retry(task_id: str) -> dict:
    _check_id(task_id)
    task = get(task_id)
    if task["status"] not in {"failed", "cancelled"}:
        raise ValueError("只有失败或已取消的任务可以重试")
    now = _now()
    with db.engine().begin() as conn:
        conn.execute(
            update(db.tasks)
            .where(db.tasks.c.id == task_id)
            .values(
                status="queued",
                stage="queued",
                progress=0,
                result=None,
                error="",
                attempts=0,
                cancel_requested=False,
                started_at=None,
                finished_at=None,
                worker_id="",
                lease_until=None,
                updated_at=now,
            )
        )
    return public(get(task_id))


def recover_stale() -> int:
    """Requeue running tasks whose worker lease expired (restart/crash)."""
    db.ensure_schema()
    now = _now()
    with db.engine().begin() as conn:
        cancelled = conn.execute(
            update(db.tasks)
            .where(
                db.tasks.c.status == "running",
                db.tasks.c.cancel_requested.is_(True),
                (db.tasks.c.lease_until.is_(None)) | (db.tasks.c.lease_until < now),
            )
            .values(
                status="cancelled",
                stage="cancelled",
                finished_at=now,
                worker_id="",
                lease_until=None,
                updated_at=now,
            )
        )
        requeued = conn.execute(
            update(db.tasks)
            .where(
                db.tasks.c.status == "running",
                db.tasks.c.cancel_requested.is_(False),
                (db.tasks.c.lease_until.is_(None)) | (db.tasks.c.lease_until < now),
            )
            .values(
                status="queued",
                stage="requeued",
                progress=0,
                worker_id="",
                lease_until=None,
                updated_at=now,
                error="",
            )
        )
    return int(cancelled.rowcount or 0) + int(requeued.rowcount or 0)


def stats() -> dict:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.tasks.c.status, func.count()).group_by(db.tasks.c.status)
        ).all()
    counts = {status: int(count) for status, count in rows}
    return {status: counts.get(status, 0) for status in ("queued", "running", "succeeded", "failed", "cancelled")}
