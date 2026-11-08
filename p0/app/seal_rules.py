"""Seal reasonableness rules (RA-1.10 / RA-4.6): configurable, reserved.

Customer answer “用章无标准，预留规则”, so the module is off by default. Seal boxes come from OCR/vision in millimetres (origin: page top-left); the docx parser does not produce them yet, so this is not wired into ``audit_report``. Failure branches F1-F10 are listed in tests/test_seal_rules.py.
"""
from __future__ import annotations

import copy
from pathlib import Path

try:  # requirements installs PyYAML; keep the module importable without it.
    import yaml
except ImportError:  # pragma: no cover - defensive
    yaml = None

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "seal_rules.yaml"
TYPES = ("YF", "QA", "ZY", "ZW")
DEFAULTS = {
    "enabled": False, "tolerance_mm": 6.0, "overlap_ratio": 0.15, "english_forbidden": ["CMA章"],
    "types": {"YF": {"required": ["实验室章"], "allowed": ["实验室章"], "forbidden": ["CMA章", "CNAS章"]}, "QA": {}, "ZY": {}, "ZW": {}},
}

def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _page(value):
    number = _num(value, 0)
    return int(number) if 0 <= number < 100000 else 0

def _names(value):
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, (list, tuple)) else []

def _box(value):
    """Return (x0, y0, x1, y1) in mm, or None for a malformed/degenerate box."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

def _mid(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

_TITLES = {"SE01": "用章种类", "SE02": "印章位置", "SE03": "印章遮挡"}

def _finding(code, status, message, evidence="", severity=None, suggestion=""):
    return {"code": code, "title": _TITLES[code], "status": status, "severity": severity or ("high" if status == "fail" else "info"),
            "message": message, "evidence": evidence, "suggestion": suggestion, "page": 1, "section": "用章", "cell": code}

def load_config(path=None):
    """Return the effective config; any read/parse error falls back to disabled."""
    config = copy.deepcopy(DEFAULTS)
    source = Path(path) if path else CONFIG_PATH
    if yaml is None or not source.exists():
        return config
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return config
    if not isinstance(raw, dict):
        return config
    config["enabled"] = bool(raw.get("enabled", False))
    config["tolerance_mm"] = max(0.0, _num(raw.get("tolerance_mm"), config["tolerance_mm"]))
    config["overlap_ratio"] = min(1.0, max(0.0, _num(raw.get("overlap_ratio"), config["overlap_ratio"])))
    if "english_forbidden" in raw:
        config["english_forbidden"] = _names(raw["english_forbidden"])
    raw_types = raw.get("types") if isinstance(raw.get("types"), dict) else {}
    for kind, rules in raw_types.items():
        if kind not in TYPES or not isinstance(rules, dict):
            continue
        for key in ("required", "allowed", "forbidden", "expected", "protected"):
            if key in rules:
                value = rules[key]
                config["types"][kind][key] = ([item for item in value if isinstance(item, dict)]
                                              if key in ("expected", "protected") and isinstance(value, list)
                                              else _names(value))
    return config

def report_type(report_no):
    """YF/QA/ZY/ZW from the report-number prefix."""
    prefix = str(report_no or "").strip().upper()[:2]
    return prefix if prefix in TYPES else ""

def _observations(items):
    out = []
    for item in items or []:
        if isinstance(item, dict) and (name := str(item.get("type") or item.get("seal") or "").strip()):
            out.append({"type": name, "page": _page(item.get("page")), "box": _box(item.get("bbox"))})
    return out

def _rules_for(config, kind):
    raw = (config.get("types") or {}).get(kind) or {}
    return {"required": _names(raw.get("required")), "allowed": _names(raw.get("allowed")),
            "forbidden": _names(raw.get("forbidden")),
            "expected": [item for item in raw.get("expected") or [] if isinstance(item, dict)],
            "protected": [item for item in raw.get("protected") or [] if isinstance(item, dict)]}

def _judge(kind, observations, rules, config, english):
    findings = []
    # SE01 seal types
    observed = {item["type"] for item in observations}
    forbidden = dict.fromkeys(rules["forbidden"] + (_names(config.get("english_forbidden")) if english else []))
    if not (rules["required"] or rules["allowed"] or forbidden):
        findings.append(_finding("SE01", "skip", f"{kind} 尚未配置用章要求（客户答复：预留规则）"))
    else:
        problems = []
        if missing := [name for name in rules["required"] if name not in observed]:
            problems.append("缺少必需用章：" + "、".join(missing))
        if hits := [name for name in forbidden if name in observed]:
            problems.append("出现禁止用章：" + "、".join(hits))
        if extra := [name for name in observed if rules["allowed"] and name not in rules["allowed"] and name not in forbidden]:
            problems.append("超出允许用章：" + "、".join(sorted(extra)))
        evidence = f"类型={kind}; 观测={sorted(observed)}; 必需={rules['required']}; 允许={rules['allowed']}; 禁止={list(forbidden)}"
        findings.append(_finding("SE01", "fail" if problems else "pass", "；".join(problems) or f"用章种类符合 {kind} 配置", evidence, suggestion="按报告类型补盖/去掉对应印章后复核" if problems else ""))
    # SE02 seal positions
    expected = rules["expected"]
    if not expected:
        findings.append(_finding("SE02", "skip", f"{kind} 未配置印章期望位置"))
    else:
        usable, problems, located = [item for item in observations if item["box"]], [], 0
        for want in expected:
            box = _box(want.get("bbox"))
            page, name = _page(want.get("page")), str(want.get("type") or "").strip()
            if not (name and page > 0 and box):
                continue
            same = [item for item in usable if item["type"] == name and item["page"] == page]
            if not same:
                pages = sorted({item["page"] for item in usable if item["type"] == name})
                if pages:
                    problems.append(f"「{name}」出现在第 {'、'.join(map(str, pages))} 页，期望第 {page} 页")
                continue
            located += 1
            tx, ty = _mid(box)
            cx, cy = min((_mid(item["box"]) for item in same), key=lambda c: abs(c[0] - tx) + abs(c[1] - ty))
            if (distance := ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5) > max(0.0, _num(config.get("tolerance_mm"), 6.0)):
                problems.append(f"「{name}」第 {page} 页偏移 {distance:.1f} mm（dx={cx - tx:.1f}, dy={cy - ty:.1f}）")
        evidence = f"期望={len(expected)}; 已定位={located}; 容差={max(0.0, _num(config.get('tolerance_mm'), 6.0)):g} mm"
        if problems:
            findings.append(_finding("SE02", "fail", "印章移位：" + "；".join(problems), evidence, "medium", "按模板位置重新盖章"))
        elif not located:
            findings.append(_finding("SE02", "skip", "期望位置的印章均未识别到，无法判断是否移位", evidence))
        else:
            findings.append(_finding("SE02", "pass", f"{located} 处印章位置在容差内", evidence))
    # SE03 seal occlusion
    regions = [{"name": str(item.get("name") or "受保护区域"), "page": _page(item.get("page")),
                "box": _box(item.get("bbox"))} for item in rules["protected"]]
    regions = [item for item in regions if item["box"] and item["page"] > 0]
    seals = [item for item in observations if item["box"]]
    if not regions:
        findings.append(_finding("SE03", "skip", f"{kind} 未配置签字/认证标志区域"))
    elif not seals:
        findings.append(_finding("SE03", "skip", "未识别到有效印章框，无法判断遮挡"))
    else:
        limit, problems, worst = min(1.0, max(0.0, _num(config.get("overlap_ratio"), 0.15))), [], 0.0
        for region in regions:
            rx0, ry0, rx1, ry1 = region["box"]
            for seal in seals:
                if seal["page"] != region["page"]:
                    continue
                width = min(seal["box"][2], rx1) - max(seal["box"][0], rx0)
                height = min(seal["box"][3], ry1) - max(seal["box"][1], ry0)
                ratio = width * height / ((rx1 - rx0) * (ry1 - ry0)) if width > 0 and height > 0 else 0.0
                worst = max(worst, ratio)
                if ratio > limit:
                    problems.append(f"「{seal['type']}」遮挡「{region['name']}」（第 {region['page']} 页，覆盖 {ratio:.0%}）")
        evidence = f"受保护区域={len(regions)}; 印章={len(seals)}; 最大覆盖={worst:.0%}; 阈值={limit:.0%}"
        if problems:
            findings.append(_finding("SE03", "fail", "；".join(problems), evidence, suggestion="移开印章，避免覆盖签字/认证标志"))
        else:
            findings.append(_finding("SE03", "pass", f"未发现印章遮挡（最大覆盖 {worst:.0%}）", evidence))
    return findings

def evaluate_seal_rules(report_no, seals, *, english=False, config=None):
    """Judge seal types, positions and occlusion from OCR/vision observations.

    ``seals`` items are ``{"type": "实验室章", "page": 1, "bbox": [x0, y0, x1, y1]}``
    in millimetres. Never returns ``pass`` when disabled or data is missing.
    """
    config = config or load_config()
    kind, observations = report_type(report_no), _observations(seals)
    invalid = sum(1 for item in observations if not item["box"] or item["page"] < 1)
    base = {"enabled": bool(config.get("enabled")), "report_type": kind, "seals": len(observations),
            "located": len(observations) - invalid, "invalid": invalid, "findings": []}
    if not config.get("enabled"):
        return {**base, "status": "skipped", "reason": "module_disabled"}
    if not kind:
        return {**base, "status": "skipped", "reason": "unknown_report_type"}
    if not observations:
        return {**base, "status": "skipped", "reason": "no_seal_observations"}
    findings = _judge(kind, observations, _rules_for(config, kind), config, english)
    return {**base, "status": "checked", "findings": findings,
            "summary": {state: sum(1 for item in findings if item["status"] == state)
                        for state in ("pass", "fail", "warn", "skip")}}
