"""DeepSeek semantic review (stage 2.1).

The module is disabled by default and never sends report text unless
``P0_SEMANTIC_ENABLED=1`` and ``DEEPSEEK_API_KEY`` are set (D5 compliance
gate).  Any transport/schema failure degrades to ``error``/``skip``; it never
returns a passing semantic result.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "semantic.yaml"
CHECK_IDS = ("S01", "S02", "S03", "S04")
_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


@dataclass
class SemanticConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_seconds: int = 20
    max_chars: int = 12000
    checks: dict[str, str] = field(default_factory=lambda: {key: key for key in CHECK_IDS})


def _yaml_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, Exception):  # noqa: BLE001 - config boundary
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_config() -> SemanticConfig:
    data = _yaml_config()
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    return SemanticConfig(
        enabled=os.getenv("P0_SEMANTIC_ENABLED", "0") == "1" or bool(data.get("enabled")),
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("P0_SEMANTIC_BASE_URL", str(data.get("base_url") or "https://api.deepseek.com")),
        model=os.getenv("P0_SEMANTIC_MODEL", str(data.get("model") or "deepseek-chat")),
        timeout_seconds=int(os.getenv("P0_SEMANTIC_TIMEOUT", str(data.get("timeout_seconds") or 20))),
        max_chars=int(data.get("max_chars") or 12000),
        checks={key: str(checks.get(key, key)) for key in CHECK_IDS},
    )


def redact(text: str) -> str:
    text = _VIN.sub("[VIN]", text or "")
    text = _PHONE.sub("[PHONE]", text)
    return _EMAIL.sub("[EMAIL]", text)


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_findings(content: str, allowed: dict[str, str]) -> list[dict]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    data = json.loads(cleaned)
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        raise ValueError("missing findings array")
    parsed: list[dict] = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("finding must be an object")
        rule_id = str(item.get("rule_id") or "")
        if rule_id not in allowed:
            raise ValueError(f"unknown semantic rule {rule_id}")
        status = str(item.get("status") or "warn")
        if status not in {"fail", "warn"}:
            raise ValueError(f"invalid semantic status {status}")
        message = str(item.get("message") or "").strip()
        if not message:
            raise ValueError("empty semantic message")
        parsed.append(
            {
                "rule_id": rule_id,
                "title": allowed[rule_id],
                "status": status,
                "severity": str(item.get("severity") or "medium"),
                "message": message,
                "evidence": str(item.get("evidence") or ""),
                "suggestion": str(item.get("suggestion") or ""),
                "location": {"page": 0, "section": "语义审核", "cell": ""},
            }
        )
    return parsed


def audit_semantics(
    report_text: str,
    config: SemanticConfig | None = None,
    post: Callable[[str, dict, dict, int], dict] | None = None,
) -> dict:
    cfg = config or load_config()
    if not cfg.enabled:
        return {"status": "skipped", "reason": "semantic_disabled", "findings": []}
    if not cfg.api_key:
        return {"status": "skipped", "reason": "missing_api_key", "findings": []}
    if not (report_text or "").strip():
        return {"status": "skipped", "reason": "empty_report", "findings": []}
    prompt = "只输出 JSON：{\"findings\":[{\"rule_id\":\"S01..S04\",\"status\":\"fail|warn\",\"severity\":\"high|medium|low\",\"message\":\"...\",\"evidence\":\"...\",\"suggestion\":\"...\"}]}。检查：" + "；".join(f"{key} {label}" for key, label in cfg.checks.items())
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是授权签字人前的语义复核助手，只标疑似问题，不替代签字。"},
            {"role": "user", "content": f"{prompt}\n\n报告文本：\n{redact(report_text)[:cfg.max_chars]}"},
        ],
    }
    try:
        response = (post or _post_json)(
            f"{cfg.base_url.rstrip('/')}/chat/completions",
            payload,
            {"Authorization": f"Bearer {cfg.api_key}"},
            cfg.timeout_seconds,
        )
        content = response["choices"][0]["message"]["content"]
        findings = _parse_findings(content, cfg.checks)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "error", "reason": f"transport: {exc}", "findings": []}
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "reason": f"schema: {exc}", "findings": []}
    return {"status": "ok", "model": cfg.model, "findings": findings}
