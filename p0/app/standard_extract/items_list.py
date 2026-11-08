"""B9: reviewable inspection-item list from a parsed standard.

The list keeps the three columns a reviewer needs: item name, test-method
clause and applicable component types, each traceable to its source.  Parts
come from an explicit "适用零件" table column or the standard's scope
("本标准适用于…"); when both are silent the entry is flagged for manual
review.  The list is only ready for storage once every entry is confirmed.
"""
from __future__ import annotations

import re

_SCOPE = re.compile(r"(?<!不)本(?:标准|文件|规范|部分)\s*适用于([^\n。；;]+)")
_PART_HEADER = (
    "适用零件", "适用部件", "适用车型", "适用材料", "适用对象",
    "零件类型", "部件类型",
)
_COMPONENT = re.compile(
    "控制器|传感器|总成|零部件|部件|模块|单元|系统|材料|产品|组件|元件|装置|"
    "零件|电池|电机|灯具|座椅|内饰|外饰|车身|底盘|发动机|变速器|线束|连接器|"
    "开关|继电器|保险丝|气囊|安全带|仪表|屏幕|摄像头|雷达"
)
_PLACEHOLDER = {"", "-", "—", "–", "/", "无", "不适用", "N/A", "n/a", "√", "×"}
_CONJUNCTION = re.compile(r"以及|及|和|与")


def _split_conjunction(text: str) -> list[str]:
    for match in _CONJUNCTION.finditer(text):
        left, right = text[:match.start()], text[match.end():]
        if _COMPONENT.search(left) and _COMPONENT.search(right):
            return [left] + _split_conjunction(right)
    return [text]


def _trim(part: str) -> str:
    part = part.strip(" 。；;，,、")
    if "的" in part and (len(part) > 20 or part.startswith(("配置", "用于", "适合"))):
        tail = part.rsplit("的", 1)[1].strip()
        if len(tail) >= 4 and _COMPONENT.search(tail):
            part = tail
    part = re.sub(r"^(?:包括|包含|如|例如|适用于|用于)\s*", "", part)
    return re.sub(r"(?:等|等等)$", "", part).strip(" 。；;，,、")


def normalise_parts(value) -> list[str]:
    """Split a scope cell/list into short component names, de-duplicated."""
    raw = [value] if isinstance(value, str) else list(value or [])
    parts: list[str] = []
    for entry in raw:
        text = re.sub(r"[（(][^）)]*[）)]", "", str(entry or ""))
        for segment in re.split(r"[，,；;、]", text.strip(" 。；;，,、")):
            parts.extend(_split_conjunction(segment) if segment.strip() else [])
    return list(dict.fromkeys(part for part in map(_trim, parts) if part))


def extract_scope_parts(lines: list[tuple[str, int]]) -> list[str]:
    """Applicable parts stated by the standard's scope sentence."""
    text = "\n".join(str(line).strip() for line, _page in lines)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    for match in _SCOPE.finditer(text):
        if parts := normalise_parts(match.group(1)):
            return parts
    return []


def _table_parts(item: dict) -> list[str]:
    parts: list[str] = []
    for table in item.get("tables") or []:
        header = [re.sub(r"\s+", "", str(cell or "")) for cell in table.get("header") or []]
        columns = [i for i, cell in enumerate(header) if any(w in cell for w in _PART_HEADER)]
        for row in table.get("rows") or []:
            for index in columns:
                cell = str(row[index] if index < len(row) else "").strip()
                if cell not in _PLACEHOLDER:
                    parts.extend(normalise_parts(cell))
    return parts


def build(meta: dict, items: list[dict], scope: list[str] | None = None) -> dict:
    """Build the checklist from stored item fields without mutating them."""
    scope = normalise_parts(scope) if scope is not None else []
    if not scope:
        scope = next(
            (
                normalise_parts(item.get("applicable_parts"))
                for item in items
                if item.get("applicable_parts_source") == "scope"
            ),
            [],
        )
    entries: list[dict] = []
    for index, item in enumerate(items, start=1):
        name = str(item.get("name") or "").strip()
        method_no = str(item.get("method_clause") or "").strip()
        parts = normalise_parts(item.get("applicable_parts"))
        pending = [f for f, v in (
            ("name", name), ("method_clause", method_no), ("applicable_parts", parts)
        ) if not v]
        reviewed = bool(item.get("reviewed"))
        entries.append({
            "seq": item.get("seq", index),
            "clause": str(item.get("clause") or ""),
            "name": name,
            "method_no": method_no,
            "applicable_parts": parts,
            "applicable_parts_source": (
                item.get("applicable_parts_source") or ("scope" if parts else "none")
            ),
            "page": item.get("page"),
            "review_status": "confirmed" if reviewed else "pending",
            "review_fields": [] if reviewed else pending,
        })
    pending_count = sum(1 for entry in entries if entry["review_status"] == "pending")
    return {
        "standard_no": str((meta or {}).get("standard_no") or ""),
        "standard_name": str((meta or {}).get("name") or ""),
        "applicable_parts": scope,
        "items": entries,
        "summary": {
            "items": len(entries),
            "items_with_applicable_parts": sum(1 for e in entries if e["applicable_parts"]),
            "pending_review": pending_count,
            "ready_for_store": bool(entries) and pending_count == 0,
        },
    }


def apply(meta: dict, items: list[dict], lines: list[tuple[str, int]]) -> tuple[dict, dict]:
    """Attach applicable parts to ``items`` and return ``(checklist, summary)``.

    Table evidence wins over the document scope; an unresolved entry is
    flagged for review instead of inheriting a possibly wrong scope.
    """
    scope = extract_scope_parts(lines)
    for item in items:
        parts, source = _table_parts(item), "table"
        if not parts and scope:
            parts, source = list(scope), "scope"
        if not parts:
            source = "none"
        item["applicable_parts"] = normalise_parts(parts)
        item["applicable_parts_source"] = source
        item["applicable_parts_confidence"] = 0.0 if not parts else (0.7 if source == "table" else 0.8)
        if not parts:
            fields = item.setdefault("review_fields", [])
            if not any(entry.get("field") == "applicable_parts" for entry in fields):
                fields.append({
                    "field": "applicable_parts",
                    "confidence": 0.0,
                    "reason": "未从标准范围或表格中提取到适用零部件，需人工确认",
                })
            item["review_required"] = True
    checklist = build(meta, items, scope)
    return checklist, checklist["summary"]
