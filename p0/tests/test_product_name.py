"""R29 产品名称异常识别（RA-5.2）。

失败场景 F1–F8 见 ``p0/app/format_rules.py``；验收三条：产品名称与检测项目明显不符
能报出、纯编号类名称（如 001）能报出、正常名称不报。真实产品名与检测项目是否适配
（如“后视镜”做“气味”试验）不在确定性规则里判，避免误报。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")


def _r29(sample_name: str = "", item: str = "") -> dict:
    data = ReportData(file_name="product.docx", sample_name=sample_name, item=item)
    findings = [finding for finding in audit_report(data)["findings"] if finding["rule_id"] == "R29"]
    assert len(findings) == 1, "R29 应只运行一次"
    return findings[0]


def test_rule_is_in_catalog():
    assert ("R29", "产品名称异常识别",
            "样品名称须能识别产品种类：纯编号/代号、填成检测项目或仅通用词时报出；正常名称不报") in RULES


@pytest.mark.parametrize("name", ["001", "0001", "0", "Ａ０１", "A-001", "SN2024", "2024-01", "001号样品", "A001-1"])
def test_pure_identifier_names_are_reported(name):
    # 验收：纯编号类名称（如 001）能报出。
    finding = _r29(name, "气味")
    assert finding["status"] == "fail" and finding["severity"] == "medium"
    assert "编号/代号" in finding["message"] and "无法判定产品种类" in finding["message"]
    assert f"样品名称={name}" in finding["evidence"]
    assert finding["location"]["section"] == "样品名称"


@pytest.mark.parametrize("name", ["气味", "气味试验", "气味检测样品", "外观检查", "拉伸强度", "门板尺寸", "气味等级"])
def test_test_item_as_product_name_is_reported(name):
    finding = _r29(name, "气味")
    assert finding["status"] == "fail"
    assert ("检测项目" in finding["message"] or "检验项目" in finding["message"])
    assert "无法判定产品种类" in finding["message"]


def test_product_name_is_another_test_item_is_reported_as_mismatch():
    # 验收：产品名称与检测项目明显不符能报出（名称=拉伸强度，检验项目=气味）。
    finding = _r29("拉伸强度", "气味")
    assert finding["status"] == "fail" and "明显不符" in finding["message"]
    assert "拉伸强度" in finding["message"] and "气味" in finding["message"]
    assert "检验项目=气味" in finding["evidence"]


def test_product_name_equal_to_report_item_is_reported():
    finding = _r29("气味", "气味")
    assert finding["status"] == "fail" and "与检验项目相同" in finding["message"]


def test_product_name_matching_one_item_of_item_list_is_reported():
    finding = _r29("拉伸强度", "气味、拉伸强度")
    assert finding["status"] == "fail" and "检验项目相同" in finding["message"]


@pytest.mark.parametrize("name", ["样品", "产品", "试样", "样件", "零部件", "零件", "总成", "材料"])
def test_generic_names_are_reported(name):
    finding = _r29(name, "气味")
    assert finding["status"] == "fail"
    assert "通用词" in finding["message"] and "无法判定产品种类" in finding["message"]


@pytest.mark.parametrize(("name", "item"), [
    ("后视镜", "气味"),  # 验收：正常名称不报
    ("袋子-100L", "气味"), ("F610S座椅安全带提醒器总成", "气味"),
    ("PP材料", "气味"), ("安全带", "气味"),  # 真实产品名与检验项目能否适配不在本规则判定
    ("Seat", "气味"),  # 英文产品名
    ("气味瓶", "气味"),  # 含检测项目词，但产品词“瓶”仍可识别种类
    ("座椅气味", "气味"), ("1号支架", "气味"),
])
def test_recognizable_product_names_pass(name, item):
    finding = _r29(name, item)
    assert finding["status"] == "pass" and finding["severity"] == "info"
    assert "可识别" in finding["message"] and f"样品名称={name}" in finding["evidence"]


@pytest.mark.parametrize("name", ["", "XXXXX", "XX", "x", "___", "—", "/", "待填", "待定", "未填", "未知", "无", "N/A"])
def test_empty_placeholder_or_na_name_is_skipped(name):
    # 空缺/占位符由 R14 报出，R29 不重复报；skip 不能冒充 pass。
    finding = _r29(name, "气味")
    assert finding["status"] == "skip"
    assert "R14" in finding["message"] and "无法判定产品种类" in finding["message"]


def test_empty_item_still_reports_test_item_name():
    finding = _r29("气味", "")
    assert finding["status"] == "fail"
    assert "检测项目" in finding["message"] and "无法判定产品种类" in finding["message"]


@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="真实草稿样例目录不存在")
def test_real_byd_drafts_skip_on_placeholder_names():
    drafts = [path for path in sorted(SAMPLE_DIR.glob("*.docx")) if not path.name.startswith(".")]
    assert len(drafts) == 12
    statuses = Counter()
    for path in drafts:
        finding = next(item for item in audit_report(parse_docx(str(path)))["findings"] if item["rule_id"] == "R29")
        statuses[finding["status"]] += 1
        assert finding["status"] == "skip" and "占位符" in finding["message"]
    assert statuses == {"skip": 12}
