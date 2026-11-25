"""Local customer-service conversation demo, isolated by registration capability.

No message is sent to WeChat/WeCom or a human notification channel. The
employee queue is admin-only until business-manager roles are confirmed.
Failure paths: F1 demo disabled/production; F2 bad registration capability;
F3 invalid/oversize messages; F4 foreign conversation access; F5 archived
conversation mutation; F6 assignment collision or invalid target; F7 missing
operation reason; F8 corrupt/write-failed storage; F9 non-admin queue access;
F10 unread counts are per employee and read receipts only advance through the
message snapshot that the employee actually opened; F11 malformed/missing
client operation key; F12 same key with a different body or owner; F13 retry
after a committed write returns the original conversation without duplicating
the message; F14 corrupt lock/storage data fails closed. Cross-process locks
protect only a shared local POSIX filesystem; this JSON demo is not a
multi-host production store; F15 unassigned/foreign staff cannot reply and
archived conversations reject replies; F16 customer responses never disclose
the employee account that authored a reply; F17 manager claim/assign/archive
actions require stable operation keys, replay matching retries, conflict on
changed payloads, and fail closed on corrupt or over-limit metadata; F18 assign
target validation runs under the write lock after replay detection; F19 the
browser retains uncertain actions and exposes them across queue filters.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import customer_registration


class DeskError(ValueError):
    """Invalid message, missing conversation, or capability error."""


class DeskConflict(DeskError):
    """Illegal or stale conversation state transition."""


_LOCK = threading.RLock()
_MAX_MESSAGES = 200
_MAX_ADMIN_OPERATIONS = 100


@contextmanager
def _locked():
    """Serialize file-backed read/modify/write transactions across workers."""
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


def _path() -> Path:
    configured = os.getenv("P0_CUSTOMER_SERVICE_DESK_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = Path(os.getenv("P0_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return root / "customer-service" / "demo_conversations.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {"version": 0, "conversations": [], "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeskError("演示会话数据不可读") from exc
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), list) or not isinstance(data.get("events"), list):
        raise DeskError("演示会话数据格式错误")
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _public(record: dict, *, include_employee_state: bool = False, reader: str = "") -> dict:
    public = {
        key: value for key, value in record.items()
        if key not in {"access_token_hash", "employee_read_message_counts", "admin_operations"}
    }
    if not include_employee_state and isinstance(public.get("history"), list):
        public["history"] = [
            ({key: value for key, value in item.items()
              if key not in {"actor", "previous_assignee", "reason"}}
             if item.get("actor") not in {"", "customer-demo"} else item)
            for item in public["history"] if item.get("action") != "marked_read"
        ]
        public.pop("assigned_to", None)
        public.pop("archived_reason", None)
    if include_employee_state:
        message_count = len(record.get("messages", []))
        read_counts = record.get("employee_read_message_counts", {})
        read_count = read_counts.get(reader, 0) if isinstance(read_counts, dict) else 0
        if isinstance(read_count, bool) or not isinstance(read_count, int):
            read_count = 0
        read_count = min(max(0, read_count), message_count)
        public["message_count"] = message_count
        public["unread_count"] = sum(
            1 for message in record.get("messages", [])[read_count:]
            if message.get("sender") == "customer"
        )
    if isinstance(public.get("messages"), list):
        public["messages"] = [
            {key: value for key, value in item.items()
             if key not in {"client_operation_id", "client_operation_fingerprint", "client_payload_hash"}
             and (include_employee_state or key != "actor")}
            for item in public["messages"]
        ]
    return public


def _find(data: dict, conversation_id: str) -> dict:
    rows = [row for row in data["conversations"] if row.get("conversation_id") == conversation_id]
    if len(rows) != 1:
        raise DeskError("演示会话不存在")
    return rows[0]


def _event(data: dict, record: dict, actor: str, action: str, previous: str, reason: str = "") -> None:
    at = _now()
    record["updated_at"] = at
    record.setdefault("history", []).append({
        "at": at, "actor": actor, "action": action,
        "from": previous, "to": record["status"], "reason": reason,
    })
    data["version"] = int(data.get("version", 0)) + 1
    data["events"].append({
        "at": at, "actor": actor, "action": action,
        "conversation_id": record["conversation_id"], "from": previous,
        "to": record["status"], "version": data["version"],
    })


def _validate_message(text: str) -> str:
    message = str(text or "").strip()
    if not message:
        raise DeskError("消息不能为空")
    if len(message) > 2000:
        raise DeskError("消息不能超过 2000 字")
    return message


def _validate_operation_id(operation_id: str) -> str:
    operation_id = str(operation_id or "").strip()
    if not 16 <= len(operation_id) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in operation_id):
        raise DeskError("消息操作编号无效，请重试")
    return operation_id


def _admin_operation(conversation_id: str, actor: str, action: str, operation_id: str, payload: dict) -> tuple[str, str]:
    operation_id = _validate_operation_id(operation_id)
    fingerprint = hashlib.sha256(
        f"desk-admin-action-v1\0{conversation_id}\0{actor}\0{action}\0{operation_id}".encode("utf-8")
    ).hexdigest()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return fingerprint, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _admin_operations(record: dict) -> list[dict]:
    operations = record.get("admin_operations", [])
    if not isinstance(operations, list) or any(
        not isinstance(item, dict)
        or any(
            not isinstance(item.get(key), str) or len(item[key]) != 64
            or any(char not in "0123456789abcdef" for char in item[key])
            for key in ("fingerprint", "payload_hash")
        )
        for item in operations
    ):
        raise DeskError("客服队列操作幂等记录格式错误")
    fingerprints = [item["fingerprint"] for item in operations]
    if len(fingerprints) != len(set(fingerprints)):
        raise DeskError("客服队列操作幂等记录重复")
    return operations


def _admin_operation_start(record: dict, operation: tuple[str, str] | None) -> tuple[list[dict], bool]:
    operations = _admin_operations(record)
    if operation is None:
        return operations, False
    matches = [item for item in operations if item["fingerprint"] == operation[0]]
    if len(matches) > 1:
        raise DeskError("客服队列操作幂等记录重复，已拒绝写入")
    if matches:
        if matches[0]["payload_hash"] != operation[1]:
            raise DeskConflict("客服队列操作编号已用于不同内容")
        return operations, True
    if len(operations) >= _MAX_ADMIN_OPERATIONS:
        raise DeskConflict("客服队列操作记录已达本地上限，请联系维护人员")
    return operations, False


def _admin_operation_finish(record: dict, operations: list[dict], operation: tuple[str, str] | None) -> None:
    if operation:
        operations.append({"fingerprint": operation[0], "payload_hash": operation[1]})
        record["admin_operations"] = operations


def _operation_fingerprint(registration_id: str, access_token: str, operation_id: str) -> str:
    scope = hashlib.sha256((registration_id + "\0" + access_token).encode("utf-8")).hexdigest()
    return hashlib.sha256((scope + "\0" + operation_id).encode("utf-8")).hexdigest()


def _find_operation(data: dict, registration_id: str, access_token: str, operation_id: str) -> tuple[dict, dict] | None:
    fingerprint = _operation_fingerprint(registration_id, access_token, operation_id)
    for record in data["conversations"]:
        for item in record.get("messages", []):
            if item.get("client_operation_fingerprint") == fingerprint:
                return record, item
    return None


def _verify_registration(registration_id: str, access_token: str) -> dict:
    if not registration_id or not access_token:
        raise DeskError("请提供本地演示登记凭证")
    try:
        return customer_registration.get_owned(registration_id, access_token)["registration"]
    except customer_registration.RegistrationError as exc:
        raise DeskError("登记凭证无效") from exc


def create(registration_id: str, access_token: str, text: str, operation_id: str) -> tuple[dict, bool]:
    registration = _verify_registration(registration_id, access_token)
    message = _validate_message(text)
    operation_id = _validate_operation_id(operation_id)
    fingerprint = _operation_fingerprint(registration_id, access_token, operation_id)
    at = _now()
    record = {
        "conversation_id": "CONV-DEMO-" + secrets.token_hex(8),
        "registration_id": registration_id,
        "company_name": registration["company_name"],
        "status": "open", "assigned_to": "",
        "employee_read_message_counts": {},
        "access_token_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
        "messages": [{"at": at, "sender": "customer", "text": message,
                      "client_operation_id": operation_id,
                      "client_operation_fingerprint": fingerprint,
                      "client_payload_hash": hashlib.sha256(message.encode("utf-8")).hexdigest()}],
        "created_at": at, "updated_at": at,
        "history": [{"at": at, "actor": "customer-demo", "action": "created", "from": "", "to": "open", "reason": "本地演示消息已存档"}],
    }
    with _locked():
        data = _read()
        existing = _find_operation(data, registration_id, access_token, operation_id)
        if existing:
            existing_record, existing_message = existing
            payload_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
            if existing_message.get("client_payload_hash") != payload_hash:
                raise DeskConflict("消息操作编号已用于另一条消息")
            return _public(existing_record), True
        data["conversations"].append(record)
        data["version"] = int(data.get("version", 0)) + 1
        data["events"].append({"at": at, "actor": "customer-demo", "action": "created", "conversation_id": record["conversation_id"], "from": "", "to": "open", "version": data["version"]})
        _write(data)
    return _public(record), False


def _owned_record(data: dict, conversation_id: str, registration_id: str, access_token: str) -> dict:
    _verify_registration(registration_id, access_token)
    record = _find(data, conversation_id)
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    if record.get("registration_id") != registration_id or not secrets.compare_digest(record.get("access_token_hash", ""), token_hash):
        raise DeskError("演示会话不存在")
    return record


def add_message(conversation_id: str, registration_id: str, access_token: str, text: str, operation_id: str) -> tuple[dict, bool]:
    message = _validate_message(text)
    operation_id = _validate_operation_id(operation_id)
    fingerprint = _operation_fingerprint(registration_id, access_token, operation_id)
    payload_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    with _locked():
        data = _read()
        record = _owned_record(data, conversation_id, registration_id, access_token)
        existing = _find_operation(data, registration_id, access_token, operation_id)
        if existing:
            existing_record, existing_message = existing
            if existing_record["conversation_id"] != conversation_id or existing_message.get("client_payload_hash") != payload_hash:
                raise DeskConflict("消息操作编号已用于另一条消息")
            return _public(existing_record), True
        if record["status"] not in {"open", "assigned"}:
            raise DeskConflict("当前会话已归档，不能继续发送")
        if len(record["messages"]) >= _MAX_MESSAGES:
            raise DeskConflict("演示会话消息数已达上限，请新建会话")
        record["messages"].append({"at": _now(), "sender": "customer", "text": message,
                                   "client_operation_id": operation_id,
                                   "client_operation_fingerprint": fingerprint,
                                   "client_payload_hash": payload_hash})
        _event(data, record, "customer-demo", "message_added", record["status"], "客户发送本地演示消息")
        _write(data)
        return _public(record), False


def reply(conversation_id: str, actor: str, text: str, operation_id: str) -> tuple[dict, bool]:
    """Append an employee reply to an assigned, active demo conversation."""
    message = _validate_message(text)
    actor = str(actor or "").strip()
    if not actor:
        raise DeskError("客服账号无效")
    operation_id = _validate_operation_id(operation_id)
    fingerprint = _operation_fingerprint("employee:" + actor, "service-desk-reply-v1", operation_id)
    payload_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    with _locked():
        data = _read()
        record = _find(data, conversation_id)
        existing = _find_operation(data, "employee:" + actor, "service-desk-reply-v1", operation_id)
        if existing:
            existing_record, existing_message = existing
            if existing_record["conversation_id"] != conversation_id or existing_message.get("sender") != "agent" or existing_message.get("client_payload_hash") != payload_hash:
                raise DeskConflict("回复操作编号已用于其他内容")
            return _public(existing_record, include_employee_state=True, reader=actor), True
        if record["status"] == "archived":
            raise DeskConflict("已归档会话不能回复")
        if record["status"] != "assigned" or record.get("assigned_to") != actor:
            raise DeskConflict("请先由当前客服接单后再回复")
        if len(record.get("messages", [])) >= _MAX_MESSAGES:
            raise DeskConflict("演示会话消息数已达上限，请新建会话")
        record.setdefault("messages", []).append({
            "at": _now(), "sender": "agent", "text": message, "actor": actor,
            "client_operation_id": operation_id,
            "client_operation_fingerprint": fingerprint,
            "client_payload_hash": payload_hash,
        })
        read_counts = record.get("employee_read_message_counts", {})
        if not isinstance(read_counts, dict):
            read_counts = {}
        read_counts[actor] = len(record["messages"])
        record["employee_read_message_counts"] = read_counts
        _event(data, record, actor, "employee_replied", record["status"], "客服发送演示回复")
        _write(data)
        return _public(record, include_employee_state=True, reader=actor), False


def get_owned(conversation_id: str, registration_id: str, access_token: str) -> dict:
    with _locked():
        return _public(_owned_record(_read(), conversation_id, registration_id, access_token))


def list_owned(registration_id: str, access_token: str) -> list[dict]:
    _verify_registration(registration_id, access_token)
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _locked():
        rows = _read()["conversations"]
        owned = [row for row in rows if row.get("registration_id") == registration_id and secrets.compare_digest(row.get("access_token_hash", ""), token_hash)]
        return [_public(row) for row in sorted(owned, key=lambda row: row.get("updated_at", ""), reverse=True)]


def list_admin(status: str = "", reader: str = "") -> list[dict]:
    if status not in {"", "open", "assigned", "archived"}:
        raise DeskError("会话状态筛选值无效")
    with _locked():
        rows = _read()["conversations"]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return [
            _public(row, include_employee_state=True, reader=reader)
            for row in sorted(rows, key=lambda row: row.get("updated_at", ""), reverse=True)
        ]


def mark_read(conversation_id: str, actor: str, through_message_count: int) -> dict:
    if isinstance(through_message_count, bool) or not isinstance(through_message_count, int):
        raise DeskError("已读消息数量必须是整数")
    with _locked():
        data = _read()
        record = _find(data, conversation_id)
        message_count = len(record.get("messages", []))
        if through_message_count < 0 or through_message_count > message_count:
            raise DeskConflict("已读消息快照已失效，请刷新会话后再标记")
        counts = record.get("employee_read_message_counts", {})
        if not isinstance(counts, dict):
            counts = {}
        current_read_count = counts.get(actor, 0)
        if isinstance(current_read_count, bool) or not isinstance(current_read_count, int):
            current_read_count = 0
        next_read_count = min(message_count, max(0, current_read_count, through_message_count))
        if next_read_count != current_read_count:
            counts[actor] = next_read_count
            record["employee_read_message_counts"] = counts
            data["version"] = int(data.get("version", 0)) + 1
            data["events"].append({
                "at": _now(), "actor": actor, "action": "marked_read",
                "conversation_id": conversation_id,
                "message_count": next_read_count, "version": data["version"],
            })
            _write(data)
        return _public(record, include_employee_state=True, reader=actor)


def claim(conversation_id: str, actor: str, reason: str, operation_id: str | None = None, *, with_replay: bool = False):
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise DeskError("接单原因不能为空且不能超过 500 字")
    operation = _admin_operation(conversation_id, actor, "claim", operation_id, {"reason": reason}) if operation_id is not None else None
    with _locked():
        data = _read()
        record = _find(data, conversation_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record, include_employee_state=True, reader=actor)
            return (result, True) if with_replay else result
        if record["status"] != "open":
            raise DeskConflict("当前会话不能接单")
        previous = record["status"]
        record.update(status="assigned", assigned_to=actor)
        _event(data, record, actor, "claimed", previous, reason)
        _admin_operation_finish(record, operations, operation)
        _write(data)
        result = _public(record, include_employee_state=True, reader=actor)
        return (result, False) if with_replay else result


def assign(conversation_id: str, actor: str, assignee: str, reason: str, operation_id: str | None = None, *, with_replay: bool = False, validate_assignee=None):
    assignee, reason = str(assignee or "").strip(), str(reason or "").strip()
    if not assignee or not reason or len(reason) > 500:
        raise DeskError("转交对象和原因不能为空；原因不能超过 500 字")
    operation = _admin_operation(
        conversation_id, actor, "assign", operation_id, {"assignee": assignee, "reason": reason},
    ) if operation_id is not None else None
    with _locked():
        data = _read()
        record = _find(data, conversation_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record, include_employee_state=True, reader=actor)
            return (result, True) if with_replay else result
        if validate_assignee is not None:
            validate_assignee(record)
        if record["status"] not in {"open", "assigned"}:
            raise DeskConflict("当前会话不能转交")
        previous_assignee = record.get("assigned_to", "")
        previous = record["status"]
        record.update(status="assigned", assigned_to=assignee)
        _event(data, record, actor, "assigned", previous, reason)
        record["history"][-1]["previous_assignee"] = previous_assignee
        _admin_operation_finish(record, operations, operation)
        _write(data)
        result = _public(record, include_employee_state=True, reader=actor)
        return (result, False) if with_replay else result


def archive(conversation_id: str, actor: str, reason: str, operation_id: str | None = None, *, with_replay: bool = False):
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise DeskError("归档原因不能为空且不能超过 500 字")
    operation = _admin_operation(conversation_id, actor, "archive", operation_id, {"reason": reason}) if operation_id is not None else None
    with _locked():
        data = _read()
        record = _find(data, conversation_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record, include_employee_state=True, reader=actor)
            return (result, True) if with_replay else result
        if record["status"] == "archived":
            raise DeskConflict("会话已经归档")
        previous = record["status"]
        record.update(status="archived", archived_reason=reason)
        _event(data, record, actor, "archived", previous, reason)
        _admin_operation_finish(record, operations, operation)
        _write(data)
        result = _public(record, include_employee_state=True, reader=actor)
        return (result, False) if with_replay else result
