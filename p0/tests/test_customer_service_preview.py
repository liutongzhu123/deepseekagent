from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import auth, crm, customer_service_preview, main


@pytest.fixture()
def catalog() -> dict:
    return {
        "dataset": {
            "kind": "synthetic",
            "version": 1,
            "demo_only": True,
            "production_usable": False,
        },
        "prices": [
            {
                "record_id": "PRICE-1",
                "project_name": "项目甲",
                "item_name": "外观检查",
                "standard": "STD-1",
                "unit": "件",
                "minimum_quantity": 1,
                "unit_price": 100,
                "currency": "CNY",
                "tax_inclusive": True,
                "valid_until": "2099-12-31",
                "source": {"system": "synthetic-fixture", "record_id": "PRICE-1", "revision": "v1"},
            },
            {
                "record_id": "PRICE-EXPIRED",
                "project_name": "项目甲",
                "item_name": "过期检查",
                "standard": "STD-1",
                "unit": "件",
                "minimum_quantity": 1,
                "unit_price": 200,
                "valid_until": "2020-12-31",
                "source": {"system": "synthetic-fixture", "record_id": "PRICE-EXPIRED", "revision": "v1"},
            },
        ],
        "schedules": [],
        "qualifications": [
            {
                "record_id": "QUAL-PROJECT",
                "project_name": "项目甲",
                "scope_level": "project",
                "standard": "",
                "certificate_no": "CERT-1",
                "valid_until": "2099-12-31",
                "source": {"system": "synthetic-fixture", "record_id": "QUAL-PROJECT", "revision": "v1"},
            }
        ],
        "name_mappings": [
            {
                "record_id": "MAP-UNREVIEWED",
                "alias": "外观",
                "canonical": "外观检查",
                "review_status": "pending",
                "source": {"system": "synthetic-fixture", "record_id": "MAP-UNREVIEWED", "revision": "v1"},
            }
        ],
    }


def _requests(line: dict) -> dict:
    return {"dataset": {"kind": "parsed_quote", "demo_only": True}, "lines": [line]}


def _line(**overrides) -> dict:
    return {
        "line_id": "LINE-1",
        "project_name": "项目甲",
        "item_name": "外观检查",
        "standard": "STD-1",
        "quantity": 2,
        "unit": "件",
        **overrides,
    }


def _reviewed_preview_payload(project_name: str, lines: list[dict]) -> dict:
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    reviewed_rows = []
    normalized_lines = []
    for index, raw_line in enumerate(lines):
        line = {
            "item_name": "", "standard": "", "sample_name": "", "quantity": None, "unit": "",
            **raw_line, "source_row": raw_line.get("source_row") or index + 1,
        }
        normalized_lines.append(line)
        reviewed_rows.append({
            "line_index": index,
            "source_row": line["source_row"],
            "parse_confidence": line.get("confidence"),
            "reviewed_values": {
                field: {"before": line.get(field), "after": line.get(field)}
                for field in ("item_name", "standard", "sample_name", "quantity", "unit")
            },
        })
    assert auth.audit({
        "user": "sales-user",
        "role": "sales",
        "event": "customer_service_quote_parse_reviewed",
        "review_id": review_id,
        "project": project_name,
        "source_sha256": "0" * 64,
        "reviewed_rows": reviewed_rows,
    })
    assert main._verified_quote_parse_review(
        review_id,
        {"username": "sales-user"},
        project_name,
        [main.S1QuotePreviewLine.model_validate(row) for row in normalized_lines],
    )
    return {"project_name": project_name, "review_id": review_id, "lines": normalized_lines}


def test_preview_keeps_price_as_internal_fact_without_total(catalog):
    result = customer_service_preview.resolve_preview(catalog, _requests(_line()))

    line = result["lines"][0]
    assert result["demo_only"] is True
    assert line["price_status"] == "matched"
    assert line["price_preview"]["unit_price"] == 100
    assert line["qualification_status"] == "project_scope_only"
    assert result["decision_summary"]["total_preview"] is None
    assert result["decision_summary"]["threshold_result"] == "not_evaluated"


