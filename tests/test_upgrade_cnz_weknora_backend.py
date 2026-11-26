from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
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
        "cnz-weknora_postgres-data", "cnz-weknora_redis-data", "cnz-weknora_ollama-data",
        "127.0.0.1:39301/health", "127.0.0.1:18082/", "DEPLOYED_FROM_CODE", "DEPLOYED_FROM_MAIN",
        "realpath -e -- \"$backup\"",
        "cnz-weknora_data-files", "cnz-weknora_WeKnora-network",
        "BOOTSTRAP_PRESENT=%s", "cnz-weknora-bootstrap", "127.0.0.1:39302",
        "registration_mode\")==\"self_serve\"",
    ):
        assert required in upgrade.PREFLIGHT


def test_release_build_has_no_sql_migration_after_138():
    assert "max(versions)!=138" in Path(upgrade.__file__).read_text()
    assert "DEPLOYED_FROM_CODE" in upgrade.CONFIGURE
    assert "docker-compose.testkb.bootstrap.override.yml" in Path(upgrade.__file__).read_text()


def test_upgrade_builds_and_replaces_only_app_with_rollback():
    script = upgrade.UPGRADE
    assert "compose_new build app" in script
    assert "compose_new up -d --no-deps app" in script
    assert "compose_old up -d --no-deps app" in script
    assert "compose_bootstrap_new up -d --no-deps --no-build bootstrap-app" in script
    assert "compose_bootstrap_old up -d --no-deps --no-build bootstrap-app" in script
    assert "wait_bootstrap \"$new_image\"" in script
    assert "len(p)==1" in script
    assert script.index("compose_new build app") < script.index("phase=app_replace") < script.index("compose_new up -d --no-deps app")
    assert "BACKEND_ROLLBACK_OK_NON_APP_CONTAINERS_UNTOUCHED" in script
    assert "BACKEND_ROLLBACK_POINTER_VERIFY_FAILED" in script
    assert "BACKEND_ACTIVE_POINTER_CHANGED" in script
    assert "flock -n 9" in script
    assert 'exec 9>"$root/.deploy.lock"' in script
    assert 'assert not definition.get("external",False)' in script
    assert 'assert not definition.get("driver_opts")' in script
    assert 'assert definition.get("name") in (None,"cnz-weknora_"+logical)' in script
    assert "down -v" not in script and "volume rm" not in script
    assert "compose_new up -d --no-deps frontend" not in script
    assert script.index('app_mounts=') < script.index("compose_new up -d --no-deps app")
    assert 'set(c["networks"])=={"WeKnora-network"}' in script


def test_bootstrap_override_is_loopback_only_and_keeps_registration_separate():
    override = (ROOT / "weknora/docker-compose.testkb.bootstrap.override.yml").read_text()
    assert "ports: !override" in override
    assert '"127.0.0.1:39302:8080"' in override
    assert 'DISABLE_REGISTRATION: "false"' in override
    assert 'OIDC_AUTH_ENABLE: "false"' in override
    assert "OLLAMA_BASE_URL: http://ollama:11434" in override


def test_post_upgrade_checks_database_siblings_volumes_and_oidc_off():
    script = upgrade.UPGRADE
    for required in (
        "schema_after", "frontend_id", "docreader_id", "postgres_id", "redis_id",
        "volumes_before", "OIDC_AUTH_ENABLE=false", "BACKEND_UPGRADE_OK_APP_ONLY",
        "cnz-weknora_data-files", "cnz-weknora_docreader-tmp",
    ):
        assert required in script


