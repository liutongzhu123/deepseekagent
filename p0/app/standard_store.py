"""Store for parsed standards, review edits and KB publication.

``P0_STORE=json`` keeps the original file layout; ``P0_STORE=postgres`` writes
the record, revision chain and clause rows to PostgreSQL and the source/export
objects to the P1 object store, while still mirroring the JSON tree for a
single-switch rollback.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import store, weknora
from .standard_extract import items_list
from .xlsx_writer import write_xlsx

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


def _versions_dir(standard_id: str) -> Path:
    return _dir(standard_id) / "versions"


def _write_snapshot(record: dict) -> None:
    """Keep a per-revision snapshot so a review can be rolled back later.

    PostgreSQL already stores every revision in ``standard_versions``; this is
    the file-mode equivalent and is a no-op there.
    """
    if store.config.postgres_enabled():
        return
    directory = _versions_dir(record["id"])
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / f"{int(record.get('revision', 0))}.json", record)



def create(
    parsed: dict,
    source_bytes: bytes,
    file_name: str,
    project: str = "",
    created_by: str = "",
) -> dict:
    with _LOCK:
        standard_id = "std_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)
        record = {
            "id": standard_id,
            "created_at": _now(),
            "updated_at": _now(),
            "revision": 0,
            "published": False,
            "published_at": "",
            "reviewer": "",
            "review_history": [],
            "review_feedback": [],
            "source": {"file_name": file_name, "bytes": len(source_bytes)},
            **parsed,
        }
        # P1 project tag: additive; legacy records simply keep empty values.
        record["meta"] = {
            **(record.get("meta") or {}),
            "project": (project or "").strip(),
            "created_by": created_by or "",
        }
        if store.config.postgres_enabled():
            return store.standards.create(record, source_bytes, file_name)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        directory = DATA_DIR / standard_id
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(source_bytes)
        _write_json(directory / "result.json", record)
        _write_snapshot(record)
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
        if store.config.postgres_enabled():
            return store.standards.save(record)
        _write_json(_dir(standard_id) / "result.json", record)
        return record


def load(standard_id: str) -> dict:
    if store.config.postgres_enabled():
        return store.standards.load(standard_id)
    directory = _dir(standard_id)
    path = directory / "result.json"
    if not path.exists():
        raise KeyError(standard_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_standards() -> list[dict]:
    if store.config.postgres_enabled():
        return store.standards.list_standards()
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


def _review_reason(item: dict, field: str) -> str:
    return next(
        (str(entry.get("reason", "")) for entry in item.get("review_fields") or []
         if isinstance(entry, dict) and entry.get("field") == field),
        "",
    )


def _feedback_sample(record, item, action, reviewer, field="", before="", after="", reason=""):
    """One human decision, kept as a labelled sample for parser tuning."""
    meta = record.get("meta") or {}
    return {
        "id": "rf_" + secrets.token_hex(6), "at": _now(),
        "standard_id": record["id"], "standard_no": meta.get("standard_no", ""),
        "standard_name": meta.get("name", ""), "project": meta.get("project", ""),
        "clause": str(item.get("clause", "")), "name": str(item.get("name", "")),
        "action": action, "field": field, "before": before, "after": after,
        "confidence": item.get("confidence"), "reason": reason, "reviewer": reviewer,
    }


def _add_review_items(record, additions, reviewer, feedback):
    """Append clauses the parser missed; each is retained as a supplement sample."""
    existing = {str(item.get("clause", "")).strip() for item in record["items"]}
    for raw in additions:
        clause = str(raw.get("clause", "")).strip()
        name = str(raw.get("name", "")).strip()
        if not clause or not name:
            raise ValueError("补充条款需要条款号与项目名称")
        if clause in existing:
            raise ValueError(f"条款 {clause} 已存在，不能重复补充")
        item = {
            "seq": len(record["items"]) + 1, "name": name, "name_auto": "",
            "group": str(raw.get("group", "")).strip(), "clause": clause,
            "requirement": str(raw.get("requirement", "")).strip(),
            "method_clause": str(raw.get("method_clause", "")).strip(),
            "method": str(raw.get("method", "")).strip(),
            "page": raw.get("page"), "method_page": raw.get("method_page") or raw.get("page"),
            "confidence": 0.0, "review_required": False, "review_fields": [],
            "reviewed": True, "reviewed_at": _now(), "reviewed_by": reviewer,
            "references": [], "source": "manual",
        }
        record["items"].append(item)
        existing.add(clause)
        record["review_history"].append({
            "at": item["reviewed_at"], "reviewer": reviewer, "clause": clause,
            "field": "item", "action": "add", "before": "", "after": name,
        })
        sample = _feedback_sample(record, item, "supplement", reviewer, "item", "", name)
        sample["addition"] = {key: item.get(key) for key in (
            "clause", "name", "requirement", "method_clause", "method", "page")}
        feedback.append(sample)
    return len(additions)


def update_review(standard_id: str, updates: list[dict], reviewer: str = "",
                  additions: list[dict] | None = None) -> dict:
    with _LOCK:
        record = load(standard_id)
        record.setdefault("review_feedback", [])
        by_clause = {item["clause"]: item for item in record["items"]}
        changed = 0
        for update in updates:
            clause = str(update.get("clause", "")).strip()
            item = by_clause.get(clause)
            if item is None:
                continue
            confirmed = update.get("reviewed")
            if isinstance(confirmed, bool) and confirmed != bool(item.get("reviewed")):
                record["review_history"].append({
                    "at": _now(), "reviewer": reviewer, "clause": clause,
                    "field": "reviewed", "before": bool(item.get("reviewed")),
                    "after": confirmed})
                item["reviewed"] = confirmed
                item["reviewed_at"] = record["review_history"][-1]["at"]
                item["reviewed_by"] = reviewer if confirmed else ""
                if confirmed:
                    record["review_feedback"].append(_feedback_sample(
                        record, item, "confirm", reviewer, "item", "", item.get("name", "")))
                changed += 1
            for field in ("name", "requirement", "method", "method_clause",
                          "applicable_parts"):
                if field not in update or update[field] is None:
                    continue
                value = (
                    items_list.normalise_parts(update[field])
                    if field == "applicable_parts"
                    else str(update[field]).strip()
                )
                current = item.get(field) or ([] if field == "applicable_parts" else "")
                if field == "applicable_parts":
                    current = items_list.normalise_parts(current)
                if value == current:
                    continue
                reason = _review_reason(item, field)
                record["review_history"].append({
                    "at": _now(), "reviewer": reviewer, "clause": clause,
                    "field": field, "before": current, "after": value})
                item[field] = value
                if field == "applicable_parts":
                    item["applicable_parts_source"] = "manual"
                item.update({"reviewed": True, "reviewed_by": reviewer,
                             "reviewed_at": record["review_history"][-1]["at"]})
                if isinstance(item.get("review_fields"), list):
                    item["review_fields"] = [entry for entry in item["review_fields"]
                                             if entry.get("field") != field]
                    item["review_required"] = bool(item["review_fields"])
                record["review_feedback"].append(_feedback_sample(
                    record, item, "correct", reviewer, field, current, value, reason))
                changed += 1
        changed += _add_review_items(record, additions or [], reviewer,
                                     record["review_feedback"])
        if changed:
            record["revision"] = record.get("revision", 0) + 1
            record["updated_at"] = _now()
            record["reviewer"] = reviewer
            # 未确认不入库：已发布标准一旦出现未确认条目，立即从知识库下架，
            # 否则 ask 仍会引用未确认条款（#196 验证拒绝项）。
            auto_unpublished = bool(record.get("published")) and bool(
                unreviewed_clauses(record)
            )
            if auto_unpublished:
                removed, weknora_error = _detach_knowledge(standard_id)
                _mark_unpublished(record, reviewer)
                record.setdefault("review_history", []).append({
                    "at": record["unpublished_at"], "reviewer": reviewer,
                    "action": "auto_unpublish",
                    "reason": "检测项目取消确认，未确认不入库",
                    "removed_knowledge_ids": removed,
                    "weknora_error": weknora_error,
                })
            record["summary"] = _recompute_summary(record)
            if store.config.postgres_enabled():
                if auto_unpublished:
                    store.standards.clear_kb(standard_id)
                store.standards.save(record)
            else:
                _write_json(_dir(standard_id) / "result.json", record)
                _write_snapshot(record)
                if auto_unpublished:
                    (_dir(standard_id) / "kb.json").unlink(missing_ok=True)
        return record


def _recompute_summary(record: dict) -> dict:
    items = record["items"]
    summary = dict(record.get("summary", {}))
    summary.update(
        {
            "items": len(items),
            "items_with_requirement": sum(1 for item in items if item.get("requirement")),
            "items_with_method": sum(1 for item in items if item.get("method")),
            "items_with_applicable_parts": sum(
                1 for item in items if item.get("applicable_parts")
            ),
            "clauses_traced": sum(
                1
                for item in items
                if item.get("clause") and item.get("method_clause") and item.get("page")
            ),
            "reviewed_items": sum(1 for item in items if item.get("reviewed")),
            "feedback_samples": len(record.get("review_feedback") or []),
        }
    )
    record["items_list"] = items_list.build(record.get("meta") or {}, items)
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


def unreviewed_clauses(record: dict) -> list[str]:
    """Clauses still waiting for a human confirmation (``reviewed=false``)."""
    return [
        str(item.get("clause") or item.get("name") or "")
        for item in record.get("items") or []
        if not item.get("reviewed")
    ]


def publish_blocker(record: dict) -> str:
    """Why the record may not enter the knowledge base yet ('' when ready).

    Review gate for SP-2.2「未确认不入库」: publication is all-or-nothing,
    because a partially published standard would silently miss clauses in
    Q&A.  Every item must first be confirmed through ``update_review``.
    """
    pending = unreviewed_clauses(record)
    if pending:
        sample = "、".join(pending[:5]) + (" 等" if len(pending) > 5 else "")
        return f"还有 {len(pending)} 个检测项目未人工复核确认（{sample}），未确认不入库"
    if not build_chunks(record):
        return "没有可发布的解析条目"
    return ""


def publish(standard_id: str) -> dict:
    with _LOCK:
        record = load(standard_id)
        blocker = publish_blocker(record)
        if blocker:
            raise ValueError(blocker)
        chunks = build_chunks(record)
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
        record["published"] = True
        record["published_at"] = kb["published_at"]
        record["updated_at"] = kb["published_at"]
        if store.config.postgres_enabled():
            # ``save`` stores the record and the kb document; the mirror keeps
            # data/standards/<id>/ usable for a file-mode rollback.
            store.standards.save(record, kb)
        else:
            _write_json(_dir(standard_id) / "kb.json", kb)
            _write_json(_dir(standard_id) / "result.json", record)
        return {
            "published": True,
            "chunks": len(chunks),
            "items": len(record["items"]),
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
    if store.config.postgres_enabled():
        return store.standards.published_kbs(standard_id)
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


def _version_summary(version: int, record: dict, current: int, rollbackable: bool) -> dict:
    return {
        "version": version,
        "created_at": record.get("updated_at") or record.get("created_at", ""),
        "reviewer": str(record.get("reviewer", "")),
        "items": len(record.get("items") or []),
        "current": version == current,
        "rollbackable": rollbackable,
    }


def list_versions(standard_id: str) -> list[dict]:
    """Revision list for the review panel; the current revision is always shown."""
    if store.config.postgres_enabled():
        return store.standards.list_versions(standard_id)
    record = load(standard_id)
    current = int(record.get("revision", 0) or 0)
    versions: list[dict] = []
    directory = _versions_dir(standard_id)
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                version = int(snapshot.get("revision", path.stem))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            versions.append(
                _version_summary(version, snapshot, current, version != current)
            )
    if not any(item["version"] == current for item in versions):
        versions.append(_version_summary(current, record, current, False))
    versions.sort(key=lambda item: item["version"], reverse=True)
    return versions


def version_record(standard_id: str, version: int) -> dict:
    if store.config.postgres_enabled():
        return store.standards.version_record(standard_id, version)
    path = _versions_dir(standard_id) / f"{int(version)}.json"
    if not path.exists():
        raise KeyError(f"{standard_id}@{version}")
    return json.loads(path.read_text(encoding="utf-8"))


# Parser bookkeeping and page locators are not standard content: a re-parse or
# a new edition that only moves these fields did not change the clause.
_DIFF_EXCLUDED = frozenset(
    {"seq", "name_auto", "confidence", "review_required", "review_fields",
     "reviewed", "reviewed_at", "reviewed_by", "page", "method_page"}
)
_DIFF_FIELDS = ("name", "group", "requirement", "method_clause", "method")
_DIFF_LABELS = {
    "name": "项目名称", "group": "分组",
    "requirement": "技术要求/判定要求", "method_clause": "试验方法条款",
    "method": "试验方法",
}


def _buckets(items: list[dict] | None) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for index, item in enumerate(items or []):
        clause = str(item.get("clause", "")).strip()
        buckets.setdefault(clause or f"#{index + 1:03d}", []).append(item)
    return buckets


def _changed_fields(old: dict, new: dict) -> list[dict]:
    extra = sorted(
        (set(old) | set(new)) - set(_DIFF_FIELDS) - _DIFF_EXCLUDED - {"clause"}
    )
    changes: list[dict] = []
    for field in list(_DIFF_FIELDS) + extra:
        before, after = old.get(field), new.get(field)
        before = " ".join(before.split()) if isinstance(before, str) else before
        after = " ".join(after.split()) if isinstance(after, str) else after
        if before == after:
            continue
        changes.append({"field": field, "label": _DIFF_LABELS.get(field, field),
                        "before": before, "after": after})
    return changes


def diff_items(old_items: list[dict] | None, new_items: list[dict] | None) -> dict:
    """Clause-level 增/删/改 diff with before/after values for changed fields."""
    old_buckets, new_buckets = _buckets(old_items), _buckets(new_items)
    added: list[dict] = []
    removed: list[dict] = []
    modified: list[dict] = []
    unchanged = 0
    for clause in sorted(set(old_buckets) | set(new_buckets)):
        olds, news = old_buckets.get(clause, []), new_buckets.get(clause, [])
        for old, new in zip(olds, news):
            changes = _changed_fields(old, new)
            if not changes:
                unchanged += 1
                continue
            modified.append({"change": "modified", "clause": clause,
                             "name": new.get("name") or old.get("name", ""),
                             "fields": changes})
        for kind, items in (("added", news[len(olds):]), ("removed", olds[len(news):])):
            target = added if kind == "added" else removed
            target.extend(
                {"change": kind, "clause": str(item.get("clause", "")),
                 "name": item.get("name", ""),
                 "requirement": item.get("requirement", ""),
                 "method": item.get("method", "")} for item in items
            )
    counts = {"added": len(added), "removed": len(removed),
              "modified": len(modified), "unchanged": unchanged}
    return {"added": added, "removed": removed, "modified": modified,
            "unchanged": unchanged, "counts": counts}


def _report_value(value: object, limit: int = 160) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    return text if len(text) <= limit else text[: limit - 3] + "…"


def render_change_report(diff: dict) -> str:
    """Markdown 变更对比报告 for a diff produced by this module."""
    old, new = diff.get("from") or {}, diff.get("to") or {}
    counts = diff.get("counts") or {}
    lines = [
        "# 标准变更对比报告", "",
        f"- 旧版本：{old.get('standard_no') or old.get('name') or '旧版本'}"
        f"（修订 r{old.get('revision', 0)}）",
        f"- 新版本：{new.get('standard_no') or new.get('name') or '新版本'}"
        f"（修订 r{new.get('revision', 0)}）", "",
        f"新增 {counts.get('added', 0)} · 删除 {counts.get('removed', 0)}"
        f" · 修改 {counts.get('modified', 0)} · 未变 {counts.get('unchanged', 0)}",
    ]
    for title, key in (("新增", "added"), ("删除", "removed")):
        entries = diff.get(key) or []
        if entries:
            lines += ["", f"## {title}（{len(entries)}）"]
            lines += [
                f"- {entry.get('clause', '')} {entry.get('name', '')}".rstrip()
                for entry in entries
            ]
    modified = diff.get("modified") or []
    if modified:
        lines += ["", f"## 修改（{len(modified)}）"]
        for entry in modified:
            lines.append(
                f"- {entry.get('clause', '')} {entry.get('name', '')}".rstrip()
            )
            lines += [
                f"  - {field.get('label') or field.get('field')}："
                f"{_report_value(field.get('before')) or '（空）'} → "
                f"{_report_value(field.get('after')) or '（空）'}"
                for field in entry.get("fields") or []
            ]
    if not any(diff.get(key) for key in ("added", "removed", "modified")):
        lines += ["", "两个版本没有条款差异。"]
    return "\n".join(lines) + "\n"


def _diff_records(old: dict, new: dict) -> dict:
    diff = diff_items(old.get("items") or [], new.get("items") or [])
    for side, record in (("from", old), ("to", new)):
        meta = record.get("meta") or {}
        diff[side] = {
            "id": record.get("id", ""),
            "standard_no": meta.get("standard_no", ""),
            "name": meta.get("name", ""),
            "revision": int(record.get("revision", 0) or 0),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
        }
    diff["generated_at"] = _now()
    diff["report"] = render_change_report(diff)
    return diff


def diff_standards(old_standard_id: str, new_standard_id: str) -> dict:
    """Diff two stored records, e.g. the previous and current edition."""
    diff = _diff_records(load(old_standard_id), load(new_standard_id))
    diff["kind"] = "standards"
    return diff


def diff_versions(
    standard_id: str, from_version: int | None = None, to_version: int | None = None
) -> dict:
    """Diff two revision snapshots; defaults to previous vs current revision."""
    current = load(standard_id)
    current_version = int(current.get("revision", 0) or 0)
    to_version = current_version if to_version is None else int(to_version)
    if from_version is None:
        earlier = [
            version["version"]
            for version in list_versions(standard_id)
            if version["version"] < to_version
        ]
        if not earlier:
            raise ValueError("没有更早的修订可对比")
        from_version = max(earlier)
    from_version = int(from_version)
    if from_version == to_version:
        raise ValueError("新旧修订不能相同")
    old = current if from_version == current_version else version_record(
        standard_id, from_version
    )
    new = current if to_version == current_version else version_record(
        standard_id, to_version
    )
    diff = _diff_records(old, new)
    diff.update(kind="revisions", from_version=from_version, to_version=to_version)
    return diff


def _knowledge_ids(standard_id: str) -> list[str]:
    ids: list[str] = []
    for kb in published_kbs(standard_id):
        knowledge_id = (kb.get("weknora") or {}).get("knowledge_id")
        if knowledge_id:
            ids.append(knowledge_id)
    return ids


def _detach_knowledge(standard_id: str) -> tuple[list[str], str]:
    """Delete the standard's WeKnora entries, honouring required/optional mode."""
    knowledge_ids = _knowledge_ids(standard_id)
    removed: list[str] = []
    weknora_error = ""
    if not knowledge_ids:
        return removed, weknora_error
    if not weknora.configured():
        if weknora.required():
            raise weknora.WeKnoraError(
                "WeKnora 是唯一知识底座，但服务端尚未配置 WeKnora 连接"
            )
        return removed, "WeKnora 未配置，远端知识条目未删除"
    for knowledge_id in knowledge_ids:
        try:
            weknora.delete_knowledge(knowledge_id)
        except weknora.WeKnoraError as exc:
            if weknora.required():
                raise
            weknora_error = str(exc)
        else:
            removed.append(knowledge_id)
    return removed, weknora_error


