"""PostgreSQL repository for parsed standards, revisions and published KBs.

Each standard keeps its full API document in ``standards.data``.  Every save
also refreshes a ``standard_versions`` snapshot (the review chain) and the
clause rows in ``standard_items``.  Source PDFs, exports and the local ``kb``
document live in the object store; ``files`` records their keys and hashes.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, insert, select, update

from . import config, db, objects

_ID_RE = re.compile(r"^std_[0-9]{14}_[0-9a-f]{6}$")
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PDF = "application/pdf"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return db.utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _check_id(standard_id: str) -> str:
    if not _ID_RE.fullmatch(standard_id or ""):
        raise KeyError(standard_id)
    return standard_id


def _mirror_dir(standard_id: str) -> Path:
    root = Path(os.getenv("P0_STANDARD_DIR", config.data_dir() / "standards"))
    return root / _check_id(standard_id)


def _mirror_write(standard_id: str, name: str, data: bytes) -> None:
    if not config.mirror_enabled():
        return
    try:
        directory = _mirror_dir(standard_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        pass


def _mirror_record(record: dict, kb: dict | None = None) -> None:
    _mirror_write(
        record["id"], "result.json", json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")
    )
    if kb is not None:
        _mirror_write(record["id"], "kb.json", json.dumps(kb, ensure_ascii=False, indent=1).encode("utf-8"))


def _row(record: dict) -> dict:
    meta = record.get("meta") or {}
    return {
        "id": record["id"],
        "revision": int(record.get("revision", 0)),
        "project": str(meta.get("project", "")),
        "created_by": str(meta.get("created_by", "")),
        "standard_no": str(meta.get("standard_no", "")),
        "name": str(meta.get("name", "")),
        "published": bool(record.get("published", False)),
        "source_file_id": None,
        "data": record,
        "created_at": _as_datetime(record.get("created_at")),
        "updated_at": _as_datetime(record.get("updated_at")),
    }


def _write_version(conn, record: dict) -> None:
    version = int(record.get("revision", 0))
    existing = conn.execute(
        select(db.standard_versions.c.id).where(
            db.standard_versions.c.standard_id == record["id"],
            db.standard_versions.c.version == version,
        )
    ).first()
    values = {
        "standard_id": record["id"],
        "version": version,
        "data": record,
        "created_at": _as_datetime(record.get("updated_at") or record.get("created_at")),
    }
    if existing:
        conn.execute(
            update(db.standard_versions)
            .where(db.standard_versions.c.id == existing[0])
            .values(data=values["data"], created_at=values["created_at"])
        )
    else:
        conn.execute(insert(db.standard_versions).values(**values))
    conn.execute(
        delete(db.standard_items).where(
            db.standard_items.c.standard_id == record["id"],
            db.standard_items.c.version == version,
        )
    )
    items = record.get("items") or []
    if items:
        conn.execute(
            insert(db.standard_items),
            [
                {
                    "standard_id": record["id"],
                    "version": version,
                    "clause": str(item.get("clause", "")),
                    "name": str(item.get("name", "")),
                    "requirement": str(item.get("requirement", "")),
                    "method": str(item.get("method", "")),
                    "data": item,
                }
                for item in items
            ],
        )


def create(record: dict, source_bytes: bytes, file_name: str = "source.pdf") -> dict:
    db.ensure_schema()
    row = _row(record)
    with db.engine().begin() as conn:
        file_record = objects.upsert_file(
            conn,
            owner_type="standard",
            owner_id=record["id"],
            name="source.pdf",
            data=source_bytes,
            content_type=_PDF,
        )
        row["source_file_id"] = file_record["id"]
        conn.execute(insert(db.standards).values(**row))
        _write_version(conn, record)
    _mirror_record(record)
    _mirror_write(record["id"], "source.pdf", source_bytes)
    return record


def save(record: dict, kb: dict | None = None) -> dict:
    db.ensure_schema()
    _check_id(record["id"])
    row = _row(record)
    with db.engine().begin() as conn:
        current = conn.execute(
            select(db.standards.c.source_file_id).where(db.standards.c.id == record["id"])
        ).first()
        if current is None:
            raise KeyError(record["id"])
        values = {key: value for key, value in row.items() if key not in {"id", "source_file_id"}}
        if kb is not None:
            values["kb"] = kb
        conn.execute(update(db.standards).where(db.standards.c.id == record["id"]).values(**values))
        _write_version(conn, record)
    _mirror_record(record, kb)
    return record


def load(standard_id: str) -> dict:
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.standards.c.data).where(db.standards.c.id == standard_id)
        ).first()
    if row is None:
        raise KeyError(standard_id)
    return dict(row[0])


def list_standards() -> list[dict]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.standards.c.data).order_by(db.standards.c.created_at.desc())
        ).scalars()
        records = [dict(row) for row in rows if row]
    out: list[dict] = []
    for record in records:
        meta = record.get("meta") or {}
        out.append(
            {
                "id": record["id"],
                "standard_no": meta.get("standard_no", ""),
                "name": meta.get("name", ""),
                "items": (record.get("summary") or {}).get("items", 0),
                "pages": meta.get("pages", 0),
                "published": record.get("published", False),
                "revision": record.get("revision", 0),
                "created_at": record.get("created_at", ""),
                "project": meta.get("project", ""),
                "created_by": meta.get("created_by", ""),
            }
        )
    return out


def get_kb(standard_id: str) -> dict | None:
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.standards.c.kb).where(db.standards.c.id == standard_id)
        ).first()
    return dict(row[0]) if row and row[0] else None


def published_kbs(standard_id: str | None = None) -> list[dict]:
    db.ensure_schema()
    statement = (
        select(db.standards.c.kb)
        .where(db.standards.c.kb.is_not(None))
        .order_by(db.standards.c.created_at.desc())
    )
    if standard_id:
        statement = statement.where(db.standards.c.id == _check_id(standard_id))
    with db.engine().connect() as conn:
        return [dict(row) for row in conn.execute(statement).scalars() if row]


def save_export(standard_id: str, data: bytes) -> dict:
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().begin() as conn:
        file_record = objects.upsert_file(
            conn,
            owner_type="standard",
            owner_id=standard_id,
            name="export.xlsx",
            data=data,
            content_type=_XLSX,
        )
    _mirror_write(standard_id, "export.xlsx", data)
    return file_record


def read_export(standard_id: str) -> bytes | None:
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        record = objects.find_file(conn, "standard", standard_id, "export.xlsx")
        if record is None:
            return None
        _, data = objects.read_file(conn, record["id"])
        return data


def import_record(
    record: dict,
    source_bytes: bytes,
    kb: dict | None = None,
    export_bytes: bytes | None = None,
) -> None:
    """Idempotent import used by ``scripts/migrate_to_postgres.py``."""
    _check_id(record["id"])
    db.ensure_schema()
    row = _row(record)
    with db.engine().begin() as conn:
        source = objects.upsert_file(
            conn,
            owner_type="standard",
            owner_id=record["id"],
            name="source.pdf",
            data=source_bytes,
            content_type=_PDF,
        )
        row["source_file_id"] = source["id"]
        existing = conn.execute(
            select(db.standards.c.id).where(db.standards.c.id == record["id"])
        ).first()
        values = dict(row)
        if kb is not None:
            values["kb"] = kb
        if existing:
            values.pop("id", None)
            values.pop("source_file_id", None)
            conn.execute(
                update(db.standards).where(db.standards.c.id == record["id"]).values(**values)
            )
        else:
            conn.execute(insert(db.standards).values(**values))
        _write_version(conn, record)
        if export_bytes is not None:
            objects.upsert_file(
                conn,
                owner_type="standard",
                owner_id=record["id"],
                name="export.xlsx",
                data=export_bytes,
                content_type=_XLSX,
            )
    _mirror_record(record, kb)
    _mirror_write(record["id"], "source.pdf", source_bytes)
    if export_bytes is not None:
        _mirror_write(record["id"], "export.xlsx", export_bytes)


def items_for(standard_id: str, version: int | None = None) -> list[dict]:
    _check_id(standard_id)
    db.ensure_schema()
    statement = select(db.standard_items.c.data).where(
        db.standard_items.c.standard_id == standard_id
    )
    if version is not None:
        statement = statement.where(db.standard_items.c.version == int(version))
    statement = statement.order_by(db.standard_items.c.id)
    with db.engine().connect() as conn:
        return [dict(row) for row in conn.execute(statement).scalars() if row]


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value or "")


def list_versions(standard_id: str) -> list[dict]:
    """Revision snapshots for the review/rollback panel, newest first."""
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        current_row = conn.execute(
            select(db.standards.c.data).where(db.standards.c.id == standard_id)
        ).first()
        if current_row is None or not current_row[0]:
            raise KeyError(standard_id)
        current = int((current_row[0] or {}).get("revision", 0))
        rows = conn.execute(
            select(
                db.standard_versions.c.version,
                db.standard_versions.c.data,
                db.standard_versions.c.created_at,
            )
            .where(db.standard_versions.c.standard_id == standard_id)
            .order_by(db.standard_versions.c.version.desc())
        ).mappings().all()
    versions: list[dict] = []
    for row in rows:
        data = row["data"] or {}
        version = int(row["version"])
        versions.append(
            {
                "version": version,
                "created_at": _iso(row["created_at"]),
                "reviewer": str(data.get("reviewer", "")),
                "items": len(data.get("items") or []),
                "current": version == current,
                "rollbackable": version != current,
            }
        )
    return versions


def version_record(standard_id: str, version: int) -> dict:
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.standard_versions.c.data).where(
                db.standard_versions.c.standard_id == standard_id,
                db.standard_versions.c.version == int(version),
            )
        ).first()
    if row is None or not row[0]:
        raise KeyError(f"{standard_id}@{version}")
    return dict(row[0])


def clear_kb(standard_id: str) -> None:
    """Drop the local KB document while keeping the review record."""
    _check_id(standard_id)
    db.ensure_schema()
    with db.engine().begin() as conn:
        conn.execute(
            update(db.standards)
            .where(db.standards.c.id == standard_id)
            .values(kb=None)
        )
    try:
        (_mirror_dir(standard_id) / "kb.json").unlink(missing_ok=True)
    except OSError:
        pass


