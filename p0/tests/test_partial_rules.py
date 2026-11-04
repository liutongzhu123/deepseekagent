from app.docx_parser import ReportData
from app.partial_rules import PARTIAL_RULES, evaluate_partial_rules


def _data(**overrides) -> ReportData:
    base = dict(file_name="sample.docx", sample_no="S1", cover_number="R1", test_start="2025-03-02", test_end="2025-03-03", page_totals=[5, 5, 5, 5, 5], toc_entries=[{"title": "检验结果", "page": 3}], template_family="BYD-odor")
    base.update(overrides)
    return ReportData(**base)


def test_eight_partial_rules_are_present():
    findings = evaluate_partial_rules(_data())
    assert [item.rule_id for item in findings] == [rule for rule, _ in PARTIAL_RULES]


def test_missing_data_skips_not_passes():
    findings = {item.rule_id: item for item in evaluate_partial_rules(_data(sample_no="", cover_number=""))}
    for rule in ("P01", "P04", "P05", "P06", "P08"):
        assert findings[rule].status == "skip"
    assert "未自动审核" in findings["P06"].message


def test_time_and_markup_are_evaluated():
    findings = {item.rule_id: item for item in evaluate_partial_rules(_data(test_start="2025-03-04", test_end="2025-03-03", highlights=2))}
    assert findings["P03"].status == "fail"
    assert findings["P07"].status == "fail"
