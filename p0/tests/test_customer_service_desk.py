import json
import multiprocessing
import os

import pytest
from fastapi.testclient import TestClient

from app import admin_config, auth, customer_registration, customer_service_desk, main


def _create_idempotent_operation(registration_path, conversations_path, registration_id, access_token, barrier, result_queue):
    os.environ["P0_CUSTOMER_REGISTRATION_PATH"] = registration_path
    os.environ["P0_CUSTOMER_SERVICE_DESK_PATH"] = conversations_path
    barrier.wait(timeout=30)
    _, replayed = customer_service_desk.create(registration_id, access_token, "并发重试消息", "mini-concurrent-operation-001")
    result_queue.put(replayed)


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("P0_CUSTOMER_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_CUSTOMER_REGISTRATION_PATH", str(tmp_path / "registrations.json"))
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_PATH", str(tmp_path / "conversations.json"))
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    created = customer_registration.create({
        "company_name": "演示会话企业", "contact_name": "演示联系人", "region": "演示地区",
    })
    customer_registration.submit(created["registration"]["registration_id"], created["access_token"])
    return created, TestClient(main.app)


def _create(client, created, text="请咨询演示事项"):
    return client.post(
        "/api/v1/customer-service/demo/conversations",
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "mini-test-create-operation-0001"},
        json={"registration_id": created["registration"]["registration_id"], "text": text},
    )


def _admin(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    auth.create_user("desk-admin", "admin-password", role="admin")
    auth.create_user("desk-sales", "sales-password", role="sales")
    client = TestClient(main.app)
    assert client.post("/api/v1/auth/login", json={"username": "desk-admin", "password": "admin-password"}).status_code == 200
    return client


def _admin_action(client, conversation_id, action, payload, operation_id=None):
    headers = {"Idempotency-Key": operation_id} if operation_id else {}
    return client.post(
        f"/api/v1/customer-service/demo/admin/conversations/{conversation_id}/{action}",
        headers=headers, json=payload,
    )


def test_conversation_demo_is_opt_in_and_production_disabled(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    payload = {"registration_id": created["registration"]["registration_id"], "text": "你好"}
    headers = {"X-Demo-Access-Token": created["access_token"]}
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED", "0")
    assert client.post("/api/v1/customer-service/demo/conversations", json=payload, headers=headers).status_code == 404
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED", "1")
    monkeypatch.setenv("P0_ENV", "production")
    assert client.post("/api/v1/customer-service/demo/conversations", json=payload, headers=headers).status_code == 404
    assert not (tmp_path / "conversations.json").exists()


def test_customer_messages_are_capability_scoped_and_not_external_delivery(monkeypatch, tmp_path):
    first, client = _setup(monkeypatch, tmp_path)
    created = _create(client, first)
    assert created.status_code == 201
    result = created.json()
    conversation = result["conversation"]
    assert result["external_delivery"] is False
    assert conversation["status"] == "open"
    assert conversation["messages"][0]["text"].startswith("请咨询演示事项")
    assert "access_token_hash" not in conversation
    bad_message = client.post(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}/messages?registration_id={first['registration']['registration_id']}",
        headers={"X-Demo-Access-Token": first["access_token"]}, json={"text": "  "},
    )
    assert bad_message.status_code == 400
    added = client.post(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}/messages?registration_id={first['registration']['registration_id']}",
        headers={"X-Demo-Access-Token": first["access_token"], "Idempotency-Key": "mini-test-followup-operation-0001"}, json={"text": "第二条消息"},
    )
    assert added.status_code == 200 and len(added.json()["conversation"]["messages"]) == 2

    second = customer_registration.create({"company_name": "另一演示企业", "contact_name": "另一个人", "region": "演示地区"})
    customer_registration.submit(second["registration"]["registration_id"], second["access_token"])
    foreign = client.get(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}?registration_id={second['registration']['registration_id']}",
        headers={"X-Demo-Access-Token": second["access_token"]},
    )
    assert foreign.status_code == 404


