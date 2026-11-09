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
  default), A12 v5.  It reports only 照片0 / gap / duplicate for standard
  captions and skips ambiguous forms; 试验前/中/后 completeness is not
  auto-judged.  It returns ``skip`` when the report embeds no photo, so the
  current drafts are not falsely failed.

Requirements covered by this scaffold:

* 1.2-15 同一份报告中照片是否有完全一样的  → ``duplicate_photos``
* 1.4-35 识别照片中设备标签运行状态          → ``equipment_label_states``
* RA-4.16 附录中照片序号无编错或漏编          → ``evaluate_photo_rules``
* RA-4.17 每个测试项目图片都有完整试验前、中、后 → 暂不自动判定（``skip``，待 D3）

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
# A12 v5 (2026-11-08, conservative rewrite): judge only a *standard caption* —
# a line with exactly one ``照片N`` and a series name it owns or inherits from
# the nearest heading.  Everything else is skipped with an explicit reason.
#
# The only failures are the three unambiguous numbering defects:
#   F1 number 0 (or lower);
#   F2 a gap inside one group (numbers must be 1..N);
#   F3 a duplicate inside one group.
# There is no stage-completeness failure, no "backwards" failure and no
# splitting of a line that carries several ``照片N`` captions.
#
# Scenarios, each covered in ``p0/tests/test_photo_sequence.py``:
# F1  ``照片0`` -> fail 序号从 0 开始.
# F2  group 1,3 -> fail 缺 2; a group starting at 2 -> fail 缺 1.
# F3  group 1,2,2 -> fail 序号重复.
# S1  two or more ``照片N`` on one line -> skip (do not split).
# S2  list-style numbering ``1、2、3`` without 照片 -> skip.
# S3  non-numeric number ``照片一`` / ``照片A`` -> skip.
# S4  no series name and no heading -> skip (grouping unreliable).
# S5  no embedded photo / no stage word in the report -> skip.
# S6  narrative that only mentions photos (``随报告附照片2张。``) -> skip;
#     the line is recorded in evidence so a following 照片2/3 cannot look like
#     a gap the narrative line owns.
# S7  zero standard captions -> skip, never pass.
# S8  a determiner plus a series name is a caption (``该外包装照片1``), while a
#     bare pronoun (``该照片8``) stays prose: the prefix alone decides nothing.
# ---------------------------------------------------------------------------

PHOTO_RULE_ID = "R23"
PHOTO_TITLE = "附录照片完整性"
_PHOTO_WORD_RE = re.compile(r"照片")
_PHOTO_NUMBER_RE = re.compile(r"照片\s*([0-9０-９]+)")
_NONNUMERIC_NUMBER_RE = re.compile(
    r"照片\s*(?:[一二三四五六七八九十百千万零〇两]+|[A-Za-z])"
    r"(?=\s*(?:$|[（(、，,。.;；]|试验[前中后]))"
)
_LIST_SEQUENCE_RE = re.compile(r"^\s*[0-9０-９]+(?:\s*[、,，；;]\s*[0-9０-９]+)+\s*$")
_STAGE_RE = re.compile(r"试验\s*([前中后])")
_STAGE_NAMES = {"前": "before", "中": "middle", "后": "after"}
# Step 2: body narrative mentions photos but is not a caption.
_NARRATIVE_PATTERN_RE = re.compile(r"(?:附照片\s*\d+\s*张|详见照片\s*\d+|见照片\s*\d+\s*张)")
_NARRATIVE_SUFFIX_RE = re.compile(r"照片\s*\d+\s*(?:张|幅|页|组|所示|为)")
# Prose words count only when they touch 照片N: immediately before it, or
# immediately after it (optionally after an annotation).  A description after
# punctuation (``外包装照片2，试验前可见划痕。``) and a series name that merely
# contains such a word (``显示面板照片1``) stay captions.
# Position carries meaning: a verb BEFORE 照片N is a narrative action
# (``拍摄照片1``), while a verb AFTER it describes the photo and is part of the
# caption (``照片1拍摄于试验前``, ``照片1显示了外包装``).  Only pointer words
# (可见/如下/详见正文/附在…) count as narrative when they immediately follow
# 照片N.  This keeps ``外包装照片0拍摄于试验前。`` from being dropped (F16).
_NARRATIVE_COMMON_WORDS = (
    r"随报告|随附|附上?|共附|合计|详见|参见|另见|见附|记录了?|"
    r"显示了?|展示了?|说明了?|引用了?|采集了?|附在|位于|放在|置于|可见|如下|"
    r"正文|报告末尾|报告正文|报告中|供参考|可以看到|我们|本文|本报告"
)
_NARRATIVE_BEFORE_WORDS = _NARRATIVE_COMMON_WORDS + r"|拍摄了?"
_NARRATIVE_AFTER_WORDS = (
    r"附在|位于|放在|置于|另见|参见|详见|见附|正文|如下|供参考|"
    r"可以看到|可见|报告末尾|报告正文|报告中"
)
_NARRATIVE_BEFORE_RE = re.compile(rf"(?:{_NARRATIVE_BEFORE_WORDS})\s*$")
_NARRATIVE_AFTER_RE = re.compile(
    rf"^\s*(?:[（(][^）)]*[）)])?\s*(?:{_NARRATIVE_AFTER_WORDS})"
)
# A demonstrative determiner is not part of a series name: ``该外包装照片1``
# belongs to 外包装, so it must group with ``外包装照片2``.  A bare pronoun
# (``该照片8``) names no series and stays a prose reference.  Only the three
# forms named by the README are stripped; 这/那/其/此 can start ordinary words
# (其他/此外) and are out of this rework's scope.
_DEMONSTRATIVE_PREFIX_RE = re.compile(r"^(?:该|上述|本次)")
_BARE_PRONOUN_RE = re.compile(r"^(?:该|这|那|其|此|上述|本次)$")
_LEGACY_PRONOUN_PREFIX_RE = re.compile(r"^(?:这|那|其|此)")
_SENTENCE_END = "。！？；;"
# A series label is a short noun phrase; a long leading clause is prose.
_MAX_SERIES_LEN = 12
_HEADING_RE = re.compile(
    r"^(?:附\s*录|附\s*件|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.．](?!\d)|[（(]\d+[)）]"
    r"|(?:分项|项目)\s*[一二三四五六七八九十\d]+)"
)
_ANNOTATION_RE = re.compile(r"^\s*[（(]([^）)]*)[）)]")
_SEPARATOR_CHARS = "、，,；;：:·-—–_()（）[]【】 \t　"


