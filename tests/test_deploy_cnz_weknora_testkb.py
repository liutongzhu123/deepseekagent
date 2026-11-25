"""Failure-path checks for the initial testkb WeKnora deployer.

All external process calls are mocked. These tests never contact SSH, Docker,
GitHub, or a real target host.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "deploy_cnz_weknora_testkb", ROOT / "scripts/deploy_cnz_weknora_testkb.py"
)
deploy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(deploy)


def valid_config():
    return {
        "services": {
            "app": {"container_name": "cnz-weknora-app", "ports": [
                {"host_ip": "127.0.0.1", "target": 8080, "published": "39301"}],
                "build": {"args": {"CNZ_DOCKERHUB_MIRROR_ARG": "docker.m.daocloud.io", "GOPROXY_ARG": "https://goproxy.cn"}},
                "environment": {"DISABLE_REGISTRATION": "true", "OIDC_AUTH_ENABLE": "false"}},
            "frontend": {"container_name": "cnz-weknora-frontend", "ports": [
                {"host_ip": "127.0.0.1", "target": 80, "published": "18082"}],
                "build": {"args": {"CNZ_DOCKERHUB_MIRROR_ARG": "docker.m.daocloud.io"}}},
            "docreader": {"container_name": "cnz-weknora-docreader",
                "build": {"args": {"CNZ_DOCKERHUB_MIRROR_ARG": "docker.m.daocloud.io"}}},
            "postgres": {"container_name": "cnz-weknora-postgres"},
            "redis": {"container_name": "cnz-weknora-redis"},
        },
        "volumes": {name: {} for name in ("data-files", "docreader-tmp", "postgres-data", "redis-data")},
    }


def test_f1_fetch_failure_stops_before_any_remote_action(monkeypatch, tmp_path):
    answers = [
        subprocess.CompletedProcess([], 0, "main", ""),
        subprocess.CompletedProcess([], 1, "", "network down"),
        subprocess.CompletedProcess([], 0, "https://github.com/acme/repo.git", ""),
        subprocess.CompletedProcess([], 1, "", "API unavailable"),
    ]
    monkeypatch.setattr(deploy, "run", lambda *_a, **_k: answers.pop(0))
    with pytest.raises(RuntimeError, match="F1"):
        deploy.main_sha(tmp_path)
    assert answers == []


def test_f1_dirty_checkout_rejected(monkeypatch, tmp_path):
    answers = [
        subprocess.CompletedProcess([], 0, "main", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "abc", ""),
        subprocess.CompletedProcess([], 0, "abc", ""),
        subprocess.CompletedProcess([], 0, "M file.py", ""),
    ]
    monkeypatch.setattr(deploy, "run", lambda *_a, **_k: answers.pop(0))
    with pytest.raises(RuntimeError, match="F1"):
        deploy.main_sha(tmp_path)


def test_f1_github_api_fallback_requires_exact_main_tree(monkeypatch, tmp_path):
    answers = [
        subprocess.CompletedProcess([], 0, "main", ""),
        subprocess.CompletedProcess([], 1, "", "network down"),
        subprocess.CompletedProcess([], 0, "https://github.com/acme/repo.git", ""),
        subprocess.CompletedProcess([], 0, "a" * 40 + "\t" + "tree-sha", ""),
        subprocess.CompletedProcess([], 0, "tree-sha", ""),
        subprocess.CompletedProcess([], 0, "local-commit-sha", ""),
        subprocess.CompletedProcess([], 0, "", ""),
    ]
    monkeypatch.setattr(deploy, "run", lambda *_a, **_k: answers.pop(0))
    assert deploy.main_sha(tmp_path) == "a" * 40
    assert answers == []


def test_f1_github_api_fallback_rejects_tree_mismatch(monkeypatch, tmp_path):
    answers = [
        subprocess.CompletedProcess([], 0, "main", ""),
        subprocess.CompletedProcess([], 1, "", "network down"),
        subprocess.CompletedProcess([], 0, "git@github.com:acme/repo.git", ""),
        subprocess.CompletedProcess([], 0, "a" * 40 + "\tremote-tree", ""),
        subprocess.CompletedProcess([], 0, "different-tree", ""),
    ]
    monkeypatch.setattr(deploy, "run", lambda *_a, **_k: answers.pop(0))
    with pytest.raises(RuntimeError, match="tree does not match"):
        deploy.main_sha(tmp_path)


def test_f2_bad_upstream_aborts_bundle_before_ssh(monkeypatch, tmp_path):
    archive = tmp_path / "upstream.tar.gz"
    archive.write_bytes(b"not the pinned source")
    monkeypatch.setattr(deploy, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", ""))
    with pytest.raises(RuntimeError, match="F2"):
        deploy.make_bundle("a" * 40, archive, tmp_path)
    assert not (tmp_path / "release.tar.gz").exists()


def test_f3_preflight_requires_backup_health_and_free_space():
    assert "sha256sum -c SHA256SUMS" in deploy.PREFLIGHT
    assert "yunan-persist" in deploy.PREFLIGHT and "-eq 5" in deploy.PREFLIGHT
    assert "31457280" in deploy.PREFLIGHT
    assert "cnz-weknora" in deploy.PREFLIGHT


def test_f4_release_directory_is_never_overwritten():
    assert 'if [ -e "$release" ] || [ -L "$release" ]; then' in deploy.PREFLIGHT
    assert "grep -Fxq \"$sha\" \"$release/DEPLOYED_FROM_MAIN\"" in deploy.PREFLIGHT


def test_f5_transfer_uses_temporary_release_then_cleans_failures():
    assert 'incoming="$root/.incoming-$sha"' in deploy.CONFIGURE
    assert 'test -f "$incoming/docker-compose.testkb.override.yml"' in deploy.CONFIGURE
    assert 'grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_MAIN"' in deploy.CONFIGURE
    assert 'mv -T "$incoming" "$release"' in deploy.CONFIGURE
    assert any(
        "tar -xzf - -C" in value
        for value in deploy.stream_bundle.__code__.co_consts
        if isinstance(value, str)
    )
    assert any(
        "rm -rf --" in value
        for value in deploy.cleanup_incoming.__code__.co_consts
        if isinstance(value, str)
    )


def test_f6_compose_requires_loopback_isolated_contract():
    deploy.validate_compose(valid_config())
    config = valid_config()
    config["services"]["app"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ValueError, match="F6"):
        deploy.validate_compose(config)


def test_f6_compose_rejects_docker_socket_registration_or_oidc():
    config = valid_config()
    config["services"]["app"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    with pytest.raises(ValueError, match="F6"):
        deploy.validate_compose(config)
    config = valid_config()
    config["services"]["app"]["environment"]["DISABLE_REGISTRATION"] = "false"
    with pytest.raises(ValueError, match="F6"):
        deploy.validate_compose(config)


def test_f6_compose_requires_reachable_build_mirrors():
    config = valid_config()
    config["services"]["app"]["build"]["args"]["GOPROXY_ARG"] = ""
    with pytest.raises(ValueError, match="Go module proxy"):
        deploy.validate_compose(config)
    config = valid_config()
    config["services"]["frontend"]["build"]["args"]["CNZ_DOCKERHUB_MIRROR_ARG"] = "docker.io"
    with pytest.raises(ValueError, match="Docker Hub mirror"):
        deploy.validate_compose(config)


def test_f7_build_precedes_startup_and_failed_start_preserves_volumes():
    assert deploy.DEPLOY.index("compose build app frontend docreader") < deploy.DEPLOY.index("compose up -d")
    assert "STARTED=1\nphase=service_start\ncompose up -d" in deploy.DEPLOY
    assert 'echo "DEPLOY_FAILED_PHASE=$phase"' in deploy.DEPLOY
    assert 'phase=image_build' in deploy.DEPLOY and 'phase=health_check' in deploy.DEPLOY
    assert "compose down --remove-orphans" in deploy.DEPLOY
    assert "down -v" not in deploy.DEPLOY and "--volumes" not in deploy.DEPLOY


def test_f8_f9_health_precedes_active_pointer():
    assert deploy.DEPLOY.index('test "$ready" -eq 1') < deploy.DEPLOY.index('mv -Tf')
    assert "http://127.0.0.1:39301/health" in deploy.DEPLOY
    assert "http://127.0.0.1:18082/" in deploy.DEPLOY
    assert "ACTIVE_RELEASE=$sha" in deploy.DEPLOY
