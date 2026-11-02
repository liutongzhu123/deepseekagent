"""S3-compatible object store seam for standard sources and generated files.

Production uses MinIO; development and the existing server can keep objects in a
directory (``P0_OBJECT_STORE=local``) without changing the database layer.  The
``files`` table always records the object key, size and SHA-256, so the two
backends are interchangeable and the migration can reconcile them.
"""
from __future__ import annotations

import hashlib
import io
import re
import secrets
import threading
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from . import config, db

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_STORE_LOCK = threading.Lock()
_STORE: "ObjectStore | None" = None
_STORE_KEY: tuple | None = None


class ObjectStoreError(RuntimeError):
    pass


def _safe_part(value: str, default: str = "file") -> str:
    cleaned = _UNSAFE.sub("-", (value or "").strip()).strip("-.")
    return cleaned[:120] or default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str = "") -> list[dict]: ...


class LocalObjectStore:
    """Directory-backed object store used by tests and small installs."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ObjectStoreError(f"unsafe object key: {key!r}")
        return candidate

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ObjectStoreError(f"object not found: {key}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str = "") -> list[dict]:
        base = self._path(prefix) if prefix else self.root
        if base.is_file():
            candidates = [base]
        elif base.exists():
            candidates = [path for path in base.rglob("*") if path.is_file()]
        else:
            candidates = []
        out: list[dict] = []
        for path in sorted(candidates):
            relative = path.relative_to(self.root).as_posix()
            out.append({"key": relative, "size": path.stat().st_size})
        return out


class MinioObjectStore:
    """MinIO/S3 backend.  ``minio`` is imported lazily so local mode stays light."""

    def __init__(self, settings: dict):
        if not settings["endpoint"] or not settings["access_key"] or not settings["secret_key"]:
            raise ObjectStoreError(
                "P0_OBJECT_STORE=minio requires P0_MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY"
            )
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise ObjectStoreError("python package 'minio' is not installed") from exc
        self.bucket = settings["bucket"]
        self.client = Minio(
            settings["endpoint"],
            access_key=settings["access_key"],
            secret_key=settings["secret_key"],
            secure=settings["secure"],
            region=settings["region"],
        )
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    def get(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except Exception:  # noqa: BLE001 - SDK raises several not-found types
            return False

    def list(self, prefix: str = "") -> list[dict]:
        return [
            {"key": item.object_name, "size": item.size}
            for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True)
        ]


def get_object_store() -> ObjectStore:
    global _STORE, _STORE_KEY
    mode = config.object_store_mode()
    key = (mode, str(config.object_dir()), tuple(config.minio_settings().items()))
    with _STORE_LOCK:
        if _STORE is None or _STORE_KEY != key:
            if mode == "minio":
                _STORE = MinioObjectStore(config.minio_settings())
            else:
                _STORE = LocalObjectStore(config.object_dir())
            _STORE_KEY = key
        return _STORE


def reset_object_store() -> None:
    global _STORE, _STORE_KEY
    with _STORE_LOCK:
        _STORE = None
        _STORE_KEY = None


def object_key(owner_type: str, owner_id: str, name: str, file_id: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix and len(suffix) <= 12 and re.fullmatch(r"\.[a-z0-9.]+", suffix):
        safe_name = _safe_part(Path(name).stem) + suffix
    else:
        safe_name = _safe_part(name)
    return f"{_safe_part(owner_type)}/{_safe_part(owner_id)}/{_safe_part(file_id)}-{safe_name}"


def upsert_file(
    conn: Connection,
    *,
    owner_type: str,
    owner_id: str,
    name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> dict:
    """Store bytes and replace any previous file with the same owner/name."""
    existing = conn.execute(
        select(db.files).where(
            db.files.c.owner_type == owner_type,
            db.files.c.owner_id == owner_id,
            db.files.c.name == name,
        )
    ).mappings().all()
    store = get_object_store()
    for old in existing:
        if old["object_key"]:
            try:
                store.delete(old["object_key"])
            except ObjectStoreError:
                pass
        conn.execute(delete(db.files).where(db.files.c.id == old["id"]))

    file_id = "file_" + secrets.token_hex(8)
    key = object_key(owner_type, owner_id, name, file_id)
    store.put(key, data, content_type)
    record = {
        "id": file_id,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "name": name,
        "object_key": key,
        "size": len(data),
        "sha256": sha256_bytes(data),
        "content_type": content_type,
        "created_at": db.utcnow(),
    }
    conn.execute(insert(db.files).values(**record))
    return record


def read_file(conn: Connection, file_id: str) -> tuple[dict, bytes]:
    row = conn.execute(select(db.files).where(db.files.c.id == file_id)).mappings().first()
    if row is None:
        raise KeyError(file_id)
    record = dict(row)
    return record, get_object_store().get(record["object_key"])


def find_file(conn: Connection, owner_type: str, owner_id: str, name: str) -> dict | None:
    row = conn.execute(
        select(db.files).where(
            db.files.c.owner_type == owner_type,
            db.files.c.owner_id == owner_id,
            db.files.c.name == name,
        )
    ).mappings().first()
    return dict(row) if row else None


def delete_files(conn: Connection, owner_type: str, owner_id: str) -> int:
    rows = conn.execute(
        select(db.files).where(
            db.files.c.owner_type == owner_type,
            db.files.c.owner_id == owner_id,
        )
    ).mappings().all()
    store = get_object_store()
    for row in rows:
        try:
            store.delete(row["object_key"])
        except ObjectStoreError:
            pass
    result = conn.execute(
        delete(db.files).where(
            db.files.c.owner_type == owner_type,
            db.files.c.owner_id == owner_id,
        )
    )
    return int(result.rowcount or 0)