def _mark_unpublished(record: dict, actor: str = "") -> None:
    at = _now()
    record["published"] = False
    record["published_at"] = ""
    record["unpublished_at"] = at
    record["unpublished_by"] = actor
    record["updated_at"] = at
    record.pop("weknora", None)


def unpublish(standard_id: str, actor: str = "") -> dict:
    """Remove the standard from the knowledge base but keep the review trail.

    The local ``kb`` document is deleted so Q&A can no longer cite it.  With
    WeKnora configured the knowledge entry is deleted first; if that fails and
    WeKnora is required, nothing local is changed and the caller sees an error.
    """
    with _LOCK:
        record = load(standard_id)
        removed, weknora_error = _detach_knowledge(standard_id)
        _mark_unpublished(record, actor)
        if store.config.postgres_enabled():
            store.standards.clear_kb(standard_id)
            store.standards.save(record)
        else:
            (_dir(standard_id) / "kb.json").unlink(missing_ok=True)
            _write_json(_dir(standard_id) / "result.json", record)
        return {
            "published": False,
            "removed_knowledge_ids": removed,
            "weknora_error": weknora_error,
        }


def rollback(standard_id: str, version: int, reviewer: str = "") -> dict:
    """Restore the items of an earlier revision as a new revision."""
    with _LOCK:
        current = load(standard_id)
        target = version_record(standard_id, version)
        if int(version) >= int(current.get("revision", 0) or 0):
            raise ValueError("只能回滚到更早的修订")
        record = json.loads(json.dumps(current))
        record["items"] = json.loads(json.dumps(target.get("items") or []))
        record["revision"] = int(current.get("revision", 0) or 0) + 1
        record["reviewer"] = reviewer or str(current.get("reviewer", ""))
        # 回滚前先真正下架：旧实现只改 published 标志，kb.json/WeKnora
        # 仍可被 ask 引用（本次验证同源缺陷）。
        removed, weknora_error = _detach_knowledge(standard_id)
        _mark_unpublished(record, record["reviewer"])
        record.setdefault("review_history", []).append(
            {
                "at": record["updated_at"],
                "reviewer": record["reviewer"],
                "action": "rollback",
                "from_version": int(version),
                "to_revision": record["revision"],
                "removed_knowledge_ids": removed,
                "weknora_error": weknora_error,
            }
        )
        record["summary"] = _recompute_summary(record)
        if store.config.postgres_enabled():
            store.standards.clear_kb(standard_id)
            store.standards.save(record)
        else:
            (_dir(standard_id) / "kb.json").unlink(missing_ok=True)
            _write_json(_dir(standard_id) / "result.json", record)
            _write_snapshot(record)
        return record



