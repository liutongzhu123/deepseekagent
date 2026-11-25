import multiprocessing
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import auth, customer_order_flow, customer_registration, main


def _confirm_from_worker(start, results, order_path, registration_path, order_id, decision, registration_id, access_token):
    os.environ["P0_CUSTOMER_ORDER_PATH"] = order_path
    os.environ["P0_CUSTOMER_REGISTRATION_PATH"] = registration_path
    start.wait(timeout=30)
    try:
        order, replay = customer_order_flow.confirm_owned(
            order_id, decision, "并发演示确认", registration_id, access_token,
        )
        results.put(("ok", order["status"], replay))
    except customer_order_flow.OrderFlowConflict:
        results.put(("conflict", "", False))


def _setup(monkeypatch, tmp_path, *, approved=True):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_ORDER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    monkeypatch.setenv("P0_CUSTOMER_ORDER_PATH", str(tmp_path / "orders.json"))
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    created = customer_registration.create({
        "company_name": "演示公司甲", "contact_name": "联系人甲", "region": "演示地区",
    })
    registration = created["registration"]
    customer_registration.submit(registration["registration_id"], created["access_token"])
    if approved:
        customer_registration.review(registration["registration_id"], "approved", "演示批准", "admin-demo")
    return created, TestClient(main.app)


def _payload():
    return {
        "template_id": "演示委托模板 A", "project_name": "外观检查",
        "sample_name": "演示样品", "quantity": "2 件", "standard": "STD-DEMO",
        "notes": "不含真实客户资料",
    }


def _admin_review_key(order_id, payload):
    return "admin-review-" + hashlib.sha256(
        f"{order_id}\0{json.dumps(payload, ensure_ascii=False, sort_keys=True)}".encode()
    ).hexdigest()[:40]


def _admin_review(client, order_id, payload, operation_id=None):
    payload = dict(payload)
    if "expected_revision" not in payload:
        listed = client.get("/api/v1/customer-service/demo/admin/orders")
        payload["expected_revision"] = next(
            row["revision"] for row in listed.json()["orders"] if row["order_id"] == order_id
        )
    operation_id = operation_id or _admin_review_key(order_id, payload)
    return client.post(
        f"/api/v1/customer-service/demo/admin/orders/{order_id}/review",
        headers={"Idempotency-Key": operation_id}, json=payload,
    )


def test_order_api_is_opt_in_and_production_disabled(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("P0_CUSTOMER_ORDER_DEMO_ENABLED", "0")
    order_payload = {**_payload(), "registration_id": created["registration"]["registration_id"]}
    disabled = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers={"X-Demo-Access-Token": created["access_token"]})
    assert disabled.status_code == 404
    monkeypatch.setenv("P0_CUSTOMER_ORDER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_ENV", "production")
    production = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers={"X-Demo-Access-Token": created["access_token"]})
    assert production.status_code == 404
    assert not (tmp_path / "orders.json").exists()


def test_order_requires_approved_registration_and_valid_complete_fields(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path, approved=False)
    headers = {"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "order-op-valid-0001"}
    order_payload = {**_payload(), "registration_id": created["registration"]["registration_id"]}
    denied = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers=headers)
    assert denied.status_code == 409
    customer_registration.review(created["registration"]["registration_id"], "approved", "演示批准", "admin-demo")
    incomplete = client.post("/api/v1/customer-service/demo/orders", json={**order_payload, "sample_name": ""}, headers={**headers, "Idempotency-Key": "order-op-invalid-0001"})
    assert incomplete.status_code == 400
    valid = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers=headers)
    assert valid.status_code == 201
    assert valid.json()["idempotent_replay"] is False
    order = valid.json()["order"]
    assert order["status"] == "submitted"
    assert order["template_snapshot"]["demo_only"] is True
    assert order["template_snapshot"]["label"] == "演示委托模板 A"
    assert "access_token_hash" not in order
    replay = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers=headers)
    assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True
    assert replay.json()["order"]["order_id"] == order["order_id"]
    assert len(customer_order_flow.list_admin()) == 1
    registrations_path = tmp_path / "registrations.json"
    registration_data = json.loads(registrations_path.read_text(encoding="utf-8"))
    registration_data["registrations"][0]["status"] = "rejected"
    registrations_path.write_text(json.dumps(registration_data), encoding="utf-8")
    replay_after_status_change = client.post("/api/v1/customer-service/demo/orders", json=order_payload, headers=headers)
    assert replay_after_status_change.status_code == 201
    assert replay_after_status_change.json()["idempotent_replay"] is True
    assert replay_after_status_change.json()["order"]["order_id"] == order["order_id"]
    blocked_new = client.post(
        "/api/v1/customer-service/demo/orders", json=order_payload,
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "order-op-after-status-0001"},
    )
    assert blocked_new.status_code == 409
    mismatch = client.post(
        "/api/v1/customer-service/demo/orders",
        json={**order_payload, "notes": "different payload"}, headers=headers,
    )
    assert mismatch.status_code == 409
    assert len(customer_order_flow.list_admin()) == 1
    missing_key = client.post(
        "/api/v1/customer-service/demo/orders", json=order_payload,
        headers={"X-Demo-Access-Token": created["access_token"]},
    )
    assert missing_key.status_code == 400


