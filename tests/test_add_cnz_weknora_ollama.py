from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "add_cnz_weknora_ollama", ROOT / "scripts/add_cnz_weknora_ollama.py"
)
assert SPEC and SPEC.loader
add = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(add)


def completed(stdout: str = "", code: int = 0):
    return subprocess.CompletedProcess([], code, stdout.encode(), b"")


def test_wrong_target_and_bad_backup_stop_before_main_or_ssh(monkeypatch, tmp_path):
    monkeypatch.setattr(add.base, "main_sha", lambda: pytest.fail("must stop before git"))
    calls = []
    monkeypatch.setattr(add, "remote", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="only yunan-101"):
        add.install("another-host", "/var/backups/cnz-weknora/snapshot")
    with pytest.raises(ValueError, match="direct child"):
        add.install("yunan-101", "/var/backups/cnz-weknora/snapshot;id")
    assert calls == []


def test_failure_inventory_is_covered_by_preflight_and_rollback_guards():
    for token in (
        "sha256sum -c SHA256SUMS", "INCOMPLETE", "database_schema=138|f",
        "expected_containers", "yunan-persist", "OLLAMA_PREFLIGHT_OK",
    ):
        assert token in add.PREFLIGHT
    for token in (
        "flock -n 9", "up -d --no-deps ollama", "docker rm -f cnz-weknora-ollama",
        "OLLAMA_CONTAINER_REMOVED_VOLUME_PRESERVED", "cnz-weknora_ollama-data",
        "old_ids", "schema_before", "active-release",
    ):
        assert token in add.INSTALL
    assert "docker compose down" not in add.INSTALL
    assert "docker volume rm" not in add.INSTALL
    assert "down -v" not in add.INSTALL


def test_configure_verifies_the_same_main_marker_created_by_bundle_builder():
    assert 'grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_MAIN"' in add.CONFIGURE
    assert 'grep -Fxq "$sha" "$incoming/DEPLOYED_FROM_CODE"' not in add.CONFIGURE
    assert "OLLAMA_CONFIGURE_FAILED_PHASE" in add.CONFIGURE
    assert "activate_staged_release" in add.CONFIGURE


def test_remote_scripts_parse_and_deployment_targets_are_constrained(tmp_path):
    for name, script in (("preflight", add.PREFLIGHT), ("configure", add.CONFIGURE), ("install", add.INSTALL)):
        path = tmp_path / f"{name}.sh"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert add.TARGETS == {"yunan-101"}
    assert "OLLAMA_HOST" in add.INSTALL
    assert "ports" in add.INSTALL


def test_preflight_rejection_stops_before_transfer(monkeypatch, tmp_path):
    sha = "a" * 40
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    upstream = tmp_path / "upstream.tar.gz"
    upstream.write_bytes(b"synthetic upstream")
    monkeypatch.setattr(add.base, "main_sha", lambda: sha)
    monkeypatch.setattr(add.base, "make_bundle", lambda *_: (bundle, "d" * 64))
    calls = []
    monkeypatch.setattr(add, "remote", lambda *a, **k: calls.append(a) or completed("F2_BACKUP_PATH", 21))
    monkeypatch.setattr(add.base, "stream_bundle", lambda *_: pytest.fail("must not transfer"))
    with pytest.raises(RuntimeError, match="preflight rejected"):
        add.install("yunan-101", "/var/backups/cnz-weknora/snapshot", upstream)
    assert len(calls) == 1


