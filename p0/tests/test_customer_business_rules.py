from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import admin_config, auth, customer_business_rules, main


def _business() -> dict:
    return {
        "discount_tiers": [
            {"min_amount": 0, "discount": 100},
            {"min_amount": 10_000, "discount": 95},
        ],
        "rush": {"enabled": True, "fee_rate": 12, "lead_days": 3},
        "intermediary": {"enabled": True, "block": True, "keywords": ["代理"]},
        "blacklist": {"companies": ["甲公司"]},
        "whitelist": {"companies": ["甲公司"], "keywords": ["集团客户"]},
    }


def _request(**overrides) -> dict:
    return {
        "project_name": "项目甲",
        "company_name": "甲公司代理",
        "subtotal": 10_000,
        "rush_requested": True,
        **overrides,
    }


def _client(monkeypatch, tmp_path, *, projects: list[str] | None = None) -> TestClient:
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(admin_config, "CONFIG_PATH", tmp_path / "rules.json")
    admin_config.reset_cache()
    auth.create_user("sales-user", "sales-password", role="sales", projects=projects or ["项目甲"])
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/auth/login", json={"username": "sales-user", "password": "sales-password"}
    )
    assert response.status_code == 200
    return client


def _write_config(path, *, warning_free=True) -> None:
    config = admin_config.default_config()
    config["revision"] = 17
    config["updated_at"] = "2026-11-23T00:00:00Z"
    config["business"] = _business()
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    admin_config.reset_cache()


def test_evaluator_is_deterministic_and_never_makes_decisions():
    first = customer_business_rules.evaluate_candidates(
        _business(), company_name="甲公司代理", subtotal=10_000, rush_requested=True
    )
    second = customer_business_rules.evaluate_candidates(
        _business(), company_name="甲公司代理", subtotal=10_000, rush_requested=True
    )
    assert first == second
    assert first["decision"] == "manual_review"
    assert first["auto_apply"] is False
    assert first["candidates"]["discount_tier"] == {"min_amount": 10_000, "discount": 95}
    assert first["candidates"]["rush"] == {"fee_rate": 12, "lead_days": 3}
    assert first["candidates"]["blacklist_match"] is False
    assert first["candidates"]["whitelist_match"] is False
    assert first["candidates"]["intermediary_keywords"] == ["代理"]


def test_vip_keyword_is_candidate_only_and_does_not_change_exact_company_match():
    result = customer_business_rules.evaluate_candidates(
        _business(), company_name="甲公司集团客户分部", subtotal=0, rush_requested=False
    )
    candidates = result["candidates"]
    assert candidates["whitelist_match"] is False
    assert candidates["whitelist_keyword_match"] is True
    assert candidates["whitelist_keyword_matches"] == ["集团客户"]
    assert result["decision"] == "manual_review" and result["auto_apply"] is False


def test_malformed_vip_keywords_fail_closed():
    business = _business()
    business["whitelist"]["keywords"] = "集团"
    with pytest.raises(ValueError, match="whitelist keywords"):
        customer_business_rules.evaluate_candidates(
            business, company_name="甲公司", subtotal=0, rush_requested=False
        )


def test_preview_audits_revision_and_redacts_company_name(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _write_config(tmp_path / "rules.json")
    response = client.post("/api/v1/customer-service/business-rules/preview", json=_request(company_name="甲公司"))
    assert response.status_code == 200
    body = response.json()
    assert body["config_revision"] == 17
    assert body["decision"] == "manual_review"
    assert body["auto_apply"] is False
    assert "company_list_conflict" not in body["candidates"]
    assert "blacklist_match" not in body["candidates"]
    assert body["scope"] == "read_only_candidate"
    audit_text = (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8")
    assert "customer_business_rules_previewed" in audit_text
    assert "甲公司" not in audit_text
    assert "company_fingerprint" in audit_text


def test_preview_denies_unassigned_project_before_config_read(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, projects=["项目乙"])
    monkeypatch.setattr(admin_config, "describe", lambda: pytest.fail("must deny before config access"))
    response = client.post("/api/v1/customer-service/business-rules/preview", json=_request())
    assert response.status_code == 403


def test_preview_rejects_invalid_configuration_and_audit_failure(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_config, "describe", lambda: {"warning": "bad config", "config": {}})
    invalid = client.post("/api/v1/customer-service/business-rules/preview", json=_request())
    assert invalid.status_code == 503

    _write_config(tmp_path / "rules.json")
    monkeypatch.setattr(main, "_audit_event", lambda *args, **kwargs: False)
    audit_failure = client.post("/api/v1/customer-service/business-rules/preview", json=_request())
    assert audit_failure.status_code == 503


def test_preview_requires_login_and_valid_input(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    anonymous = TestClient(main.app)
    assert anonymous.post("/api/v1/customer-service/business-rules/preview", json=_request()).status_code == 401
    auth.create_user("viewer", "viewer-password", role="viewer", projects=["项目甲"])
    viewer = TestClient(main.app)
    assert viewer.post("/api/v1/auth/login", json={
        "username": "viewer", "password": "viewer-password"
    }).status_code == 200
    assert viewer.post("/api/v1/customer-service/business-rules/preview", json=_request()).status_code == 403
    assert client.post("/api/v1/customer-service/business-rules/preview", json=_request(subtotal=-1)).status_code == 422
