#!/usr/bin/env python3
"""Verify and prepare the pinned official WeKnora source plus CNZ patch.

This script never downloads anything and refuses to overwrite the destination.
It extracts only the pinned official v0.9.1 archive into an empty directory,
checks every regular file against the locked manifest, then applies the
reviewable project patch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SHA256 = "183b0538d895b85906da030b8b62a93bd884cd1967d10bcb38020b549b3bc5cd"
MANIFEST_SHA256 = "3a05e5efc4f5e1a18e5b3233fc96c752e35548339400fda20573804ae553b26e"
MANIFEST = ROOT / "ci/upstream-v0.9.1.json"
PATCHES = (
    ROOT / "weknora/patches/0001-cnz-explicit-oidc-identity.patch",
    ROOT / "weknora/patches/0002-rustup-mirror.patch",
    ROOT / "weknora/patches/0003-cnz-testkb-build-mirrors.patch",
    ROOT / "weknora/patches/0004-cnz-knowledge-workspace-profile.patch",
    ROOT / "weknora/patches/0005-cnz-login-scope-copy.patch",
    ROOT / "weknora/patches/0006-cnz-oidc-synthetic-flow-tests.patch",
    ROOT / "weknora/patches/0007-cnz-oidc-nonce-binding.patch",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path, help="official WeKnora v0.9.1 source tar.gz")
    parser.add_argument("destination", type=Path, help="new, empty destination directory")
    args = parser.parse_args()

    if sha256(MANIFEST) != MANIFEST_SHA256:
        raise SystemExit("pinned upstream manifest hash mismatch")
    if sha256(args.archive) != ARCHIVE_SHA256:
        raise SystemExit("official v0.9.1 archive SHA-256 mismatch")
    if args.destination.exists() and any(args.destination.iterdir()):
        raise SystemExit("destination exists and is not empty; refusing to overwrite")
    args.destination.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(MANIFEST.read_text())
    prefix = "WeKnora-0.9.1/"
    seen = set()
    with tarfile.open(args.archive, "r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "WeKnora-0.9.1":
                raise SystemExit(f"unsafe archive path: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise SystemExit(f"unsupported archive entry type: {member.name}")
            if member.name in seen:
                raise SystemExit(f"duplicate archive path: {member.name}")
            seen.add(member.name)
            target = args.destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise SystemExit(f"archive file payload is missing: {member.name}")
                with source_file, target.open("wb") as destination_file:
                    shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
                os.chmod(target, (member.mode & 0o111) | 0o644)

    source = args.destination / "WeKnora-0.9.1"
    actual = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"unexpected symlink in extracted source: {path}")
        if path.is_file():
            rel = path.relative_to(source).as_posix()
            file_stat = path.stat()
            actual[rel] = {
                "bytes": file_stat.st_size,
                "executable": bool(file_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
                "sha256": sha256(path),
            }
    expected = manifest["files"]
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(name for name in actual.keys() & expected.keys() if actual[name] != expected[name])
        raise SystemExit(f"source manifest mismatch: missing={len(missing)} extra={len(extra)} changed={len(changed)}")

    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    for patch in PATCHES:
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
    print(f"verified upstream commit={manifest['commit']} files={len(actual)}")
    print(f"applied patches={','.join(patch.name for patch in PATCHES)}")
    print(f"prepared source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
