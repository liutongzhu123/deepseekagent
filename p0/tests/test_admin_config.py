"""Admin rule-configuration store, API and downstream wiring tests."""
from __future__ import annotations

import copy
import json
import multiprocessing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import admin_config, auth, main, quote, standard_store
from app.docx_parser import ReportData
from app.rules import RULES, audit_report
from app.standard_parser import parse_standard


def _write_config_from_worker(start, results, config_path, payload):
    admin_config.CONFIG_PATH = Path(config_path)
    admin_config.reset_cache()
    start.wait(timeout=30)
    try:
        config = admin_config.update_section(
            "business", payload, updated_by="concurrent-worker", expected_revision=0,
        )
        results.put(("ok", config["revision"]))
    except admin_config.ConfigRevisionConflict:
        results.put(("conflict", None))


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


def test_defaults_and_section_access(env):
    viewer = _client("viewer1", "viewer-pass-1", "viewer")
    assert viewer.get("/api/v1/admin/config").status_code == 403
    assert viewer.put("/api/v1/admin/config/report", json={}).status_code == 403
    assert viewer.put("/api/v1/admin/config/business", json={}).status_code == 403

    admin = _admin(env)
    response = admin.get("/api/v1/admin/config")
    assert response.status_code == 200
    data = response.json()
    config = data["config"]
    assert config["revision"] == 0
    assert len(config["report"]["rules"]) == len(RULES)
    assert len(config["standard"]["fields"]) == 8
    assert config["quote"]["default_discount"] == 60
    assert len(config["quote"]["crm_columns"]) == 11
    assert config["business"]["discount_tiers"] == [{"min_amount": 0.0, "discount": 60.0}]
    assert config["business"]["rush"]["enabled"] is False
    assert config["business"]["region_routing"] == {
        "enabled": False, "region_managers": [], "backup_manager_id": "",
    }
    assert data["sections"] == ["report", "standard", "quote", "business"]
    assert data["warning"] == ""


def test_global_rules_are_admin_only_and_business_manager_gets_redacted_read(env):
    admin = _admin(env)
    admin_business = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["business"])
    admin_business["region_routing"] = {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "private-manager"}],
        "backup_manager_id": "private-backup",
    }
    admin_business["blacklist"] = {"companies": ["受限名单企业"]}
    admin_business["whitelist"] = {"companies": ["重点客户企业"], "keywords": ["内部关键词"]}
    admin_business["expected_revision"] = admin_config.load()["revision"]
    admin_business["expected_revision"] = admin_config.load()["revision"]
    assert admin.put("/api/v1/admin/config/business", json=admin_business).status_code == 200

    manager = _client("manager1", "manager-pass-1", "business_manager")
    assert manager.get("/api/v1/admin/config").status_code == 403
    data = manager.get("/api/v1/config/effective")
    assert data.status_code == 200
    assert "region_routing" not in data.json()["config"]["business"]
    assert "blacklist" not in data.json()["config"]["business"]
    assert "whitelist" not in data.json()["config"]["business"]
    assert "受限名单企业" not in data.text
    business = copy.deepcopy(data.json()["config"]["business"])
    business["rush"] = {"enabled": True, "fee_rate": 25, "lead_days": 2}
    business["intermediary"] = {"enabled": True, "block": False, "keywords": ["中介", "代理"]}
    assert manager.put("/api/v1/admin/config/report", json={}).status_code == 403
    assert manager.put("/api/v1/admin/config/quote", json={}).status_code == 403
    assert manager.put("/api/v1/admin/config/business", json=business).status_code == 403
    forbidden_routing = business | {"region_routing": {
        "enabled": True, "region_managers": [{"region": "上海", "manager_id": "secret-manager"}],
        "backup_manager_id": "secret-backup",
    }}
    assert manager.put("/api/v1/admin/config/business", json=forbidden_routing).status_code == 403
    sales_revision = admin_config.load()["revision"]
    assert manager.post(f"/api/v1/admin/config/business/reset?expected_revision={sales_revision}").status_code == 403
    assert admin_config.business_config()["region_routing"] == {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "private-manager"}],
        "backup_manager_id": "private-backup",
    }
    assert admin_config.business_config()["blacklist"]["companies"] == ["受限名单企业"]
    assert admin_config.business_config()["whitelist"]["companies"] == ["重点客户企业"]
    assert "whitelist" not in manager.get("/api/v1/config/effective").json()["config"]["business"]
    assert manager.put("/api/v1/admin/config/business", json=business | {
        "blacklist": {"companies": ["销售尝试改写"]},
    }).status_code == 403
    assert manager.get("/api/v1/admin/config/business/blacklist.csv").status_code == 403
    assert manager.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": "企业名称\\n越权企业", "expected_revision": admin_config.load()["revision"],
    }).status_code == 403


