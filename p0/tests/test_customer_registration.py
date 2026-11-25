import json
import multiprocessing
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import admin_config, auth, customer_registration, main


def _review_registration_key(registration_id, payload):
    return "registration-review-" + hashlib.sha256(
        f"{registration_id}\0{json.dumps(payload, ensure_ascii=False, sort_keys=True)}".encode()
    ).hexdigest()[:40]


def _review_registration(client, registration_id, payload, operation_id=None):
    operation_id = operation_id or _review_registration_key(registration_id, payload)
    return client.post(
        f"/api/v1/customer-service/demo/admin/registrations/{registration_id}/review",
        headers={"Idempotency-Key": operation_id}, json=payload,
    )


def _create_registration_from_worker(start, results, registration_path, suffix):
    import os
    os.environ["P0_CUSTOMER_REGISTRATION_PATH"] = registration_path
    start.wait(timeout=30)
    created = customer_registration.create({
        "company_name": f"并发演示企业{suffix}", "contact_name": f"联系人{suffix}", "region": "演示地区",
    })
    results.put(created["registration"]["registration_id"])


def _replace_admin_config_from_worker(started, lock_blocked, finished, config_path, config):
    import fcntl
    import os
    admin_config.CONFIG_PATH = Path(config_path)
    started.set()
    lock_path = Path(config_path).with_name(Path(config_path).name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BlockingIOError:
            lock_blocked.set()
    finally:
        os.close(descriptor)
    with admin_config._locked():
        admin_config._write(config)
        admin_config._invalidate_cache()
    finished.set()


def test_customer_registration_demo_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("P0_CUSTOMER_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    client = TestClient(main.app)
    response = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-disabled-op-0001"}, json={
        "company_name": "演示企业", "contact_name": "演示联系人", "region": "上海",
    })
    assert response.status_code == 404
    assert not (tmp_path / "registrations.json").exists()

    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_ENV", "production")
    blocked = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-production-op-0001"}, json={
        "company_name": "演示企业", "contact_name": "演示联系人", "region": "上海",
    })
    assert blocked.status_code == 404


