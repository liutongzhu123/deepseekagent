#!/usr/bin/env python3
"""Create a verified snapshot of the P0 file data directory.

P0 keeps users, audit events, parsed standards and quotes as JSON / JSONL /
files under ``P0_DATA_DIR``.  Before the P1 PostgreSQL migration this script is
the file-data safety net: it writes one checksummed ``tar.gz`` that
``restore_data.py`` can verify and restore.

The archive is self-contained: ``manifest.json`` (inside the tarball) records
the size, SHA-256 and mode of every file, and ``<archive>.sha256`` records the
archive hash.  Files are read twice (hash pass, copy pass); if a file changes
size in between, the snapshot aborts instead of producing a mixed copy.

The snapshot is crash-consistent per file (the service writes files with atomic
renames), but it is not a multi-file transaction.  Schedule it off-peak and
check the manifest on restore.  Backups contain customer data and secrets, so
the archive is created with mode 0600.

Usage::

    python scripts/backup_data.py
    python scripts/backup_data.py --data-dir /opt/cq-ai-p0/data \
        --backup-dir /var/backups/cq-ai-p0 --keep 14
    python scripts/backup_data.py --dry-run

Defaults come from ``P0_DATA_DIR`` / ``P0_BACKUP_DIR`` or ``p0/data`` and
``p0/backups``.  See ``docs/p1-ci-backup/README.md`` for the daily timer, the
off-machine copy and the restore drill.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import socket
import stat
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 1
MANIFEST_NAME = "manifest.json"
ARCHIVE_PREFIX = "p0-data-"
DEFAULT_KEEP = 14
CHUNK_SIZE = 1024 * 1024
SKIP_NAMES = {".DS_Store"}
SKIP_SUFFIXES = (".tmp",)
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class SnapshotError(RuntimeError):
    """A snapshot could not be produced safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_data_dir() -> Path:
    env = os.getenv("P0_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[1] / "data"


def default_backup_dir() -> Path:
    env = os.getenv("P0_BACKUP_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[1] / "backups"


def collect_files(data_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(relative POSIX path, absolute path)`` pairs for regular files.

    Symlinks and special files are skipped (with a warning) so a snapshot can
    never quietly copy data from outside the data directory, and in-progress
    ``*.tmp`` files are skipped because the service replaces them atomically.
    """
    files: list[tuple[str, Path]] = []
    for root, dirs, names in os.walk(data_dir, followlinks=False):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            candidate = root_path / name
            if candidate.is_symlink():
                print(
                    f"warning: skipping symlinked directory {candidate}",
                    file=sys.stderr,
                )
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            path = root_path / name
            if path.is_symlink():
                print(f"warning: skipping symlink {path}", file=sys.stderr)
                continue
            if not path.is_file():
                # Sockets, FIFOs and devices are not restorable data.
                print(f"warning: skipping special file {path}", file=sys.stderr)
                continue
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            files.append((path.relative_to(data_dir).as_posix(), path))
    files.sort(key=lambda item: item[0])
    return files


def build_manifest(data_dir: Path, files: list[tuple[str, Path]]) -> dict:
    entries: list[dict] = []
    total_bytes = 0
    for relative, path in files:
        try:
            metadata = path.stat()
            digest = sha256_file(path)
        except OSError as exc:
            raise SnapshotError(f"cannot read {path}: {exc}") from exc
        entries.append(
            {
                "path": relative,
                "size": metadata.st_size,
                "sha256": digest,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        )
        total_bytes += metadata.st_size
    return {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_dir": str(data_dir),
        "hostname": socket.gethostname(),
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "files": entries,
    }


def write_archive(archive: Path, manifest: dict, files: list[tuple[str, Path]]) -> str:
    """Write the tarball atomically and return its SHA-256."""
    temporary = archive.with_name(archive.name + ".part")
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=1, sort_keys=True
    ).encode("utf-8")
    started = time.time()
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as tar:
            header = tarfile.TarInfo(MANIFEST_NAME)
            header.size = len(manifest_bytes)
            header.mode = 0o600
            header.mtime = int(started)
            tar.addfile(header, io.BytesIO(manifest_bytes))

            by_path = dict(files)
            for entry in manifest["files"]:
                path = by_path[entry["path"]]
                with path.open("rb") as handle:
                    info = tar.gettarinfo(str(path), arcname=f"data/{entry['path']}")
                    if info.size != entry["size"]:
                        raise SnapshotError(
                            f"{path} changed while the snapshot was being written; retry"
                        )
                    info.mode = entry["mode"]
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, handle)
        temporary.chmod(0o600)
        digest = sha256_file(temporary)
        os.replace(temporary, archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return digest


def prune_snapshots(backup_dir: Path, keep: int) -> list[Path]:
    """Delete the oldest snapshots above ``keep``; return the removed paths."""
    snapshots = sorted(
        backup_dir.glob(f"{ARCHIVE_PREFIX}*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for old in snapshots[keep:]:
        old.unlink(missing_ok=True)
        old.with_name(old.name + ".sha256").unlink(missing_ok=True)
        removed.append(old)
    return removed


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Snapshot the P0 data directory into a verified tar.gz.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="data directory to snapshot (default: $P0_DATA_DIR or p0/data)",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=default_backup_dir(),
        help="where to write snapshots (default: $P0_BACKUP_DIR or p0/backups)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"snapshots to retain in the backup directory (default: {DEFAULT_KEEP})",
    )
    parser.add_argument("--label", default="", help="optional filename label")
    parser.add_argument(
        "--dry-run", action="store_true", help="list files without writing a snapshot"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.umask(0o077)
    if args.keep < 1:
        print("error: --keep must be at least 1", file=sys.stderr)
        return 2
    if args.label and not LABEL_RE.fullmatch(args.label):
        print("error: --label may only contain letters, digits, '.', '_' and '-'", file=sys.stderr)
        return 2

    data_dir = args.data_dir.expanduser().resolve()
    backup_dir = args.backup_dir.expanduser().resolve()
    if not data_dir.is_dir():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    try:
        files = collect_files(data_dir)
        if args.dry_run:
            total = sum(path.stat().st_size for _, path in files)
            print(f"data_dir={data_dir}")
            print(f"files={len(files)} bytes={total}")
            for relative, _ in files:
                print(relative)
            return 0
        if not files:
            print(f"warning: no restorable files under {data_dir}", file=sys.stderr)

        manifest = build_manifest(data_dir, files)
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label = f"-{args.label}" if args.label else ""
        archive = backup_dir / f"{ARCHIVE_PREFIX}{stamp}{label}.tar.gz"
        if archive.exists():
            raise SnapshotError(f"snapshot already exists: {archive}")

        digest = write_archive(archive, manifest, files)
        sidecar = archive.with_name(archive.name + ".sha256")
        sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        sidecar.chmod(0o600)
        removed = prune_snapshots(backup_dir, args.keep)
    except (OSError, SnapshotError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"snapshot={archive}")
    print(f"sha256={digest}")
    print(f"files={manifest['file_count']} bytes={manifest['total_bytes']}")
    print(f"retention={args.keep}")
    if removed:
        print(f"pruned={len(removed)}")
        for old in removed:
            print(f"pruned_file={old}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