def export_path(standard_id: str) -> Path:
    return _dir(standard_id) / "export.xlsx"


def export_bytes(standard_id: str, sheets: list[tuple[str, list[list[object]], list[int] | None]]) -> bytes:
    """Render the workbook and persist it in the active backend.

    The API returns bytes so the same route works with a local directory and
    with MinIO; PostgreSQL mode also records the object in ``files``.
    """
    _dir(standard_id)
    with tempfile.TemporaryDirectory(prefix="p0-export-") as tmp:
        path = Path(tmp) / "export.xlsx"
        write_xlsx(path, sheets)
        data = path.read_bytes()
    if store.config.postgres_enabled():
        store.standards.save_export(standard_id, data)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        target = export_path(standard_id)
        tmp_target = target.with_name(target.name + ".tmp")
        tmp_target.write_bytes(data)
        tmp_target.replace(target)
    return data


INDEX_PATH = DATA_DIR / "kb_index.json"


def rebuild_index() -> dict:
    """Rebuild every standard's chunks and write one combined KB index.

    This is the P0 "重建索引" operation: it is deterministic and file-backed,
    so the demo can be repeated without WeKnora or an external vector store.
    Standards with unconfirmed items are skipped so a rebuild cannot bypass
    the review gate (未确认不入库).
    """
    with _LOCK:
        records: list[dict] = []
        if store.config.postgres_enabled():
            for summary in store.standards.list_standards():
                try:
                    records.append(store.standards.load(summary["id"]))
                except KeyError:
                    continue
        else:
            if not DATA_DIR.exists():
                return {"standards": 0, "chunks": 0, "built_at": _now()}
            for directory in sorted(DATA_DIR.iterdir()):
                result_path = directory / "result.json"
                if not result_path.exists():
                    continue
                try:
                    records.append(json.loads(result_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        standards: list[dict] = []
        chunks: list[dict] = []
        for record in records:
            if unreviewed_clauses(record):
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
            record["published"] = True
            record["published_at"] = kb["published_at"]
            record["updated_at"] = kb["published_at"]
            if store.config.postgres_enabled():
                store.standards.save(record, kb)
            else:
                target = _dir(record["id"])
                _write_json(target / "kb.json", kb)
                _write_json(target / "result.json", record)
            standards.append(
                {
                    "id": record["id"],
                    "standard_no": record["meta"].get("standard_no", ""),
                    "standard_name": record["meta"].get("name", ""),
                    "items": len(record.get("items", [])),
                    "chunks": len(standard_chunks),
                    "project": record["meta"].get("project", ""),
                    "created_by": record["meta"].get("created_by", ""),
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
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
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
