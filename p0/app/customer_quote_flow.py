"""Local internal inquiry workflow around the read-only CRM fact preview.

Failure cases are recorded before the implementation is enabled:
F1 project authorization must happen before CRM reads or writes; F2 CRM outages
and mapping conflicts retain the inquiry and route it to a human without a
synthetic fallback; F3 duplicate/illegal transitions fail; F4 non-owners cannot
read private inquiries; F5 non-admins cannot use the manager queue; F6 claim
collisions cannot overwrite assignments; F7 invalid reasons/targets do not
mutate records; F8 corrupt or failed storage never reports a successful write;
F9 a committed create with a lost response replays the original record for the
same actor/key; F10 reusing that key for different content conflicts; F11
duplicated or malformed idempotency metadata fails closed; F12 untrusted
source-row/confidence values are type-checked and bounded before storage; F13
the client sends no create/reply request if secure randomness or pending storage
is unavailable; F14 numeric quantities must be finite and positive, preventing
non-standard JSON values from poisoning the store; F15 manager claim/assign/
close actions require a stable operation key, replay the current record for
matching retries, conflict on changed payloads, and reject malformed or
over-limit action metadata before mutation; assignment target validation runs
inside the same local write lock after replay detection.
File-backed mutations use a process lock plus POSIX flock for local multi-worker
serialization; this JSON store does not support multi-host production writes.
See test_customer_quote_flow.py for the regression cases.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


class QuoteFlowError(ValueError):
    """Invalid inquiry data, state transition, or missing record."""


class QuoteFlowConflict(QuoteFlowError):
    """A concurrent assignment or stale state needs a fresh read."""


_LOCK = threading.RLock()
_MAX_LINES = 200
_LINE_FIELDS = ("sample_name", "item_name", "standard", "quantity", "unit", "confidence", "source_row")
_FAILURE_REASONS = {
    "preview_disabled": "CRM 预览未启用",
    "source_unavailable": "CRM 只读数据不可用",
    "source_mismatch": "CRM 只读数据模式不匹配",
    "project_conflict": "CRM 项目映射冲突",
    "preview_invalid": "CRM 预览数据无效",
}
_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_MAX_ADMIN_OPERATIONS = 100
_PRIVATE_RECORD_FIELDS = {"_create_operation_fingerprint", "_create_payload_hash", "admin_operations"}


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


def _path() -> Path:
    configured = os.getenv("P0_CUSTOMER_QUOTE_FLOW_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = Path(os.getenv("P0_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return root / "customer-service" / "quote_inquiries.json"


def _empty() -> dict:
    return {"version": 0, "records": [], "events": []}


def _read() -> dict:
    path = _path()
    if not path.exists():
        return _empty()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuoteFlowError("询价工单存储不可用") from exc
    if (not isinstance(document, dict) or not isinstance(document.get("records"), list)
            or not isinstance(document.get("events"), list)):
        raise QuoteFlowError("询价工单存储格式错误")
    return document


def _write(document: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _record(document: dict, inquiry_id: str) -> dict:
    rows = [row for row in document["records"] if row.get("inquiry_id") == inquiry_id]
    if len(rows) != 1:
        raise QuoteFlowError("询价工单不存在")
    return rows[0]


def _public(record: dict) -> dict:
    return {key: deepcopy(value) for key, value in record.items() if key not in _PRIVATE_RECORD_FIELDS}


def _create_operation(operation_id: str, actor: str, payload: dict) -> tuple[str, str]:
    if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise QuoteFlowError("询价创建操作编号无效")
    fingerprint = hashlib.sha256(f"{actor}\0{operation_id}".encode("utf-8")).hexdigest()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return fingerprint, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _admin_operation(inquiry_id: str, actor: str, action: str, operation_id: str, payload: dict) -> tuple[str, str]:
    if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise QuoteFlowError("询价队列操作编号无效")
    fingerprint = hashlib.sha256(
        f"quote-admin-action\0{inquiry_id}\0{actor}\0{action}\0{operation_id}".encode("utf-8")
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
        raise QuoteFlowError("询价队列操作幂等记录格式错误")
    fingerprints = [item["fingerprint"] for item in operations]
    if len(fingerprints) != len(set(fingerprints)):
        raise QuoteFlowError("询价队列操作幂等记录重复")
    return operations


def _event(document: dict, record: dict, actor: str, action: str, reason: str = "", **extra) -> None:
    timestamp = _now()
    record["updated_at"] = timestamp
    record.setdefault("history", []).append({
        "at": timestamp, "actor": actor, "action": action,
        "from": extra.pop("from_status", record.get("status", "")),
        "to": record.get("status", ""), "reason": reason,
        **extra,
    })
    document["version"] = int(document.get("version", 0)) + 1
    document["events"].append({
        "at": timestamp, "actor": actor, "action": action,
        "inquiry_id": record["inquiry_id"], "version": document["version"],
        "status": record.get("status", ""),
    })


def _clean_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise QuoteFlowError("询价必须是对象")
    project = str(payload.get("project_name") or "").strip()
    if not project or len(project) > 300:
        raise QuoteFlowError("检测项目不能为空且不能超过 300 字")
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= _MAX_LINES:
        raise QuoteFlowError(f"询价明细必须是 1 到 {_MAX_LINES} 行")
    lines = []
    for index, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict):
            raise QuoteFlowError(f"第 {index} 行明细格式错误")
        line = {key: raw.get(key) for key in _LINE_FIELDS if key in raw}
        for key in ("sample_name", "item_name", "standard", "unit"):
            value = str(line.get(key) or "").strip()
            if len(value) > 300:
                raise QuoteFlowError(f"第 {index} 行 {key} 长度超限")
            line[key] = value
        quantity = line.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float, str))):
            raise QuoteFlowError(f"第 {index} 行数量格式错误")
        if isinstance(quantity, (int, float)) and (
            not 0 < quantity < 10**40
            or (isinstance(quantity, float) and not math.isfinite(quantity))
        ):
            raise QuoteFlowError(f"第 {index} 行数量必须为有限正数且不超过 40 位")
        if isinstance(quantity, str) and len(quantity.strip()) > 40:
            raise QuoteFlowError(f"第 {index} 行数量长度超限")
        source_row = line.get("source_row")
        if source_row is not None and (
            isinstance(source_row, bool) or not isinstance(source_row, int)
            or not 1 <= source_row <= 1_000_000
        ):
            raise QuoteFlowError(f"第 {index} 行来源行号格式错误")
        confidence = line.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1 or not math.isfinite(confidence)
        ):
            raise QuoteFlowError(f"第 {index} 行置信度必须在 0 到 1 之间")
        line["project_name"] = project
        line["line_id"] = f"INQUIRY-{index:03d}"
        lines.append(line)
    context = str(payload.get("context") or "").strip()
    if len(context) > 2000:
        raise QuoteFlowError("补充说明不能超过 2000 字")
    return {"project_name": project, "lines": lines, "context": context}


def create(payload: dict, actor: str, operation_id: str | None = None, *, with_replay: bool = False):
    data = _clean_payload(payload)
    operation_fingerprint = payload_hash = ""
    if operation_id is not None:
        operation_fingerprint, payload_hash = _create_operation(operation_id, actor, data)
    now = _now()
    record = {
        "inquiry_id": "QINQ-" + secrets.token_hex(8),
        **data,
        "status": "draft",
        "preview": None,
        "manual_reason": "",
        "assigned_to": "",
        "resolved_reason": "",
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
        "history": [{"at": now, "actor": actor, "action": "created", "from": "", "to": "draft", "reason": "询价草稿已保存"}],
    }
    if operation_fingerprint:
        record["_create_operation_fingerprint"] = operation_fingerprint
        record["_create_payload_hash"] = payload_hash
    with _locked():
        document = _read()
        if operation_fingerprint:
            matches = [row for row in document["records"] if row.get("_create_operation_fingerprint") == operation_fingerprint]
            if len(matches) > 1:
                raise QuoteFlowError("询价工单存储中的幂等记录重复，已拒绝写入")
            if matches:
                existing = matches[0]
                if existing.get("_create_payload_hash") != payload_hash:
                    raise QuoteFlowConflict("询价创建操作编号已用于不同内容")
                result = _public(existing)
                return (result, True) if with_replay else result
        document["records"].append(record)
        document["version"] = int(document.get("version", 0)) + 1
        document["events"].append({"at": now, "actor": actor, "action": "created", "inquiry_id": record["inquiry_id"], "version": document["version"], "status": "draft"})
        _write(document)
    result = _public(record)
    return (result, False) if with_replay else result


def update(inquiry_id: str, payload: dict, actor: str) -> dict:
    data = _clean_payload(payload)
    with _locked():
        document = _read()
        record = _record(document, inquiry_id)
        if record["status"] not in {"draft", "need_info", "needs_human"}:
            raise QuoteFlowConflict("当前状态不可修改询价内容")
        old_status = record["status"]
        record.update(data, status="draft", preview=None, manual_reason="")
        _event(document, record, actor, "updated", "询价信息已修改", from_status=old_status)
        _write(document)
        return _public(record)


def submit(inquiry_id: str, actor: str, *, preview: dict | None = None, failure_code: str = "") -> dict:
    if failure_code and failure_code not in _FAILURE_REASONS:
        raise QuoteFlowError("预览失败类型无效")
    with _locked():
        document = _read()
        record = _record(document, inquiry_id)
        if record["status"] != "draft":
            raise QuoteFlowConflict("当前状态不能重复提交；如需修改请先补充信息")
        previous = record["status"]
        record["status"] = "submitted"
        _event(document, record, actor, "submitted", "询价已提交", from_status=previous)
        if failure_code:
            record.update(status="needs_human", preview=None, manual_reason=_FAILURE_REASONS[failure_code])
        elif not isinstance(preview, dict) or not isinstance(preview.get("lines"), list):
            record.update(status="needs_human", preview=None, manual_reason="报价预览结果缺失")
        else:
            actions = {line.get("action") for line in preview["lines"] if isinstance(line, dict)}
            status = "need_info" if "ask_customer" in actions else "needs_human" if "manual_review" in actions else "preview"
            reason = "" if status == "preview" else "询价信息待补充" if status == "need_info" else "报价事实需人工复核"
            record.update(status=status, preview=preview, manual_reason=reason)
        _event(document, record, actor, "previewed" if record["preview"] else "routed_to_human",
               record["manual_reason"], from_status="submitted")
        _write(document)
        return _public(record)


def get(inquiry_id: str) -> dict:
    return _public(_record(_read(), inquiry_id))


def list_records(*, status: str = "", assigned_to: str = "") -> list[dict]:
    rows = _read()["records"]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    if assigned_to:
        rows = [row for row in rows if row.get("assigned_to") == assigned_to]
    return [_public(row) for row in sorted(rows, key=lambda row: row.get("updated_at", ""), reverse=True)]


def _admin_operation_start(record: dict, operation: tuple[str, str] | None):
    operations = _admin_operations(record)
    if not operation:
        return operations, False
    fingerprint, payload_hash = operation
    prior = next((item for item in operations if item["fingerprint"] == fingerprint), None)
    if prior:
        if prior["payload_hash"] != payload_hash:
            raise QuoteFlowConflict("询价队列操作编号已用于不同内容")
        return operations, True
    if len(operations) >= _MAX_ADMIN_OPERATIONS:
        raise QuoteFlowConflict("询价队列操作记录已达本地上限，请联系维护人员")
    return operations, False


def _admin_operation_finish(record: dict, operations: list[dict], operation: tuple[str, str] | None) -> None:
    if operation:
        operations.append({"fingerprint": operation[0], "payload_hash": operation[1]})
        record["admin_operations"] = operations


def replay_admin_action(inquiry_id: str, actor: str, action: str, operation_id: str, payload: dict) -> dict | None:
    """Preflight a retry before mutable target-account checks; mutation rechecks under lock."""
    operation = _admin_operation(inquiry_id, str(actor or "").strip(), action, operation_id, payload)
    with _locked():
        record = _record(_read(), inquiry_id)
        operations = _admin_operations(record)
        previous = next((item for item in operations if item["fingerprint"] == operation[0]), None)
        if not previous:
            return None
        if previous["payload_hash"] != operation[1]:
            raise QuoteFlowConflict("询价队列操作编号已用于不同内容")
        return _public(record)


def claim(
    inquiry_id: str, actor: str, reason: str, operation_id: str | None = None,
    *, with_replay: bool = False,
):
    reason = str(reason or "").strip()
    actor = str(actor or "").strip()
    if not reason or len(reason) > 500:
        raise QuoteFlowError("接单原因不能为空且不能超过 500 字")
    if not actor:
        raise QuoteFlowError("接单账号无效")
    operation = _admin_operation(inquiry_id, actor, "claim", operation_id, {"reason": reason}) if operation_id is not None else None
    with _locked():
        document = _read()
        record = _record(document, inquiry_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record)
            return (result, True) if with_replay else result
        if record["status"] not in {"needs_human", "need_info", "preview"}:
            raise QuoteFlowConflict("当前工单状态不能接单")
        if record.get("assigned_to") and record["assigned_to"] != actor:
            raise QuoteFlowConflict("工单已由其他人员接单")
        record["assigned_to"] = actor
        _event(document, record, actor, "claimed", reason)
        _admin_operation_finish(record, operations, operation)
        _write(document)
        result = _public(record)
        return (result, False) if with_replay else result


def assign(
    inquiry_id: str, actor: str, assignee: str, reason: str,
    operation_id: str | None = None, *, with_replay: bool = False,
    validate_assignee=None,
):
    assignee, reason = str(assignee or "").strip(), str(reason or "").strip()
    actor = str(actor or "").strip()
    if not assignee or not reason or len(reason) > 500:
        raise QuoteFlowError("转派对象和原因不能为空；原因不能超过 500 字")
    if not actor:
        raise QuoteFlowError("转派账号无效")
    operation = _admin_operation(
        inquiry_id, actor, "assign", operation_id, {"assignee": assignee, "reason": reason},
    ) if operation_id is not None else None
    with _locked():
        document = _read()
        record = _record(document, inquiry_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record)
            return (result, True) if with_replay else result
        if validate_assignee is not None:
            validate_assignee(record)
        if record["status"] not in {"needs_human", "need_info", "preview"}:
            raise QuoteFlowConflict("当前工单状态不能转派")
        previous_assignee = record.get("assigned_to", "")
        record["assigned_to"] = assignee
        _event(document, record, actor, "assigned", reason, previous_assignee=previous_assignee)
        _admin_operation_finish(record, operations, operation)
        _write(document)
        result = _public(record)
        return (result, False) if with_replay else result


def resolve(
    inquiry_id: str, actor: str, reason: str, operation_id: str | None = None,
    *, with_replay: bool = False,
):
    reason = str(reason or "").strip()
    actor = str(actor or "").strip()
    if not reason or len(reason) > 500:
        raise QuoteFlowError("关闭原因不能为空且不能超过 500 字")
    if not actor:
        raise QuoteFlowError("关闭账号无效")
    operation = _admin_operation(inquiry_id, actor, "resolve", operation_id, {"reason": reason}) if operation_id is not None else None
    with _locked():
        document = _read()
        record = _record(document, inquiry_id)
        operations, replayed = _admin_operation_start(record, operation)
        if replayed:
            result = _public(record)
            return (result, True) if with_replay else result
        if record["status"] not in {"needs_human", "need_info", "preview"}:
            raise QuoteFlowConflict("当前工单状态不能关闭")
        previous = record["status"]
        record.update(status="resolved", resolved_reason=reason)
        _event(document, record, actor, "resolved", reason, from_status=previous)
        _admin_operation_finish(record, operations, operation)
        _write(document)
        result = _public(record)
        return (result, False) if with_replay else result


def failure_reason(code: str) -> str:
    return _FAILURE_REASONS.get(code, "")
