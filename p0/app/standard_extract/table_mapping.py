"""T2: map table columns to B1 ``conditions`` and B2 ``limits`` entries.

T1 exports ``tables[]`` with the raw ``header``/``rows``; T2 classifies each
column by header keyword (项目/序号 -> identifier, 限值/要求/指标 -> limit,
条件/温度/时间/次数 -> condition) and maps row cells into the B1/B2 shapes.
A limit column gives a bare number/range the meaning a PDF line lacks (the
extra ``value`` kind keeps B2 fields); a condition column plus a ``℃`` header
turns a bare ``-40～125`` into a temperature range.  Cell values stay
distinguishable through ``raw``/``cell_raw``.  Unusable headers and rows are
not guessed: ``mapping_confidence`` is ``low`` and the raw row stays in
``tables[]``.  A unit stated only in a table footnote (``table["footnotes"]``
or a merged note row) is applied when the column has no header unit; ambiguous
or contradicting notes keep the entry ``low`` (see ``table_units``).
Table-mapped entries carry ``source=table``/``table_page``/``table_row``/
``column`` for traceability.
"""
from __future__ import annotations

import re

from .conditions import extract_conditions
from .limits import _UNIT as _LIMIT_UNIT, extract_limits
from .table_units import (
    is_footnote_row,
    normalise_unit,
    resolve_footnote_units,
    unit_compatible,
)

_NUM = r"-?\d+(?:\.\d+)?"
_UNIT = rf"(?:ms|mΩ|{_LIMIT_UNIT})"
_ID_WORDS = ("项目", "序号")
_LIMIT_WORDS = ("限值", "要求", "指标")
_CONDITION_WORDS = ("条件", "温度", "时间", "次数", "循环", "湿度")
_KINDS = (("温度", "temperature"), ("湿度", "humidity"),
          ("次数", "cycles"), ("循环", "cycles"), ("时间", "duration"),
          ("持续", "duration"))
_DEFAULT_UNIT = {"temperature": "℃", "humidity": "%", "cycles": "次", "duration": ""}
_PLACEHOLDER = {"", "-", "—", "–", "/", "√", "×", "无", "不适用", "N/A", "n/a"}
_BARE = re.compile(rf"[（(]?\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?\s*[)）]?")
_RANGE = re.compile(
    rf"[（(]?\s*(?P<low>{_NUM})\s*[~～至]\s*(?P<high>{_NUM})\s*"
    rf"[)）]?\s*(?P<unit>{_UNIT})?"
)

