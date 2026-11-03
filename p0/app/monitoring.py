"""P1-6 observability: request metrics, structured access logs and gauges.

Prometheus is not required for the app to run.  ``/metrics`` renders the small
set of series the deployment actually needs; the external
``scripts/monitor_check.py`` adds blackbox checks (TLS, backup age, disk).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_STARTED = time.time()


def _path_group(path: str) -> str:
    """Collapse IDs so request metrics stay low-cardinality."""
    parts = [part for part in (path or "").split("/") if part]
    if not parts:
        return "/"
    grouped = []
    for part in parts:
        if part.isdigit() or len(part) > 32 or part.startswith(("std_", "q_", "task_", "file_")):
            grouped.append(":id")
        else:
            grouped.append(part)
    return "/" + "/".join(grouped)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        self._duration: dict[tuple[str, str], tuple[float, int]] = {}

    def observe(self, method: str, path: str, status: int, seconds: float) -> None:
        route = _path_group(path)
        key = (method.upper(), route, int(status))
        duration_key = (method.upper(), route)
        with self._lock:
            self._requests[key] = self._requests.get(key, 0) + 1
            total, count = self._duration.get(duration_key, (0.0, 0))
            self._duration[duration_key] = (total + max(0.0, seconds), count + 1)

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()
            self._duration.clear()

    def request_totals(self) -> dict:
        with self._lock:
            by_status: dict[str, int] = {}
            total = 0
            for (_method, _route, status), count in self._requests.items():
                total += count
                bucket = f"{status // 100}xx"
                by_status[bucket] = by_status.get(bucket, 0) + count
            return {"total": total, "by_status": by_status, "series": len(self._requests)}

    def render(self) -> str:
        lines = [
            "# HELP p0_up P0 API process is up",
            "# TYPE p0_up gauge",
            "p0_up 1",
            "# HELP p0_uptime_seconds Process uptime",
            "# TYPE p0_uptime_seconds gauge",
            f"p0_uptime_seconds {time.time() - _STARTED:.1f}",
        ]
        with self._lock:
            if self._requests:
                lines += [
                    "# HELP p0_http_requests_total HTTP requests by method/route/status",
                    "# TYPE p0_http_requests_total counter",
                ]
                for (method, route, status), count in sorted(self._requests.items()):
                    lines.append(
                        f'p0_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
                    )
            if self._duration:
                lines += [
                    "# HELP p0_http_request_duration_seconds_sum Request duration sum",
                    "# TYPE p0_http_request_duration_seconds_sum counter",
                    "# HELP p0_http_request_duration_seconds_count Request duration count",
                    "# TYPE p0_http_request_duration_seconds_count counter",
                ]
                for (method, route), (total, count) in sorted(self._duration.items()):
                    lines.append(
                        f'p0_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {total:.6f}'
                    )
                    lines.append(
                        f'p0_http_request_duration_seconds_count{{method="{method}",route="{route}"}} {count}'
                    )
        lines.extend(_task_lines())
        lines.extend(_disk_lines())
        lines.extend(_backup_lines())
        lines.extend(_store_lines())
        lines.extend(_weknora_lines())
        return "\n".join(lines) + "\n"


METRICS = Metrics()


def _task_lines() -> list[str]:
    lines = [
        "# HELP p0_tasks Background tasks by status",
        "# TYPE p0_tasks gauge",
    ]
    try:
        from .store import tasks

        if not tasks.enabled():
            return lines + ["# task queue disabled"]
        stats = tasks.stats()
    except Exception:  # noqa: BLE001 - metrics must never fail the scrape
        return lines + ["p0_tasks_db_error 1"]
    for status in ("queued", "running", "succeeded", "failed", "cancelled"):
        lines.append(f'p0_tasks{{status="{status}"}} {stats.get(status, 0)}')
    return lines


def _disk_lines() -> list[str]:
    target = Path(os.getenv("P0_DATA_DIR", ROOT / "data")).expanduser()
    probe = target if target.exists() else target.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError:
        return ["p0_disk_used_ratio 0", "p0_disk_free_bytes 0"]
    ratio = usage.used / usage.total if usage.total else 0.0
    return [
        "# HELP p0_disk_used_ratio Filesystem used ratio for P0 data",
        "# TYPE p0_disk_used_ratio gauge",
        f"p0_disk_used_ratio {ratio:.4f}",
        "# HELP p0_disk_free_bytes Filesystem free bytes",
        "# TYPE p0_disk_free_bytes gauge",
        f"p0_disk_free_bytes {usage.free}",
    ]


def _latest_backup() -> Path | None:
    backup_dir = Path(os.getenv("P0_BACKUP_DIR", ROOT / "backups")).expanduser()
    if not backup_dir.exists():
        return None
    candidates = [
        path
        for pattern in ("p0-data-*.tar.gz", "p0-store-*.json", "p0-pg-*.dump", "p0-objects-*.tar.gz")
        for path in backup_dir.glob(pattern)
        if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _backup_lines() -> list[str]:
    latest = _latest_backup()
    if latest is None:
        return [
            "# HELP p0_backup_age_seconds Seconds since the latest backup (-1 when absent)",
            "# TYPE p0_backup_age_seconds gauge",
            "p0_backup_age_seconds -1",
        ]
    age = max(0.0, time.time() - latest.stat().st_mtime)
    return [
        "# HELP p0_backup_age_seconds Seconds since the latest backup (-1 when absent)",
        "# TYPE p0_backup_age_seconds gauge",
        f"p0_backup_age_seconds {age:.0f}",
    ]


def _store_lines() -> list[str]:
    lines = ["# HELP p0_store_up Storage backend reachable", "# TYPE p0_store_up gauge"]
    try:
        from . import store

        if not store.config.postgres_enabled():
            return lines + ["p0_store_up 1"]
        from sqlalchemy import text

        with store.db.engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return lines + ["p0_store_up 1"]
    except Exception:  # noqa: BLE001 - report 0 instead of failing scrape
        return lines + ["p0_store_up 0"]


def _weknora_lines() -> list[str]:
    try:
        from . import weknora

        configured = weknora.configured()
        required = weknora.required()
    except Exception:  # noqa: BLE001
        configured = required = False
    return [
        "# HELP p0_weknora_configured WeKnora base URL/key/KB configured",
        "# TYPE p0_weknora_configured gauge",
        f"p0_weknora_configured {1 if configured else 0}",
        "# HELP p0_weknora_required Publication/QA must use WeKnora",
        "# TYPE p0_weknora_required gauge",
        f"p0_weknora_required {1 if required else 0}",
    ]


def snapshot() -> dict:
    """JSON view used by the ops monitor (not a Prometheus scrape)."""
    tasks: dict = {}
    store_up = True
    try:
        from . import store

        if store.config.postgres_enabled():
            from sqlalchemy import text

            with store.db.engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            from .store import tasks as task_repo

            if task_repo.enabled():
                tasks = task_repo.stats()
    except Exception:  # noqa: BLE001
        store_up = False
    target = Path(os.getenv("P0_DATA_DIR", ROOT / "data")).expanduser()
    probe = target if target.exists() else target.parent
    try:
        usage = shutil.disk_usage(str(probe))
        disk = {
            "used_ratio": round(usage.used / usage.total, 4) if usage.total else 0.0,
            "free_bytes": usage.free,
        }
    except OSError:
        disk = {"used_ratio": 0.0, "free_bytes": 0}
    latest = _latest_backup()
    backup = {
        "path": str(latest) if latest else "",
        "age_seconds": round(time.time() - latest.stat().st_mtime) if latest else None,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uptime_seconds": round(time.time() - _STARTED, 1),
        "requests": METRICS.request_totals(),
        "tasks": tasks,
        "store_up": store_up,
        "disk": disk,
        "backup": backup,
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "ms", "user", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    if os.getenv("P0_LOG_FORMAT", "").strip().lower() not in {"json", "structured"}:
        return
    root = logging.getLogger()
    if getattr(root, "_p0_json", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("P0_LOG_LEVEL", "INFO").upper())
    root._p0_json = True  # type: ignore[attr-defined]


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
