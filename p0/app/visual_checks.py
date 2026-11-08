"""Visual checks scaffold: duplicate photos and equipment-label states (2026-11-07).

The two image-based checks are **off by default**. This module exists so the
two photo-dependent requirements have a concrete interface and a place to plug
a model in, without blocking on customer sample data:

* ``duplicate_photos`` — implemented with an 8×8 average hash. Works today on any
  report that actually contains images; the current 12 BYD drafts contain none,
  so it stays a no-op until a report with photos arrives.
* ``equipment_label_states`` — **stub**. Returns ``skipped: model_not_configured``
  until ``P0_VISUAL_LABEL_ENDPOINT`` is set *and* the customer supplies label
  photos (waiting/repair/running) so the model can be calibrated.
* ``evaluate_photo_rules`` — text-only caption check (rule ``R23``, on by
  default). It verifies that appendix photo numbers are consecutive and that
  every item with stage captions has 试验前/中/后.  It returns ``skip`` when the
  report embeds no photo, so the current drafts are not falsely failed.

Requirements covered by this scaffold:

* 1.2-15 同一份报告中照片是否有完全一样的  → ``duplicate_photos``
* 1.4-35 识别照片中设备标签运行状态          → ``equipment_label_states``
* RA-4.16 附录中照片序号无编错或漏编          → ``evaluate_photo_rules``
* RA-4.17 每个测试项目图片都有完整试验前、中、后 → ``evaluate_photo_rules``

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
import re
import zipfile
from typing import TYPE_CHECKING, Sequence

from .docx_parser import ReportData

if TYPE_CHECKING:  # avoid importing rules at module load (rules imports us lazily)
    from .rules import Finding

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


# ---------------------------------------------------------------------------
# Appendix photo captions (RA-4.16 / RA-4.17, deterministic rule R23).
#
# The BYD appendices label photos in text, e.g. ``外包装照片1`` /
# ``试验中照片`` / ``试验后照片``; images themselves live in ``word/media``.
# Failure scenarios, each covered in ``p0/tests/test_photo_sequence.py``:
# F1  no caption at all -> skip, never pass.
# F2  a number is skipped (1, 3) -> fail with the missing value.
# F3  the same number appears twice in one series -> fail.
# F4  a numbering series starts above 1 -> fail.
# F5  a number goes backwards inside one series -> fail.
# F6  an item with stage captions misses 试验前/中/后 -> fail.
# F7  captions carry no stage label -> skip completeness, never claim pass.
# F8  captions exist but the report embeds no photo -> skip completeness (D3 gap).
# F9  numbering restarts at 1 -> a new series, not a defect.
# F10 ``取样后`` is not a test stage; only 试验/测试/检测/处理/老化/加热/运行/评价 + 前/中/后 count.
# ---------------------------------------------------------------------------

PHOTO_RULE_ID = "R23"
PHOTO_TITLE = "附录照片完整性"
_PHOTO_NUMBER_RE = re.compile(r"(?:照片|图片|相片|附图|photo)\s*(?:No\.?\s*)?(\d+)", re.IGNORECASE)
_PHOTO_WORD_RE = re.compile(r"(?:照片|图片|相片|附图|photo|No\.?)", re.IGNORECASE)
_EXPLICIT_STAGE_RE = re.compile(r"(?:试验|测试|检测|处理|老化|加热|运行|评价)\s*([前中后])")
_STAGE_NAMES = {"前": "before", "中": "middle", "后": "after"}
_STAGE_LABELS = {"before": "试验前", "middle": "试验中", "after": "试验后"}
_HEADING_RE = re.compile(
    r"^(?:附\s*录|附\s*件|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.．]|[（(]\d+[)）])"
)


def _photo_entry(text: str, index: int, section: str) -> dict | None:
    """Parse one caption line; ``None`` when the line is not a photo caption."""
    if not text or len(text) > 60:
        return None
    lowered = text.lower()
    if not any(word in lowered for word in ("照片", "图片", "相片", "附图", "photo")):
        return None
    number_match = _PHOTO_NUMBER_RE.search(text)
    stage_match = _EXPLICIT_STAGE_RE.search(text)
    stage = _STAGE_NAMES[stage_match.group(1)] if stage_match else ""
    if not number_match and not stage:  # F1: "照片宽5cm"/"附录A：照片" are not captions
        return None
    item = text
    spans = ([number_match.span()] if number_match else []) + ([stage_match.span()] if stage_match else [])
    for start, end in sorted(spans, reverse=True):
        item = item[:start] + " " + item[end:]
    item = _PHOTO_WORD_RE.sub(" ", item)
    item = re.sub(r"^\s*(?:\d+[.、．]|[（(]\d+[)）])\s*", "", item)
    item = re.sub(r"[\s:：,，、;；\-—_()（）]+", " ", item).strip()
    if not stage and item in _STAGE_NAMES:  # F10: a bare 前/中/后 only when it is the whole label
        stage, item = _STAGE_NAMES[item], ""
    return {
        "index": index,
        "text": text,
        "number": int(number_match.group(1)) if number_match else None,
        "stage": stage,
        "item": item or section or "照片",
    }


def _photo_entries(paragraphs: Sequence[str]) -> list[dict]:
    entries: list[dict] = []
    section = ""
    for index, raw in enumerate(paragraphs, 1):
        text = (raw or "").strip()
        if not text:
            continue
        entry = _photo_entry(text, index, section)
        if entry is not None:
            entries.append(entry)
        elif _is_heading(text):
            section = text
    return entries


def _is_heading(text: str) -> bool:
    if len(text) > 40 or text.endswith(("。", "，", "；", ";", "、")):
        return False
    if _HEADING_RE.match(text):
        return True
    if any(word in text for word in ("照片", "图片", "相片", "附图")):
        return False
    return len(text) <= 20 and text.endswith(("试验", "测试", "检测", "评价"))


def _number_issues(entries: Sequence[dict]) -> list[str]:
    """Continuity per numbering series; a restart at 1 opens a new series (F9)."""
    issues: list[str] = []
    previous: int | None = None
    for entry in entries:
        number = entry["number"]
        if number is None:
            continue
        label = entry["item"]
        if number == 1 and previous != 1:
            previous = 1
            continue
        if previous is None:
            issues.append(f"{label} 序号从 {number} 开始，缺 1")
        elif number == previous:
            issues.append(f"{label} 序号 {number} 重复")
        elif number < previous:
            issues.append(f"{label} 序号 {previous} 之后出现 {number}（倒退，可能漏编）")
        elif number > previous + 1:
            issues.append(f"{label} 序号 {previous} 之后跳到 {number}，缺 {previous + 1}")
        previous = number
    return issues


def _stage_issues(entries: Sequence[dict]) -> tuple[list[str], dict[str, set[str]]]:
    groups: dict[str, set[str]] = {}
    for entry in entries:
        if entry["stage"]:
            groups.setdefault(entry["item"], set()).add(entry["stage"])
    issues = [
        f"{label} 缺{'、'.join(name for key, name in _STAGE_LABELS.items() if key not in stages)}照片"
        for label, stages in groups.items()
        if len(stages) < len(_STAGE_LABELS)
    ]
    return issues, groups


def evaluate_photo_rules(data: ReportData) -> list[Finding]:
    """R23: appendix photo numbering and 试验前/中/后 completeness."""
    entries = _photo_entries(data.paragraphs)
    if not entries:
        return [_photo_finding("skip", "info", "未识别到附录照片题注，无法核对照片序号与试验前/中/后", "", "题注使用「照片N」「试验前照片」等写法后重试", 1)]
    number_issues = _number_issues(entries)
    stage_issues, stage_groups = _stage_issues(entries)
    numbered = sum(1 for entry in entries if entry["number"] is not None)
    evidence = f"题注={len(entries)}; 有序号={numbered}; 阶段组={len(stage_groups)}; 嵌入图片={data.embedded_images}"
    page = max(data.page_totals) if data.page_totals else 1
    if number_issues:
        return [_photo_finding("fail", "medium", f"附录照片序号存在 {len(number_issues)} 处不连续",
                               evidence + "; " + "; ".join(number_issues[:6]),
                               "按附录题注顺序连续编号，补齐缺号并删除重复号", page)]
    if not data.embedded_images:  # F8: the drafts carry captions but no word/media photo
        return [_photo_finding("skip", "info", f"附录题注序号连续（{len(entries)} 条），但报告未嵌入照片，完整性无法核对",
                               evidence, "客户 D3 带嵌入照片的样例到位后复核试验前/中/后", page)]
    if not stage_groups:  # F7
        return [_photo_finding("skip", "info", "照片题注未标注试验前/中/后，完整性无法核对",
                               evidence, "题注补充试验前/中/后后复核", page)]
    if stage_issues:
        return [_photo_finding("fail", "medium", f"照片完整性存在 {len(stage_issues)} 处缺失",
                               evidence + "; " + "; ".join(stage_issues[:6]),
                               "每个检验项目补齐试验前/中/后照片", page)]
    return [_photo_finding("pass", "info", f"{len(stage_groups)} 组照片均含试验前/中/后，序号连续",
                           evidence, "", page)]


def _photo_finding(status: str, severity: str, message: str, evidence: str, suggestion: str, page: int) -> Finding:
    from .rules import Finding  # local import: rules imports this module lazily

    return Finding(
        PHOTO_RULE_ID, PHOTO_TITLE, status, severity, message, evidence, suggestion,
        {"page": page, "section": "附录照片", "cell": "照片序号/试验前中后"},
    )


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
