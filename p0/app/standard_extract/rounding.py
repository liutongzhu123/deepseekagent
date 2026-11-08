"""B5: numeric rounding rules in standard clause text.

Recognised kinds (explicit only): ``significant_digits``, ``decimal_places``
and ``interval``; a stated ``四舍六入五单双`` / ``四舍五入`` method becomes a
``method`` entry attached to the clause's precision rules.  Relative limits
are not rounding.  Items gain ``roundings``/``rounding_status``; a
document-level rule is shared by every item with ``scope="standard"``.
"""
from __future__ import annotations

import re

_NUM = r"\d+(?:\.\d+)?"
_NUM_WORD = rf"(?:{_NUM}|[一二两三四五六七八九十]+)"
_UNIT = r"(?:%|mm|cm|m|g|mg|kg|mL|ml|L|l|℃|°C|s|min|h)"
_CLAUSE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S")
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_PRECISION = {
    "significant_digits": (
        re.compile(rf"(?:保留|取|修约(?:至|到)|报告(?:至|到))?\s*(?P<num>{_NUM_WORD})\s*位\s*有效数字"),
        re.compile(rf"有效数字\s*(?:位数)?\s*(?:为|是|：|:)?\s*(?P<num>{_NUM_WORD})\s*位"),
        re.compile(rf"有效数字\s*(?:保留|取)\s*(?P<num>{_NUM_WORD})\s*位"),
    ),
    "decimal_places": (
        re.compile(rf"(?:保留|取|修约(?:至|到)|报告(?:至|到))?\s*(?P<num>{_NUM_WORD})\s*位\s*小数"),
        re.compile(rf"(?:精确|修约|保留)(?:至|到)\s*小数点后\s*(?P<num>{_NUM_WORD})\s*位"),
        re.compile(rf"小数(?:点后|位数)?\s*(?:保留|取|为|是|：|:)?\s*(?P<num>{_NUM_WORD})\s*位"),
    ),
}
_INTERVAL = (
    re.compile(rf"修约间隔\s*(?:为|是|：|:)?\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?"),
    re.compile(rf"(?:以|按)\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?\s*(?:为|作为)\s*(?:修约)?间隔"),
    re.compile(rf"(?:按|以)\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?\s*(?:的)?\s*(?:修约间隔|间隔)?\s*修约"),
    re.compile(rf"(?:修约|精确)(?:至|到)\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?"),
)
_METHODS = (
    ("round_half_even",
     re.compile(r"四舍六入五(?:单双|成双|留双|考虑)|四舍六入|奇进偶舍|银行家舍入")),
    ("round_half_up", re.compile(r"四舍五入")),
)


def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _entry(kind: str, clause: str, item_clause: str, raw: str, *,
           value=None, unit: str = "", method=None) -> dict:
    return {"kind": kind, "value": value, "unit": unit, "method": method,
            "clause": clause, "item_clause": item_clause, "raw": raw.strip()}


def extract_rounding(
    text: str, clause: str = "", item_clause: str = ""
) -> list[dict]:
    """Extract explicit rounding expressions from one clause's text."""
    text = (text or "").replace("−", "-").replace("－", "-")
    text = text.replace("\u3000", " ").replace("％", "%")
    methods: list[tuple[str, str]] = []
    for code, regex in _METHODS:
        for match in regex.finditer(text):
            if (code, match.group(0)) not in methods:
                methods.append((code, match.group(0)))
    primary = methods[0][0] if methods else None
    found: list[dict] = []

    def add(kind: str, value, unit: str, raw: str) -> None:
        found.append(_entry(kind, clause, item_clause, raw,
                            value=value, unit=unit, method=primary))

    for kind, patterns in _PRECISION.items():
        def make(match: re.Match, kind: str = kind) -> str:
            num = match.group("num")
            count = int(num) if num.isdigit() else _CN.get(num)
            if count is not None:
                add(kind, count, "位", match.group(0))
            return " " * (match.end() - match.start())

        for regex in patterns:
            text = regex.sub(make, text)

    for regex in _INTERVAL:
        def make_interval(match: re.Match) -> str:
            value = _num(match.group("num"))
            unit = (match.groupdict().get("unit") or "").replace("°C", "℃")
            add("interval", value, unit, match.group(0))
            return " " * (match.end() - match.start())

        text = regex.sub(make_interval, text)

    for code, label in methods:
        found.append(_entry("method", clause, item_clause, label, method=code))
    return _dedupe(found)


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["kind"], entry["value"], entry["unit"], entry["method"],
               entry["clause"], entry["raw"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def extract_roundings(
    lines: list[tuple[str, int]], items: list[dict]
) -> list[dict]:
    """Scan cleaned ``(text, page)`` lines with clause tracking."""
    owners = {
        key: item["clause"]
        for item in items
        for key in (item.get("clause"), item.get("method_clause"))
        if key
    }

    def owner(clause: str) -> str:
        if clause in owners:
            return owners[clause]
        return next((item_clause for key, item_clause in owners.items()
                     if clause.startswith(key + ".")), "")

    entries: list[dict] = []
    clause, buffer = "", ""

    def flush() -> None:
        if buffer.strip():
            entries.extend(extract_rounding(buffer, clause, owner(clause)))

    for text, _page in lines:
        line = text.strip()
        if "...." in line:  # table-of-contents dot leaders are not clauses
            continue
        match = _CLAUSE.match(line)
        if match:
            flush()
            clause, buffer = match.group(1), line
        else:
            buffer += line
        if re.search(r"[。；;]$", line):
            flush()
            buffer = ""
    flush()
    return _dedupe(entries)


def apply_rounding(
    items: list[dict], lines: list[tuple[str, int]]
) -> tuple[list[dict], dict]:
    """Return ``(roundings, summary)`` and attach per-item rules/status."""
    entries = extract_roundings(lines, items)
    direct: dict[str, list[dict]] = {}
    standard: list[dict] = []
    for entry in entries:
        entry["scope"] = "item" if entry["item_clause"] else "standard"
        if entry["scope"] == "item":
            direct.setdefault(entry["item_clause"], []).append(entry)
        else:
            standard.append(entry)

    # Document-level precision rules apply to every item; a bare method
    # statement stays in ``roundings[]`` without claiming items.
    shared = [entry for entry in standard if entry["kind"] != "method"]
    for item in items:
        rules = _dedupe([dict(entry) for entry in
                         direct.get(item["clause"], []) + shared])
        item["roundings"] = rules
        item["rounding_status"] = "extracted" if rules else "not_extracted"

    kinds = ("significant_digits", "decimal_places", "interval", "method")
    counts = {kind: sum(1 for entry in entries if entry["kind"] == kind) for kind in kinds}
    return entries, {
        "roundings": len(entries),
        "items_with_rounding": sum(1 for item in items if item["roundings"]),
        "rounding_not_extracted": sum(1 for item in items if not item["roundings"]),
        "rounding_significant_digits": counts["significant_digits"],
        "rounding_decimal_places": counts["decimal_places"],
        "rounding_intervals": counts["interval"],
        "rounding_methods": counts["method"],
    }
