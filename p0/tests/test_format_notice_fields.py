"""R08 required notice fields: client and test type."""
from __future__ import annotations

from app.docx_parser import ReportData
from app.rules import audit_report


def _data(**kwargs) -> ReportData:
    base = dict(
        file_name="sample.docx",
        notices=8,
        client="比亚迪汽车工业有限公司",
        template_family="BYD-odor",
        template_variant="OTS",
        paragraphs=[],
    )
    base.update(kwargs)
    return ReportData(**base)


def _r08(data: ReportData) -> dict:
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R08")


def test_complete_notice_fields_pass():
    finding = _r08(_data())

    assert finding["status"] == "pass"
    assert finding["title"] == "注意事项完整"


def test_missing_client_fails():
    finding = _r08(_data(client=""))

    assert finding["status"] == "fail"
    assert "委托单位未填写" in finding["message"]


def test_missing_byd_test_type_fails():
    finding = _r08(_data(template_variant=""))

    assert finding["status"] == "fail"
    assert "试验类型未选择" in finding["message"]


def test_notice_section_values_override_empty_fields():
    paragraphs = [
        "注 意 事 项",
        "1. 样品信息以委托单位提供为准。",
        "委托单位：示例委托单位",
        "试验类型：摸底",
        "检验单位地址电话：020-00000000",
    ]
    finding = _r08(_data(client="", template_variant="", paragraphs=paragraphs))

    assert finding["status"] == "pass"
    assert "示例委托单位" in finding["evidence"]
    assert "摸底" in finding["evidence"]


def test_count_mismatch_and_missing_type_combine():
    finding = _r08(_data(notices=7, template_variant=""))

    assert finding["status"] == "fail"
    assert "注意事项应为 8 条，实际 7 条" in finding["message"]
    assert "试验类型未选择" in finding["message"]


def test_no_notices_skips():
    finding = _r08(_data(notices=0))

    assert finding["status"] == "skip"
    assert "未识别到注意事项段落" in finding["message"]
