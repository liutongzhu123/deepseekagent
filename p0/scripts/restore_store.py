#!/usr/bin/env python3
"""Restore a ``backup_store.py`` snapshot into PostgreSQL and the object store.

    # inspect without writing anything
    .venv/bin/python scripts/restore_store.py --backup-dir /var/backups/cq-ai-p0 --verify-only

    # destructive restore; stop the API first
    systemctl stop cq-p0-api
    .venv/bin/python scripts/restore_store.py --backup-dir /var/backups/cq-ai-p0 --yes

The latest ``p0-store-*.json`` manifest is used unless ``--manifest`` names one.
Every archive and object is checked against its SHA-256 before use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.engine import make_url  # noqa: E402

from app.store import objects  # noqa: E402

CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_manifest(backup_dir: Path) -> Path:
    candidates = sorted(backup_dir.glob("p0-store-*.json"), reverse=True)
    if not candidates:
        raise RuntimeError(f"no p0-store-*.json manifest in {backup_dir}")
    return candidates[0]


def check_manifest(backup_dir: Path, manifest: dict) -> None:
    for key in ("database_dump", "objects"):
        item = manifest.get(key)
        if not item:
            continue
        path = backup_dir / item["name"]
        if not path.exists():
            raise RuntimeError(f"archive missing: {path}")
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"archive hash mismatch: {path} expected {item['sha256']} got {actual}")


def restore_database(backup_dir: Path, manifest: dict, url: str) -> None:
    item = manifest.get("database_dump")
    if not item:
        return
    parsed = make_url(url)
    query = dict(parsed.query)
    host = parsed.host or query.get("host")
    port = parsed.port or query.get("port")
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    argv = [
        "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
        "--dbname", parsed.database or "postgres",
        str(backup_dir / item["name"]),
    ]
    if parsed.username:
        argv += ["--username", parsed.username]
    if host:
        argv += ["--host", str(host)]
    if port:
        argv += ["--port", str(port)]
    completed = subprocess.run(argv, env=env, capture_output=True, text=True)
    # pg_restore returns 1 for warnings such as "role does not exist"; with
    # --no-owner those should not occur.  Treat only >=2 as failure.
    if completed.returncode >= 2:
        raise RuntimeError(f"pg_restore failed: {completed.stderr.strip()}")


def restore_objects(backup_dir: Path, manifest: dict) -> int:
    item = manifest.get("objects")
    if not item:
        return 0
    archive_path = backup_dir / item["name"]
    store = objects.get_object_store()
    restored = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest_member = archive.extractfile("manifest.json")
        if manifest_member is None:
            raise RuntimeError("object archive has no manifest.json")
        expected = {
            entry["key"]: entry
            for entry in json.loads(manifest_member.read().decode("utf-8")).get("objects", [])
        }
        for member in archive.getmembers():
            if not member.name.startswith("objects/"):
                continue
            key = member.name[len("objects/"):]
            handle = archive.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            record = expected.get(key)
            if record is None:
                raise RuntimeError(f"object not listed in manifest: {key}")
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise RuntimeError(f"object hash mismatch: {key}")
            store.put(key, data)
            restored += 1
    return restored


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--manifest", default="")
    parser.add_argument("--database-url", default=os.getenv("P0_DATABASE_URL", ""))
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-objects", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm the destructive restore")
    args = parser.parse_args(argv)
    backup_dir = Path(args.backup_dir).expanduser()
    try:
        manifest_path = (
            Path(args.manifest).expanduser() if args.manifest else latest_manifest(backup_dir)
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check_manifest(backup_dir, manifest)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"verify failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "database_dump": manifest.get("database_dump"),
                "objects": manifest.get("objects"),
                "verified": True,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    if args.verify_only:
        return 0
    if not args.yes:
        print("refusing to restore without --yes", file=sys.stderr)
        return 2
    try:
        if not args.skip_db:
            if not args.database_url:
                raise RuntimeError("P0_DATABASE_URL is required for a database restore")
            restore_database(backup_dir, manifest, args.database_url)
        restored_objects = 0 if args.skip_objects else restore_objects(backup_dir, manifest)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"restored_objects": restored_objects}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
