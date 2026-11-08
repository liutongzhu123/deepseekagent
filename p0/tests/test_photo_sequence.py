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
