"""C: recover table units stated in a footnote.

Some standards put the limit unit in a note instead of the table header
(``注：限值单位为dB(μV)``).  This module finds those notes, extracts a unit
only from an explicit "单位…为/是/：X" statement, and associates it with the
columns that miss a header unit.  Footnotes with several different units are
mapped by the column keyword in front of the statement; if that is impossible
the table keeps ``mapping_confidence=low`` instead of guessing.  A note that
is far below the table or points at another semantic (``温度`` column with
``dB``) is ignored with a warning.
"""
from __future__ import annotations

import re

from .limits import _UNIT as _LIMIT_UNIT

#: A note is only considered this close below the table bbox (points).
NOTE_GAP = 72.0
#: A note row is one merged cell (the rest empty) containing one of these.
_NOTE_ROW = re.compile(r"注|备注|限值单位|单位均|除注明外")
_NOTE_TEXT = re.compile(
    r"(?:^|[\s：:])(?:注|备注)\s*(?:\d|[:：])?|限值单位|单位均|"
    r"单位\s*(?:均)?\s*(?:为|是|：|:)"
)
#: ``dB(μV)`` sometimes arrives as ``dTB( O μV)`` after watermark overprint.
_DB_REPAIR = re.compile(
    r"d\s*[A-Za-z]?\s*B\s*[（(]\s*(?:[A-Za-z]\s*)?([μµuU])\s*([VA])\s*"
    r"(?:/\s*(m))?\s*[)）]"
)
_STATEMENT = re.compile(
    rf"(?P<prefix>[^。；;\n]{{0,24}}?)单位\s*(?:均)?\s*"
    rf"(?:为|是|：|:)?\s*(?P<unit>{_LIMIT_UNIT})"
)
_MULTI = re.compile(
    rf"(?:、|，|,|/|或|和|及)\s*(?P<unit>{_LIMIT_UNIT})"
)
_COLUMN_WORDS = (
    "峰值", "平均值", "准峰值", "电场", "磁场", "电流", "电压", "电阻",
    "温度", "湿度", "时间", "频率", "长度", "质量", "力矩",
)
_FREQ_UNITS = {"Hz", "kHz", "MHz", "GHz"}
_ALLOWED = {
    "temperature": {"℃"},
    "humidity": {"%"},
    "cycles": {"次"},
    "duration": {"s", "ms", "min", "h", "d", "秒", "分钟", "小时", "天"},
}


def normalise_unit(unit: str) -> str:
    """Normalise a footnote/header unit for comparison and output."""
    text = (unit or "").strip().replace("（", "(").replace("）", ")")
    text = text.replace("µ", "μ").replace("°C", "℃")
    if text.lower().startswith("db"):
        text = re.sub(r"\s+", "", text)
        match = re.fullmatch(r"dB\(([μu])([VA])(?:/(m))?\)", text, re.IGNORECASE)
        if match:
            micro, base, metre = match.groups()
            return (f"dB({micro.replace('u', 'μ')}{base.upper()}"
                    f"{'/' + metre if metre else ''})")
        if text.lower() in {"db", "db(a)"}:
            return "dB" if text.lower() == "db" else "dB(A)"
    return {"N.m": "N·m", "Nm": "N·m", "Mpa": "MPa", "MPA": "MPa"}.get(text, text)


def repair_watermark(text: str) -> str:
    """Put ``dB(μV)`` back together when watermark letters split it."""
    text = (text or "").replace("（", "(").replace("）", ")")
    # ``dB(μV)/m`` is the same unit as ``dB(μV/m)``.
    text = re.sub(r"dB\s*\(([^)]*?)\)\s*/\s*m\b", r"dB(\1/m)", text,
                  flags=re.IGNORECASE)

    def fix(match: re.Match) -> str:
        return (f"dB({match.group(1).replace('u', 'μ').replace('U', 'μ')}"
                f"{match.group(2).upper()}{'/' + match.group(3) if match.group(3) else ''})")

    text = _DB_REPAIR.sub(fix, text)
    return re.sub(r"\s+", " ", text)


