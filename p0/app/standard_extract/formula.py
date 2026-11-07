"""B4: formula recognition in standard clause text.

Kinds: arithmetic mean, weighted mean, range (max-min), standard deviation and
percentage/change rate.  A type keyword only counts in a calculation sentence,
so prose such as "平均车速" or "平均值检波器" is not a formula.  The scanner
walks the whole document with clause tracking and joins wrapped lines.

Text that looks like a formula but cannot be typed ("按式（3）计算") is kept
with ``status="not_extracted"``; no formula is invented.  Each item gains
``formulas``/``formula_status``; the result gets ``formulas[]``::

    {"kind": "mean", "status": "extracted", "variables": [...],
     "clause": "6.5", "item_clause": "5.3.3", "raw": "..."}
"""
from __future__ import annotations

import re

_TYPES = (
    ("weighted_mean", re.compile(r"加权平均|权重平均|按权重")),
    ("std_dev", re.compile(r"标准差|标准偏差|均方根偏差|均方差")),
    ("range", re.compile(r"极差|最大值与最小值之差")),
    ("percentage", re.compile(r"百分比|百分率|变化率|增长率|下降率|相对偏差|偏差率")),
    ("mean", re.compile(r"算术平均|平均值|均值")),
)
_CALC = re.compile(
    r"计算|按式|公式|式\s*[（(]|取[^。；;]{0,30}?平均|之差|"
    r"作为[^。；;]{0,10}?(?:结果|试验值|测定值)|[×xX]\s*100\s*%|/\s*n(?![A-Za-z])"
)
_PERCENT_CALC = re.compile(r"按式|公式|计算式|式\s*[（(]|[=＝]|×\s*100\s*%|百分率|相对偏差|偏差率")
_FORMULA_REF = re.compile(r"按式|公式|计算式|式\s*[（(]\s*\d")
_VARIABLE = re.compile(r"([A-Za-zα-ωΑ-Ω][\w′̄]{0,4})\s*[—–\-]{1,2}\s*([^；;。]+)")
_CLAUSE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S")


def _sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[。；;]", text or "")
        if len(part.strip()) >= 4
    ]


def _variables(text: str) -> list[dict]:
    index = (text or "").find("式中")
    if index < 0:
        return []
    section = re.split(r"[。]", text[index + 2:])[0]
    variables: list[dict] = []
    for match in _VARIABLE.finditer(section):
        definition = match.group(2).strip(" ：:，,")
        if definition and len(definition) <= 60:
            variables.append({"symbol": match.group(1), "definition": definition})
    return variables[:12]


def _entry(kind, status, sentence, variables, clause, item_clause) -> dict:
    return {"kind": kind, "status": status, "variables": variables,
            "clause": clause, "item_clause": item_clause, "raw": sentence}


def _classify(sentence, variables, clause, item_clause) -> dict | None:
    kind = next((name for name, pattern in _TYPES if pattern.search(sentence)), None)
    if kind == "percentage":
        # A percentage is only a formula with an explicit ratio/equation
        # context; "取峰值的 20%~80% 进行计算" is a sampling rule, not one.
        if _PERCENT_CALC.search(sentence):
            return _entry(kind, "extracted", sentence, variables, clause, item_clause)
    elif kind is not None and _CALC.search(sentence):
        return _entry(kind, "extracted", sentence, variables, clause, item_clause)
    if _FORMULA_REF.search(sentence):
        return _entry(None, "not_extracted", sentence, variables, clause, item_clause)
    return None


def extract_text_formulas(
    text: str, clause: str = "", item_clause: str = ""
) -> list[dict]:
    """Classify formula sentences from one clause's text (no document scan)."""
    variables = _variables(text)
    return [
        entry
        for sentence in _sentences(text)
        if (entry := _classify(sentence, variables, clause, item_clause)) is not None
    ]


def extract_formulas(lines: list[tuple[str, int]], items: list[dict]) -> list[dict]:
    """Extract formulas from cleaned lines ``(text, page)`` in document order."""
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
        if buffer.strip():
            entries.extend(extract_text_formulas(buffer, clause, owner(clause)))

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


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["kind"], entry["status"], entry["raw"], entry["clause"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def apply_formulas(
    items: list[dict], lines: list[tuple[str, int]]
) -> tuple[list[dict], dict]:
    """Return ``(formulas, summary)`` and attach per-item ``formulas``/status."""
    entries = extract_formulas(lines, items)
    by_item: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["item_clause"]:
            by_item.setdefault(entry["item_clause"], []).append(entry)
    for item in items:
        item["formulas"] = by_item.get(item["clause"], [])
        extracted = any(
            entry["status"] == "extracted" for entry in item["formulas"]
        )
        item["formula_status"] = "extracted" if extracted else "not_extracted"
    extracted = [entry for entry in entries if entry["status"] == "extracted"]
    kinds = (("mean", "formula_means"), ("weighted_mean", "formula_weighted_means"),
             ("range", "formula_ranges"), ("std_dev", "formula_std_devs"),
             ("percentage", "formula_percentages"))
    summary = {
        "formulas": len(entries),
        "formulas_extracted": len(extracted),
        "formulas_not_extracted": len(entries) - len(extracted),
        "items_with_formulas": sum(
            1 for item in items if item["formula_status"] == "extracted"
        ),
        **{
            key: sum(1 for entry in extracted if entry["kind"] == kind)
            for kind, key in kinds
        },
    }
    return entries, summary
