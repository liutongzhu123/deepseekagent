"""Shared standard-PDF parsing used by the synchronous route and the worker.

Keeping the OCR/parse sequence in one place is what makes the async path
behave exactly like the old synchronous endpoint: the same OCR gate, the same
parser configuration and the same warnings.  The optional ``progress`` callback
receives ``(stage, percent)`` for the task UI.
"""
from __future__ import annotations

import os
from typing import Callable

from . import admin_config
from . import ocr as ocr_service
from .pdf_text import extract_pages
from .standard_parser import parse_standard

Progress = Callable[[str, int], None]


def looks_scanned(data: bytes) -> bool:
    """Cheap hint: a scanned PDF has little or no text on its first pages."""
    try:
        pages = extract_pages(data)
    except ValueError:
        return False
    sample = pages[:3]
    return sum(len(text or "") for text in sample) < 120


def parse_pdf_standard(
    data: bytes,
    filename: str,
    *,
    enable_ocr: bool = True,
    rule_config: dict | None = None,
    progress: Progress | None = None,
) -> dict:
    def report(stage: str, percent: int) -> None:
        if progress is not None:
            progress(stage, percent)

    report("extract", 8)
    pages = extract_pages(data)
    page_confidence: list[float] | None = None
    ocr_meta: dict | None = None
    ocr_warning: str | None = None
    ocr_enabled = enable_ocr and os.getenv("P0_OCR_ENABLED", "1") not in {"0", "false", "off"}
    if sum(len(page) for page in pages) < 500 and ocr_enabled:
        report("ocr", 25)
        try:
            dpi = int(os.getenv("P0_OCR_DPI", "220"))
            max_pages = int(os.getenv("P0_OCR_MAX_PAGES", "0")) or None
            def ocr_progress(done: int, total: int) -> None:
                report("ocr", 25 + int(45 * done / max(total, 1)))

            ocr_result = ocr_service.ocr_pdf(
                data, dpi=dpi, max_pages=max_pages, progress=ocr_progress
            )
            pages = ocr_result.page_texts()
            page_confidence = ocr_result.page_confidences()
            ocr_meta = ocr_result.meta()
        except ocr_service.OcrUnavailable as exc:
            ocr_warning = f"扫描件 OCR 未启用：{exc}"
    report("parse", 75)
    parsed = parse_standard(
        pages,
        filename,
        pdf_bytes=data,
        page_confidence=page_confidence,
        ocr=ocr_meta,
        rule_config=rule_config or admin_config.standard_config(),
    )
    if ocr_warning:
        parsed.setdefault("warnings", []).append(ocr_warning)
    report("parsed", 90)
    return parsed
