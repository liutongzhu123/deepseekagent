"""Explicitly opt-in, local-only customer registration demonstration store.

This module does not verify identity or call CRM, WeChat, Qichacha, or email.
It stores only the demonstration registration fields and an append-only state
history in one atomically replaced, owner-readable JSON file.

Failure paths (F1-F9): disabled/production access is rejected; invalid fields do
not write; wrong owner capability cannot read/update; illegal state transitions
are rejected; non-admin review is rejected by the API layer; corrupt storage
fails closed; client network/configuration errors never become local success.
Initial creation and owner updates use client-held operation keys and payloads;
retries replay the existing state, while reuse with changed content conflicts.
File-backed reads and mutations use a POSIX sidecar lock for a consistent view
across local workers; this store is not safe for multi-host writes.
Coverage: test_customer_registration.py and miniprogram/tests/registration-api.test.js.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RegistrationError(ValueError):
    """Invalid input, missing registration, or an invalid state transition."""


class RegistrationConflict(RegistrationError):
    """The registration changed after it was loaded for review."""


_LOCK = threading.RLock()
_FIELDS = ("company_name", "contact_name", "region", "notes")
_MAX_UPDATE_OPERATIONS = 100
_MAX_REVIEW_OPERATIONS = 100


@contextmanager
def _locked():
    """Serialize JSON read/modify/write operations across local workers."""
    with _LOCK:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        lock_path = path.with_name(path.name + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enabled() -> bool:
    return os.getenv("P0_CUSTOMER_DEMO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _path() -> Path:
    configured = os.getenv("P0_CUSTOMER_REGISTRATION_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = Path(os.getenv("P0_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return data_dir / "customer-service" / "demo_registrations.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {"version": 0, "registrations": [], "events": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"演示注册数据不可读：{exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("registrations", []), list) or not isinstance(value.get("events", []), list):
        raise RegistrationError("演示注册数据格式错误")
    value.setdefault("version", 0)
    value.setdefault("registrations", [])
    value.setdefault("events", [])
    return value


def _write(document: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _public(record: dict) -> dict:
    private = {"access_token_hash", "creation_operation_hash", "creation_payload_hash", "update_operations", "review_operations"}
    return {key: value for key, value in record.items() if key not in private}


def _review_operation_values(registration_id: str, reviewer: str, operation_id: str, payload: dict) -> tuple[str, str]:
    operation_id = str(operation_id or "").strip()
    if not 16 <= len(operation_id) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in operation_id):
        raise RegistrationError("登记审核操作编号无效")
    fingerprint = hashlib.sha256(
        f"registration-review\0{registration_id}\0{reviewer}\0{operation_id}".encode("utf-8")
    ).hexdigest()
    payload_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return fingerprint, payload_hash


def _review_operations(record: dict) -> list[dict]:
    operations = record.get("review_operations", [])
    if not isinstance(operations, list) or any(
        not isinstance(item, dict)
        or any(
            not isinstance(item.get(key), str) or len(item[key]) != 64
            or any(char not in "0123456789abcdef" for char in item[key])
            for key in ("fingerprint", "payload_hash")
        )
        for item in operations
    ):
        raise RegistrationError("登记审核幂等记录格式错误")
    return operations


def review_replay(registration_id: str, reviewer: str, operation_id: str, payload: dict) -> dict | None:
    fingerprint, payload_hash = _review_operation_values(registration_id, reviewer, operation_id, payload)
    with _locked():
        record = _find(_read(), registration_id)
        previous = next((item for item in _review_operations(record) if item["fingerprint"] == fingerprint), None)
        if not previous:
            return None
        if previous["payload_hash"] != payload_hash:
            raise RegistrationConflict("登记审核操作编号已用于不同内容")
        return _public(record)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _registration_access_token(registration_id: str, operation_key: str) -> str:
    from . import auth

    message = f"customer-registration\0{registration_id}\0{operation_key}".encode("utf-8")
    digest = hmac.new(auth.state_secret(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _update_operation_fingerprint(registration_id: str, access_token: str, operation_id: str) -> str:
    scope = hashlib.sha256((registration_id + "\0" + access_token).encode("utf-8")).hexdigest()
    return hashlib.sha256((scope + "\0" + operation_id).encode("ascii")).hexdigest()


def create(payload: dict, operation_key: str | None = None) -> dict:
    """Create a draft and return a one-time capability token for its owner."""
    data = _clean_fields(payload)
    idempotent = operation_key is not None
    if operation_key is None:
        operation_key = secrets.token_urlsafe(32)
    operation_key = str(operation_key).strip()
    if not 16 <= len(operation_key) <= 200 or any(ord(char) < 33 or ord(char) > 126 for char in operation_key):
        raise RegistrationError("登记操作编号无效，请重试")
    operation_hash = hashlib.sha256(operation_key.encode("ascii")).hexdigest()
    payload_hash = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    with _locked():
        document = _read()
        previous = next((row for row in document["registrations"]
                         if idempotent and row.get("creation_operation_hash") == operation_hash), None)
        if previous:
            if previous.get("creation_payload_hash") != payload_hash:
                raise RegistrationConflict("登记操作编号已用于不同内容")
            return {
                "demo": True, "registration": _public(previous),
                "access_token": _registration_access_token(previous["registration_id"], operation_key),
                "idempotent_replay": True,
            }
        now = _now()
        registration_id = "REG-DEMO-" + secrets.token_hex(8)
        token = (_registration_access_token(registration_id, operation_key)
                 if idempotent else secrets.token_urlsafe(32))
        record = {
            "registration_id": registration_id,
            **data,
            "status": "draft",
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "access_token_hash": _token_hash(token),
            **({"creation_operation_hash": operation_hash, "creation_payload_hash": payload_hash} if idempotent else {}),
            "history": [{"at": now, "actor": "customer-demo", "from": "", "to": "draft", "reason": "本地演示登记"}],
        }
        document["registrations"].append(record)
        document["version"] = int(document["version"]) + 1
        document["events"].append({"at": now, "actor": "customer-demo", "action": "registration_created", "registration_id": record["registration_id"], "version": document["version"]})
        _write(document)
    return {"demo": True, "registration": _public(record), "access_token": token, "idempotent_replay": False}


def _clean_fields(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RegistrationError("注册信息必须是对象")
    data = {}
    for key in _FIELDS:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise RegistrationError("注册字段必须为文本")
        data[key] = value.strip()
    for key, label, maximum in (
        ("company_name", "企业名称", 120), ("contact_name", "联系人", 60), ("region", "所在地区", 100),
    ):
        if not data[key]:
            raise RegistrationError(f"{label}不能为空")
        if len(data[key]) > maximum:
            raise RegistrationError(f"{label}长度超限")
    if len(data["notes"]) > 1000:
        raise RegistrationError("补充说明不能超过 1000 字")
    return data


def update_owned(registration_id: str, access_token: str, payload: dict, operation_id: str) -> tuple[dict, bool]:
    data = _clean_fields(payload)
    operation_id = str(operation_id or "").strip()
    if not 16 <= len(operation_id) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in operation_id):
        raise RegistrationError("登记更新操作编号无效，请重试")
    operation_fingerprint = _update_operation_fingerprint(registration_id, access_token, operation_id)
    payload_hash = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    with _locked():
        document = _read()
        record = _find(document, registration_id)
        if not secrets.compare_digest(record.get("access_token_hash", ""), _token_hash(access_token)):
            raise RegistrationError("登记凭证无效")
        operations = record.get("update_operations", [])
        if not isinstance(operations, list) or any(
            not isinstance(item, dict)
            or not all(
                isinstance(item.get(key), str)
                and len(item[key]) == 64
                and all(char in "0123456789abcdef" for char in item[key])
                for key in ("fingerprint", "payload_hash")
            )
            for item in operations
        ):
            raise RegistrationError("登记更新幂等记录格式错误")
        previous_operation = next(
            (item for item in operations if item["fingerprint"] == operation_fingerprint), None
        )
        if previous_operation:
            if previous_operation["payload_hash"] != payload_hash:
                raise RegistrationConflict("登记更新操作编号已用于不同内容")
            return {"demo": True, "registration": _public(record)}, True
        if len(operations) >= _MAX_UPDATE_OPERATIONS:
            raise RegistrationConflict("登记更新次数已达演示上限，请联系管理员处理")
        if record["status"] not in {"draft", "needs_info"}:
            raise RegistrationError("当前状态不能修改")
        previous = record["status"]
        now = _now()
        record.update(data, status="draft", updated_at=now, revision=int(record.get("revision", 1)) + 1)
        record["history"].append({"at": now, "actor": "customer-demo", "from": previous, "to": "draft", "reason": "更新本地演示资料"})
        operations.append({"fingerprint": operation_fingerprint, "payload_hash": payload_hash})
        record["update_operations"] = operations
        document["version"] = int(document["version"]) + 1
        document["events"].append({"at": now, "actor": "customer-demo", "action": "registration_updated", "registration_id": registration_id, "version": document["version"]})
        _write(document)
        return {"demo": True, "registration": _public(record)}, False


def _find(document: dict, registration_id: str) -> dict:
    rows = [row for row in document["registrations"] if row.get("registration_id") == registration_id]
    if len(rows) != 1:
        raise RegistrationError("演示注册不存在")
    return rows[0]


def submit(registration_id: str, access_token: str) -> dict:
    with _locked():
        document = _read()
        record = _find(document, registration_id)
        if not secrets.compare_digest(record.get("access_token_hash", ""), _token_hash(access_token)):
            raise RegistrationError("登记凭证无效")
        if record["status"] not in {"draft", "needs_info"}:
            raise RegistrationError("当前状态不能提交")
        previous = record["status"]
        now = _now()
        record.update(status="submitted", updated_at=now, revision=int(record.get("revision", 1)) + 1)
        record["history"].append({"at": now, "actor": "customer-demo", "from": previous, "to": "submitted", "reason": "客户提交本地演示资料"})
        document["version"] = int(document["version"]) + 1
        document["events"].append({"at": now, "actor": "customer-demo", "action": "registration_submitted", "registration_id": registration_id, "version": document["version"]})
        _write(document)
        return {"demo": True, "registration": _public(record)}


def get_owned(registration_id: str, access_token: str) -> dict:
    with _locked():
        document = _read()
        record = _find(document, registration_id)
        if not secrets.compare_digest(record.get("access_token_hash", ""), _token_hash(access_token)):
            raise RegistrationError("登记凭证无效")
        return {"demo": True, "registration": _public(record)}


def list_admin(status: str = "") -> dict:
    with _locked():
        document = _read()
        rows = document["registrations"]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return {"demo": True, "version": document["version"], "registrations": [_public(row) for row in rows]}


def review(
    registration_id: str,
    decision: str,
    reason: str,
    reviewer: str,
    rule_review: dict | None = None,
    expected_revision: int | None = None,
    expected_company_name: str | None = None,
    operation_id: str | None = None,
    operation_payload: dict | None = None,
    with_replay: bool = False,
):
    transitions = {"needs_info", "approved", "rejected"}
    if decision not in transitions:
        raise RegistrationError("审核结果必须是 needs_info、approved 或 rejected")
    reason = str(reason or "").strip()
    if not reason:
        raise RegistrationError("审核原因不能为空")
    if len(reason) > 500:
        raise RegistrationError("审核原因不能超过 500 字")
    operation_fingerprint = operation_hash = ""
    if operation_id is not None:
        operation_fingerprint, operation_hash = _review_operation_values(
            registration_id, reviewer, operation_id, operation_payload or {"decision": decision, "reason": reason},
        )
    with _locked():
        document = _read()
        record = _find(document, registration_id)
        operations = _review_operations(record)
        if operation_fingerprint:
            prior = next((item for item in operations if item["fingerprint"] == operation_fingerprint), None)
            if prior:
                if prior["payload_hash"] != operation_hash:
                    raise RegistrationConflict("登记审核操作编号已用于不同内容")
                result = _public(record)
                return (result, True) if with_replay else result
            if len(operations) >= _MAX_REVIEW_OPERATIONS:
                raise RegistrationConflict("登记审核操作记录已达本地上限")
        if record["status"] != "submitted":
            raise RegistrationError("当前状态不能审核")
        if expected_revision is not None and int(record.get("revision", 1)) != expected_revision:
            raise RegistrationConflict("登记资料已变化，请刷新审核队列")
        if expected_company_name is not None and record.get("company_name") != expected_company_name:
            raise RegistrationConflict("企业名称已变化，请刷新审核队列")
        previous = record["status"]
        now = _now()
        record.update(status=decision, updated_at=now, revision=int(record.get("revision", 1)) + 1)
        if operation_fingerprint:
            operations.append({"fingerprint": operation_fingerprint, "payload_hash": operation_hash})
            record["review_operations"] = operations
        record["history"].append({"at": now, "actor": reviewer, "from": previous, "to": decision, "reason": reason})
        document["version"] = int(document["version"]) + 1
        event = {
            "at": now, "actor": reviewer, "action": "registration_reviewed",
            "registration_id": registration_id, "from": previous, "to": decision,
            "version": document["version"],
        }
        if isinstance(rule_review, dict):
            # Persist only non-identifying rule facts in the internal event log;
            # customer-visible registration history never receives this context.
            allowed = (
                "status", "decision", "requires_human_review", "config_revision",
                "blacklist_match", "whitelist_match", "whitelist_keyword_match", "company_list_conflict",
                "intermediary_keyword_match", "intermediary_block_configured",
                "routing_status", "routing_candidate_manager_id", "routing_route_type",
                "routing_config_revision",
            )
            event["rule_review"] = {key: rule_review[key] for key in allowed if key in rule_review}
        document["events"].append(event)
        _write(document)
        result = _public(record)
        return (result, False) if with_replay else result


def audit_events() -> list[dict[str, Any]]:
    with _locked():
        return list(_read()["events"])
