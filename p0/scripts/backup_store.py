#!/usr/bin/env python3
"""Daily backup for the P1 data layer: PostgreSQL dump + object-store archive.

The P0 file snapshot (``backup_data.py``) stays in place during the transition
because the JSON tree is the rollback target.  This script adds the other half:
a custom-format ``pg_dump`` and a checksummed tar of every object in MinIO / the
local object store, described by one manifest.

Usage::

    .venv/bin/python scripts/backup_store.py \
        --backup-dir /var/backups/cq-ai-p0 --keep 14

``pg_dump`` must be on PATH and P0_DATABASE_URL / P0_MINIO_* (or P0_OBJECT_DIR)
must be set exactly as for the API.  The archives contain customer material and
account hashes: mode 0600, off-machine copy, restricted access.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.engine import make_url  # noqa: E402

from app.store import config, objects  # noqa: E402

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_database(target: Path, url: str) -> dict:
    parsed = make_url(url)
    query = dict(parsed.query)
    host = parsed.host or query.get("host")
    port = parsed.port or query.get("port")
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    argv = [
        "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
        "--file", str(target),
        "--dbname", parsed.database or "postgres",
    ]
    if parsed.username:
        argv += ["--username", parsed.username]
    if host:
        argv += ["--host", str(host)]
    if port:
        argv += ["--port", str(port)]
    completed = subprocess.run(argv, env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {completed.stderr.strip()}")
    return {"name": target.name, "size": target.stat().st_size, "sha256": sha256_file(target)}


def dump_objects(target: Path) -> dict:
    store = objects.get_object_store()
    entries = store.list("")
    manifest = []
    with tarfile.open(target, "w:gz") as archive:
        for item in entries:
            data = store.get(item["key"])
            digest = hashlib.sha256(data).hexdigest()
            manifest.append({"key": item["key"], "size": len(data), "sha256": digest})
            info = tarfile.TarInfo(name=f"objects/{item['key']}")
            info.size = len(data)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
        payload = json.dumps({"objects": manifest}, ensure_ascii=False, indent=1).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return {
        "name": target.name,
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
        "object_count": len(manifest),
    }


def prune(backup_dir: Path, keep: int) -> list[str]:
    removed: list[str] = []
    for pattern in ("p0-pg-*.dump", "p0-objects-*.tar.gz", "p0-store-*.json"):
        files = sorted(backup_dir.glob(pattern), reverse=True)
        for path in files[keep:]:
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
    return removed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", default=os.getenv("P0_BACKUP_DIR", ""))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-objects", action="store_true")
    args = parser.parse_args(argv)

    backup_dir = Path(args.backup_dir or (Path(__file__).resolve().parents[1] / "backups"))
    url = args.database_url or os.getenv("P0_DATABASE_URL", "")
    if not args.skip_db and not url:
        print("P0_DATABASE_URL (or --database-url) is required", file=sys.stderr)
        return 2
    if not args.skip_objects:
        try:
            store = objects.get_object_store()
            object_count = len(store.list(""))
        except Exception as exc:  # noqa: BLE001 - report configuration errors
            print(f"object store unavailable: {exc}", file=sys.stderr)
            return 2
    else:
        object_count = 0
    if args.dry_run:
        print(json.dumps({"backup_dir": str(backup_dir), "objects": object_count}, ensure_ascii=False))
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": make_url(url).render_as_string(hide_password=True) if url else "",
        "object_store": config.object_store_mode(),
        "objects": None,
        "database_dump": None,
    }
    try:
        if not args.skip_db:
            dump_path = backup_dir / f"p0-pg-{stamp}.dump"
            manifest["database_dump"] = dump_database(dump_path, url)
        if not args.skip_objects:
            objects_path = backup_dir / f"p0-objects-{stamp}.tar.gz"
            manifest["objects"] = dump_objects(objects_path)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1

    manifest_path = backup_dir / f"p0-store-{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        manifest_path.chmod(0o600)
    except OSError:
        pass
    removed = prune(backup_dir, max(1, args.keep))
    print(json.dumps({**manifest, "manifest": str(manifest_path), "pruned": removed}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
