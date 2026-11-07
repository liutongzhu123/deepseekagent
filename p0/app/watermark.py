"""PDF/OCR watermark pre-processing (T403).

Watermarks exported into the PDF text layer show up as isolated Latin letters
between CJK glyphs ("外A观B应C无D缺E陷"), as a repeated standard-number-like string
("Q/JLY J7111074B-2021" / "Q-JLY J7111074B"), as a repeated four-line recipient
block ("TO/OF/BY/AT"), or as a configured customer mark.
Left in place they pollute clause/number extraction, table values and the
content published into WeKnora.

The cleaner is deliberately conservative and reversible:

* configured terms (``P0_WATERMARK_TERMS``, comma separated) are removed;
* a four-line ``TO/OF/BY/AT`` block is removed only when the same block repeats
  at least ``P0_WATERMARK_BLOCK_MIN`` times on the page (default 4), so a real
  transfer/trace header survives;
* isolated single Latin letters *between CJK characters* are removed only when a
  page has at least ``min_fragments`` of them — one or two matches are treated
  as real text (``分为A类``);
* a standard-number-like token is removed only when the same token repeats at
  least ``P0_WATERMARK_TOKEN_MIN`` times on the page (default 4), so a single
  citation survives;
* ``P0_WATERMARK_CLEAN=0`` turns the whole step off.

Both PDF text and OCR text run through it in ``standard_parse`` before parsing,
so ingestion/publishing receives the cleaned text.
"""
from __future__ import annotations

import collections
import os
import re

_SINGLE_LETTER = re.compile(r"(?<=[\u4e00-\u9fff])[A-Za-z](?=[\u4e00-\u9fff])")
#: Four consecutive ``TO/OF/BY/AT`` lines, the recipient watermark block seen in
#: the customer sample.  Removal is gated by repetition, never by shape alone.
_RECIPIENT_BLOCK = re.compile(
    r"(?m)^TO\s*[：:][^\r\n]*\r?\n"
    r"OF\s*[：:][^\r\n]*\r?\n"
    r"BY\s*[：:][^\r\n]*\r?\n"
    r"AT\s*[：:][^\r\n]*\r?\n?"
)
#: ``Q/JLY J7111074B-2021`` and ``Q-JLY J7111074B`` — standard numbers used as
#: diagonal watermarks.  Removal is gated by repetition, never by shape alone.
_STANDARD_TOKEN = re.compile(
    r"(?i)\bQ[-/][A-Z]{2,6}\s?[A-Z]?\d{2,}[A-Z]?(?:-\d{2,4})?\b"
)
DEFAULT_MIN_FRAGMENTS = 3
DEFAULT_TOKEN_MIN = 4
DEFAULT_BLOCK_MIN = 4


def enabled() -> bool:
    return os.getenv("P0_WATERMARK_CLEAN", "1").strip().lower() not in {"0", "false", "off", "no"}


def configured_terms() -> tuple[str, ...]:
    raw = os.getenv("P0_WATERMARK_TERMS", "")
    return tuple(term.strip() for term in raw.split(",") if term.strip())


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _normalise_block(block: str) -> str:
    return block.replace("\r\n", "\n").strip()


def _remove_repeated_blocks(text: str, threshold: int) -> tuple[str, int]:
    """Remove identical four-line recipient blocks repeated on one page."""
    matches = list(_RECIPIENT_BLOCK.finditer(text))
    if not matches:
        return text, 0
    blocks = [_normalise_block(match.group(0)) for match in matches]
    counts = collections.Counter(blocks)
    removable = {block for block, count in counts.items() if count >= threshold}
    if not removable:
        return text, 0
    cleaned = _RECIPIENT_BLOCK.sub(
        lambda match: "" if _normalise_block(match.group(0)) in removable else match.group(0),
        text,
    )
    return cleaned, sum(counts[block] for block in removable)


def clean_text(text: str, *, min_fragments: int | None = None) -> tuple[str, int]:
    """Return ``(cleaned_text, removed_count)``.

    ``removed_count`` counts removed recipient blocks, single-letter fragments,
    repeated standard tokens and configured terms, so callers can report that
    the page was touched.
    """
    if not text or not enabled():
        return text or "", 0
    removed = 0
    value = text
    block_min = max(1, _env_int("P0_WATERMARK_BLOCK_MIN", DEFAULT_BLOCK_MIN))
    value, block_removed = _remove_repeated_blocks(value, block_min)
    removed += block_removed
    for term in configured_terms():
        count = value.count(term)
        if count:
            value = value.replace(term, "")
            removed += count

    token_min = max(1, _env_int("P0_WATERMARK_TOKEN_MIN", DEFAULT_TOKEN_MIN))
    for token, count in collections.Counter(_STANDARD_TOKEN.findall(value)).items():
        if count >= token_min:
            value = value.replace(token, "")
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