def test_registration_owner_submit_review_needs_info_and_approve(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    config_path = tmp_path / "rules.json"
    monkeypatch.setattr(admin_config, "CONFIG_PATH", config_path)
    config = admin_config.default_config()
    config["revision"] = 23
    config["business"]["blacklist"]["companies"] = ["演示企业"]
    config["business"]["whitelist"]["companies"] = ["演示企业"]
    config["business"]["whitelist"]["keywords"] = ["演示企业"]
    config["business"]["intermediary"] = {
        "enabled": True, "block": True, "keywords": ["演示"]
    }
    config["business"]["region_routing"] = {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "demo-manager"}],
        "backup_manager_id": "demo-backup",
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    admin_config.reset_cache()
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    client = TestClient(main.app)
    payload = {"company_name": "演示企业", "contact_name": "演示联系人", "region": "上海", "notes": "测试"}

    created = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-owner-create-op-0001"}, json=payload)
    assert created.status_code == 201 and created.json()["demo"] is True
    registration = created.json()["registration"]
    registration_id = registration["registration_id"]
    token = created.json()["access_token"]
    assert registration["status"] == "draft"
    assert "access_token" not in registration

    assert client.get(f"/api/v1/customer-service/demo/registrations/{registration_id}").status_code == 403
    headers = {"X-Demo-Access-Token": token}
    submitted = client.post(f"/api/v1/customer-service/demo/registrations/{registration_id}/submit", headers=headers)
    assert submitted.status_code == 200 and submitted.json()["registration"]["status"] == "submitted"
    assert client.post(f"/api/v1/customer-service/demo/registrations/{registration_id}/submit", headers=headers).status_code == 400
    assert client.patch(f"/api/v1/customer-service/demo/registrations/{registration_id}",
                        headers={"X-Demo-Access-Token": "wrong"}, json=payload).status_code == 400
    listed = client.get("/api/v1/customer-service/demo/admin/registrations")
    assert listed.status_code == 200 and len(listed.json()["registrations"]) == 1
    assert listed.json()["registrations"][0]["status"] == "submitted"
    submitted_revision = listed.json()["registrations"][0]["revision"]
    assert isinstance(submitted_revision, int) and submitted_revision > 0
    risk = listed.json()["registrations"][0]["rule_review"]
    assert risk["status"] == "candidate"
    assert risk["decision"] == "manual_review"
    assert risk["requires_human_review"] is True
    assert risk["config_revision"] == 23
    assert risk["blacklist_match"] is True
    assert risk["whitelist_match"] is True
    assert risk["whitelist_keyword_match"] is True
    assert risk["company_list_conflict"] is True
    assert risk["intermediary_keyword_match"] is True
    assignment = listed.json()["registrations"][0]["assignment_preview"]
    assert assignment["status"] == "matched_candidate"
    assert assignment["candidate_manager_id"] == "demo-manager"
    assert assignment["assigned"] is False
    assert assignment["notification_sent"] is False
    assert "rule_review" not in client.get(
        f"/api/v1/customer-service/demo/registrations/{registration_id}", headers=headers
    ).json()["registration"]

    missing_review_key = client.post(
        f"/api/v1/customer-service/demo/admin/registrations/{registration_id}/review",
        json={"decision": "needs_info", "reason": "审核必须可安全重试"},
    )
    assert missing_review_key.status_code == 400


    missing_revision = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "缺少规则版本"})
    assert missing_revision.status_code == 422
    assert client.get("/api/v1/customer-service/demo/admin/registrations").json()["registrations"][0]["status"] == "submitted"

    stale_review = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "配置版本已过期", "rule_config_revision": 22})
    assert stale_review.status_code == 409
    still_submitted = client.get("/api/v1/customer-service/demo/admin/registrations").json()["registrations"][0]
    assert still_submitted["status"] == "submitted"
    fractional_rule_revision = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "版本必须精确", "rule_config_revision": 23.9,
              "registration_revision": submitted_revision})
    assert fractional_rule_revision.status_code == 422
    oversized_rule_revision = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "版本长度异常", "rule_config_revision": "9" * 5000,
              "registration_revision": submitted_revision})
    assert oversized_rule_revision.status_code == 422

    stale_registration = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "登记内容版本已过期", "rule_config_revision": 23,
              "registration_revision": submitted_revision - 1})
    assert stale_registration.status_code == 409
    missing_registration_revision = _review_registration(client, registration_id, {"decision": "needs_info", "reason": "缺少登记版本", "rule_config_revision": 23})
    assert missing_registration_revision.status_code == 422
    try:
        customer_registration.review(
            registration_id, "needs_info", "候选企业名已过期", "test", rule_review=risk,
            expected_revision=submitted_revision, expected_company_name="旧企业名称",
        )
    except customer_registration.RegistrationConflict as exc:
        assert "企业名称" in str(exc)
    else:
        raise AssertionError("candidate snapshot must be bound to the current company name")

    needs_info_payload = {"decision": "needs_info", "reason": "演示补充材料", "rule_config_revision": 23,
              "registration_revision": submitted_revision}
    needs_info = _review_registration(client, registration_id, needs_info_payload)
    assert needs_info.status_code == 200 and needs_info.json()["registration"]["status"] == "needs_info"
    assert "review_operations" not in needs_info.json()["registration"]
    review_history_count = len(needs_info.json()["registration"]["history"])
    replay_review = _review_registration(client, registration_id, needs_info_payload)
    assert replay_review.status_code == 200 and replay_review.json()["idempotent_replay"] is True
    assert len(replay_review.json()["registration"]["history"]) == review_history_count
    changed_review = _review_registration(client, registration_id, needs_info_payload | {"reason": "不同审核原因"},
                                          operation_id=_review_registration_key(registration_id, needs_info_payload))
    assert changed_review.status_code == 409
    review_event = customer_registration.audit_events()[-1]
    assert review_event["rule_review"]["config_revision"] == 23
    assert review_event["rule_review"]["company_list_conflict"] is True
    assert review_event["rule_review"]["whitelist_keyword_match"] is True
    assert review_event["rule_review"]["routing_status"] == "matched_candidate"
    assert review_event["rule_review"]["routing_candidate_manager_id"] == "demo-manager"
    assert review_event["rule_review"]["routing_status"] == "matched_candidate"
    assert review_event["rule_review"]["routing_route_type"] == "exact_region"
    assert review_event["rule_review"]["routing_config_revision"] == 23
    update_headers = {**headers, "Idempotency-Key": "registration-update-operation-0001"}
    update_payload = payload | {"notes": "已补充"}
    updated = client.patch(f"/api/v1/customer-service/demo/registrations/{registration_id}",
                           headers=update_headers, json=update_payload)
    assert updated.status_code == 200 and updated.json()["registration"]["status"] == "draft"
    assert updated.json()["idempotent_replay"] is False
    assert "update_operations" not in updated.json()["registration"]
    update_history = updated.json()["registration"]["history"]
    replay_update = client.patch(f"/api/v1/customer-service/demo/registrations/{registration_id}",
                                 headers=update_headers, json=update_payload)
    assert replay_update.status_code == 200 and replay_update.json()["idempotent_replay"] is True
    assert replay_update.json()["registration"]["history"] == update_history
    changed_update = client.patch(f"/api/v1/customer-service/demo/registrations/{registration_id}",
                                  headers=update_headers, json=payload | {"notes": "不同资料"})
    assert changed_update.status_code == 409
    assert client.post(f"/api/v1/customer-service/demo/registrations/{registration_id}/submit", headers=headers).status_code == 200
    resubmitted_revision = client.get("/api/v1/customer-service/demo/admin/registrations").json()["registrations"][0]["revision"]
    approved = _review_registration(client, registration_id, {"decision": "approved", "reason": "本地演示审核通过", "rule_config_revision": 23,
              "registration_revision": resubmitted_revision})
    assert approved.status_code == 200
    review_history_after_approval = len(approved.json()["registration"]["history"])
    replay_old_review = _review_registration(client, registration_id, needs_info_payload)
    assert replay_old_review.status_code == 200
    assert replay_old_review.json()["idempotent_replay"] is True
    assert replay_old_review.json()["registration"]["status"] == "approved"
    assert len(replay_old_review.json()["registration"]["history"]) == review_history_after_approval
    assert "review_operations" not in replay_old_review.json()["registration"]
    replay_after_approval = client.patch(f"/api/v1/customer-service/demo/registrations/{registration_id}",
                                         headers=update_headers, json=update_payload)
    assert replay_after_approval.status_code == 200
    assert replay_after_approval.json()["idempotent_replay"] is True
    assert replay_after_approval.json()["registration"]["status"] == "approved"
    assert replay_after_approval.json()["registration"]["history"] == approved.json()["registration"]["history"]
    result = client.get(f"/api/v1/customer-service/demo/registrations/{registration_id}", headers=headers)
    assert result.json()["registration"]["status"] == "approved"
    assert result.json()["registration"]["history"][-1]["actor"] == "test"
    assert "rule_review" not in result.json()["registration"]["history"][-1]
    assert "assignment_preview" not in result.json()["registration"]["history"][-1]
    assert "whitelist_keyword_match" not in str(result.json()["registration"]["history"])

    config_path.write_text("{broken", encoding="utf-8")
    admin_config.reset_cache()
    unavailable = client.get("/api/v1/customer-service/demo/admin/registrations")
    assert unavailable.status_code == 200
    unavailable_risk = unavailable.json()["registrations"][0]["rule_review"]
    assert unavailable_risk["status"] == "unavailable"
    assert unavailable_risk["decision"] == "manual_review"
    assert unavailable_risk["requires_human_review"] is True
    unavailable_assignment = unavailable.json()["registrations"][0]["assignment_preview"]
    assert unavailable_assignment["status"] == "unavailable"
    assert unavailable_assignment["candidate_manager_id"] == ""

    document = customer_registration._read()
    assert len(document["events"]) == 6
    assert token not in str(document)
    assert "access_token_hash" in document["registrations"][0]


