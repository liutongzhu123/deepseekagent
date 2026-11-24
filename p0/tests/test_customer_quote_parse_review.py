from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import auth, main


def _client(monkeypatch, tmp_path: Path, projects: list[str] | None = None) -> TestClient:
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    auth.create_user("sales-user", "sales-password", role="sales", projects=projects or ["项目甲"])
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "sales-user", "password": "sales-password"},
    )
    assert response.status_code == 200, response.text
    return client


def _quote_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "客户报价单.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["序号", "样品名称", "检验标准", "检验项目", "单价（元）", "数量", "单位"])
    sheet.append([1, "样品甲", "标准甲", "气味", 1200, 2, "件"])
    workbook.save(path)
    return path.read_bytes()


def _edits(**overrides) -> list[dict]:
    row = {
        "line_index": 0,
        "source_row": 2,
        "item_name": "气味",
        "standard": "标准甲",
        "sample_name": "样品甲",
        "quantity": 2,
        "unit": "件",
    }
    row.update(overrides)
    return [row]


def _post_review(
    client: TestClient, content: bytes, edits: list[dict], project: str = "项目甲",
    source_sha256: str | None = None,
    parse_token: str | None = None,
    parse_source: bool = True,
    filename: str = "客户报价单.xlsx",
):
    if parse_source:
        parsed = client.post(
            "/api/v1/quotes/parse",
            data={"project": project},
            files={"file": (filename, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        if parsed.status_code == 200:
            source_sha256 = source_sha256 or parsed.json()["source_sha256"]
            parse_token = parse_token or parsed.json()["parse_token"]
            assert main._valid_quote_parse_token(
                parse_token, {"username": "sales-user"}, source_sha256,
                filename, project, main.admin_config.quote_config(),
            ) or not parse_source
    return client.post(
        "/api/v1/quotes/review",
        data={
            "project": project,
            "source_sha256": source_sha256 or hashlib.sha256(content).hexdigest(),
            "parse_token": parse_token or "invalid-token",
            "edits": json.dumps(edits),
        },
        files={"file": (filename, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _record_review(review_id: str, project: str, line: dict) -> None:
    fields = ("item_name", "standard", "sample_name", "quantity", "unit")
    assert auth.audit({
        "user": "sales-user", "role": "sales",
        "event": "customer_service_quote_parse_reviewed",
        "review_id": review_id, "project": project,
        "source_sha256": "a" * 64,
        "reviewed_rows": [{
            "line_index": 0,
            "source_row": line["source_row"],
            "parse_confidence": line.get("confidence"),
            "reviewed_values": {field: {"before": line.get(field), "after": line.get(field)} for field in fields},
        }],
    })


def test_review_records_before_after_and_keeps_source_price_out_of_review_payload(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    content = _quote_bytes(tmp_path)
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("parse review must not read CRM"))

    response = _post_review(client, content, _edits(item_name="气味检测", quantity=3))

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recorded"] is True
    assert result["source_sha256"]
    assert result["lines"] == [{
        "item_name": "气味检测", "standard": "标准甲", "sample_name": "样品甲",
        "quantity": 3.0, "unit": "件", "source_row": 2, "confidence": result["lines"][0]["confidence"],
    }]
    assert "unit_price" not in result["lines"][0]
    event = next(row for row in _events(auth.AUDIT_PATH) if row.get("event") == "customer_service_quote_parse_reviewed")
    assert event["user"] == "sales-user"
    assert event["review_id"] == result["review_id"]
    assert event["reviewed_rows"][0]["changes"]["item_name"] == {"before": "气味", "after": "气味检测"}
    assert event["reviewed_rows"][0]["changes"]["quantity"] == {"before": 2, "after": 3.0}
    assert "unit_price" not in json.dumps(event["reviewed_rows"], ensure_ascii=False)


def test_review_denies_unassigned_project_before_parsing_or_auditing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, ["项目甲"])
    monkeypatch.setattr(main.quote_service, "parse_quote_bytes", lambda *args, **kwargs: pytest.fail("must deny before parsing"))

    response = _post_review(client, b"ignored", _edits(), project="项目乙", parse_source=False)

    assert response.status_code == 403
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_review_requires_the_same_source_file_as_initial_parse(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    first_file = _quote_bytes(tmp_path)
    another_file = first_file + b"changed"
    parsed = client.post(
        "/api/v1/quotes/parse",
        data={"project": "项目甲"},
        files={"file": ("客户报价单.xlsx", first_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert parsed.status_code == 200
    assert parsed.json()["source_sha256"] == hashlib.sha256(first_file).hexdigest()

    response = _post_review(
        client, another_file, _edits(), source_sha256=parsed.json()["source_sha256"],
        parse_token=parsed.json()["parse_token"], parse_source=False,
    )

    assert response.status_code == 409
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_parse_token_is_server_issued_user_bound_and_config_bound(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, ["项目甲", "项目乙"])
    content = _quote_bytes(tmp_path)
    parsed = client.post(
        "/api/v1/quotes/parse",
        data={"project": "项目甲"},
        files={"file": ("客户报价单.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert parsed.status_code == 200
    proof = parsed.json()
    forged = _post_review(
        client, content, _edits(), source_sha256=proof["source_sha256"],
        parse_token="forged", parse_source=False,
    )
    assert forged.status_code == 409

    auth.create_user("other-sales", "other-password", role="sales", projects=["项目甲"])
    other = TestClient(main.app)
    assert other.post("/api/v1/auth/login", json={"username": "other-sales", "password": "other-password"}).status_code == 200
    wrong_user = _post_review(
        other, content, _edits(), source_sha256=proof["source_sha256"],
        parse_token=proof["parse_token"], parse_source=False,
    )
    assert wrong_user.status_code == 409

    wrong_project = _post_review(
        client, content, _edits(), project="项目乙", source_sha256=proof["source_sha256"],
        parse_token=proof["parse_token"], parse_source=False,
    )
    assert wrong_project.status_code == 409

    monkeypatch.setattr(main.admin_config, "quote_config", lambda: {"changed": True})
    stale_config = _post_review(
        client, content, _edits(), source_sha256=proof["source_sha256"],
        parse_token=proof["parse_token"], parse_source=False,
    )
    assert stale_config.status_code == 409
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_parse_token_is_bound_to_filename_that_can_change_parse_result(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    content = _quote_bytes(tmp_path)
    parsed = client.post(
        "/api/v1/quotes/parse",
        data={"project": "项目甲"},
        files={"file": ("样品甲报价.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert parsed.status_code == 200

    response = _post_review(
        client, content, _edits(), source_sha256=parsed.json()["source_sha256"],
        parse_token=parsed.json()["parse_token"], parse_source=False, filename="样品乙报价.xlsx",
    )

    assert response.status_code == 409
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_windows_path_filename_is_canonical_before_parse_and_review(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    content = _quote_bytes(tmp_path)
    parsed = client.post(
        "/api/v1/quotes/parse",
        data={"project": "项目甲"},
        files={"file": ("name.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert parsed.status_code == 200

    response = _post_review(
        client, content, _edits(), source_sha256=parsed.json()["source_sha256"],
        parse_token=parsed.json()["parse_token"], parse_source=False, filename=r"DVP\name.xlsx",
    )

    assert response.status_code == 200, response.text
    assert response.json()["parse_method"] == parsed.json()["parse_meta"]["parse_method"]


@pytest.mark.parametrize("rows", [
    [],
    [{"line_index": 0, "source_row": 99, "item_name": "气味", "standard": "标准甲", "sample_name": "样品甲", "quantity": 2, "unit": "件"}],
    [{"line_index": 1, "source_row": 2, "item_name": "气味", "standard": "标准甲", "sample_name": "样品甲", "quantity": 2, "unit": "件"}],
])
def test_review_rejects_changed_row_count_or_source_identity(monkeypatch, tmp_path, rows):
    client = _client(monkeypatch, tmp_path)

    response = _post_review(client, _quote_bytes(tmp_path), rows)

    assert response.status_code in (409, 422)
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


@pytest.mark.parametrize("quantity", [0, -1, float("nan"), float("inf")])
def test_review_rejects_non_positive_or_non_finite_quantity(monkeypatch, tmp_path, quantity):
    client = _client(monkeypatch, tmp_path)

    response = _post_review(client, _quote_bytes(tmp_path), _edits(quantity=quantity))

    assert response.status_code == 422
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_review_rejects_unparseable_file_without_audit(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/v1/quotes/parse",
        files={"file": ("客户报价单.xlsx", b"not an xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 422
    assert not any(row.get("event") == "customer_service_quote_parse_reviewed" for row in _events(auth.AUDIT_PATH))


def test_review_fails_closed_when_audit_cannot_persist(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_audit_event", lambda *args, **kwargs: False)

    response = _post_review(client, _quote_bytes(tmp_path), _edits())

    assert response.status_code == 503


def test_crm_preview_rejects_missing_forged_or_changed_review_before_crm(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("unreviewed rows must not read CRM"))
    line = {
        "item_name": "气味", "standard": "标准甲", "sample_name": "样品甲",
        "quantity": 2, "unit": "件", "source_row": 2,
    }

    missing = client.post("/api/v1/customer-service/quote-preview/resolve", json={"project_name": "项目甲", "lines": [line]})
    forged = client.post("/api/v1/customer-service/quote-preview/resolve", json={
        "project_name": "项目甲", "review_id": "CS-QUOTE-PARSE-REVIEW-0123456789abcdef", "lines": [line],
    })
    review_id = "CS-QUOTE-PARSE-REVIEW-fedcba9876543210"
    _record_review(review_id, "项目甲", {**line, "confidence": 0.5})
    changed = client.post("/api/v1/customer-service/quote-preview/resolve", json={
        "project_name": "项目甲", "review_id": review_id,
        "lines": [{**line, "confidence": 0.99}],
    })

    assert missing.status_code == 422
    assert forged.status_code == 409
    assert changed.status_code == 409


def test_crm_preview_accepts_only_exact_audited_review_rows(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    line = {
        "item_name": "气味", "standard": "标准甲", "sample_name": "样品甲",
        "quantity": 2, "unit": "件", "source_row": 2,
    }
    review_id = "CS-QUOTE-PARSE-REVIEW-0123456789abcdef"
    _record_review(review_id, "项目甲", line)
    snapshot = main.crm.CrmSnapshot(
        mode="excel", source="isolated",
        projects=[main.crm.CrmProject("PROJ-A", "项目甲")],
        prices=[main.crm.CrmPrice("PROJ-A", "项目甲", "样品甲", "气味", "标准甲", "", "件", 1, 100)],
    )
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: snapshot})())

    response = client.post("/api/v1/customer-service/quote-preview/resolve", json={
        "project_name": "项目甲", "review_id": review_id, "lines": [line],
    })

    assert response.status_code == 200, response.text
    assert response.json()["lines"][0]["price_status"] == "matched"
    assert review_id in response.json()["lines"][0]["line_id"]
