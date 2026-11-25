from __future__ import annotations

import multiprocessing
import errno
import fcntl
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app import admin_config, auth, business_rule_versions, main


def _create_draft_from_worker(start, results, config_path, payload, actor):
    admin_config.CONFIG_PATH = Path(config_path)
    admin_config.reset_cache()
    start.wait(timeout=30)
    draft = business_rule_versions.create_draft(payload, created_by=actor)
    results.put(draft["draft_id"])


def _hold_draft_lock(ready, entered, release, config_path, worker_id):
    admin_config.CONFIG_PATH = Path(config_path)
    admin_config.reset_cache()
    if worker_id != "holder":
        lock_path = Path(config_path).with_suffix(".business-drafts.json.lock")
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                ready.put((worker_id, "blocked"))
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                ready.put((worker_id, "not_blocked"))
        finally:
            os.close(descriptor)
    else:
        ready.put((worker_id, "holder_ready"))
    with business_rule_versions._locked():
        entered.put(worker_id)
        release.wait(timeout=30)


def _client(monkeypatch, tmp_path, username="root", role="admin"):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(admin_config, "CONFIG_PATH", tmp_path / "config" / "rules.json")
    admin_config.reset_cache()
    auth.create_user(username, "password-123", role=role)
    client = TestClient(main.app)
    response = client.post("/api/v1/auth/login", json={
        "username": username, "password": "password-123"
    })
    assert response.status_code == 200
    return client


def test_draft_is_versioned_comparable_and_never_changes_live_rules(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    before = client.get("/api/v1/admin/config").json()["config"]
    candidate = dict(before["business"])
    candidate["discount_tiers"] = [dict(row) for row in candidate["discount_tiers"]]
    candidate["discount_tiers"][0]["discount"] = 99

    created = client.post("/api/v1/admin/config/business/drafts", json=candidate)
    assert created.status_code == 201
    draft = created.json()["draft"]
    assert draft["status"] == "draft"
    assert draft["base_revision"] == before["revision"]
    assert draft["created_by"] == "root"
    assert "discount_tiers" in draft["changed_fields"]
    assert created.json()["published"] is False

    after = client.get("/api/v1/admin/config").json()["config"]
    assert after["revision"] == before["revision"]
    assert after["business"] == before["business"]

    listed = client.get("/api/v1/admin/config/business/drafts")
    assert listed.status_code == 200
    assert listed.json()["drafts"][0]["draft_id"] == draft["draft_id"]
    assert "business" not in listed.json()["drafts"][0]

    detail = client.get(f"/api/v1/admin/config/business/drafts/{draft['draft_id']}")
    assert detail.status_code == 200
    compare = client.get(f"/api/v1/admin/config/business/drafts/{draft['draft_id']}/compare")
    assert compare.status_code == 200
    assert compare.json()["stale"] is False
    assert compare.json()["published"] is False
    assert compare.json()["candidate"] == candidate

    replay_payload = {
        "project_name": "项目甲", "company_name": "样例公司", "subtotal": 10000,
        "rush_requested": True,
    }
    replay = client.post(
        f"/api/v1/admin/config/business/drafts/{draft['draft_id']}/replay",
        json=replay_payload,
    )
    assert replay.status_code == 200
    assert replay.json()["decision"] == "manual_review"
    assert replay.json()["published"] is False
    assert replay.json()["scope"] == "read_only_draft_replay"
    assert "blacklist_match" not in replay.json()["candidates"]
    assert "whitelist_match" not in replay.json()["candidates"]

    changed_live = {
        "rush": {
            **after["business"]["rush"],
            "fee_rate": after["business"]["rush"]["fee_rate"] + 0.01,
        }
    }
    assert client.put("/api/v1/admin/config/business", json=changed_live).status_code == 200
    stale = client.get(f"/api/v1/admin/config/business/drafts/{draft['draft_id']}/compare")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["live_revision"] == before["revision"] + 1
    stale_replay = client.post(
        f"/api/v1/admin/config/business/drafts/{draft['draft_id']}/replay",
        json=replay_payload,
    )
    assert stale_replay.status_code == 200
    assert stale_replay.json()["stale"] is True
    assert stale_replay.json()["published"] is False


def test_draft_authorization_validation_and_corrupt_store_fail_closed(monkeypatch, tmp_path):
    viewer = _client(monkeypatch, tmp_path, username="viewer", role="viewer")
    denied = viewer.get("/api/v1/admin/config/business/drafts")
    assert denied.status_code == 403
    assert viewer.post("/api/v1/admin/config/business/drafts", json={}).status_code == 403

    auth.create_user("root", "password-123", role="admin")
    admin = TestClient(main.app)
    assert admin.post("/api/v1/auth/login", json={
        "username": "root", "password": "password-123"
    }).status_code == 200
    business = admin_config.load()["business"]
    business["region_routing"] = {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "private-manager"}],
        "backup_manager_id": "private-backup",
    }
    business["expected_revision"] = admin_config.load()["revision"]
    assert admin.put("/api/v1/admin/config/business", json=business).status_code == 200
    admin_candidate = admin_config.load()["business"]
    admin_candidate["region_routing"]["region_managers"] = [
        {"region": "上海", "manager_id": "another-private-manager"}
    ]
    route_draft = admin.post(
        "/api/v1/admin/config/business/drafts", json=admin_candidate
    ).json()["draft"]
    assert route_draft["business"]["region_routing"]["region_managers"][0]["manager_id"] == "another-private-manager"

    auth.create_user("manager", "password-123", role="business_manager")
    manager = TestClient(main.app)
    assert manager.post("/api/v1/auth/login", json={
        "username": "manager", "password": "password-123"
    }).status_code == 200
    route_detail = manager.get(
        f"/api/v1/admin/config/business/drafts/{route_draft['draft_id']}"
    )
    assert route_detail.status_code == 403
    route_compare = manager.get(
        f"/api/v1/admin/config/business/drafts/{route_draft['draft_id']}/compare"
    )
    assert route_compare.status_code == 403
    assert manager.get("/api/v1/admin/config/business/drafts").status_code == 403
    forbidden = manager.post("/api/v1/admin/config/business/drafts", json={
        "region_routing": {
            "enabled": True,
            "region_managers": [{"region": "上海", "manager_id": "forged-manager"}],
            "backup_manager_id": "forged-backup",
        }
    })
    assert forbidden.status_code == 403
    forbidden_list = manager.post("/api/v1/admin/config/business/drafts", json={
        "blacklist": {"companies": ["forged-private-list"]},
    })
    assert forbidden_list.status_code == 403
    assert admin.get(
        f"/api/v1/admin/config/business/drafts/{route_draft['draft_id']}"
    ).status_code == 200

    draft_path = (tmp_path / "config" / "rules.business-drafts.json")
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text("not-json", encoding="utf-8")
    unavailable = admin.get("/api/v1/admin/config/business/drafts")
    assert unavailable.status_code == 503


