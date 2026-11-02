"""PostgreSQL repository for generated quote drafts and their workbooks."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, select, update

from . import config, db, objects

_ID_RE = re.compile(r"^q_[0-9]{14}_[0-9a-f]{6}$")
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return db.utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _check_id(quote_id: str) -> str:
    if not _ID_RE.fullmatch(quote_id or ""):
        raise KeyError(quote_id)
    return quote_id


def _mirror_dir(quote_id: str) -> Path:
    root = Path(os.getenv("P0_QUOTE_DIR", config.data_dir() / "quotes"))
    return root / _check_id(quote_id)


def _mirror_write(quote_id: str, name: str, data: bytes) -> None:
    if not config.mirror_enabled():
        return
    try:
        directory = _mirror_dir(quote_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        pass


def _content_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return _XLSX
    if suffix == ".xls":
        return "application/vnd.ms-excel"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".png", ".jpg", ".jpeg"}:
        return "image/jpeg" if suffix != ".png" else "image/png"
    return "application/octet-stream"


def create(meta: dict, files: dict[str, bytes]) -> dict:
    _check_id(meta["quote_id"])
    db.ensure_schema()
    row = {
        "id": meta["quote_id"],
        "project": str(meta.get("project", "")),
        "created_by": str(meta.get("created_by", "")),
        "data": meta,
        "created_at": _as_datetime(meta.get("created_at")),
    }
    with db.engine().begin() as conn:
        conn.execute(insert(db.quotes).values(**row))
        for name, data in files.items():
            objects.upsert_file(
                conn,
                owner_type="quote",
                owner_id=meta["quote_id"],
                name=name,
                data=data,
                content_type=_content_type(name),
            )
    _mirror_write(meta["quote_id"], "meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    for name, data in files.items():
        _mirror_write(meta["quote_id"], name, data)
    return meta


def load(quote_id: str) -> dict:
    _check_id(quote_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        row = conn.execute(select(db.quotes.c.data).where(db.quotes.c.id == quote_id)).first()
    return dict(row[0]) if row and row[0] else {}


def read_file(quote_id: str, name: str) -> bytes:
    _check_id(quote_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        record = objects.find_file(conn, "quote", quote_id, name)
        if record is None:
            raise KeyError(name)
        _, data = objects.read_file(conn, record["id"])
        return data


def file_count(quote_id: str) -> int:
    _check_id(quote_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        return len(conn.execute(
            select(db.files.c.id).where(
                db.files.c.owner_type == "quote", db.files.c.owner_id == quote_id
            )
        ).all())


def import_record(meta: dict, files: dict[str, bytes]) -> None:
    """Idempotent import used by ``scripts/migrate_to_postgres.py``."""
    _check_id(meta["quote_id"])
    db.ensure_schema()
    row = {
        "id": meta["quote_id"],
        "project": str(meta.get("project", "")),
        "created_by": str(meta.get("created_by", "")),
        "data": meta,
        "created_at": _as_datetime(meta.get("created_at")),
    }
    with db.engine().begin() as conn:
        existing = conn.execute(
            select(db.quotes.c.id).where(db.quotes.c.id == meta["quote_id"])
        ).first()
        if existing:
            values = {key: value for key, value in row.items() if key != "id"}
            conn.execute(update(db.quotes).where(db.quotes.c.id == meta["quote_id"]).values(**values))
        else:
            conn.execute(insert(db.quotes).values(**row))
        for name, data in files.items():
            objects.upsert_file(
                conn,
                owner_type="quote",
                owner_id=meta["quote_id"],
                name=name,
                data=data,
                content_type=_content_type(name),
            )
    _mirror_write(meta["quote_id"], "meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    for name, data in files.items():
        _mirror_write(meta["quote_id"], name, data)


def all_ids() -> list[str]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        return [str(row[0]) for row in conn.execute(select(db.quotes.c.id).order_by(db.quotes.c.id)).all()]


def file_names(quote_id: str) -> list[str]:
    _check_id(quote_id)
    db.ensure_schema()
    with db.engine().connect() as conn:
        return [
            str(row[0])
            for row in conn.execute(
                select(db.files.c.name).where(
                    db.files.c.owner_type == "quote",
                    db.files.c.owner_id == quote_id,
                ).order_by(db.files.c.name)
            ).all()
        ]


def files_for(quote_id: str) -> dict[str, bytes]:
    _check_id(quote_id)
    return {name: read_file(quote_id, name) for name in file_names(quote_id)}
