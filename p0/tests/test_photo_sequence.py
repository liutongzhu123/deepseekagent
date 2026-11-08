"""RA-4.16 / RA-4.17: 附录照片序号与试验前/中/后完整性（R23）。

The 12 BYD drafts carry appendix captions but no ``word/media`` photos, so the
real-sample test only proves "numbering consecutive + completeness skipped".
Missing 前/中/后 is covered with synthetic captions; failure branches F1-F10
are listed in ``p0/app/visual_checks.py``.
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


def _r23(data: ReportData) -> dict:
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R23")


def test_complete_sequence_passes():
    finding = _r23(_data([APPENDIX, "外包装照片1", "外包装照片2", "试验前照片", "试验中照片", "试验后照片"]))

    assert ("R23", "附录照片完整性", "附录照片序号连续，且每个检验项目含试验前/中/后照片") in RULES
    assert finding["status"] == "pass"
    assert finding["location"]["section"] == "附录照片"
    assert finding["evidence"] == "题注=5; 有序号=2; 阶段组=1; 嵌入图片=3"


@pytest.mark.parametrize(
    "captions,images,fragment",
    [
        (["照片1", "照片3"], 3, "缺 2"),  # F2
        (["照片1", "照片2", "照片2"], 3, "序号 2 重复"),  # F3
        (["照片2", "照片3"], 3, "从 2 开始，缺 1"),  # F4
        (["照片1", "照片3", "照片2"], 3, "倒退"),  # F5
        (["照片1", "照片3"], 0, "缺 2"),  # numbering is text-only, still checked without photos
    ],
)
def test_numbering_issues(captions, images, fragment):
    finding = _r23(_data([APPENDIX, *captions], images=images))

    assert finding["status"] == "fail"
    assert fragment in finding["evidence"]


@pytest.mark.parametrize(
    "captions,status,fragment",
    [
        # F6: each stage is reported when missing.
        ([APPENDIX, "试验中照片", "试验后照片"], "fail", "缺试验前照片"),
        ([APPENDIX, "试验前照片", "试验后照片"], "fail", "缺试验中照片"),
        ([APPENDIX, "试验前照片", "试验中照片"], "fail", "缺试验后照片"),
        # Items are grouped by the nearest heading; 取样后 is not 试验后 (F10).
        ([APPENDIX, "1. 气味试验", "试验前照片", "试验中照片", "试验后照片",
          "2. 外观试验", "试验前照片", "试验后照片"], "fail", "2. 外观试验 缺试验中照片"),
        ([APPENDIX, "试验前照片", "试验中照片", "取样后照片"], "fail", "缺试验后照片"),
        # F9: renumbering from 1 opens a new series.
        ([APPENDIX, "照片1 试验前", "照片2 试验中", "照片1 试验后"], "pass", "均含试验前/中/后"),
    ],
)
def test_stage_completeness_and_grouping(captions, status, fragment):
    finding = _r23(_data(captions))

    assert finding["status"] == status
    assert fragment in finding["message"] + finding["evidence"]


@pytest.mark.parametrize(
    "paragraphs,images,fragment",
    [
        ([APPENDIX, "外包装照片1", "外包装照片2", "内包装照片1", "试验中照片", "试验后照片"], 0, "未嵌入照片"),  # F8
        ([APPENDIX, "照片1", "照片2"], 3, "未标注试验前/中/后"),  # F7
        ([APPENDIX, "照片宽5cm"], 3, "未识别到附录照片题注"),  # F1
    ],
)
def test_skip_cases(paragraphs, images, fragment):
    finding = _r23(_data(paragraphs, images=images))

    assert finding["status"] == "skip"
    assert fragment in finding["message"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="customer sample not mounted")
def test_real_draft_numbering_is_consecutive_but_completeness_skips():
    finding = _r23(parse_docx(SAMPLE))

    assert finding["status"] == "skip"
    assert "序号连续" in finding["message"] and "未嵌入照片" in finding["message"]


# ---------- 第二轮验证反例（F11 / F12） ----------

def test_body_narrative_is_not_a_caption():  # F11
    """「随报告附照片2张。」是正文叙述，不是题注，不能报“序号从 2 开始”。"""
    finding = _r23(_data([APPENDIX, "随报告附照片2张。"]))

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


@pytest.mark.parametrize("narrative", ["随报告附照片2张。", "随报告附照片2张", "详见照片2张"])
def test_narrative_variants_are_not_captions(narrative):  # F11
    finding = _r23(_data([APPENDIX, narrative]))

    assert finding["status"] == "skip"
    assert "未识别到附录照片题注" in finding["message"]


@pytest.mark.parametrize(
    "first,second",
    [
        ("分项1：气味试验", "分项2：外观试验"),
        ("分项1", "分项2"),
        ("1、气味试验", "2、外观试验"),
    ],
)
def test_each_sub_item_needs_its_own_stages(first, second):  # F12
    """分项1 有前/中/后，分项2 只有照片1、照片2 → 分项2 缺前/中/后要报出。"""
    finding = _r23(_data([
        APPENDIX,
        first, "试验前照片", "试验中照片", "试验后照片",
        second, "照片1、照片2",
    ]))

    assert finding["status"] == "fail"
    assert second in finding["message"] + finding["evidence"]
    assert "缺试验前、试验中、试验后照片" in finding["evidence"]


def test_named_prefix_is_a_photo_group_not_a_test_item():  # F15
    """分项写在题注前缀里（分项2照片1）是一个具名照片组，不单独强求前/中/后。"""
    finding = _r23(_data([
        APPENDIX,
        "分项1照片1", "分项1照片2", "分项1试验前照片", "分项1试验中照片", "分项1试验后照片",
        "分项2照片1", "分项2照片2",
    ]))

    assert finding["status"] == "pass"


def test_multiple_numbers_on_one_line_are_all_captions():  # F12
    finding = _r23(_data([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "分项2：外观试验", "外包装照片1、外包装照片2", "试验前照片", "试验中照片", "试验后照片",
    ]))

    # 两个编号都识别到，且两项都完整 → pass；证据里的题注数不能丢。
    assert finding["status"] == "pass"
    assert "题注=8" in finding["evidence"] and "有序号=2" in finding["evidence"]


# ---------- 第三轮验证反例（F13 / F14 / F15） ----------

def test_sample_caption_is_not_a_test_item():  # F13
    """「1号样品照片1」是题注（样品名+照片1），不是一个检验项目，不能报缺前/中/后。"""
    finding = _r23(_data([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "1号样品照片1",
    ]))

    assert finding["status"] == "pass"
    assert "1号样品" not in finding["evidence"]


def test_two_series_on_one_line_each_start_at_one():  # F14
    """「外包装照片1、内包装照片1」是两个不同系列，序号各自从 1 开始，不判重复。"""
    finding = _r23(_data([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "外包装照片1、内包装照片1",
    ]))

    assert finding["status"] == "pass"
    assert "重复" not in finding["evidence"]


@pytest.mark.parametrize(
    "captions",
    [
        "外观照片1、外观照片2",
        "分项2照片1、分项2照片2",
        "照片1（外观）、照片2（外观）",
    ],
)
def test_named_series_naming_does_not_flip_verdict(captions):  # F15
    """「外观照片N」「分项2照片N」「照片N（外观）」是同一语义，判定必须一致。"""
    finding = _r23(_data([
        APPENDIX,
        "分项1：气味试验", "试验前照片", "试验中照片", "试验后照片",
        "分项2：外观试验", captions,
    ]))

    assert finding["status"] == "pass"
    assert "缺试验前" not in finding["evidence"]


def test_duplicate_number_within_one_series_still_fails():  # F14 boundary
    """按系列计数不能放过同一系列内的重复号。"""
    finding = _r23(_data([APPENDIX, "外包装照片1、外包装照片2、外包装照片2"]))

    assert finding["status"] == "fail"
    assert "序号 2 重复" in finding["evidence"]