def test_orders_are_isolated_by_registration_capability(monkeypatch, tmp_path):
    first, client = _setup(monkeypatch, tmp_path)
    first_headers = {"X-Demo-Access-Token": first["access_token"], "Idempotency-Key": "order-op-isolation-0001"}
    order = client.post("/api/v1/customer-service/demo/orders", json={**_payload(), "registration_id": first["registration"]["registration_id"]}, headers=first_headers).json()["order"]
    second = customer_registration.create({"company_name": "演示公司乙", "contact_name": "联系人乙", "region": "演示地区"})
    customer_registration.submit(second["registration"]["registration_id"], second["access_token"])
    customer_registration.review(second["registration"]["registration_id"], "approved", "演示批准", "admin-demo")
    second_headers = {"X-Demo-Access-Token": second["access_token"]}
    listed = client.get(f"/api/v1/customer-service/demo/orders?registration_id={second['registration']['registration_id']}", headers=second_headers)
    hidden = client.get(f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={second['registration']['registration_id']}", headers=second_headers)
    assert listed.status_code == 200 and listed.json()["orders"] == []
    assert hidden.status_code == 404
    hidden_confirmation = client.post(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}/confirmation?registration_id={second['registration']['registration_id']}",
        headers=second_headers, json={"decision": "accepted"},
    )
    assert hidden_confirmation.status_code == 404


