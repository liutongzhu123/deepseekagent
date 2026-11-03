#!/usr/bin/env python3
"""WeKnora knowledge-base operations: status, duplicate cleanup, summary model.

Read-only by default.  Mutations require ``--apply`` and print exactly what they
will delete/change.  The script uses the same API key as the P0 service; for
cleanup/summary changes the key needs KB write/manage permission.

Examples::

    .venv/bin/python scripts/weknora_ops.py status
    .venv/bin/python scripts/weknora_ops.py cleanup --delete-failed --apply
    .venv/bin/python scripts/weknora_ops.py summary --model-id <model-id> --apply
    .venv/bin/python scripts/weknora_ops.py models
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import weknora  # noqa: E402


def _title(item: dict) -> str:
    return str(item.get("title") or "").strip()


def _is_p0_title(item: dict) -> bool:
    return _title(item).startswith("P0 |")


def _canonical(matches: list[dict]) -> dict:
    def key(item: dict) -> tuple:
        status = str(item.get("parse_status") or "")
        return (
            1 if status == "completed" else 0,
            str(item.get("updated_at") or item.get("created_at") or ""),
        )

    return max(matches, key=key)


def collect_status() -> dict:
    kb = weknora.get_knowledge_base()
    models = weknora.list_models()
    items = weknora.iter_knowledge()
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[_title(item)].append(item)
    duplicates = {
        title: [
            {
                "id": item.get("id"),
                "parse_status": item.get("parse_status"),
                "updated_at": item.get("updated_at"),
            }
            for item in matches
        ]
        for title, matches in groups.items()
        if title and len(matches) > 1 and _is_p0_title(matches[0])
    }
    failed = [
        {
            "id": item.get("id"),
            "title": _title(item),
            "parse_status": item.get("parse_status"),
            "updated_at": item.get("updated_at"),
        }
        for item in items
        if str(item.get("parse_status") or "") == "failed"
    ]
    pending = [
        {
            "id": item.get("id"),
            "title": _title(item),
            "parse_status": item.get("parse_status"),
            "updated_at": item.get("updated_at"),
        }
        for item in items
        if str(item.get("parse_status") or "") in {"pending", "processing"}
    ]
    status_counts: dict[str, int] = defaultdict(int)
    for item in items:
        status_counts[str(item.get("parse_status") or "unknown")] += 1
    model_ids = {str(model.get("id") or "") for model in models}
    summary_model_id = str(kb.get("summary_model_id") or "")
    warnings: list[str] = []
    if not summary_model_id:
        warnings.append("知识库未配置 summary_model_id（摘要/自动标签/AI 描述会缺失）")
    elif summary_model_id not in model_ids:
        warnings.append(f"summary_model_id={summary_model_id} 不在模型列表中")
    if duplicates:
        warnings.append(f"发现 {sum(len(v) - 1 for v in duplicates.values())} 条重复知识")
    if failed:
        warnings.append(f"发现 {len(failed)} 条解析失败知识")
    if pending:
        warnings.append(f"发现 {len(pending)} 条 pending/processing 知识（可能卡住）")
    return {
        "kb": {
            "id": kb.get("id"),
            "name": kb.get("name"),
            "embedding_model_id": kb.get("embedding_model_id"),
            "summary_model_id": summary_model_id,
            "vector": (kb.get("capabilities") or {}).get("vector"),
            "keyword": (kb.get("capabilities") or {}).get("keyword"),
        },
        "models": [
            {"id": model.get("id"), "name": model.get("name"), "type": model.get("type")}
            for model in models
        ],
        "knowledge": {
            "total": len(items),
            "by_parse_status": dict(sorted(status_counts.items())),
            "duplicates": duplicates,
            "failed": failed,
            "pending": pending,
        },
        "warnings": warnings,
    }


def cleanup(apply: bool, delete_failed: bool) -> dict:
    status = collect_status()
    plan: list[dict] = []
    for title, matches in status["knowledge"]["duplicates"].items():
        keep = _canonical(matches)
        for item in matches:
            if item.get("id") == keep.get("id"):
                continue
            plan.append({"id": item.get("id"), "title": title, "reason": "duplicate", "keep": keep.get("id")})
        if delete_failed and str(keep.get("parse_status") or "") == "failed":
            plan.append({"id": keep.get("id"), "title": title, "reason": "parse_failed"})
    if delete_failed:
        already = {item["id"] for item in plan}
        for item in status["knowledge"]["failed"]:
            if item.get("id") not in already:
                plan.append({"id": item.get("id"), "title": item.get("title"), "reason": "parse_failed"})
    result = {"apply": apply, "delete_failed": delete_failed, "plan": plan, "deleted": [], "errors": []}
    if apply:
        for item in plan:
            try:
                weknora.delete_knowledge(str(item["id"]))
                result["deleted"].append(item)
            except weknora.WeKnoraError as exc:
                result["errors"].append({"id": item["id"], "error": str(exc)})
    return result


def models() -> dict:
    kb = weknora.get_knowledge_base()
    return {
        "summary_model_id": str(kb.get("summary_model_id") or ""),
        "embedding_model_id": str(kb.get("embedding_model_id") or ""),
        "models": [
            {
                "id": model.get("id"),
                "name": model.get("name"),
                "type": model.get("type"),
                "display_name": model.get("display_name"),
            }
            for model in weknora.list_models()
        ],
    }


def set_summary_model(model_id: str, apply: bool) -> dict:
    current = models()
    result = {"apply": apply, "model_id": model_id, "current": current["summary_model_id"]}
    if apply:
        updated = weknora.update_summary_model(model_id)
        result["updated"] = {
            "id": updated.get("id"),
            "summary_model_id": updated.get("summary_model_id"),
        }
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "cleanup", "summary", "models"])
    parser.add_argument("--apply", action="store_true", help="actually delete/update (default: dry-run)")
    parser.add_argument("--delete-failed", action="store_true", help="also delete parse_status=failed documents")
    parser.add_argument("--model-id", default="", help="summary model ID for the summary command")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--report", default="", help="write the JSON report to this file")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            report = collect_status()
        elif args.command == "cleanup":
            report = cleanup(args.apply, args.delete_failed)
        elif args.command == "models":
            report = models()
        else:
            if not args.model_id:
                print("summary 需要 --model-id", file=sys.stderr)
                return 2
            report = set_summary_model(args.model_id, args.apply)
    except (weknora.WeKnoraError, weknora.WeKnoraUnavailable) as exc:
        print(f"WeKnora 操作失败：{exc}", file=sys.stderr)
        return 1
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text if args.json else text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
