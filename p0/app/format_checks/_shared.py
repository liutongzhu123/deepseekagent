"""Small shared primitives used by the format-check modules."""
from __future__ import annotations

import re

from ..docx_parser import ReportData

_NA_VALUES = {"/", "／", "—", "——", "-", "－", "–", "无", "不适用", "n/a", "na", "不涉及"}


def _norm_cell(text: str) -> str:
    return re.sub(r"[\s\*＊]+", "", text or "")


def _finding(rule_id, title, status, severity, message, evidence="", suggestion="", page=1, section="全文", cell=""):
    from ..rules import Finding

    return Finding(rule_id, title, status, severity, message, evidence, suggestion, {"page": max(1, int(page or 1)), "section": section, "cell": cell})
