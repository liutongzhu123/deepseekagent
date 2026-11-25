"""Guardrails for the frontend-only CNZ WeKnora upgrade and rollback path."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upgrade_cnz_weknora_frontend", ROOT / "scripts/upgrade_cnz_weknora_frontend.py"
)
upgrade = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(upgrade)


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout.encode(), stderr.encode())


def test_preflight_requires_isolated_healthy_cnz_and_valid_backup():
    for required in (
        "sha256sum -c SHA256SUMS",
        "UPGRADE_PREFLIGHT_OK",
        "active-release",
        "test \"$old_sha\" != \"$sha\"",
        "expected_containers",
        "cnz-weknora_postgres-data",
        "com.docker.compose.project",
        "yunan-persist",
        "10485760",
        "127.0.0.1:18082",
    ):
        assert required in upgrade.UPGRADE_PREFLIGHT


def test_frontend_update_rebuilds_only_ui_and_rolls_back_on_failure():
    script = upgrade.UPGRADE
    assert "compose_new build frontend" in script
    assert "compose_new up -d --no-deps frontend" in script
    assert "compose_old up -d --no-deps frontend" in script
    assert script.index("compose_new build frontend") < script.index("frontend_switch_started=1") < script.index("compose_new up -d --no-deps frontend")
    assert "cnz-weknora-ui:rollback-$old_sha" in script
    assert "FRONTEND_ROLLBACK_OK_APP_AND_VOLUMES_UNTOUCHED" in script
    assert "down -v" not in script and "--volumes" not in script
    assert "compose_new up -d --no-deps app" not in script
    assert script.index("wait_frontend \"$new_image\"") < script.index('mv -Tf "$root/active-release.new.$sha"')


def test_remote_shell_scripts_parse(tmp_path):
    for name, script in (
        ("preflight", upgrade.UPGRADE_PREFLIGHT),
        ("configure", upgrade.CONFIGURE_UPGRADE),
        ("upgrade", upgrade.UPGRADE),
    ):
        path = tmp_path / f"{name}.sh"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_remote_source_fingerprint_matches_local_fingerprint(tmp_path):
    start = upgrade.UPGRADE.index("fingerprint_source() {")
    end = upgrade.UPGRADE.index("\ncompose_new()", start)
    function = upgrade.UPGRADE[start:end]
    (tmp_path / "file.txt").write_text("synthetic knowledge only\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=omitted\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", function + '\nfingerprint_source "$1"', "bash", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == upgrade.base.source_fingerprint(tmp_path)


def test_success_path_bundles_preflights_transfers_and_confirms_switch(monkeypatch, tmp_path):
    sha = "a" * 40
    old_sha = "b" * 40
    old_image = "sha256:" + "c" * 64
    fingerprint = "d" * 64
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    monkeypatch.setattr(upgrade.base, "main_sha", lambda: sha)
    monkeypatch.setattr(upgrade.base, "make_bundle", lambda *_: (bundle, fingerprint))
    calls = []
    upgrade_calls = []

    def fake_remote(target, script, got_sha, timeout=300, source_fingerprint_value=None):
        calls.append((target, script, got_sha, timeout, source_fingerprint_value))
        if script == upgrade.UPGRADE_PREFLIGHT:
            return completed(f"UPGRADE_PREFLIGHT_OK\nOLD_SHA={old_sha}\nOLD_IMAGE={old_image}\n")
        if script == upgrade.CONFIGURE_UPGRADE:
            return completed("FRONTEND_RELEASE_READY\n")
        return completed()

    monkeypatch.setattr(upgrade.base, "remote", fake_remote)
    def fake_upgrade(target, got_sha, got_old_sha, got_old_image, got_fingerprint):
        upgrade_calls.append((target, got_sha, got_old_sha, got_old_image, got_fingerprint))
        return completed(f"FRONTEND_UPGRADE_OK_APP_AND_VOLUMES_UNTOUCHED\nACTIVE_RELEASE={sha}\n")
    monkeypatch.setattr(upgrade, "remote_upgrade", fake_upgrade)
    monkeypatch.setattr(upgrade.base, "stream_bundle", lambda *_: completed())
    monkeypatch.setattr(upgrade.base, "cleanup_incoming", lambda *_: None)
    upgrade.upgrade("testkb", tmp_path / "unused-upstream.tar.gz")

    assert calls[0][1] == upgrade.UPGRADE_PREFLIGHT
    assert "install -d -m 0755" in calls[1][1]
    assert calls[2][1] == upgrade.CONFIGURE_UPGRADE
    assert calls[0][4] == fingerprint
    assert upgrade_calls == [("testkb", sha, old_sha, old_image, fingerprint)]


def test_remote_upgrade_passes_each_verified_value_as_a_separate_argument(monkeypatch):
    captured = {}
    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs["input"]
        return completed()
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.remote_upgrade("testkb", "a" * 40, "b" * 40, "sha256:" + "c" * 64, "d" * 64)
    assert captured["args"][-4:] == ["a" * 40, "b" * 40, "sha256:" + "c" * 64, "d" * 64]
    assert captured["input"] == upgrade.UPGRADE.encode()


def test_preflight_rejection_stops_before_transfer(monkeypatch, tmp_path):
    sha = "a" * 40
    fingerprint = "d" * 64
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    monkeypatch.setattr(upgrade.base, "main_sha", lambda: sha)
    monkeypatch.setattr(upgrade.base, "make_bundle", lambda *_: (bundle, fingerprint))
    calls = []
    monkeypatch.setattr(upgrade.base, "remote", lambda *a, **k: calls.append(a) or completed("", 1))
    monkeypatch.setattr(upgrade.base, "stream_bundle", lambda *_: pytest.fail("must not transfer"))
    with pytest.raises(RuntimeError, match="preflight rejected"):
        upgrade.upgrade("testkb", tmp_path / "unused-upstream.tar.gz")
    assert len(calls) == 1


def test_failed_transfer_cleans_only_incoming_release(monkeypatch, tmp_path):
    sha = "a" * 40
    old_sha = "b" * 40
    old_image = "sha256:" + "c" * 64
    fingerprint = "d" * 64
    bundle = tmp_path / "release.tar.gz"
    bundle.write_bytes(b"synthetic bundle")
    monkeypatch.setattr(upgrade.base, "main_sha", lambda: sha)
    monkeypatch.setattr(upgrade.base, "make_bundle", lambda *_: (bundle, fingerprint))
    outputs = [
        completed(f"UPGRADE_PREFLIGHT_OK\nOLD_SHA={old_sha}\nOLD_IMAGE={old_image}\n"),
        completed(),
    ]
    monkeypatch.setattr(upgrade.base, "remote", lambda *a, **k: outputs.pop(0))
    monkeypatch.setattr(upgrade.base, "stream_bundle", lambda *_: completed("", 1))
    cleaned = []
    monkeypatch.setattr(upgrade.base, "cleanup_incoming", lambda *a: cleaned.append(a))
    with pytest.raises(RuntimeError, match="source transfer failed"):
        upgrade.upgrade("testkb", tmp_path / "unused-upstream.tar.gz")
    assert cleaned == [("testkb", sha)]
