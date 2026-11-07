"""B2: structured technical limits from standard requirement text.

Only explicit comparisons (≥/≤/不超过/...), ranges (10~20 MPa) and tolerances
(150±5 g) produce a limit; a bare value or a reference to another standard is
left alone.  Each parsed item gains ``limits`` and ``limits_status``::

    {"kind": "upper", "lower": None, "upper": 3, "value": 3,
     "tolerance": None, "unit": "N·m", "clause": "5.3.3.2",
     "item_clause": "5.3.3.2", "raw": "≤3N·m"}

``kind`` is ``lower``/``upper``/``range``/``tolerance``.  For ``tolerance``
``lower``/``upper`` are the nominal value plus/minus the stated tolerance.
"""
from __future__ import annotations

import re

_NUM = r"-?\d+(?:\.\d+)?"
_UNIT = (
    r"(?:N·m|N\.m|kN·m|kN|N|MPa|kPa|GPa|Pa|MΩ|kΩ|Ω|mA|kV|V|A|"
    r"dB\(A\)|dB|HV|HBW|HRC|HRB|μm|um|mm|cm|mL/min|ml/min|mL|ml|L/min|L|m|"
    r"kg|mg|g|℃|°C|%|h|min|s|次|级|个|倍|份|根|张|片|支|"
    r"兆帕|千帕|帕|千牛|牛|毫米|厘米|米|千克|克|毫克|毫升|升|小时|分钟|秒|摄氏度|分贝)"
)
_UNIT_FIX = {"°C": "℃", "Mpa": "MPa", "MPA": "MPa", "N.m": "N·m", "Nm": "N·m"}
_UPPER_OPS = {
    "≤", "＜", "<", "<=", "不大于", "不高于", "不超过", "不应大于",
    "不得大于", "不应超过", "不得超过", "最多", "最大",
}
_LOWER_OPS = {
    "≥", "＞", ">", ">=", "不小于", "不低于", "不少于", "不应小于",
    "不得小于", "至少", "最小",
}

_TOL_PAREN = re.compile(
    rf"[（(]\s*(?P<value>{_NUM})\s*±\s*(?P<tol>{_NUM})\s*[)）]\s*(?P<u2>{_UNIT})"
)
_TOL = re.compile(
    rf"(?P<value>{_NUM})\s*(?P<u1>{_UNIT})?\s*±\s*(?P<tol>{_NUM})\s*(?P<u2>{_UNIT})?"
)
_TOL_BARE = re.compile(rf"±\s*(?P<tol>{_NUM})\s*(?P<u2>{_UNIT})")
_RANGE = re.compile(
    rf"(?:(?P<pre>{_UNIT})\s*)?[（(]?\s*(?P<low>{_NUM})\s*(?P<u1>{_UNIT})?\s*"
    rf"[~～至]\s*(?P<high>{_NUM})\s*(?P<u2>{_UNIT})?\s*[)）]?\s*(?P<u3>{_UNIT})?"
)
_CMP = re.compile(
    r"(?P<op>≥|≤|＞|＜|>=|<=|>|<|不应大于|不得大于|不应小于|不得小于|"
    r"不小于|不大于|不低于|不高于|不少于|不超过|至少|最多|最大|最小)"
    rf"\s*[:：]?\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?"
)


def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _round(value: float) -> float | int:
    rounded = round(value, 9)
    return int(rounded) if float(rounded).is_integer() else rounded


def _unit(value: str | None) -> str:
    return _UNIT_FIX.get(value, value) if value else ""


def _normalise(text: str) -> str:
    text = (text or "").replace("−", "-").replace("－", "-").replace("\u3000", " ")
    for raw, fixed in _UNIT_FIX.items():
        text = text.replace(raw, fixed)
    return text


def _entry(kind: str, unit: str | None, lower=None, upper=None, value=None,
           tolerance=None) -> dict:
    return {
        "kind": kind,
        "lower": lower,
        "upper": upper,
        "value": value,
        "tolerance": tolerance,
        "unit": _unit(unit),
    }


def _tolerance(match: re.Match) -> dict | None:
    groups = match.groupdict()
    unit = groups.get("u1") or groups.get("u2")
    if not unit:
        return None
    value, tolerance = _num(match.group("value")), _num(match.group("tol"))
    return _entry("tolerance", unit, _round(value - tolerance),
                  _round(value + tolerance), value, tolerance)


def _bare_tolerance(match: re.Match) -> dict:
    return _entry("tolerance", match.group("u2"),
                  tolerance=_num(match.group("tol")))


def _range(match: re.Match) -> dict | None:
    unit = (
        match.group("pre") or match.group("u1") or match.group("u2")
        or match.group("u3")
    )
    if not unit:
        return None
    return _entry("range", unit, _num(match.group("low")), _num(match.group("high")))


def _comparison(match: re.Match) -> dict:
    op, value = match.group("op"), _num(match.group("num"))
    upper = op in _UPPER_OPS
    return _entry("upper" if upper else "lower", match.group("unit"),
                  upper=value if upper else None,
                  lower=None if upper else value, value=value)


def _collect(text: str) -> list[dict]:
    """Apply the limit patterns in priority order, masking matched spans."""
    found: list[dict] = []
    for regex, build in (
        (_TOL_PAREN, _tolerance),
        (_TOL, _tolerance),
        (_TOL_BARE, _bare_tolerance),
        (_RANGE, _range),
        (_CMP, _comparison),
    ):
        def replace(match: re.Match) -> str:
            entry = build(match)
            if entry is None:
                return match.group(0)
            entry["raw"] = match.group(0).strip()
            found.append(entry)
            return " " * (match.end() - match.start())

        text = regex.sub(replace, text)
    return found


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (
            entry["kind"], entry["lower"], entry["upper"], entry["value"],
            entry["tolerance"], entry["unit"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def extract_limits(text: str) -> list[dict]:
    """Extract explicit limits from one requirement clause (no inference)."""
    return _dedupe(_collect(_normalise(text)))


def apply_limits(items: list[dict]) -> dict:
    """Attach ``limits``/``limits_status`` and return summary counts."""
    total = extracted = 0
    for item in items:
        clause = item.get("clause", "")
        entries = extract_limits(item.get("requirement") or "")
        for entry in entries:
            entry["clause"] = clause
            entry["item_clause"] = clause
        item["limits"] = entries
        item["limits_status"] = "extracted" if entries else "not_extracted"
        total += len(entries)
        extracted += 1 if entries else 0
    return {
        "limits": total,
        "items_with_limits": extracted,
        "limits_not_extracted": len(items) - extracted,
    }
