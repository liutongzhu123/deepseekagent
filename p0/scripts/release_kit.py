#!/usr/bin/env python3
"""Build, verify and post-check a versioned P0 release bundle.

The server keeps one working tree (``/opt/cq-ai-p0``) and deploys by replacing
the code directories.  This script makes that operation reproducible:

* ``build``  – package ``app/ static/ tests/ scripts/ deploy/ requirements.txt``
  with a ``manifest.json`` (version, sizes, SHA-256) and a sidecar hash;
* ``verify`` – reject a corrupted/tampered bundle before it reaches the server;
* ``check``  – post-deploy check of ``/health`` (expected version, auth enabled)
  and the optional ``/metrics`` snapshot.

Examples::

    .venv/bin/python scripts/release_kit.py build --out /tmp/releases
    .venv/bin/python scripts/release_kit.py verify --bundle /tmp/releases/cq-ai-p0-0.8.3.tar.gz
    .venv/bin/python scripts/release_kit.py check --base-url https://DOMAIN/p0 \
        --expect-version 0.8.3 --metrics-token "$P0_METRICS_TOKEN" --strict
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

VERSION_RE = re.compile(r'version="([0-9]+\.[0-9]+\.[0-9]+)"')
BUNDLE_PREFIX = "cq-ai-p0-"
INCLUDE = ("app", "static", "tests", "scripts", "deploy")
INCLUDE_FILES = ("requirements.txt", "README.md")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".venv", "data", "dist"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".tmp", ".log"}


class ReleaseError(RuntimeError):
    pass


def project_version(p0_root: Path) -> str:
    text = (p0_root / "app" / "main.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise ReleaseError("cannot find app.main version")
    return match.group(1)


def _iter_files(p0_root: Path):
    for name in INCLUDE_FILES:
        path = p0_root / name
        if path.is_file():
            yield path
    for name in INCLUDE:
        root = p0_root / name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in path.relative_to(p0_root).parts):
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(p0_root: Path) -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "-C", str(p0_root), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
    except Exception:  # noqa: BLE001 - git is optional in a packaged checkout
        return ""


def build_bundle(p0_root: Path, out_dir: Path, version: str | None = None) -> dict:
    version = version or project_version(p0_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{BUNDLE_PREFIX}{version}.tar.gz"
    top = f"p0-{version}"
    files = []
    manifest_files = []
    for path in _iter_files(p0_root):
        relative = path.relative_to(p0_root).as_posix()
        digest = _sha256(path)
        size = path.stat().st_size
        files.append((path, relative))
        manifest_files.append({"path": relative, "size": size, "sha256": digest})
    manifest = {
        "name": "cq-ai-p0",
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(p0_root),
        "files": manifest_files,
    }
    raw = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    with tarfile.open(bundle, "w:gz") as archive:
        for path, relative in files:
            archive.add(path, arcname=f"{top}/{relative}")
        info = tarfile.TarInfo(f"{top}/manifest.json")
        info.size = len(raw)
        info.mtime = int(time.time())
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(raw))
    digest = _sha256(bundle)
    sidecar = bundle.with_suffix(bundle.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    return {
        "bundle": str(bundle),
        "sha256": digest,
        "sidecar": str(sidecar),
        "version": version,
        "files": len(files),
    }


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk():
            raise ReleaseError(f"bundle contains a link: {member.name}")
        target = (destination / member.name).resolve()
        if target != root and root not in target.parents:
            raise ReleaseError(f"unsafe path in bundle: {member.name}")
    archive.extractall(destination, filter="data")


def verify_bundle(bundle: Path) -> dict:
    bundle = Path(bundle)
    if not bundle.exists():
        raise ReleaseError(f"bundle not found: {bundle}")
    expected_hash = ""
    sidecar = bundle.with_suffix(bundle.suffix + ".sha256")
    if sidecar.exists():
        expected_hash = sidecar.read_text(encoding="utf-8").split()[0]
    actual_hash = _sha256(bundle)
    if expected_hash and expected_hash != actual_hash:
        raise ReleaseError("bundle sha256 does not match its .sha256 sidecar")
    with tarfile.open(bundle, "r:gz") as archive:
        names = archive.getnames()
        if not names:
            raise ReleaseError("empty bundle")
        manifests = [name for name in names if name.endswith("/manifest.json") or name == "manifest.json"]
        if not manifests:
            raise ReleaseError("bundle has no manifest.json")
        manifest_name = manifests[0]
        with tempfile.TemporaryDirectory(prefix="p0-release-verify-") as tmp:
            _safe_extract(archive, Path(tmp))
            manifest_path = Path(tmp) / manifest_name
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            base = manifest_path.parent
            missing = []
            bad = []
            for item in manifest.get("files") or []:
                path = base / item["path"]
                if not path.is_file():
                    missing.append(item["path"])
                    continue
                if path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
                    bad.append(item["path"])
            if missing:
                raise ReleaseError(f"manifest files missing: {missing[:3]}")
            if bad:
                raise ReleaseError(f"manifest hash mismatch: {bad[:3]}")
            for required in ("app/main.py", "static/index.html", "requirements.txt"):
                if not (base / required).is_file():
                    raise ReleaseError(f"bundle missing required file: {required}")
            return {
                "ok": True,
                "version": manifest.get("version", ""),
                "files": len(manifest.get("files") or []),
                "sha256": actual_hash,
                "bundle": str(bundle),
            }


def check_http(
    base_url: str,
    expect_version: str,
    *,
    metrics_token: str = "",
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> dict:
    base = base_url.rstrip("/")
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        health = client.get(f"{base}/health")
        payload: dict = {}
        try:
            payload = health.json()
        except ValueError:
            payload = {}
        add(
            "health",
            health.status_code == 200 and payload.get("status") == "ok",
            f"status={health.status_code} body={payload.get('status', '')}",
        )
        version = str(payload.get("version", ""))
        add(
            "version",
            version == expect_version,
            f"expected={expect_version} actual={version or '—'}",
        )
        auth = str(payload.get("auth", ""))
        add("auth", auth == "enabled", f"auth={auth or '—'}")
        try:
            metrics = client.get(
                f"{base}/metrics",
                headers={"X-Metrics-Token": metrics_token} if metrics_token else {},
            )
            if metrics.status_code == 200:
                body = metrics.text
                up = any(line.strip() == "p0_up 1" for line in body.splitlines())
                add("metrics", up, "p0_up=1" if up else "p0_up is not 1")
            elif metrics.status_code == 403:
                add("metrics", True, "skipped (token required)")
            else:
                add("metrics", False, f"status={metrics.status_code}")
        except httpx.HTTPError as exc:
            add("metrics", False, str(exc))
    finally:
        if owns_client:
            client.close()
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="package the current tree")
    build.add_argument("--p0-root", default=str(Path(__file__).resolve().parent.parent))
    build.add_argument("--out", default="dist")
    build.add_argument("--version", default="")

    verify = sub.add_parser("verify", help="verify a release bundle")
    verify.add_argument("--bundle", required=True)

    check = sub.add_parser("check", help="post-deploy HTTP checks")
    check.add_argument("--base-url", required=True)
    check.add_argument("--expect-version", required=True)
    check.add_argument("--metrics-token", default="")
    check.add_argument("--timeout", type=float, default=10.0)
    check.add_argument("--strict", action="store_true")
    check.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(Path(args.p0_root), Path(args.out), args.version or None)
        elif args.command == "verify":
            result = verify_bundle(Path(args.bundle))
        else:
            result = check_http(
                args.base_url, args.expect_version,
                metrics_token=args.metrics_token, timeout=args.timeout,
            )
    except (ReleaseError, OSError, tarfile.TarError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.command == "check":
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            for item in result["checks"]:
                print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}: {item['detail']}")
            print("OK" if result["ok"] else "FAILED")
        if args.strict and not result["ok"]:
            return 1
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