def test_admin_review_requires_auth_reason_and_enforces_state(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    order = client.post("/api/v1/customer-service/demo/orders", json={**_payload(), "registration_id": created["registration"]["registration_id"]}, headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "order-op-admin-flow-0001"}).json()["order"]
    assert client.get("/api/v1/customer-service/demo/admin/orders").status_code == 401

    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    auth.create_user("order-admin", "admin-password", role="business_manager")
    auth.create_user("order-sales", "sales-password", role="technical")
    admin = TestClient(main.app)
    assert admin.post("/api/v1/auth/login", json={"username": "order-admin", "password": "admin-password"}).status_code == 200
    queue = admin.get("/api/v1/customer-service/demo/admin/orders")
    assert queue.status_code == 200 and queue.json()["orders"][0]["order_id"] == order["order_id"]
    sales = TestClient(main.app)
    assert sales.post("/api/v1/auth/login", json={"username": "order-sales", "password": "sales-password"}).status_code == 200
    assert sales.get("/api/v1/customer-service/demo/admin/orders").status_code == 403
    missing_review_key = admin.post(
        f"/api/v1/customer-service/demo/admin/orders/{order['order_id']}/review",
        json={"decision": "needs_info", "reason": "稳定操作号必填"},
    )
    assert missing_review_key.status_code == 400
    missing_review_revision = admin.post(
        f"/api/v1/customer-service/demo/admin/orders/{order['order_id']}/review",
        headers={"Idempotency-Key": "order-review-missing-revision-0001"},
        json={"decision": "needs_info", "reason": "版本必填"},
    )
    assert missing_review_revision.status_code == 422
    malformed_review_revision = admin.post(
        f"/api/v1/customer-service/demo/admin/orders/{order['order_id']}/review",
        headers={"Idempotency-Key": "order-review-malformed-revision-0001"},
        json={"decision": "needs_info", "reason": "版本不能是布尔值", "expected_revision": True},
    )
    assert malformed_review_revision.status_code == 422
    missing_reason = _admin_review(admin, order["order_id"], {"decision": "needs_info", "reason": ""})
    assert missing_reason.status_code == 400
    needs_info = _admin_review(admin, order["order_id"], {"decision": "needs_info", "reason": "请补充样品状态"})
    assert needs_info.status_code == 200 and needs_info.json()["order"]["status"] == "needs_info"
    assert "review_operations" not in needs_info.json()["order"]
    first_review_history = len(needs_info.json()["order"]["history"])
    first_review_payload = {
        "decision": "needs_info", "reason": "请补充样品状态", "expected_revision": 1,
    }
    replay_review = _admin_review(admin, order["order_id"], first_review_payload)
    assert replay_review.status_code == 200 and replay_review.json()["idempotent_replay"] is True
    assert len(replay_review.json()["order"]["history"]) == first_review_history
    changed_review = _admin_review(
        admin, order["order_id"], {"decision": "needs_info", "reason": "更改理由"},
        operation_id=_admin_review_key(order["order_id"], first_review_payload),
    )
    assert changed_review.status_code == 409
    owner_headers = {"X-Demo-Access-Token": created["access_token"]}
    missing_update_key = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=owner_headers, json={**_payload(), "notes": "按要求补充"},
    )
    assert missing_update_key.status_code == 400
    update_headers = {**owner_headers, "Idempotency-Key": "order-update-replay-operation-0001"}
    updated = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers, json={**_payload(), "template_id": "演示委托模板 B", "notes": "按要求补充"},
    )
    assert updated.status_code == 200
    assert updated.json()["idempotent_replay"] is False
    assert updated.json()["order"]["status"] == "submitted"
    assert updated.json()["order"]["template_snapshot"]["label"] == "演示委托模板 A"
    stale_manager_review = _admin_review(
        admin, order["order_id"],
        {"decision": "approved", "reason": "基于旧队列批准", "expected_revision": needs_info.json()["order"]["revision"]},
    )
    assert stale_manager_review.status_code == 409
    current_after_stale = client.get(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=owner_headers,
    )
    assert current_after_stale.status_code == 200
    assert current_after_stale.json()["order"]["status"] == "submitted"
    assert current_after_stale.json()["order"]["notes"] == "按要求补充"
    history_after_update = len(updated.json()["order"]["history"])
    assert "resubmission_operations" not in updated.json()["order"]
    registrations_path = tmp_path / "registrations.json"
    registration_document = json.loads(registrations_path.read_text(encoding="utf-8"))
    next(row for row in registration_document["registrations"]
         if row["registration_id"] == created["registration"]["registration_id"])["status"] = "rejected"
    registrations_path.write_text(json.dumps(registration_document), encoding="utf-8")
    replay_update = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers, json={**_payload(), "template_id": "演示委托模板 B", "notes": "按要求补充"},
    )
    assert replay_update.status_code == 200
    assert replay_update.json()["idempotent_replay"] is True
    assert len(replay_update.json()["order"]["history"]) == history_after_update
    registration_document = json.loads(registrations_path.read_text(encoding="utf-8"))
    next(row for row in registration_document["registrations"]
         if row["registration_id"] == created["registration"]["registration_id"])["status"] = "approved"
    registrations_path.write_text(json.dumps(registration_document), encoding="utf-8")
    second_needs_info = _admin_review(admin, order["order_id"], {"decision": "needs_info", "reason": "二次补充资料"})
    assert second_needs_info.status_code == 200
    history_after_rereview = len(second_needs_info.json()["order"]["history"])
    replay_after_rereview = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers, json={**_payload(), "template_id": "演示委托模板 B", "notes": "按要求补充"},
    )
    assert replay_after_rereview.status_code == 200
    assert replay_after_rereview.json()["idempotent_replay"] is True
    assert replay_after_rereview.json()["order"]["status"] == "needs_info"
    assert len(replay_after_rereview.json()["order"]["history"]) == history_after_rereview
    update_headers_b = {**owner_headers, "Idempotency-Key": "order-update-replay-operation-0002"}
    second_update = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers_b, json={**_payload(), "template_id": "演示委托模板 B", "notes": "第二轮补件"},
    )
    assert second_update.status_code == 200 and second_update.json()["idempotent_replay"] is False
    third_needs_info = _admin_review(admin, order["order_id"], {"decision": "needs_info", "reason": "第三轮补充资料"})
    assert third_needs_info.status_code == 200
    history_after_third_review = len(third_needs_info.json()["order"]["history"])
    replay_first_review_after_later_change = _admin_review(admin, order["order_id"], first_review_payload)
    assert replay_first_review_after_later_change.status_code == 200
    assert replay_first_review_after_later_change.json()["idempotent_replay"] is True
    assert replay_first_review_after_later_change.json()["order"]["status"] == "needs_info"
    assert len(replay_first_review_after_later_change.json()["order"]["history"]) == history_after_third_review
    old_operation_replay = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers, json={**_payload(), "template_id": "演示委托模板 B", "notes": "按要求补充"},
    )
    assert old_operation_replay.status_code == 200
    assert old_operation_replay.json()["idempotent_replay"] is True
    assert old_operation_replay.json()["order"]["status"] == "needs_info"
    assert old_operation_replay.json()["order"]["notes"] == "第二轮补件"
    assert len(old_operation_replay.json()["order"]["history"]) == history_after_third_review
    changed_replay = client.patch(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}?registration_id={created['registration']['registration_id']}",
        headers=update_headers, json={**_payload(), "template_id": "演示委托模板 B", "notes": "不同补件内容"},
    )
    assert changed_replay.status_code == 409
    premature_confirmation = client.post(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}/confirmation?registration_id={created['registration']['registration_id']}",
        headers=owner_headers, json={"decision": "accepted"},
    )
    assert premature_confirmation.status_code == 409
    approved = _admin_review(admin, order["order_id"], {"decision": "approved", "reason": "演示审核完成"})
    assert approved.status_code == 200 and approved.json()["order"]["status"] == "approved"
    duplicate = _admin_review(admin, order["order_id"], {"decision": "rejected", "reason": "重复终态"})
    assert duplicate.status_code == 409
    assert approved.json()["order"]["history"][-1]["reason"] == "演示审核完成"
    confirmed = client.post(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}/confirmation?registration_id={created['registration']['registration_id']}",
        headers=owner_headers, json={"decision": "accepted", "note": "演示客户核对完成"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["demo"] is True
    assert confirmed.json()["idempotent_replay"] is False
    assert confirmed.json()["order"]["status"] == "customer_confirmed"
    assert confirmed.json()["order"]["history"][-1]["actor"] == "customer-demo"
    assert confirmed.json()["order"].get("actual_order_created") is None
    registrations_path = tmp_path / "registrations.json"
    registration_document = json.loads(registrations_path.read_text(encoding="utf-8"))
    next(row for row in registration_document["registrations"]
         if row["registration_id"] == created["registration"]["registration_id"])["status"] = "rejected"
    registrations_path.write_text(json.dumps(registration_document), encoding="utf-8")
    duplicate_confirmation = client.post(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}/confirmation?registration_id={created['registration']['registration_id']}",
        headers=owner_headers, json={"decision": "accepted"},
    )
    assert duplicate_confirmation.status_code == 200
    assert duplicate_confirmation.json()["idempotent_replay"] is True
    assert len(duplicate_confirmation.json()["order"]["history"]) == len(confirmed.json()["order"]["history"])
    conflicting_confirmation = client.post(
        f"/api/v1/customer-service/demo/orders/{order['order_id']}/confirmation?registration_id={created['registration']['registration_id']}",
        headers=owner_headers, json={"decision": "declined"},
    )
    assert conflicting_confirmation.status_code == 409