def test_preview_rejects_catalog_with_wrong_provenance(catalog):
    catalog["prices"][0]["source"]["system"] = "customer-crm"

    with pytest.raises(ValueError, match="来源标记无效"):
        customer_service_preview.resolve_preview(catalog, _requests(_line()))


def test_preview_missing_fields_and_unreviewed_mapping_require_human_action(catalog):
    incomplete = customer_service_preview.resolve_preview(catalog, _requests(_line(quantity=None)))["lines"][0]
    pending_mapping = customer_service_preview.resolve_preview(
        catalog, _requests(_line(item_name="外观"))
    )["lines"][0]

    assert incomplete["action"] == "ask_customer"
    assert "有效样品数量" in incomplete["missing_fields"]
    assert pending_mapping["price_status"] == "ambiguous"
    assert pending_mapping["action"] == "manual_review"


def test_preview_does_not_infer_nearby_price_and_hides_expired_price(catalog):
    missing = customer_service_preview.resolve_preview(
        catalog, _requests(_line(item_name="不存在的项目"))
    )["lines"][0]
    expired = customer_service_preview.resolve_preview(
        catalog, _requests(_line(item_name="过期检查"))
    )["lines"][0]

    assert missing["price_status"] == "missing"
    assert missing["price_preview"] is None
    assert expired["price_status"] == "expired"
    assert expired["price_preview"] is None


def test_preview_marks_duplicate_prices_ambiguous_without_selecting_one(catalog):
    duplicate = dict(catalog["prices"][0], record_id="PRICE-2", unit_price=120)
    duplicate["source"] = {"system": "synthetic-fixture", "record_id": "PRICE-2", "revision": "v1"}
    catalog["prices"].append(duplicate)

    line = customer_service_preview.resolve_preview(catalog, _requests(_line()))["lines"][0]

    assert line["price_status"] == "ambiguous"
    assert line["price_preview"] is None
    assert {candidate["record_id"] for candidate in line["price_candidates"]} == {"PRICE-1", "PRICE-2"}


def test_preview_requires_explicit_demo_status_before_matching_schedule(catalog):
    catalog["schedules"] = [{
        "record_id": "SCHEDULE-UNCONFIRMED",
        "project_name": "项目甲",
        "start_date": "2099-02-01",
        "end_date": "2099-02-15",
        "status": "",
        "source": {"system": "synthetic-fixture", "record_id": "SCHEDULE-UNCONFIRMED", "revision": "v1"},
    }]

    line = customer_service_preview.resolve_preview(catalog, _requests(_line()))["lines"][0]

    assert line["schedule_status"] == "unknown"
    assert line["schedule_preview"] == {
        "start_date": "2099-02-01", "end_date": "2099-02-15", "status": ""
    }
    assert line["action"] == "manual_review"
    assert any(ref["record_id"] == "SCHEDULE-UNCONFIRMED" for ref in line["source_refs"])


def test_preview_does_not_interpret_crm_schedule_status_without_customer_semantics():
    catalog = {
        "dataset": {"kind": "crm_snapshot"},
        "schedules": [{
            "record_id": "CRM-SCHEDULE-1",
            "project_name": "项目甲",
            "start_date": "2026-12-01",
            "end_date": "2026-12-15",
            "status": "已排",
            "source": {"system": "customer-crm", "record_id": "CRM-SCHEDULE-1", "revision": "r1"},
        }],
    }

    status, preview, refs = customer_service_preview._resolve_schedule(
        catalog, {"project_name": "项目甲"}, date.fromisoformat("2026-11-24")
    )

    assert status == "unknown"
    assert preview["status"] == "已排"
    assert preview["start_date"] == "2026-12-01"
    assert refs[0]["record_id"] == "CRM-SCHEDULE-1"


def test_preview_outage_is_not_replaced_by_synthetic_facts(catalog):
    line = customer_service_preview.resolve_preview(
        catalog, _requests(_line(simulate_source_unavailable=True))
    )["lines"][0]

    assert line["price_status"] == "source_unavailable"
    assert line["price_preview"] is None
    assert line["source_refs"] == []


