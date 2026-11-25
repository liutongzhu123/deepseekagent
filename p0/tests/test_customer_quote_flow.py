import multiprocessing
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import auth, crm, customer_quote_flow, main


def _claim_from_worker(start, results, inquiry_path, inquiry_id, actor):
    os.environ["P0_CUSTOMER_QUOTE_FLOW_PATH"] = inquiry_path
    start.wait(timeout=30)
    try:
        customer_quote_flow.claim(inquiry_id, actor, "并发接单测试")
        results.put("ok")
    except customer_quote_flow.QuoteFlowConflict:
        results.put("conflict")


def _create_from_worker(start, results, inquiry_path, actor, operation_id, payload):
    os.environ["P0_CUSTOMER_QUOTE_FLOW_PATH"] = inquiry_path
    start.wait(timeout=30)
    try:
        record = customer_quote_flow.create(payload, actor, operation_id)
        results.put(record["inquiry_id"])
    except Exception as exc:  # pragma: no cover - asserted by the parent on worker failure
        results.put(f"error:{type(exc).__name__}:{exc}")


def _admin_quote_action(client, inquiry_id, action, payload, operation_id):
    return client.post(
        f"/api/v1/customer-service/quote-inquiries/admin/{inquiry_id}/{action}",
        headers={"Idempotency-Key": operation_id}, json=payload,
    )


def _login_sales(monkeypatch, tmp_path, username="sales-flow", projects=None):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    auth.create_user(username, "sales-password", role="sales", projects=projects or ["P1"])
    client = TestClient(main.app)
    response = client.post("/api/v1/auth/login", json={"username": username, "password": "sales-password"})
    assert response.status_code == 200
    return client


def _payload(project="项目甲", *, complete=True):
    line = {"item_name": "外观检查", "sample_name": "样品A"}
    if complete:
        line.update(standard="STD-1", quantity=2, unit="件", source_row=3, confidence=0.99)
    return {"project_name": project, "context": "仅内部演示询价", "lines": [line]}


def _create_inquiry(client, payload=None, key="quote-create-operation-0001"):
    return client.post(
        "/api/v1/customer-service/quote-inquiries",
        headers={"Idempotency-Key": key}, json=payload or _payload(),
    )


def _crm_snapshot():
    return crm.CrmSnapshot(
        mode="excel", source="local-test",
        projects=[crm.CrmProject("P1", "项目甲")],
        prices=[crm.CrmPrice("P1", "项目甲", "样品A", "外观检查", "STD-1", "", "件", 2, 100)],
    )


def _enable_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CS_QUOTE_FLOW_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(tmp_path / "inquiries.json"))


def test_quote_flow_disabled_and_project_scope_fail_before_write(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    client = _login_sales(monkeypatch, tmp_path, projects=["项目甲"])
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("scope denial must precede CRM read"))

    denied = _create_inquiry(client, _payload("项目乙"))
    assert denied.status_code == 403
    assert not (tmp_path / "inquiries.json").exists()

    monkeypatch.setenv("P0_CS_QUOTE_FLOW_ENABLED", "0")
    disabled = _create_inquiry(client)
    assert disabled.status_code == 404
    assert not (tmp_path / "inquiries.json").exists()


def test_create_requires_operation_key_and_replays_after_status_changes(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "0")
    client = TestClient(main.app)
    missing = client.post("/api/v1/customer-service/quote-inquiries", json=_payload())
    assert missing.status_code == 400

    key = "quote-create-lost-response-operation-0001"
    first = client.post("/api/v1/customer-service/quote-inquiries", headers={"Idempotency-Key": key}, json=_payload())
    assert first.status_code == 201
    inquiry_id = first.json()["inquiry"]["inquiry_id"]
    assert "_create_payload_hash" not in first.json()["inquiry"]
    submitted = client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
    assert submitted.status_code == 200

    replay = client.post("/api/v1/customer-service/quote-inquiries", headers={"Idempotency-Key": key}, json=_payload())
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["inquiry"]["inquiry_id"] == inquiry_id
    assert replay.json()["inquiry"]["status"] == "needs_human"
    assert len(customer_quote_flow.list_records()) == 1

    mismatch = client.post("/api/v1/customer-service/quote-inquiries", headers={"Idempotency-Key": key}, json=_payload("项目甲", complete=False))
    assert mismatch.status_code == 409
    assert len(customer_quote_flow.list_records()) == 1


