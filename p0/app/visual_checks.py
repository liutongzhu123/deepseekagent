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
import unicodedata
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
# Algorithm (docs/report-format-checks/README.md A12, 2026-11-08 rewrite):
#   1. split one line at every ``[前缀][阶段]照片N`` position;
#   2. drop body narrative (step-2 patterns);
#   3. series name = prefix minus photo word/number/stage, normalised;
#      empty -> nearest heading;
#   4. group by series name; each group must be 1..N in document order;
#   5. completeness: only unnamed groups must carry 试验前/中/后;
#   6. no embedded photo -> skip; no stage word anywhere -> skip.
#
# Failure scenarios, each covered in ``p0/tests/test_photo_sequence.py``:
# F1  no caption at all -> skip, never pass.
# F2  a number is skipped inside a group (1, 3) -> fail with the missing value.
# F3  the same number appears twice in one group -> fail.
# F4  a group starts above 1 -> fail (group numbers must be 1..N).
# F5  a number goes backwards inside one group -> fail.
# F6  an unnamed group misses a stage while the report labels stages -> fail.
# F7  no stage word anywhere -> skip completeness, never claim pass.
# F8  captions exist but the report embeds no photo -> skip completeness (D3 gap).
# F9  one line may hold several series; split by position (外包装照片1、内包装照片1).
# F10 multi-series interleaving (A1,B1,A2,B2) is counted per group, not globally.
# F11 body narrative (随报告附照片2张。) mentions a photo but is not a caption.
# F12 an unnamed group inherits its nearest heading; named groups are not forced.
# F13 1号样品照片1 is a named sample caption, not a test item.
# F14 取样后 is not 试验后.
# F15 the bracketed annotation (照片1（外观）) is the series name only without a prefix.
# F16 normalization (spaces, full-width, 的/共/计) must not split a group.
# F17 a new heading starts a new unnamed group.
# F18 numbering is text-only and checked even without embedded photos.
# ---------------------------------------------------------------------------

PHOTO_RULE_ID = "R23"
PHOTO_TITLE = "附录照片完整性"
_PHOTO_WORD_RE = re.compile(r"(?:照片|图片|相片|附图|photo)", re.IGNORECASE)
_PHOTO_NUMBER_RE = re.compile(r"(?:照片|图片|相片|附图|photo)\s*(\d+)", re.IGNORECASE)
_STAGE_RE = re.compile(r"试验\s*([前中后])")
_STAGE_NAMES = {"前": "before", "中": "middle", "后": "after"}
_STAGE_LABELS = {"before": "试验前", "middle": "试验中", "after": "试验后"}
# F11 / step 2: body narrative mentions photos but is not a caption.
_NARRATIVE_PATTERN_RE = re.compile(r"(?:附照片\s*\d+\s*张|详见照片\s*\d+|见照片\s*\d+\s*张)")
_NARRATIVE_SUFFIX_RE = re.compile(r"(?:照片|图片|相片|附图|photo)\s*\d+\s*(?:张|页|所示|为)", re.IGNORECASE)
_HEADING_RE = re.compile(
    r"^(?:附\s*录|附\s*件|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.．]|[（(]\d+[)）]"
    r"|(?:分项|项目)\s*[一二三四五六七八九十\d]+)"
)
_ANNOTATION_RE = re.compile(r"^\s*[（(]([^）)]*)[）)]")
_SEPARATOR_CHARS = "、，,；;：:·-—–_()（）[]【】 \t　"


