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
