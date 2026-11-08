"""RA-4.16 / RA-4.17: appendix photo numbering (R23), A12 v5.

``docs/report-format-checks/README.md`` A12 v5 is deliberately conservative:
judge only a *standard caption* (one ``照片N`` on the line, series name owned
or inherited), report only 照片0 / gap / duplicate, and skip every ambiguous
form with a reason.  Each failure (F) and skip (S) scenario in the
implementation header has at least one test here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

APPENDIX = "附录A：照片"
SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")


def _r23(paragraphs: list[str], images: int = 3) -> dict:
    data = ReportData(file_name="photo.docx", paragraphs=paragraphs, embedded_images=images)
    return _r23_from(data)


def _r23_from(data: ReportData) -> dict:
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R23")


# ---------------------------------------------------------------------------
# F1: 照片0 (N < 1) is the one failure the previous round missed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption", ["照片0", "外包装照片0", "试验前照片0", "照片0 试验前"])
def test_photo_zero_fails(caption):
    finding = _r23([APPENDIX, caption])

    assert finding["status"] == "fail"
    assert "序号从 0 开始" in finding["evidence"]


def test_photo_zero_fails_without_embedded_photos():
    """Numbering is text-only: the zero failure survives no word/media photos."""
    finding = _r23([APPENDIX, "外包装照片0"], images=0)

    assert finding["status"] == "fail"
    assert "序号从 0 开始" in finding["evidence"]


def test_photo_zero_in_a_multi_caption_line_still_fails():
    finding = _r23([APPENDIX, "外包装照片0、内包装照片1"])

    assert finding["status"] == "fail"
    assert "序号从 0 开始" in finding["evidence"]


# ---------------------------------------------------------------------------
# F2: a gap inside one group.
# ---------------------------------------------------------------------------

def test_gap_in_named_group_fails():
    finding = _r23([APPENDIX, "分项1照片1", "分项1照片3"])

    assert finding["status"] == "fail"
    assert "分项1 序号从 1 开始，缺 2" in finding["evidence"]


def test_group_starting_above_one_fails():
    finding = _r23([APPENDIX, "照片2", "照片3"])

    assert finding["status"] == "fail"
    assert "序号从 2 开始，缺 1" in finding["evidence"]


def test_multiple_missing_numbers_are_listed():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片4"])

    assert finding["status"] == "fail"
    assert "外包装 序号从 1 开始，缺 2、3" in finding["evidence"]


# ---------------------------------------------------------------------------
# F3: a duplicate inside one group.
# ---------------------------------------------------------------------------

def test_duplicate_number_fails():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2", "外包装照片2"])

    assert finding["status"] == "fail"
    assert "外包装 序号重复" in finding["evidence"]


def test_duplicate_without_embedded_photos_still_fails():
    finding = _r23([APPENDIX, "照片1", "照片1"], images=0)

    assert finding["status"] == "fail"
    assert "序号重复" in finding["evidence"]


# ---------------------------------------------------------------------------
# Standard captions: only these are judged (pass when everything lines up).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption", [
    "外包装照片1",          # named series
    "照片1",                # inherits 附录A：照片
    "试验前照片1",          # stage before the photo word
    "照片1 试验前",         # stage after the number
    "分项2照片1（外观）",   # prefix wins over the annotation
    "照片1（外观）",        # annotation is the series only without a prefix
])
def test_standard_caption_forms_are_judged(caption):
    finding = _r23([APPENDIX, caption])

    # The caption itself was judged (the difference from the five skipped
    # forms below); the stage gate may still make the final status skip/pass.
    assert finding["status"] != "fail"
    assert "标准题注=1" in finding["evidence"]
    assert "跳过=" not in finding["evidence"]


def test_standard_sequence_passes():
    finding = _r23([APPENDIX, "试验前照片1", "试验中照片2", "试验后照片3"])

    assert ("R23", "附录照片完整性", "附录照片序号连续（只判一行一个标准题注；试验前/中/后完整性暂不自动判定）") in RULES
    assert finding["status"] == "pass"
    assert finding["location"]["section"] == "附录照片"
    assert "标准题注=3" in finding["evidence"]


def test_named_stage_attributes_pass():
    finding = _r23([
        APPENDIX,
        "分项1试验前照片1", "分项1试验中照片2", "分项1试验后照片3",
    ])

    assert finding["status"] == "pass"
    assert "标准题注=3" in finding["evidence"]


def test_bare_stage_attributes_pass():
    finding = _r23([APPENDIX, "前照片1", "中照片2", "后照片3"])

    assert finding["status"] == "pass"
    assert "标准题注=3" in finding["evidence"]


def test_normalisation_keeps_one_group():
    """Spaces / 的 / 共 / full-width digits must not split the same series."""
    finding = _r23([
        APPENDIX,
        "外包装照片1", "外包装 照片2", "外包装的照片3", "外包装共照片４",
    ])

    assert finding["status"] == "skip"  # no stage word: correctness gate only
    assert "标准题注=4" in finding["evidence"]
    assert "缺" not in finding["evidence"]


def test_omitted_second_photo_word_only_counts_first():
    """README-known limitation: ``照片1、2`` only recognises the first number."""
    finding = _r23([APPENDIX, "照片1、2"])

    assert finding["status"] != "fail"
    assert "标准题注=1" in finding["evidence"]


# ---------------------------------------------------------------------------
# S1: two or more 照片N on one line -> skip, never split.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "外包装照片1、内包装照片1",
    "照片1 试验前、照片2 试验中、照片3 试验后",
    "外包装照片1、内包装照片1、外包装照片2",
])
def test_multi_caption_line_skips(line):
    finding = _r23([APPENDIX, line])

    assert finding["status"] == "skip"
    assert "一行多个照片N" in finding["evidence"]
    assert "缺" not in finding["evidence"]
    assert "重复" not in finding["evidence"]


def test_multi_line_does_not_create_a_false_gap():
    """外包装照片1 lives in the skipped line; it must not make 外包装照片2 a gap."""
    finding = _r23([APPENDIX, "外包装照片1、内包装照片1", "外包装照片2"])

    assert finding["status"] == "skip"
    assert "缺" not in finding["evidence"]
    assert "跳过=一行多个照片N" in finding["evidence"]


# ---------------------------------------------------------------------------
# S2: list-style numbering without the photo word -> skip.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["1、2、3", "1, 2, 3", "1；2；3"])
def test_list_sequence_skips(line):
    finding = _r23([APPENDIX, line])

    assert finding["status"] == "skip"
    assert "列表型序号题注" in finding["evidence"]


def test_decimal_value_is_not_a_list_sequence():
    """``4.0`` is a measurement, not a caption list (real-draft regression)."""
    finding = _r23([APPENDIX, "4.0"])

    assert finding["status"] == "skip"
    assert "列表型序号题注" not in finding["evidence"]


# ---------------------------------------------------------------------------
# S3: a number that is not a pure digit -> skip.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["照片一", "照片A", "外包装照片A"])
def test_non_numeric_number_skips(line):
    finding = _r23([APPENDIX, line])

    assert finding["status"] == "skip"
    assert "序号不是纯数字" in finding["evidence"]


# ---------------------------------------------------------------------------
# S4: no series name and no heading -> grouping unreliable, skip.
# ---------------------------------------------------------------------------

def test_undetermined_group_skips():
    finding = _r23(["照片1", "照片2"])

    assert finding["status"] == "skip"
    assert "无法确定分组" in finding["evidence"]


def test_named_caption_needs_no_heading():
    finding = _r23(["外包装照片1"])

    assert finding["status"] == "skip"  # no stage word, but grouping was known
    assert "标准题注=1" in finding["evidence"]
    assert "跳过=" not in finding["evidence"]


# ---------------------------------------------------------------------------
# S5: no embedded photo / no stage word anywhere -> skip.
# ---------------------------------------------------------------------------

def test_no_embedded_photo_skips_after_numbering():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2"], images=0)

    assert finding["status"] == "skip"
    assert "未嵌入照片" in finding["message"]
    assert "标准题注=2" in finding["evidence"]


def test_no_stage_word_anywhere_skips():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2"])

    assert finding["status"] == "skip"
    assert "未标注试验前/中/后" in finding["message"]


# ---------------------------------------------------------------------------
# S6/S7: narrative is not a caption; no caption never becomes pass.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "随报告附照片2张。",
    "详见照片2张",
    "见照片2张",
    "照片2所示",
    "照片2为外观",
    "照片宽5cm",
])
def test_narrative_or_non_caption_skips(line):
    finding = _r23([APPENDIX, line])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


def test_empty_appendix_skips():
    finding = _r23([APPENDIX])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


# ---------------------------------------------------------------------------
# Round 6: a long standard caption with a description is still a caption.
# The previous narrative filter dropped every >20-char line ending in 。
# ---------------------------------------------------------------------------

LONG_TAIL = "，试验前完整外观检查，无破损。"
LONG_TAIL_MIDDLE = "，试验中完整外观检查，无破损。"
LONG_TAIL_AFTER = "，试验后完整外观检查，无破损。"


def test_long_caption_is_not_dropped_as_narrative():
    """外包装照片2，… must not disappear and create a false "缺 2"."""
    finding = _r23([
        APPENDIX,
        "外包装照片1",
        "外包装照片2" + LONG_TAIL,
        "外包装照片3",
    ])

    assert finding["status"] == "pass"
    assert "标准题注=3" in finding["evidence"]
    assert "缺" not in finding["message"]


def test_long_caption_and_short_caption_get_the_same_verdict():
    short = _r23([APPENDIX, "外包装照片1 试验前", "外包装照片2 试验中", "外包装照片3 试验后"])
    long = _r23([
        APPENDIX,
        "外包装照片1" + LONG_TAIL,
        "外包装照片2" + LONG_TAIL_MIDDLE,
        "外包装照片3" + LONG_TAIL_AFTER,
    ])

    assert short["status"] == long["status"] == "pass"
    assert "标准题注=3" in short["evidence"]
    assert "标准题注=3" in long["evidence"]


def test_long_caption_without_prefix_inherits_heading():
    finding = _r23([
        APPENDIX,
        "照片1，试验前对完整外观进行检查，确认无破损。",
        "照片2，试验中对完整外观进行检查，确认无破损。",
        "照片3，试验后对完整外观进行检查，确认无破损。",
    ])

    assert finding["status"] == "pass"
    assert "标准题注=3" in finding["evidence"]


def test_long_caption_with_stage_before_is_judged():
    finding = _r23([
        APPENDIX,
        "试验前外包装照片1，完整外观检查，无破损。",
        "试验中外包装照片2，完整外观检查，无破损。",
        "试验后外包装照片3，完整外观检查，无破损。",
    ])

    assert finding["status"] == "pass"
    assert "标准题注=3" in finding["evidence"]


def test_long_photo_zero_is_not_dropped():
    """A >20-char 照片0 line was skipped as narrative, missing the zero."""
    finding = _r23([
        APPENDIX,
        "外包装照片0" + LONG_TAIL,
        "外包装照片1",
        "外包装照片2",
    ])

    assert finding["status"] == "fail"
    assert "序号从 0 开始" in finding["evidence"]


def test_long_caption_still_participates_in_gap_check():
    finding = _r23([
        APPENDIX,
        "外包装照片1",
        "外包装照片3" + LONG_TAIL,
    ])

    assert finding["status"] == "fail"
    assert "外包装 序号从 1 开始，缺 2" in finding["evidence"]


@pytest.mark.parametrize("line", [
    "试验过程中拍摄了照片1，用于记录样品外观变化并留档。",
    "本次共采集照片4张。",
    "该照片8拍摄于试验前。",
    "照片5附在报告末尾。",
])
def test_long_narrative_is_still_skipped(line):
    finding = _r23([APPENDIX, line])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


# ---------------------------------------------------------------------------
# Round-4 regressions that must now be skips, not failures.
# ---------------------------------------------------------------------------

def test_backwards_is_no_longer_a_failure():
    finding = _r23([APPENDIX, "照片1", "照片3", "照片2"])

    assert finding["status"] != "fail"
    assert "倒退" not in finding["evidence"]


def test_stage_names_do_not_create_a_completeness_failure():
    finding = _r23([APPENDIX, "试验前照片1", "试验中照片2"])

    # A12 v5 reports no stage-completeness failure; numbering {1,2} is fine.
    assert finding["status"] != "fail"
    assert "缺试验后" not in finding["evidence"]


# ---------------------------------------------------------------------------
# Real drafts: all 12 must still skip (no word/media photos).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="customer samples not mounted")
def test_twelve_real_drafts_still_skip():
    samples = sorted(
        path for path in SAMPLE_DIR.iterdir()
        if path.suffix == ".docx" and not path.name.startswith("._")
    )
    assert len(samples) == 12

    statuses = []
    for path in samples:
        finding = _r23_from(parse_docx(path))
        statuses.append(finding["status"])
        assert finding["status"] == "skip", path.name
        assert "标准题注=9" in finding["evidence"], path.name
        assert "嵌入图片=0" in finding["evidence"], path.name
    assert statuses.count("skip") == 12
