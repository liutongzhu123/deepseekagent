"""Backup/restore tests for the P1-0.5 file-data safety net."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

P0_ROOT = Path(__file__).resolve().parents[1]
BACKUP = P0_ROOT / "scripts" / "backup_data.py"
RESTORE = P0_ROOT / "scripts" / "restore_data.py"
STANDARD_ID = "std_20260101000000_abc"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def run(script: Path, *args: object, env: dict | None = None) -> subprocess.CompletedProcess:
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, str(script), *(str(arg) for arg in args)],
        cwd=P0_ROOT,
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )


def write_data_dir(root: Path) -> None:
    auth = root / "auth"
    auth.mkdir(parents=True)
    users = {
        "users": [
            {
                "username": "admin",
                "display_name": "Admin",
                "role": "admin",
                "active": True,
                "department": "",
                "projects": [],
                "auth_provider": "local",
                "session_epoch": 0,
                "failed_logins": 0,
                "locked_until": "",
                "last_login_at": "",
                "password_hash": "pbkdf2_sha256$1$stub$stub",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    }
    (auth / "users.json").write_text(json.dumps(users), encoding="utf-8")
    (auth / "users.json").chmod(0o600)
    (auth / "session_secret").write_text("unit-test-secret", encoding="utf-8")
    (auth / "session_secret").chmod(0o600)

    audit = root / "audit"
    audit.mkdir()
    (audit / "audit.jsonl").write_text(
        '{"at":"2026-01-01T00:00:00+00:00","event":"login","status":200}\n',
        encoding="utf-8",
    )

    standard = root / "standards" / STANDARD_ID
    standard.mkdir(parents=True)
    result = {
        "id": STANDARD_ID,
        "created_at": "2026-01-01T00:00:00+00:00",
        "revision": 0,
        "published": False,
        "meta": {
            "standard_no": "Q/TEST 456-2024",
            "name": "车用插接器",
            "pages": 3,
            "project": "",
            "created_by": "admin",
        },
        "summary": {"items": 2},
    }
    (standard / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (standard / "source.pdf").write_bytes(b"%PDF-1.4 unit test")
    (standard / "复核说明.txt").write_text("中文文件名与内容", encoding="utf-8")

    quote = root / "quotes" / "quote_1"
    quote.mkdir(parents=True)
    (quote / "meta.json").write_text(
        json.dumps({"project": "", "created_by": "admin"}), encoding="utf-8"
    )


def files_under(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_backup_then_restore_yields_usable_data_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_data_dir(data)
    backups = tmp_path / "backups"

    assert run(BACKUP, "--data-dir", data, "--backup-dir", backups).returncode == 0
    archives = sorted(backups.glob("p0-data-*.tar.gz"))
    assert len(archives) == 1
    archive = archives[0]
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.read_text(encoding="utf-8").split()[0] == sha256(archive)

    restored = tmp_path / "restored"
    restored.parent.mkdir(parents=True, exist_ok=True)
    result = run(RESTORE, archive, "--data-dir", restored)
    assert result.returncode == 0, result.stderr
    assert files_under(restored) == files_under(data)
    assert (restored / "auth" / "users.json").stat().st_mode & 0o777 == 0o600

    # The restored directory must be enough to boot the service and read data.
    app_check = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
health = client.get("/health")
standards = client.get("/api/v1/standards")
print(json.dumps({
    "health": health.status_code,
    "ids": [item["id"] for item in standards.json().get("standards", [])],
}))
"""
    env = {
        "P0_AUTH_DISABLED": "1",
        "P0_COOKIE_SECURE": "0",
        "P0_WEKNORA_REQUIRED": "0",
        "P0_DATA_DIR": str(restored),
        "P0_STANDARD_DIR": str(restored / "standards"),
        "P0_QUOTE_DIR": str(restored / "quotes"),
        "P0_AUDIT_LOG": str(restored / "audit" / "audit.jsonl"),
    }
    check = subprocess.run(
        [sys.executable, "-c", app_check],
        cwd=P0_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    payload = json.loads(check.stdout.strip().splitlines()[-1])
    assert payload["health"] == 200
    assert payload["ids"] == [STANDARD_ID]


def test_restore_refuses_non_empty_target_unless_forced(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_data_dir(data)
    backups = tmp_path / "backups"
    assert run(BACKUP, "--data-dir", data, "--backup-dir", backups).returncode == 0
    archive = sorted(backups.glob("p0-data-*.tar.gz"))[0]

    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("old data", encoding="utf-8")

    refused = run(RESTORE, archive, "--data-dir", target)
    assert refused.returncode == 1
    assert "not empty" in refused.stderr
    assert (target / "keep.txt").read_text(encoding="utf-8") == "old data"

    forced = run(RESTORE, archive, "--data-dir", target, "--force")
    assert forced.returncode == 0, forced.stderr
    assert (target / "auth" / "users.json").exists()
    previous = sorted(tmp_path.glob("target.pre-restore-*"))
    assert len(previous) == 1
    assert (previous[0] / "keep.txt").read_text(encoding="utf-8") == "old data"


def test_verify_rejects_tampered_file(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_data_dir(data)
    backups = tmp_path / "backups"
    assert run(BACKUP, "--data-dir", data, "--backup-dir", backups).returncode == 0
    archive = sorted(backups.glob("p0-data-*.tar.gz"))[0]

    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source:
        manifest = source.extractfile("manifest.json").read()
        members = {
            member.name: source.extractfile(member).read()
            for member in source.getmembers()
            if member.isfile() and member.name != "manifest.json"
        }
    members["data/auth/users.json"] = b'{"users": []}'
    with tarfile.open(tampered, "w:gz") as target:
        header = tarfile.TarInfo("manifest.json")
        header.size = len(manifest)
        target.addfile(header, io.BytesIO(manifest))
        for name, payload in members.items():
            header = tarfile.TarInfo(name)
            header.size = len(payload)
            target.addfile(header, io.BytesIO(payload))

    verify = run(RESTORE, tampered, "--verify-only")
    assert verify.returncode == 1
    assert "mismatch" in verify.stderr


def test_verify_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    manifest = {
        "format": 1,
        "files": [
            {
                "path": "../evil.txt",
                "size": 4,
                "sha256": hashlib.sha256(b"evil").hexdigest(),
                "mode": 0o600,
            }
        ],
    }
    payload = json.dumps(manifest).encode("utf-8")
    with tarfile.open(archive, "w:gz") as target:
        header = tarfile.TarInfo("manifest.json")
        header.size = len(payload)
        target.addfile(header, io.BytesIO(payload))
        header = tarfile.TarInfo("data/../evil.txt")
        header.size = 4
        target.addfile(header, io.BytesIO(b"evil"))

    target = tmp_path / "restored"
    result = run(RESTORE, archive, "--data-dir", target)
    assert result.returncode == 1
    assert "unsafe file path" in result.stderr
    assert not (target.parent / "evil.txt").exists()


def test_retention_keeps_newest_snapshot(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_data_dir(data)
    backups = tmp_path / "backups"
    first = run(BACKUP, "--data-dir", data, "--backup-dir", backups, "--label", "a")
    assert first.returncode == 0, first.stderr
    second = run(
        BACKUP, "--data-dir", data, "--backup-dir", backups, "--label", "b", "--keep", "1"
    )
    assert second.returncode == 0, second.stderr
    archives = sorted(backups.glob("p0-data-*.tar.gz"))
    assert len(archives) == 1
    assert archives[0].name.endswith("-b.tar.gz")
