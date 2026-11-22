"""Small shared primitives used by the format-check modules."""
from __future__ import annotations

import re

from ..docx_parser import ReportData

_NA_VALUES = {"/", "／", "—", "——", "-", "－", "–", "无", "不适用", "n/a", "na", "不涉及"}
_TOC_LEADER = re.compile(r"(?:…{2,}|\.{4,}|·{4,})")


def _norm_cell(text: str) -> str:
    return re.sub(r"[\s\*＊]+", "", text or "")


def _toc_like(text: str) -> bool:
    return bool(_TOC_LEADER.search(text))


_CONCLUSION_HEADING_RE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,2})*[.、]?\s*)?(检验结论|检测结论)\s*(?:[:：]\s*(.*))?$"
)
_JUDGMENT_NA = {"", "/", "／", "—", "——", "-", "－", "–", "无", "不适用", "n/a", "na", "不涉及", "未判定"}


def _is_judgment_na(text: str) -> bool:
    norm = _norm_cell(text)
    return not norm or norm.lower() in _JUDGMENT_NA


def _conclusion_text(data: ReportData) -> tuple[str, bool]:
    """Return (conclusion text, whether the conclusion field was identified)."""
    text = (data.conclusion or "").strip()
    if text:
        return text, True
    paragraphs = [line.strip() for line in data.paragraphs if line and line.strip()]
    for index, line in enumerate(paragraphs):
        match = _CONCLUSION_HEADING_RE.match(line)
        if not match:
            continue
        if match.group(2) and match.group(2).strip():
            return match.group(2).strip(), True
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        # F6: another labeled field means the conclusion value is missing.
        if following and not re.match(r"^[^：:]{1,12}\s*[:：]", following):
            return following, True
        return "", True
    return "", False


def _result_judgments(data: ReportData) -> tuple[list[str], bool]:
    """Return judgment cells and whether the exact verdict column was found."""
    if not data.result_cells:
        return ([data.judgment.strip()], True) if (data.judgment or "").strip() else ([], False)
    header = [cell.strip() for cell in data.result_cells[0]]
    if "符合性判定" not in header:
        return [], False
    column = header.index("符合性判定")
    values = [
        row[column].strip()
        for row in data.result_cells[1:]
        if column < len(row) and row[column].strip()
    ]
    return values, True


def _finding(rule_id, title, status, severity, message, evidence="", suggestion="", page=1, section="全文", cell=""):
    from ..rules import Finding

    return Finding(rule_id, title, status, severity, message, evidence, suggestion, {"page": max(1, int(page or 1)), "section": section, "cell": cell})