def test_install_switch_requires_release_ready_health_and_postcheck_markers(monkeypatch, tmp_path):
    sha, old = "a" * 40, "b" * 40
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    upstream = tmp_path / "upstream.tar.gz"
    upstream.write_bytes(b"synthetic upstream")
    monkeypatch.setattr(add.base, "main_sha", lambda: sha)
    monkeypatch.setattr(add.base, "make_bundle", lambda *_: (bundle, "d" * 64))
    responses = [
        completed(f"OLLAMA_PREFLIGHT_OK\nOLD_SHA={old}\nRUNTIME_SHA={old}\n"),
        completed(),
        completed("OLLAMA_RELEASE_READY\n"),
        completed(f"OLLAMA_RUNTIME_OK\nACTIVE_RELEASE={sha}\n"),
    ]
    scripts = []

    def fake_remote(target, script, *args, **kwargs):
        scripts.append(script)
        return responses.pop(0)

    monkeypatch.setattr(add, "remote", fake_remote)
    monkeypatch.setattr(add.base, "stream_bundle", lambda *_: completed())
    monkeypatch.setattr(add.base, "cleanup_incoming", lambda *_: None)
    add.install("yunan-101", "/var/backups/cnz-weknora/snapshot", upstream)
    assert scripts[0] == add.PREFLIGHT
    assert scripts[-1] == add.INSTALL
    assert len(scripts) == 4


def test_missing_runtime_marker_is_reported_as_failure(monkeypatch, tmp_path):
    sha, old = "a" * 40, "b" * 40
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    upstream = tmp_path / "upstream.tar.gz"
    upstream.write_bytes(b"synthetic upstream")
    monkeypatch.setattr(add.base, "main_sha", lambda: sha)
    monkeypatch.setattr(add.base, "make_bundle", lambda *_: (bundle, "d" * 64))
    responses = [
        completed(f"OLLAMA_PREFLIGHT_OK\nOLD_SHA={old}\nRUNTIME_SHA={old}\n"),
        completed(),
        completed("OLLAMA_RELEASE_READY\n"),
        completed("missing marker"),
    ]
    monkeypatch.setattr(add, "remote", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(add.base, "stream_bundle", lambda *_: completed())
    monkeypatch.setattr(add.base, "cleanup_incoming", lambda *_: None)
    with pytest.raises(RuntimeError, match="completion marker missing"):
        add.install("yunan-101", "/var/backups/cnz-weknora/snapshot", upstream)


def test_configure_failure_reports_safe_stage_and_stops_before_service_start(monkeypatch, tmp_path):
    sha, old = "a" * 40, "b" * 40
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    upstream = tmp_path / "upstream.tar.gz"
    upstream.write_bytes(b"synthetic upstream")
    monkeypatch.setattr(add.base, "main_sha", lambda: sha)
    monkeypatch.setattr(add.base, "make_bundle", lambda *_: (bundle, "d" * 64))
    responses = [
        completed(f"OLLAMA_PREFLIGHT_OK\nOLD_SHA={old}\nRUNTIME_SHA={old}\n"),
        completed(),
        completed("OLLAMA_CONFIGURE_FAILED_PHASE=verify_release_inputs\n", 2),
    ]
    scripts = []
    monkeypatch.setattr(add, "remote", lambda target, script, *a, **k: scripts.append(script) or responses.pop(0))
    monkeypatch.setattr(add.base, "stream_bundle", lambda *_: completed())
    cleaned = []
    monkeypatch.setattr(add.base, "cleanup_incoming", lambda *a: cleaned.append(a))
    with pytest.raises(RuntimeError, match="verify_release_inputs; runtime remains unchanged"):
        add.install("yunan-101", "/var/backups/cnz-weknora/snapshot", upstream)
    assert scripts[-1] == add.CONFIGURE
    assert add.INSTALL not in scripts
    assert cleaned == [("yunan-101", sha)]


def test_ollama_override_joins_only_the_cnz_weknora_network():
    override = (ROOT / "weknora/docker-compose.testkb.override.yml").read_text(encoding="utf-8")
    ollama = override.split("  ollama:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "    networks:\n      - WeKnora-network\n" in ollama
    assert 'set(v.get("networks",{}))=={"WeKnora-network"}' in add.INSTALL
    guide = (ROOT / "docs/cnz-ollama-runtime-addition.md").read_text(encoding="utf-8")
    assert "uv run --python 3.12 python scripts/add_cnz_weknora_ollama.py" in guide
    assert "55 项通过" in guide
