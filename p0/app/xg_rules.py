"""XG 变更报告编号规则配置（预留，不参与审核判定）。

客户尚未确认“XG 报告编号 = 作废报告编号尾号 +1”等规则；本模块只负责读取
`p0/config/xg_report_number.yaml`，默认关闭，等规则确认后再接校验逻辑。
"""
from __future__ import annotations

from pathlib import Path

try:  # requirements installs PyYAML; keep the loader importable without it
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "xg_report_number.yaml"
DEFAULT_RULES = {
    "report_number_offset_from_voided": 1,
    "require_void_note": True,
    "void_note_keyword": "原报告作废",
}


def load_config(path: str | Path | None = None) -> dict:
    """Return the XG rule config with defaults; disabled unless explicitly enabled."""
    config = {"enabled": False, "rules": dict(DEFAULT_RULES)}
    source = Path(path) if path else CONFIG_PATH
    if yaml is None or not source.exists():
        return config
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return config
    if not isinstance(loaded, dict):
        return config
    config["enabled"] = bool(loaded.get("enabled", False))
    rules = loaded.get("rules")
    if isinstance(rules, dict):
        for key in DEFAULT_RULES:
            if key in rules:
                config["rules"][key] = rules[key]
    return config
