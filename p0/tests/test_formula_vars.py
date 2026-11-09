"""SP_FORMULAVAR (SP-6.2): variable definitions, executable rules, missing."""
from app.standard_extract.formula import (
    apply_formulas, evaluate_formula, extract_text_formulas)


def _e(text: str) -> list[dict]:
    return extract_text_formulas(text, "6.1", "5.1")


def test_variables_separators_and_unused_definitions():
    entry = _e("试验结果按式（1）计算：x=(a+b)/2。"
               "式中：a——第一次测量值，b——第二次测量值；n——次数。")[0]
    assert [item["symbol"] for item in entry["variables"]] == ["a", "b", "n"]
    assert (entry["uses"], entry["missing_variables"]) == (["a", "b"], [])
    entry = _e("变化率=（A-B）/B×100%。式中：A——值；B——值；C——备注。")[0]
    assert [item["symbol"] for item in entry["variables"]] == ["A", "B", "C"]
    assert entry["missing_variables"] == []


def test_executable_percentage_rule_and_missing_variable():
    entry = _e("变化率按式（1）计算：变化率=（A-B）/B×100%。"
               "式中：A——实测值；B——基准值。")[0]
    assert (entry["target"], entry["uses"]) == ("变化率", ["A", "B"])
    assert entry["rule"]["expression"] == "(A-B)/B*100"
    assert evaluate_formula(entry, {"A": 120, "B": 100})["value"] == 20.0
    entry = _e("变化率=（A-B）/B×100%。式中：A——实测值。")[0]
    assert entry["missing_variables"] == ["B"]
    assert entry["rule"]["status"] == "missing_variables"
    assert evaluate_formula(entry, {"A": 1})["missing"] == ["B"]


def test_fullwidth_operators_and_sqrt_are_normalised():
    entry = _e("按式（2）计算：u＝√（a²＋b²）。式中：a——A类；b——B类。")[0]
    assert entry["rule"]["expression"] == "sqrt(a**2+b**2)"
    assert evaluate_formula(entry, {"a": 3, "b": 4})["value"] == 5.0


def test_series_mean_subscript_coverage_and_missing_base():
    entry = _e("试验结果按式（1）计算：x=(x1+x2+...+xn)/n。"
               "式中：x——每次试验结果；n——试验次数。")[0]
    assert entry["missing_variables"] == []
    assert _e("x̄=(α+β)/2。式中：x̄——均值；α——A；β——B。")[0]["uses"] == ["α", "β"]
    assert _e("x=(xi+xj+xk)/3。式中：x——每次试验结果。")[0]["missing_variables"] == []
    assert entry["rule"]["expression"] == "mean(x)"
    assert evaluate_formula(entry, {"x": [1, 2, 3]})["value"] == 2.0
    assert _e("x=(x1+x2+...+xn)/n。式中：n——试验次数。")[0][
        "missing_variables"] == ["x"]


def test_kind_only_mean_gets_template_rule():
    entry = _e("取三次试验结果的算术平均值作为试验结果。"
               "式中：x——每次试验结果；n——试验次数。")[0]
    assert (entry["kind"], entry["rule"]["expression"]) == ("mean", "mean(x)")
    assert evaluate_formula(entry, {"x": [1, 2, 3]})["value"] == 2.0


def test_reference_only_formula_and_unsafe_expression():
    entry = _e("试验结果按式（3）计算。")[0]
    assert (entry["status"], entry["rule"]["status"]) == (
        "not_extracted", "not_extracted")
    entry = _e("x=(a).__class__。式中：a——值。")[0]
    assert entry["rule"]["status"] == "not_executable"


def test_evaluation_reports_missing_values_and_zero_division():
    entry = _e("变化率=（A-B）/B×100%。式中：A——值；B——值。")[0]
    assert evaluate_formula(entry, {"A": 1, "B": 0})["status"] == "error"
    assert evaluate_formula(entry, {"A": 1})["status"] == "missing_values"


