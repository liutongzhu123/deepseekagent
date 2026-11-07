"""Layout variants found in the customer standards (synthetic text only)."""
from __future__ import annotations

from app.standard_parser import (
    _clean_lines,
    _detect_headings,
    _section_number,
    parse_standard,
)


def test_combined_method_and_requirement_section_is_parsed():
    """A single 5 试验方法和技术要求 chapter carries both fields (J7111029E)."""
    body = "\n".join([
        "5 试验方法和技术要求 Test Method And Technical Requirements",
        "5.1 电气负荷 Electrical loads",
        "5.1.1 通用要求 General requirement",
        "对DUT进行测试的试验条件应符合本标准。",
        "5.1.2 工作电压 Operating voltage",
        "工作电压范围见表1。",
        "5.1.3 启动脉冲 Start pulses",
        "5.1.3.1 试验目的 Test aim",
        "验证启动行为。",
        "5.1.3.2 试验参数 Test parameters",
        "试验参数见表2。",
        "5.1.3.3 试验方法 Test methods",
        "按 ISO 16750-2 执行。",
        "5.1.3.4 技术要求 Technical Requirement",
        "功能状态应为A级。",
        "5.4 化学负荷试验 Chemical load test",
        "5.4.1 试验目的 Test aim",
        "验证耐化学性。",
        "5.4.2 试验参数 Test parameters",
        "试验参数见表3。",
        "5.4.3 试验方法 Test methods",
        "按 ISO 16750-5 执行。",
        "5.4.4 技术要求 Technical Requirement",
        "表面不应开裂。",
    ])
    result = parse_standard(["Q/TEST 1029-2024\n电动零部件通用规范", body], "combined.pdf")
    assert result["mapping_source"] == "combined-sections"
    items = {item["clause"]: item for item in result["items"]}
    assert set(items) == {"5.1.1", "5.1.2", "5.1.3", "5.4"}
    assert items["5.1.3"]["method_clause"] == "5.1.3.3"
    assert "ISO 16750-2" in items["5.1.3"]["method"]
    assert "功能状态应为A级" in items["5.1.3"]["requirement"]
    assert items["5.4"]["method_clause"] == "5.4.3"
    assert "ISO 16750-5" in items["5.4"]["method"]
    assert "表面不应开裂" in items["5.4"]["requirement"]


def test_per_chapter_requirement_and_method_is_parsed():
    """Each test chapter has its own x.1 技术要求 / x.2 试验方法 (J7110779E)."""
    body = "\n".join([
        "6 瞬态传导发射（CE01） Transient Emission（CE01）",
        "6.1 技术要求 Technical Requirements",
        "瞬态发射应满足表5。",
        "6.2 试验方法 Test Method",
        "按 ISO 7637-2 执行。",
        "7 电磁传导发射（CE02） Conducted Emission（CE02）",
        "7.1 技术要求 Technical Requirements",
        "传导发射应满足表6。",
        "7.2 试验方法 Test Method",
        "按 CISPR 25 执行。",
    ])
    result = parse_standard(["Q/TEST 779-2023\n乘用车电磁兼容规范", body], "chapter.pdf")
    assert result["mapping_source"] == "chapter-items"
    assert [item["clause"] for item in result["items"]] == ["6.1", "7.1"]
    first, second = result["items"]
    assert first["method_clause"] == "6.2" and "ISO 7637-2" in first["method"]
    assert "瞬态发射" in first["requirement"]
    assert second["method_clause"] == "7.2" and "CISPR 25" in second["method"]


def test_generic_requirement_alias_does_not_match_general_requirements():
    lines = _clean_lines(["4 一般要求\n4.1 基本要求\n5 试验方法\n5.1 环境试验"], "")
    assert _section_number(_detect_headings(lines), "技术要求") is None
    lines = _clean_lines(["4 要求\n4.1 基本要求\n5 试验方法\n5.1 环境试验"], "")
    assert _section_number(_detect_headings(lines), "技术要求") == "4"


def test_grouped_method_only_section_exposes_leaf_clauses():
    lines = ["5 试验方法 Test methods"]
    groups = {
        "5.1": ("一般试验条件", ["温度", "湿度"]),
        "5.2": ("功能检测", ["自检", "巡检", "点火"]),
        "5.3": ("环境试验", ["高温", "低温", "温度循环", "振动", "防水"]),
    }
    for group_no, (group_name, leaves) in groups.items():
        lines.append(f"{group_no} {group_name}")
        for index, name in enumerate(leaves, start=1):
            lines.append(f"{group_no}.{index} {name}试验")
            lines.append(f"{name}试验按相应方法进行。")
    result = parse_standard(["\n".join(lines)], "methods.pdf")
    assert result["mapping_source"] == "method-only"
    assert result["summary"]["items"] == 10
    assert result["items"][0]["clause"] == "5.1.1"
    assert result["items"][-1]["clause"] == "5.3.5"
