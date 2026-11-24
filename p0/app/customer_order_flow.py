"""Opt-in local customer-order demonstration with registration capability auth.

This simulator never creates a real lab task, sends email, or writes CRM. It
freezes the demo template version at submission and stores an append-only state
history. Failures: disabled/production, invalid or unapproved registration,
missing fields, foreign capability, illegal transitions, non-admin review,
corrupt storage, and write failure must all fail closed. Admin review retries
are scoped to reviewer/order/operation key; same-key same-decision replays the
current record, changed content conflicts, and malformed operation metadata
blocks writes. Admin decisions also compare the queue revision inside the same
file lock so a stale review cannot approve customer changes made after the
reviewer loaded the order.
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


class OrderFlowError(ValueError):
    """Invalid order data, capability, or missing record."""


class OrderFlowConflict(OrderFlowError):
    """An order cannot transition from its current state."""


_LOCK = threading.RLock()
_TEMPLATE = {
    "template_id": "DEMO-COMMISSION-01",
    "version": "demo-2026-11-23-v1",
    "label": "本地演示委托模板",
    "demo_only": True,
}
_DECISIONS = {"needs_info", "approved", "rejected"}
_CUSTOMER_DECISIONS = {"accepted": "customer_confirmed", "declined": "customer_declined"}
_MAX_OPERATION_ID = 128
_MAX_RESUBMISSION_OPERATIONS = 100


@contextmanager
def _locked():
    """Serialize file-backed transactions across local workers."""
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
    configured = os.getenv("P0_CUSTOMER_ORDER_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = Path(os.getenv("P0_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return root / "customer-service" / "demo_orders.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {"version": 0, "orders": [], "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrderFlowError("演示委托数据不可读") from exc
    if not isinstance(data, dict) or not isinstance(data.get("orders"), list) or not isinstance(data.get("events"), list):
        raise OrderFlowError("演示委托数据格式错误")
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _public(record: dict) -> dict:
    return {
        key: value for key, value in record.items()
        if key not in {
            "access_token_hash", "submission_operation_fingerprint", "submission_payload_hash",
            "resubmission_operation_fingerprint", "resubmission_payload_hash", "resubmission_operations",
            "review_operations",
        }
    }


def _operation_fingerprint(registration_id: str, access_token: str, operation_id: str) -> str:
    scope = hashlib.sha256((registration_id + "\0" + access_token).encode("utf-8")).hexdigest()
    return hashlib.sha256((scope + "\0" + operation_id).encode("utf-8")).hexdigest()


def _validate_operation_id(operation_id: str) -> str:
    operation_id = str(operation_id or "").strip()
    if not 16 <= len(operation_id) <= _MAX_OPERATION_ID or any(ord(char) < 33 or ord(char) > 126 for char in operation_id):
        raise OrderFlowError("委托操作编号无效，请重试")
    return operation_id


def _event(data: dict, record: dict, actor: str, action: str, previous: str, reason: str) -> None:
    now = _now()
    record["updated_at"] = now
    record["revision"] = int(record.get("revision", 1)) + 1
    record.setdefault("history", []).append({
        "at": now, "actor": actor, "action": action, "from": previous,
        "to": record["status"], "reason": reason,
    })
    data["version"] = int(data.get("version", 0)) + 1
    data["events"].append({
        "at": now, "actor": actor, "action": action, "order_id": record["order_id"],
        "from": previous, "to": record["status"], "version": data["version"],
    })


def _validate(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise OrderFlowError("委托信息必须是对象")
    template_label = str(payload.get("template_id") or "").strip()
    allowed_templates = {"演示委托模板 A", "演示委托模板 B"}
    if template_label not in allowed_templates:
        raise OrderFlowError("请选择有效的本地演示委托模板")
    fields = {
        "project_name": ("检测项目", 300),
        "sample_name": ("样品名称", 300),
        "standard": ("检测标准", 300),
        "quantity": ("样品数量", 100),
        "notes": ("补充说明", 2000),
    }
    result = {}
    for field, (label, limit) in fields.items():
        value = str(payload.get(field) or "").strip()
        if field in {"project_name", "sample_name", "quantity"} and not value:
            raise OrderFlowError(f"{label}不能为空")
        if len(value) > limit:
            raise OrderFlowError(f"{label}长度超限")
        result[field] = value
    result["template_snapshot"] = {
        "template_id": "DEMO-COMMISSION-" + template_label[-1],
        "version": "demo-2026-11-23-v1",
        "label": template_label,
        "demo_only": True,
    }
    return result


def create(payload: dict, registration_id: str, access_token: str, operation_id: str) -> tuple[dict, bool]:
    fields = _validate(payload)
    operation_id = _validate_operation_id(operation_id)
    try:
        registration = customer_registration.get_owned(registration_id, access_token)["registration"]
    except customer_registration.RegistrationError as exc:
        raise OrderFlowError(str(exc)) from exc
    operation_fingerprint = _operation_fingerprint(registration_id, access_token, operation_id)
    payload_hash = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    now = _now()
    record = {
        "order_id": "ORDER-DEMO-" + secrets.token_hex(8),
        "registration_id": registration_id,
        "company_name": registration["company_name"],
        "contact_name": registration["contact_name"],
        **fields,
        "template_snapshot": fields.pop("template_snapshot"),
        "status": "submitted",
        "revision": 1,
        "review_reason": "",
        "created_at": now,
        "updated_at": now,
        "access_token_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
        "submission_operation_fingerprint": operation_fingerprint,
        "submission_payload_hash": payload_hash,
        "history": [{"at": now, "actor": "customer-demo", "action": "submitted", "from": "draft", "to": "submitted", "reason": "提交本地演示委托"}],
    }
    with _locked():
        data = _read()
        previous = next((row for row in data["orders"] if row.get("submission_operation_fingerprint") == operation_fingerprint), None)
        if previous:
            if previous.get("submission_payload_hash") != payload_hash:
                raise OrderFlowConflict("委托操作编号已用于不同内容")
            return _public(previous), True
        # Replay an already committed request even if its registration status
        # changed after the response was lost. New writes still require a fresh
        # approved-status check under the order transaction lock.
        try:
            registration = customer_registration.get_owned(registration_id, access_token)["registration"]
        except customer_registration.RegistrationError as exc:
            raise OrderFlowError(str(exc)) from exc
        if registration.get("status") != "approved":
            raise OrderFlowConflict("客户演示登记尚未批准，不能提交委托")
        data["orders"].append(record)
        data["version"] = int(data.get("version", 0)) + 1
        data["events"].append({"at": now, "actor": "customer-demo", "action": "submitted", "order_id": record["order_id"], "from": "draft", "to": "submitted", "version": data["version"]})
        _write(data)
    return _public(record), False


def list_owned(access_token: str) -> list[dict]:
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _locked():
        records = _read()["orders"]
        return [_public(row) for row in sorted(records, key=lambda row: row.get("updated_at", ""), reverse=True)
                if secrets.compare_digest(row.get("access_token_hash", ""), token_hash)]


def update_owned(order_id: str, payload: dict, registration_id: str, access_token: str, operation_id: str) -> tuple[dict, bool]:
    fields = _validate(payload)
    operation_id = _validate_operation_id(operation_id)
    operation_fingerprint = _operation_fingerprint(registration_id, access_token, operation_id)
    payload_hash = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _locked():
        data = _read()
        rows = [row for row in data["orders"] if row.get("order_id") == order_id]
        if len(rows) != 1 or not secrets.compare_digest(rows[0].get("access_token_hash", ""), token_hash):
            raise OrderFlowError("演示委托不存在")
        record = rows[0]
        if record.get("registration_id") != registration_id:
            raise OrderFlowError("演示委托不存在")
        operations = record.get("resubmission_operations", [])
        if not isinstance(operations, list):
            raise OrderFlowError("委托补件幂等记录格式错误")
        if any(
            not isinstance(item, dict)
            or not all(
                isinstance(item.get(key), str)
                and len(item[key]) == 64
                and all(char in "0123456789abcdef" for char in item[key])
                for key in ("fingerprint", "payload_hash")
            )
            for item in operations
        ):
            raise OrderFlowError("委托补件幂等记录格式错误")
        previous_operation = next(
            (item for item in operations if isinstance(item, dict) and item.get("fingerprint") == operation_fingerprint),
            None,
        )
        # Read records written by the earlier single-operation format safely.
        if previous_operation is None and record.get("resubmission_operation_fingerprint") == operation_fingerprint:
            previous_payload_hash = record.get("resubmission_payload_hash")
        else:
            previous_payload_hash = previous_operation.get("payload_hash") if previous_operation else None
        if previous_payload_hash is not None:
            if previous_payload_hash != payload_hash:
                raise OrderFlowConflict("补件操作编号已用于不同内容")
            return _public(record), True
        try:
            registration = customer_registration.get_owned(registration_id, access_token)["registration"]
        except customer_registration.RegistrationError as exc:
            raise OrderFlowError(str(exc)) from exc
        if registration.get("status") != "approved":
            raise OrderFlowConflict("客户演示登记尚未批准，不能补充委托")
        if record["status"] != "needs_info":
            raise OrderFlowConflict("当前状态不接受补充信息")
        if len(operations) >= _MAX_RESUBMISSION_OPERATIONS:
            raise OrderFlowConflict("委托补件次数已达演示上限，请联系管理员处理")
        previous = record["status"]
        snapshot = record["template_snapshot"]
        record.update({key: value for key, value in fields.items() if key != "template_snapshot"})
        record["template_snapshot"] = snapshot
        record.update(
            status="submitted", review_reason="",
        )
        operations.append({
            "fingerprint": operation_fingerprint, "payload_hash": payload_hash,
        })
        record["resubmission_operations"] = operations
        _event(data, record, "customer-demo", "resubmitted", previous, "客户补充委托信息并重新提交")
        _write(data)
        return _public(record), False


def get_owned(order_id: str, access_token: str) -> dict:
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _locked():
        rows = [row for row in _read()["orders"] if row.get("order_id") == order_id]
        if len(rows) != 1 or not secrets.compare_digest(rows[0].get("access_token_hash", ""), token_hash):
            raise OrderFlowError("演示委托不存在")
        return _public(rows[0])


def confirm_owned(order_id: str, decision: str, note: str, registration_id: str, access_token: str) -> tuple[dict, bool]:
    """Record the customer's explicit response to an approved demo order."""
    if decision not in _CUSTOMER_DECISIONS:
        raise OrderFlowError("客户确认结果无效")
    note = str(note or "").strip()
    if len(note) > 500:
        raise OrderFlowError("确认说明不能超过 500 字")
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    with _locked():
        data = _read()
        rows = [row for row in data["orders"] if row.get("order_id") == order_id]
        if len(rows) != 1 or not secrets.compare_digest(rows[0].get("access_token_hash", ""), token_hash):
            raise OrderFlowError("演示委托不存在")
        record = rows[0]
        if record.get("registration_id") != registration_id:
            raise OrderFlowError("演示委托不存在")
        status = _CUSTOMER_DECISIONS[decision]
        if record.get("status") == status:
            return _public(record), True
        try:
            registration = customer_registration.get_owned(registration_id, access_token)["registration"]
        except customer_registration.RegistrationError as exc:
            raise OrderFlowError("登记凭证无效") from exc
        if registration.get("status") != "approved":
            raise OrderFlowConflict("客户演示登记尚未批准，不能确认委托")
        if record.get("status") != "approved":
            raise OrderFlowConflict("只有已批准的演示委托可以确认，且不能重复确认")
        previous = record["status"]
        record.update(status=status, customer_confirmation_note=note)
        _event(data, record, "customer-demo", decision, previous, note or "客户完成本地演示确认")
        _write(data)
        return _public(record), False