def test_registration_review_holds_config_file_lock_through_decision(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    config_path = tmp_path / "rules.json"
    monkeypatch.setattr(admin_config, "CONFIG_PATH", config_path)
    config = admin_config.default_config()
    config["revision"] = 23
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    admin_config.reset_cache()
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    client = TestClient(main.app)
    created = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-cross-lock-create-0001"}, json={
        "company_name": "锁测试企业", "contact_name": "联系人", "region": "上海",
    }).json()
    registration_id = created["registration"]["registration_id"]
    assert client.post(
        f"/api/v1/customer-service/demo/registrations/{registration_id}/submit",
        headers={"X-Demo-Access-Token": created["access_token"]},
    ).status_code == 200
    target = client.get("/api/v1/customer-service/demo/admin/registrations").json()["registrations"][0]

    in_review = threading.Event()
    release_review = threading.Event()
    original_assignment_preview = main._registration_assignment_preview

    def pause_during_review(region, snapshot=None):
        in_review.set()
        assert release_review.wait(timeout=10)
        return original_assignment_preview(region, snapshot)

    monkeypatch.setattr(main, "_registration_assignment_preview", pause_during_review)
    config_after = admin_config.default_config()
    config_after["revision"] = 24
    context = multiprocessing.get_context("spawn")
    writer_started = context.Event()
    writer_blocked = context.Event()
    writer_finished = context.Event()
    writer = None
    with admin_config._locked():
        pass
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            response_future = executor.submit(_review_registration, client, registration_id, {
                "decision": "approved", "reason": "跨进程锁验证",
                "rule_config_revision": 23, "registration_revision": target["revision"],
            })
            assert in_review.wait(timeout=5)
            writer = context.Process(
                target=_replace_admin_config_from_worker,
                args=(writer_started, writer_blocked, writer_finished, str(config_path), config_after),
            )
            writer.start()
            assert writer_started.wait(timeout=5)
            assert writer_blocked.wait(timeout=5), "配置写进程未观察到审核持有的文件锁"
            assert not writer_finished.is_set(), "配置写进程不应在登记审核提交前写入"
            # The route is deliberately paused inside the config lock. Release
            # it before waiting for the request thread, otherwise the test
            # deadlocks waiting on the event it only sets in finally.
            release_review.set()
            response = response_future.result(timeout=10)
    finally:
        release_review.set()
        if writer is not None:
            writer.join(timeout=10)
            if writer.is_alive():
                writer.terminate()
                writer.join(timeout=5)
    assert writer is not None
    assert writer.exitcode == 0
    assert response.status_code == 200
    assert response.json()["registration"]["status"] == "approved"
    assert writer_finished.is_set()
    admin_config.reset_cache()
    assert admin_config.describe()["config"]["revision"] == 24


