"""PostgreSQL schema and engine for the P1 data layer.

The schema stores the canonical P0 records as JSON/JSONB and repeats the fields
the API filters on (owners, project, publication state, audit time) as indexed
columns.  Parsed clause rows also get a narrow table so later reporting can
query them without opening the JSON document; the JSON document stays the
source of truth for the API contract.

``SQLAlchemy`` is used for the connection/pooling and dialect handling, but the
repository modules use Core statements rather than an ORM: the existing P0 code
works with plain dictionaries and should keep doing so.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.postgresql import JSONB

from . import config

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("username", String(64), primary_key=True),
    Column("role", String(16), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("data", JSON_TYPE, nullable=False),
    # Kept as a separate column so a future auth audit can verify hashes
    # without reading the whole document.
    Column("password_hash", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

standards = Table(
    "standards",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("revision", Integer, nullable=False, default=0),
    Column("project", String(128), nullable=False, default=""),
    Column("created_by", String(64), nullable=False, default=""),
    Column("standard_no", String(128), nullable=False, default=""),
    Column("name", String(512), nullable=False, default=""),
    Column("published", Boolean, nullable=False, default=False),
    Column("source_file_id", String(64)),
    Column("data", JSON_TYPE, nullable=False),
    Column("kb", JSON_TYPE),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_standards_project", standards.c.project)
Index("ix_standards_created_at", standards.c.created_at)

standard_versions = Table(
    "standard_versions",
    metadata,
    Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
    Column("standard_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("data", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("standard_id", "version", name="uq_standard_version"),
)

standard_items = Table(
    "standard_items",
    metadata,
    Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
    Column("standard_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("clause", String(128), nullable=False, default=""),
    Column("name", String(512), nullable=False, default=""),
    Column("requirement", Text, nullable=False, default=""),
    Column("method", Text, nullable=False, default=""),
    Column("data", JSON_TYPE, nullable=False),
)
Index("ix_standard_items_standard", standard_items.c.standard_id, standard_items.c.version)

quotes = Table(
    "quotes",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("project", String(128), nullable=False, default=""),
    Column("created_by", String(64), nullable=False, default=""),
    Column("data", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_quotes_project", quotes.c.project)
Index("ix_quotes_created_at", quotes.c.created_at)

report_audits = Table(
    "report_audits",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("batch_id", String(48), nullable=False, default=""),
    Column("project", String(128), nullable=False, default=""),
    Column("created_by", String(64), nullable=False, default=""),
    Column("file_name", String(512), nullable=False, default=""),
    Column("template", String(64), nullable=False, default=""),
    Column("summary", JSON_TYPE, nullable=False),
    Column("source_file_id", String(64)),
    Column("data", JSON_TYPE, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_report_audits_project", report_audits.c.project)
Index("ix_report_audits_created_at", report_audits.c.created_at)
Index("ix_report_audits_batch", report_audits.c.batch_id)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("user_name", String(64), nullable=False, default=""),
    Column("role", String(16), nullable=False, default=""),
    Column("event", String(128), nullable=False, default=""),
    Column("method", String(16), nullable=False, default=""),
    Column("path", String(512), nullable=False, default=""),
    Column("status", Integer),
    Column("data", JSON_TYPE, nullable=False),
    # Migration provenance: reruns skip rows already imported from a file.
    Column("source", String(128)),
    Column("source_seq", Integer),
    UniqueConstraint("source", "source_seq", name="uq_audit_source_seq"),
)
Index("ix_audit_at", audit_events.c.at)
Index("ix_audit_user", audit_events.c.user_name)
Index("ix_audit_event", audit_events.c.event)

tasks = Table(
    "tasks",
    metadata,
    Column("id", String(48), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False, default="queued"),
    Column("stage", String(64), nullable=False, default=""),
    Column("progress", Integer, nullable=False, default=0),
    Column("payload", JSON_TYPE, nullable=False),
    Column("result", JSON_TYPE),
    Column("error", Text, nullable=False, default=""),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=2),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("created_by", String(64), nullable=False, default=""),
    Column("project", String(128), nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("worker_id", String(64), nullable=False, default=""),
    Column("lease_until", DateTime(timezone=True)),
)
Index("ix_tasks_status", tasks.c.status, tasks.c.created_at)
Index("ix_tasks_creator", tasks.c.created_by, tasks.c.created_at)

files = Table(
    "files",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("owner_type", String(32), nullable=False),
    Column("owner_id", String(64), nullable=False),
    Column("name", String(512), nullable=False),
    Column("object_key", String(1024), nullable=False),
    Column("size", BigInteger, nullable=False, default=0),
    Column("sha256", String(64), nullable=False, default=""),
    Column("content_type", String(128), nullable=False, default="application/octet-stream"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_files_owner", files.c.owner_type, files.c.owner_id)
UniqueConstraint("owner_type", "owner_id", "name", name="uq_file_owner_name")

_LOCK = threading.RLock()
_ENGINE: Engine | None = None
_ENGINE_URL: str | None = None
_SCHEMA_READY = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def reset_engine() -> None:
    global _ENGINE, _ENGINE_URL, _SCHEMA_READY
    with _LOCK:
        if _ENGINE is not None:
            _ENGINE.dispose()
        _ENGINE = None
        _ENGINE_URL = None
        _SCHEMA_READY = False


def engine() -> Engine:
    global _ENGINE, _ENGINE_URL
    url = config.database_url()
    with _LOCK:
        if _ENGINE is None or _ENGINE_URL != url:
            if _ENGINE is not None:
                _ENGINE.dispose()
            options: dict = {"future": True, "pool_pre_ping": True}
            if not url.startswith("sqlite"):
                options["pool_size"] = int(os_env("P0_DB_POOL_SIZE", "5"))
                options["max_overflow"] = int(os_env("P0_DB_MAX_OVERFLOW", "10"))
            _ENGINE = create_engine(url, **options)
            _ENGINE_URL = url
        return _ENGINE


def os_env(name: str, default: str) -> str:
    import os

    return os.getenv(name, default)


def ensure_schema(eng: Engine | None = None) -> None:
    global _SCHEMA_READY
    with _LOCK:
        if _SCHEMA_READY and eng is None:
            return
        metadata.create_all(eng or engine(), checkfirst=True)
        if eng is None:
            _SCHEMA_READY = True


def reset_schema() -> None:
    """Drop every P1 table (tests only)."""
    global _SCHEMA_READY
    metadata.drop_all(engine(), checkfirst=True)
    with _LOCK:
        _SCHEMA_READY = False
    ensure_schema()
