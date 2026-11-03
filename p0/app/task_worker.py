"""In-process task worker for OCR/parse and knowledge-base publication.

One systemd service runs uvicorn; the worker is a small thread pool inside that
process, so no new Redis/queue service is needed.  Coordination is in
PostgreSQL (``tasks`` table with ``SKIP LOCKED`` claims and leases), which keeps
restart recovery and concurrency correct even if the process is later started
with more uvicorn workers.
"""
from __future__ import annotations

import os
import socket
import threading
import traceback
from datetime import datetime, timezone
from typing import Callable

from . import auth, standard_store
from .standard_parse import parse_pdf_standard
from .store import db, objects, tasks


class TaskCancelled(Exception):
    """Raised by a handler when the task was cancelled or the worker stopped."""


Handler = Callable[[dict, Callable[[str, int], None]], dict]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _handle_standard_parse(task: dict, report: Callable[[str, int], None]) -> dict:
    payload = task.get("payload") or {}
    file_id = str(payload.get("file_id", ""))
    if not file_id:
        raise ValueError("解析任务缺少源文件")
    with db.engine().connect() as conn:
        _, data = objects.read_file(conn, file_id)
    report("extract", 8)
    parsed = parse_pdf_standard(
        data,
        str(payload.get("file_name") or "standard.pdf"),
        enable_ocr=bool(payload.get("enable_ocr", True)),
        progress=report,
    )
    report("save", 92)
    record = standard_store.create(
        parsed,
        data,
        str(payload.get("file_name") or "standard.pdf"),
        project=str(payload.get("project", "")),
        created_by=str(payload.get("created_by", "")),
    )
    _audit_task(
        task,
        "standard_parsed",
        standard_id=record["id"],
        items=(record.get("summary") or {}).get("items", 0),
    )
    return {
        "standard_id": record["id"],
        "items": (record.get("summary") or {}).get("items", 0),
        "pages": (record.get("meta") or {}).get("pages", 0),
    }


def _handle_standard_publish(task: dict, report: Callable[[str, int], None]) -> dict:
    standard_id = str((task.get("payload") or {}).get("standard_id", ""))
    if not standard_id:
        raise ValueError("发布任务缺少 standard_id")
    report("load", 10)
    standard_store.load(standard_id)
    report("indexing", 45)
    result = standard_store.publish(standard_id)
    report("save", 92)
    _audit_task(task, "standard_published", standard_id=standard_id, chunks=result.get("chunks", 0))
    return {
        "standard_id": standard_id,
        "chunks": result.get("chunks", 0),
        "published_at": result.get("published_at", ""),
        "weknora": result.get("weknora"),
    }


def _audit_task(task: dict, event: str, **extra) -> None:
    created_by = str(task.get("created_by", ""))
    user = auth.find_user(created_by) if created_by else None
    auth.audit(
        {
            "user": created_by,
            "role": (user or {}).get("role", ""),
            "event": event,
            "method": "WORKER",
            "path": task.get("kind", ""),
            "status": 200,
            "task_id": task.get("id", ""),
            **extra,
        }
    )


def _audit_failure(task: dict, error: str) -> None:
    created_by = str(task.get("created_by", ""))
    user = auth.find_user(created_by) if created_by else None
    auth.audit(
        {
            "user": created_by,
            "role": (user or {}).get("role", ""),
            "event": "task_failed",
            "method": "WORKER",
            "path": task.get("kind", ""),
            "status": 500,
            "task_id": task.get("id", ""),
            "error": error[:500],
        }
    )


DEFAULT_HANDLERS: dict[str, Handler] = {
    "standard_parse": _handle_standard_parse,
    "standard_publish": _handle_standard_publish,
}


class TaskWorker:
    def __init__(self, handlers: dict[str, Handler] | None = None, concurrency: int | None = None):
        self.handlers = dict(DEFAULT_HANDLERS)
        if handlers:
            self.handlers.update(handlers)
        self.concurrency = concurrency or tasks.concurrency()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._active = 0
        self._started = False
        self._recovered = 0

    @property
    def running(self) -> bool:
        return self._started and not self._stop.is_set()

    def start(self) -> bool:
        if not tasks.enabled():
            return False
        with self._lock:
            if self._started:
                return True
            self._recovered = tasks.recover_stale()
            self._stop.clear()
            self._threads = [
                threading.Thread(
                    target=self._loop,
                    name=f"p0-task-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.concurrency)
            ]
            for thread in self._threads:
                thread.start()
            self._started = True
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._stop.set()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)
        with self._lock:
            self._started = False

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": tasks.enabled(),
                "running": self.running,
                "concurrency": self.concurrency,
                "active": self._active,
                "recovered": self._recovered,
            }

    def _loop(self) -> None:
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{threading.current_thread().name}"
        while not self._stop.is_set():
            try:
                task = tasks.claim_next(worker_id)
            except Exception:  # noqa: BLE001 - DB hiccup must not kill the worker
                self._stop.wait(2.0)
                continue
            if task is None:
                self._stop.wait(float(os.getenv("P0_TASKS_POLL_SECONDS", "0.5")))
                continue
            self._run(task)

    def _run(self, task: dict) -> None:
        task_id = task["id"]
        with self._lock:
            self._active += 1
        try:
            handler = self.handlers.get(task.get("kind", ""))
            if handler is None:
                raise ValueError(f"未注册的任务类型：{task.get('kind')}")

            def report(stage: str, progress: int) -> None:
                if self._stop.is_set():
                    raise TaskCancelled("worker stopping")
                if tasks.cancel_requested(task_id):
                    raise TaskCancelled("cancelled")
                tasks.heartbeat(task_id, stage, progress)

            report(task.get("stage") or "running", int(task.get("progress", 0)))
            result = handler(task, report)
            # A cancellation request is checked before every stage; once the
            # handler returns its side effects exist, so record success rather
            # than a misleading cancelled status.
            tasks.finish(task_id, "succeeded", result=result or {})
        except TaskCancelled:
            tasks.finish(task_id, "cancelled", error="任务已取消")
        except Exception as exc:  # noqa: BLE001 - report any handler failure
            message = f"{type(exc).__name__}: {exc}"
            tasks.finish(task_id, "failed", error=message)
            _audit_failure(task, message)
            if os.getenv("P0_TASKS_LOG_TRACEBACK", "0").lower() in {"1", "true", "yes"}:
                traceback.print_exc()
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)


WORKER = TaskWorker()


def start() -> bool:
    return WORKER.start()


def stop() -> None:
    WORKER.stop()


def status() -> dict:
    return WORKER.status()


def start_worker_if_enabled() -> bool:
    if not tasks.enabled():
        return False
    return WORKER.start()
