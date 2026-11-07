"""Visual checks scaffold: duplicate photos and equipment-label states (2026-11-07).

Both checks are **off by default**. This module exists so the two photo-dependent
requirements have a concrete interface and a place to plug a model in, without
blocking on customer sample data:

* ``duplicate_photos`` — implemented with an 8×8 average hash. Works today on any
  report that actually contains images; the current 12 BYD drafts contain none,
  so it stays a no-op until a report with photos arrives.
* ``equipment_label_states`` — **stub**. Returns ``skipped: model_not_configured``
  until ``P0_VISUAL_LABEL_ENDPOINT`` is set *and* the customer supplies label
  photos (waiting/repair/running) so the model can be calibrated.

Requirements covered by this scaffold:

* 1.2-15 同一份报告中照片是否有完全一样的  → ``duplicate_photos``
* 1.4-35 识别照片中设备标签运行状态          → ``equipment_label_states``

Environment:

* ``P0_VISUAL_CHECKS=1``            enable the whole module (default off)
* ``P0_VISUAL_DUP_HAMMING``         max Hamming distance for "same photo" (default 2)
* ``P0_VISUAL_DUP_MIN_BYTES``       ignore images smaller than this (default 2048)
* ``P0_VISUAL_LABEL_ENDPOINT``      model endpoint for label state (default empty = off)

Known limitations (documented so nobody mistakes the scaffold for a finished check):

* The hash catches re-encoded/resized duplicates, not crops or rotations.
* Images inside ``word/media`` have no reliable page mapping, so findings report
  image ordinals rather than page numbers.
"""

from __future__ import annotations

import io
import os
import zipfile
from typing import Sequence

try:  # Pillow is in requirements.txt; keep the module importable without it.
    from PIL import Image
except Exception:  # pragma: no cover - defensive
    Image = None  # type: ignore[assignment]

DEFAULT_DUP_HAMMING_MAX = 2
DEFAULT_DUP_MIN_BYTES = 2048

SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")


def enabled() -> bool:
    """Whole-module switch; default off so an upgrade never changes audit output."""
    return os.getenv("P0_VISUAL_CHECKS", "0").strip().lower() in {"1", "true", "on", "yes"}


def _dup_hamming_max() -> int:
    """Read per call so a test or a restart-with-different-env can change it."""
    try:
        return int(os.getenv("P0_VISUAL_DUP_HAMMING", str(DEFAULT_DUP_HAMMING_MAX)))
    except ValueError:
        return DEFAULT_DUP_HAMMING_MAX


def _dup_min_bytes() -> int:
    try:
        return int(os.getenv("P0_VISUAL_DUP_MIN_BYTES", str(DEFAULT_DUP_MIN_BYTES)))
    except ValueError:
        return DEFAULT_DUP_MIN_BYTES


def label_endpoint() -> str:
    return os.getenv("P0_VISUAL_LABEL_ENDPOINT", "").strip()


def label_enabled() -> bool:
    return bool(label_endpoint())


def _ahash(data: bytes, size: int = 8) -> int | None:
    """Average hash of a grayscale thumbnail; None when the bytes are not an image."""
    if Image is None:
        return None
    try:
        image = Image.open(io.BytesIO(data)).convert("L").resize((size, size))
    except Exception:
        return None
    pixels = list(image.getdata())
    if not pixels:
        return None
    average = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= average:
            bits |= 1 << index
    return bits


def _distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def duplicate_photos(images: Sequence[bytes]) -> list[dict]:
    """Return findings for image pairs whose average hashes are within the threshold."""
    minimum = _dup_min_bytes()
    threshold = _dup_hamming_max()
    hashed: list[tuple[int, int]] = []
    for index, data in enumerate(images):
        if len(data) < minimum:
            continue
        digest = _ahash(data)
        if digest is not None:
            hashed.append((index, digest))

    findings: list[dict] = []
    for first in range(len(hashed)):
        for second in range(first + 1, len(hashed)):
            (index_a, hash_a), (index_b, hash_b) = hashed[first], hashed[second]
            distance = _distance(hash_a, hash_b)
            if distance <= threshold:
                findings.append({
                    "code": "V01",
                    "rule": "同一报告中照片完全重复",
                    "severity": "fail",
                    "message": (
                        f"报告中第 {index_a + 1} 张与第 {index_b + 1} 张图片完全相同或高度相似"
                        f"（汉明距离 {distance}）"
                    ),
                    "images": [index_a + 1, index_b + 1],
                    "hamming": distance,
                })
    return findings


def equipment_label_states(images: Sequence[bytes]) -> dict:
    """Reserved interface for 1.4-35; no model yet, so it always reports why it skipped."""
    if not label_enabled():
        return {
            "status": "skipped",
            "reason": "model_not_configured",
            "checked_images": 0,
            "hint": (
                "设置 P0_VISUAL_LABEL_ENDPOINT 后启用。启用前需要客户提供设备标签样例"
                "（待机 / 运行 / 检修各若干张），否则无法校准模型。"
            ),
            "findings": [],
        }
    return {
        "status": "skipped",
        "reason": "not_implemented",
        "checked_images": len(images),
        "hint": "接口已预留；模型接入与阈值校准待设备标签样例到位后进行。",
        "findings": [],
    }


def extract_images(docx_bytes: bytes) -> list[bytes]:
    """Pull embedded pictures out of a .docx (word/media/*), in stable name order."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except Exception:
        return []
    names = sorted(
        name for name in archive.namelist()
        if name.startswith("word/media/") and name.lower().endswith(SUPPORTED_SUFFIXES)
    )
    images: list[bytes] = []
    for name in names:
        try:
            images.append(archive.read(name))
        except Exception:
            continue
    return images


def check_images(images: Sequence[bytes]) -> dict:
    """Run every visual check that is currently available, always returning a summary."""
    if not enabled():
        return {
            "enabled": False,
            "status": "skipped",
            "reason": "module_disabled",
            "hint": "设置 P0_VISUAL_CHECKS=1 后启用",
            "images": len(images),
            "findings": [],
        }

    duplicates = duplicate_photos(images)
    labels = equipment_label_states(images)
    findings = list(duplicates) + list(labels.get("findings") or [])
    summary = {
        "pass": sum(1 for item in findings if item.get("severity") == "pass"),
        "warn": sum(1 for item in findings if item.get("severity") == "warn"),
        "fail": sum(1 for item in findings if item.get("severity") == "fail"),
    }
    return {
        "enabled": True,
        "status": "ok",
        "images": len(images),
        "duplicates": len(duplicates),
        "equipment_labels": labels,
        "findings": findings,
        "summary": summary,
    }


def check_docx(docx_bytes: bytes) -> dict:
    """Convenience entry point for an uploaded .docx report."""
    images = extract_images(docx_bytes)
    result = check_images(images)
    result["source"] = {"kind": "docx", "images": len(images)}
    return result
