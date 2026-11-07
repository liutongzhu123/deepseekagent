"""B1: structured test conditions from standard clause text.

Only an explicit number together with a unit becomes a condition; a clause
that only refers to another standard is marked ``not_extracted``, never guessed.
An entry is ``{"kind","value","value_max","tolerance","unit","clause",
"item_clause","source","raw"}``: ``clause`` is where the number appears,
``item_clause`` the requirement clause that owns it.  ``value_max`` and
``tolerance`` are set only when the text states a range or a ± tolerance.
"""
from __future__ import annotations

import re

_NUM = r"-?\d+(?:\.\d+)?"
_TEMP = r"(?:℃|°C)"
_UNIT_MAP = {"°C": "℃"}


def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _normalise(text: str) -> str:
    return (text or "").replace("−", "-").replace("－", "-").replace("\u3000", " ")


def _single(match: re.Match) -> dict:
    return {"value": _num(match.group("num")), "value_max": None, "tolerance": None}


def _range(match: re.Match) -> dict:
    return {
        "value": _num(match.group("num")),
        "value_max": _num(match.group("high")),
        "tolerance": None,
    }


def _tolerance(match: re.Match) -> dict:
    tol = match.groupdict().get("tol")
    return {
        "value": _num(match.group("num")),
        "value_max": None,
        "tolerance": _num(tol) if tol else None,
    }


def _with_unit(match: re.Match) -> dict:
    entry = _single(match)
    entry["unit"] = _UNIT_MAP.get(match.group("unit"), match.group("unit"))
    return entry


def _collect(text: str, specs: list[tuple]) -> list[dict]:
    """Apply (regex, builder, fallback unit, kind) specs, masking matched spans."""
    found: list[dict] = []
    for regex, build, unit, kind in specs:
        def replace(match: re.Match) -> str:
            entry = build(match)
            if entry is not None:
                entry.setdefault("unit", unit)
                entry["kind"] = kind
                entry["raw"] = match.group(0).strip()
                found.append(entry)
            return " " * (match.end() - match.start())

        text = regex.sub(replace, text)
    return found


