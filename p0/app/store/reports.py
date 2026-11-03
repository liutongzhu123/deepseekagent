"""PostgreSQL repository for persisted report reviews.

Each audit keeps the full finding document in ``report_audits.data`` (the API
contract), repeats the fields the workbench filters on (project, owner, batch,
audit time) as indexed columns, and stores the original ``.docx`` in the object
store so a reviewer can reopen the evidence.  The JSON tree is mirrored during
the transition, matching the standards/quotes repositories.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, select

from . import config, db, objects

_ID_RE = re.compile(r"^rp_[0-9]{14}_[0-9a-f]{6}$")
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return db.utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value or "")


def _check_id(report_id: str) -> str:
    if not _ID_RE.fullmatch(report_id or ""):
        raise KeyError(report_id)
    return report_id


def _mirror_dir(report_id: str) -> Path:
    root = Path(os.getenv("P0_REPORT_DIR", config.data_dir() / "reports"))
    return root / _check_id(report_id)


def _mirror_write(report_id: str, name: str, data: bytes) -> None:
    if not config.mirror_enabled():
        return
    try:
        directory = _mirror_dir(report_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        pass


def _mirror_record(record: dict, result: dict, source: bytes | None) -> None:
    meta = {key: value for key, value in record.items() if key != "result"}
    _mirror_write(record["id"], "meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    _mirror_write(record["id"], "result.json", json.dumps(result, ensure_ascii=False, indent=1).encode("utf-8"))
    if source is not None:
        _mirror_write(record["id"], "source.docx", source)


def create(record: dict, result: dict, source: bytes | None = None) -> dict:
    """Store one review plus its original document; return the full record."""
    _check_id(record["id"])
    db.ensure_schema()
    stored = {**record, "result": result}
    source_file_id: str | None = None
    with db.engine().begin() as conn:
        if source is not None:
            file_record = objects.upsert_file(
                conn,
                owner_type="report",
                owner_id=record["id"],
                name="source.docx",
                data=source,
                content_type=_DOCX,
            )
            source_file_id = file_record["id"]
        conn.execute(
            insert(db.report_audits).values(
                id=record["id"],
                batch_id=str(record.get("batch_id", "")),
                project=str(record.get("project", "")),
                created_by=str(record.get("created_by", "")),
                file_name=str(record.get("file_name", "")),
                template=str(record.get("template", "")),
                summary=record.get("summary") or {},
                source_file_id=source_file_id,
                data=stored,
                created_at=_as_datetime(record.get("created_at")),
            )
        )
    _mirror_record(stored, result, source)
    return stored


def load(report_id: str) -> dict:
    _check_id(report_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.report_audits.c.data).where(db.report_audits.c.id == report_id)
        ).first()
    if row is None or not row[0]:
        raise KeyError(report_id)
    return dict(row[0])


def list_reports() -> list[dict]:
    """Lightweight rows for the workbench list, newest first."""
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(
                db.report_audits.c.id,
                db.report_audits.c.batch_id,
                db.report_audits.c.project,
                db.report_audits.c.created_by,
                db.report_audits.c.file_name,
                db.report_audits.c.template,
                db.report_audits.c.summary,
                db.report_audits.c.created_at,
            ).order_by(db.report_audits.c.created_at.desc())
        ).mappings()
        out = [
            {
                "id": row["id"],
                "batch_id": row["batch_id"] or "",
                "project": row["project"] or "",
                "created_by": row["created_by"] or "",
                "file_name": row["file_name"] or "",
                "template": row["template"] or "",
                "summary": row["summary"] or {},
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ]
    return out


def read_source(report_id: str) -> bytes:
    _check_id(report_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.report_audits.c.source_file_id).where(db.report_audits.c.id == report_id)
        ).first()
        if row is None or not row[0]:
            raise KeyError(report_id)
        _, data = objects.read_file(conn, row[0])
    return data
