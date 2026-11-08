"""R22 检测依据标准年代号：缺年代号报出与非标准号格式校验。"""
from __future__ import annotations

import pytest

from app.docx_parser import ReportData
from app.format_rules import _standard_tokens
from app.rules import RULES, audit_report


def _r22(basis: str = "", paragraphs: list[str] | None = None) -> dict:
    data = ReportData(file_name="standard.docx", basis=basis, paragraphs=paragraphs or [])
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R22")


def test_rule_is_in_catalog():
    assert ("R22", "检测依据标准年代号", "检验依据中的标准号必须带具体年代号，并校验标准号/年代号格式") in RULES


def test_missing_years_are_reported():
    finding = _r22("GB/T 1037、GB/T 2406")

    assert finding["status"] == "fail"
    assert finding["severity"] == "medium"
    assert "GB/T 1037 缺年代号" in finding["message"]
    assert "GB/T 2406 缺年代号" in finding["message"]
    assert "缺年代号=2" in finding["evidence"]
    assert finding["location"]["section"] == "检验依据"


def test_legal_standard_numbers_pass():
    finding = _r22(
        "GB/T 1037-2021、GB 4806.7-2023、DB11/T 1234-2021、QC/T 17-1992、"
        "T/CSTM 00001-2019、Q/BYDQ-A1901.404—2025、ISO 1183-1:2012、"
        "IEC 60068-2-1:2007、ASTM D638-14"
    )

    assert finding["status"] == "pass"
    assert "检测依据含 9 个标准号" in finding["message"]
    assert "Q/BYDQ-A1901.404-2025" in finding["evidence"]
    assert "ASTM D638-14" in finding["evidence"]


def test_enterprise_standard_with_trailing_text_passes():
    finding = _r22("Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2委托单位提供限值：气味等级≤3.3")

    assert finding["status"] == "pass"
    assert "1 个标准号" in finding["message"]


def test_enterprise_standard_without_year_fails():
    finding = _r22("Q/BYDQ-A1901.404《汽车零部件气味性试验方法》")

    assert finding["status"] == "fail"
    assert "Q/BYDQ-A1901.404 缺年代号" in finding["message"]


def test_enterprise_standard_with_letter_led_code_without_year_fails():
    finding = _r22("Q/JLY J7110109C")

    assert finding["status"] == "fail"
    assert "Q/JLY J7110109C 缺年代号" in finding["message"]
    assert "缺年代号=1" in finding["evidence"]


def test_enterprise_standard_with_letter_led_code_and_year_passes():
    finding = _r22("Q/JLY J7111074B-2021")

    assert finding["status"] == "pass"
    assert "1 个标准号" in finding["message"]


def test_legal_standard_followed_by_fullwidth_colon_passes():
    finding = _r22("GB/T 1037-2021：塑料薄膜水蒸气透过率试验方法")

    assert finding["status"] == "pass"
    assert "1 个标准号" in finding["message"]


def test_double_year_colon_is_preserved():
    finding = _r22("GB/T 1037-2021:2015")

    assert finding["status"] == "pass"
    assert "1 个标准号" in finding["message"]


def test_colon_only_joins_a_following_number():
    def tokens(text):
        return [token for token, _, _ in _standard_tokens(text)]

    assert tokens("GB/T 1037-2021：塑料薄膜水蒸气透过率试验方法") == ["GB/T 1037-2021"]
    assert tokens("GB/T 1037-2021:2015") == ["GB/T 1037-2021:2015"]
    assert tokens("Q/BYDQ-A1901.404-2025：气味等级≤3.3") == ["Q/BYDQ-A1901.404-2025"]


@pytest.mark.parametrize(
    ("basis", "needle"),
    [
        ("GB/T 1037-21、GB/T 2406-202", "GB/T 1037-21 年代号应为 4 位数字"),
        ("ISO 1183:12", "ISO 1183:12 年代号应为 4 位数字"),
        ("GB/T 1037-1899", "1899 超出 1900–2099"),
        ("GBT 1037-2021", "GBT 1037-2021 标准号格式不合法（前缀后应为数字）"),
    ],
)
def test_format_problems_fail(basis, needle):
    finding = _r22(basis)

    assert finding["status"] == "fail"
    assert needle in finding["message"]
    assert "缺年代号=0" in finding["evidence"]
    assert "格式不合法=" in finding["evidence"]


def test_mixed_standards_report_only_problems():
    finding = _r22("GB/T 1037-2021、GB/T 2406、GB/T 1040-21")

    assert finding["status"] == "fail"
    assert "检测依据标准问题 2 处" in finding["message"]
    assert "GB/T 2406 缺年代号" in finding["message"]
    assert "GB/T 1040-21 年代号应为 4 位数字" in finding["message"]
    assert "GB/T 1037-2021" not in finding["message"].split("：", 1)[1]
    assert "标准号=3" in finding["evidence"]


def test_duplicate_standard_is_reported_once():
    finding = _r22("GB/T 1037、GB/T 1037")

    assert finding["status"] == "fail"
    assert finding["message"].count("GB/T 1037 缺年代号") == 1
    assert "标准号=1" in finding["evidence"]
    assert "缺年代号=1" in finding["evidence"]


def test_astm_two_digit_year_is_legal():
    finding = _r22("ASTM D638-14、ASTM D638-2014e1")

    assert finding["status"] == "pass"
    assert "2 个标准号" in finding["message"]


@pytest.mark.parametrize(
    ("basis", "needle"),
    [
        ("", "未识别到检验依据"),
        ("方案2 委托单位提供限值：气味等级≤3.3，试验温度 25 ℃", "未识别到标准号"),
    ],
)
def test_skip_cases(basis, needle):
    finding = _r22(basis)

    assert finding["status"] == "skip"
    assert needle in finding["message"]


@pytest.mark.parametrize(
    "paragraphs",
    [["检验依据：GB/T 1037-2021"], ["2.检验依据", "检验依据", "GB/T 1037-2021"]],
)
def test_paragraph_is_used_when_basis_missing(paragraphs):
    finding = _r22("", paragraphs)

    assert finding["status"] == "pass"
    assert "1 个标准号" in finding["message"]