def test_order_review_limit_allows_replay_but_rejects_new_operation(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    order = client.post(
        "/api/v1/customer-service/demo/orders",
        json={**_payload(), "registration_id": created["registration"]["registration_id"]},
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "order-review-cap-create-0001"},
    ).json()["order"]
    operation_id = "admin-order-review-cap-original-0001"
    reviewer = "admin-demo"
    payload = {"decision": "approved", "reason": "批准", "expected_revision": 1}
    fingerprint = hashlib.sha256(
        f"order-review\0{order['order_id']}\0{reviewer}\0{operation_id}".encode("utf-8")
    ).hexdigest()
    payload_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    path = tmp_path / "orders.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    operations = [{"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99)]
    operations.append({"fingerprint": fingerprint, "payload_hash": payload_hash})
    document["orders"][0]["review_operations"] = operations
    path.write_text(json.dumps(document), encoding="utf-8")

    replay, replayed = customer_order_flow.review(
        order["order_id"], "approved", "批准", reviewer, operation_id,
        expected_revision=1, with_replay=True,
    )
    assert replayed is True
    assert replay["status"] == "submitted"
    try:
        customer_order_flow.review(
            order["order_id"], "approved", "新审核", reviewer,
            "admin-order-review-cap-new-0001", expected_revision=1,
        )
    except customer_order_flow.OrderFlowConflict as exc:
        assert "已达本地上限" in str(exc)
    else:
        raise AssertionError("new order reviews must stop at the history limit")


