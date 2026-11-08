"""Stage-2.3 configurable rule DSL.

A rule is data, not code: source + field + operator + value + evidence +
severity + suggestion.  Packs carry a version and per-rule rollout percentage;
hit counts are kept in memory for feedback.  This module evaluates flat report
or input contexts and does not mutate the deterministic R01–R11 rules.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .standard_extract.table_mapping import is_high_confidence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACK = ROOT / "config" / "rules_dsl.yaml"
VALID_OPS = {
    "empty", "eq", "ne", "contains", "not_contains", "regex", "not_regex",
    "gt", "gte", "lt", "lte", "between", "not_between",
}
VALID_SEVERITIES = {"high", "medium", "low", "info"}


@dataclass
class DSLRule:
    id: str
    name: str
    source: str
    field: str
    op: str
    value: Any
    severity: str = "medium"
    message: str = ""
    suggestion: str = ""
    enabled: bool = True
    rollout: int = 100

    def in_rollout(self) -> bool:
        bucket = int(hashlib.sha1(self.id.encode("utf-8")).hexdigest()[:8], 16) % 100
        return bucket < max(0, min(100, self.rollout))


@dataclass
class RulePack:
    version: int
    rules: list[DSLRule]
    hits: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def by_id(self, rule_id: str) -> DSLRule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    def evaluate(self, context: dict, available_sources: set[str] | None = None) -> list[dict]:
        findings: list[dict] = []
        for rule in self.rules:
            if not rule.enabled or not rule.in_rollout():
                continue
            if available_sources is not None and rule.source not in available_sources:
                self.skipped[rule.id] = self.skipped.get(rule.id, 0) + 1
                continue
            actual = _field(context.get(rule.source) or {}, rule.field)
            if not _match(actual, rule.op, rule.value):
                continue
            self.hits[rule.id] = self.hits.get(rule.id, 0) + 1
            findings.append(
                {
                    "rule_id": rule.id,
                    "title": rule.name,
                    "status": "warn" if rule.severity in {"low", "info"} else "fail",
                    "severity": rule.severity,
                    "message": rule.message or rule.name,
                    "evidence": f"{rule.source}.{rule.field}={actual}",
                    "suggestion": rule.suggestion,
                    "location": {"page": 1, "section": "规则 DSL", "cell": rule.field},
                    "layer": "dsl",
                }
            )
        return findings

    def stats(self) -> dict:
        return {
            "version": self.version,
            "hits": dict(sorted(self.hits.items())),
            "skipped": dict(sorted(self.skipped.items())),
        }


def _field(data: dict, path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _match(actual: Any, op: str, expected: Any) -> bool:
    if op == "empty":
        return actual in (None, "", [], {})
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "contains":
        return expected in (actual or "")
    if op == "not_contains":
        return expected not in (actual or "")
    if op == "regex":
        return re.search(str(expected), str(actual or "")) is not None
    if op == "not_regex":
        return re.search(str(expected), str(actual or "")) is None
    if op in {"between", "not_between"}:
        try:
            left, low, high = float(actual), float(expected[0]), float(expected[1])
        except (TypeError, ValueError, IndexError):
            return False
        inside = low <= left <= high
        return inside if op == "between" else not inside
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}.get(op, False)


def validate_pack_data(data: dict) -> list[str]:
    """Return human-readable validation errors for one pack document."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["规则包必须是 JSON/YAML 对象"]
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        errors.append("version 必须是正整数")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return [*errors, "rules 必须是非空数组"]
    seen: set[str] = set()
    for index, item in enumerate(rules, start=1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条规则必须是对象")
            continue
        rule_id = str(item.get("id") or "").strip()
        if not rule_id:
            errors.append(f"第 {index} 条规则缺少 id")
        elif rule_id in seen:
            errors.append(f"规则 id 重复：{rule_id}")
        seen.add(rule_id)
        if not str(item.get("name") or "").strip():
            errors.append(f"{rule_id or index} 缺少 name")
        if str(item.get("source") or "") not in {"report", "inputs"}:
            errors.append(f"{rule_id or index} source 必须是 report 或 inputs")
        if not str(item.get("field") or "").strip():
            errors.append(f"{rule_id or index} 缺少 field")
        if str(item.get("op") or "") not in VALID_OPS:
            errors.append(f"{rule_id or index} op 不支持：{item.get('op')}")
        if str(item.get("severity") or "medium") not in VALID_SEVERITIES:
            errors.append(f"{rule_id or index} severity 不支持：{item.get('severity')}")
        try:
            rollout = int(item.get("rollout", 100))
        except (TypeError, ValueError):
            rollout = -1
        if not 0 <= rollout <= 100:
            errors.append(f"{rule_id or index} rollout 必须在 0..100")
    return errors


def validate_pack(path: str | Path | None = None) -> list[str]:
    source = Path(path or DEFAULT_PACK)
    if not source.exists():
        return [f"规则包不存在：{source}"]
    try:
        data = _load_document(source)
    except Exception as exc:  # noqa: BLE001 - config boundary
        return [f"规则包无法解析：{exc}"]
    return validate_pack_data(data)


def rule_catalog(pack: RulePack) -> list[dict]:
    return [
        {
            "id": rule.id,
            "title": rule.name,
            "description": rule.message or rule.name,
            "source": rule.source,
            "field": rule.field,
            "op": rule.op,
            "severity": rule.severity,
            "enabled": rule.enabled,
            "rollout": rule.rollout,
            "layer": "dsl",
        }
        for rule in pack.rules
    ]


def _load_document(source: Path) -> dict:
    text = source.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("规则包必须是对象")
    return data


def load_pack(path: str | Path | None = None) -> RulePack:
    source = Path(path or DEFAULT_PACK)
    if not source.exists():
        return RulePack(version=0, rules=[])
    data = _load_document(source)
    rules = [DSLRule(**item) for item in data.get("rules") or []]
    return RulePack(version=int(data.get("version") or 1), rules=rules)


_LIMIT_RULE_OPS = {
    "upper": "gt",
    "lower": "lt",
    "range": "not_between",
    "tolerance": "not_between",
}


def generate_judgment_rules(items: list[dict]) -> dict:
    """Draft judgment rules from extracted standard limits.

    Rule semantics follow ``RulePack.evaluate``: a hit means the value is out
    of specification (``gt`` upper for a ≤ limit, ``not_between`` for a range).
    Only high-confidence limits become rules.  Low-confidence table mappings
    are returned under ``pending`` (待确认) and never enter ``rules``; a
    direction-less ``kind=value`` limit is also held back, because judging it
    would mean guessing a bound.  Drafts keep the source clause and table
    evidence; binding them to record templates is B6 (SP-5.2).
    """
    rules: list[dict] = []
    pending: list[dict] = []
    for item in items:
        for index, entry in enumerate(item.get("limits") or []):
            if not is_high_confidence(entry):
                pending.append(_pending_limit(item, entry, index, "低置信限值，待人工确认"))
                continue
            op = _LIMIT_RULE_OPS.get(str(entry.get("kind") or ""))
            if op is None:
                pending.append(_pending_limit(item, entry, index, "限值缺少比较方向，待人工确认"))
                continue
            if op == "not_between":
                if entry.get("lower") is None or entry.get("upper") is None:
                    pending.append(_pending_limit(item, entry, index, "区间上下限不完整，待人工确认"))
                    continue
                value: Any = [entry["lower"], entry["upper"]]
            else:
                bound = "lower" if op == "lt" else "upper"
                if entry.get(bound) is None:
                    pending.append(_pending_limit(item, entry, index, "限值边界缺失，待人工确认"))
                    continue
                value = entry[bound]
            rules.append(
                {
                    "id": f"J{len(rules) + 1:03d}",
                    "clause": item.get("clause") or entry.get("clause") or "",
                    "item_clause": entry.get("item_clause") or item.get("clause") or "",
                    "name": item.get("name") or "",
                    "kind": entry.get("kind"),
                    "op": op,
                    "lower": entry.get("lower"),
                    "upper": entry.get("upper"),
                    "value": value,
                    "unit": entry.get("unit") or "",
                    "raw": entry.get("raw") or "",
                    "page": entry.get("table_page") or item.get("page"),
                    "table_row": entry.get("table_row"),
                    "column": entry.get("column") or "",
                    "confidence": "high",
                    "status": "draft",
                    "source": "standard",
                }
            )
    return {"version": 1, "rules": rules, "pending": pending}


def _pending_limit(item: dict, entry: dict, index: int, reason: str) -> dict:
    """One isolated limit, kept for the workbench's 待确认 list."""
    pending = dict(entry)
    pending.update(
        clause=item.get("clause") or entry.get("clause") or "",
        item_clause=entry.get("item_clause") or item.get("clause") or "",
        name=item.get("name") or "",
        limit_index=index,
        page=entry.get("table_page") or item.get("page"),
        reason=reason,
    )
    return pending
