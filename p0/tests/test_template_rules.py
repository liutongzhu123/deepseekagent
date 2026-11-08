"""RA-4.18 / R11: 按主机厂识别模板，并在错用模板时报出。

分支与 ``app/template_rules.py`` 的 F1–F10 一一对应。
"""
from __future__ import annotations

import app.template_rules as template_rules
from app.docx_parser import ReportData, identify_template
from app.rules import audit_report
from app.template_rules import match_template

JLY = "Q/JLY J7110109C-2017《安全气囊电子控制单元及碰撞传感器技术条件》"
BYD = "Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》"


def _match(**overrides: object):
    base = dict(basis=BYD, item="气味", page_totals=[5, 5, 5, 5, 5], client="比亚迪汽车工业有限公司")
    base.update(overrides)
    return match_template(**base)


def _r11(**overrides: object) -> dict:
    base = dict(file_name="t.docx", basis=BYD, item="气味", page_totals=[5] * 5, client="比亚迪汽车工业有限公司")
    base.update(overrides)
    findings = audit_report(ReportData(**base))["findings"]
    return next(item for item in findings if item["rule_id"] == "R11")


def test_byd_ots_and_moban_pass():
    assert _match().status == "pass" and _match().variant == "OTS"
    assert _match(page_totals=[4, 4, 4, 4]).variant == "摸底"


def test_jili_odor_is_not_misidentified_as_byd():  # F3/F4
    match = _match(basis=JLY, client="")
    assert match.family == "" and match.status == "skip"
    assert identify_template(JLY, "气味", [5]) == ("generic", "")


def test_cross_oem_template_fails_both_ways():  # F1
    assert "错用模板" in _match(basis=JLY, client="", paragraphs=["比亚迪汽车工业有限公司 检验报告"]).message
    assert "错用模板" in _match(paragraphs=["吉利汽车研究院 检验报告"]).message


def test_conflicting_and_multiple_signals_fail():  # F2/F6
    assert "多个主机厂" in _match(basis=JLY, client="比亚迪汽车工业有限公司").message
    assert "多个主机厂模板标记" in _match(basis=JLY, client="", paragraphs=["比亚迪 检验报告", "吉利 检验报告"]).message


def test_unknown_and_variantless_cases():  # F5/F8/F9/F10
    assert _match(basis="GB/T 1234", item="拉伸", page_totals=[3] * 3, client="").status == "warn"
    assert _match(page_totals=[3] * 3).status == "pass"
    assert "变体未识别" in _match(page_totals=[3] * 3).message
    assert _match(page_totals=[]).status == "pass"
    assert match_template().status == "warn"


def test_missing_config_skips(monkeypatch, tmp_path):  # F7
    monkeypatch.setattr(template_rules, "CONFIG_DIR", tmp_path)
    match = _match()
    assert match.status == "skip" and "配置不可用" in match.message


def test_r11_integration():
    assert _r11()["status"] == "pass"
    assert _r11(basis=JLY, client="", paragraphs=["比亚迪 检验报告"])["status"] == "fail"
    assert _r11(basis=JLY, client="")["status"] == "skip"