def _normalise_line(text: str) -> str:
    """Strip one paragraph; full-width digits are handled by the regexes."""
    return (text or "").strip()


def _is_photo_narrative(text: str) -> bool:
    """True for a prose sentence that merely mentions photos.

    Length alone is not evidence: a standard caption can be long because it
    carries a description.  A verb before 照片N is narrative (``拍摄照片1``);
    a verb after it describes the photo and stays part of the caption
    (``外包装照片0拍摄于试验前。``).  Only pointer words immediately after
    照片N (可见/如下/详见正文/附在…) mark prose.  A bare demonstrative
    (``该照片8``) only refers to a photo; the same word before a series name
    (``该外包装照片1``) is a determiner and the line stays a caption.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _NARRATIVE_PATTERN_RE.search(stripped):
        return True
    if _NARRATIVE_SUFFIX_RE.search(stripped):
        return True
    match = _PHOTO_NUMBER_RE.search(stripped)
    prefix = stripped[:match.start()] if match else stripped
    if _NARRATIVE_BEFORE_RE.search(prefix) or _is_prose_pronoun(prefix):
        return True
    if match and _NARRATIVE_AFTER_RE.match(stripped[match.end():]):
        return True
    if stripped.endswith(_SENTENCE_END) and len(stripped) > 20:
        return not _is_series_label(prefix)
    return False


def _normalise(text: str) -> str:
    """Normalise a series name (spaces, full-width, 的/共/计, separators)."""
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("的", "").replace("共", "").replace("计", "")
    return value.strip(_SEPARATOR_CHARS)


def _split_stage(prefix: str) -> tuple[str, str]:
    """Pull one stage attribute out of a prefix/tail and return the rest.

    ``试验前/中/后`` count anywhere; a bare ``前/中/后`` only when it is the
    whole remaining label.  Thus ``取样后`` stays part of a named group.
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


def _is_bare_pronoun(prefix: str) -> bool:
    """True when the whole prefix is just a demonstrative pronoun.

    ``该外包装`` names a series, so it is a caption; ``该`` alone only refers
    to a photo already named elsewhere.  Stage words are removed first, so
    ``该试验前`` is still a bare reference.
    """
    _, remainder = _split_stage(prefix)
    return bool(_BARE_PRONOUN_RE.match(_normalise(remainder)))


def _is_prose_pronoun(prefix: str) -> bool:
    """True for a reference pronoun, including the out-of-scope legacy forms.

    该/上述/本次 only count when the prefix is the pronoun alone; a series
    name after them makes a caption.  这/那/其/此 keep their old behavior
    (series-prefixed or bare forms drop as prose) because they also start
    ordinary words and this rework did not change them.
    """
    return _is_bare_pronoun(prefix) or bool(_LEGACY_PRONOUN_PREFIX_RE.match(prefix))