def test_preview_low_parser_confidence_preserves_facts_but_requires_review(catalog):
    line = customer_service_preview.resolve_preview(
        catalog, _requests(_line(confidence=0.5))
    )["lines"][0]

    assert line["price_status"] == "matched"
    assert line["price_preview"]["unit_price"] == 100
    assert line["action"] == "manual_review"
    assert any("置信度低于 0.85" in fact for fact in line["missing_fields"])


def test_crm_qualification_preview_does_not_expose_global_record_count():
    populated = crm.CrmSnapshot(
        mode="excel",
        source="isolated",
        qualifications=[
            crm.CrmQualification("资质甲", "CERT-A"),
            crm.CrmQualification("资质乙", "CERT-B"),
        ],
    )
    empty = crm.CrmSnapshot(mode="excel", source="isolated")
    populated_line = customer_service_preview.resolve_preview(
        customer_service_preview.crm_snapshot_catalog(populated), _requests(_line())
    )["lines"][0]
    empty_line = customer_service_preview.resolve_preview(
        customer_service_preview.crm_snapshot_catalog(empty), _requests(_line())
    )["lines"][0]

    assert populated_line["qualification_status"] == "unknown"
    assert populated_line["qualification_preview"] == empty_line["qualification_preview"]
    assert populated_line["source_refs"] == empty_line["source_refs"]
    assert "candidate_count" not in populated_line["qualification_preview"]


def test_crm_snapshot_gives_duplicate_rows_distinct_ids():
    identical = crm.CrmPrice("P-A", "项目甲", "样品A", "外观检查", "STD-1", "", "件", 2, 100)
    catalog_view = customer_service_preview.crm_snapshot_catalog(
        crm.CrmSnapshot(mode="excel", source="isolated", prices=[identical, identical])
    )

    record_ids = [row["record_id"] for row in catalog_view["prices"]]

    assert len(record_ids) == 2
    assert len(set(record_ids)) == 2


def test_project_scoped_crm_catalog_excludes_other_projects_and_global_revisions():
    snapshot = crm.CrmSnapshot(
        mode="excel",
        source="isolated",
        projects=[crm.CrmProject("PROJ-A", "项目甲"), crm.CrmProject("PROJ-B", "项目乙")],
        prices=[
            crm.CrmPrice("PROJ-A", "项目甲", "样品A", "检验A", "S1", "", "件", 2, 100),
            crm.CrmPrice("PROJ-B", "项目乙", "样品B", "检验B", "S1", "", "件", 3, 900),
        ],
        schedules=[
            crm.CrmSchedule("PROJ-A", "项目甲", end_date="2099-12-31"),
            crm.CrmSchedule("PROJ-B", "项目乙", end_date="2099-12-31"),
        ],
        qualifications=[crm.CrmQualification("全局资质", "CERT-GLOBAL")],
        name_mappings=[crm.CrmNameMapping("全局别名", "检验A")],
    )
    catalog = customer_service_preview.scope_crm_catalog_to_project(
        customer_service_preview.crm_snapshot_catalog(snapshot), "PROJ-A", "项目甲"
    )
    scoped_revision = catalog["dataset"]["revision"]
    scoped_ids = [row["record_id"] for row in catalog["prices"]]

    snapshot.prices[1].unit_price = 901
    snapshot.schedules[1].end_date = "2098-12-31"
    snapshot.qualifications.append(crm.CrmQualification("新增全局资质", "CERT-NEW"))
    snapshot.name_mappings.append(crm.CrmNameMapping("新增全局别名", "检验A"))
    after_hidden_change = customer_service_preview.scope_crm_catalog_to_project(
        customer_service_preview.crm_snapshot_catalog(snapshot), "PROJ-A", "项目甲"
    )

    assert [row["project_id"] for row in catalog["prices"]] == ["PROJ-A"]
    assert [row["project_id"] for row in catalog["schedules"]] == ["PROJ-A"]
    assert catalog["qualifications"] == []
    assert catalog["name_mappings"] == []
    assert after_hidden_change["dataset"]["revision"] == scoped_revision
    assert [row["record_id"] for row in after_hidden_change["prices"]] == scoped_ids