def test_business_rules_validation_and_reset(env):
    admin = _admin(env)
    business = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["business"])
    business["discount_tiers"] = [
        {"min_amount": 0, "discount": 60}, {"min_amount": 10000, "discount": 50}
    ]
    business["rush"] = {"enabled": True, "fee_rate": 20, "lead_days": 3}
    business["intermediary"] = {"enabled": True, "block": True, "keywords": ["中介", "代理", "中介"]}
    business["blacklist"] = {"companies": ["甲公司"]}
    business["whitelist"] = {"companies": ["乙公司"], "keywords": ["集团"]}
    business["expected_revision"] = admin_config.load()["revision"]
    response = admin.put("/api/v1/admin/config/business", json=business)
    assert response.status_code == 200
    saved = admin_config.business_config()
    assert [tier["min_amount"] for tier in saved["discount_tiers"]] == [0, 10000]
    assert saved["intermediary"]["keywords"] == ["中介", "代理"]
    assert saved["blacklist"]["companies"] == ["甲公司"]
    assert saved["whitelist"]["keywords"] == ["集团"]
    business["expected_revision"] = admin_config.load()["revision"]

    bad = copy.deepcopy(business)
    bad["discount_tiers"] = [{"min_amount": 10000, "discount": 50}]
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400
    bad["discount_tiers"] = [
        {"min_amount": 0, "discount": 60}, {"min_amount": 0, "discount": 50}
    ]
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400
    bad["discount_tiers"] = [{"min_amount": 0, "discount": 120}]
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400
    bad = copy.deepcopy(business)
    bad["intermediary"] = {"enabled": True, "block": False, "keywords": []}
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400

    bad = copy.deepcopy(business)
    bad["whitelist"]["keywords"] = [" "] * 101
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400

    bad = copy.deepcopy(business)
    bad["region_routing"] = {
        "enabled": True,
        "region_managers": [
            {"region": "上海", "manager_id": "manager-a"},
            {"region": "上 海", "manager_id": "manager-b"},
        ],
        "backup_manager_id": "backup-manager",
    }
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400
    bad["region_routing"] = {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "manager-a"}],
        "backup_manager_id": "",
    }
    assert admin.put("/api/v1/admin/config/business", json=bad).status_code == 400

    reset = admin.post(f"/api/v1/admin/config/business/reset?expected_revision={admin_config.load()['revision']}")
    assert reset.status_code == 200
    assert reset.json()["config"]["business"] == admin_config.default_config()["business"]


def test_blacklist_csv_import_export_revision_and_audit(env):
    admin = _admin(env)
    current_business = admin_config.business_config()
    missing_revision = admin.put("/api/v1/admin/config/business", json={
        **current_business,
        "blacklist": current_business["blacklist"],
    })
    assert missing_revision.status_code == 422
    payload = {"csv_text": "企业名称\r\n甲公司\r\n+危险格式企业\r\n甲公司\r\n", "mode": "merge", "expected_revision": 0}
    imported = admin.post("/api/v1/admin/config/business/blacklist/import", json=payload)
    assert imported.status_code == 200
    assert imported.json()["blacklist_import"] == {
        "mode": "merge", "input_rows": 3, "added": 2, "removed": 0, "company_count": 2,
    }
    assert admin_config.business_config()["blacklist"]["companies"] == ["甲公司", "+危险格式企业"]

    stale = admin.post("/api/v1/admin/config/business/blacklist/import", json=payload)
    assert stale.status_code == 409
    stale_form = admin.put("/api/v1/admin/config/business", json={
        **admin_config.business_config(), "expected_revision": 0,
        "blacklist": {"companies": ["旧表单覆盖"]},
    })
    assert stale_form.status_code == 409
    malformed = admin.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": "企业名称,其他\n甲公司,值", "mode": "merge", "expected_revision": 1,
    })
    assert malformed.status_code == 400
    empty = admin.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": "企业名称\n", "mode": "replace", "expected_revision": 1,
    })
    assert empty.status_code == 400

    exported = admin.get("/api/v1/admin/config/business/blacklist.csv")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert exported.text.startswith("\ufeff企业名称")
    assert "'+危险格式企业" in exported.text
    roundtrip = admin.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": exported.text, "mode": "replace", "expected_revision": 1,
    })
    assert roundtrip.status_code == 200
    assert admin_config.business_config()["blacklist"]["companies"] == ["甲公司", "+危险格式企业"]

    audit_text = auth.AUDIT_PATH.read_text(encoding="utf-8")
    assert "blacklist_imported" in audit_text
    assert "blacklist_export_requested" in audit_text
    assert '"user": "root"' in audit_text
    assert "甲公司" not in audit_text and "+危险格式企业" not in audit_text