def test_message_idempotency_replays_committed_send_and_rejects_key_reuse(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    key = "mini-test-operation-0001"
    first = client.post(
        "/api/v1/customer-service/demo/conversations",
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": key},
        json={"registration_id": created["registration"]["registration_id"], "text": "首次发送"},
    )
    assert first.status_code == 201
    first_conversation = first.json()["conversation"]
    replay = client.post(
        "/api/v1/customer-service/demo/conversations",
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": key},
        json={"registration_id": created["registration"]["registration_id"], "text": "首次发送"},
    )
    assert replay.status_code == 201
    assert replay.json()["conversation"]["conversation_id"] == first_conversation["conversation_id"]
    assert replay.json()["idempotent_replay"] is True
    assert first.json()["idempotent_replay"] is False
    assert len(replay.json()["conversation"]["messages"]) == 1
    serialized = str(replay.json())
    assert all(key not in serialized for key in ("client_operation_id", "client_operation_fingerprint", "client_payload_hash"))

    other = customer_registration.create({"company_name": "第二演示企业", "contact_name": "另一联系人", "region": "演示地区"})
    customer_registration.submit(other["registration"]["registration_id"], other["access_token"])
    isolated = client.post(
        "/api/v1/customer-service/demo/conversations",
        headers={"X-Demo-Access-Token": other["access_token"], "Idempotency-Key": key},
        json={"registration_id": other["registration"]["registration_id"], "text": "首次发送"},
    )
    assert isolated.status_code == 201
    assert isolated.json()["conversation"]["conversation_id"] != first_conversation["conversation_id"]

    changed_create = client.post(
        "/api/v1/customer-service/demo/conversations",
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": key},
        json={"registration_id": created["registration"]["registration_id"], "text": "另一条内容"},
    )
    assert changed_create.status_code == 409

    url = (
        f"/api/v1/customer-service/demo/conversations/{first_conversation['conversation_id']}/messages"
        f"?registration_id={created['registration']['registration_id']}"
    )
    headers = {"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "mini-test-operation-0002"}
    appended = client.post(url, headers=headers, json={"text": "追加消息"})
    retried = client.post(url, headers=headers, json={"text": "追加消息"})
    assert appended.status_code == retried.status_code == 200
    assert retried.json()["idempotent_replay"] is True
    assert len(retried.json()["conversation"]["messages"]) == 2
    reused = client.post(url, headers=headers, json={"text": "操作编号被错误复用"})
    assert reused.status_code == 409

    missing_key = client.post(
        url, headers={"X-Demo-Access-Token": created["access_token"]}, json={"text": "缺少操作编号"}
    )
    assert missing_key.status_code == 400


def test_cross_process_retries_write_only_one_customer_message(monkeypatch, tmp_path):
    created, _ = _setup(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    result_queue = context.Queue()
    args = (
        str(tmp_path / "registrations.json"), str(tmp_path / "conversations.json"),
        created["registration"]["registration_id"], created["access_token"], barrier, result_queue,
    )
    workers = [context.Process(target=_create_idempotent_operation, args=args) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=30)
    outcomes = [result_queue.get(timeout=30), result_queue.get(timeout=30)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    assert sorted(outcomes) == [False, True]
    rows = customer_service_desk.list_admin()
    assert len(rows) == 1 and len(rows[0]["messages"]) == 1


def test_admin_queue_claim_assign_archive_and_reason_audit(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    conversation = _create(client, created).json()["conversation"]
    assert client.get("/api/v1/customer-service/demo/admin/conversations").status_code == 401
    admin = _admin(monkeypatch, tmp_path)
    auth.create_user("desk-admin-2", "admin-password-2", role="admin")
    assert admin.get("/api/v1/customer-service/demo/admin/conversations").status_code == 200
    sales = TestClient(main.app)
    assert sales.post("/api/v1/auth/login", json={"username": "desk-sales", "password": "sales-password"}).status_code == 200
    assert sales.get("/api/v1/customer-service/demo/admin/conversations").status_code == 403
    missing_reason = admin.post(f"/api/v1/customer-service/demo/admin/conversations/{conversation['conversation_id']}/claim", json={"reason": ""})
    assert missing_reason.status_code == 400
    assert _admin_action(admin, conversation["conversation_id"], "claim", {"reason": "管理员接手演示"}).status_code == 400
    claim_payload = {"reason": "管理员接手演示"}
    claim_key = "desk-admin-claim-operation-0001"
    claimed = _admin_action(admin, conversation["conversation_id"], "claim", claim_payload, claim_key)
    assert claimed.status_code == 200 and claimed.json()["conversation"]["status"] == "assigned"
    assert claimed.json()["idempotent_replay"] is False
    claim_history = len(claimed.json()["conversation"]["history"])
    claim_replay = _admin_action(admin, conversation["conversation_id"], "claim", claim_payload, claim_key)
    assert claim_replay.status_code == 200 and claim_replay.json()["idempotent_replay"] is True
    assert len(claim_replay.json()["conversation"]["history"]) == claim_history
    collision = _admin_action(admin, conversation["conversation_id"], "claim", {"reason": "重复接单"}, "desk-admin-claim-operation-0002")
    assert collision.status_code == 409
    assign_payload = {"assignee": "desk-admin-2", "reason": "转交给值班管理员"}
    assign_key = "desk-admin-assign-operation-0001"
    known_users = auth.load_users()
    monkeypatch.setattr(auth, "load_users", lambda: [item for item in known_users if item.get("username") != "desk-admin-2"])
    invalid_target = _admin_action(admin, conversation["conversation_id"], "assign", assign_payload, assign_key)
    assert invalid_target.status_code == 422
    monkeypatch.setattr(auth, "load_users", lambda: known_users)
    assigned = _admin_action(admin, conversation["conversation_id"], "assign", assign_payload, assign_key)
    assert assigned.status_code == 200 and assigned.json()["conversation"]["assigned_to"] == "desk-admin-2"
    assert assigned.json()["idempotent_replay"] is False
    monkeypatch.setattr(auth, "load_users", lambda: [item for item in known_users if item.get("username") != "desk-admin-2"])
    assign_replay = _admin_action(admin, conversation["conversation_id"], "assign", assign_payload, assign_key)
    assert assign_replay.status_code == 200 and assign_replay.json()["idempotent_replay"] is True
    assign_conflict = _admin_action(admin, conversation["conversation_id"], "assign", assign_payload | {"reason": "异文"}, assign_key)
    assert assign_conflict.status_code == 409
    archive_payload = {"reason": "演示咨询处理完毕"}
    archive_key = "desk-admin-archive-operation-0001"
    archived = _admin_action(admin, conversation["conversation_id"], "archive", archive_payload, archive_key)
    assert archived.status_code == 200 and archived.json()["conversation"]["status"] == "archived"
    assert archived.json()["conversation"]["history"][-1]["reason"] == "演示咨询处理完毕"
    history_count = len(archived.json()["conversation"]["history"])
    archive_replay = _admin_action(admin, conversation["conversation_id"], "archive", archive_payload, archive_key)
    assert archive_replay.status_code == 200 and archive_replay.json()["idempotent_replay"] is True
    assert len(archive_replay.json()["conversation"]["history"]) == history_count
    old_assign_replay = _admin_action(admin, conversation["conversation_id"], "assign", assign_payload, assign_key)
    assert old_assign_replay.status_code == 200 and old_assign_replay.json()["idempotent_replay"] is True
    assert old_assign_replay.json()["conversation"]["status"] == "archived"
    assert "admin_operations" not in old_assign_replay.json()["conversation"]
    owner_headers = {"X-Demo-Access-Token": created["access_token"]}
    archived_message = client.post(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}/messages?registration_id={created['registration']['registration_id']}",
        headers=owner_headers | {"Idempotency-Key": "mini-archived-message-operation-001"}, json={"text": "归档后不能发"},
    )
    assert archived_message.status_code == 409


def test_admin_queue_action_cap_replay_and_corrupt_metadata_fail_closed(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    conversation = _create(client, created).json()["conversation"]
    path = tmp_path / "conversations.json"
    action_key = "desk-admin-cap-claim-operation-001"
    first, replayed = customer_service_desk.claim(
        conversation["conversation_id"], "desk-admin", "接手", action_key, with_replay=True,
    )
    assert not replayed
    document = json.loads(path.read_text(encoding="utf-8"))
    operations = document["conversations"][0]["admin_operations"]
    operations.extend({"fingerprint": f"{index:064x}", "payload_hash": "a" * 64} for index in range(99))
    path.write_text(json.dumps(document), encoding="utf-8")
    current, replayed = customer_service_desk.claim(
        conversation["conversation_id"], "desk-admin", "接手", action_key, with_replay=True,
    )
    assert replayed and current["history"] == first["history"]
    with pytest.raises(customer_service_desk.DeskConflict, match="已达本地上限"):
        customer_service_desk.assign(
            conversation["conversation_id"], "desk-admin", "desk-admin-2", "转派",
            "desk-admin-cap-assign-operation-001",
        )

    document["conversations"][0]["admin_operations"] = [{"fingerprint": "bad", "payload_hash": "a" * 64}]
    original = json.dumps(document)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(customer_service_desk.DeskError, match="幂等记录格式错误"):
        customer_service_desk.archive(
            conversation["conversation_id"], "desk-admin", "归档", "desk-admin-corrupt-archive-001",
        )
    assert path.read_text(encoding="utf-8") == original


def test_employee_reply_is_assignee_scoped_customer_readable_and_not_external(monkeypatch, tmp_path):
    created, customer = _setup(monkeypatch, tmp_path)
    conversation = _create(customer, created).json()["conversation"]
    admin = _admin(monkeypatch, tmp_path)
    admin_url = f"/api/v1/customer-service/demo/admin/conversations/{conversation['conversation_id']}"
    unassigned = admin.post(f"{admin_url}/reply", headers={"Idempotency-Key": "web-unassigned-reply-operation-001"}, json={"text": "尚未接单不可回复"})
    assert unassigned.status_code == 409

    assert _admin_action(admin, conversation["conversation_id"], "claim", {"reason": "desk-admin 内部处置理由"}, "desk-admin-claim-operation-0003").status_code == 200
    auth.create_user("desk-admin-2", "admin-password-2", role="admin")
    other_staff = TestClient(main.app)
    assert other_staff.post("/api/v1/auth/login", json={"username": "desk-admin-2", "password": "admin-password-2"}).status_code == 200
    assert other_staff.post(f"{admin_url}/reply", headers={"Idempotency-Key": "web-unassigned-reply-operation-002"}, json={"text": "非当前负责人不能回复"}).status_code == 409
    reply_headers = {"Idempotency-Key": "web-test-agent-reply-operation-0001"}
    reply = admin.post(f"{admin_url}/reply", headers=reply_headers, json={"text": "您好，已收到您的咨询。"})
    assert reply.status_code == 200
    assert reply.json()["idempotent_replay"] is False
    assert reply.json()["external_delivery"] is False
    record = reply.json()["conversation"]
    assert record["messages"][-1]["sender"] == "agent"
    assert record["messages"][-1]["text"] == "您好，已收到您的咨询。"
    assert record["messages"][-1]["actor"] == "desk-admin"
    assert record["unread_count"] == 0
    replay = admin.post(f"{admin_url}/reply", headers=reply_headers, json={"text": "您好，已收到您的咨询。"})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert len(replay.json()["conversation"]["messages"]) == 2
    assert admin.post(f"{admin_url}/reply", headers=reply_headers, json={"text": "改用不同内容"}).status_code == 409

    registration_id = created["registration"]["registration_id"]
    customer_record = customer.get(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}?registration_id={registration_id}",
        headers={"X-Demo-Access-Token": created["access_token"]},
    ).json()["conversation"]
    assert customer_record["messages"][-1]["sender"] == "agent"
    assert customer_record["messages"][-1]["text"] == "您好，已收到您的咨询。"
    assert "actor" not in customer_record["messages"][-1]
    assert "desk-admin" not in str(customer_record)
    assert "内部处置理由" not in str(customer_record)
    assert "unread_count" not in customer_record

    assert customer.post(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}/messages?registration_id={registration_id}",
        headers={"X-Demo-Access-Token": created["access_token"], "Idempotency-Key": "mini-after-reply-customer-msg-001"},
        json={"text": "我再补充一条"},
    ).status_code == 200
    queue = admin.get("/api/v1/customer-service/demo/admin/conversations").json()["conversations"]
    assert queue[0]["unread_count"] == 1
    archived = _admin_action(admin, conversation["conversation_id"], "archive", {"reason": "结束演示"}, "desk-admin-archive-operation-0002")
    assert archived.status_code == 200
    assert admin.post(f"{admin_url}/reply", headers={"Idempotency-Key": "web-unassigned-reply-operation-003"}, json={"text": "归档后回复"}).status_code == 409


def test_employee_unread_count_is_per_user_and_read_snapshot_preserves_new_messages(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    conversation = _create(client, created).json()["conversation"]
    owner_headers = {"X-Demo-Access-Token": created["access_token"]}
    messages_url = (
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}/messages"
        f"?registration_id={created['registration']['registration_id']}"
    )
    assert client.post(messages_url, headers=owner_headers | {"Idempotency-Key": "mini-unread-second-operation-001"}, json={"text": "第二条消息"}).status_code == 200

    # Employee unread metadata is never exposed on customer-owned API responses.
    customer_list = client.get(
        f"/api/v1/customer-service/demo/conversations?registration_id={created['registration']['registration_id']}",
        headers=owner_headers,
    ).json()["conversations"]
    assert "unread_count" not in customer_list[0]
    assert "employee_read_message_counts" not in str(customer_list)
    assert "marked_read" not in str(customer_list[0]["history"])

    admin = _admin(monkeypatch, tmp_path)
    second_admin = auth.create_user("desk-admin-2", "admin-password-2", role="admin")
    assert second_admin["username"] == "desk-admin-2"
    other = TestClient(main.app)
    assert other.post("/api/v1/auth/login", json={"username": "desk-admin-2", "password": "admin-password-2"}).status_code == 200
    sales = TestClient(main.app)
    assert sales.post("/api/v1/auth/login", json={"username": "desk-sales", "password": "sales-password"}).status_code == 200

    queue_url = "/api/v1/customer-service/demo/admin/conversations"
    first_queue = admin.get(queue_url).json()["conversations"]
    assert first_queue[0]["message_count"] == 2
    assert first_queue[0]["unread_count"] == 2
    assert other.get(queue_url).json()["conversations"][0]["unread_count"] == 2
    assert sales.post(
        f"{queue_url}/{conversation['conversation_id']}/read", json={"through_message_count": 2}
    ).status_code == 403

    read_url = f"{queue_url}/{conversation['conversation_id']}/read"
    assert admin.post(read_url, json={"through_message_count": True}).status_code == 400
    assert admin.post(read_url, json={"through_message_count": 3}).status_code == 409
    partial_read = admin.post(read_url, json={"through_message_count": 1})
    assert partial_read.status_code == 200
    assert partial_read.json()["conversation"]["unread_count"] == 1

    assert client.post(messages_url, headers=owner_headers | {"Idempotency-Key": "mini-unread-third-operation-001"}, json={"text": "新消息在读取之后到达"}).status_code == 200
    stale_snapshot = admin.post(read_url, json={"through_message_count": 2})
    assert stale_snapshot.status_code == 200
    assert stale_snapshot.json()["conversation"]["unread_count"] == 1
    assert admin.get(queue_url).json()["conversations"][0]["unread_count"] == 1
    assert other.get(queue_url).json()["conversations"][0]["unread_count"] == 3

    updated_at_before_read = admin.get(queue_url).json()["conversations"][0]["updated_at"]
    fully_read = admin.post(read_url, json={"through_message_count": 3})
    assert fully_read.status_code == 200
    assert fully_read.json()["conversation"]["unread_count"] == 0
    assert fully_read.json()["conversation"]["updated_at"] == updated_at_before_read
    customer_after_read = client.get(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}?registration_id={created['registration']['registration_id']}",
        headers=owner_headers,
    ).json()["conversation"]
    assert "unread_count" not in customer_after_read
    assert "marked_read" not in str(customer_after_read["history"])


def test_admin_conversation_queue_shows_vip_keyword_candidate_only_to_staff(monkeypatch, tmp_path):
    created, client = _setup(monkeypatch, tmp_path)
    conversation = _create(client, created).json()["conversation"]
    config_path = tmp_path / "admin-config.json"
    monkeypatch.setattr(admin_config, "CONFIG_PATH", config_path)
    config = admin_config.default_config()
    config["revision"] = 31
    config["business"]["whitelist"]["keywords"] = ["演示会话"]
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    admin_config.reset_cache()

    admin = _admin(monkeypatch, tmp_path)
    queue = admin.get("/api/v1/customer-service/demo/admin/conversations").json()["conversations"]
    priority = next(row for row in queue if row["conversation_id"] == conversation["conversation_id"])["priority_preview"]
    assert priority == {
        "status": "candidate", "priority_candidate": True,
        "whitelist_match": False, "whitelist_keyword_match": True,
        "requires_human_review": True, "config_revision": 31,
    }

    owner_headers = {"X-Demo-Access-Token": created["access_token"]}
    customer_record = client.get(
        f"/api/v1/customer-service/demo/conversations/{conversation['conversation_id']}"
        f"?registration_id={created['registration']['registration_id']}",
        headers=owner_headers,
    ).json()["conversation"]
    customer_list = client.get(
        f"/api/v1/customer-service/demo/conversations?registration_id={created['registration']['registration_id']}",
        headers=owner_headers,
    ).json()["conversations"]
    assert "priority_preview" not in customer_record
    assert "priority_preview" not in str(customer_list)

    original_list_admin = customer_registration.list_admin
    monkeypatch.setattr(customer_registration, "list_admin", lambda status="": {
        "registrations": [original_list_admin()["registrations"][0] | {"company_name": "已变更名称集团"}]
    })
    stale_name = admin.get("/api/v1/customer-service/demo/admin/conversations").json()["conversations"]
    stale_preview = next(row for row in stale_name if row["conversation_id"] == conversation["conversation_id"])["priority_preview"]
    assert stale_preview["status"] == "unavailable" and stale_preview["priority_candidate"] is False

    monkeypatch.setattr(customer_registration, "list_admin", lambda status="": {"registrations": []})
    missing_registration = admin.get("/api/v1/customer-service/demo/admin/conversations").json()["conversations"]
    missing_preview = next(row for row in missing_registration if row["conversation_id"] == conversation["conversation_id"])["priority_preview"]
    assert missing_preview["status"] == "unavailable" and missing_preview["priority_candidate"] is False

    monkeypatch.setattr(customer_registration, "list_admin", original_list_admin)
    monkeypatch.setattr(admin_config, "describe", lambda: {"warning": "bad config", "config": {}})
    unavailable = admin.get("/api/v1/customer-service/demo/admin/conversations").json()["conversations"]
    fallback = next(row for row in unavailable if row["conversation_id"] == conversation["conversation_id"])["priority_preview"]
    assert fallback["status"] == "unavailable" and fallback["priority_candidate"] is False


def test_conversation_storage_corruption_and_write_failure_fail_closed(monkeypatch, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_PATH", str(path))
    with pytest.raises(customer_service_desk.DeskError):
        customer_service_desk.list_admin()
    created, _ = _setup(monkeypatch, tmp_path / "write-test")
    blocker = tmp_path / "occupied"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("P0_CUSTOMER_SERVICE_DESK_PATH", str(blocker / "conversations.json"))
    with pytest.raises(OSError):
        customer_service_desk.create(
            created["registration"]["registration_id"], created["access_token"], "消息不会写入", "test-operation-0001"
        )
