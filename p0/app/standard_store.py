"""File-backed store for parsed standards, review edits and KB publication."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import weknora

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


def create(
    parsed: dict,
    source_bytes: bytes,
    file_name: str,
    project: str = "",
    created_by: str = "",
) -> dict:
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
        # P1 project tag: additive; legacy records simply keep empty values.
        record["meta"] = {
            **(record.get("meta") or {}),
            "project": (project or "").strip(),
            "created_by": created_by or "",
        }
        _write_json(directory / "result.json", record)
        return record


def set_project(standard_id: str, project: str, created_by: str | None = None) -> dict:
    """Assign (or clear) the project tag used by P1 row-level authorization."""
    with _LOCK:
        record = load(standard_id)
        record["meta"] = {
            **(record.get("meta") or {}),
            "project": (project or "").strip(),
        }
        if created_by is not None:
            record["meta"]["created_by"] = created_by or ""
        record["updated_at"] = _now()
        _write_json(_dir(standard_id) / "result.json", record)
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
                "project": record["meta"].get("project", ""),
                "created_by": record["meta"].get("created_by", ""),
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
            "project": record["meta"].get("project", ""),
            "created_by": record["meta"].get("created_by", ""),
            "source": record.get("source", {}),
            "chunks": chunks,
            "published_at": _now(),
        }
        # WeKnora is the single knowledge base.  The local kb.json remains as a
        # deterministic audit/fallback copy, but the business Q&A path reads
        # from WeKnora and every publication must reach it.
        weknora_result = None
        if weknora.configured():
            try:
                weknora_result = weknora.publish_standard(record, chunks)
            except weknora.WeKnoraError as exc:
                if weknora.required():
                    raise weknora.WeKnoraError(f"发布到 WeKnora 失败：{exc}") from exc
                kb["weknora_error"] = str(exc)
            else:
                kb["weknora"] = weknora_result
                record["weknora"] = weknora_result
        elif weknora.required():
            raise ValueError("WeKnora 是唯一知识底座，但服务端尚未配置 WeKnora 连接")
        _write_json(_dir(standard_id) / "kb.json", kb)
        record["published"] = True
        record["published_at"] = kb["published_at"]
        record["updated_at"] = kb["published_at"]
        _write_json(_dir(standard_id) / "result.json", record)
        return {
            "published": True,
            "chunks": len(chunks),
            "published_at": kb["published_at"],
            "weknora": weknora_result,
        }


def published_knowledge_ids(standard_id: str | None = None) -> list[str]:
    """WeKnora knowledge IDs for the published standard(s)."""
    ids: list[str] = []
    for kb in published_kbs(standard_id):
        knowledge_id = (kb.get("weknora") or {}).get("knowledge_id")
        if knowledge_id:
            ids.append(knowledge_id)
    return ids



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


INDEX_PATH = DATA_DIR / "kb_index.json"


def rebuild_index() -> dict:
    """Rebuild every standard's chunks and write one combined KB index.

    This is the P0 "重建索引" operation: it is deterministic and file-backed,
    so the demo can be repeated without WeKnora or an external vector store.
    """
    with _LOCK:
        if not DATA_DIR.exists():
            return {"standards": 0, "chunks": 0, "built_at": _now()}
        standards: list[dict] = []
        chunks: list[dict] = []
        for directory in sorted(DATA_DIR.iterdir()):
            result_path = directory / "result.json"
            if not result_path.exists():
                continue
            try:
                record = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            standard_chunks = build_chunks(record)
            if not standard_chunks:
                continue
            kb = {
                "standard_id": record["id"],
                "standard_no": record["meta"].get("standard_no", ""),
                "standard_name": record["meta"].get("name", ""),
                "project": record["meta"].get("project", ""),
                "created_by": record["meta"].get("created_by", ""),
                "source": record.get("source", {}),
                "chunks": standard_chunks,
                "published_at": _now(),
            }
            _write_json(directory / "kb.json", kb)
            record["published"] = True
            record["published_at"] = kb["published_at"]
            record["updated_at"] = kb["published_at"]
            _write_json(result_path, record)
            standards.append(
                {
                    "id": record["id"],
                    "standard_no": record["meta"].get("standard_no", ""),
                    "standard_name": record["meta"].get("name", ""),
                    "items": len(record.get("items", [])),
                    "chunks": len(standard_chunks),
                }
            )
            chunks.extend(standard_chunks)
        index = {
            "built_at": _now(),
            "standard_count": len(standards),
            "chunk_count": len(chunks),
            "standards": standards,
            "chunks": chunks,
        }
        _write_json(INDEX_PATH, index)
        return index


def index_stats() -> dict:
    if INDEX_PATH.exists():
        try:
            index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            return {
                "built_at": index.get("built_at", ""),
                "standard_count": index.get("standard_count", 0),
                "chunk_count": index.get("chunk_count", 0),
                "standards": index.get("standards", []),
            }
        except (OSError, json.JSONDecodeError):
            pass
    return {"built_at": "", "standard_count": 0, "chunk_count": 0, "standards": []}


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n\t]', "-", value).strip()
    return value or "standard"
