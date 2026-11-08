"""Shared standard-document parsing used by the synchronous route and the worker.

Keeping the OCR/parse sequence in one place is what makes the async path
behave exactly like the old synchronous endpoint: the same OCR gate, the same
parser configuration and the same warnings.  The optional ``progress`` callback
receives ``(stage, percent)`` for the task UI.

PDF goes through the text layer (with the existing scanned-PDF OCR fallback),
Word (``.docx``/``.docm``) through its OOXML text body, and a bare image through
local OCR.  Unsupported or unreadable inputs raise ``ValueError`` with a
user-facing message so the API can return one explicit error.
"""
from __future__ import annotations

import os
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from . import admin_config
from . import ocr as ocr_service
from . import watermark
from .pdf_text import extract_pages
from .standard_parser import parse_standard

Progress = Callable[[str, int], None]

WORD_SUFFIXES = {".docx", ".docm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_STANDARD_SUFFIXES = {".pdf", *WORD_SUFFIXES, *IMAGE_SUFFIXES}
SUPPORTED_STANDARD_HINT = (
    "支持 PDF、Word（.docx/.docm）和图片（.png/.jpg/.jpeg/.webp/.bmp）；"
    "旧版 .doc 请先用 Word 另存为 .docx 后上传"
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _ocr_enabled(enable_ocr: bool) -> bool:
    return enable_ocr and os.getenv("P0_OCR_ENABLED", "1").strip().lower() not in {
        "0", "false", "off",
    }


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
    ocr_enabled = _ocr_enabled(enable_ocr)
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
    pages, watermark_removed = watermark.clean_pages(pages)
    report("parse", 75)
    parsed = parse_standard(
        pages,
        filename,
        pdf_bytes=data,
        page_confidence=page_confidence,
        ocr=ocr_meta,
        rule_config=rule_config or admin_config.standard_config(),
    )
    if watermark_removed:
        parsed.setdefault("warnings", []).append(f"已清洗水印碎片 {watermark_removed} 处")
    if ocr_meta:
        fallback_pages = ocr_meta.get("watermark_mask_fallback_pages") or []
        if fallback_pages:
            pages_text = "、".join(str(page) for page in fallback_pages)
            parsed.setdefault("warnings", []).append(
                f"第 {pages_text} 页去水印后校验发现内容行缺失，已回退原图，请复核"
            )
        review_pages = ocr_meta.get("watermark_mask_review_pages") or []
        if review_pages:
            pages_text = "、".join(str(page) for page in review_pages)
            parsed.setdefault("warnings", []).append(
                f"第 {pages_text} 页去水印后内容行数/字符数减少，请复核"
            )
    if ocr_warning:
        parsed.setdefault("warnings", []).append(ocr_warning)
    report("parsed", 90)
    return parsed


def _docx_pages_and_tables(data: bytes) -> tuple[list[str], list[tuple[int, list[list[str]]]]]:
    """Split a Word OOXML body into page-ish text blobs and structured table grids."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise ValueError("无法读取 Word 文档：不是有效的 .docx 文件") from exc
    except ET.ParseError as exc:
        raise ValueError(f"无法读取 Word 文档：{exc}") from exc
    body = root.find(_W + "body")
    if body is None:
        raise ValueError("Word 文档缺少正文（word/document.xml）")
    pages = [""]
    tables: list[tuple[int, list[list[str]]]] = []

    def add(text: str) -> None:
        if (text or "").strip():
            pages[-1] += text.strip() + "\n"

    def paragraph_text(element: ET.Element) -> str:
        parts: list[str] = []
        for node in element.iter():
            if node.tag == _W + "t":
                parts.append(node.text or "")
            elif node.tag == _W + "br":
                if (node.get(_W + "type") or "").lower() != "page":
                    parts.append("\n")
            elif node.tag in {_W + "cr", _W + "tab"}:
                parts.append("\n" if node.tag == _W + "cr" else "\t")
        return "".join(parts)

    def cell_text(cell: ET.Element) -> str:
        texts = [" ".join(paragraph_text(p).split()) for p in cell.iter(_W + "p")]
        return " ".join(text for text in texts if text)

    def walk(element: ET.Element) -> None:
        if element.tag == _W + "p":
            add(paragraph_text(element))
            broken = any(
                (br.get(_W + "type") or "").lower() == "page"
                for br in element.iter(_W + "br")
            ) or any(True for _ in element.iter(_W + "lastRenderedPageBreak"))
            if broken:
                pages.append("")
        elif element.tag == _W + "tbl":
            grid = [
                [cell_text(cell) for cell in row.findall(_W + "tc")]
                for row in element.findall(_W + "tr")
            ]
            if grid:
                tables.append((len(pages), grid))
        elif element.tag == _W + "sdt":
            content = element.find(_W + "sdtContent")
            for child in list(content) if content is not None else []:
                walk(child)

    for child in list(body):
        walk(child)
    while len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    if not any(page.strip() for page in pages) and not tables:
        raise ValueError("Word 文档没有可解析的文字内容")
    return pages, tables


def _report(progress: Progress | None) -> Progress:
    return progress or (lambda stage, percent: None)


def parse_word_standard(data: bytes, filename: str, *, rule_config: dict | None = None, progress: Progress | None = None) -> dict:
    report = _report(progress)
    report("extract", 8)
    pages, tables = _docx_pages_and_tables(data)
    parsed = parse_standard(pages, filename, source_tables=tables, rule_config=rule_config or admin_config.standard_config())
    parsed.setdefault("meta", {})["source_format"] = "docx"
    parsed.setdefault("warnings", []).append("Word 文档按段落与分页符解析，页码为近似值，请复核")
    report("parsed", 90)
    return parsed


def parse_image_standard(data: bytes, filename: str, *, enable_ocr: bool = True, rule_config: dict | None = None, progress: Progress | None = None) -> dict:
    report = _report(progress)
    if not _ocr_enabled(enable_ocr):
        raise ValueError("图片解析依赖 OCR，但当前已关闭 OCR（enable_ocr 或 P0_OCR_ENABLED）")
    report("ocr", 25)
    try:
        page = ocr_service.ocr_image(data)
    except ocr_service.OcrUnavailable as exc:
        raise ValueError(f"图片文字识别不可用：{exc}") from exc
    if not page.text.strip():
        raise ValueError("未能从图片中识别出文字，请上传更清晰的图片，或改用 PDF/Word 文档")
    pages, watermark_removed = watermark.clean_pages([page.text])
    ocr_meta = {
        "engine": "rapidocr_onnxruntime",
        "pages": 1,
        "avg_confidence": round(page.confidence, 4),
        "line_count": len(page.lines),
        "low_confidence_pages": [1] if page.confidence < 0.9 else [],
    }
    report("parse", 75)
    parsed = parse_standard(pages, filename, page_confidence=[page.confidence], ocr=ocr_meta, rule_config=rule_config or admin_config.standard_config())
    parsed.setdefault("meta", {})["source_format"] = "image"
    if watermark_removed:
        parsed.setdefault("warnings", []).append(f"已清洗水印碎片 {watermark_removed} 处")
    report("parsed", 90)
    return parsed


def parse_standard_document(data: bytes, filename: str, *, enable_ocr: bool = True, rule_config: dict | None = None, progress: Progress | None = None) -> dict:
    """Parse a PDF, Word or image standard upload by its file extension."""
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        return parse_pdf_standard(data, filename, enable_ocr=enable_ocr, rule_config=rule_config, progress=progress)
    if suffix in WORD_SUFFIXES:
        return parse_word_standard(data, filename, rule_config=rule_config, progress=progress)
    if suffix in IMAGE_SUFFIXES:
        return parse_image_standard(data, filename, enable_ocr=enable_ocr, rule_config=rule_config, progress=progress)
    raise ValueError(SUPPORTED_STANDARD_HINT)
