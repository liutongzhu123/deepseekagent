"""RA-4.16 / RA-4.17: 附录照片序号与试验前/中/后完整性（R23）。

Rules: ``docs/report-format-checks/README.md`` A12 (2026-11-08 algorithm):
split captions -> drop narrative -> series name -> per-series numbering ->
unnamed-group completeness -> skip instead of fake pass.

The 12 BYD drafts carry captions but no ``word/media`` photos, so the real
sample only proves "numbering consecutive + completeness skipped".  The eight
README cases below are the acceptance table, one test (or more) per row.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

APPENDIX = "附录A：照片"
SAMPLE = Path("/opt/dsh-agent/samples/byd/ots-250717(1)__ots-250717__袋子-100L.docx")


def _data(paragraphs: list[str], images: int = 3) -> ReportData:
    return ReportData(file_name="photo.docx", paragraphs=paragraphs, embedded_images=images)


def _r23(paragraphs: list[str], images: int = 3) -> dict:
    return _r23_from(_data(paragraphs, images))


def _r23_from(data: ReportData) -> dict:
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R23")


def _assert_no_number_issue(finding: dict) -> None:
    assert finding["status"] != "fail", finding
    assert "缺" not in finding["evidence"], finding
    assert "重复" not in finding["evidence"], finding
    assert "倒退" not in finding["evidence"], finding


# ---------------------------------------------------------------------------
# Case 1: body narrative is not a caption (step 2).
# ---------------------------------------------------------------------------

def test_case1_narrative_is_not_a_caption():
    finding = _r23([APPENDIX, "随报告附照片2张。"])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


@pytest.mark.parametrize(
    "narrative",
    [
        "随报告附照片2张。",
        "随报告附照片2张",
        "详见照片2张",
        "见照片2张",
        "照片2张",
        "照片2页",
        "照片2所示",
        "照片2为外观",
    ],
)
def test_case1_narrative_variants(narrative):
    finding = _r23([APPENDIX, narrative])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


def test_case1_long_sentence_ending_with_full_stop():
    narrative = "本报告附录内的全部照片均按检验项目顺序统一编号并附在报告末尾照片2。"
    finding = _r23([APPENDIX, narrative])

    assert len(narrative) > 20
    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


# ---------------------------------------------------------------------------
# Case 2: a named sample caption is one single-photo group.
# ---------------------------------------------------------------------------

def test_case2_sample_series_single_caption():
    finding = _r23([APPENDIX, "1号样品照片1"])

    # Numbering is fine; no stage word anywhere -> completeness skips (step 6).
    assert finding["status"] == "skip"
    assert "题注=1; 有序号=1" in finding["evidence"]
    _assert_no_number_issue(finding)
    assert "1号样品" not in finding["evidence"]  # named group is never forced


def test_case2_sample_series_passes_with_stages():
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "1号样品照片1",
    ])

    assert finding["status"] == "pass"


# ---------------------------------------------------------------------------
# Case 3: two series on one line, each starts at 1 (step 1).
# ---------------------------------------------------------------------------

def test_case3_two_series_on_one_line():
    finding = _r23([APPENDIX, "外包装照片1、内包装照片1"])

    assert finding["status"] == "skip"  # no stage word anywhere (step 6)
    assert "题注=2; 有序号=2" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_case3_two_series_passes_with_stages():
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "外包装照片1、内包装照片1",
    ])

    assert finding["status"] == "pass"


# ---------------------------------------------------------------------------
# Case 4: multi-series interleaving (the round-3 failure point).
# ---------------------------------------------------------------------------

INTERLEAVED = [
    APPENDIX,
    "外包装照片1、内包装照片1",
    "外包装照片2、内包装照片2",
]


def test_case4_interleaved_series_have_no_number_gap():
    finding = _r23(INTERLEAVED)

    assert finding["status"] == "skip"  # no stage word anywhere (step 6)
    assert "题注=4; 有序号=4" in finding["evidence"]
    _assert_no_number_issue(finding)
    assert "内包装" not in finding["evidence"]


def test_case4_interleaved_series_passes_with_stages():
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        *INTERLEAVED[1:],
    ])

    assert finding["status"] == "pass"


# ---------------------------------------------------------------------------
# Case 5: naming variants must reach the same verdict.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "captions",
    [
        ["外观照片1、外观照片2", "分项2照片1、分项2照片2"],
        ["外观照片1", "外观照片2", "分项2照片1", "分项2照片2"],
    ],
)
def test_case5_named_variants_same_verdict(captions):
    finding = _r23([APPENDIX, *captions])

    assert finding["status"] == "skip"  # no stage word anywhere (step 6)
    assert "题注=4; 有序号=4" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_case5_named_variants_pass_with_stages():
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "外观照片1、外观照片2", "分项2照片1、分项2照片2",
    ])

    assert finding["status"] == "pass"


def test_named_series_with_stage_captions_is_not_forced():
    """A prefix that names its own group may carry stages; they stay its own."""
    finding = _r23([
        APPENDIX,
        "分项1照片1", "分项1照片2", "分项1试验前照片", "分项1试验中照片", "分项1试验后照片",
        "分项2照片1", "分项2照片2",
    ])

    assert finding["status"] == "pass"


# ---------------------------------------------------------------------------
# Case 6: a gap inside one named series fails.
# ---------------------------------------------------------------------------

def test_case6_gap_inside_one_series_fails():
    finding = _r23([APPENDIX, "分项1照片1", "分项1照片3"])

    assert finding["status"] == "fail"
    assert "分项1 序号从 1 开始，缺 2" in finding["evidence"]


# ---------------------------------------------------------------------------
# Case 7: unnamed group completeness, gated by "any stage word in the report".
# ---------------------------------------------------------------------------

def test_case7_unnamed_group_missing_stages_fails():
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "分项2：外观试验", "照片1、照片2",
    ])

    assert finding["status"] == "fail"
    assert "分项2：外观试验 缺试验前、试验中、试验后照片" in finding["evidence"]


def test_case7_without_any_stage_word_skips():
    """Two-level gate: no stage word anywhere -> skip, not fail (README step 6)."""
    finding = _r23([APPENDIX, "分项2：外观试验", "照片1、照片2"])

    assert finding["status"] == "skip"
    assert "未标注试验前/中/后" in finding["message"]


# ---------------------------------------------------------------------------
# Case 8: the stage is an attribute, not part of the series name.
# ---------------------------------------------------------------------------

def test_case8_stages_do_not_split_the_series():
    finding = _r23([APPENDIX, "试验前照片1、试验中照片2、试验后照片3"])

    assert finding["status"] == "pass"
    assert "1 组照片均含试验前/中/后" in finding["message"]


# ---------------------------------------------------------------------------
# Boundary cases found while implementing the six steps.
# ---------------------------------------------------------------------------

def test_transposed_table_interleaving_has_no_false_gap():
    """A1,B1,C1 / A2,B2,C2 / A3,B3,C3 must be per-series, not a global run."""
    finding = _r23([
        APPENDIX,
        "外包装照片1、内包装照片1、样品照片1",
        "外包装照片2、内包装照片2、样品照片2",
        "外包装照片3、内包装照片3、样品照片3",
    ])

    assert finding["status"] == "skip"  # no stage word anywhere (step 6)
    assert "题注=9; 有序号=9" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_normalisation_keeps_one_group():
    """Spaces / full-width / 的 / 共 must not split the same series name."""
    finding = _r23([APPENDIX, "外包装照片1", "外包装 照片2", "外包装的照片3", "外包装共照片4"])

    assert finding["status"] == "skip"
    assert "题注=4; 有序号=4" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_fullwidth_numbers_still_count():
    finding = _r23([APPENDIX, "外包装照片１", "外包装照片２"])

    assert finding["status"] == "skip"
    assert "题注=2; 有序号=2" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_annotation_is_series_only_without_a_prefix():
    finding = _r23([APPENDIX, "照片1（外观）、照片2（外观）"])
    assert finding["status"] == "skip"
    assert "题注=2; 有序号=2" in finding["evidence"]
    _assert_no_number_issue(finding)

    finding = _r23([APPENDIX, "外包装照片1（局部）、内包装照片1"])
    assert finding["status"] == "skip"
    assert "题注=2; 有序号=2" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_unnamed_group_follows_the_nearest_heading():
    """Each heading starts its own unnamed group; a global run would see 1,2,1,2."""
    finding = _r23([
        APPENDIX,
        "分项1：气味试验", "照片1", "照片2",
        "分项2：外观试验", "照片1", "照片2",
    ])

    assert finding["status"] == "skip"
    assert "题注=4; 有序号=4" in finding["evidence"]
    _assert_no_number_issue(finding)


def test_numbering_fail_is_kept_without_embedded_photos():
    """Step 6 skips completeness but keeps the numbering conclusion (F18)."""
    finding = _r23([APPENDIX, "照片1", "照片3"], images=0)

    assert finding["status"] == "fail"
    assert "缺 2" in finding["evidence"]


def test_no_embedded_photo_skips_after_numbering():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2"], images=0)

    assert finding["status"] == "skip"
    assert "未嵌入照片" in finding["message"]


def test_no_stage_word_anywhere_skips_completeness():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2"])

    assert finding["status"] == "skip"
    assert "未标注试验前/中/后" in finding["message"]


def test_sampling_after_is_not_test_after():
    finding = _r23([APPENDIX, "试验前照片", "试验中照片", "取样后照片"])

    assert finding["status"] == "fail"
    assert "缺试验后照片" in finding["evidence"]


def test_photo_width_is_not_a_caption():
    finding = _r23([APPENDIX, "照片宽5cm"])

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


def test_duplicate_number_in_one_series_fails():
    finding = _r23([APPENDIX, "外包装照片1、外包装照片2、外包装照片2"])

    assert finding["status"] == "fail"
    assert "外包装 序号 2 重复" in finding["evidence"]


def test_backwards_number_fails():
    finding = _r23([APPENDIX, "照片1", "照片3", "照片2"])

    assert finding["status"] == "fail"
    assert "倒退" in finding["evidence"]


def test_group_starting_above_one_fails():
    finding = _r23([APPENDIX, "照片2", "照片3"])

    assert finding["status"] == "fail"
    assert "从 2 开始，缺 1" in finding["evidence"]


def test_repeated_one_in_one_group_is_a_duplicate():
    """A12 removed the old "回到 1 = 新开一组" rule: one group must be 1..N."""
    finding = _r23([APPENDIX, "照片1 试验前", "照片2 试验中", "照片1 试验后"])

    assert finding["status"] == "fail"
    assert "重复" in finding["evidence"] or "倒退" in finding["evidence"]


def test_complete_sequence_passes():
    finding = _r23([APPENDIX, "外包装照片1", "外包装照片2", "试验前照片", "试验中照片", "试验后照片"])

    assert ("R23", "附录照片完整性", "附录照片序号连续，且每个检验项目含试验前/中/后照片") in RULES
    assert finding["status"] == "pass"
    assert finding["location"]["section"] == "附录照片"
    assert finding["evidence"] == "题注=5; 有序号=2; 阶段组=1; 嵌入图片=3"


@pytest.mark.skipif(not SAMPLE.exists(), reason="customer sample not mounted")
def test_real_draft_numbering_is_consecutive_but_completeness_skips():
    finding = _r23_from(parse_docx(SAMPLE))

    assert finding["status"] == "skip"
    assert "序号连续" in finding["message"] and "未嵌入照片" in finding["message"]
