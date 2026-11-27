"""Failure-mode coverage for the CNZ-only snapshot workflow."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backup_cnz_weknora_testkb", ROOT / "scripts/backup_cnz_weknora_testkb.py"
)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout.encode(), stderr.encode())


def test_target_allowlist_refuses_before_ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(backup.base, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="only the explicitly authorized"):
        backup.backup("some-other-host")
    assert calls == []


def test_remote_preflight_requires_isolated_healthy_stack_backup_and_clean_schema():
    script = backup.REMOTE_BACKUP
    for required in (
        "cnz-weknora-app", "cnz-weknora-postgres", "cnz-weknora-redis",
        "cnz-weknora-ollama",
        "cnz-weknora_docreader-tmp", "cnz-weknora_postgres-data",
        "cnz-weknora_ollama-data",
        "com.docker.compose.project", "schema_migrations", "10485760",
        "yunan-persist", "127.0.0.1:39301", "127.0.0.1:18082",
        "DEPLOYED_FROM_MAIN", "DEPLOYED_FROM_CODE", "verify_release_marker", "stat -c %a \"$active/.env\"",
    ):
        assert required in script
    for expected in (
        "docker inspect -f '{{json .Mounts}}'", "assert_mounts cnz-weknora-app",
        "assert_mounts cnz-weknora-docreader", "assert_mounts cnz-weknora-postgres",
        "assert_mounts cnz-weknora-redis",
        "assert_mounts cnz-weknora-ollama",
        "runtime_source_sha=", "runtime_config_sha256=", "runtime_env_path=",
        "bootstrap_source_sha=", "bootstrap_config_path=", "bootstrap_config_sha256=",
        "{{ index .Config.Labels \"com.docker.compose.project\" }}",
    ):
        assert expected in script


def test_backup_accepts_bootstrap_from_active_release_with_older_app_runtime():
    script = backup.REMOTE_BACKUP
    assert 'test "$bootstrap_source_sha" = "$source_sha"' in script
    assert 'test "$(docker inspect -f \'{{index .Config.Labels "com.docker.compose.project.working_dir"}}\' cnz-weknora-bootstrap)" = "$runtime_source"' not in script
    assert 'cmp -s "$active/.env" "$runtime_source/.env"' in script
    assert 'bootstrap_config="$(' in script
    assert 'bootstrap_config_path=%s\\nbootstrap_config_sha256=%s' in script


def test_backup_supports_pre_ollama_and_post_ollama_cnz_stacks():
    script = backup.REMOTE_BACKUP
    assert 'if docker inspect cnz-weknora-ollama' in script
    assert 'expected_containers="$(printf' in script
    assert 'expected_volumes="$(printf' in script
    assert 'if [ "$ollama_present" -eq 1 ]; then' in script
    assert 'ollama_image=none' in script
    assert 'if [ "$ollama_volume_present" -eq 1 ]; then\n  test -s "$backup/ollama-data.tar.gz"' in script


def test_remote_script_quiesces_and_restarts_only_cnz_services_in_safe_order():
    script = backup.REMOTE_BACKUP
    stop_bootstrap = script.index("docker stop --time 60 cnz-weknora-bootstrap")
    stop_app = script.index("docker stop --time 60 cnz-weknora-app")
    stop_docreader = script.index("docker stop --time 60 cnz-weknora-docreader")
    stop_redis = script.index("docker stop --time 60 cnz-weknora-redis")
    stop_ollama = script.index("docker stop --time 60 cnz-weknora-ollama")
    assert stop_bootstrap < stop_app < stop_docreader < stop_redis < stop_ollama
    start_ollama = script.index("docker start cnz-weknora-ollama")
    start_redis = script.index("docker start cnz-weknora-redis")
    start_docreader = script.index("docker start cnz-weknora-docreader")
    start_app = script.index("docker start cnz-weknora-app")
    start_bootstrap = script.index("docker start cnz-weknora-bootstrap")
    assert start_ollama < start_redis < start_docreader < start_app
    assert start_app < start_bootstrap
    assert "docker compose down" not in script
    assert "docker volume rm" not in script
    assert "down -v" not in script
    for attempted in (
        "echo CNZ_RESTART_START_FAILED=redis", "echo CNZ_RESTART_START_FAILED=docreader",
        "echo CNZ_RESTART_START_FAILED=app", 'return "$failures"',
        "echo CNZ_RESTART_START_FAILED=ollama",
        "echo CNZ_RESTART_START_FAILED=bootstrap",
    ):
        assert attempted in script
    assert 'mktemp -d "$backup_root/${stamp}-${source_sha}.XXXXXX"' in script
    assert "--volumes-from" not in script
    assert "--mount type=volume,source=cnz-weknora_data-files,target=/data/files,readonly" in script
    assert script.index('test ! -L "$backup_root"') < script.index('install -d -m 0700 "$backup_root"')
    assert 'app_stopped=1; docker stop --time 60 cnz-weknora-app' in script
    assert 'bootstrap_stopped=1; docker stop --time 60 cnz-weknora-bootstrap' in script
    assert 'HostIp":"127.0.0.1","HostPort":"39302"' in script
    assert 'registration_mode")=="self_serve"' in script


def test_backup_archives_are_verified_private_and_do_not_copy_credentials():
    script = backup.REMOTE_BACKUP
    assert "pg_dump" in script and 'docker run --rm --network none --mount type=bind,source="$backup",target=/backup,readonly --entrypoint pg_restore "$pg_image" --list /backup/database.dump' in script
    assert '"$backup/files.tar.gz"' in script
    assert '"$backup/docreader-tmp.tar.gz"' in script
    assert '"$backup/redis-volume.tar.gz"' in script
    assert '"$backup/ollama-data.tar.gz"' in script
    assert "sha256sum -c SHA256SUMS" in script
    assert 'stat -c %a "$active/.env"' in script
    assert 'runtime_env_path=%s' in script
    assert 'cp -- "$active/.env"' not in script
    assert '"$backup/.env"' not in script
    assert 'test "$(stat -c %a "$backup")" = 700' in script
    assert "cat \"$backup/.env\"" not in script
    assert 'find "$backup" -type f ! -perm 0600' in script
    assert "CNZ_BACKUP_OK=$backup" in script
    assert 'backup_complete=0' in script and 'backup_complete=1' in script
    assert 'if [ "$code" -eq 0 ] && [ "$backup_complete" -ne 1 ]; then' in script
    assert "CNZ_BACKUP_NOT_COMPLETE" in script


def test_remote_shell_script_parses(tmp_path):
    path = tmp_path / "backup.sh"
    path.write_text(backup.REMOTE_BACKUP, encoding="utf-8")
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backup_serializes_with_deploy_transactions():
    assert 'exec 9>"$root/.deploy.lock"' in backup.REMOTE_BACKUP
    assert "flock -n 9" in backup.REMOTE_BACKUP


def test_success_requires_completion_marker_and_failure_does_not_leak_output(monkeypatch):
    monkeypatch.setattr(
        backup.base,
        "remote",
        lambda *args, **kwargs: completed("CNZ_BACKUP_OK=/var/backups/cnz-weknora/backup\n"),
    )
    assert backup.backup("yunan-101") == "/var/backups/cnz-weknora/backup"
    monkeypatch.setattr(
        backup.base,
        "remote",
        lambda *args, **kwargs: completed("secret-value\nCNZ_BACKUP_FAILED_STAGE=database_dump\n", 1),
    )
    with pytest.raises(RuntimeError, match="stopped safely") as error:
        backup.backup("yunan-101")
    assert "secret-value" not in str(error.value)
