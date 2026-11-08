"""B5: rounding rules (roundings[] + rounding_status) and relative limits."""
from app.rule_dsl import generate_judgment_rules
from app.standard_extract.limits import extract_limits
from app.standard_extract.rounding import extract_rounding
from app.standard_parser import parse_standard


def test_extracts_three_rounding_expression_classes():
    found = extract_rounding(
        "结果保留3位有效数字；测量值保留两位小数；按0.5修约，修约方法为四舍六入五单双。"
    )
    by_kind = {entry["kind"]: entry for entry in found}
    assert {"significant_digits", "decimal_places", "interval", "method"} <= set(by_kind)
    assert (by_kind["significant_digits"]["value"], by_kind["decimal_places"]["value"]) == (3, 2)
    assert by_kind["method"]["method"] == "round_half_even"
    # The stated method is attached to the precision rules of the same clause.
    assert by_kind["decimal_places"]["method"] == "round_half_even"


def test_chinese_numerals_places_and_false_positives():
    values = {(entry["kind"], entry["value"], entry["unit"]) for entry in extract_rounding(
        "修约到小数点后两位；精确至0.01mm；精确到0.01。"
    )}
    assert ("interval", 0.01, "mm") in values
    assert extract_rounding(
        "不应大于图样规定值的30%；相对湿度25%~75%；GB/T 8170 数字修约规则。"
    ) == []


def test_relative_baseline_percentage_limit_never_becomes_a_numeric_rule():
    found = extract_limits("密封性不应大于图样规定值的30%。")
    assert len(found) == 1
    entry = found[0]
    assert (entry["kind"], entry["value"], entry["unit"]) == ("upper", 30, "%")
    assert (entry["relative"], entry["reference"], entry["ratio"]) == (True, "图样规定值", 0.3)
    assert entry["upper"] is None  # symbolic until the referenced base is known
    draft = generate_judgment_rules([{"clause": "5.2", "name": "密封性", "limits": found}])
    assert draft["rules"] == []
    assert "待人工确认" in draft["pending"][0]["reason"]


def _pages() -> list[str]:
    return [
        "Q/TEST 123-2024\n测试标准\n2024-01-01 发布\n2024-02-01 实施",
        "5  技术要求 Technical requirements\n5.1  外观检查 Appearance inspection\n"
        "结果保留2位小数。\n5.2  密封性能 Tightness\n密封性应良好。",
        "6  试验方法 Test methods\n6.1  外观检查试验 Appearance test\n目测检查。\n"
        "6.2  密封性试验 Tightness test\n按上述要求试验。",
        "7  结果表示\n7.1  结果保留3位有效数字，按四舍五入修约。",
        "5.1 6.1\n5.2 6.2",
    ]


def test_parse_binds_rules_to_items_and_shares_result_section_rules():
    result = parse_standard(_pages(), "测试标准.pdf")
    by_clause = {item["clause"]: item for item in result["items"]}
    direct, shared = by_clause["5.1"]["roundings"], by_clause["5.2"]["roundings"]
    assert by_clause["5.1"]["rounding_status"] == by_clause["5.2"]["rounding_status"] == "extracted"
    assert ("decimal_places", 2, "item") in {
        (entry["kind"], entry["value"], entry["scope"]) for entry in direct
    }
    assert ("significant_digits", 3, "standard") in {
        (entry["kind"], entry["value"], entry["scope"]) for entry in shared
    }
    assert (result["summary"]["roundings"], result["summary"]["items_with_rounding"]) == (3, 2)
    assert (result["summary"]["rounding_significant_digits"],
            result["summary"]["rounding_decimal_places"],
            result["summary"]["rounding_methods"]) == (1, 1, 1)
