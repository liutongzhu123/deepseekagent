"""Small shared primitives used by the format-check modules."""
from __future__ import annotations

from ..docx_parser import ReportData


def _finding(rule_id, title, status, severity, message, evidence="", suggestion="", page=1, section="全文", cell=""):
    from ..rules import Finding

    return Finding(rule_id, title, status, severity, message, evidence, suggestion, {"page": max(1, int(page or 1)), "section": section, "cell": cell})