def test_registration_create_replays_same_capability_and_rejects_key_reuse(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    client = TestClient(main.app)
    payload = {"company_name": "创建重试演示企业", "contact_name": "联系人", "region": "上海"}
    headers = {"Idempotency-Key": "registration-create-retry-key-0001"}

    missing_key = client.post("/api/v1/customer-service/demo/registrations", json=payload)
    assert missing_key.status_code == 400
    assert customer_registration.list_admin()["registrations"] == []
    created = client.post("/api/v1/customer-service/demo/registrations", json=payload, headers=headers)
    replay = client.post("/api/v1/customer-service/demo/registrations", json=payload, headers=headers)
    assert created.status_code == replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["registration"]["registration_id"] == created.json()["registration"]["registration_id"]
    assert replay.json()["access_token"] == created.json()["access_token"]
    assert "creation_operation_hash" not in replay.json()["registration"]
    assert len(customer_registration.list_admin()["registrations"]) == 1

    conflict = client.post(
        "/api/v1/customer-service/demo/registrations",
        json={**payload, "company_name": "不同内容"}, headers=headers,
    )
    assert conflict.status_code == 409
    assert len(customer_registration.list_admin()["registrations"]) == 1


def test_registration_validation_and_employee_admin_permission(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    auth.create_user("review-sales", "sales-password", role="sales")
    client = TestClient(main.app)
    assert client.post("/api/v1/auth/login", json={"username": "review-sales", "password": "sales-password"}).status_code == 200

    invalid = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-invalid-op-0001"}, json={"company_name": "", "contact_name": "x", "region": "上海"})
    assert invalid.status_code == 400
    for index, invalid_value in enumerate((None, 123, {"name": "伪造对象"}), start=2):
        malformed = client.post(
            "/api/v1/customer-service/demo/registrations",
            headers={"Idempotency-Key": f"reg-invalid-op-{index:04d}"},
            json={"company_name": invalid_value, "contact_name": "联系人", "region": "上海"},
        )
        assert malformed.status_code == 400
    created = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-permission-op-0001"}, json={
        "company_name": "演示企业", "contact_name": "联系人", "region": "上海",
    }).json()
    registration_id = created["registration"]["registration_id"]
    assert client.get("/api/v1/customer-service/demo/admin/registrations").status_code == 403
    assert client.post(f"/api/v1/customer-service/demo/admin/registrations/{registration_id}/review",
                       json={"decision": "approved", "reason": "测试"}).status_code == 403