def test_draft_lock_failure_returns_service_unavailable(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    def fail_lock(*args):
        raise OSError("lock unavailable")

    monkeypatch.setattr(business_rule_versions.fcntl, "flock", fail_lock)
    response = client.get("/api/v1/admin/config/business/drafts")
    assert response.status_code == 503
    assert response.json()["detail"] == "业务规则草稿库不可用"


def test_concurrent_rule_drafts_are_not_lost_across_processes(monkeypatch, tmp_path):
    config_path = tmp_path / "config" / "rules.json"
    monkeypatch.setattr(admin_config, "CONFIG_PATH", config_path)
    admin_config.reset_cache()
    base = admin_config.default_config()["business"]
    payloads = []
    for discount in range(91, 99):
        candidate = {**base, "discount_tiers": [dict(row) for row in base["discount_tiers"]]}
        candidate["discount_tiers"][0]["discount"] = discount
        payloads.append(candidate)

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_create_draft_from_worker, args=(start, results, str(config_path), payload, f"worker-{index}"))
        for index, payload in enumerate(payloads, start=1)
    ]
    started = []
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        start.set()
        draft_ids = [results.get(timeout=30) for _ in workers]
    finally:
        start.set()
        for worker in started:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            assert not worker.is_alive()
        results.close()
    assert all(worker.exitcode == 0 for worker in started)

    drafts = business_rule_versions.list_drafts()
    assert len(set(draft_ids)) == len(workers)
    assert {row["draft_id"] for row in drafts} == set(draft_ids)


def test_draft_lock_excludes_other_processes_deterministically(tmp_path):
    config_path = tmp_path / "config" / "rules.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    entered = context.Queue()
    release = context.Event()
    holder = context.Process(target=_hold_draft_lock, args=(ready, entered, release, str(config_path), "holder"))
    waiters = [
        context.Process(target=_hold_draft_lock, args=(ready, entered, release, str(config_path), f"waiter-{index}"))
        for index in range(2)
    ]
    holder.start()
    started = [holder]
    try:
        assert ready.get(timeout=30) == ("holder", "holder_ready")
        assert entered.get(timeout=30) == "holder"
        for worker in waiters:
            worker.start()
            started.append(worker)
        outcomes = {ready.get(timeout=30), ready.get(timeout=30)}
        assert outcomes == {("waiter-0", "blocked"), ("waiter-1", "blocked")}
        release.set()
        later_entries = {entered.get(timeout=30), entered.get(timeout=30)}
        assert later_entries == {"waiter-0", "waiter-1"}
    finally:
        release.set()
        for worker in started:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            assert not worker.is_alive()
        ready.close()
        entered.close()
    assert all(worker.exitcode == 0 for worker in started)
