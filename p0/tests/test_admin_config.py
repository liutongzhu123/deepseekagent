"""Admin rule-configuration store, API and downstream wiring tests."""
from __future__ import annotations

import copy
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import admin_config, auth, main, quote, standard_store
from app.docx_parser import ReportData
from app.rules import audit_report
from app.standard_parser import parse_standard


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config" / "rules.json"
    monkeypatch.setattr(admin_config, "CONFIG_PATH", path)
    admin_config.reset_cache()
    yield path
    admin_config.reset_cache()


@pytest.fixture()
def env(tmp_path, monkeypatch, config_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")
    monkeypatch.setattr(main, "QUOTE_DIR", tmp_path / "quotes")
    return SimpleNamespace(tmp=tmp_path, client=TestClient(main.app))


def _client(username: str, password: str, role: str) -> TestClient:
    if not any(user["username"] == username for user in auth.load_users()):
        auth.create_user(username, password, role=role)
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return client


def _admin(env) -> TestClient:
    return _client("root", "root-pass-1", "admin")


def _report(**overrides) -> ReportData:
    values = dict(
        file_name="sample.docx",
        cover_number="QA17LX1LDT861",
        header_numbers=["QA17LX1LDT861"] * 5,
        scores=[3.5, 3.5, 4.0],
        reported_mean=3.7,
        limit=3.3,
        judgment="不符合",
        notices=8,
        highlights=1,
        red_runs=0,
        embedded_images=1,
        template_family="BYD-odor",
    )
    values.update(overrides)
    return ReportData(**values)


def test_defaults_and_admin_only_access(env):
    viewer = _client("viewer1", "viewer-pass-1", "viewer")
    assert viewer.get("/api/v1/admin/config").status_code == 403
    assert viewer.put("/api/v1/admin/config/report", json={}).status_code == 403

    admin = _admin(env)
    response = admin.get("/api/v1/admin/config")
    assert response.status_code == 200
    data = response.json()
    config = data["config"]
    assert config["revision"] == 0
    assert len(config["report"]["rules"]) == 11
    assert len(config["standard"]["fields"]) == 8
    assert config["quote"]["default_discount"] == 60
    assert len(config["quote"]["crm_columns"]) == 11
    assert data["warning"] == ""


def test_report_rule_update_changes_audit_behaviour(env):
    admin = _admin(env)
    section = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["report"])
    section["mean_tolerance"] = 0.01
    section["expected_notices"] = 10
    section["rules"][7]["enabled"] = False  # R08
    section["rules"][8]["severity"] = "low"  # R09
    section["rules"][1]["title"] = "均值复核（自定义）"

    response = admin.put("/api/v1/admin/config/report", json=section)
    assert response.status_code == 200
    assert response.json()["config"]["revision"] == 1

    config = admin_config.report_config()
    result = audit_report(_report(), config)
    by_id = {item["rule_id"]: item for item in result["findings"]}
    # 3.666 -> 3.7 vs reported 3.7 would pass; use a value just outside 0.01.
    assert audit_report(_report(reported_mean=3.75), config)["findings"][1]["status"] == "fail"
    assert by_id["R08"]["status"] == "skip"
    assert "停用" in by_id["R08"]["message"]
    assert by_id["R09"]["severity"] == "low"
    assert by_id["R02"]["title"] == "均值复核（自定义）"

    catalog = admin.get("/api/v1/rules").json()
    assert catalog["enabled"] == 10
    assert {rule["id"]: rule for rule in catalog["rules"]}["R08"]["enabled"] is False

    events = admin.get("/api/v1/audit/events?limit=20").json()["events"]
    assert any(
        event.get("event") == "admin_config_updated" and event.get("section") == "report"
        for event in events
    )
    assert json.loads((env.tmp / "config" / "rules.json").read_text("utf-8"))["revision"] == 1


def test_expected_notices_zero_disables_the_count_check():
    config = copy.deepcopy(admin_config.default_config()["report"])
    config["expected_notices"] = 0
    result = audit_report(_report(notices=8), config)
    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert by_id["R08"]["status"] == "skip"
    assert "不校验" in by_id["R08"]["message"]


def test_invalid_sections_are_rejected_and_reset_restores_defaults(env):
    admin = _admin(env)
    section = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["report"])
    section["rules"][0]["severity"] = "critical"
    assert admin.put("/api/v1/admin/config/report", json=section).status_code == 400

    quote_section = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["quote"])
    quote_section["default_discount"] = 120
    assert admin.put("/api/v1/admin/config/quote", json=quote_section).status_code == 400

    reset = admin.post("/api/v1/admin/config/report/reset")
    assert reset.status_code == 200
    report = reset.json()["config"]["report"]
    assert report["mean_tolerance"] == 0.05
    assert all(rule["enabled"] for rule in report["rules"])
    assert reset.json()["config"]["revision"] == 1