def test_corrupt_order_file_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_ORDER_PATH", str(path))
    with pytest.raises(customer_order_flow.OrderFlowError):
        customer_order_flow.list_admin()


def test_corrupt_resubmission_idempotency_history_fails_closed(monkeypatch, tmp_path):
    created, _ = _setup(monkeypatch, tmp_path)
    registration_id = created["registration"]["registration_id"]
    access_token = created["access_token"]
    order, _ = customer_order_flow.create(_payload(), registration_id, access_token, "order-create-corrupt-history-01")
    customer_order_flow.review(order["order_id"], "needs_info", "请补件", "admin-demo")
    path = tmp_path / "orders.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orders"][0]["resubmission_operations"] = [{"fingerprint": "broken"}]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(customer_order_flow.OrderFlowError, match="幂等记录格式错误"):
        customer_order_flow.update_owned(
            order["order_id"], _payload(), registration_id, access_token,
            "order-update-corrupt-history-01",
        )


def test_resubmission_history_limit_preserves_old_replay(monkeypatch, tmp_path):
    created, _ = _setup(monkeypatch, tmp_path)
    registration_id = created["registration"]["registration_id"]
    access_token = created["access_token"]
    order, _ = customer_order_flow.create(_payload(), registration_id, access_token, "order-create-history-limit-01")
    customer_order_flow.review(order["order_id"], "needs_info", "请补件", "admin-demo")
    update_key = "order-update-history-limit-a-0001"
    first, replayed = customer_order_flow.update_owned(order["order_id"], _payload(), registration_id, access_token, update_key)
    assert replayed is False
    customer_order_flow.review(order["order_id"], "needs_info", "再次补件", "admin-demo")
    history_after_review = customer_order_flow.get_owned(order["order_id"], access_token)["history"]

    path = tmp_path / "orders.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    record = data["orders"][0]
    operations = record["resubmission_operations"]
    operations.extend({"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99))
    path.write_text(json.dumps(data), encoding="utf-8")

    replay, replayed = customer_order_flow.update_owned(order["order_id"], _payload(), registration_id, access_token, update_key)
    assert replayed is True
    assert replay["history"] == history_after_review
    with pytest.raises(customer_order_flow.OrderFlowConflict, match="已达演示上限"):
        customer_order_flow.update_owned(
            order["order_id"], {**_payload(), "notes": "新的操作"}, registration_id, access_token,
            "order-update-history-limit-b-0001",
        )


def test_order_write_failure_does_not_claim_submission(monkeypatch, tmp_path):
    created, _ = _setup(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_ORDER_PATH", str(blocker / "orders.json"))
    with pytest.raises(OSError):
        customer_order_flow.create(_payload(), created["registration"]["registration_id"], created["access_token"], "order-op-write-fail-0001")


def test_conflicting_customer_decisions_are_serialized_across_processes(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    registration_id = created["registration"]["registration_id"]
    access_token = created["access_token"]
    order = client.post(
        "/api/v1/customer-service/demo/orders",
        json={**_payload(), "registration_id": registration_id},
        headers={"X-Demo-Access-Token": access_token, "Idempotency-Key": "order-op-concurrency-0001"},
    ).json()["order"]
    assert customer_order_flow.review(order["order_id"], "approved", "并发测试", "admin-demo")["status"] == "approved"

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_confirm_from_worker, args=(
            start, results, str(tmp_path / "orders.json"), str(tmp_path / "registrations.json"),
            order["order_id"], decision, registration_id, access_token,
        ))
        for decision in ("accepted", "declined")
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sum(outcome[0] == "ok" for outcome in outcomes) == 1
    assert sum(outcome[0] == "conflict" for outcome in outcomes) == 1
    final = customer_order_flow.get_owned(order["order_id"], access_token)
    assert final["status"] in {"customer_confirmed", "customer_declined"}
    assert len([event for event in final["history"] if event["action"] in {"accepted", "declined"}]) == 1
