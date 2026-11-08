from pathlib import Path

import pytest

from app.docx_parser import ReportData, identify_template, parse_docx
from app.rules import audit_report
from eval import report_files

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples" / "byd"


def _report(**overrides) -> ReportData:
    base = dict(file_name="sample.docx", basis="Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》", item="气味")
    base.update(overrides)
    return ReportData(**base)


def test_byd_odor_ots_by_page_count():
    assert identify_template("Q/BYDQ-A1901.404—2025", "气味", [5, 5, 5, 5, 5]) == ("BYD-odor", "OTS")


def test_byd_odor_moban_by_page_count():
    assert identify_template("Q/BYDQ-A1901.404—2025", "气味", [4, 4, 4, 4]) == ("BYD-odor", "摸底")


def test_unknown_template_is_generic():
    assert identify_template("GB/T 1234", "拉伸", [3, 3, 3]) == ("generic", "")


def test_r11_reports_variant():
    result = audit_report(_report(template_family="BYD-odor", template_variant="OTS"))
    finding = next(item for item in result["findings"] if item["rule_id"] == "R11")

    assert finding["status"] == "pass"
    assert "OTS" in finding["message"]


@pytest.mark.skipif(len(report_files(SAMPLES)) != 12, reason="12 BYD drafts not mounted")
def test_twelve_drafts_are_classified():
    variants = []
    for path in report_files(SAMPLES):
        data = parse_docx(path)
        assert data.template_family == "BYD-odor"
        assert data.template_variant in {"OTS", "摸底"}
        finding = next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R11")
        assert finding["status"] == "pass"
        variants.append(data.template_variant)
    assert len(variants) == 12
    assert variants.count("OTS") == 6
    assert variants.count("摸底") == 6
