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

SP_FORMULAVAR (SP-6.2): target/expression, uses/missing_variables (reported) and a
restricted-AST rule.  F1 no 式中, F2 unused, F3 separators, F4 full-width, F5 ellipsis,
F6 reference-only, F7 Greek/subscripts, F8 eval error, F9 unsafe AST, F10 series,
F11 equation without 式中, F12 trailing comma, F13 eval KeyError, F14 argument comma,
F15 note (注/备注/附注) is not a formula.
"""
from __future__ import annotations

import ast
import math
import operator
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
_VARIABLE = re.compile(
    r"([A-Za-zα-ωΑ-Ω][\w′̄]{0,5})\s*[—–\-:：]{1,2}\s*"
    r"([^；;。\n]+?)(?=\s*[；;。\n]|\s*[，,]\s*"
    r"[A-Za-zα-ωΑ-Ω][\w′̄]{0,5}\s*[—–\-:：]|$)"
)
_CLAUSE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S")
# A 注/备注/附注 sentence is prose about the standard, never a formula; an
# optional clause number may precede it.
_NOTE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}\s+)?(?:注|备注|附注)\s*\d*\s*[：:）)、]")
_LHS = r"(?:[A-Za-zα-ωΑ-Ω][A-Za-z0-9_\u0304′̄]{0,5}|[\u4e00-\u9fff]{2,6})"
_RHS = r"[0-9A-Za-zα-ωΑ-Ω+\-−－＋*/×÷·().%％√²³_′̄\s^…（）,，]+"
_EQUATION = re.compile(rf"(?P<lhs>{_LHS})\s*[=＝]\s*(?P<rhs>{_RHS})")
_SYMBOL = re.compile(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9_\u0304′̄]*")
_SERIES_MEAN = re.compile(
    r"^\((?P<base>[A-Za-z])1\+(?P=base)2\+\.\.\.\+(?P=base)n\)/n$"
)
_TRANS = str.maketrans({
    "（": "(", "）": ")", "×": "*", "÷": "/", "−": "-", "－": "-",
    "＋": "+", "％": "%", "·": "*", "—": "-", "–": "-", "，": ",",
})
_FUNCTIONS = {
    "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "sum": sum,
    "mean": lambda values: sum(values) / len(values),
}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Pow, ast.USub, ast.UAdd, ast.Name, ast.Load, ast.Constant, ast.Call,
)
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow}


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


def _top_level_cut(text: str) -> str:
    """Cut at the first comma outside parentheses.

    ``max(a,b)`` keeps its argument comma; a clause boundary such as
    ``x=a+b，式中…`` still ends at the comma (F14).
    """
    depth = 0
    for index, char in enumerate(text or ""):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif char in ",，" and depth == 0:
            return text[:index]
    return text


def _expression(sentence: str) -> tuple[str, str] | None:
    match = _EQUATION.search(sentence or "")
    if not match:
        return None
    rhs = _top_level_cut(match.group("rhs")).strip()
    return (match.group("lhs").strip(), rhs) if len(rhs) >= 2 else None


def _norm(text: str) -> str:
    return text.translate(_TRANS).replace(" ", "").replace("…", "...")


def _series_base(text: str) -> str:
    match = _SERIES_MEAN.fullmatch(_norm(text))
    return match.group("base") if match else ""


def _python(rhs: str) -> str:
    """Translate standard notation to a safe Python expression (or "")."""
    text = _norm(rhs)
    if base := _series_base(text):
        return f"mean({base})"
    if "..." in text:
        return ""
    text = text.replace("²", "**2").replace("³", "**3").replace("^", "**")
    text = re.sub(r"√\s*\(([^()]*)\)", r"sqrt(\1)", text)
    text = re.sub(r"√\s*([A-Za-z0-9_.]+)", r"sqrt(\1)", text)
    text = text.rstrip("%")
    return "" if not text or "%" in text else text


def _safe(text: str) -> bool:
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return False
    return all(
        isinstance(node, _ALLOWED_NODES)
        and (not isinstance(node, ast.Call)
             or (isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
                 and not node.keywords))
        for node in ast.walk(tree))


def _eval(node, values: dict):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise KeyError(node.id)
        return values[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, values)
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_eval(node.left, values), _eval(node.right, values))
    if isinstance(node, ast.Call):
        return _FUNCTIONS[node.func.id](*[_eval(a, values) for a in node.args])
    raise ValueError(type(node).__name__)


def evaluate_formula(formula: dict | None, values: dict | None = None) -> dict:
    """Run a formula entry (or its ``rule``); report missing values explicitly."""
    rule = formula.get("rule") if isinstance(formula, dict) else None
    if rule is None and isinstance(formula, dict) and "status" in formula:
        rule = formula
    if not isinstance(rule, dict) or not rule:
        return {"status": "not_executable", "value": None, "missing": [],
                "error": "没有可执行规则"}
    if rule.get("status") != "executable":
        return {"status": rule.get("status") or "not_executable", "value": None,
                "missing": list(rule.get("missing") or []), "error": ""}
    missing = [name for name in rule.get("inputs") or []
               if (values or {}).get(name) is None]
    if missing:
        return {"status": "missing_values", "value": None, "missing": missing,
                "error": ""}
    try:
        value = _eval(ast.parse(rule["expression"], mode="eval").body, values or {})
    except ZeroDivisionError:
        return {"status": "error", "value": None, "missing": [], "error": "除数为 0"}
    except KeyError as exc:
        name = str(exc.args[0]) if exc.args else ""
        return {"status": "missing_values", "value": None, "missing": [name],
                "error": ""}
    except Exception as exc:  # noqa: BLE001 - report, never crash
        return {"status": "error", "value": None, "missing": [], "error": str(exc)}
    return {"status": "ok", "value": value, "missing": [], "error": ""}


def _covers(symbol: str, defined: list[str]) -> bool:
    base = symbol.rstrip("′̄\u0304")
    return any(base == name.rstrip("′̄\u0304")
               or (len(name) == 1 and base.startswith(name)
                   and re.fullmatch(r"[0-9_′̄ijkn\u0304]+", base[len(name):]))
               for name in defined)


def _used(rhs: str, variables: list[dict]) -> list[str]:
    text = re.sub(r"\d+(?:\.\d+)?[eE][+-]?\d+", "", _norm(rhs))
    if base := _series_base(text):
        return [base, "n"]
    defined = [variable["symbol"] for variable in variables]
    used: list[str] = []
    for token in _SYMBOL.findall(text):
        if token in _FUNCTIONS or token == "pi":
            continue
        if (len(token) == 1 or token in defined
                or re.fullmatch(r"[A-Za-zα-ωΑ-Ω][0-9_′̄ijkn]{0,3}", token)):
            if token not in used:
                used.append(token)
    return used


def _names(expression: str) -> list[str]:
    return sorted({
        node.id for node in ast.walk(ast.parse(expression, mode="eval"))
        if isinstance(node, ast.Name) and node.id not in _FUNCTIONS
    })


def _rule(kind, target: str, rhs: str, variables: list[dict],
          used: list[str], missing: list[str]) -> dict:
    rule = {"kind": kind or "expression", "target": target, "inputs": used,
            "expression": rhs, "missing": list(missing)}
    if missing:
        return {**rule, "status": "missing_variables"}
    if rhs:
        python = _python(rhs)
        if python and _safe(python):
            base = _series_base(rhs)
            inputs = [base] if base else used
            return {**rule, "status": "executable", "expression": python,
                    "inputs": sorted(set(inputs) | set(_names(python)))}
        return {**rule, "status": "not_executable"}
    symbol = next((variable["symbol"] for variable in variables
                   if "次数" not in variable["definition"]), "")
    if kind == "mean" and symbol:
        return {**rule, "status": "executable", "inputs": [symbol],
                "expression": f"mean({symbol})"}
    return {**rule, "status": "not_extracted"}


def _entry(kind, status, sentence, variables, clause, item_clause,
           expression=None) -> dict:
    target, rhs = expression or ("", "")
    used = _used(rhs, variables) if rhs else []
    if rhs and (python := _python(rhs)) and _safe(python):
        used = list(dict.fromkeys(used + _names(python)))
    missing = [s for s in used
               if not _covers(s, [v["symbol"] for v in variables])]
    return {"kind": kind, "status": status, "variables": variables,
            "clause": clause, "item_clause": item_clause, "raw": sentence,
            "target": target, "expression": rhs, "uses": used,
            "missing_variables": missing,
            "rule": _rule(kind, target, rhs, variables, used, missing)}


def _classify(sentence, variables, clause, item_clause) -> dict | None:
    kind = next((name for name, pattern in _TYPES if pattern.search(sentence)), None)
    expression = _expression(sentence)
    calculated = (
        (kind == "percentage" and _PERCENT_CALC.search(sentence))
        or (kind not in (None, "percentage") and _CALC.search(sentence))
    )
    if expression is not None and not variables:
        return None
    if calculated or expression is not None:
        return _entry(kind, "extracted", sentence, variables, clause,
                      item_clause, expression)
    if _FORMULA_REF.search(sentence):
        return _entry(None, "not_extracted", sentence, variables, clause,
                      item_clause)
    return None


def extract_text_formulas(
    text: str, clause: str = "", item_clause: str = ""
) -> list[dict]:
    """Classify formula sentences from one clause's text (no document scan)."""
    sentences = [sentence for sentence in _sentences(text)
                 if not _NOTE.match(sentence)]
    variables = _variables(text)
    # 注/备注/附注 sentences are already gone; a 式中 section also defines the
    # variables of the equation it follows, and later sentences must not inherit
    # them (F15).
    boundary = next(
        (index for index, sentence in enumerate(sentences) if "式中" in sentence),
        len(sentences),
    )
    return [
        entry for index, sentence in enumerate(sentences)
        if (entry := _classify(
            sentence, variables if index <= boundary else [], clause, item_clause))
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
        item["formula_status"] = "extracted" if any(
            entry["status"] == "extracted" for entry in item["formulas"]
        ) else "not_extracted"
    extracted = [entry for entry in entries if entry["status"] == "extracted"]
    kinds = (("mean", "formula_means"), ("weighted_mean", "formula_weighted_means"),
             ("range", "formula_ranges"), ("std_dev", "formula_std_devs"),
             ("percentage", "formula_percentages"))
    summary = {
        "formulas": len(entries),
        "formulas_extracted": len(extracted),
        "formulas_not_extracted": len(entries) - len(extracted),
        "formulas_executable": sum(
            1 for e in entries if e["rule"] and e["rule"]["status"] == "executable"),
        "formulas_missing_variables": sum(
            1 for e in entries if e["missing_variables"]),
        "items_with_formulas": sum(
            1 for item in items if item["formula_status"] == "extracted"),
        **{
            key: sum(1 for entry in extracted if entry["kind"] == kind)
            for kind, key in kinds
        },
    }
    return entries, summary