_TEMP_TOL = re.compile(rf"(?P<num>{_NUM})\s*{_TEMP}\s*±\s*(?P<tol>{_NUM})\s*{_TEMP}?")
_TEMP_TOL_BARE = re.compile(rf"(?P<num>{_NUM})\s*±\s*(?P<tol>{_NUM})\s*{_TEMP}")
_TEMP_TOL_PAREN = re.compile(
    rf"[（(]\s*(?P<num>{_NUM})\s*±\s*(?P<tol>{_NUM})\s*[)）]\s*{_TEMP}"
)
_TEMP_RANGE = re.compile(
    rf"(?P<num>{_NUM})\s*{_TEMP}\s*[~～至]\s*(?P<high>{_NUM})\s*{_TEMP}"
)
_TEMP_ONE = re.compile(rf"(?P<num>{_NUM})\s*{_TEMP}")
_HUM_TOL = re.compile(rf"(?P<num>{_NUM})\s*%\s*±\s*(?P<tol>{_NUM})\s*%\s*(?:RH)?")
_HUM_TOL_BARE = re.compile(rf"(?P<num>{_NUM})\s*±\s*(?P<tol>{_NUM})\s*%\s*(?:RH)?")
_HUM_LABEL = re.compile(
    rf"(?:相对)?湿度[^。；;]{{0,12}}?(?P<num>{_NUM})\s*%(?:\s*±\s*(?P<tol>{_NUM})\s*%)?"
)
_HUM_LABEL_RANGE = re.compile(
    rf"(?:相对)?湿度[^。；;]{{0,12}}?(?P<num>{_NUM})\s*%\s*[~～至]\s*(?P<high>{_NUM})\s*%"
)
_HUM_RH = re.compile(rf"(?P<num>{_NUM})\s*%\s*RH")
_RH_HUM = re.compile(rf"RH\s*(?P<num>{_NUM})\s*%")
_TIME = re.compile(
    rf"(?<![A-Za-z0-9.])(?P<num>{_NUM})\s*"
    rf"(?P<unit>小时|分钟|秒|天|周|h|min|s|d)(?![A-Za-z])",
    re.IGNORECASE,
)
_CYCLE = re.compile(rf"(?P<num>{_NUM})\s*(?:次|循环)")
_VOLT = re.compile(
    r"(?<![A-Za-z0-9.])(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>kV|V)(?![A-Za-z])"
)
_VOLT_RANGE = re.compile(r"(?<![A-Za-z0-9.])(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>kV|V)\s*[~～至]\s*(?P<high>-?\d+(?:\.\d+)?)\s*(?:kV|V)")
_CURRENT = re.compile(
    r"(?<![A-Za-z0-9.])(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>mA|A)(?![A-Za-z])"
)
_CURRENT_RANGE = re.compile(r"(?<![A-Za-z0-9.])(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>mA|A)\s*[~～至]\s*(?P<high>-?\d+(?:\.\d+)?)\s*(?:mA|A)")
_SALT = re.compile(rf"(?:盐雾|氯化钠|NaCl)[^。；;]{{0,30}}?(?P<num>{_NUM})\s*(?P<unit>%|g/L|g/l)")
_SALT_REV = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>%|g/L|g/l)[^。；;]{{0,30}}?(?:盐雾|氯化钠|NaCl)")
_SPECS = (
    [(_TEMP_TOL, _tolerance, "℃", "temperature"),
     (_TEMP_TOL_PAREN, _tolerance, "℃", "temperature"),
     (_TEMP_TOL_BARE, _tolerance, "℃", "temperature"),
     (_TEMP_RANGE, _range, "℃", "temperature"),
     (_TEMP_ONE, _single, "℃", "temperature")],
    [(_HUM_TOL, _tolerance, "%", "humidity"),
     (_HUM_TOL_BARE, _tolerance, "%", "humidity"),
     (_HUM_LABEL_RANGE, _range, "%", "humidity"),
     (_HUM_LABEL, _tolerance, "%", "humidity"),
     (_HUM_RH, _single, "%", "humidity"),
     (_RH_HUM, _single, "%", "humidity")],
    [(_TIME, _with_unit, "h", "duration")],
    [(_CYCLE, _single, "次", "cycles")],
    [(_VOLT_RANGE, _range, "V", "voltage"), (_VOLT, _with_unit, "V", "voltage")],
    [(_CURRENT_RANGE, _range, "A", "current"), (_CURRENT, _with_unit, "A", "current")],
    [(_SALT, _with_unit, "%", "salt_fog"), (_SALT_REV, _with_unit, "%", "salt_fog")],
)


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["kind"], entry.get("value"), entry.get("value_max"),
               entry.get("tolerance"), entry.get("unit", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def extract_conditions(text: str) -> list[dict]:
    """Extract explicit conditions from one clause text (no inference)."""
    text = _normalise(text)
    found: list[dict] = []
    for specs in _SPECS:
        found.extend(_collect(text, specs))
    return _dedupe(found)


def extract_item_conditions(item: dict) -> list[dict]:
    """Conditions of one item, from its requirement and method text."""
    item_clause = item.get("clause", "")
    entries: list[dict] = []
    for field, source, clause_key in (
        ("requirement", "requirement", "clause"),
        ("method", "method", "method_clause"),
    ):
        text = item.get(field) or ""
        if not text:
            continue
        clause = item.get(clause_key) or item_clause
        for entry in extract_conditions(text):
            entry.update(clause=clause, item_clause=item_clause, source=source)
            entries.append(entry)
    return _dedupe(entries)


def apply_conditions(items: list[dict]) -> dict:
    """Attach ``conditions``/``conditions_status`` and return summary counts."""
    total = extracted = 0
    for item in items:
        entries = extract_item_conditions(item)
        item["conditions"] = entries
        item["conditions_status"] = "extracted" if entries else "not_extracted"
        total += len(entries)
        extracted += 1 if entries else 0
    return {
        "conditions": total,
        "items_with_conditions": extracted,
        "conditions_not_extracted": len(items) - extracted,
    }
