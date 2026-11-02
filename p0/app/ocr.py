"""Local, free OCR for scanned standard PDFs.

The P0 parser could only read a PDF text layer.  This module renders each page
with ``pypdfium2`` and runs the bundled PP-OCR models through RapidOCR
(``rapidocr_onnxruntime``), both free and offline.  No cloud OCR or paid API is
used, so confidential standards stay local.

The OCR result keeps per-line and per-page confidence; ``standard_parser``
turns those into per-field review flags.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any


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

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "confidence": round(self.confidence, 4),
            "text": self.text,
            "lines": [line.to_dict() for line in self.lines],
        }


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
        return {
            "engine": self.engine,
            "dpi": self.dpi,
            "pages": len(self.pages),
            "avg_confidence": self.avg_confidence,
            "low_confidence_pages": low_pages,
            "elapsed_ms": self.elapsed_ms,
            "line_count": sum(len(page.lines) for page in self.pages),
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


def ocr_pdf(
    data: bytes,
    dpi: int = 220,
    max_pages: int | None = None,
    min_line_confidence: float = 0.35,
    engine: Any | None = None,
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
        raw, _elapse = recognizer(np.array(image))
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
        kept = [line for line in lines if line.confidence >= min_line_confidence]
        confidence_pool = kept or lines
        confidence = (
            sum(line.confidence for line in confidence_pool) / len(confidence_pool)
            if confidence_pool
            else 0.0
        )
        pages.append(OcrPage(page=index + 1, lines=kept, confidence=confidence))

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return OcrResult(
        pages=pages,
        engine="rapidocr_onnxruntime",
        dpi=dpi,
        elapsed_ms=elapsed_ms,
    )
