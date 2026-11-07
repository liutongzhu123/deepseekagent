"""Local, free OCR for scanned standard PDFs.

The P0 parser could only read a PDF text layer.  This module renders each page
with ``pypdfium2`` and runs the bundled PP-OCR models through RapidOCR
(``rapidocr_onnxruntime``), both free and offline.  No cloud OCR or paid API is
used, so confidential standards stay local.

The OCR result keeps per-line and per-page confidence; ``standard_parser``
turns those into per-field review flags.
"""
from __future__ import annotations

import difflib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from io import BytesIO
from typing import Any

from . import watermark


class OcrUnavailable(RuntimeError):
    """RapidOCR/pypdfium2 is not installed or cannot be initialised."""


@dataclass
class OcrLine:
    text: str
    confidence: float
    top: float
    left: float
    bbox: list[list[float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "top": round(self.top, 1),
            "left": round(self.left, 1),
            "bbox": self.bbox,
        }


@dataclass
class OcrPage:
    page: int
    lines: list[OcrLine]
    confidence: float
    watermark_mask: dict | None = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def to_dict(self) -> dict:
        payload = {
            "page": self.page,
            "confidence": round(self.confidence, 4),
            "text": self.text,
            "lines": [line.to_dict() for line in self.lines],
        }
        if self.watermark_mask is not None:
            payload["watermark_mask"] = self.watermark_mask
        return payload


@dataclass
class OcrResult:
    pages: list[OcrPage]
    engine: str
    dpi: int
    elapsed_ms: int

    @property
    def avg_confidence(self) -> float:
        if not self.pages:
            return 0.0
        return round(sum(page.confidence for page in self.pages) / len(self.pages), 4)

    def meta(self) -> dict:
        low_pages = [page.page for page in self.pages if page.confidence < 0.9]
        masked_pages = [
            page.page for page in self.pages
            if (page.watermark_mask or {}).get("applied")
        ]
        fallback_pages = [
            page.page for page in self.pages
            if (page.watermark_mask or {}).get("fallback")
        ]
        review_pages = []
        for page in self.pages:
            mask = page.watermark_mask or {}
            if not mask.get("applied"):
                continue
            before_lines = mask.get("before_content_lines")
            after_lines = mask.get("after_content_lines")
            before_chars = mask.get("before_chars")
            after_chars = mask.get("after_chars")
            line_loss = (
                before_lines is not None and after_lines is not None
                and after_lines < before_lines
            )
            char_loss = (
                before_chars is not None and after_chars is not None
                and before_chars > 0 and after_chars < before_chars * 0.95
            )
            if line_loss or char_loss:
                review_pages.append(page.page)
        return {
            "engine": self.engine,
            "dpi": self.dpi,
            "pages": len(self.pages),
            "avg_confidence": self.avg_confidence,
            "low_confidence_pages": low_pages,
            "elapsed_ms": self.elapsed_ms,
            "line_count": sum(len(page.lines) for page in self.pages),
            "watermark_mask_pages": masked_pages,
            "watermark_mask_fallback_pages": fallback_pages,
            "watermark_mask_review_pages": review_pages,
        }

    def page_texts(self) -> list[str]:
        return [page.text for page in self.pages]

    def page_confidences(self) -> list[float]:
        return [page.confidence for page in self.pages]

    def to_dict(self) -> dict:
        return {**self.meta(), "page_details": [page.to_dict() for page in self.pages]}


_ENGINE: Any | None = None


def _rapidocr_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise OcrUnavailable(
            "未安装 OCR 依赖：pip install rapidocr_onnxruntime pypdfium2"
        ) from exc
    _ENGINE = RapidOCR()
    return _ENGINE


def _lines_from_raw(raw: Any, min_line_confidence: float = 0.35) -> list[OcrLine]:
    """Turn RapidOCR boxes into de-duplicated reading-order lines."""
    lines: list[OcrLine] = []
    for box, text, score in raw or []:
        text = str(text).strip()
        if not text:
            continue
        top = min(point[1] for point in box)
        left = min(point[0] for point in box)
        lines.append(
            OcrLine(
                text=text,
                confidence=float(score),
                top=float(top),
                left=float(left),
                bbox=[[round(float(x), 1), round(float(y), 1)] for x, y in box],
            )
        )
    # RapidOCR returns reading order per box; sort by row then column to
    # keep split headings and body lines in document order.
    lines.sort(key=lambda line: (round(line.top / 14), line.left))
    return [line for line in lines if line.confidence >= min_line_confidence]


_WATERMARK_LINE = re.compile(
    r"^\s*(?:[T1]\s*[O0]|[O0]\s*F|[B8]\s*Y|[A4]\s*[T7])\s*[：:]", re.IGNORECASE
)


def _is_watermark_line(text: str) -> bool:
    # OCR sometimes reads TO as T0/1o and AT as A T; treat those as watermark
    # labels so they do not look like content loss during validation.
    return bool(_WATERMARK_LINE.match(text or ""))


def _normalise_line(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _line_matches(normalised: str, after_lines: list[str], threshold: float = 0.82) -> bool:
    for candidate in after_lines:
        if not candidate:
            continue
        if normalised in candidate or candidate in normalised:
            return True
        if difflib.SequenceMatcher(None, normalised, candidate).ratio() >= threshold:
            return True
    return False


def _missing_content_lines(before: list[OcrLine], after: list[OcrLine],
                           min_chars: int = 8) -> list[str]:
    """Before-OCR content lines that the masked OCR no longer contains.

    Watermark lines and very short fragments are ignored; comparison is on a
    whitespace/punctuation-free form with a fuzzy ratio, so OCR spacing and
    single-character variants do not count as loss.  A non-empty result makes
    the caller fall back to the original image.
    """
    after_lines = [_normalise_line(line.text) for line in after]
    missing: list[str] = []
    for line in before:
        if _is_watermark_line(line.text):
            continue
        normalised = _normalise_line(line.text)
        if len(normalised) < min_chars:
            continue
        if not _line_matches(normalised, after_lines):
            missing.append(line.text)
    return missing


def _watermark_mask_enabled() -> bool:
    return os.getenv("P0_OCR_WATERMARK_MASK", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _recognise(image, recognizer, min_line_confidence: float):
    import numpy as np
    raw, _elapse = recognizer(np.asarray(image))
    return raw, _lines_from_raw(raw, min_line_confidence)


def _masked_page(image, recognizer, min_line_confidence: float):
    """Return ``(raw, lines, mask_meta)`` for one rendered page.

    A page with watermark-like pink pixels is OCR'd twice: original and masked.
    If any non-watermark content line disappears, the original result wins and
    ``fallback`` is recorded for the review warning.  Clean pages (no pink
    area) keep the single-OCR path.
    """
    try:
        masked, stats = watermark.mask_light_pink(image)
    except Exception:  # noqa: BLE001 - an optional preprocessing step must not fail OCR
        return None, None, {"applied": False, "fallback": False, "error": "mask unavailable"}
    if masked is None:
        return None, None, stats
    raw_before, lines_before = _recognise(image, recognizer, min_line_confidence)
    raw_after, lines_after = _recognise(masked, recognizer, min_line_confidence)
    missing = _missing_content_lines(lines_before, lines_after)
    before_content = [line for line in lines_before if not _is_watermark_line(line.text)]
    after_content = [line for line in lines_after if not _is_watermark_line(line.text)]
    meta = {
        **stats,
        "before_lines": len(lines_before),
        "after_lines": len(lines_after),
        "before_content_lines": len(before_content),
        "after_content_lines": len(after_content),
        "before_chars": sum(len(line.text) for line in before_content),
        "after_chars": sum(len(line.text) for line in after_content),
        "missing_lines": missing[:5],
    }
    if missing:
        meta.update({"applied": False, "fallback": True})
        return raw_before, lines_before, meta
    meta.update({"applied": True, "fallback": False})
    return raw_after, lines_after, meta


def ocr_image(
    data: bytes,
    min_line_confidence: float = 0.35,
    engine: Any | None = None,
) -> OcrPage:
    """OCR a single photo/screenshot quote inquiry (PNG/JPEG/WebP/BMP).

    Real customer inquiries often arrive as a phone photo or a pasted
    screenshot of a test-requirement table.  The same local RapidOCR models
    as the scanned-standard path are used so no customer data leaves the
    host.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise OcrUnavailable(
            "未安装 OCR 依赖：pip install rapidocr_onnxruntime Pillow"
        ) from exc

    try:
        image = Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - optional library boundary
        raise OcrUnavailable(f"无法读取图片：{exc}") from exc

    recognizer = engine or _rapidocr_engine()
    # Tall phone screenshots are OCR'd in overlapping horizontal tiles: the
    # detector's memory use grows with page height, and a single very tall
    # image can crash the ONNX session on memory-constrained hosts.  Each tile
    # is processed at the original resolution so small cell text stays legible.
    if image.height > 1100 and image.width <= 1000:
        lines: list[OcrLine] = []
        tile_height = 850
        overlap = 80
        top = 0
        while top < image.height:
            bottom = min(top + tile_height, image.height)
            tile = image.crop((0, top, image.width, bottom))
            raw, _elapse = recognizer(np.array(tile))
            for line in _lines_from_raw(raw, min_line_confidence):
                line.top += top
                for bbox_point in line.bbox:
                    bbox_point[1] += top
                lines.append(line)
            if bottom >= image.height:
                break
            top = bottom - overlap
        # De-duplicate the overlap region (same text, nearly same position).
        seen: set[tuple[str, int, int]] = set()
        unique: list[OcrLine] = []
        for line in lines:
            key = (line.text, round(line.top / 8), round(line.left / 8))
            if key in seen:
                continue
            seen.add(key)
            unique.append(line)
        lines = sorted(unique, key=lambda line: (line.top, line.left))
    else:
        # Bound the detector input for large single-page images.
        longest = max(image.size)
        if longest > 1200:
            scale = 1200 / longest
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            )
        raw, _elapse = recognizer(np.array(image))
        lines = _lines_from_raw(raw, min_line_confidence)
    confidence = (
        sum(line.confidence for line in lines) / len(lines) if lines else 0.0
    )
    return OcrPage(page=1, lines=lines, confidence=confidence)


def ocr_pdf(
    data: bytes,
    dpi: int = 220,
    max_pages: int | None = None,
    min_line_confidence: float = 0.35,
    engine: Any | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> OcrResult:
    """Render a PDF and OCR every page, preserving reading order.

    ``dpi=220`` keeps small printed values legible (the scanned standard's table
    values) while staying fast enough for a 44-page scan on four CPU cores.
    """
    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise OcrUnavailable(
            "未安装 OCR 依赖：pip install rapidocr_onnxruntime pypdfium2"
        ) from exc

    started = time.perf_counter()
    try:
        document = pdfium.PdfDocument(BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - optional library boundary
        raise OcrUnavailable(f"无法渲染 PDF：{exc}") from exc

    recognizer = engine or _rapidocr_engine()
    pages: list[OcrPage] = []
    total = len(document)
    limit = min(total, max_pages) if max_pages else total
    for index in range(limit):
        page = document[index]
        image = page.render(scale=dpi / 72.0).to_pil()
        raw = lines = None
        mask_meta = None
        if _watermark_mask_enabled():
            raw, lines, mask_meta = _masked_page(image, recognizer, min_line_confidence)
        if raw is None:
            raw, _elapse = recognizer(np.array(image))
            lines = _lines_from_raw(raw, min_line_confidence)
        confidence_pool = lines or _lines_from_raw(raw, 0.0)
        confidence = (
            sum(line.confidence for line in confidence_pool) / len(confidence_pool)
            if confidence_pool
            else 0.0
        )
        pages.append(OcrPage(
            page=index + 1, lines=lines, confidence=confidence,
            watermark_mask=mask_meta,
        ))
        if progress is not None:
            progress(index + 1, limit)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return OcrResult(
        pages=pages,
        engine="rapidocr_onnxruntime",
        dpi=dpi,
        elapsed_ms=elapsed_ms,
    )
