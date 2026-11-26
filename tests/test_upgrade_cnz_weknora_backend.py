from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upgrade_cnz_weknora_backend", ROOT / "scripts/upgrade_cnz_weknora_backend.py"
)
assert SPEC and SPEC.loader
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def test_preflight_requires_valid_snapshot_and_pinned_runtime_state():
    for required in (
        "sha256sum -c SHA256SUMS", "active_source_sha=$active_sha",
        "runtime_source_sha=$runtime_sha", "database_schema=138|f",
        "schema\" = 138\\|f", "cnz-weknora-postgres", "yunan-persist",
        "cnz-weknora_postgres-data", "cnz-weknora_redis-data",
        "127.0.0.1:39301/health", "127.0.0.1:18082/", "DEPLOYED_FROM_CODE", "DEPLOYED_FROM_MAIN",
    ):
        assert required in upgrade.PREFLIGHT


def test_release_build_has_no_sql_migration_after_138():
    assert "max(versions)!=138" in Path(upgrade.__file__).read_text()
    assert "DEPLOYED_FROM_CODE" in upgrade.CONFIGURE


def test_upgrade_builds_and_replaces_only_app_with_rollback():
    script = upgrade.UPGRADE
    assert "compose_new build app" in script
    assert "compose_new up -d --no-deps app" in script
    assert "compose_old up -d --no-deps app" in script
    assert script.index("compose_new build app") < script.index("phase=app_replace") < script.index("compose_new up -d --no-deps app")
    assert "BACKEND_ROLLBACK_OK_NON_APP_CONTAINERS_UNTOUCHED" in script
    assert "down -v" not in script and "volume rm" not in script
    assert "compose_new up -d --no-deps frontend" not in script


def test_post_upgrade_checks_database_siblings_volumes_and_oidc_off():
    script = upgrade.UPGRADE
    for required in (
        "schema_after", "frontend_id", "docreader_id", "postgres_id", "redis_id",
        "volumes_before", "OIDC_AUTH_ENABLE=false", "BACKEND_UPGRADE_OK_APP_ONLY",
        "cnz-weknora_data-files", "cnz-weknora_docreader-tmp",
    ):
        assert required in script


def test_wrong_target_is_rejected_before_any_remote_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="only yunan-101"):
        upgrade.upgrade("other-host", tmp_path / "archive.tar.gz", "/var/backups/cnz-weknora/x")
    assert calls == []


def test_dirty_worktree_is_rejected_before_remote_call(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"fixture")
    calls = []
    def fake_run(args, timeout=300):
        if args[1:3] == ["rev-parse", "HEAD"]:
            return type("Result", (), {"returncode": 0, "stdout": "a" * 40})()
        if args[1:3] == ["status", "--porcelain"]:
            return type("Result", (), {"returncode": 0, "stdout": " M tracked.py"})()
        raise AssertionError(args)
    monkeypatch.setattr(upgrade.base, "run", fake_run)
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="commit intended changes"):
        upgrade.upgrade("yunan-101", archive, "/var/backups/cnz-weknora/x")
    assert calls == []


def test_success_path_uses_clean_commit_and_verified_backup(monkeypatch, tmp_path):
    sha = "a" * 40
    archive = tmp_path / "upstream.tar.gz"
    archive.write_bytes(b"fixture")
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"bundle")
    class Result:
        returncode = 0
        def __init__(self, stdout): self.stdout = stdout
    monkeypatch.setattr(upgrade.base, "run", lambda args, *_, **kwargs:
        Result(sha + "\n" if args[1:3] == ["rev-parse", "HEAD"] else ""))
    monkeypatch.setattr(upgrade, "make_bundle", lambda *_: (bundle, "d" * 64))
    responses = [
        Result(b"BACKEND_PREFLIGHT_OK\nRUNTIME_SHA=" + b"b" * 40 + b"\nOLD_IMAGE=sha256:" + b"c" * 64 + b"\n"),
        Result(b""), Result(b"BACKEND_RELEASE_READY\n"),
        Result(b"BACKEND_UPGRADE_OK_APP_ONLY\nACTIVE_RELEASE=" + sha.encode() + b"\nDATABASE_SCHEMA=138|f\nOIDC_AUTH_ENABLE=false\n"),
    ]
    remote_calls = []
    def fake_remote(target, script, *args, **kwargs):
        remote_calls.append((script, args))
        return responses.pop(0)
    monkeypatch.setattr(upgrade, "remote", fake_remote)
    monkeypatch.setattr(upgrade.base, "stream_bundle", lambda *_: Result(b""))
    monkeypatch.setattr(upgrade.base, "cleanup_incoming", lambda *_: None)
    upgrade.upgrade("yunan-101", archive, "/var/backups/cnz-weknora/snapshot")
    assert len(remote_calls) == 4
    assert remote_calls[0][0] == upgrade.PREFLIGHT
    assert remote_calls[-1][0] == upgrade.UPGRADE


def test_remote_shell_scripts_pass_bash_parser(tmp_path):
    for name, script in (("preflight", upgrade.PREFLIGHT),
                         ("configure", upgrade.CONFIGURE),
                         ("upgrade", upgrade.UPGRADE)):
        path = tmp_path / f"{name}.sh"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
