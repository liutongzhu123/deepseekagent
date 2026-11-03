#!/usr/bin/env python3
"""P1-6 blackbox/timer check for service, queue, disk, backup and TLS.

The web process exposes ``/metrics`` for Prometheus (local-only unless
``P0_METRICS_TOKEN`` is set).  This script is the deployment-independent check
that can run from a systemd timer even when Prometheus/Alertmanager are not yet
installed.  It exits non-zero on a threshold breach with ``--strict``.

    .venv/bin/python scripts/monitor_check.py \
      --base-url http://127.0.0.1:8090 --metrics-token "$P0_METRICS_TOKEN" \
      --data-dir /opt/cq-ai-p0/data --backup-dir /var/backups/cq-ai-p0 \
      --tls-host <DOMAIN> --strict
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKUP_PATTERNS = ("p0-data-*.tar.gz", "p0-store-*.json", "p0-pg-*.dump", "p0-objects-*.tar.gz")


def latest_backup(backup_dir: Path) -> Path | None:
    if not backup_dir.exists():
        return None
    candidates = [
        path
        for pattern in BACKUP_PATTERNS
        for path in backup_dir.glob(pattern)
        if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def disk_ratio(path: Path) -> float:
    target = path if path.exists() else path.parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError:
        return 0.0
    return usage.used / usage.total if usage.total else 0.0


def cert_expiry_seconds(host: str, port: int = 443) -> float | None:
    if not host:
        return None
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
        not_after = cert.get("notAfter", "")
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (expires - datetime.now(timezone.utc)).total_seconds()
    except Exception:  # noqa: BLE001 - report as unknown
        return None


def parse_metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return values


def _http_check(base_url: str, timeout: float, metrics_token: str) -> tuple[list[dict], str]:
    checks: list[dict] = []
    metrics_text = ""
    started = time.perf_counter()
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
            health = client.get("/health")
            latency_ms = (time.perf_counter() - started) * 1000
            payload = health.json() if health.status_code == 200 else {}
            checks.append(
                {
                    "name": "service",
                    "ok": health.status_code == 200,
                    "detail": f"HTTP {health.status_code} version={payload.get('version', '')}",
                    "value": round(latency_ms, 1),
                    "unit": "ms",
                }
            )
            checks.append(
                {
                    "name": "health_latency",
                    "ok": health.status_code == 200 and latency_ms < timeout * 1000,
                    "detail": f"{latency_ms:.0f} ms",
                    "value": round(latency_ms, 1),
                    "unit": "ms",
                }
            )
            if metrics_token:
                headers = {"X-Metrics-Token": metrics_token}
                metrics = client.get("/metrics", headers=headers)
                if metrics.status_code == 200:
                    metrics_text = metrics.text
                checks.append(
                    {
                        "name": "metrics",
                        "ok": metrics.status_code == 200,
                        "detail": f"HTTP {metrics.status_code}",
                        "value": metrics.status_code,
                    }
                )
    except Exception as exc:  # noqa: BLE001 - network boundary
        checks.append({"name": "service", "ok": False, "detail": str(exc), "value": 0})
    return checks, metrics_text


def evaluate(args: argparse.Namespace) -> dict:
    checks, metrics_text = _http_check(args.base_url, args.timeout, args.metrics_token)
    metrics = parse_metrics(metrics_text)
    if metrics:
        failed = metrics.get('p0_tasks{status="failed"}', 0.0)
        queued = metrics.get('p0_tasks{status="queued"}', 0.0)
        checks.append(
            {
                "name": "task_failed",
                "ok": failed <= args.max_failed,
                "detail": f"{failed:.0f} failed / {queued:.0f} queued",
                "value": failed,
            }
        )
        checks.append(
            {
                "name": "task_queued",
                "ok": queued <= args.max_queued,
                "detail": f"{queued:.0f} queued",
                "value": queued,
            }
        )
        store_up = metrics.get("p0_store_up", 1.0)
        checks.append({"name": "store", "ok": store_up >= 1, "detail": f"p0_store_up={store_up}", "value": store_up})
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    if data_dir:
        ratio = disk_ratio(data_dir)
        checks.append(
            {
                "name": "disk",
                "ok": ratio < args.max_disk,
                "detail": f"{ratio:.1%} used",
                "value": round(ratio, 4),
            }
        )
    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else None
    if backup_dir:
        latest = latest_backup(backup_dir)
        age_seconds = time.time() - latest.stat().st_mtime if latest else None
        checks.append(
            {
                "name": "backup",
                "ok": age_seconds is not None and age_seconds < args.max_backup_hours * 3600,
                "detail": (
                    f"{latest.name} / {age_seconds / 3600:.1f} h"
                    if latest and age_seconds is not None
                    else "no backup archive"
                ),
                "value": round(age_seconds) if age_seconds is not None else None,
                "unit": "s",
            }
        )
    if args.tls_host:
        remaining = cert_expiry_seconds(args.tls_host, args.tls_port)
        checks.append(
            {
                "name": "tls",
                "ok": remaining is not None and remaining > args.min_cert_days * 86400,
                "detail": (
                    f"{remaining / 86400:.1f} days remaining"
                    if remaining is not None
                    else "certificate check failed"
                ),
                "value": round(remaining) if remaining is not None else None,
                "unit": "s",
            }
        )
    failures = [check for check in checks if not check["ok"]]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "ok": not failures,
        "checks": checks,
        "failures": [check["name"] for check in failures],
        "metrics": metrics,
    }


def prometheus(report: dict) -> str:
    lines = [
        "# HELP p0_monitor_up External blackbox check overall",
        "# TYPE p0_monitor_up gauge",
        f"p0_monitor_up {1 if report['ok'] else 0}",
    ]
    for check in report["checks"]:
        name = "p0_monitor_" + check["name"]
        if check.get("value") is not None:
            lines.append(f'{name} {check["value"]}')
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--metrics-token", default=__import__("os").getenv("P0_METRICS_TOKEN", ""))
    parser.add_argument("--data-dir", default=__import__("os").getenv("P0_DATA_DIR", ""))
    parser.add_argument("--backup-dir", default=__import__("os").getenv("P0_BACKUP_DIR", ""))
    parser.add_argument("--tls-host", default=__import__("os").getenv("P0_MONITOR_TLS_HOST", ""))
    parser.add_argument("--tls-port", type=int, default=443)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--max-disk", type=float, default=0.85)
    parser.add_argument("--max-backup-hours", type=float, default=26.0)
    parser.add_argument("--min-cert-days", type=float, default=21.0)
    parser.add_argument("--max-failed", type=int, default=0)
    parser.add_argument("--max-queued", type=int, default=50)
    parser.add_argument("--prometheus", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args)
    if args.prometheus:
        print(prometheus(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
