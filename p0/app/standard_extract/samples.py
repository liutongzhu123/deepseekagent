"""B3: sample quantity, dimensions and pretreatment from standard text.

Clause-level lines are joined and tracked, so requirements in the inspection
chapter stay traceable.  Three explicit kinds are reported, never inferred::

    {"kind": "quantity", "value": 2, "value_max": 3, "unit": "套",
     "text": None, "conditions": None, "clause": "7.2.2",
     "item_clause": "", "raw": "每组数量（2～3）套"}
    {"kind": "size", "text": "100mm×50mm×2mm", ...}
    {"kind": "pretreatment", "text": "室温下放置 12h", "conditions": [...]}

The result is document-level ``samples[]``; owning items also get ``samples`` and ``samples_status``.
"""
from __future__ import annotations

import re

from .conditions import extract_conditions

_QTY_UNIT = r"(?:件|个|套|组|只|台|根|片|块|支|份)"
_QTY_RANGE = re.compile(
    rf"数量\s*[（(]?\s*(\d+(?:\.\d+)?)\s*[~～至\-]\s*(\d+(?:\.\d+)?)\s*[)）]?\s*({_QTY_UNIT})"
)
_QTY_AFTER = re.compile(
    rf"(\d+(?:\.\d+)?)\s*({_QTY_UNIT})\s*(?:的)?\s*(?:样品|试样|试件|样本)"
)
_QTY_BEFORE = re.compile(
    rf"(?:样品|试样|试件|样本)\s*(?:数量)?\s*[:：为]?\s*(\d+(?:\.\d+)?)\s*({_QTY_UNIT})"
)
_GROUPS = re.compile(r"(?:均?分成|分为|分成)\s*(\d+(?:\.\d+)?)\s*组")

_DIM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mm|毫米)?\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?"
    r"(?:\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?)?"
)
_SIZE_LABEL = re.compile(r"(?:尺寸|规格)[^。；;]{0,8}?(\d+(?:\.\d+)?)\s*(mm|毫米)")
_DIAMETER = re.compile(r"(?:直径|[φΦ])\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(mm|毫米)")
_MEASURE = re.compile(r"(?:厚度|宽度|长度|高度)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(mm|毫米)")

_PREP_KEY = re.compile(r"(?:预处理|状态调节|试验前|静置|放置|恒温|恒湿)")
_ROOM = re.compile(r"室温|恒温|恒湿")
_CLAUSE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S")


def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _entry(kind: str, clause: str, item_clause: str, raw: str, **values) -> dict:
    return {
        "kind": kind,
        "value": values.get("value"),
        "value_max": values.get("value_max"),
        "unit": values.get("unit", ""),
        "text": values.get("text"),
        "conditions": values.get("conditions"),
        "clause": clause,
        "item_clause": item_clause,
        "raw": raw,
    }


def _quantity_entries(line: str, clause: str, item_clause: str) -> list[dict]:
    entries: list[dict] = []
    for match in _QTY_RANGE.finditer(line):
        entries.append(_entry("quantity", clause, item_clause, match.group(0),
                              value=_num(match.group(1)), value_max=_num(match.group(2)),
                              unit=match.group(3)))
    for match in _GROUPS.finditer(line):
        entries.append(_entry("quantity", clause, item_clause, match.group(0),
                              value=_num(match.group(1)), unit="组"))
    for regex in (_QTY_AFTER, _QTY_BEFORE):
        for match in regex.finditer(line):
            entries.append(_entry("quantity", clause, item_clause, match.group(0),
                                  value=_num(match.group(1)), unit=match.group(2)))
    return entries


def _size_entries(line: str, clause: str, item_clause: str) -> list[dict]:
    entries: list[dict] = []
    work = line
    for match in _DIM.finditer(line):
        raw = match.group(0).strip()
        if not re.search(r"mm|毫米", raw):
            continue
        entries.append(_entry("size", clause, item_clause, raw, text=raw, unit="mm"))
        work = work[:match.start()] + " " * (match.end() - match.start()) + work[match.end():]
    for regex in (_SIZE_LABEL, _DIAMETER, _MEASURE):
        for match in regex.finditer(work):
            raw = match.group(0).strip()
            entries.append(_entry("size", clause, item_clause, raw, text=raw, unit=match.group(2)))
    return entries


def _pretreatment_entry(line: str, clause: str, item_clause: str) -> dict | None:
    if not _PREP_KEY.search(line):
        return None
    conditions = extract_conditions(line)
    if not conditions:
        return None
    if not (_ROOM.search(line) or any(
        item["kind"] in ("temperature", "humidity") for item in conditions
    )):
        return None
    for condition in conditions:
        condition.update(clause=clause, item_clause=item_clause, source="sample")
    tail = re.split(r"[a-z]\)", line)[-1]
    text = next(
        (part.strip() for part in re.split(r"[。；;]", tail) if _PREP_KEY.search(part)),
        tail.strip(),
    )
    return _entry(
        "pretreatment", clause, item_clause, line.strip(),
        text=text, conditions=conditions,
    )


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["kind"], entry["value"], entry["value_max"],
               entry["unit"], entry["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def extract_samples(lines: list[tuple[str, int]], items: list[dict]) -> list[dict]:
    """Extract ``samples[]`` from cleaned lines ``(text, page)`` in order."""
    owners: dict[str, str] = {}
    for item in items:
        for key in (item.get("clause"), item.get("method_clause")):
            if key:
                owners[key] = item["clause"]

    def owner(clause: str) -> str:
        if clause in owners:
            return owners[clause]
        for key, item_clause in owners.items():
            if clause.startswith(key + "."):
                return item_clause
        return ""

    entries: list[dict] = []
    clause, buffer = "", ""

    def flush() -> None:
        item_clause = owner(clause)
        entries.extend(_quantity_entries(buffer, clause, item_clause))
        entries.extend(_size_entries(buffer, clause, item_clause))
        pretreatment = _pretreatment_entry(buffer, clause, item_clause)
        if pretreatment is not None:
            entries.append(pretreatment)

    for text, _page in lines:
        line = text.strip()
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


def apply_samples(items: list[dict], lines: list[tuple[str, int]]) -> tuple[list[dict], dict]:
    """Return ``(samples, summary)`` and attach per-item ``samples``/status."""
    entries = extract_samples(lines, items)
    by_item: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["item_clause"]:
            by_item.setdefault(entry["item_clause"], []).append(entry)
    for item in items:
        item["samples"] = by_item.get(item["clause"], [])
        item["samples_status"] = "extracted" if item["samples"] else "not_extracted"
    counts = {
        kind: sum(1 for entry in entries if entry["kind"] == kind)
        for kind in ("quantity", "size", "pretreatment")
    }
    summary = {
        "samples": len(entries),
        "items_with_samples": sum(1 for item in items if item["samples"]),
        "samples_not_extracted": sum(1 for item in items if not item["samples"]),
        "sample_quantities": counts["quantity"],
        "sample_sizes": counts["size"],
        "sample_pretreatments": counts["pretreatment"],
    }
    return entries, summary
