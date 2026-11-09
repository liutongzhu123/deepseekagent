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

import hashlib
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
# "A 或 B" joins alternative dimensions; matching one is a full match.
_ALT_SPLIT = re.compile(r"或\s*者?")


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
    for chunk in re.split(r"[；;。]", line):
        segments = _ALT_SPLIT.split(chunk)
        alternative = ""
        if len(segments) > 1:
            digest = hashlib.sha1(chunk.strip().encode("utf-8")).hexdigest()[:10]
            alternative = f"{clause}|{digest}"
        for segment in segments:
            entries.extend(_segment_size_entries(
                segment, clause, item_clause, alternative))
    return entries


def _segment_size_entries(
    line: str, clause: str, item_clause: str, alternative: str
) -> list[dict]:
    entries: list[dict] = []
    work = line
    for match in _DIM.finditer(line):
        raw = match.group(0).strip()
        if not re.search(r"mm|毫米", raw):
            continue
        entries.append(_size_entry(clause, item_clause, raw, raw, "mm",
                                   alternative))
        work = work[:match.start()] + " " * (match.end() - match.start()) + work[match.end():]
    for regex in (_SIZE_LABEL, _DIAMETER, _MEASURE):
        for match in regex.finditer(work):
            raw = match.group(0).strip()
            entries.append(_size_entry(clause, item_clause, raw, raw,
                                       match.group(2), alternative))
    return entries


def _size_entry(clause, item_clause, raw, text, unit, alternative):
    entry = _entry("size", clause, item_clause, raw, text=text, unit=unit)
    if alternative:
        # Dimensions joined by 或 are alternatives: matching one is enough.
        entry["alternative"] = alternative
    return entry


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


# SP-8.2 sample-preparation guide + commission-order (委托单) cross-check.
# Narrowed scope (只判数量与编号): compare per-item quantity only, matching order
# items by clause number (编号) then name; size/pretreatment/unowned -> skipped.

_KIND_ORDER = {"quantity": 0, "size": 1, "pretreatment": 2}
_COUNT_UNITS = {"件", "个", "只", "支", "根", "片", "块", "份", "台"}
_SET_UNITS = {"套", "组"}
_SIZE_SKIP_REASON = (
    "尺寸规格的“或/且”语义无法机械判定（#206：标准 100mmx100mm 或 30mmx30mm，"
    "委托单 50mm×100mm 曾被误判通过）→ skip")
_PRETREATMENT_SKIP_REASON = "收窄范围：只判数量与编号，预处理不参与机械比对 → skip"
_UNASSIGNED_SKIP_REASON = {
    "quantity": "未归属的数量要求无法确定对应的委托单分项，不能与委托单总量机械比对 → skip",
    "size": _SIZE_SKIP_REASON + "；且该尺寸要求未归属到具体项目",
    "pretreatment": "未归属的预处理要求无法确定对应的委托单分项 → skip"}

_kind = lambda entry: _KIND_ORDER.get(entry.get("kind"), 9)


def _step(entry: dict) -> str:
    if entry.get("kind") == "quantity":
        top = entry.get("value_max")
        amount = f"{entry['value']}～{top}" if top not in (None, entry["value"]) else f"{entry['value']}"
        return f"准备 {amount}{entry.get('unit') or ''}样品"
    label = "试样尺寸：" if entry.get("kind") == "size" else "预处理："
    return label + str(entry.get("text") or entry.get("raw") or "")


def build_sample_guide(samples: list[dict], items: list[dict]) -> dict:
    """Build a per-item preparation guide from document-level ``samples[]``."""
    grouped: dict[str, list[dict]] = {}
    unassigned: list[dict] = []
    for entry in samples or []:
        clause = str(entry.get("item_clause") or "").strip()
        if clause:
            grouped.setdefault(clause, []).append(entry)
        else:
            unassigned.append(entry)
    guide = []
    for item in items or []:
        clause = str(item.get("clause") or "").strip()
        requirements = sorted(grouped.pop(clause, []), key=_kind)
        guide.append({
            "item_clause": clause, "item_name": str(item.get("name") or item.get("item_name") or ""),
            "requirements": requirements, "steps": [_step(entry) for entry in requirements],
            "status": "ready" if requirements else "not_extracted",
        })
    # Requirements whose owning item was removed by a review edit must survive.
    leftovers = unassigned + [entry for entries in grouped.values() for entry in entries]
    leftovers.sort(key=_kind)
    return {
        "items": guide,
        "unassigned": {"requirements": leftovers, "steps": [_step(entry) for entry in leftovers]},
        "summary": {
            "items": len(guide), "items_with_requirements": sum(1 for item in guide if item["requirements"]),
            "requirements": len(samples or []), "unassigned_requirements": len(leftovers),
        },
    }


def _qty(value) -> tuple[float | int | None, str]:
    if value is None or isinstance(value, bool):
        return None, ""
    if isinstance(value, (int, float)):
        return _num(value), ""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value))
    unit = re.search(_QTY_UNIT, str(value))
    return (_num(numbers[0]) if len(numbers) == 1 else None), (unit.group(0) if unit else "")