def list_admin(status: str = "") -> list[dict]:
    with _locked():
        rows = _read()["orders"]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return [_public(row) for row in sorted(rows, key=lambda row: row.get("updated_at", ""), reverse=True)]


def review(
    order_id: str, decision: str, reason: str, reviewer: str,
    operation_id: str | None = None, *, expected_revision: int | None = None, with_replay: bool = False,
):
    if decision not in _DECISIONS:
        raise OrderFlowError("审核结果无效")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 500:
        raise OrderFlowError("审核原因不能为空且不能超过 500 字")
    operation_fingerprint = payload_hash = ""
    if operation_id is not None:
        operation_id = _validate_operation_id(operation_id)
        operation_fingerprint = hashlib.sha256(
            f"order-review\0{order_id}\0{reviewer}\0{operation_id}".encode("utf-8")
        ).hexdigest()
        payload_hash = hashlib.sha256(json.dumps(
            {"decision": decision, "reason": reason, "expected_revision": expected_revision}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
    with _locked():
        data = _read()
        rows = [row for row in data["orders"] if row.get("order_id") == order_id]
        if len(rows) != 1:
            raise OrderFlowError("演示委托不存在")
        record = rows[0]
        operations = record.get("review_operations", [])
        if not isinstance(operations, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("fingerprint"), str) or len(item["fingerprint"]) != 64
            or any(char not in "0123456789abcdef" for char in item["fingerprint"])
            or not isinstance(item.get("payload_hash"), str) or len(item["payload_hash"]) != 64
            or any(char not in "0123456789abcdef" for char in item["payload_hash"])
            for item in operations
        ):
            raise OrderFlowError("演示委托审核幂等记录格式错误")
        if operation_fingerprint:
            previous_operation = next(
                (item for item in operations if item["fingerprint"] == operation_fingerprint), None
            )
            if previous_operation:
                if previous_operation["payload_hash"] != payload_hash:
                    raise OrderFlowConflict("委托审核操作编号已用于不同内容")
                result = _public(record)
                return (result, True) if with_replay else result
            if len(operations) >= 100:
                raise OrderFlowConflict("委托审核操作记录已达本地上限，请联系维护人员")
        if expected_revision is not None and int(record.get("revision", 1)) != expected_revision:
            raise OrderFlowConflict("委托资料已变化，请刷新审核队列后再审核")
        if record["status"] not in {"submitted", "needs_info"}:
            raise OrderFlowConflict("当前状态不能审核")
        previous = record["status"]
        record.update(status=decision, review_reason=reason)
        if operation_fingerprint:
            operations.append({"fingerprint": operation_fingerprint, "payload_hash": payload_hash})
            record["review_operations"] = operations
        _event(data, record, reviewer, "reviewed", previous, reason)
        _write(data)
        result = _public(record)
        return (result, False) if with_replay else result


def template_snapshot() -> dict:
    return deepcopy(_TEMPLATE)