def test_apply_formulas_summary_reports_missing_variables():
    text = "变化率=（A-B）/B×100%。式中：A——实测值。"
    items = [{"clause": "5.1", "method_clause": "6.1", "requirement": "", "method": text}]
    _, summary = apply_formulas(items, [("5.1 变化率", 1), (text, 1)])
    assert (summary["formulas_executable"], summary["formulas_missing_variables"]) == (0, 1)


def test_equation_without_variable_definitions_is_ignored():
    for text in ("注：1）脉冲PRR=217Hz，PD=577usec。", "T=Tmin。", "Itest=Iblock。"):
        assert _e(text) == []


def test_trailing_comma_equation_is_consistent_and_reports_missing_values():
    entry = _e("变化率=（A-B）/B×100%，式中：A——值；B——值。")[0]
    assert entry["rule"]["status"] == "executable"
    assert evaluate_formula(entry, {"A": 120, "B": 100})["value"] == 20.0
    assert evaluate_formula(entry, {})["status"] == "missing_values"
    assert set(entry["rule"]["inputs"]) == {"A", "B"}


def test_undefined_multi_letter_variable_is_reported():
    entry = _e("y=Tmin+1。式中：x——值。")[0]
    assert entry["missing_variables"] == ["Tmin"]
    assert entry["rule"]["inputs"] == ["Tmin"]


def test_unlisted_executable_name_reports_missing_instead_of_keyerror():
    result = evaluate_formula(
        {"status": "executable", "expression": "Tmin", "inputs": []}, {})
    assert (result["status"], result["missing"]) == ("missing_values", ["Tmin"])


def test_multi_argument_comma_is_not_a_clause_boundary():
    """F14: max(a,b) keeps its argument comma; the clause comma still cuts."""
    entry = _e("x=max(a,b)。式中：a——值；b——值。")[0]
    assert (entry["expression"], entry["uses"]) == ("max(a,b)", ["a", "b"])
    assert entry["rule"]["status"] == "executable"
    assert evaluate_formula(entry, {"a": 3, "b": 4})["value"] == 4
    entry = _e("x=max（a，b）。式中：a——值；b——值。")[0]
    assert entry["rule"]["status"] == "executable"
    assert evaluate_formula(entry, {"a": 3, "b": 4})["value"] == 4
    # A top-level comma still ends the equation, as F12 documented.
    entry = _e("变化率=（A-B）/B×100%，式中：A——值；B——值。")[0]
    assert entry["expression"] == "（A-B）/B×100%"


def test_annotation_after_shi_zhong_is_not_a_formula():
    """F15: the 式中 definitions belong to the preceding equation, not to 注."""
    entries = _e("6.5 按式（1）计算：x=(a+b)/n。"
                 "式中：x—平均值；a—测量值；b—测量值；n—次数。"
                 "注：1）脉冲PRR=217Hz，PD=577usec。")
    assert [(entry["target"], entry["expression"]) for entry in entries] == [
        ("x", "(a+b)/n")]


def test_annotation_before_shi_zhong_is_not_a_formula():
    entries = _e("注：1）脉冲PRR=217Hz。x=(a+b)/n。"
                 "式中：x—平均值；a—测量值；b—测量值；n—次数。")
    assert [entry["target"] for entry in entries] == ["x"]


