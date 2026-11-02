#!/usr/bin/env python3
"""Verify and restore a P0 data snapshot created by ``backup_data.py``.

Restore is deliberately conservative:

* the archive hash is checked against ``<archive>.sha256`` when present;
* every file listed in the embedded ``manifest.json`` is hashed, both inside
  the archive and again after extraction;
* nothing outside the snapshot's ``data/`` tree is extracted (PEP 706
  ``filter="data"`` rejects absolute paths, ``..`` escapes, links and devices);
* a non-empty target directory is never overwritten silently: without
  ``--force`` the command fails, with ``--force`` the old directory is moved
  aside to ``<data-dir>.pre-restore-<timestamp>`` and can be moved back.

Usage::

    python scripts/restore_data.py snapshot.tar.gz --verify-only
    python scripts/restore_data.py snapshot.tar.gz --data-dir /tmp/p0-restore-check
    systemctl stop cq-p0-api
    python scripts/restore_data.py /var/backups/cq-ai-p0/p0-data-...tar.gz \
        --data-dir /opt/cq-ai-p0/data --force
    systemctl start cq-p0-api

Restoring production data is an operator action: run the drill in
``docs/p1-ci-backup/README.md`` first and keep the pre-restore directory until
``/health`` and the business smoke test pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT = 1
MANIFEST_NAME = "manifest.json"
DATA_PREFIX = "data/"
CHUNK_SIZE = 1024 * 1024


class RestoreError(RuntimeError):
    """The snapshot is invalid or cannot be restored safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    handle = tar.extractfile(member)
    if handle is None:
        raise RestoreError(f"cannot read {member.name} from archive")
    digest = hashlib.sha256()
    with handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_data_dir() -> Path:
    env = os.getenv("P0_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[1] / "data"


def validate_relative_path(relative: str) -> None:
    candidate = PurePosixPath(relative)
    if not relative or candidate.is_absolute():
        raise RestoreError(f"unsafe file path in manifest: {relative!r}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise RestoreError(f"unsafe file path in manifest: {relative!r}")
    if "\\" in relative:
        raise RestoreError(f"unsafe file path in manifest: {relative!r}")


def verify_archive(archive: Path) -> dict:
    """Check the sidecar hash and every file hash; return the manifest."""
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.exists():
        fields = sidecar.read_text(encoding="utf-8").split()
        if not fields:
            raise RestoreError(f"empty checksum file: {sidecar}")
        expected = fields[0].lower()
        actual = sha256_file(archive)
        if actual != expected:
            raise RestoreError(
                f"archive hash mismatch: expected {expected}, got {actual}"
            )
    else:
        print(
            f"warning: {sidecar.name} not found; checking the embedded manifest only",
            file=sys.stderr,
        )

    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                manifest_file = tar.extractfile(MANIFEST_NAME)
            except KeyError as exc:
                raise RestoreError(f"{archive} has no {MANIFEST_NAME}") from exc
            if manifest_file is None:
                raise RestoreError(f"cannot read {MANIFEST_NAME} from {archive}")
            with manifest_file:
                manifest = json.load(manifest_file)
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise RestoreError(f"unsupported snapshot format: {manifest.get('format')!r}")

            expected_members: set[str] = set()
            for entry in manifest.get("files", []):
                relative = entry.get("path", "")
                validate_relative_path(relative)
                member_name = DATA_PREFIX + relative
                try:
                    member = tar.getmember(member_name)
                except KeyError as exc:
                    raise RestoreError(f"missing file in archive: {member_name}") from exc
                if not member.isfile():
                    raise RestoreError(f"not a regular file in archive: {member_name}")
                if member.size != entry.get("size"):
                    raise RestoreError(f"size mismatch for {member_name}")
                if sha256_member(tar, member) != entry.get("sha256"):
                    raise RestoreError(f"hash mismatch for {member_name}")
                expected_members.add(member_name)

            for member in tar.getmembers():
                if not member.name.startswith(DATA_PREFIX):
                    continue
                if member.name not in expected_members:
                    raise RestoreError(f"unexpected file in archive: {member.name}")
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise RestoreError(f"cannot read {archive}: {exc}") from exc
    return manifest


def apply_modes(root: Path, manifest: dict) -> None:
    """Restore file modes from the manifest and keep directories private."""
    for directory, _, _ in os.walk(root):
        os.chmod(directory, 0o700)
    for entry in manifest.get("files", []):
        path = root / entry["path"]
        mode = int(entry.get("mode", 0o600)) & 0o777
        if not mode:
            mode = 0o600
        path.chmod(mode)


def verify_extracted(root: Path, manifest: dict) -> None:
    for entry in manifest.get("files", []):
        path = root / entry["path"]
        if not path.is_file():
            raise RestoreError(f"file missing after extraction: {entry['path']}")
        if path.stat().st_size != entry.get("size"):
            raise RestoreError(f"size mismatch after extraction: {entry['path']}")
        if sha256_file(path) != entry.get("sha256"):
            raise RestoreError(f"hash mismatch after extraction: {entry['path']}")


def extract_to_staging(archive: Path, staging: Path) -> Path:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name.startswith(DATA_PREFIX)]
            try:
                tar.extractall(staging, members=members, filter="data")
            except TypeError as exc:  # pragma: no cover - Python < 3.12
                raise RestoreError(
                    "restore requires Python 3.12+ (tarfile extraction filter)"
                ) from exc
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"cannot extract {archive}: {exc}") from exc
    root = staging / "data"
    if not root.is_dir():
        raise RestoreError("archive contains no data/ directory")
    return root


