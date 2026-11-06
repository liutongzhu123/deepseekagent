"""Batch report aggregation shared by the sync API and the async worker (T303).

Both paths call ``run_batch``; ``audit`` is injected (the API's
``_audit_report_bytes``, reused by the worker) so the shape cannot drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable

Entry = tuple[str, bytes | Exception]
Audit = Callable[..., dict]
BatchProgress = Callable[[str, bool, int, int], None]
def _failure(name: str, error: Exception) -> dict:
    detail = getattr(error, "detail", None)
    return {
        "file": name, "report_id": "", "summary": {}, "ok": False,
        "error": str(detail) if detail else f"failed to parse report: {error}",
    }
def run_batch(
    entries: Iterable[Entry],
    *,
    user: dict,
    project: str,
    batch_id: str,
    audit: Audit,
    inputs=None,
    on_item: BatchProgress | None = None,
) -> dict:
    """Audit every entry, keeping one bad file from dropping the batch."""
    entries = list(entries)
    items: list[dict] = []
    totals = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for index, (name, payload) in enumerate(entries):
        if isinstance(payload, Exception):
            items.append(_failure(name, payload))
        else:
            try:
                result = audit(
                    payload, name, user=user, project=project, batch_id=batch_id, inputs=inputs
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not drop the batch
                items.append(_failure(name, exc))
            else:
                summary = result.get("summary") or {}
                for key in totals:
                    totals[key] += int(summary.get(key, 0) or 0)
                items.append({
                    "file": name, "report_id": result["report_id"],
                    "summary": summary, "ok": True, "error": "",
                })
        if on_item is not None:
            on_item(name, items[-1]["ok"], index, len(entries))
    stored = sum(1 for item in items if item["ok"])
    return {
        "batch_id": batch_id,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
        "summary": {
            "total": len(items), "stored": stored, "failed": len(items) - stored, **totals,
        },
    }