def _isolated_sales_client(monkeypatch, tmp_path, projects: list[str]) -> TestClient:
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    auth.create_user("sales-user", "sales-password", role="sales", projects=projects)
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "sales-user", "password": "sales-password"},
    )
    assert response.status_code == 200
    return client


def _snapshot(*, mode: str = "excel", duplicate: bool = False) -> crm.CrmSnapshot:
    prices = [
        crm.CrmPrice("PROJ-A", "项目甲", "样品A", "外观检查", "STD-1", "", "件", 2, 100),
    ]
    if duplicate:
        prices.append(crm.CrmPrice("PROJ-A", "项目甲", "样品A", "外观检查", "STD-1", "", "件", 3, 120))
    return crm.CrmSnapshot(
        mode=mode,
        source="isolated",
        projects=[crm.CrmProject("PROJ-A", "项目甲")],
        prices=prices,
    )


def test_crm_preview_denies_unassigned_project_before_reading_source(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")

    def unexpected_provider():
        pytest.fail("unauthorized project must be rejected before CRM access")

    monkeypatch.setattr(main.crm, "provider", unexpected_provider)
    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json={"project_name": "项目乙", "review_id": "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", "lines": [_line(project_name="ignored")]},
    )

    assert response.status_code == 403


def test_crm_preview_requires_nonblank_project_before_reading_source(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("blank project must not read CRM"))

    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json={"project_name": "   ", "review_id": "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", "lines": [_line()]},
    )

    assert response.status_code == 422


def test_restricted_crm_preview_does_not_consume_global_name_mappings(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot()
    snapshot.name_mappings = [crm.CrmNameMapping("外观别名", "外观检查", "global mapping")]

    class Provider:
        def snapshot(self):
            return snapshot

    monkeypatch.setattr(main.crm, "provider", lambda: Provider())
    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("项目甲", [{**_line(item_name="外观别名"), "sample_name": "样品A"}]),
    )

    assert response.status_code == 200, response.text
    line = response.json()["lines"][0]
    assert line["matched_item_name"] == "外观别名"
    assert line["price_status"] == "missing"
    assert not any(ref["source_type"] == "name_mapping" for ref in line["source_refs"])


def test_restricted_crm_preview_rejects_ambiguous_project_name(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot()
    snapshot.projects.append(crm.CrmProject("PROJ-DUPLICATE", "项目甲"))
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())

    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("项目甲", [_line()]),
    )

    assert response.status_code == 409
    assert "项目映射冲突" in response.json()["detail"]


def test_restricted_crm_preview_fails_closed_without_project_catalog(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot()
    snapshot.projects = []
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())

    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("项目甲", [_line()]),
    )

    assert response.status_code == 409


def test_restricted_crm_preview_accepts_authorized_project_id(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["PROJ-A"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot()
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())

    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("PROJ-A", [_line(project_name="PROJ-A", sample_name="样品A")]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["lines"][0]["price_status"] == "matched"


@pytest.mark.parametrize(
    ("enabled", "mode", "expected_status"),
    [("0", "excel", 404), ("1", "off", 503), ("1", "api", 503)],
)
def test_crm_preview_fails_closed_without_supported_enabled_source(
    monkeypatch, tmp_path, enabled, mode, expected_status
):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", enabled)
    monkeypatch.setenv("P0_CRM_MODE", mode)
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("disabled/unsupported source must not be read"))

    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("项目甲", [_line()]),
    )

    assert response.status_code == expected_status


