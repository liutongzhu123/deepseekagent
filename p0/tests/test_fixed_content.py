"""R28 固定注意事项（RA-4.11）：中文 8 条 / 英文 7 条。

覆盖失败清单 F1–F9：缺条逐条报出、顺序/措辞错误报出、中英文按 8/7 条核对。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report
from app.template_rules import FIXED_NOTICES_EN, FIXED_NOTICES_ZH

SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")


def _paragraphs(items: list[str], heading: str = "注  意  事  项") -> list[str]:
    return ["封面", heading] + [f"{index}.{text}" for index, text in enumerate(items, 1)] + [
        "检验单位地址电话："
    ]


def _r28(paragraphs: list[str]) -> dict:
    data = ReportData(file_name="fixed.docx", paragraphs=paragraphs)
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R28")


def test_catalog_and_complete_chinese_notices_pass():
    assert ("R28", "固定注意事项",
            "中文 8 条/英文 7 条固定注意事项逐条核对：缺条、顺序或措辞错误逐条报出") in RULES
    finding = _r28(_paragraphs(list(FIXED_NOTICES_ZH)))
    assert finding["status"] == "pass"
    assert finding["message"] == "中文固定注意事项 8 条逐条一致"
    assert "语言=zh; 应有=8; 实际=8; 缺=0; 顺序=0; 措辞=0; 多余=0" in finding["evidence"]

    # F7：弯引号、全角标点差异归一化后仍 pass，不误报措辞。
    variant = [text.replace("“", '"').replace("”", '"') for text in FIXED_NOTICES_ZH]
    assert _r28(_paragraphs(variant))["status"] == "pass"

    parens = ["封面", "注 意 事 项"] + [f"（{i}）{t}" for i, t in enumerate(FIXED_NOTICES_ZH, 1)]
    assert _r28(parens + ["检验单位地址电话："])["status"] == "pass"


@pytest.mark.parametrize("items, missing", [
    (list(FIXED_NOTICES_ZH[:4]) + list(FIXED_NOTICES_ZH[5:]), "缺第 5 条"),
    # F3：删掉中间一条后重新编号，编号连续也不能漏报。
    (list(FIXED_NOTICES_ZH[:2]) + list(FIXED_NOTICES_ZH[3:]), "缺第 3 条"),
])
def test_missing_item_is_reported_individually(items, missing):
    finding = _r28(_paragraphs(items))

    assert finding["status"] == "fail"
    assert missing in finding["message"]
    assert "缺=1" in finding["evidence"]


def test_swapped_items_report_order_error():
    items = list(FIXED_NOTICES_ZH)
    items[2], items[3] = items[3], items[2]

    finding = _r28(_paragraphs(items))

    assert finding["status"] == "fail"
    assert "顺序错误" in finding["message"]
    assert "顺序=1" in finding["evidence"]


def test_wording_and_extra_items_are_reported():
    items = list(FIXED_NOTICES_ZH)
    items[3] = "报告修改无效。"

    finding = _r28(_paragraphs(items))
    assert finding["status"] == "fail"
    assert "第 4 条措辞不符" in finding["message"]
    assert "应为「报告涂改无效。」" in finding["message"]
    assert "实际「报告修改无效。」" in finding["message"]
    assert "措辞=1" in finding["evidence"]

    finding = _r28(_paragraphs(list(FIXED_NOTICES_ZH) + ["额外条款。"]))
    assert "第 9 条为多余内容" in finding["message"]
    assert "多余=1" in finding["evidence"]


def test_english_uses_seven_items_and_missing_is_reported():
    finding = _r28(_paragraphs(list(FIXED_NOTICES_EN), heading="NOTICE"))
    assert finding["status"] == "pass"
    assert finding["message"] == "英文固定注意事项 7 条逐条一致"
    assert "语言=en; 应有=7; 实际=7" in finding["evidence"]

    finding = _r28(_paragraphs(list(FIXED_NOTICES_EN[:-1]), heading="NOTICE"))
    assert finding["status"] == "fail"
    assert "缺第 7 条" in finding["message"]
    assert "语言=en; 应有=7; 实际=6" in finding["evidence"]


def test_missing_or_empty_notice_section_is_not_pass():
    finding = _r28(["封面", "目录"])
    assert finding["status"] == "skip"
    assert "未识别到注意事项段落" in finding["message"]

    # F2：有标题没有编号条目，按应有条数逐条报缺。
    finding = _r28(["封面", "注 意 事 项", "检验单位地址电话："])
    assert finding["status"] == "fail"
    assert finding["message"].count("缺第 ") == 8


@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="真实草稿样例目录不存在")
def test_real_byd_drafts_have_complete_fixed_notices():
    drafts = [path for path in sorted(SAMPLE_DIR.glob("*.docx")) if not path.name.startswith(".")]
    assert len(drafts) == 12
    statuses = {"pass": 0, "fail": 0, "skip": 0}
    for path in drafts:
        finding = next(
            item for item in audit_report(parse_docx(str(path)))["findings"]
            if item["rule_id"] == "R28"
        )
        statuses[finding["status"]] += 1
        assert finding["evidence"] == "语言=zh; 应有=8; 实际=8; 缺=0; 顺序=0; 措辞=0; 多余=0"
    assert statuses == {"pass": 12, "fail": 0, "skip": 0}
