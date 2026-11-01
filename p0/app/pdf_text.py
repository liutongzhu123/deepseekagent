"""PDF text extraction for the P0 standard-parsing service.

``pypdf`` is vendored under ``p0/vendor`` so the P0 server keeps working
without network access (the configured pip mirror is not usable there).
Only the text layer is used: scanned standards need OCR/VLM, which is out
of scope for this increment and is reported explicitly by the parser.
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from pypdf import PdfReader  # noqa: E402  (after vendor path is registered)
from pypdf.errors import PdfReadError  # noqa: E402


def extract_pages(data: bytes) -> list[str]:
    """Return one text blob per PDF page, preserving page order."""
    try:
        reader = PdfReader(BytesIO(data))
    except PdfReadError as exc:  # pragma: no cover - depends on input file
        raise ValueError(f"无法读取 PDF：{exc}") from exc
    if reader.is_encrypted:
        # Empty-password encryption is common for exported enterprise PDFs.
        if reader.decrypt("") == 0:
            raise ValueError("PDF 已加密，无法读取文本层")
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a broken page must not kill the batch
            pages.append("")
    if not pages:
        raise ValueError("PDF 没有页面")
    return pages