def test_compose_contract_rejects_extra_mounts_and_networks(tmp_path):
    script = upgrade.UPGRADE
    marker = 'config --format json | python3 -c \'\n'
    start = script.index(marker) + len(marker)
    end = script.index('\n \' "$project_config"', start)
    validator = script[start:end]
    config_file = tmp_path / "config.yaml"
    config_file.write_text("synthetic only\n", encoding="utf-8")
    names = {"app":"cnz-weknora-app", "frontend":"cnz-weknora-frontend", "docreader":"cnz-weknora-docreader", "postgres":"cnz-weknora-postgres", "redis":"cnz-weknora-redis", "ollama":"cnz-weknora-ollama"}
    services = {name:{"container_name":container,"networks":{"WeKnora-network":{}}} for name,container in names.items()}
    services["ollama"].update({
        "image":"ollama/ollama@sha256:b2f0122c476bc9fe7ce679d8bb4bd555351e25e2554eefb156f61cb0a89cf429",
        "environment":{"OLLAMA_HOST":"0.0.0.0:11434"},
        "healthcheck":{"test":["CMD","ollama","list"]},
        "volumes":[{"type":"volume","source":"ollama-data","target":"/root/.ollama"}],
    })
    services["app"].update({
        "environment":{"DISABLE_REGISTRATION":"true","OIDC_AUTH_ENABLE":"false"},
        "ports":[{"host_ip":"127.0.0.1","target":8080,"published":"39301"}],
        "volumes":[
            {"type":"volume","source":"data-files","target":"/data/files"},
            {"type":"volume","source":"docreader-tmp","target":"/tmp/docreader","read_only":True},
            {"type":"bind","source":os.path.abspath(config_file),"target":"/app/config/config.yaml"},
        ],
    })
    services["frontend"]["ports"]=[{"host_ip":"127.0.0.1","target":80,"published":"18082"}]
    valid={"services":services,"volumes":{"data-files":{"driver":"local"},"docreader-tmp":{"driver":"local"},"postgres-data":{"driver":"local"},"redis-data":{"driver":"local"},"ollama-data":{"driver":"local"}},"networks":{"WeKnora-network":{"driver":"bridge"}}}
    def validate(config):
        return subprocess.run([sys.executable,"-c",validator,str(config_file)],input=json.dumps(config).encode(),capture_output=True)
    assert validate(valid).returncode == 0
    private_host_port=json.loads(json.dumps(valid))
    private_host_port["services"]["ollama"]["ports"]=[{"host_ip":"0.0.0.0","target":11434,"published":"11434"}]
    assert validate(private_host_port).returncode != 0
    shared_volume=json.loads(json.dumps(valid))
    shared_volume["services"]["ollama"]["volumes"][0]["source"]="redis-data"
    assert validate(shared_volume).returncode != 0
    extra_mount=json.loads(json.dumps(valid))
    extra_mount["services"]["app"]["volumes"].append({"type":"bind","source":"/etc","target":"/host"})
    assert validate(extra_mount).returncode != 0
    extra_network=json.loads(json.dumps(valid))
    extra_network["networks"]["shared"]={"external":True}
    assert validate(extra_network).returncode != 0
    driver_opts=json.loads(json.dumps(valid))
    driver_opts["volumes"]["data-files"]={"driver":"local","driver_opts":{"type":"none","o":"bind","device":"/srv/yunan-persist/data-files"}}
    assert validate(driver_opts).returncode != 0
    external_yunan_volume=json.loads(json.dumps(valid))
    external_yunan_volume["volumes"]["data-files"]={"external":True,"name":"yunan-persist_data-files"}
    assert validate(external_yunan_volume).returncode != 0
    renamed_volume=json.loads(json.dumps(valid))
    renamed_volume["volumes"]["data-files"]={"name":"yunan-persist_data-files"}
    assert validate(renamed_volume).returncode != 0


def test_wrong_target_is_rejected_before_any_remote_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="only yunan-101"):
        upgrade.upgrade("other-host", tmp_path / "archive.tar.gz", "/var/backups/cnz-weknora/x")
    assert calls == []


def test_remote_shell_arguments_are_quoted_as_individual_arguments(monkeypatch):
    captured = {}
    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    payload = "/var/backups/cnz-weknora/x; touch /tmp/pwn $(id)"
    upgrade.remote("yunan-101", "printf test", payload, timeout=17)
    assert shlex.split(captured["args"][-1]) == ["bash", "-s", "--", payload]
    assert captured["input"] == b"printf test"


