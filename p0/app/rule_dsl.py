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
_RULE_PREFIX = {"interval": "J", "combination": "C", "grade": "G"}


def generate_judgment_rules(
    items: list[dict], record_template: dict | list | None = None
) -> dict:
    """Draft high-confidence interval, combination and grade rules.

    A hit means out of specification (``RulePack.evaluate`` semantics).
    Combination rules group same-unit limits (``and``, or ``or`` for an
    explicit 或); grade rules require explicit ``level``/``grade`` metadata and
    never infer levels.  Low-confidence or unusable limits stay in ``pending``.
    """
    rules: list[dict] = []
    pending: list[dict] = []
    for item in items:
        checks, skipped = _item_checks(item)
        pending.extend(skipped)
        groups: dict[str, list[dict]] = {}
        for check in checks:
            groups.setdefault(check["unit"] or check["column"], []).append(check)
        for members in groups.values():
            leveled = [check for check in members if check["level"]]
            if len(leveled) > 1:
                rules.append(_rule(item, "grade", leveled))
                members = [check for check in members if not check["level"]]
            if len(members) > 1:
                rules.append(_rule(item, "combination", members))
            elif members:
                rules.append(_rule(item, "interval", members))
    counts = {kind: 0 for kind in _RULE_PREFIX}
    for rule in rules:
        counts[rule["type"]] += 1
        rule["id"] = f"{_RULE_PREFIX[rule['type']]}{counts[rule['type']]:03d}"
    result = {"version": 1, "rules": rules, "pending": pending, "counts": counts}
    if record_template is not None:
        result["rules"] = bind_rules_to_template(rules, record_template)
    return result


def _item_checks(item: dict) -> tuple[list[dict], list[dict]]:
    """Return usable high-confidence checks plus the limits held for review."""
    checks: list[dict] = []
    pending: list[dict] = []
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
        elif entry.get("lower" if op == "lt" else "upper") is None:
            pending.append(_pending_limit(item, entry, index, "限值边界缺失，待人工确认"))
            continue
        else:
            value = entry["lower" if op == "lt" else "upper"]
        checks.append({
            "op": op, "value": value, "kind": entry.get("kind"),
            "lower": entry.get("lower"), "upper": entry.get("upper"),
            "unit": str(entry.get("unit") or ""), "raw": str(entry.get("raw") or ""),
            "cell_raw": str(entry.get("cell_raw") or ""),
            "level": str(entry.get("level") or entry.get("grade") or "").strip(),
            "logic": str(entry.get("logic") or "").strip().lower(),
            "page": entry.get("table_page") or item.get("page"),
            "table_row": entry.get("table_row"), "column": entry.get("column") or "",
            "item_clause": entry.get("item_clause") or item.get("clause") or "",
        })
    return checks, pending


def _rule(item: dict, kind: str, checks: list[dict]) -> dict:
    first = checks[0]
    rule = {
        "id": "", "type": kind, "clause": first["item_clause"],
        "item_clause": first["item_clause"], "name": item.get("name") or "",
        "unit": first["unit"], "page": first["page"],
        "table_row": first["table_row"], "column": first["column"],
        "confidence": "high", "status": "draft", "source": "standard",
    }
    payload = [{key: check[key] for key in
                ("op", "value", "kind", "lower", "upper", "raw", "level")}
               for check in checks]
    if kind == "interval":
        rule.update({**payload[0], "raw": first["raw"]})
    elif kind == "combination":
        rule["logic"] = "or" if any(
            check["logic"] == "or" or "或" in check["raw"] or "或" in check["cell_raw"]
            for check in checks) else "and"
        rule["raw"] = (" 或 " if rule["logic"] == "or" else " 且 ").join(
            check["raw"] or "—" for check in checks)
        rule["checks"] = payload
    else:
        rule["raw"] = "；".join(check["raw"] or "—" for check in checks)
        rule["levels"] = [{"level": check["level"], **data}
                          for check, data in zip(checks, payload)]
    return rule


def bind_rules_to_template(
    rules: list[dict], template: dict | list[dict]
) -> list[dict]:
    """Bind rules to explicit original-record template fields.

    ``template`` is ``{"id": ..., "fields": [{"key", "clause", "unit"?}]}``.
    A rule binds only to exactly one matching field; ambiguous rules stay
    unbound, so no record field is guessed.
    """
    fields = template.get("fields") if isinstance(template, dict) else template
    fields = [field for field in fields or []
              if isinstance(field, dict) and field.get("key")]
    template_id = str(template.get("id") or "") if isinstance(template, dict) else ""
    bound: list[dict] = []
    for rule in rules:
        matches = [field for field in fields
                   if str(field.get("clause") or "") == rule.get("item_clause")
                   and (not field.get("unit") or str(field["unit"]) == rule.get("unit"))]
        item = dict(rule)
        item["binding"] = ({"template": template_id, "field": str(matches[0]["key"])}
                           if len(matches) == 1 else None)
        bound.append(item)
    return bound


def judge_record(record: dict, rules: list[dict] | dict) -> dict:
    """Judge an entered original record against template-bound rules.

    Returns 合格/不合格/待录入; a failed rule yields 不合格, while missing or
    non-numeric values keep the record at 待录入.
    """
    if isinstance(rules, dict):
        rules = rules.get("rules") or []
    results: list[dict] = []
    missing: list[str] = []
    for rule in rules:
        field = (rule.get("binding") or {}).get("field")
        if not field:
            continue
        try:
            value = float(record.get(field))
        except (TypeError, ValueError):
            missing.append(field)
            continue
        results.append({
            "rule_id": rule.get("id"), "type": rule.get("type") or "interval",
            "field": field, "value": value, **_judge(value, rule),
        })
    if any(result["passed"] is False for result in results):
        verdict = "不合格"
    elif results and not missing:
        verdict = "合格"
    else:
        verdict = "待录入"
    return {
        "verdict": verdict, "evaluated": len(results),
        "results": results, "missing": missing,
    }


def _judge(value: float, rule: dict) -> dict:
    if rule.get("type") == "grade":
        passed = [level for level in rule.get("levels") or []
                  if _passes(value, level)]
        if not passed:
            return {"passed": False, "level": None}
        ops = {level.get("op") for level in passed}
        try:
            if ops == {"gt"}:
                chosen = min(passed, key=lambda level: float(level["value"]))
            elif ops == {"lt"}:
                chosen = max(passed, key=lambda level: float(level["value"]))
            else:
                chosen = passed[0]
        except (KeyError, TypeError, ValueError):
            chosen = passed[0]
        return {"passed": True, "level": chosen.get("level")}
    outcomes = [_passes(value, check) for check in rule.get("checks") or [rule]]
    passed = any(outcomes) if rule.get("logic") == "or" else all(outcomes)
    return {"passed": passed, "level": None}


def _passes(value: float, check: dict) -> bool:
    op = str(check.get("op") or "")
    if op not in VALID_OPS:
        return False
    try:
        return not _match(value, op, check.get("value"))
    except (TypeError, ValueError, IndexError):
        return False


def _pending_limit(item: dict, entry: dict, index: int, reason: str) -> dict:
    """One isolated limit, kept for the workbench's 待确认 list."""
    pending = dict(entry)
    pending.update(
        clause=item.get("clause") or entry.get("clause") or "",
        item_clause=entry.get("item_clause") or item.get("clause") or "",
        name=item.get("name") or "", limit_index=index,
        page=entry.get("table_page") or item.get("page"), reason=reason,
    )
    return pending