def _is_photo_narrative(text: str) -> bool:
    """Step 2: True for a prose sentence that merely mentions photos."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("。") and len(stripped) > 20:  # step 2a
        return True
    if _NARRATIVE_PATTERN_RE.search(stripped):  # step 2b
        return True
    return bool(_NARRATIVE_SUFFIX_RE.search(stripped))  # step 2c


def _normalise(text: str) -> str:
    """Step 3: normalise a series name (spaces, full-width, 的/共/计, separators)."""
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("的", "").replace("共", "").replace("计", "")
    return value.strip(_SEPARATOR_CHARS)


def _without_photo_words(text: str) -> str:
    return _PHOTO_WORD_RE.sub(" ", text or "")


def _split_stage(prefix: str) -> tuple[str, str]:
    """Step 3: pull one stage attribute out of the prefix and return the rest.

    ``试验前/中/后`` count anywhere; a bare ``前/中/后`` only when it is the whole
    remaining label.  Thus ``取样后照片`` stays a named group (F14) instead of
    being counted as 试验后.
    """
    stages: list[str] = []

    def take(match: re.Match) -> str:
        stages.append(_STAGE_NAMES[match.group(1)])
        return " "

    remainder = _STAGE_RE.sub(take, prefix)
    bare = _normalise(remainder)
    if not stages and bare in _STAGE_NAMES:
        stages.append(_STAGE_NAMES[bare])
        remainder = ""
    return (stages[0] if stages else ""), remainder


def _caption_series(prefix_text: str, annotation: str) -> tuple[str, str]:
    """Step 3: (stage, series name) for one caption; empty series inherits."""
    stage, remainder = _split_stage(_without_photo_words(prefix_text))
    series = _normalise(remainder)
    if not series and annotation:  # F15: annotation only when there is no prefix
        series = _normalise(annotation)
    return stage, series


def _annotation_after(text: str, start: int) -> tuple[str, int]:
    match = _ANNOTATION_RE.match(text[start:])
    if not match:
        return "", start
    return match.group(1), start + match.end()


def _make_photo_entry(index: int, text: str, number: int | None, stage: str,
                      series: str, section: str) -> dict:
    return {
        "index": index,
        "text": text,
        "number": number,
        "stage": stage,
        "series": series,
        "group": series or section or "照片",
        "unnamed": not series,
        "section": section,
    }


def _photo_entries_for_line(text: str, index: int, section: str) -> list[dict]:
    """Steps 1-3: parse one line into caption entries; ``[]`` when not a caption."""
    if not text or not _PHOTO_WORD_RE.search(text):
        return []
    if _is_photo_narrative(text):  # F11
        return []
    number_matches = list(_PHOTO_NUMBER_RE.finditer(text))
    if not number_matches:
        # Stage-only caption (``试验前照片``); ``照片宽5cm`` has neither.
        word = _PHOTO_WORD_RE.search(text)
        annotation, _ = _annotation_after(text, word.end())
        stage, series = _caption_series(text[:word.start()], annotation)
        if not stage:
            return []
        return [_make_photo_entry(index, text, None, stage, series, section)]

    entries: list[dict] = []
    previous_end = 0
    for match in number_matches:
        annotation, caption_end = _annotation_after(text, match.end())
        stage, series = _caption_series(text[previous_end:match.start()], annotation)
        entries.append(_make_photo_entry(index, text, int(match.group(1)), stage, series, section))
        previous_end = caption_end
    return entries


def _photo_entries(paragraphs: Sequence[str]) -> list[dict]:
    entries: list[dict] = []
    section = ""
    for index, raw in enumerate(paragraphs, 1):
        text = (raw or "").strip()
        if not text:
            continue
        line_entries = _photo_entries_for_line(text, index, section)
        if line_entries:
            entries.extend(line_entries)
        elif _is_heading(text):
            section = text
    return entries


def _is_heading(text: str) -> bool:
    if len(text) > 40 or text.endswith(("。", "，", "；", ";", "、")):
        return False
    if _HEADING_RE.match(text):
        return True
    if _PHOTO_WORD_RE.search(text):
        return False
    return len(text) <= 20 and text.endswith(("试验", "测试", "检测", "评价"))


def _number_issues(entries: Sequence[dict]) -> list[str]:
    """Step 4: per-series numbering; every group must be 1..N (F2-F5, F10)."""
    groups: dict[str, list[int]] = {}
    for entry in entries:
        if entry["number"] is None:
            continue
        groups.setdefault(entry["group"], []).append(entry["number"])
    issues: list[str] = []
    for label, numbers in groups.items():
        seen: set[int] = set()
        duplicates: list[int] = []
        backwards: list[tuple[int, int]] = []
        previous: int | None = None
        for number in numbers:
            if number in seen and number not in duplicates:
                duplicates.append(number)
            if previous is not None and number < previous:
                backwards.append((previous, number))
            seen.add(number)
            previous = number
        missing = [value for value in range(1, max(seen) + 1) if value not in seen]
        if missing:
            start = min(seen)
            missing_text = "、".join(str(value) for value in missing)
            issues.append(f"{label} 序号从 {start} 开始，缺 {missing_text}")
        for number in duplicates:
            issues.append(f"{label} 序号 {number} 重复")
        for before, after in backwards:
            issues.append(f"{label} 序号 {before} 之后出现 {after}（倒退）")
    return issues


def _stage_issues(entries: Sequence[dict]) -> tuple[list[str], dict[str, dict]]:
    """Step 5: only unnamed groups (captions that inherit a heading) are forced.

    A group is unnamed only when every caption in it inherited its name (F12),
    so a named group that happens to share the heading text is not forced and
    naming never flips the verdict.
    """
    groups: dict[str, dict] = {}
    for entry in entries:
        group = groups.setdefault(entry["group"], {"stages": set(), "unnamed": True})
        if entry["stage"]:
            group["stages"].add(entry["stage"])
        if not entry["unnamed"]:
            group["unnamed"] = False
    unnamed = {label: group for label, group in groups.items() if group["unnamed"]}
    issues = [
        f"{label} 缺{'、'.join(_STAGE_LABELS[key] for key in _STAGE_LABELS if key not in group['stages'])}照片"
        for label, group in unnamed.items()
        if len(group["stages"]) < len(_STAGE_LABELS)
    ]
    return issues, unnamed


def evaluate_photo_rules(data: ReportData) -> list[Finding]:
    """R23: appendix photo numbering and 试验前/中/后 completeness."""
    entries = _photo_entries(data.paragraphs)
    if not entries:
        return [_photo_finding("skip", "info", "未识别到附录照片题注，无法核对照片序号与试验前/中/后", "", "题注使用「照片N」「试验前照片」等写法后重试", 1)]
    number_issues = _number_issues(entries)
    stage_issues, stage_groups = _stage_issues(entries)
    has_stage = any(entry["stage"] for entry in entries)
    numbered = sum(1 for entry in entries if entry["number"] is not None)
    evidence = f"题注={len(entries)}; 有序号={numbered}; 阶段组={len(stage_groups)}; 嵌入图片={data.embedded_images}"
    page = max(data.page_totals) if data.page_totals else 1
    if number_issues:  # step 4: numbering is text-only, keep it even without photos (F18)
        return [_photo_finding("fail", "medium", f"附录照片序号存在 {len(number_issues)} 处不连续",
                               evidence + "; " + "; ".join(number_issues[:6]),
                               "按附录题注顺序连续编号，补齐缺号并删除重复号", page)]
    if not data.embedded_images:  # step 6a: F8
        return [_photo_finding("skip", "info", f"附录题注序号连续（{len(entries)} 条），但报告未嵌入照片，完整性无法核对",
                               evidence, "客户 D3 带嵌入照片的样例到位后复核试验前/中/后", page)]
    if not has_stage:  # step 6b: F7
        return [_photo_finding("skip", "info", "照片题注未标注试验前/中/后，完整性无法核对",
                               evidence, "题注补充试验前/中/后后复核", page)]
    if stage_issues:  # step 5: F6
        return [_photo_finding("fail", "medium", f"照片完整性存在 {len(stage_issues)} 处缺失",
                               evidence + "; " + "; ".join(stage_issues[:6]),
                               "每个检验项目补齐试验前/中/后照片", page)]
    if stage_groups:
        message = f"{len(stage_groups)} 组照片均含试验前/中/后，序号连续"
    else:
        message = "照片序号连续（具名照片组不强制试验前/中/后）"
    return [_photo_finding("pass", "info", message, evidence, "", page)]


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