def test_crm_preview_source_error_does_not_fall_back_or_expose_provider_details(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")

    class Provider:
        def snapshot(self):
            raise crm.CrmUnavailable("/private/customer/prices.xlsx is unavailable")

    monkeypatch.setattr(main.crm, "provider", lambda: Provider())
    response = client.post(
        "/api/v1/customer-service/quote-preview/resolve",
        json=_reviewed_preview_payload("项目甲", [_line()]),
    )

    assert response.status_code == 503
    assert "private/customer" not in response.text
    assert "source_unavailable" not in response.text


def test_price_review_requires_current_candidate_and_writes_audit(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot(duplicate=True)
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())
    preview_request = {
        **_reviewed_preview_payload("项目甲", [{**_line(sample_name="样品A"), "source_row": 4, "confidence": 0.98}]),
    }
    preview = client.post("/api/v1/customer-service/quote-preview/resolve", json=preview_request)
    assert preview.status_code == 200, preview.text
    line_preview = preview.json()["lines"][0]
    assert line_preview["price_status"] == "ambiguous"
    selected = line_preview["price_candidates"][0]["record_id"]
    review_payload = {
        **preview_request["lines"][0],
        "project_name": "项目甲",
        "source_mode": "excel",
        "source_revision": preview.json()["source_revision"],
        "line_id": line_preview["line_id"],
        "selected_record_id": selected,
    }

    recorded = client.post(
        "/api/v1/customer-service/quote-preview/price-reviews", json=review_payload
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["recorded"] is True
    assert "不写回 CRM" in recorded.json()["effect"]
    assert [row.unit_price for row in snapshot.prices] == [100, 120]
    audit_records = [json.loads(line) for line in auth.AUDIT_PATH.read_text(encoding="utf-8").splitlines()]
    review = next(row for row in audit_records if row.get("event") == "customer_service_price_candidate_reviewed")
    assert review["selected_record_id"] == selected
    assert review["user"] == "sales-user"

    snapshot.prices[0].unit_price = 101
    stale = client.post(
        "/api/v1/customer-service/quote-preview/price-reviews", json=review_payload
    )
    assert stale.status_code == 409


def test_price_review_rejects_unassigned_project(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("unauthorized project must not read CRM"))

    response = client.post(
        "/api/v1/customer-service/quote-preview/price-reviews",
        json={
            **_line(project_name="项目乙"),
            "source_mode": "excel",
            "source_revision": "excel-revision",
            "line_id": "LINE-1",
            "selected_record_id": "CRM-PRICE-1",
        },
    )

    assert response.status_code == 403


def test_price_review_rejects_forged_candidate_without_audit(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot(duplicate=True)

    class Provider:
        def snapshot(self):
            return snapshot

    monkeypatch.setattr(main.crm, "provider", lambda: Provider())
    revision = customer_service_preview.scope_crm_catalog_to_project(
        customer_service_preview.crm_snapshot_catalog(snapshot), "PROJ-A", "项目甲"
    )["dataset"]["revision"]
    response = client.post(
        "/api/v1/customer-service/quote-preview/price-reviews",
        json={
            **_line(sample_name="样品A"),
            "source_mode": "excel",
            "source_revision": revision,
            "line_id": "LINE-1",
            "selected_record_id": "FORGED-PRICE-ID",
        },
    )

    assert response.status_code == 409
    records = [json.loads(line) for line in auth.AUDIT_PATH.read_text(encoding="utf-8").splitlines()]
    assert not any(row.get("event") == "customer_service_price_candidate_reviewed" for row in records)


def test_price_review_does_not_claim_success_when_audit_write_fails(monkeypatch, tmp_path):
    client = _isolated_sales_client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    snapshot = _snapshot(duplicate=True)
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())
    monkeypatch.setattr(main, "_audit_event", lambda *_args, **_kwargs: False)
    catalog_view = customer_service_preview.scope_crm_catalog_to_project(
        customer_service_preview.crm_snapshot_catalog(snapshot), "PROJ-A", "项目甲"
    )
    line = _line(sample_name="样品A", confidence=0.98, source_row=3)
    reviewed = _reviewed_preview_payload("项目甲", [line])
    line = reviewed["lines"][0]
    line["line_id"] = f"PARSED-REVIEW-{reviewed['review_id']}-{line['source_row']}-1"
    preview = customer_service_preview.resolve_preview(
        catalog_view, _requests(line)
    )
    payload = {
        **line,
        "project_name": "项目甲",
        "source_mode": "excel",
        "source_revision": preview["source_revision"],
        "line_id": line["line_id"],
        "selected_record_id": preview["lines"][0]["price_candidates"][0]["record_id"],
    }

    response = client.post(
        "/api/v1/customer-service/quote-preview/price-reviews", json=payload
    )

    assert response.status_code == 503
    assert "未能写入审计记录" in response.json()["detail"]
