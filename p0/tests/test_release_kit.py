"""Release bundle build/verify and post-deploy HTTP checks."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from scripts import release_kit


def make_tree(tmp_path: Path, version: str = "9.9.9") -> Path:
    root = tmp_path / "p0"
    (root / "app").mkdir(parents=True)
    (root / "static").mkdir()
    (root / "scripts").mkdir()
    (root / "app" / "main.py").write_text(f'version="{version}"\n', encoding="utf-8")
    (root / "static" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (root / "scripts" / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


def test_build_and_verify_roundtrip(tmp_path):
    root = make_tree(tmp_path)
    built = release_kit.build_bundle(root, tmp_path / "dist")
    assert built["version"] == "9.9.9"
    assert Path(built["bundle"]).exists()
    result = release_kit.verify_bundle(Path(built["bundle"]))
    assert result["ok"] is True
    assert result["version"] == "9.9.9"
    assert result["files"] >= 5


def test_verify_rejects_wrong_sidecar(tmp_path):
    root = make_tree(tmp_path)
    built = release_kit.build_bundle(root, tmp_path / "dist")
    sidecar = Path(built["sidecar"])
    sidecar.write_text("0" * 64 + "  fake\n", encoding="utf-8")
    with pytest.raises(release_kit.ReleaseError):
        release_kit.verify_bundle(Path(built["bundle"]))


def test_verify_rejects_tampered_file(tmp_path):
    root = make_tree(tmp_path)
    built = release_kit.build_bundle(root, tmp_path / "dist")
    bundle = Path(built["bundle"])
    # Re-pack with modified static/index.html and a matching sidecar hash, so
    # the manifest hash check (not the sidecar check) must catch it.
    original = tmp_path / "original.tar.gz"
    original.write_bytes(bundle.read_bytes())
    target = "p0-9.9.9/static/index.html"
    with tarfile.open(original, "r:gz") as source, tarfile.open(bundle, "w:gz") as sink:
        for member in source.getmembers():
            data = source.extractfile(member).read() if member.isfile() else None
            if member.name == target:
                data = b"<html>tampered</html>\n"
                member.size = len(data)
            if data is None:
                sink.addfile(member)
            else:
                member.size = len(data)
                sink.addfile(member, io.BytesIO(data))
    import hashlib

    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    Path(built["sidecar"]).write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    with pytest.raises(release_kit.ReleaseError):
        release_kit.verify_bundle(bundle)


def _client(version: str, metrics_status: int = 200, auth: str = "enabled") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "version": version, "auth": auth})
        if request.url.path.endswith("/metrics"):
            if metrics_status != 200:
                return httpx.Response(metrics_status, text="denied")
            return httpx.Response(200, text="# HELP p0_up\np0_up 1\n")
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_http_passes_for_matching_release():
    result = release_kit.check_http(
        "http://p0.local/p0", "9.9.9", client=_client("9.9.9")
    )
    assert result["ok"] is True
    assert {item["name"] for item in result["checks"]} == {"health", "version", "auth", "metrics"}


def test_check_http_fails_on_version_mismatch_and_auth_disabled():
    result = release_kit.check_http(
        "http://p0.local/p0", "9.9.9", client=_client("9.9.8", auth="disabled")
    )
    assert result["ok"] is False
    failed = {item["name"] for item in result["checks"] if not item["ok"]}
    assert failed == {"version", "auth"}


def test_check_http_allows_metrics_403():
    result = release_kit.check_http(
        "http://p0.local/p0", "9.9.9", client=_client("9.9.9", metrics_status=403)
    )
    assert result["ok"] is True
    metrics = next(item for item in result["checks"] if item["name"] == "metrics")
    assert "skipped" in metrics["detail"]
