"""Runtime configuration for the P1 data layer.

The file-backed P0 store remains the default so an existing install keeps
working after an upgrade.  ``P0_STORE=postgres`` switches users, audit events,
standards (with their source/export objects) and quotes to PostgreSQL + an
S3-compatible object store (MinIO in production).  Every switch is read at call
time; tests flip the environment without re-importing the application.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
VALID_STORES = ("json", "postgres")
VALID_OBJECT_STORES = ("local", "minio")


def data_dir() -> Path:
    return Path(os.getenv("P0_DATA_DIR", ROOT / "data"))


def store_mode() -> str:
    value = os.getenv("P0_STORE", "json").strip().lower() or "json"
    if value not in VALID_STORES:
        raise RuntimeError(f"P0_STORE must be one of {VALID_STORES}, got {value!r}")
    return value


def postgres_enabled() -> bool:
    return store_mode() == "postgres"


def database_url() -> str:
    value = os.getenv("P0_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("P0_STORE=postgres requires P0_DATABASE_URL")
    return value


def object_store_mode() -> str:
    value = os.getenv("P0_OBJECT_STORE", "local").strip().lower() or "local"
    if value not in VALID_OBJECT_STORES:
        raise RuntimeError(
            f"P0_OBJECT_STORE must be one of {VALID_OBJECT_STORES}, got {value!r}"
        )
    return value


def object_dir() -> Path:
    """Root of the local object store used in development and tests."""
    return Path(os.getenv("P0_OBJECT_DIR", data_dir() / "objects"))


def minio_settings() -> dict:
    return {
        "endpoint": os.getenv("P0_MINIO_ENDPOINT", "").strip(),
        "access_key": os.getenv("P0_MINIO_ACCESS_KEY", "").strip(),
        "secret_key": os.getenv("P0_MINIO_SECRET_KEY", "").strip(),
        "bucket": os.getenv("P0_MINIO_BUCKET", "cq-p0").strip() or "cq-p0",
        "secure": os.getenv("P0_MINIO_SECURE", "0").lower() in {"1", "true", "yes"},
        "region": os.getenv("P0_MINIO_REGION", "").strip() or None,
    }


def mirror_enabled() -> bool:
    """Keep the JSON files current so a rollback to file mode is instant.

    Dual-writing is on by default while the data layer is introduced; operators
    can turn it off with ``P0_STORE_MIRROR=0`` after the JSON tree is retired.
    """
    return os.getenv("P0_STORE_MIRROR", "1").lower() not in {"0", "false", "off"}


def reset_caches() -> None:
    """Drop cached engines/object stores after an environment change (tests)."""
    from . import db, objects

    db.reset_engine()
    objects.reset_object_store()
