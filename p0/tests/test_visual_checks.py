"""Unit tests for the visual-checks scaffold (2026-11-07).

The app venv deliberately has no python-docx (the project ships its own
``docx_parser``), so these tests build a minimal .docx zip by hand instead of
depending on an extra package.
"""

from __future__ import annotations

import io
import zipfile

from PIL import Image

from app import visual_checks


def _png(color: tuple[int, int, int], size: tuple[int, int] = (256, 256), pattern: bool = False) -> bytes:
    """PNG bytes comfortably above DUP_MIN_BYTES so the hash actually runs."""
    image = Image.new("RGB", size, color)
    if pattern:
        pixels = image.load()
        for x in range(size[0]):
            for y in range(size[1]):
                if (x // 8 + y // 8) % 2 == 0:
                    pixels[x, y] = (255 - color[0], 255 - color[1], 255 - color[2])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _minimal_docx(images: list[bytes]) -> bytes:
    """A zip that looks enough like a .docx for extract_images()."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", '<?xml version="1.0"?><w:document/>')
        for index, data in enumerate(images, start=1):
            archive.writestr(f"word/media/image{index}.png", data)
    return buffer.getvalue()


def test_module_is_off_by_default(monkeypatch):
    monkeypatch.delenv("P0_VISUAL_CHECKS", raising=False)
    result = visual_checks.check_images([_png((10, 10, 10))])
    assert result["enabled"] is False
    assert result["status"] == "skipped"
    assert result["reason"] == "module_disabled"


def test_identical_images_are_reported(monkeypatch):
    monkeypatch.setenv("P0_VISUAL_CHECKS", "1")
    monkeypatch.setenv("P0_VISUAL_DUP_MIN_BYTES", "0")
    image = _png((30, 30, 30), pattern=True)
    findings = visual_checks.duplicate_photos([image, image])
    assert len(findings) == 1
    assert findings[0]["code"] == "V01"
    assert findings[0]["images"] == [1, 2]


def test_different_images_are_not_reported(monkeypatch):
    monkeypatch.setenv("P0_VISUAL_CHECKS", "1")
    left = _png((0, 0, 0))
    right = _png((255, 255, 255))
    assert visual_checks.duplicate_photos([left, right]) == []


def test_small_images_are_ignored(monkeypatch):
    monkeypatch.setenv("P0_VISUAL_CHECKS", "1")
    monkeypatch.setenv("P0_VISUAL_DUP_MIN_BYTES", "2048")
    tiny = b"\x89PNG\r\n\x1a\n"  # far below DUP_MIN_BYTES
    assert visual_checks.duplicate_photos([tiny, tiny]) == []


def test_equipment_labels_skip_without_model(monkeypatch):
    monkeypatch.setenv("P0_VISUAL_CHECKS", "1")
    monkeypatch.delenv("P0_VISUAL_LABEL_ENDPOINT", raising=False)
    result = visual_checks.equipment_label_states([_png((1, 2, 3))])
    assert result["status"] == "skipped"
    assert result["reason"] == "model_not_configured"
    assert "设备标签样例" in result["hint"]


def test_extract_images_reads_docx_media():
    docx = _minimal_docx([_png((12, 34, 56)), _png((90, 90, 90))])
    images = visual_checks.extract_images(docx)
    assert len(images) == 2
    assert visual_checks.extract_images(b"not a docx") == []


def test_check_docx_reports_summary_when_enabled(monkeypatch):
    monkeypatch.setenv("P0_VISUAL_CHECKS", "1")
    monkeypatch.setenv("P0_VISUAL_DUP_MIN_BYTES", "0")
    photo = _png((90, 90, 90), pattern=True)
    docx = _minimal_docx([photo, photo])  # same image twice -> one duplicate finding
    result = visual_checks.check_docx(docx)
    assert result["enabled"] is True
    assert result["images"] == 2
    assert result["duplicates"] == 1
    assert result["summary"]["fail"] == 1
    assert result["source"]["kind"] == "docx"
    assert result["equipment_labels"]["status"] == "skipped"