def test_audit_and_quote_endpoints_use_saved_config(env, monkeypatch, tmp_path):
    admin = _admin(env)
    report = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["report"])
    report["rules"][7]["enabled"] = False
    assert admin.put("/api/v1/admin/config/report", json=report).status_code == 200
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    response = admin.post(
        "/api/v1/reports/audit", files={"file": ("sample.docx", b"fake-docx")}
    )
    assert response.status_code == 200
    by_id = {item["rule_id"]: item for item in response.json()["findings"]}
    assert by_id["R08"]["status"] == "skip"

    quote_section = copy.deepcopy(
        admin.get("/api/v1/admin/config").json()["config"]["quote"]
    )
    quote_section["headers"]["item"] = ["测试项目"]
    assert admin.put("/api/v1/admin/config/quote", json=quote_section).status_code == 200
    source = tmp_path / "DVP.xlsx"
    _dvp(source)
    response = admin.post(
        "/api/v1/quotes/parse", files={"file": ("DVP.xlsx", source.read_bytes())}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["item_name"] == "调角器同步性能"
    assert response.json()["discount_percent"] == 60


def _standard_pages() -> list[str]:
    return [
        "Q/T 900-2026\n演示标准\n2026-01-01 发布",
        "5 性能指标\n5.1 外观\n外观无缺陷。\n5.2 密封性\n密封良好。",
        "6 检测方法\n6.1 外观检测\n目测。\n6.2 密封检测\n加压检查。",
        "5.1 6.1\n5.2 6.2",
    ]


def test_standard_rules_change_aliases_required_fields_and_export(env):
    admin = _admin(env)
    section = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["standard"])
    section["section_aliases"] = {
        "技术要求": ["性能指标"],
        "试验方法": ["检测方法"],
    }
    for field in section["fields"]:
        if field["key"] == "group":
            field["required"] = True
        if field["key"] == "requirement":
            field["label"] = "判定要求（自定义）"
        if field["key"] == "clause":
            field["export"] = False
    assert admin.put("/api/v1/admin/config/standard", json=section).status_code == 200

    parsed = parse_standard(
        _standard_pages(), "演示标准.pdf", rule_config=admin_config.standard_config()
    )
    assert parsed["summary"]["items"] == 2
    assert parsed["mapping_source"] == "item-table"
    assert all(item["review_required"] for item in parsed["items"])
    assert any(
        entry["field"] == "group"
        for item in parsed["items"]
        for entry in item["review_fields"]
    )

    record = standard_store.create(parsed, b"%PDF-fake", "演示标准.pdf")
    export = admin.get(f"/api/v1/standards/{record['id']}/export.xlsx")
    assert export.status_code == 200
    workbook = load_workbook(BytesIO(export.content))
    headers = [cell.value for cell in workbook["试验方法明细"][1]]
    assert "判定要求（自定义）" in headers
    assert "技术要求条款" not in headers


def _dvp(path) -> None:
    workbook = Workbook()
    ws = workbook.active
    ws.title = "DVP"
    ws["C8"] = "测试项目"
    ws["R9"] = "含税单价"
    ws["S9"] = "数量"
    ws["T9"] = "单位"
    ws["C11"] = "调角器同步性能"
    ws["R11"] = 100
    ws["S11"] = 2
    ws["T11"] = "席位"
    workbook.save(path)


def test_multiformat_quote_path_uses_custom_header_aliases(tmp_path):
    config = copy.deepcopy(admin_config.default_config()["quote"])
    config["default_discount"] = 55
    config["product_tax_rate"] = 0.1
    config["headers"]["item"] = ["自定义项目"]
    config["headers"]["price"] = ["自定义单价"]
    config["unit_map"] = {"席位": "人天"}
    source = tmp_path / "DVP.xlsx"
    workbook = Workbook()
    ws = workbook.active
    ws.title = "DVP"
    ws["C8"] = "自定义项目"
    ws["R9"] = "自定义单价"
    ws["S9"] = "数量"
    ws["T9"] = "单位"
    ws["C11"] = "自定义试验项目"
    ws["R11"] = 100
    ws["S11"] = 2
    ws["T11"] = "席位"
    workbook.save(source)

    result = quote.parse_quote_bytes(
        source.read_bytes(), "DVP.xlsx", rule_config=config
    )
    assert result.lines[0].item_name == "自定义试验项目"
    assert result.lines[0].unit == "人天"
    assert result.lines[0].discount == 55
    assert result.product_tax_rate == 0.1


def test_quote_rules_change_headers_units_tax_and_crm_columns(config_path, tmp_path):
    config = copy.deepcopy(admin_config.default_config()["quote"])
    config["default_discount"] = 50
    config["product_tax_rate"] = 0.1
    config["service_tax_rate"] = 0.05
    config["headers"]["item"] = ["测试项目"]
    config["headers"]["price"] = ["含税单价"]
    config["unit_map"] = {"席位": "人天"}
    config["crm_columns"] = [
        {"source": "item_name", "label": "试验项目（自定义）", "enabled": True},
        {"source": "unit", "label": "单位", "enabled": True},
        {"source": "quantity", "label": "数量", "enabled": False},
    ]

    source = tmp_path / "DVP.xlsx"
    _dvp(source)
    result = quote.parse_dvp(source, rule_config=config)
    assert result.discount == 50
    assert result.product_tax_rate == 0.1
    assert result.service_tax_rate == 0.05
    assert result.lines[0].unit == "人天"
    assert result.lines[0].discount == 50

    crm = quote.write_crm_xlsx(tmp_path / "crm.xlsx", result, config["crm_columns"])
    sheet = load_workbook(crm)["CRM报价草稿"]
    assert [cell.value for cell in sheet[1]] == ["试验项目（自定义）", "单位"]
    assert sheet["A2"].value == "调角器同步性能"
