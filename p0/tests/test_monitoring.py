import logging
import time
from argparse import Namespace
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, monitoring
from scripts import monitor_check


def test_metrics_render_contains_expected_series():
    metrics = monitoring.Metrics()
    metrics.observe("GET", "/api/v1/standards/std_20260101000000_abcdef", 200, 0.012)
    metrics.observe("POST", "/api/v1/standards/parse", 500, 0.5)
    text = metrics.render()
    assert 'p0_http_requests_total{method="GET",route="/api/v1/standards/:id",status="200"} 1' in text
    assert 'p0_http_requests_total{method="POST",route="/api/v1/standards/parse",status="500"} 1' in text
    assert "p0_http_request_duration_seconds_sum" in text
    assert "p0_disk_used_ratio" in text
    assert "p0_backup_age_seconds" in text


def test_metrics_endpoint_requires_token_and_echoes_request_id(monkeypatch):
    monkeypatch.setenv("P0_METRICS_TOKEN", "ops-token")
    client = TestClient(main.app)
    assert client.get("/metrics").status_code == 403
    response = client.get("/metrics", headers={"X-Metrics-Token": "ops-token", "X-Request-ID": "req-42"})
    assert response.status_code == 200
    assert "p0_up 1" in response.text
    assert response.headers["x-request-id"] == "req-42"


def test_metrics_endpoint_generates_request_id(monkeypatch):
    monkeypatch.setenv("P0_METRICS_TOKEN", "ops-token")
    client = TestClient(main.app)
    response = client.get("/health")
    assert response.headers.get("x-request-id")


def test_monitor_snapshot_uses_disk_and_backup(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "p0-data-20260101T000000Z.tar.gz"
    archive.write_bytes(b"x")
    monkeypatch.setenv("P0_DATA_DIR", str(data))
    monkeypatch.setenv("P0_BACKUP_DIR", str(backups))
    snapshot = monitoring.snapshot()
    assert snapshot["disk"]["used_ratio"] >= 0
    assert snapshot["backup"]["age_seconds"] is not None
    assert snapshot["backup"]["age_seconds"] >= 0


def test_json_formatter_outputs_structured_fields():
    formatter = monitoring.JsonFormatter()
    record = logging.LogRecord(
        "p0.test", logging.INFO, __file__, 1, "hello", (), None
    )
    record.request_id = "req-1"
    record.status = 200
    text = formatter.format(record)
    assert '"message": "hello"' in text
    assert '"request_id": "req-1"' in text
    assert '"status": 200' in text


def test_monitor_check_pure_helpers(monkeypatch, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "p0-data-20260101T000000Z.tar.gz"
    archive.write_bytes(b"x")
    assert monitor_check.latest_backup(backups) == archive
    assert monitor_check.disk_ratio(tmp_path) >= 0
    parsed = monitor_check.parse_metrics('p0_tasks{status="failed"} 2\np0_up 1\n')
    assert parsed['p0_tasks{status="failed"}'] == 2
    args = Namespace(
        base_url="http://127.0.0.1:1", timeout=0.2, metrics_token="",
        max_failed=0, max_queued=10, data_dir=str(tmp_path), max_disk=0.99,
        backup_dir=str(backups), max_backup_hours=26, tls_host="", tls_port=443,
        min_cert_days=21,
    )
    report = monitor_check.evaluate(args)
    names = {check["name"] for check in report["checks"]}
    assert {"disk", "backup"} <= names
    assert report["ok"] is False  # unreachable service is a failure


def test_metrics_endpoint_rejects_without_token_in_production(monkeypatch):
    monkeypatch.delenv("P0_METRICS_TOKEN", raising=False)
    monkeypatch.setenv("P0_ENV", "production")
    response = TestClient(main.app).get("/metrics")
    assert response.status_code == 403
