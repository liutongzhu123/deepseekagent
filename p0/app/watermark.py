"""PDF/OCR watermark pre-processing (T403).

Watermarks exported into the PDF text layer show up as isolated Latin letters
between CJK glyphs ("外A观B应C无D缺E陷"), as a repeated four-line recipient block
("TO/OF/BY/AT"), or as a configured customer mark; a watermark block can also
repeat the document's own standard number.
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
* standard numbers are **never** removed token-by-token: a standard cited in
  the body can repeat many times in a requirement table (for example
  "满足 Q/JLY J7110440E-2021 中真空密封性规定"), and that is content.  The
  four-line block rule above is the only text-layer standard-watermark rule;
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
DEFAULT_MIN_FRAGMENTS = 3
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def mask_light_pink(image, *, min_ratio: float | None = None):
    """Remove light-pink watermark pixels from one rendered page.

    The customer sample uses a pale pink diagonal mark.  Only pixels with
    ``R > 180`` and a red-green/red-blue gap are set to white, so black/grey
    text is untouched.  Returns ``(masked_image, stats)``; ``masked_image`` is
    ``None`` when the pink area is below ``P0_OCR_WATERMARK_MIN_RATIO`` (default
    0.002), which keeps clean scans on the cheaper single-OCR path.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - optional OCR dependency
        raise RuntimeError("未安装 numpy，无法做水印掩膜") from exc
    rgb = np.asarray(image.convert("RGB")).astype(int)
    pink = (
        (rgb[..., 0] > 180)
        & ((rgb[..., 0] - rgb[..., 1]) > 8)
        & ((rgb[..., 0] - rgb[..., 2]) > 8)
    )
    ratio = float(pink.mean())
    threshold = (
        _env_float("P0_OCR_WATERMARK_MIN_RATIO", 0.002)
        if min_ratio is None
        else float(min_ratio)
    )
    stats = {
        "masked_pixels": int(pink.sum()),
        "ratio": round(ratio, 6),
        "threshold": threshold,
        "applied": ratio >= threshold,
    }
    if not stats["applied"]:
        return None, stats
    masked = np.asarray(image.convert("RGB")).copy()
    masked[pink] = 255
    from PIL import Image
    return Image.fromarray(masked), stats


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

    ``removed_count`` counts removed recipient blocks, single-letter fragments
    and configured terms, so callers can report that the page was touched.
    Standard numbers are never removed here: a cited standard repeated in a
    requirement table is content, and the four-line block rule already covers
    the customer watermark.
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