def restore(archive: Path, target: Path, force: bool, manifest: dict) -> Path | None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = target.parent / f".{target.name}.restore-{stamp}"
    if staging.exists():
        raise RestoreError(f"staging directory already exists: {staging}")
    staging.mkdir(mode=0o700)
    previous: Path | None = None
    try:
        extracted = extract_to_staging(archive, staging)
        verify_extracted(extracted, manifest)
        apply_modes(extracted, manifest)

        if target.exists():
            if not target.is_dir():
                raise RestoreError(f"target exists and is not a directory: {target}")
            if any(target.iterdir()):
                if not force:
                    raise RestoreError(
                        f"target is not empty: {target} (use --force to replace it)"
                    )
                previous = target.with_name(f"{target.name}.pre-restore-{stamp}")
                os.replace(target, previous)
            else:
                target.rmdir()
        os.replace(extracted, target)
        os.chmod(target, 0o700)
    except Exception:
        if previous is not None and not target.exists():
            os.replace(previous, target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return previous


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and restore a P0 data snapshot.",
    )
    parser.add_argument("archive", type=Path, help="snapshot created by backup_data.py")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="restore target (default: $P0_DATA_DIR or p0/data; required unless "
        "--verify-only)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a non-empty target; the old directory is kept as "
        "<target>.pre-restore-<timestamp>",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the archive and manifest without writing anything",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.umask(0o077)
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        print(f"error: snapshot not found: {archive}", file=sys.stderr)
        return 1
    if not args.verify_only and args.data_dir is None:
        print(
            "error: --data-dir is required for restore (or use --verify-only)",
            file=sys.stderr,
        )
        return 2

    try:
        manifest = verify_archive(archive)
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = (
        f"files={manifest.get('file_count', 0)} "
        f"bytes={manifest.get('total_bytes', 0)} "
        f"created_at={manifest.get('created_at', '')}"
    )
    if args.verify_only:
        print(f"OK: {archive} {summary}")
        return 0

    target = args.data_dir if args.data_dir is not None else default_data_dir()
    try:
        previous = restore(
            archive, target.expanduser().resolve(), args.force, manifest
        )
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"restored={target.expanduser().resolve()}")
    print(f"archive={archive}")
    print(summary)
    if previous is not None:
        print(f"previous={previous}")
        print("note: keep the previous directory until the service smoke test passes")
    print("next: restart cq-p0-api, then check /health and the business smoke test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