def test_create_operation_metadata_corruption_fails_closed(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    client = TestClient(main.app)
    key = "quote-create-corrupt-operation-0001"
    first = client.post("/api/v1/customer-service/quote-inquiries", headers={"Idempotency-Key": key}, json=_payload())
    assert first.status_code == 201
    path = tmp_path / "inquiries.json"
    document = __import__("json").loads(path.read_text(encoding="utf-8"))
    duplicate = dict(document["records"][0])
    duplicate["inquiry_id"] = "QINQ-DUPLICATE"
    document["records"].append(duplicate)
    path.write_text(__import__("json").dumps(document), encoding="utf-8")

    replay = client.post("/api/v1/customer-service/quote-inquiries", headers={"Idempotency-Key": key}, json=_payload())
    assert replay.status_code == 503


@pytest.mark.parametrize("source_row,confidence,quantity", [
    (True, 0.5, 2), (1_000_001, 0.5, 2), (2, float("nan"), 2),
    (2, {"x": 1}, 2), (2, 10**1000, 2), (2, 0.5, 10**40), (2, 0.5, float("nan")),
    (2, 0.5, float("inf")), (2, 0.5, float("-inf")), (2, 0.5, 0), (2, 0.5, -1),
])
def test_create_rejects_untrusted_source_metadata_types(source_row, confidence, quantity):
    payload = _payload()
    payload["lines"][0].update(source_row=source_row, confidence=confidence, quantity=quantity)
    with pytest.raises(customer_quote_flow.QuoteFlowError):
        customer_quote_flow._clean_payload(payload)


def test_create_accepts_numeric_quantity_just_below_limit():
    payload = _payload()
    payload["lines"][0]["quantity"] = 10**40 - 1
    assert customer_quote_flow._clean_payload(payload)["lines"][0]["quantity"] == 10**40 - 1


def test_same_key_is_isolated_between_actors(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    key = "quote-create-actor-scope-operation-0001"
    first = customer_quote_flow.create(_payload(), "sales-a", key)
    second = customer_quote_flow.create(_payload(), "sales-b", key)
    assert first["inquiry_id"] != second["inquiry_id"]
    assert len(customer_quote_flow.list_records()) == 2
    assert all("_create_operation_fingerprint" not in row for row in customer_quote_flow.list_records())


def test_same_actor_and_key_concurrent_create_persists_one_record(monkeypatch, tmp_path):
    path = tmp_path / "inquiries.json"
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(path))
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    key = "quote-create-concurrent-operation-0001"
    workers = [
        context.Process(target=_create_from_worker, args=(start, results, str(path), "same-sales-user", key, _payload()))
        for _ in range(2)
    ]
    started = []
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        start.set()
        inquiry_ids = [results.get(timeout=30) for _ in workers]
        for worker in started:
            worker.join(timeout=30)
            assert worker.exitcode == 0
    finally:
        for worker in started:
            if worker.is_alive():
                worker.terminate()
        for worker in started:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            assert not worker.is_alive()
    assert inquiry_ids[0] == inquiry_ids[1]
    assert len(customer_quote_flow.list_records()) == 1

def test_submission_routes_missing_fields_to_need_info_and_blocks_repeat(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: _crm_snapshot()})())
    client = TestClient(main.app)
    draft = _create_inquiry(client, _payload(complete=False))
    assert draft.status_code == 201
    inquiry_id = draft.json()["inquiry"]["inquiry_id"]
    assert draft.json()["inquiry"]["status"] == "draft"

    submitted = client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
    inquiry = submitted.json()["inquiry"]
    assert submitted.status_code == 200
    assert inquiry["status"] == "need_info"
    assert inquiry["preview"]["lines"][0]["action"] == "ask_customer"
    duplicate = client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
    assert duplicate.status_code == 409

    updated = client.patch(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}", json=_payload())
    assert updated.status_code == 200 and updated.json()["inquiry"]["status"] == "draft"


def test_crm_preview_records_only_internal_facts_and_hands_off_unknowns(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: type("Provider", (), {"snapshot": lambda self: _crm_snapshot()})())
    client = TestClient(main.app)
    draft = _create_inquiry(client)
    inquiry_id = draft.json()["inquiry"]["inquiry_id"]
    submitted = client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")

    inquiry = submitted.json()["inquiry"]
    assert submitted.status_code == 200
    assert inquiry["status"] == "needs_human"
    assert inquiry["preview"]["decision_summary"]["total_preview"] is None
    assert inquiry["preview"]["source_mode"] == "excel"
    assert inquiry["preview"]["lines"][0]["price_preview"]["unit_price"] == 100
    assert "不计算总价" in inquiry["preview"]["warnings"][1]
    assert inquiry["history"][-1]["to"] == "needs_human"


