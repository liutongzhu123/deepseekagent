"""PDF report adapter: text layer first, local OCR fallback.

The docx parser is the reference ``ReportData`` shape; this module fills the
same dataclass from PDF pages so the deterministic rules can run unchanged.
Fields that cannot be read are left empty and the rules must return ``skip``
with a reason instead of ``pass``.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import ocr, pdf_text
from .docx_parser import ReportData

_REPORT_NO = re.compile(r"报告编号\s*[:：]\s*([A-Za-z0-9\-]+)")
_SAMPLE_NO = re.compile(r"样品编号\s*[:：]\s*([A-Za-z0-9\-]+)")
_CLIENT = re.compile(r"委托单位\s*[:：]\s*([^\n]+)")
_SAMPLE_NAME = re.compile(r"样品名称\s*[:：]\s*([^\n]+)")
_TEST_RANGE = re.compile(r"检验于\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*至\s*(\d{4}年\d{1,2}月\d{1,2}日)")


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def parse_pdf_report(path: str | Path, *, force_ocr: bool = False) -> ReportData:
    """Parse a PDF report into the same ``ReportData`` shape as docx."""
    path = Path(path)
    raw = path.read_bytes()
    pages = pdf_text.extract_pages(raw)
    source = "pdf"
    if force_ocr or not any(page.strip() for page in pages):
        result = ocr.ocr_pdf(raw)
        pages = result.page_texts()
        source = "ocr"
    text = "\n".join(pages)
    test_match = _TEST_RANGE.search(text)
    page_numbers = list(range(1, len(pages) + 1))
    return ReportData(
        file_name=path.name,
        source_format=source,
        cover_number=_first(_REPORT_NO, text),
        header_numbers=[],
        sample_no=_first(_SAMPLE_NO, text),
        client=_first(_CLIENT, text),
        sample_name=_first(_SAMPLE_NAME, text),
        test_start=test_match.group(1) if test_match else "",
        test_end=test_match.group(2) if test_match else "",
        page_numbers=page_numbers,
        page_totals=[len(pages)] * len(pages),
        footer_pages=page_numbers,
        paragraphs=pages,
        tables=[],
        template_family="generic",
    )