def _is_series_label(prefix: str) -> bool:
    """True when the text before 照片N is a short series label, not a clause.

    Empty is allowed: the group is inherited from the nearest heading.  A clause
    carries sentence punctuation, a prose word touching 照片N, or a long leading
    phrase, so the line is prose even when it ends with ``。``.
    """
    _, remainder = _split_stage(prefix)
    label = _normalise(remainder)
    if not label:
        return True
    if len(label) > _MAX_SERIES_LEN:
        return False
    if re.search(r"[，,。；;！？!?、]", prefix):
        return False
    if _NARRATIVE_BEFORE_RE.search(prefix) or _is_prose_pronoun(prefix):
        return False
    return True


def _annotation_after(text: str, start: int) -> tuple[str, int]:
    match = _ANNOTATION_RE.match(text[start:])
    if not match:
        return "", start
    return match.group(1), start + match.end()


def _caption_parts(text: str, match: re.Match) -> tuple[int, str, str]:
    """(number, stage, series) for one ``照片N``; stage may be before/after."""
    number = int(match.group(1))
    annotation, caption_end = _annotation_after(text, match.end())
    prefix = text[:match.start()]
    tail = text[caption_end:]
    stage, remainder = _split_stage(prefix)
    tail_stage, _ = _split_stage(tail)
    stage = stage or tail_stage
    # ``该/上述/本次`` is a determiner, not the series name: 该外包装照片1 and
    # 外包装照片1 must share one group, otherwise the prefixed caption looks
    # like a second group starting at 1 and the plain 2/3 lines report a gap.
    series = _DEMONSTRATIVE_PREFIX_RE.sub("", _normalise(remainder))
    if not series and annotation:
        annotation_stage, annotation_rest = _split_stage(annotation)
        stage = stage or annotation_stage
        series = _normalise(annotation_rest)
    return number, stage, series


def _make_photo_entry(index: int, text: str, number: int, stage: str,
                      series: str, group: str) -> dict:
    return {
        "index": index,
        "text": text,
        "number": number,
        "stage": stage,
        "series": series,
        "group": group,
    }


def _parse_caption_line(text: str, index: int, section: str) -> tuple[dict | None, str | None]:
    """One line -> (standard-caption entry, skip reason), never split a line."""
    if _is_photo_narrative(text):  # S6
        # Record the drop: a silently ignored prose line makes the remaining
        # captions start above 1 and report a gap that the skipped line owns.
        return None, "正文叙述句"
    matches = list(_PHOTO_NUMBER_RE.finditer(text))
    if len(matches) > 1:  # S1
        zero = next((int(m.group(1)) for m in matches if int(m.group(1)) < 1), None)
        entry = None
        if zero is not None:  # F1 wins even on an otherwise skipped line
            entry = _make_photo_entry(index, text, zero, "", "", section or "照片")
        return entry, "一行多个照片N"
    if not matches:
        if _NONNUMERIC_NUMBER_RE.search(text):  # S3
            return None, "序号不是纯数字"
        return None, None
    number, stage, series = _caption_parts(text, matches[0])
    group = series or section
    if number < 1:  # F1: a zero is always a definite failure
        return _make_photo_entry(index, text, number, stage, series, group or "照片"), None
    if not group:  # S4
        return None, "无法确定分组"
    return _make_photo_entry(index, text, number, stage, series, group), None


def _photo_entries(paragraphs: Sequence[str]) -> tuple[list[dict], list[dict]]:
    """Scan paragraphs; return (standard captions, skipped caption forms)."""
    entries: list[dict] = []
    skipped: list[dict] = []
    section = ""
    for index, raw in enumerate(paragraphs, 1):
        text = _normalise_line(raw)
        if not text:
            continue
        if not _PHOTO_WORD_RE.search(text):
            if _LIST_SEQUENCE_RE.match(text):  # S2
                skipped.append({"index": index, "text": text, "reason": "列表型序号题注"})
            elif _is_heading(text):
                section = text
            continue
        if _is_heading(text) and not _PHOTO_NUMBER_RE.search(text):
            section = text
            continue
        entry, reason = _parse_caption_line(text, index, section)
        if entry is not None:
            entries.append(entry)
        if reason:
            skipped.append({"index": index, "text": text, "reason": reason})
    return entries, skipped


def _is_heading(text: str) -> bool:
    if len(text) > 40 or text.endswith(("。", "，", "；", ";", "、")):
        return False
    if _HEADING_RE.match(text):
        return True
    if _PHOTO_WORD_RE.search(text):
        return False
    return len(text) <= 20 and text.endswith(("试验", "测试", "检测", "评价"))


