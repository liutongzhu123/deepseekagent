from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import auth, customer_service_export, main


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    auth.create_user("sales-user", "sales-password", role="sales", projects=["项目甲"])
    client = TestClient(main.app)
    response = client.post("/api/v1/auth/login", json={"username": "sales-user", "password": "sales-password"})
    assert response.status_code == 200, response.text
    return client


def _record_review(review_id: str, line: dict) -> None:
    fields = ("item_name", "standard", "sample_name", "quantity", "unit")
    assert auth.audit({
        "user": "sales-user", "role": "sales", "event": "customer_service_quote_parse_reviewed",
        "review_id": review_id, "project": "项目甲", "source_sha256": "a" * 64,
        "reviewed_rows": [{
            "line_index": 0, "source_row": line["source_row"],
            "parse_confidence": line.get("confidence"),
            "reviewed_values": {field: {"before": line.get(field), "after": line.get(field)} for field in fields},
        }],
    })


def _payload(review_id: str) -> dict:
    return {
        "project_name": "项目甲", "review_id": review_id,
        "lines": [{
            "item_name": "气味", "standard": "标准甲", "sample_name": "样品甲",
            "quantity": 2, "unit": "件", "source_row": 2, "confidence": 0.96,
        }],
    }


def _provider(monkeypatch, prices=None):
    snapshot = main.crm.CrmSnapshot(
        mode="excel", source="isolated",
        projects=[main.crm.CrmProject("PROJ-A", "项目甲")],
        prices=prices or [main.crm.CrmPrice("PROJ-A", "项目甲", "样品甲", "气味", "标准甲", "", "件", 1, 120)],
    )
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())


def test_export_is_audited_review_bound_and_contains_only_read_only_preview(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    _provider(monkeypatch)
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    _record_review(review_id, _payload(review_id)["lines"][0])

    response = client.post("/api/v1/customer-service/quote-preview/export", json=_payload(review_id))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    workbook = load_workbook(BytesIO(response.content), data_only=False)
    sheet = workbook["内部复核草稿"]
    assert "非正式报价" in sheet["A1"].value
    assert sheet["H6"].value == "matched"
    assert sheet["I6"].value == 120
    assert sheet["U6"].value
    assert workbook["使用说明"]["B2"].value.startswith("非正式报价")
    events = [json.loads(line) for line in auth.AUDIT_PATH.read_text(encoding="utf-8").splitlines()]
    exported = next(event for event in events if event.get("event") == "customer_service_crm_review_workbook_exported")
    assert exported["review_id"] == review_id
    assert exported["source_mode"] == "excel"
    assert exported["source_revision"]
    assert exported["line_count"] == 1
    assert "reviewed_rows" not in exported


def test_export_rejects_forged_or_changed_review_before_reading_crm(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("must reject before reading CRM"))
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    _record_review(review_id, _payload(review_id)["lines"][0])
    changed = _payload(review_id)
    changed["lines"][0]["confidence"] = 0.2

    forged = client.post("/api/v1/customer-service/quote-preview/export", json=_payload("CS-QUOTE-PARSE-REVIEW-fedcba9876543210"))
    tampered = client.post("/api/v1/customer-service/quote-preview/export", json=changed)

    assert forged.status_code == 409
    assert tampered.status_code == 409


def test_export_rejects_project_outside_user_scope_before_reading_crm(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("must reject before reading CRM"))
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    _record_review(review_id, _payload(review_id)["lines"][0])
    request = _payload(review_id)
    request["project_name"] = "项目乙"

    response = client.post("/api/v1/customer-service/quote-preview/export", json=request)

    assert response.status_code == 403


def test_export_fails_closed_if_export_audit_cannot_be_written(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    _provider(monkeypatch)
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    _record_review(review_id, _payload(review_id)["lines"][0])
    monkeypatch.setattr(main, "_audit_event", lambda *args, **kwargs: False)

    response = client.post("/api/v1/customer-service/quote-preview/export", json=_payload(review_id))

    assert response.status_code == 503
    assert not response.headers.get("content-type", "").startswith("application/vnd.openxmlformats")


def test_workbook_keeps_formula_like_user_text_literal():
    preview = {
        "environment": "internal", "input_origin": "parsed_quote", "demo_only": False,
        "source_mode": "excel", "source_revision": "r1", "preview_id": "CRM-PREVIEW-1",
        "lines": [{
            "request": {"project_name": "项目甲", "item_name": "\x01=HYPERLINK(\"https://example.invalid\")"},
            "price_status": "missing", "price_preview": {"unit_price": 999},
            "input_evidence": {}, "source_refs": [], "missing_fields": [],
        }],
    }

    workbook = load_workbook(BytesIO(customer_service_export.build_internal_review_workbook(
        "项目甲", "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", preview,
    )), data_only=False)

    cell = workbook["内部复核草稿"]["B6"]
    assert cell.data_type == "s"
    assert cell.value.startswith("'=HYPERLINK")
    assert workbook["内部复核草稿"]["I6"].value is None
    assert "\x01" not in cell.value


def test_workbook_omits_ambiguous_price_and_rejects_synthetic_preview():
    preview = {
        "environment": "internal", "input_origin": "parsed_quote", "demo_only": False,
        "source_mode": "excel", "source_revision": "r1", "preview_id": "CRM-PREVIEW-1",
        "lines": [{
            "request": {"project_name": "项目甲", "item_name": "气味"},
            "price_status": "ambiguous", "price_preview": None,
            "price_candidates": [{"unit_price": 120}, {"unit_price": 140}],
            "input_evidence": {}, "source_refs": [], "missing_fields": [],
        }],
    }
    data = customer_service_export.build_internal_review_workbook(
        "项目甲", "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", preview,
    )
    workbook = load_workbook(BytesIO(data), data_only=False)
    sheet = workbook["内部复核草稿"]
    assert sheet["H6"].value == "ambiguous"
    assert sheet["I6"].value is None

    preview.update(environment="synthetic", demo_only=True, source_mode=None, source_revision=None)
    with pytest.raises(ValueError, match="只允许"):
        customer_service_export.build_internal_review_workbook(
            "项目甲", "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", preview,
        )
