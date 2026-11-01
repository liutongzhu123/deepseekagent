"""File-backed store for parsed standards, review edits and KB publication."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("P0_STANDARD_DIR", ROOT / "data" / "standards"))

_ID_RE = re.compile(r"^std_[0-9]{14}_[0-9a-f]{6}$")
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dir(standard_id: str) -> Path:
    if not _ID_RE.fullmatch(standard_id):
        raise KeyError(standard_id)
    return DATA_DIR / standard_id


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def create(parsed: dict, source_bytes: bytes, file_name: str) -> dict:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        standard_id = "std_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)
        directory = DATA_DIR / standard_id
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(source_bytes)
        record = {
            "id": standard_id,
            "created_at": _now(),
            "updated_at": _now(),
            "revision": 0,
            "published": False,
            "published_at": "",
            "reviewer": "",
            "review_history": [],
            "source": {"file_name": file_name, "bytes": len(source_bytes)},
            **parsed,
        }
        _write_json(directory / "result.json", record)
        return record


def load(standard_id: str) -> dict:
    directory = _dir(standard_id)
    path = directory / "result.json"
    if not path.exists():
        raise KeyError(standard_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_standards() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    out: list[dict] = []
    for directory in sorted(DATA_DIR.iterdir(), reverse=True):
        path = directory / "result.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "id": record["id"],
                "standard_no": record["meta"].get("standard_no", ""),
                "name": record["meta"].get("name", ""),
                "items": record["summary"].get("items", 0),
                "pages": record["meta"].get("pages", 0),
                "published": record.get("published", False),
                "revision": record.get("revision", 0),
                "created_at": record.get("created_at", ""),
            }
        )
    return out


def update_review(standard_id: str, updates: list[dict], reviewer: str = "") -> dict:
    with _LOCK:
        record = load(standard_id)
        by_clause = {item["clause"]: item for item in record["items"]}
        changed = 0
        for update in updates:
            clause = str(update.get("clause", "")).strip()
            item = by_clause.get(clause)
            if item is None:
                continue
            for field in ("name", "requirement", "method"):
                if field not in update or update[field] is None:
                    continue
                value = str(update[field]).strip()
                if value == item.get(field, ""):
                    continue
                record["review_history"].append(
                    {
                        "at": _now(),
                        "reviewer": reviewer,
                        "clause": clause,
                        "field": field,
                        "before": item.get(field, ""),
                        "after": value,
                    }
                )
                item[field] = value
                item["reviewed"] = True
                changed += 1
        if changed:
            record["revision"] = record.get("revision", 0) + 1
            record["updated_at"] = _now()
            record["reviewer"] = reviewer
            record["summary"] = _recompute_summary(record)
            _write_json(_dir(standard_id) / "result.json", record)
        return record


def _recompute_summary(record: dict) -> dict:
    items = record["items"]
    summary = dict(record.get("summary", {}))
    summary.update(
        {
            "items": len(items),
            "items_with_requirement": sum(1 for item in items if item.get("requirement")),
            "items_with_method": sum(1 for item in items if item.get("method")),
            "clauses_traced": sum(
                1
                for item in items
                if item.get("clause") and item.get("method_clause") and item.get("page")
            ),
            "reviewed_items": sum(1 for item in items if item.get("reviewed")),
        }
    )
    return summary


def build_chunks(record: dict) -> list[dict]:
    chunks: list[dict] = []
    for item in record["items"]:
        base = {
            "standard_id": record["id"],
            "standard_no": record["meta"].get("standard_no", ""),
            "standard_name": record["meta"].get("name", ""),
            "item_name": item["name"],
            "clause": item["clause"],
        }
        if item.get("requirement"):
            chunks.append(
                {
                    **base,
                    "id": f"{record['id']}:{item['clause']}:req",
                    "kind": "requirement",
                    "kind_label": "技术要求/判定要求",
                    "page": item.get("page"),
                    "text": item["requirement"],
                }
            )
        if item.get("method"):
            chunks.append(
                {
                    **base,
                    "id": f"{record['id']}:{item['method_clause']}:method",
                    "kind": "method",
                    "kind_label": "试验方法",
                    "page": item.get("method_page"),
                    "text": item["method"],
                }
            )
    return chunks


def publish(standard_id: str) -> dict:
    with _LOCK:
        record = load(standard_id)
        chunks = build_chunks(record)
        if not chunks:
            raise ValueError("没有可发布的解析条目")
        kb = {
            "standard_id": standard_id,
            "standard_no": record["meta"].get("standard_no", ""),
            "standard_name": record["meta"].get("name", ""),
            "source": record.get("source", {}),
            "chunks": chunks,
            "published_at": _now(),
        }
        _write_json(_dir(standard_id) / "kb.json", kb)
        record["published"] = True
        record["published_at"] = kb["published_at"]
        record["updated_at"] = kb["published_at"]
        _write_json(_dir(standard_id) / "result.json", record)
        return {"published": True, "chunks": len(chunks), "published_at": kb["published_at"]}


def published_kbs(standard_id: str | None = None) -> list[dict]:
    if standard_id:
        path = _dir(standard_id) / "kb.json"
        return [json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []
    if not DATA_DIR.exists():
        return []
    out: list[dict] = []
    for directory in sorted(DATA_DIR.iterdir(), reverse=True):
        path = directory / "kb.json"
        if path.exists():
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def export_path(standard_id: str) -> Path:
    return _dir(standard_id) / "export.xlsx"


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n\t]', "-", value).strip()
    return value or "standard"