def _zero_issues(entries: Sequence[dict]) -> list[str]:
    """F1: a zero (or lower) is a definite failure, even on a mixed line."""
    zero_groups = sorted({
        entry["group"] for entry in entries
        if entry["number"] is not None and entry["number"] < 1
    })
    return [f"{label} 序号从 0 开始，至少应从 1 开始" for label in zero_groups]


def _group_issues(entries: Sequence[dict]) -> list[str]:
    """F2/F3: per-group gap and duplicate; no backwards rule in A12 v5."""
    groups: dict[str, list[int]] = {}
    for entry in entries:
        if entry["number"] is not None:
            groups.setdefault(entry["group"], []).append(entry["number"])
    issues: list[str] = []
    for label, numbers in groups.items():
        seen = set(numbers)
        missing = [value for value in range(1, max(seen) + 1) if value not in seen]
        if missing:
            start = min(seen)
            issues.append(f"{label} 序号从 {start} 开始，缺 " + "、".join(map(str, missing)))
        duplicates = sorted({value for value in numbers if numbers.count(value) > 1})
        if duplicates:
            issues.append(f"{label} 序号重复（{'、'.join(map(str, duplicates))}）")
    return issues


def _skip_evidence(skipped: Sequence[dict]) -> str:
    counts: dict[str, int] = {}
    for item in skipped:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return "跳过=" + "; ".join(f"{reason}×{count}" for reason, count in counts.items())


def evaluate_photo_rules(data: ReportData) -> list[Finding]:
    """R23: only standard-caption numbering; skip everything ambiguous."""
    entries, skipped = _photo_entries(data.paragraphs)
    numbered = sum(1 for entry in entries if entry["number"] is not None)
    evidence = f"标准题注={len(entries)}; 有序号={numbered}; 嵌入图片={data.embedded_images}"
    if skipped:
        evidence += "; " + _skip_evidence(skipped)
    page = max(data.page_totals) if data.page_totals else 1
    zero_issues = _zero_issues(entries)
    if zero_issues:  # F1: text-only, kept even without embedded photos
        summary = "；".join(zero_issues[:3])
        return [_photo_finding(
            "fail", "medium", f"附录照片序号存在 {len(zero_issues)} 处问题：{summary}",
            evidence + "; " + "; ".join(zero_issues[:6]),
            "照片序号至少应从 1 开始，把照片0 改为照片1 后重试", page,
        )]
    if not entries or skipped:
        # A skipped caption form (or a prose line) may carry numbers of a group
        # judged here (外包装照片1、内包装照片1 followed by 外包装照片2, or
        # 照片1可见破损。 followed by 照片2/3), so a gap would be an artefact of
        # the skip.  Never report it, never claim pass.
        reasons = "；".join(sorted({item["reason"] for item in skipped})) if skipped else ""
        if not entries:
            message = (f"未识别到附录照片题注（{reasons}），未核对照片序号"
                       if skipped else "未识别到附录照片题注，无法核对照片序号")
        else:
            message = (f"仅核对 {len(entries)} 条标准题注，另有 {len(skipped)} 行"
                       f"非标准题注已跳过（{reasons}），无法确认整体序号")
        return [_photo_finding("skip", "info", message, evidence,
                               "将待判题注改为一行一个「照片N」后重试", page)]
    group_issues = _group_issues(entries)  # F2/F3
    if group_issues:
        summary = "；".join(group_issues[:3])
        return [_photo_finding(
            "fail", "medium", f"附录照片序号存在 {len(group_issues)} 处问题：{summary}",
            evidence + "; " + "; ".join(group_issues[:6]),
            "按标准题注顺序连续编号：从 1 开始，补齐缺号并删除重复号", page,
        )]
    if not data.embedded_images:  # S5a
        return [_photo_finding(
            "skip", "info",
            f"标准题注序号连续（{len(entries)} 条），但报告未嵌入照片，无法核对",
            evidence, "客户 D3 带嵌入照片的样例到位后复核", page,
        )]
    has_stage = (
        any(_STAGE_RE.search(paragraph or "") for paragraph in data.paragraphs)
        or any(entry["stage"] for entry in entries)
    )
    if not has_stage:  # S5b
        return [_photo_finding(
            "skip", "info",
            "标准题注序号连续，但全篇未标注试验前/中/后，无法核对",
            evidence, "题注补充试验前/中/后后复核", page,
        )]
    return [_photo_finding(
        "pass", "info", f"标准题注序号连续（{len(entries)} 条）", evidence, "", page,
    )]


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