def _num(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value

def _unit(text: str | None) -> str:
    if not text:
        return ""
    if text.lower().startswith("db"):
        return normalise_unit(text)
    return {"°C": "℃", "mΩ": "MΩ"}.get(text, text)

def _header_unit(label: str) -> str:
    """Unit stated once in a column header (``工作温度范围℃``, ``时间 min``)."""
    match = re.search(rf"(?P<unit>{_UNIT})\s*[)）]?\s*$", label or "")
    if not match:
        return ""
    unit = match.group("unit")
    before = (label or "")[:match.start("unit")].rstrip()
    if len(unit) == 1 and unit.isascii() and before[-1:].isalpha():
        return ""
    return _unit(unit)

def _role(label: str) -> tuple[str | None, str]:
    text = label or ""
    if any(word in text for word in _ID_WORDS):
        return "identifier", "high"
    limits = any(word in text for word in _LIMIT_WORDS)
    conditions = any(word in text for word in _CONDITION_WORDS)
    if limits and conditions:
        return None, "low"
    if limits:
        return "limit", "high"
    if conditions:
        return "condition", "high"
    return None, "low"

def _condition_kind(label: str) -> str:
    return next((kind for word, kind in _KINDS if word in (label or "")), "duration")

def _segments(text: str) -> list[str]:
    """Split a cell on top-level separators, keeping parenthesised groups."""
    parts, buffer, depth = [], [], 0
    for index, char in enumerate(text):
        depth += char in "（("
        depth -= char in "）)" and depth > 0
        slash = (char == "/" and 0 < index < len(text) - 1
                 and text[index - 1].isspace() and text[index + 1].isspace())
        if depth == 0 and (char in "；;。\n，" or char == "或" or slash):
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return [part.strip(" \t,，;；。") for part in parts if part.strip(" \t,，;；。")]

def _clean(segment: str) -> str:
    """Drop watermark letters around a value (``c.90 s``, ``60 n``)."""
    segment = re.sub(r"^[A-Za-z]\.", "", segment)
    if not re.search(rf"{_UNIT}\s*$", segment):
        segment = re.sub(r"\s+[A-Za-z]$", "", segment)
    return segment

def _entry(kind: str, unit: str | None, lower=None, upper=None, value=None,
           tolerance=None, raw: str = "") -> dict:
    return {"kind": kind, "lower": lower, "upper": upper, "value": value,
            "tolerance": tolerance, "unit": _unit(unit), "raw": raw.strip()}

def _fix_m_unit(entry: dict, cell: str) -> None:
    """``_UNIT`` knows metre ``m``; recover ms/min/mm/mA/mΩ from the tail."""
    if entry.get("unit") != "m":
        return
    raw = entry.get("raw") or ""
    at = cell.find(raw)
    tail = cell[at + len(raw):] if at >= 0 else ""
    for suffix, fixed in (("in", "min"), ("s", "ms"), ("Ω", "MΩ"),
                          ("m", "mm"), ("A", "mA")):
        if tail.startswith(suffix):
            entry["unit"] = fixed
            return

def _limit_cell(cell: str, hint: str, source: str = "header",
                note: str = "") -> list[dict]:
    """Limits from one limit-column cell; bare values rely on the column."""
    entries: list[dict] = []
    for segment in _segments(cell):
        segment = _clean(segment)
        if not segment or segment in _PLACEHOLDER:
            continue
        found = extract_limits(segment)
        injected = not found and bool(hint)
        if injected:
            found = extract_limits(f"{segment} {hint}")
        if not found:
            match = _RANGE.fullmatch(segment)
            if match:
                entry = _entry("range", match.group("unit") or hint,
                               lower=_num(match.group("low")),
                               upper=_num(match.group("high")), raw=segment)
            else:
                match = _BARE.fullmatch(segment)
                if not match:
                    continue
                entry = _entry("value", match.group("unit") or hint,
                               value=_num(match.group("num")), raw=segment)
            if hint and not match.group("unit"):
                entry["unit_source"] = source
                if note:
                    entry["unit_note"] = note
            entries.append(entry)
            continue
        for entry in found:
            if not entry.get("unit") and hint:
                entry["unit"] = _unit(hint)
                entry["unit_source"] = source
                if note:
                    entry["unit_note"] = note
            elif injected and hint and entry.get("unit") == _unit(hint):
                entry["unit_source"] = source
                if note:
                    entry["unit_note"] = note
            _fix_m_unit(entry, cell)
            entries.append(entry)
    return entries

def _condition_cell(cell: str, hint: str, kind: str, source: str = "header",
                    note: str = "") -> list[dict]:
    """Conditions from one condition-column cell; bare values use the header."""
    found = extract_conditions(cell)
    if found:
        return found
    entries: list[dict] = []
    for segment in _segments(cell):
        segment = _clean(segment)
        if not segment or segment in _PLACEHOLDER:
            continue
        match = _RANGE.fullmatch(segment)
        if match:
            unit = match.group("unit") or hint or _DEFAULT_UNIT[kind]
            if unit:
                entries.append({"kind": kind, "value": _num(match.group("low")),
                                "value_max": _num(match.group("high")), "tolerance": None,
                                "unit": _unit(unit), "raw": segment,
                                "unit_source": source if unit == hint else "default",
                                **({"unit_note": note} if note and unit == hint else {})})
            continue
        match = _BARE.fullmatch(segment)
        if match:
            unit = match.group("unit") or hint or _DEFAULT_UNIT[kind]
            if unit:
                entries.append({"kind": kind, "value": _num(match.group("num")),
                                "value_max": None, "tolerance": None,
                                "unit": _unit(unit), "raw": segment,
                                "unit_source": source if unit == hint else "default",
                                **({"unit_note": note} if note and unit == hint else {})})
    return entries

def _repair_watermark_unit(entry: dict, footnote: dict) -> bool:
    """A lone ``g``/``m`` after a number is a watermark, not a unit."""
    unit = entry.get("unit")
    if (not unit or unit == footnote.get("unit") or len(unit) != 1
            or not unit.isascii() or not unit.isalpha()
            or "除注明外" in footnote.get("note", "")):
        return False
    raw = re.sub(r"\s+", "", entry.get("raw") or "")
    if not re.fullmatch(rf"[≤≥<>]?{_NUM}[A-Za-z]", raw):
        return False
    entry["unit"] = footnote["unit"]
    entry["unit_source"] = "footnote"
    entry["unit_note"] = footnote["note"]
    return True


def _all_notes(table: dict) -> list[str]:
    """Footnotes from T1 page capture plus merged note rows in the grid."""
    notes = list(table.get("footnotes") or [])
    for row in table.get("rows") or []:
        cells = [str(cell or "") for cell in row]
        if is_footnote_row(cells):
            notes.append(next((cell for cell in cells if cell), ""))
    return [note for note in dict.fromkeys(
        str(note).strip() for note in notes if note) if note]


def _map_table(table: dict) -> tuple[dict, list[dict], list[dict]]:
    header = table.get("header") or []
    roles = [_role(label) for label in header]
    columns = [[label[:80], role, conf]
               for label, (role, conf) in zip(header, roles)]
    limit_cols = [index for index, (role, _conf) in enumerate(roles) if role == "limit"]
    condition_cols = [index for index, (role, _conf) in enumerate(roles) if role == "condition"]
    warning = table.get("parse_warning")
    structural = any(marker in (warning or "") for marker in
                     ("未识别到表头", "未定位到所属条款", "条款按邻近条目推断"))
    notes = _all_notes(table)
    resolved = resolve_footnote_units(notes, header, roles)
    note_warning = resolved.get("warning") or ""
    applied_units: dict[str, str] = {}
    for index, item in resolved["units"].items():
        if index >= len(header):
            continue
        role, _conf = roles[index]
        kind = _condition_kind(header[index]) if role == "condition" else ""
        if unit_compatible(header[index], role, kind, item["unit"]):
            applied_units[header[index][:80]] = item["unit"]
    meta = {"columns": columns, "confidence": "none", "mapped_rows": 0,
            "low_confidence_rows": [], "warning": warning, "limits": 0, "conditions": 0,
            "footnotes": notes, "footnote_warning": note_warning or None,
            "footnote_units": applied_units}
    if not header or not (limit_cols or condition_cols):
        meta["warning"] = "表头未识别到限值/条件列" + (f"；{warning}" if warning else "")
        return meta, [], []
    limits: list[dict] = []
    conditions: list[dict] = []
    for row_index, row in enumerate(table.get("rows") or []):
        cells = [str(cell or "") for cell in row]
        if is_footnote_row(cells):
            continue
        row_label = next((cells[index] for index, (role, _conf) in enumerate(roles)
                          if role == "identifier" and cells[index].strip()), "")
        row_label = row_label or (cells[0] if cells else "")
        filled_limits = sum(1 for index in limit_cols if cells[index].strip())
        filled_conditions = any(cells[index].strip() for index in condition_cols)
        if not row_label and not filled_conditions and filled_limits >= 2:
            meta["low_confidence_rows"].append({"row": row_index, "reason": "疑似子表头/参数行，未映射"})
            continue
        row_limits: list[dict] = []
        row_conditions: list[dict] = []
        for index, (role, _conf) in enumerate(roles):
            cell = cells[index].strip() if index < len(cells) else ""
            if not cell or cell in _PLACEHOLDER or role not in {"limit", "condition"}:
                continue
            label = header[index] if index < len(header) else ""
            kind = _condition_kind(label) if role == "condition" else ""
            hint = _header_unit(label)
            source = "header"
            unit_note = ""
            conflict = False
            if hint and role == "condition" and not unit_compatible(
                    label, role, kind, hint):
                note_warning = note_warning or "表头单位与列语义不符，未采用"
                hint, conflict = "", True
            footnote = resolved["units"].get(index)
            if not hint and footnote:
                if unit_compatible(label, role, kind, footnote["unit"]):
                    hint, source = footnote["unit"], "footnote"
                    unit_note = footnote["note"]
                else:
                    note_warning = note_warning or "脚注单位与列语义矛盾，未应用"
                    conflict = True
            low = structural or conflict or (role == "condition" and len(cell) > 80)
            if role == "limit":
                strong = any(word in label for word in ("限值", "指标"))
                repair = footnote if source == "footnote" else None
                for entry in _limit_cell(cell, hint, source, unit_note):
                    if repair:
                        _repair_watermark_unit(entry, repair)
                    entry.update(column=label[:80], cell_raw=cell, table_role="limit",
                                 mapping_confidence="low" if low or not entry["unit"]
                                 or (entry["kind"] == "value" and not strong) else "high")
                    row_limits.append(entry)
            else:
                for entry in _condition_cell(cell, hint, kind, source, unit_note):
                    entry.update(column=label[:80], cell_raw=cell, table_role="condition",
                                 source="table", mapping_confidence="low" if low else "high")
                    row_conditions.append(entry)
        for entry in row_limits + row_conditions:
            entry.update(table_page=table.get("page"), table_row=row_index,
                         table_clause=table.get("clause") or "", source="table")
        if row_limits or row_conditions:
            meta["mapped_rows"] += 1
            limits.extend(row_limits)
            conditions.extend(row_conditions)
            if any(e["mapping_confidence"] == "low" for e in row_limits + row_conditions):
                meta["low_confidence_rows"].append({"row": row_index, "reason": "含低置信度映射"})
        else:
            meta["low_confidence_rows"].append({"row": row_index, "reason": "未识别出限值/条件"})
    meta["limits"], meta["conditions"] = len(limits), len(conditions)
    high = any(entry["mapping_confidence"] == "high" for entry in limits + conditions)
    meta["confidence"] = "high" if high and not structural else (
        "low" if limits or conditions else "none")
    if note_warning:
        meta["warning"] = ((warning + "；") if warning else "") + note_warning
        meta["footnote_warning"] = note_warning
    return meta, limits, conditions

def _owner(items_by_clause: dict, items: list[dict], clause: str) -> dict | None:
    if not clause:
        return None
    if clause in items_by_clause:
        return items_by_clause[clause]
    candidates = [item for item in items
                  if item.get("clause") and clause.startswith(item["clause"] + ".")]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["clause"].count("."))

