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
DEFAULT_SYSTEM_PROMPT = "你是授权签字人前的语义复核助手，只报告明确、可指认的文本缺陷，不替代签字。"
DEFAULT_CRITERIA = {
    "S01": "错别字/病句：出现错别字、别字、明显病句或语义不通的句子。",
    "S02": "产品名称异常：报告中的产品/样品名称前后不一致，或出现与样品名称明显不同的产品名称，或样品名称是无法识别产品的占位符。",
    "S03": "结论表述：结论与检验数据或符合性判定矛盾（如数据超限值但结论写符合、判定写不符合但结论写符合、写“不判定”但表格有判定）。",
    "S04": "项目对应：结论中的检验项目与报告检验项目/结果表项目不一致，或结论项目与结果表项目缺失对应。",
}
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
    criteria: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CRITERIA))
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


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
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    return SemanticConfig(
        enabled=os.getenv("P0_SEMANTIC_ENABLED", "0") == "1" or bool(data.get("enabled")),
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("P0_SEMANTIC_BASE_URL", str(data.get("base_url") or "https://api.deepseek.com")),
        model=os.getenv("P0_SEMANTIC_MODEL", str(data.get("model") or "deepseek-chat")),
        timeout_seconds=int(os.getenv("P0_SEMANTIC_TIMEOUT", str(data.get("timeout_seconds") or 20))),
        max_chars=int(data.get("max_chars") or 12000),
        checks={key: str(checks.get(key, key)) for key in CHECK_IDS},
        criteria={key: str(criteria.get(key) or DEFAULT_CRITERIA[key]) for key in CHECK_IDS},
        system_prompt=str(data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
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


def build_prompt(cfg: SemanticConfig) -> tuple[str, str]:
    """Return ``(system_prompt, instruction)`` for the semantic call.

    Keeping the criteria in config lets business users tighten or relax a
    check without a code change; the JSON contract stays fixed.
    """
    criteria = cfg.criteria or cfg.checks
    lines = [
        "只报告“明确、可指认”的文本缺陷，不报告风格、完整性、缺字段、格式规范等主观问题。",
        "检查项及判定标准：",
    ]
    for key in CHECK_IDS:
        lines.append(f"- {key} {criteria.get(key) or cfg.checks.get(key, key)}")
    lines += [
        "严格要求：",
        "1. 没有明确缺陷时 findings 必须为空数组，不要为了完整性而报问题。",
        "2. 每个缺陷只报一条，选择最具体的 rule_id；不要把同一问题拆成多条。",
        "3. 只输出 JSON：{\"findings\":[{\"rule_id\":\"S01|S02|S03|S04\",\"status\":\"fail|warn\",\"severity\":\"high|medium|low\",\"message\":\"...\",\"evidence\":\"...\",\"suggestion\":\"...\"}]}。",
        "4. 同一处文本不一致只报一条；如果既像产品名称不一致又像检验项目不对应，优先报 S04（项目对应），不要重复报 S02。",
    ]
    return cfg.system_prompt or DEFAULT_SYSTEM_PROMPT, "\n".join(lines)


def _parse_findings(content: str, allowed: dict[str, str]) -> list[dict]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    data = json.loads(cleaned)
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        raise ValueError("missing findings array")
    parsed: list[dict] = []
    seen: set[str] = set()
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("finding must be an object")
        rule_id = str(item.get("rule_id") or "")
        if rule_id not in allowed:
            raise ValueError(f"unknown semantic rule {rule_id}")
        if rule_id in seen:
            # One finding per check keeps rule_id a stable review key in the workbench.
            continue
        seen.add(rule_id)
        status = str(item.get("status") or "warn")
        if status not in {"fail", "warn"}:
            raise ValueError(f"invalid semantic status {status}")
        message = str(item.get("message") or "").strip()
        if not message:
            raise ValueError("empty semantic message")
        severity = str(item.get("severity") or "medium")
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        parsed.append(
            {
                "rule_id": rule_id,
                "title": allowed[rule_id],
                "status": status,
                "severity": severity,
                "message": message,
                "evidence": str(item.get("evidence") or ""),
                "suggestion": str(item.get("suggestion") or ""),
                "location": {"page": 0, "section": "语义审核", "cell": ""},
                "layer": "semantic",
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
    system_prompt, instruction = build_prompt(cfg)
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{instruction}\n\n报告文本：\n{redact(report_text)[:cfg.max_chars]}"},
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
    return {"status": "ok", "provider": "deepseek", "model": cfg.model, "findings": findings}