def _unit_ok(left: str, right: str) -> bool:
    if not left or not right:
        return True
    group = lambda unit: "set" if unit in _SET_UNITS else "count" if unit in _COUNT_UNITS else unit
    return group(left) == group(right)


def _norm(value) -> str:
    return re.sub(r"[\s（）()、，,。.;；:：\-—_]+", "", str(value or "")).lower()


def _match(orders: list[dict], clause: str, name: str) -> dict | None:
    for item in orders:
        if clause and str(item.get("clause") or "").strip() == clause:
            return item
    wanted = _norm(name)
    return next((item for item in orders if wanted and _norm(item.get("name")) == wanted), None)


def _order_qty(order: dict, item: dict | None, item_count: int) -> tuple[float | int | None, str]:
    if item is not None and item.get("quantity") is not None:
        value, unit = _qty(item["quantity"])
        return value, unit or str(item.get("unit") or "")
    if item_count == 1 and order.get("quantity") is not None:
        value, unit = _qty(order["quantity"])
        return value, unit or str(order.get("unit") or "")
    return None, ""


def _bad(code: str, item: dict, message: str) -> dict:
    return {"code": code, "item_clause": item.get("item_clause", ""), "item_name": item.get("item_name", ""), "message": message}


def _skip(scope: str, code: str, item: dict, reason: str, raw) -> dict:
    return {"scope": scope, "code": code, "item_clause": item.get("item_clause", ""),
            "item_name": item.get("item_name", ""), "reason": reason,
            "raw": str(raw or "")}


def _check_requirements(
    item: dict,
    requirements: list[dict],
    actual: float | int | None,
    actual_unit: str,
    checked: dict,
    mismatches: list[dict],
    skipped: list[dict],
) -> None:
    quantity = next((
        entry for entry in requirements
        if entry.get("kind") == "quantity" and entry.get("value") is not None
    ), None)
    if quantity is not None:
        unit = str(quantity.get("unit") or "")
        top = quantity.get("value_max")
        expected = (
            f"{quantity['value']}～{top}{unit}"
            if top not in (None, quantity["value"])
            else f"{quantity['value']}{unit}"
        )
        if actual is not None and not _unit_ok(unit, actual_unit):
            mismatches.append(_bad(
                "unit_mismatch", item,
                f"委托单单位 {actual_unit or '空'} 与标准 {expected} 不一致",
            ))
        elif actual is not None:
            checked["quantity"] += 1
            if float(actual) < float(quantity["value"]):
                mismatches.append(_bad(
                    "quantity_short", item,
                    f"委托单样品数量 {actual}{actual_unit} 少于标准要求 {expected}",
                ))
    for entry in requirements:
        if entry.get("kind") == "size":
            skipped.append(_skip(
                "size", "size_not_judged", item, _SIZE_SKIP_REASON,
                entry.get("text") or entry.get("raw"),
            ))
        elif entry.get("kind") == "pretreatment":
            skipped.append(_skip(
                "pretreatment", "pretreatment_not_judged", item,
                _PRETREATMENT_SKIP_REASON,
                entry.get("text") or entry.get("raw"),
            ))


def check_sample_order(guide: dict, order: dict | None) -> dict:
    """Compare mechanically comparable commission-order fields with the guide.

    Only per-item quantity is judged: order items are matched to standard items
    by clause number (编号) first, then by item name.  Size, pretreatment and
    requirements without an owning item are always skipped (their semantics are
    not mechanically decidable here).  ``skipped`` carries the reason for every
    such decision.
    """
    order = order or {}
    orders = [item for item in (order.get("items") or []) if isinstance(item, dict)]
    items = [item for item in guide.get("items", []) if item.get("requirements")]
    mismatches: list[dict] = []
    skipped: list[dict] = []
    checked = {"quantity": 0, "size": 0, "pretreatment": 0}
    for item in items:
        order_item = _match(orders, item["item_clause"], item["item_name"])
        actual, actual_unit = _order_qty(order, order_item, len(items))
        _check_requirements(
            item, item["requirements"], actual, actual_unit,
            checked, mismatches, skipped,
        )
    unassigned_item = {"item_clause": "", "item_name": "未归属要求"}
    for entry in (guide.get("unassigned") or {}).get("requirements", []):
        if isinstance(entry, dict):
            reason = _UNASSIGNED_SKIP_REASON.get(
                entry.get("kind"), _UNASSIGNED_SKIP_REASON["pretreatment"])
            skipped.append(_skip(
                "unassigned", "unassigned_not_judged", unassigned_item,
                reason, entry.get("raw") or entry.get("text"),
            ))
    sample_nos = [v for v in [order.get("sample_no")] + [i.get("sample_no") for i in orders] if str(v or "").strip()]
    if sample_nos: skipped.append(_skip("numbering", "sample_no_not_judged", {"item_clause": "", "item_name": "样品编号"}, "标准未声明样品编号，委托单编号没有对照值 → skip", "、".join(map(str, sample_nos))))
    return {"ok": not mismatches, "mismatches": mismatches,
            "checked": checked, "skipped": skipped}
