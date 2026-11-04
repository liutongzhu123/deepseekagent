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

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACK = ROOT / "config" / "rules_dsl.yaml"


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

    def evaluate(self, context: dict) -> list[dict]:
        findings: list[dict] = []
        for rule in self.rules:
            if not rule.enabled or not rule.in_rollout():
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
                }
            )
        return findings

    def stats(self) -> dict:
        return {"version": self.version, "hits": dict(sorted(self.hits.items()))}


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
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}.get(op, False)


def load_pack(path: str | Path | None = None) -> RulePack:
    source = Path(path or DEFAULT_PACK)
    if not source.exists():
        return RulePack(version=0, rules=[])
    text = source.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = json.loads(text)
    rules = [DSLRule(**item) for item in data.get("rules") or []]
    return RulePack(version=int(data.get("version") or 1), rules=rules)
