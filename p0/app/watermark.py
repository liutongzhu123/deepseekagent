"""PDF/OCR watermark pre-processing (T403).

Watermarks exported into the PDF text layer show up as isolated Latin letters
between CJK glyphs ("外A观B应C无D缺E陷"), or as a repeated customer mark.  Left in
place they pollute clause/number extraction and table values.

The cleaner is deliberately conservative and reversible:

* configured terms (``P0_WATERMARK_TERMS``, comma separated) are removed;
* isolated single Latin letters *between CJK characters* are removed only when a
  page has at least ``min_fragments`` of them — one or two matches are treated
  as real text (``分为A类``);
* ``P0_WATERMARK_CLEAN=0`` turns the whole step off.

Both PDF text and OCR text run through it in ``standard_parse`` before parsing.
"""
from __future__ import annotations

import os
import re

_SINGLE_LETTER = re.compile(r"(?<=[\u4e00-\u9fff])[A-Za-z](?=[\u4e00-\u9fff])")
DEFAULT_MIN_FRAGMENTS = 3


def enabled() -> bool:
    return os.getenv("P0_WATERMARK_CLEAN", "1").strip().lower() not in {"0", "false", "off", "no"}


def configured_terms() -> tuple[str, ...]:
    raw = os.getenv("P0_WATERMARK_TERMS", "")
    return tuple(term.strip() for term in raw.split(",") if term.strip())


def clean_text(text: str, *, min_fragments: int | None = None) -> tuple[str, int]:
    """Return ``(cleaned_text, removed_count)``.

    ``removed_count`` counts removed single-letter fragments plus configured
    terms, so callers can report that the page was touched.
    """
    if not text or not enabled():
        return text or "", 0
    removed = 0
    value = text
    for term in configured_terms():
        count = value.count(term)
        if count:
            value = value.replace(term, "")
            removed += count
    threshold = DEFAULT_MIN_FRAGMENTS if min_fragments is None else max(1, int(min_fragments))
    matches = _SINGLE_LETTER.findall(value)
    if len(matches) >= threshold:
        value = _SINGLE_LETTER.sub("", value)
        removed += len(matches)
    return value, removed


def clean_pages(pages: list[str]) -> tuple[list[str], int]:
    """Clean every page and return ``(pages, total_removed)``."""
    cleaned: list[str] = []
    total = 0
    for page in pages:
        value, removed = clean_text(page)
        cleaned.append(value)
        total += removed
    return cleaned, total
