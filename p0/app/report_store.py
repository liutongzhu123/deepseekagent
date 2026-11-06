"""Report-review persistence seam: file directory (P0) or PostgreSQL + objects.

``app.main`` used to audit a report and discard the result.  Routing the write
and read paths through this module keeps the JSON layout
``data/reports/<id>/{meta,result}.json`` working in file mode while letting
``P0_STORE=postgres`` keep the same record in the P1 data layer with the source
``.docx`` in the object store.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = Path(os.getenv("P0_REPORT_DIR", store.config.data_dir() / "reports"))
_ID_RE = re.compile(r"^rp_[0-9]{14}_[0-9a-f]{6}$")


def _dir(report_id: str, base_dir: Path | None = None) -> Path:
    if not _ID_RE.fullmatch(report_id or ""):
        raise KeyError(report_id)
    return (base_dir or REPORT_DIR) / report_id


def create(
    record: dict,
    result: dict,
    source: bytes | None = None,
    base_dir: Path | None = None,
) -> dict:
    if store.config.postgres_enabled():
        return store.reports.create(record, result, source)
    directory = _dir(record["id"], base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    meta = {key: value for key, value in record.items() if key != "result"}
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (directory / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if source is not None:
        (directory / "source.docx").write_bytes(source)
    return {**record, "result": result}


def load(report_id: str, base_dir: Path | None = None) -> dict:
    if store.config.postgres_enabled():
        return store.reports.load(report_id)
    directory = _dir(report_id, base_dir)
    meta_path = directory / "meta.json"
    if not meta_path.exists():
        raise KeyError(report_id)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeyError(report_id) from exc
    if not isinstance(meta, dict):
        raise KeyError(report_id)
    result: dict = {}
    result_path = directory / "result.json"
    if result_path.exists():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            result = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            result = {}
    return {**meta, "result": result}


def list_reports(base_dir: Path | None = None) -> list[dict]:
    if store.config.postgres_enabled():
        return store.reports.list_reports()
    root = base_dir or REPORT_DIR
    if not root.exists():
        return []
    rows: list[dict] = []
    for path in sorted(root.glob("*/meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and meta.get("id"):
            rows.append(meta)
    rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return rows


def read_source(report_id: str, base_dir: Path | None = None) -> bytes:
    if store.config.postgres_enabled():
        return store.reports.read_source(report_id)
    path = _dir(report_id, base_dir) / "source.docx"
    if not path.exists():
        raise KeyError(report_id)
    return path.read_bytes()


_REVIEWS_FILE = "reviews.json"
_REVIEW_STATUSES = {"accepted", "ignored", "false_positive"}


def load_reviews(report_id: str, base_dir: Path | None = None) -> dict:
    """Load per-finding human review decisions for one report."""
    try:
        directory = _dir(report_id, base_dir)
    except KeyError:
        return {}
    path = directory / _REVIEWS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    reviews = data.get("reviews") if isinstance(data, dict) else None
    return reviews if isinstance(reviews, dict) else {}


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def prunable_ids(cutoff: datetime, base_dir: Path | None = None) -> list[str]:
    """Report ids created before ``cutoff``; missing timestamps are kept."""
    if store.config.postgres_enabled():
        return store.reports.prunable_ids(cutoff)
    root = base_dir or REPORT_DIR
    if not root.exists():
        return []
    ids: list[str] = []
    for path in sorted(root.glob("*/meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or not meta.get("id"):
            continue
        created = _parse_time(meta.get("created_at"))
        if created is not None and created < cutoff:
            ids.append(str(meta["id"]))
    return ids


def prune_before(cutoff: datetime, base_dir: Path | None = None) -> int:
    """Delete report records older than ``cutoff``; return the removed count."""
    if store.config.postgres_enabled():
        return store.reports.prune_before(cutoff)
    root = base_dir or REPORT_DIR
    removed = 0
    for report_id in prunable_ids(cutoff, root):
        try:
            directory = _dir(report_id, root)
        except KeyError:
            continue
        if directory.is_dir():
            shutil.rmtree(directory)
            removed += 1
    return removed


def save_reviews(report_id: str, reviews: dict, base_dir: Path | None = None) -> dict:
    directory = _dir(report_id, base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "reviews": reviews}
    temporary = directory / (_REVIEWS_FILE + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(directory / _REVIEWS_FILE)
    return reviews