def test_corrupt_registration_store_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "registrations.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(path))
    try:
        customer_registration.list_admin()
    except customer_registration.RegistrationError as exc:
        assert "不可读" in str(exc)
    else:
        raise AssertionError("corrupt registration storage must fail closed")
    assert path.read_text(encoding="utf-8") == "{broken"


def _registration_needing_info(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    created = customer_registration.create({
        "company_name": "幂等测试企业", "contact_name": "联系人", "region": "上海",
    })
    registration_id = created["registration"]["registration_id"]
    customer_registration.submit(registration_id, created["access_token"])
    customer_registration.review(registration_id, "needs_info", "需补充资料", "admin-demo")
    return created


def test_registration_update_idempotency_history_fails_closed_when_corrupt(monkeypatch, tmp_path):
    created = _registration_needing_info(monkeypatch, tmp_path)
    path = tmp_path / "registrations.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["registrations"][0]["update_operations"] = [{"fingerprint": "invalid"}]
    path.write_text(json.dumps(document), encoding="utf-8")

    try:
        customer_registration.update_owned(
            created["registration"]["registration_id"], created["access_token"],
            {"company_name": "幂等测试企业", "contact_name": "联系人", "region": "上海"},
            "registration-update-corrupt-operation-01",
        )
    except customer_registration.RegistrationError as exc:
        assert "幂等记录格式错误" in str(exc)
    else:
        raise AssertionError("corrupt update operation history must fail closed")


def test_registration_update_limit_allows_replay_but_rejects_new_operation(monkeypatch, tmp_path):
    created = _registration_needing_info(monkeypatch, tmp_path)
    registration_id = created["registration"]["registration_id"]
    payload = {"company_name": "幂等测试企业", "contact_name": "联系人", "region": "上海"}
    operation_id = "registration-update-history-a-0001"
    first, replayed = customer_registration.update_owned(
        registration_id, created["access_token"], payload, operation_id,
    )
    assert replayed is False
    customer_registration.submit(registration_id, created["access_token"])
    customer_registration.review(registration_id, "needs_info", "再次补件", "admin-demo")
    current_history = customer_registration.get_owned(registration_id, created["access_token"])["registration"]["history"]
    path = tmp_path / "registrations.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    operations = document["registrations"][0]["update_operations"]
    operations.extend({"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99))
    path.write_text(json.dumps(document), encoding="utf-8")

    replay, replayed = customer_registration.update_owned(
        registration_id, created["access_token"], payload, operation_id,
    )
    assert replayed is True
    assert replay["registration"]["history"] == current_history
    assert replay["registration"]["history"] != first["registration"]["history"]
    try:
        customer_registration.update_owned(
            registration_id, created["access_token"], payload | {"notes": "新操作"},
            "registration-update-history-b-0001",
        )
    except customer_registration.RegistrationConflict as exc:
        assert "已达演示上限" in str(exc)
    else:
        raise AssertionError("new registration updates must stop at the history limit")


def test_registration_review_limit_allows_replay_but_rejects_new_operation(monkeypatch, tmp_path):
    created = _registration_needing_info(monkeypatch, tmp_path)
    registration_id = created["registration"]["registration_id"]
    customer_registration.submit(registration_id, created["access_token"])
    reviewer = "admin-demo"
    operation_id = "registration-review-limit-original-0001"
    payload = {"decision": "needs_info", "reason": "补充材料"}
    fingerprint, payload_hash = customer_registration._review_operation_values(
        registration_id, reviewer, operation_id, payload,
    )
    path = tmp_path / "registrations.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    operations = [{"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99)]
    operations.append({"fingerprint": fingerprint, "payload_hash": payload_hash})
    document["registrations"][0]["review_operations"] = operations
    path.write_text(json.dumps(document), encoding="utf-8")

    before = customer_registration.get_owned(registration_id, created["access_token"])["registration"]
    replay, replayed = customer_registration.review(
        registration_id, "needs_info", "补充材料", reviewer,
        operation_id=operation_id, operation_payload=payload, with_replay=True,
    )
    assert replayed is True
    assert replay["history"] == before["history"]
    try:
        customer_registration.review(
            registration_id, "needs_info", "第二次补充", reviewer,
            operation_id="registration-review-limit-new-0001",
            operation_payload={"decision": "needs_info", "reason": "第二次补充"},
        )
    except customer_registration.RegistrationConflict as exc:
        assert "已达本地上限" in str(exc)
    else:
        raise AssertionError("new registration reviews must stop at the history limit")


def test_unavailable_business_rules_pause_registration_review(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    config_path = tmp_path / "rules.json"
    config_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(admin_config, "CONFIG_PATH", config_path)
    admin_config.reset_cache()
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    client = TestClient(main.app)
    created = client.post("/api/v1/customer-service/demo/registrations", headers={"Idempotency-Key": "reg-review-op-0001"}, json={
        "company_name": "演示企业", "contact_name": "联系人", "region": "上海",
    }).json()
    registration_id = created["registration"]["registration_id"]
    token = created["access_token"]
    assert client.post(
        f"/api/v1/customer-service/demo/registrations/{registration_id}/submit",
        headers={"X-Demo-Access-Token": token},
    ).status_code == 200

    response = _review_registration(client, registration_id, {"decision": "approved", "reason": "规则配置不可用"})
    assert response.status_code == 503
    assert customer_registration.list_admin()["registrations"][0]["status"] == "submitted"


def test_concurrent_registration_creates_are_not_lost_across_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_create_registration_from_worker, args=(start, results, str(tmp_path / "registrations.json"), suffix))
        for suffix in ("甲", "乙")
    ]
    for worker in workers:
        worker.start()
    start.set()
    registration_ids = [results.get(timeout=30), results.get(timeout=30)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    listed = customer_registration.list_admin()
    assert len(set(registration_ids)) == 2
    assert len(listed["registrations"]) == 2
    assert listed["version"] == 2