def is_footnote_row(cells) -> bool:
    """A merged footnote row: one note text, possibly repeated by extraction."""
    values = [str(cell or "").strip() for cell in cells]
    nonempty = [value for value in values if value]
    if not nonempty or len(nonempty[0]) < 5:
        return False
    if len(nonempty) > 1 and len(set(nonempty)) > 1:
        return False
    return bool(_NOTE_ROW.search(nonempty[0]))


def is_note_text(text: str) -> bool:
    """A page line that is a table note rather than body text."""
    text = (text or "").strip()
    return 5 <= len(text) <= 240 and bool(_NOTE_TEXT.search(text))


def unit_statements(notes) -> list[dict]:
    """Extract explicit unit statements from notes, with their context."""
    statements: list[dict] = []
    for note in notes or []:
        text = repair_watermark(str(note or ""))
        for match in _STATEMENT.finditer(text):
            unit = normalise_unit(match.group("unit"))
            if not unit:
                continue
            prefix = match.group("prefix")
            kind, hints = _classify(prefix, unit)
            statements.append({"unit": unit, "kind": kind, "hints": hints,
                               "note": text[:240], "prefix": prefix})
            rest = text[match.end():match.end() + 40]
            rest = re.split(r"[。；;\n]|单位", rest)[0]
            for extra in _MULTI.finditer(rest):
                other = normalise_unit(extra.group("unit"))
                if other and other != unit:
                    statements.append({"unit": other, "kind": kind, "hints": hints,
                                       "note": text[:240], "prefix": prefix})
    unique: dict[tuple, dict] = {}
    for statement in statements:
        key = (statement["unit"], statement["kind"], tuple(statement["hints"]))
        unique.setdefault(key, statement)
    return list(unique.values())


def _classify(prefix: str, unit: str) -> tuple[str, list[str]]:
    hints = [word for word in _COLUMN_WORDS if word in prefix]
    if hints:
        return "column", hints
    if unit in _FREQ_UNITS or re.search(r"(?:^|\W)[Ff]\s*$", prefix):
        return "frequency", []
    return "limit", []


def unit_compatible(label: str, role: str, kind: str, unit: str) -> bool:
    """Reject a footnote unit that contradicts the column semantics."""
    if role == "condition":
        return unit in _ALLOWED.get(kind, set())
    text = label or ""
    if any(word in text for word in ("温度", "℃")) and unit != "℃":
        return False
    if any(word in text for word in ("频率", "Hz")) and unit not in _FREQ_UNITS:
        return False
    if any(word in text for word in ("湿度", "RH")) and unit != "%":
        return False
    return True


def resolve_footnote_units(notes, header: list[str],
                           roles: list[tuple[str | None, str]]) -> dict:
    """Return ``{column_index: {unit, note}}`` and any ambiguity warning."""
    notes = [str(note) for note in notes or [] if note]
    statements = unit_statements(notes)
    usable = [item for item in statements if item["kind"] != "frequency"]
    result: dict = {"units": {}, "notes": notes, "warning": ""}
    if not usable:
        return result
    columns = [index for index, (role, _conf) in enumerate(roles)
               if role in {"limit", "condition"} and index < len(header)]
    generic = [item for item in usable if not item["hints"]]
    if len(usable) > 1 and generic:
        result["warning"] = "脚注含多个单位，无法对应到列，未应用"
        return result

    mapping: dict[int, dict] = {}
    for statement in usable:
        if statement["hints"]:
            matches = [index for index in columns
                       if any(word in header[index] for word in statement["hints"])]
            if not matches:
                if len(usable) > 1:
                    result["warning"] = "脚注单位无法对应到列，未应用"
                    return result
                continue
        else:
            matches = columns
        for index in matches:
            current = mapping.get(index)
            if current and current["unit"] != statement["unit"]:
                result["warning"] = "脚注单位与列对应冲突，未应用"
                return result
            mapping[index] = {"unit": statement["unit"], "note": statement["note"]}
    result["units"] = mapping
    return result
