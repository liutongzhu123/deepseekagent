"""Admin-managed rule configuration for the three P0/P1 capabilities.

The rules used to be constants inside ``rules.py``, ``standard_parser.py`` and
``quote.py``.  Administrators now edit them from the admin web page: report
rule switches/severity/thresholds, standard parse section aliases and output
fields, and quote header aliases/units/tax rates/CRM columns.

The document lives in one JSON file (``P0_CONFIG_PATH``, default
``data/config/rules.json``) and is validated before it is written.  Modules do
not read the file directly: ``main`` injects the relevant section, so scripts
and unit tests keep the documented defaults.

Only administrators can read or change it (``config:read`` /
``config:manage``).  Every change bumps ``revision`` and is written to the
audit log by the API layer.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rules import RULES

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("P0_DATA_DIR", ROOT / "data"))
CONFIG_PATH = Path(os.getenv("P0_CONFIG_PATH", DATA_DIR / "config" / "rules.json"))

SCHEMA_VERSION = 1
SECTIONS = ("report", "standard", "quote")

# ---------------------------------------------------------------------------
# Defaults.  They mirror the behaviour before this increment, so an untouched
# installation keeps producing the same audit/parse/quote results.
# ---------------------------------------------------------------------------

_REPORT_SEVERITY = {
    "R01": "high",
    "R02": "high",
    "R03": "high",
    "R04": "high",
    "R05": "high",
    "R06": "high",
    "R07": "medium",
    "R08": "medium",
    "R09": "medium",
    "R10": "high",
    "R11": "low",
}

DEFAULT_REPORT_RULES = [
    {
        "id": rule_id,
        "title": title,
        "description": description,
        "enabled": True,
        "severity": _REPORT_SEVERITY.get(rule_id, "medium"),
    }
    for rule_id, title, description in RULES
]

DEFAULT_STANDARD_FIELDS = [
    {"key": "name", "label": "实检项目", "required": False, "export": True},
    {"key": "clause", "label": "技术要求条款", "required": False, "export": True},
    {
        "key": "requirement",
        "label": "技术要求/判定要求",
        "required": False,
        "export": True,
    },
    {"key": "method_clause", "label": "试验方法条款", "required": False, "export": True},
    {
        "key": "method",
        "label": "试验方法（具体方法）",
        "required": False,
        "export": True,
    },
    {"key": "group", "label": "项目分组", "required": False, "export": False},
    {"key": "page", "label": "技术要求页码", "required": False, "export": False},
    {"key": "method_page", "label": "试验方法页码", "required": False, "export": False},
]

STANDARD_FIELD_KEYS = tuple(field["key"] for field in DEFAULT_STANDARD_FIELDS)

DEFAULT_STANDARD = {
    "section_aliases": {
        "技术要求": ["技术要求", "技术条件", "性能要求", "要求"],
        "试验方法": ["试验方法", "试验方法及要求", "检验方法", "测试方法"],
    },
    "fields": DEFAULT_STANDARD_FIELDS,
}

DEFAULT_QUOTE = {
    "default_discount": 60.0,
    "product_tax_rate": 0.16,
    "service_tax_rate": 0.06,
    "headers": {
        "item": ["试验项目", "测试项目"],
        "price": ["单价", "含税单价"],
        "quantity": ["数量"],
        "unit": ["单位"],
        "sample": ["零件/总成", "零件/总成名称"],
    },
    "unit_map": {
        "席位": "席位/次",
        "席位/次": "席位/次",
        "袋子": "次",
        "袋": "次",
        "套": "套",
        "件": "件",
        "批": "批",
        "次": "次",
        "小时": "小时",
        "h": "小时",
        "回路": "回路",
        "种": "种",
        "材料": "材料",
    },
    "crm_columns": [
        {"source": "sample_name", "label": "样品名称", "enabled": True},
        {"source": "item_name", "label": "项目名称", "enabled": True},
        {"source": "item_name", "label": "检验项目", "enabled": True},
        {"source": "standard", "label": "标准", "enabled": True},
        {"source": "department", "label": "检验科室", "enabled": True},
        {"source": "unit", "label": "单位", "enabled": True},
        {"source": "quantity", "label": "数量", "enabled": True},
        {"source": "unit_price", "label": "含税单价", "enabled": True},
        {"source": "has_discount", "label": "是否有折扣", "enabled": True},
        {"source": "discount", "label": "折扣", "enabled": True},
        {"source": "note", "label": "备注", "enabled": True},
    ],
}

QUOTE_HEADER_KEYS = ("item", "price", "quantity", "unit", "sample")
QUOTE_CRM_SOURCES = (
    "sample_name",
    "item_name",
    "standard",
    "department",
    "unit",
    "quantity",
    "unit_price",
    "has_discount",
    "discount",
    "note",
)

SEVERITIES = ("high", "medium", "low", "info")

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "revision": 0,
    "updated_at": "",
    "updated_by": "",
    "report": {
        "mean_tolerance": 0.05,
        "expected_notices": 8,
        "rules": DEFAULT_REPORT_RULES,
    },
    "standard": DEFAULT_STANDARD,
    "quote": DEFAULT_QUOTE,
}

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"key": None, "config": None, "warning": ""}


class ConfigError(ValueError):
    """Raised when a configuration section is invalid."""


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` into a copy of ``base`` (lists are replaced)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _text(value: Any, label: str, *, limit: int = 200, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label}必须是文本")
    text = value.strip()
    if not text and not allow_empty:
        raise ConfigError(f"{label}不能为空")
    if len(text) > limit:
        raise ConfigError(f"{label}不能超过 {limit} 个字符")
    return text


def _bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{label}必须是布尔值")


def _number(value: Any, label: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label}必须是数字")
    number = float(value)
    if not low <= number <= high:
        raise ConfigError(f"{label}必须在 {low} 到 {high} 之间")
    return round(number, 6)


def _int(value: Any, label: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label}必须是整数")
    if not low <= value <= high:
        raise ConfigError(f"{label}必须在 {low} 到 {high} 之间")
    return value


def _string_list(value: Any, label: str, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label}至少需要 1 个值")
    if len(value) > limit:
        raise ConfigError(f"{label}最多 {limit} 个值")
    output: list[str] = []
    for item in value:
        text = _text(item, label, limit=40)
        if text not in output:
            output.append(text)
    return output


def _validate_report(section: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ConfigError("report 配置必须是对象")
    rules = section.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ConfigError("report.rules 不能为空")
    known = {rule["id"]: rule for rule in DEFAULT_REPORT_RULES}
    seen: dict[str, dict[str, Any]] = {}
    for item in rules:
        if not isinstance(item, dict):
            raise ConfigError("每条审核规则必须是对象")
        rule_id = item.get("id")
        if rule_id not in known:
            raise ConfigError(f"未知的审核规则：{rule_id}")
        if rule_id in seen:
            raise ConfigError(f"审核规则重复：{rule_id}")
        severity = item.get("severity")
        if severity not in SEVERITIES:
            raise ConfigError(f"{rule_id} 的严重级别必须是 {', '.join(SEVERITIES)}")
        seen[rule_id] = {
            "id": rule_id,
            "title": _text(item.get("title"), f"{rule_id} 名称", limit=100),
            "description": _text(
                item.get("description"), f"{rule_id} 说明", limit=500, allow_empty=True
            ),
            "enabled": _bool(item.get("enabled"), f"{rule_id} 启用状态"),
            "severity": severity,
        }
    missing = [rule_id for rule_id in known if rule_id not in seen]
    if missing:
        raise ConfigError(f"缺少审核规则：{', '.join(missing)}")
    return {
        "mean_tolerance": _number(
            section.get("mean_tolerance", 0.05), "均值容差", low=0, high=1
        ),
        "expected_notices": _int(
            section.get("expected_notices", 8), "注意事项条数", low=0, high=50
        ),
        "rules": [seen[rule_id] for rule_id in known],
    }


def _validate_standard(section: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ConfigError("standard 配置必须是对象")
    aliases = section.get("section_aliases")
    if not isinstance(aliases, dict):
        raise ConfigError("standard.section_aliases 必须是对象")
    extra = set(aliases) - {"技术要求", "试验方法"}
    if extra:
        raise ConfigError(f"未知的标准章节：{', '.join(sorted(extra))}")
    clean_aliases = {
        "技术要求": _string_list(
            aliases.get("技术要求", DEFAULT_STANDARD["section_aliases"]["技术要求"]),
            "技术要求章节别名",
        ),
        "试验方法": _string_list(
            aliases.get("试验方法", DEFAULT_STANDARD["section_aliases"]["试验方法"]),
            "试验方法章节别名",
        ),
    }
    fields = section.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ConfigError("standard.fields 不能为空")
    seen: dict[str, dict[str, Any]] = {}
    for item in fields:
        if not isinstance(item, dict):
            raise ConfigError("每个标准字段必须是对象")
        key = item.get("key")
        if key not in STANDARD_FIELD_KEYS:
            raise ConfigError(f"未知的标准字段：{key}")
        if key in seen:
            raise ConfigError(f"标准字段重复：{key}")
        seen[key] = {
            "key": key,
            "label": _text(item.get("label"), f"{key} 显示名", limit=40),
            "required": _bool(item.get("required", False), f"{key} 必填状态"),
            "export": _bool(item.get("export", False), f"{key} 导出状态"),
        }
    if not any(field["export"] for field in seen.values()):
        raise ConfigError("标准字段至少需要 1 个导出字段")
    return {
        "section_aliases": clean_aliases,
        "fields": list(seen.values()),
    }


def _validate_quote(section: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(section, dict):
        raise ConfigError("quote 配置必须是对象")
    headers = section.get("headers")
    if not isinstance(headers, dict):
        raise ConfigError("quote.headers 必须是对象")
    extra = set(headers) - set(QUOTE_HEADER_KEYS)
    if extra:
        raise ConfigError(f"未知的报价表头：{', '.join(sorted(extra))}")
    clean_headers = {}
    for key in QUOTE_HEADER_KEYS:
        clean_headers[key] = _string_list(
            headers.get(key, DEFAULT_QUOTE["headers"][key]), f"{key} 表头别名"
        )
    unit_map = section.get("unit_map")
    if not isinstance(unit_map, dict):
        raise ConfigError("quote.unit_map 必须是对象")
    if len(unit_map) > 200:
        raise ConfigError("单位映射最多 200 条")
    clean_units: dict[str, str] = {}
    for source, target in unit_map.items():
        clean_units[_text(source, "单位映射来源", limit=40)] = _text(
            target, "单位映射目标", limit=40
        )
    columns = section.get("crm_columns")
    if not isinstance(columns, list) or not columns:
        raise ConfigError("quote.crm_columns 不能为空")
    if len(columns) > 50:
        raise ConfigError("CRM 列最多 50 个")
    clean_columns = []
    for item in columns:
        if not isinstance(item, dict):
            raise ConfigError("每个 CRM 列必须是对象")
        source = item.get("source")
        if source not in QUOTE_CRM_SOURCES:
            raise ConfigError(f"未知的 CRM 列来源：{source}")
        clean_columns.append(
            {
                "source": source,
                "label": _text(item.get("label"), "CRM 列名", limit=40),
                "enabled": _bool(item.get("enabled", True), "CRM 列启用状态"),
            }
        )
    if not any(column["enabled"] for column in clean_columns):
        raise ConfigError("CRM 至少需要 1 个启用列")
    return {
        "default_discount": _number(
            section.get("default_discount", 60.0), "默认折扣", low=0, high=100
        ),
        "product_tax_rate": _number(
            section.get("product_tax_rate", 0.16), "产品税率", low=0, high=1
        ),
        "service_tax_rate": _number(
            section.get("service_tax_rate", 0.06), "服务税率", low=0, high=1
        ),
        "headers": clean_headers,
        "unit_map": clean_units,
        "crm_columns": clean_columns,
    }


_VALIDATORS = {
    "report": _validate_report,
    "standard": _validate_standard,
    "quote": _validate_quote,
}


def validate_section(section: str, value: dict[str, Any]) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ConfigError(f"未知配置分区：{section}")
    return _VALIDATORS[section](value)


def validate_config(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ConfigError("配置必须是对象")
    output = {
        "schema_version": SCHEMA_VERSION,
        "revision": max(0, int(document.get("revision", 0) or 0)),
        "updated_at": str(document.get("updated_at", "") or ""),
        "updated_by": str(document.get("updated_by", "") or ""),
    }
    for section in SECTIONS:
        output[section] = validate_section(
            section, _deep_merge(DEFAULT_CONFIG[section], document.get(section, {}))
        )
    return output


def _cache_key(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(path), 0, 0)


def _load_cached() -> tuple[dict[str, Any], str]:
    path = Path(CONFIG_PATH)
    key = _cache_key(path)
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["config"] is not None:
            return copy.deepcopy(_CACHE["config"]), _CACHE["warning"]
        warning = ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            config = validate_config(raw)
        except FileNotFoundError:
            config = default_config()
        except (OSError, ValueError, TypeError, ConfigError) as exc:
            config = default_config()
            warning = f"配置文件无效，已回退到默认规则：{exc}"
        _CACHE.update({"key": key, "config": config, "warning": warning})
        return copy.deepcopy(config), warning


def load() -> dict[str, Any]:
    """Return the effective configuration (defaults when the file is absent)."""
    return _load_cached()[0]


def describe() -> dict[str, Any]:
    config, warning = _load_cached()
    return {
        "config": config,
        "path": str(CONFIG_PATH),
        "warning": warning,
        "sections": list(SECTIONS),
    }


def report_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return (config or load())["report"]


def standard_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return (config or load())["standard"]


def quote_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return (config or load())["quote"]


def _write(document: dict[str, Any]) -> None:
    path = Path(CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    with _LOCK:
        _CACHE.update({"key": None, "config": None, "warning": ""})


def update_section(
    section: str, payload: dict[str, Any], updated_by: str = ""
) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ConfigError(f"未知配置分区：{section}")
    if not isinstance(payload, dict):
        raise ConfigError("配置分区必须是对象")
    with _LOCK:
        current = load()
        merged = _deep_merge(DEFAULT_CONFIG[section], payload)
        validated = validate_section(section, merged)
        current[section] = validated
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = _now()
        current["updated_by"] = updated_by
        _write(current)
        return copy.deepcopy(current)


def reset_section(section: str, updated_by: str = "") -> dict[str, Any]:
    if section not in SECTIONS:
        raise ConfigError(f"未知配置分区：{section}")
    with _LOCK:
        current = load()
        current[section] = copy.deepcopy(DEFAULT_CONFIG[section])
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = _now()
        current["updated_by"] = updated_by
        _write(current)
        return copy.deepcopy(current)


def reset_cache() -> None:
    """Drop the in-process cache (tests and admin tooling)."""
    with _LOCK:
        _CACHE.update({"key": None, "config": None, "warning": ""})