def test_blacklist_import_audit_failure_prevents_mutation(env, monkeypatch):
    admin = _admin(env)
    seeded = admin.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": "企业名称\n甲公司", "mode": "merge", "expected_revision": 0,
    })
    assert seeded.status_code == 200
    revision = admin_config.load()["revision"]
    monkeypatch.setattr(main, "_audit_event", lambda *args, **kwargs: False)
    response = admin.post("/api/v1/admin/config/business/blacklist/import", json={
        "csv_text": "企业名称\n乙公司", "mode": "merge", "expected_revision": revision,
    })
    assert response.status_code == 503
    assert admin_config.business_config()["blacklist"]["companies"] == ["甲公司"]
    direct_update = admin.put("/api/v1/admin/config/business", json={
        **admin_config.business_config(),
        "expected_revision": revision,
        "blacklist": {"companies": ["被阻止的手工名单"]},
    })
    assert direct_update.status_code == 503
    assert admin_config.business_config()["blacklist"]["companies"] == ["甲公司"]
    direct_priority_update = admin.put("/api/v1/admin/config/business", json={
        **admin_config.business_config(),
        "expected_revision": revision,
        "whitelist": {"companies": ["重点客户"], "keywords": ["关键词"]},
    })
    assert direct_priority_update.status_code == 503
    assert admin_config.business_config()["whitelist"]["companies"] == []
    reset = admin.post(f"/api/v1/admin/config/business/reset?expected_revision={revision}")
    assert reset.status_code == 503
    assert admin_config.business_config()["blacklist"]["companies"] == ["甲公司"]


def test_blacklist_revision_conflict_and_corrupt_config_fail_closed(config_path, monkeypatch):
    initial = admin_config.default_config()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
    admin_config.reset_cache()
    with pytest.raises(admin_config.ConfigRevisionConflict):
        admin_config.update_blacklist(["甲公司"], expected_revision=1)
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(admin_config, "_load_cached", lambda: (admin_config.default_config(), "损坏配置"))
    with pytest.raises(admin_config.ConfigError, match="无效"):
        admin_config.update_blacklist(["乙公司"], expected_revision=0)
    assert config_path.read_text(encoding="utf-8") == before


def test_expected_revision_conflict_is_enforced_across_processes(config_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    payloads = [
        {"blacklist": {"companies": ["并发甲"]}},
        {"blacklist": {"companies": ["并发乙"]}},
    ]
    workers = [
        context.Process(target=_write_config_from_worker, args=(start, results, str(config_path), payload))
        for payload in payloads
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=30), results.get(timeout=30)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "ok"]
    admin_config.reset_cache()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["revision"] == 1
    assert persisted["business"]["blacklist"]["companies"] in [["并发甲"], ["并发乙"]]


def test_effective_config_is_readable_by_module_pages(env):
    assert TestClient(main.app).get("/api/v1/config/effective").status_code == 401

    viewer = _client("viewer2", "viewer-pass-2", "viewer")
    response = viewer.get("/api/v1/config/effective")
    assert response.status_code == 200
    data = response.json()
    assert "path" not in data
    config = data["config"]
    assert "region_routing" not in config["business"]
    assert len(config["report"]["rules"]) == len(RULES)
    assert len(config["standard"]["fields"]) == 8
    assert config["quote"]["default_discount"] == 60
    assert config["business"]["discount_tiers"][0]["min_amount"] == 0
    assert data["warning"] == ""


def test_effective_config_tracks_admin_updates(env):
    admin = _admin(env)
    section = copy.deepcopy(admin.get("/api/v1/admin/config").json()["config"]["quote"])
    section["default_discount"] = 55
    assert admin.put("/api/v1/admin/config/quote", json=section).status_code == 200

    data = admin.get("/api/v1/config/effective").json()
    assert data["config"]["revision"] == 1
    assert data["config"]["quote"]["default_discount"] == 55


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
    assert catalog["enabled"] == len(RULES) - 1
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
    auth.create_user("auditor", "audit-password-1", role="technical", projects=["*"])
    auth.set_review_permissions(
        "auditor", ["report:audit"], granted_by="root",
        reason="测试账号逐账号确认报告审核职责",
    )
    auditor = _client("auditor", "audit-password-1", "technical")
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    response = auditor.post(
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
    auth.create_user("quote-manager", "manager-pass-1", role="business_manager")
    manager = TestClient(main.app)
    assert manager.post(
        "/api/v1/auth/login", json={"username": "quote-manager", "password": "manager-pass-1"}
    ).status_code == 200
    response = manager.post(
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
