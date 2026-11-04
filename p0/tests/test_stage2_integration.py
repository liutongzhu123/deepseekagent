"""Stage-2 integration: T201/T202/T203 findings reach the API and workbench payload."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, report_store
from app.docx_parser import ReportData
from app.partial_rules import PARTIAL_RULES
from app.rules import audit_report


def _report() -> ReportData:
    return ReportData(
        file_name="stage2.docx",
        cover_number="QA17LX1LDT861",
        header_numbers=["QA17LX1LDT861"] * 5,
        sample_name="后视镜",
        client="演示客户",
        sample_qty="3 件",
        sample_no="TS24LXJ8A-01",
        basis="Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2",
        item="气味",
        conclusion="经检验，该样品气味的检验结果不符合委托单位提供限值的要求。",
        scores=[4.0, 4.0, 3.5],
        reported_mean=None,
        limit=3.3,
        judgment="不符合",
        device_valid="2026-11-24",
        page_totals=[5, 5, 5, 5, 5],
        page_numbers=[1, 2, 3, 4, 5],
        notices=8,
        highlights=0,
        red_runs=0,
        signature_text="批准：张三 审核：李四 主检：王五",
        test_start="2025年03月02日",
        test_end="2025年03月03日",
        template_family="BYD-odor",
        paragraphs=["报告编号：QA17LX1LDT861", "样品名称：后视镜", "检验项目：气味"],
        tables=[],
    )


@pytest.fixture()
def report_env(monkeypatch, tmp_path):
    monkeypatch.setattr(report_store, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    return SimpleNamespace(client=TestClient(main.app), reports=tmp_path / "reports")


def test_stage2_layers_append_without_changing_r01_r11():
    data = _report()
    baseline = {item["rule_id"]: item["status"] for item in audit_report(data)["findings"] if item["rule_id"].startswith("R")}
    result = audit_report(data, stage2=True)
    staged = {item["rule_id"]: item["status"] for item in result["findings"] if item["rule_id"].startswith("R")}

    assert baseline == staged
    ids = [item["rule_id"] for item in result["findings"]]
    for rule_id, _ in PARTIAL_RULES:
        assert rule_id in ids
    assert result["stage2"]["partial"]["count"] == 8
    assert result["stage2"]["dsl"]["version"] >= 1
    assert result["stage2"]["dsl"]["rules"] == 10
    assert result["stage2"]["semantic"]["status"] in {"skipped", "ok", "error"}

    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert by_id["P04"]["status"] == "skip"
    assert by_id["P05"]["status"] == "skip"
    assert by_id["P06"]["status"] == "skip"
    assert "未自动审核" in by_id["P06"]["message"]
    assert by_id["SEM"]["status"] in {"skip", "warn"}
    assert by_id["SEM"]["layer"] == "semantic"


def test_dsl_skips_input_source_without_false_positive():
    result = audit_report(_report(), stage2=True)
    # D09 is the only example rule that needs the task-order inbox; without
    # inputs it must be skipped, never fired as "任务单缺样品编号".
    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert "D09" not in result["stage2"]["dsl"]["hits"]
    assert by_id["D09"]["status"] == "skip"


def test_audit_api_returns_stage2_payload(report_env):
    response = report_env.client.post(
        "/api/v1/reports/audit", files={"file": ("stage2.docx", b"fake-docx")}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stage2"]["partial"]["count"] == 8
    assert payload["stage2"]["dsl"]["total"] == 10
    assert payload["stage2"]["semantic"]["status"] in {"skipped", "ok", "error"}
    layers = {item.get("layer") for item in payload["findings"]}
    assert {"deterministic", "partial", "dsl", "semantic"} <= layers

    detail = report_env.client.get(f"/api/v1/reports/{payload['report_id']}")
    assert detail.status_code == 200
    assert detail.json()["result"]["stage2"]["partial"]["count"] == 8


def test_rules_api_exposes_stage2_catalog(report_env):
    response = report_env.client.get("/api/v1/rules")
    assert response.status_code == 200
    stage2 = response.json()["stage2"]
    assert stage2["partial"]["total"] == 8
    assert stage2["dsl"]["total"] == 10
    assert stage2["semantic"]["enabled"] is False
    assert len(stage2["partial"]["rules"]) == 8
    assert len(stage2["dsl"]["rules"]) == 10
