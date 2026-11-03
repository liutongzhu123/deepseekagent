"""Q&A feedback persistence seam: JSONL file (P0) or PostgreSQL.

File mode appends to ``P0_FEEDBACK_DIR/qa.jsonl`` so a feedback record survives
restarts and can be inspected without a database; PostgreSQL mode writes the
``qa_feedback`` table.  Both return the same dictionary shape to the API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_DIR = Path(os.getenv("P0_FEEDBACK_DIR", store.config.data_dir() / "feedback"))
_FILE_NAME = "qa.jsonl"


def _path() -> Path:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return FEEDBACK_DIR / _FILE_NAME


def create(record: dict) -> dict:
    if store.config.postgres_enabled():
        return store.feedback.create(record)
    path = _path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def list_feedback() -> list[dict]:
    if store.config.postgres_enabled():
        return store.feedback.list_feedback()
    path = FEEDBACK_DIR / _FILE_NAME
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("rating"):
            rows.append(item)
    rows.sort(key=lambda row: str(row.get("at", "")), reverse=True)
    return rows