def test_second_equation_with_its_own_shi_zhong_is_kept():
    """F16: each 式中 group defines its own equation; the second is not dropped."""
    text = ("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "y=(a+b)/n。式中：y—平均值；a—测量值；b—测量值；n—次数。")
    entries = _e(text)
    assert [(entry["target"], entry["rule"]["status"]) for entry in entries] == [
        ("x", "executable"), ("y", "executable")]
    assert [item["symbol"] for item in entries[1]["variables"]] == [
        "y", "a", "b", "n"]
    assert evaluate_formula(entries[1], {"a": 1, "b": 2, "n": 3})["value"] == 1.0
    # The second group uses its own definitions, not the first group's.
    entries = _e("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
                 "y=(c+d)/m。式中：y—平均值；c—测量值；d—测量值；m—次数。")
    second = entries[1]
    assert (second["target"], second["expression"]) == ("y", "(c+d)/m")
    assert [item["symbol"] for item in second["variables"]] == [
        "y", "c", "d", "m"]
    assert (second["rule"]["status"], second["missing_variables"]) == (
        "executable", [])
    assert evaluate_formula(second, {"c": 3, "d": 5, "m": 2})["value"] == 4.0


def test_equation_without_its_own_shi_zhong_after_a_group_is_skipped():
    """F15: definitions do not leak into a later sentence that has no 式中."""
    text = ("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "y=(a+b)/n。")
    assert [entry["target"] for entry in _e(text)] == ["x"]


def test_annotation_between_groups_does_not_borrow_next_definitions():
    """F18: an unprefixed PRR=217Hz between two 式中 groups is not a formula."""
    text = ("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "PRR=217Hz，PD=577usec。"
            "z=(e+f)/g。式中：z—平均值；e—测量值；f—测量值；g—测量值。")
    entries = _e(text)
    assert [(entry["target"], entry["expression"]) for entry in entries] == [
        ("x", "(a+b)/n"), ("z", "(e+f)/g")]
    second = entries[1]
    assert [item["symbol"] for item in second["variables"]] == [
        "z", "e", "f", "g"]
    assert (second["missing_variables"], second["rule"]["status"]) == (
        [], "executable")
    assert evaluate_formula(second, {"e": 1, "f": 2, "g": 3})["value"] == 1.0


def test_annotation_after_the_group_equation_is_not_a_formula():
    """F18: the annotation position inside the group does not change the result."""
    text = ("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "z=(e+f)/g。PRR=217Hz，PD=577usec。"
            "式中：z—平均值；e—测量值；f—测量值；g—测量值。")
    assert [entry["target"] for entry in _e(text)] == ["x", "z"]


def test_constant_annotation_does_not_borrow_definitions():
    """F18: a bare number (PRR=217) is not a formula even before a 式中."""
    text = ("x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "PRR=217。式中：z—平均值；e—测量值。")
    assert [entry["target"] for entry in _e(text)] == ["x"]


def test_equations_sharing_one_shi_zhong_use_their_common_definitions():
    """F18: equations that reference the group's symbols keep sharing it."""
    text = ("x=(a+b)/n。y=(c+d)/m。"
            "式中：a—测量值；b—测量值；c—测量值；d—测量值；n—次数；m—次数。")
    entries = _e(text)
    assert [(entry["target"], entry["rule"]["status"]) for entry in entries] == [
        ("x", "executable"), ("y", "executable")]
    assert all([item["symbol"] for item in entry["variables"]] == [
        "a", "b", "c", "d", "n", "m"] for entry in entries)
    assert evaluate_formula(entries[0], {"a": 1, "b": 2, "n": 3})["value"] == 1.0
    assert evaluate_formula(entries[1], {"c": 3, "d": 5, "m": 2})["value"] == 4.0


def test_one_shi_zhong_still_defines_several_preceding_sentences():
    text = ("试验结果按加权平均值计算；变化率按式（2）计算，"
            "变化率=（A-B）/B×100%。式中：A——实测值；B——基准值。")
    entries = _e(text)
    assert {entry["kind"] for entry in entries} == {"weighted_mean", "percentage"}
    assert all([item["symbol"] for item in entry["variables"]] == ["A", "B"]
               for entry in entries)


def test_incomplete_expression_is_skipped_not_kept_as_half():
    """F17: a formula that cannot be parsed in full is skipped, not truncated."""
    cases = (
        "x=(a+b)/时间。式中：x—平均值；a—测量值；b—测量值。",
        "x=(a+b。式中：x—平均值；a—测量值；b—测量值。",
        "x=max(a,b。式中：a——值；b——值。",
        "x=a+。式中：x—平均值；a—测量值。",
    )
    for text in cases:
        assert _e(text) == [], text


def test_document_level_second_equation_is_kept():
    text = ("6.1 x=(a+b)/n。式中：x—平均值；a—测量值；b—测量值；n—次数。"
            "y=(a+b)/n。式中：y—平均值；a—测量值；b—测量值；n—次数。")
    entries, summary = apply_formulas([], [(text, 1)])
    assert [entry["target"] for entry in entries] == ["x", "y"]
    assert summary["formulas"] == 2
    assert summary["formulas_executable"] == 2