def test_crm_preview_unavailable_keeps_context_without_synthetic_fallback(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "0")
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setattr(main.crm, "provider", lambda: pytest.fail("disabled preview must not read CRM"))
    client = TestClient(main.app)
    draft = _create_inquiry(client)
    inquiry_id = draft.json()["inquiry"]["inquiry_id"]
    result = client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
    inquiry = result.json()["inquiry"]
    assert inquiry["status"] == "needs_human"
    assert inquiry["preview"] is None
    assert inquiry["manual_reason"] == "CRM 预览未启用"
    assert inquiry["context"] == "仅内部演示询价"


def test_crm_provider_error_routes_to_human_without_demo_price(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("P0_CRM_MODE", "excel")

    class BrokenProvider:
        def snapshot(self):
            raise crm.CrmUnavailable("/private/customer/prices.xlsx")

    monkeypatch.setattr(main.crm, "provider", lambda: BrokenProvider())
    client = TestClient(main.app)
    draft = _create_inquiry(client).json()["inquiry"]
    result = client.post(f"/api/v1/customer-service/quote-inquiries/{draft['inquiry_id']}/submit")
    inquiry = result.json()["inquiry"]
    assert inquiry["status"] == "needs_human"
    assert inquiry["preview"] is None
    assert "/private/" not in inquiry["manual_reason"]


def test_private_inquiries_and_manager_queue_permissions(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    owner = _login_sales(monkeypatch, tmp_path, username="owner", projects=["项目甲"])
    inquiry = _create_inquiry(owner).json()["inquiry"]
    other = _login_sales(monkeypatch, tmp_path, username="other", projects=["项目乙"])
    assert other.get(f"/api/v1/customer-service/quote-inquiries/{inquiry['inquiry_id']}").status_code == 404
    queue = other.get("/api/v1/customer-service/quote-inquiries/admin/queue")
    assert queue.status_code == 200 and queue.json()["inquiries"] == []
    assert [row["inquiry_id"] for row in other.get("/api/v1/customer-service/quote-inquiries").json()["inquiries"]] == []


def test_admin_claim_assign_resolve_requires_reason_and_rejects_claim_collision(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("P0_CS_CRM_PREVIEW_ENABLED", "0")
    client = TestClient(main.app)
    draft = _create_inquiry(client).json()["inquiry"]
    inquiry_id = draft["inquiry_id"]
    client.post(f"/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
    monkeypatch.setattr(auth, "load_users", lambda: [{"username": "sales-target", "role": "business_manager", "active": True, "projects": ["项目甲"]}])

    missing_key = client.post(f"/api/v1/customer-service/quote-inquiries/admin/{inquiry_id}/assign",
                              json={"assignee": "sales-target", "reason": "按项目授权转派"})
    assert missing_key.status_code == 400
    invalid_assign = _admin_quote_action(
        client, inquiry_id, "assign", {"assignee": "sales-target", "reason": ""},
        "quote-admin-assign-invalid-0001",
    )
    assert invalid_assign.status_code == 400
    assignment_payload = {"assignee": "sales-target", "reason": "按项目授权转派"}
    assignment_key = "quote-admin-assign-committed-0001"
    monkeypatch.setattr(auth, "load_users", lambda: [])
    invalid_target = _admin_quote_action(client, inquiry_id, "assign", assignment_payload, assignment_key)
    assert invalid_target.status_code == 422
    monkeypatch.setattr(auth, "load_users", lambda: [{"username": "sales-target", "role": "business_manager", "active": True, "projects": ["项目甲"]}])
    assigned = _admin_quote_action(client, inquiry_id, "assign", assignment_payload, assignment_key)
    assert assigned.status_code == 200 and assigned.json()["inquiry"]["assigned_to"] == "sales-target"
    assert assigned.json()["idempotent_replay"] is False
    assignment_history_count = len(assigned.json()["inquiry"]["history"])
    monkeypatch.setattr(auth, "load_users", lambda: [])
    replay_assign = _admin_quote_action(client, inquiry_id, "assign", assignment_payload, assignment_key)
    assert replay_assign.status_code == 200 and replay_assign.json()["idempotent_replay"] is True
    assert len(replay_assign.json()["inquiry"]["history"]) == assignment_history_count
    changed_assign = _admin_quote_action(
        client, inquiry_id, "assign", assignment_payload | {"reason": "改变转派原因"}, assignment_key,
    )
    assert changed_assign.status_code == 409
    collision = _admin_quote_action(
        client, inquiry_id, "claim", {"reason": "抢单"}, "quote-admin-claim-collision-0001",
    )
    assert collision.status_code == 409
    resolve_payload = {"reason": "演示咨询已处理"}
    resolve_key = "quote-admin-resolve-committed-0001"
    resolved = _admin_quote_action(client, inquiry_id, "resolve", resolve_payload, resolve_key)
    assert resolved.status_code == 200 and resolved.json()["inquiry"]["status"] == "resolved"
    assert resolved.json()["inquiry"]["history"][-1]["reason"] == "演示咨询已处理"
    resolve_history_count = len(resolved.json()["inquiry"]["history"])
    replay_resolve = _admin_quote_action(client, inquiry_id, "resolve", resolve_payload, resolve_key)
    assert replay_resolve.status_code == 200 and replay_resolve.json()["idempotent_replay"] is True
    assert len(replay_resolve.json()["inquiry"]["history"]) == resolve_history_count
    replay_assign_after_resolve = _admin_quote_action(client, inquiry_id, "assign", assignment_payload, assignment_key)
    assert replay_assign_after_resolve.status_code == 200
    assert replay_assign_after_resolve.json()["idempotent_replay"] is True
    assert replay_assign_after_resolve.json()["inquiry"]["status"] == "resolved"
    assert "admin_operations" not in replay_assign_after_resolve.json()["inquiry"]


def test_admin_action_history_limit_allows_old_replay_and_rejects_new_key(monkeypatch, tmp_path):
    path = tmp_path / "inquiries.json"
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(path))
    inquiry = customer_quote_flow.create(_payload(), "sales-owner", "quote-admin-cap-create-0001")
    customer_quote_flow.submit(inquiry["inquiry_id"], "sales-owner", failure_code="source_unavailable")
    action_key = "quote-admin-cap-action-original-0001"
    first, replayed = customer_quote_flow.claim(
        inquiry["inquiry_id"], "manager-a", "接单", action_key, with_replay=True,
    )
    assert not replayed
    document = json.loads(path.read_text(encoding="utf-8"))
    operations = document["records"][0]["admin_operations"]
    operations.extend({"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99))
    path.write_text(json.dumps(document), encoding="utf-8")

    current, replayed = customer_quote_flow.claim(
        inquiry["inquiry_id"], "manager-a", "接单", action_key, with_replay=True,
    )
    assert replayed and current["history"] == first["history"]
    assert "admin_operations" not in current
    with pytest.raises(customer_quote_flow.QuoteFlowConflict, match="已达本地上限"):
        customer_quote_flow.claim(
            inquiry["inquiry_id"], "manager-a", "再次接单", "quote-admin-cap-action-new-0001",
        )


def test_corrupt_admin_action_metadata_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "inquiries.json"
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(path))
    inquiry = customer_quote_flow.create(_payload(), "sales-owner", "quote-admin-corrupt-create-0001")
    customer_quote_flow.submit(inquiry["inquiry_id"], "sales-owner", failure_code="source_unavailable")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][0]["admin_operations"] = [{"fingerprint": "bad", "payload_hash": "a" * 64}]
    original = json.dumps(document)
    path.write_text(original, encoding="utf-8")

    with pytest.raises(customer_quote_flow.QuoteFlowError, match="幂等记录格式错误"):
        customer_quote_flow.claim(
            inquiry["inquiry_id"], "manager-a", "接单", "quote-admin-corrupt-action-0001",
        )
    assert path.read_text(encoding="utf-8") == original


def test_corrupt_quote_flow_store_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "inquiries.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(path))
    with pytest.raises(customer_quote_flow.QuoteFlowError):
        customer_quote_flow.list_records()
    assert path.read_text(encoding="utf-8") == "{broken"


def test_quote_flow_write_failure_does_not_claim_creation(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_QUOTE_FLOW_PATH", str(blocker / "inquiries.json"))
    with pytest.raises(OSError):
        customer_quote_flow.create(_payload(), "sales-user")


def test_conflicting_quote_claims_are_serialized_across_processes(monkeypatch, tmp_path):
    _enable_flow(monkeypatch, tmp_path)
    record = customer_quote_flow.create(_payload(), "owner")
    record = customer_quote_flow.submit(record["inquiry_id"], "owner", failure_code="preview_disabled")
    assert record["status"] == "needs_human"

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_claim_from_worker, args=(start, results, str(tmp_path / "inquiries.json"), record["inquiry_id"], actor))
        for actor in ("sales-a", "sales-b")
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sorted(outcomes) == ["conflict", "ok"]
    final = customer_quote_flow.get(record["inquiry_id"])
    assert final["assigned_to"] in {"sales-a", "sales-b"}
    assert len([event for event in final["history"] if event["action"] == "claimed"]) == 1