def test_backup_path_rejects_shell_metacharacters_before_remote_call(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"fixture")
    def fake_run(args, *_, **kwargs):
        if args[1:3] == ["branch", "--show-current"]: stdout = "main\n"
        elif args[1:3] == ["fetch", "--no-tags"]: stdout = ""
        elif args[1:3] in (["rev-parse", "HEAD"], ["rev-parse", "refs/remotes/origin/main"]): stdout = "a" * 40 + "\n"
        else: stdout = ""
        return type("Result", (), {"returncode": 0, "stdout": stdout})()
    monkeypatch.setattr(upgrade.base, "run", fake_run)
    calls = []
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="direct child of CNZ backup root"):
        upgrade.upgrade("yunan-101", archive, "/var/backups/cnz-weknora/x; id")
    assert calls == []


def test_dirty_worktree_is_rejected_before_remote_call(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"fixture")
    calls = []
    def fake_run(args, timeout=300):
        if args[1:3] == ["branch", "--show-current"]:
            return type("Result", (), {"returncode": 0, "stdout": "main\n"})()
        if args[1:3] == ["fetch", "--no-tags"]:
            return type("Result", (), {"returncode": 0, "stdout": ""})()
        if args[1:3] == ["rev-parse", "HEAD"]:
            return type("Result", (), {"returncode": 0, "stdout": "a" * 40})()
        if args[1:3] == ["rev-parse", "refs/remotes/origin/main"]:
            return type("Result", (), {"returncode": 0, "stdout": "a" * 40})()
        if args[1:3] == ["status", "--porcelain"]:
            return type("Result", (), {"returncode": 0, "stdout": " M tracked.py"})()
        raise AssertionError(args)
    monkeypatch.setattr(upgrade.base, "run", fake_run)
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="commit intended changes"):
        upgrade.upgrade("yunan-101", archive, "/var/backups/cnz-weknora/x")
    assert calls == []


def test_unmerged_branch_is_rejected_before_fetch_or_remote_call(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"fixture")
    commands = []
    def fake_run(args, *_, **kwargs):
        commands.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "codex/feature\n"})()
    monkeypatch.setattr(upgrade.base, "run", fake_run)
    remote_calls = []
    monkeypatch.setattr(upgrade, "remote", lambda *args, **kwargs: remote_calls.append(args))
    with pytest.raises(RuntimeError, match="deployment must run from main"):
        upgrade.upgrade("yunan-101", archive, "/var/backups/cnz-weknora/snapshot")
    assert len(commands) == 1
    assert remote_calls == []


def test_success_path_uses_clean_commit_and_verified_backup(monkeypatch, tmp_path):
    sha = "a" * 40
    archive = tmp_path / "upstream.tar.gz"
    archive.write_bytes(b"fixture")
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"bundle")
    class Result:
        returncode = 0
        def __init__(self, stdout): self.stdout = stdout
    def fake_git(args, *_, **kwargs):
        if args[1:3] == ["branch", "--show-current"]: return Result("main\n")
        if args[1:3] == ["fetch", "--no-tags"]: return Result("")
        if args[1:3] in (["rev-parse", "HEAD"], ["rev-parse", "refs/remotes/origin/main"]): return Result(sha + "\n")
        return Result("")
    monkeypatch.setattr(upgrade.base, "run", fake_git)
    monkeypatch.setattr(upgrade, "make_bundle", lambda *_: (bundle, "d" * 64))
    responses = [
        Result(b"BACKEND_PREFLIGHT_OK\nACTIVE_SHA=" + b"e" * 40 + b"\nRUNTIME_SHA=" + b"b" * 40 + b"\nOLD_IMAGE=sha256:" + b"c" * 64 + b"\n"),
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


def test_all_remote_deploy_transactions_share_nonblocking_lock():
    for path, marker in (
        (ROOT / "scripts/deploy_cnz_weknora_testkb.py", "CNZ_DEPLOY_LOCKED"),
        (ROOT / "scripts/upgrade_cnz_weknora_frontend.py", "CNZ_DEPLOY_LOCKED"),
        (ROOT / "scripts/upgrade_cnz_weknora_backend.py", "BACKEND_DEPLOY_LOCKED"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "flock -n 9" in source
        assert marker in source
    frontend = (ROOT / "scripts/upgrade_cnz_weknora_frontend.py").read_text(encoding="utf-8")
    assert 'test "$(readlink -f "$root/active-release")" = "$old_project"' in frontend