def apply_table_mapping(items: list[dict], tables: list[dict]) -> dict:
    """Attach mapped table rows to items; return the T2 summary and totals."""
    by_clause = {item["clause"]: item for item in items if item.get("clause")}
    counts = {"limits": 0, "low_limits": 0, "conditions": 0, "low_conditions": 0,
              "tables": 0, "low_tables": 0, "rows": 0, "low_rows": 0,
              "footnote_limits": 0, "footnote_conditions": 0}
    for table in tables:
        meta, limits, conditions = _map_table(table)
        table["mapping"] = meta
        if not limits and not conditions:
            counts["low_tables"] += 1
            continue
        owner = _owner(by_clause, items, table.get("clause") or "")
        if owner is None:
            meta["confidence"] = "low"
            meta["warning"] = ((meta.get("warning") or "") + "；未定位到所属条目，未映射").lstrip("；")
            counts["low_tables"] += 1
            continue
        for entry in limits + conditions:
            entry["clause"] = owner["clause"]
            entry["item_clause"] = owner["clause"]
        owner.setdefault("limits", []).extend(limits)
        owner.setdefault("conditions", []).extend(conditions)
        if limits:
            owner["limits_status"] = "extracted"
        if conditions:
            owner["conditions_status"] = "extracted"
        counts["limits"] += len(limits)
        counts["low_limits"] += sum(1 for e in limits if e["mapping_confidence"] == "low")
        counts["conditions"] += len(conditions)
        counts["low_conditions"] += sum(1 for e in conditions if e["mapping_confidence"] == "low")
        counts["footnote_limits"] += sum(1 for e in limits if e.get("unit_source") == "footnote")
        counts["footnote_conditions"] += sum(
            1 for e in conditions if e.get("unit_source") == "footnote")
        counts["tables"] += 1
        counts["rows"] += meta["mapped_rows"]
        counts["low_rows"] += len(meta["low_confidence_rows"])
    total_limits = sum(len(item.get("limits") or []) for item in items)
    total_conditions = sum(len(item.get("conditions") or []) for item in items)
    with_limits = sum(1 for item in items if item.get("limits"))
    with_conditions = sum(1 for item in items if item.get("conditions"))
    return {
        "limits": total_limits, "items_with_limits": with_limits,
        "limits_not_extracted": len(items) - with_limits,
        "conditions": total_conditions, "items_with_conditions": with_conditions,
        "conditions_not_extracted": len(items) - with_conditions,
        "table_limits": counts["limits"],
        "table_limits_low_confidence": counts["low_limits"],
        "table_limits_footnote_unit": counts["footnote_limits"],
        "table_conditions": counts["conditions"],
        "table_conditions_low_confidence": counts["low_conditions"],
        "table_conditions_footnote_unit": counts["footnote_conditions"],
        "tables_mapped": counts["tables"], "tables_mapping_low": counts["low_tables"],
        "table_rows_mapped": counts["rows"],
        "table_rows_low_confidence": counts["low_rows"],
    }
