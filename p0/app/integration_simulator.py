"""Deterministic, in-memory integration failure simulator.

This module deliberately has no HTTP, socket, subprocess, or provider SDK code.
It exercises the normalized integration contract without connecting to CRM,
Qichacha, WeChat/WeCom, or email. It is not a production adapter.

Failure paths (F1-F7): successful normalized receipt; transient timeout;
permanent authentication failure; bounded rate limit; idempotent replay and
payload conflict; schema change quarantine; accepted-timeout pending until
explicit read-back. Inputs are fingerprinted and never stored in audit events.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import threading
from typing import Any


SYSTEMS = frozenset({"crm", "qcc", "wechat", "wecom", "email"})
MODES = frozenset({"success", "timeout", "auth_failure", "rate_limited", "schema_changed", "accepted_timeout"})
ACTIONS = frozenset({"read_quote", "verify_company", "send_confirmation", "send_message", "send_notice"})


class IntegrationSimulationError(ValueError):
    """Invalid simulator input; never indicates a provider/network failure."""


class IntegrationSimulator:
    """An explicit test double with in-memory idempotency and safe event metadata."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._audit_secret = secrets.token_bytes(32)
        self._entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    @staticmethod
    def _payload_fingerprint(payload: dict[str, Any]) -> str:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise IntegrationSimulationError("payload 必须是可序列化的 JSON 对象") from exc
        if len(encoded) > 100_000:
            raise IntegrationSimulationError("模拟 payload 超过 100 KB")
        return hashlib.sha256(encoded).hexdigest()

    def _key_digest(self, system: str, action: str, idempotency_key: str) -> str:
        # HMAC prevents dictionary recovery of low-entropy keys such as phone numbers.
        material = "\x1f".join((system, action, idempotency_key)).encode("utf-8")
        return hmac.new(self._audit_secret, material, hashlib.sha256).hexdigest()

    def _event(self, result: dict[str, Any], key_digest: str) -> None:
        self._events.append({
            "operation_id": result["operation_id"],
            "system": result["system"],
            "action": result["action"],
            "status": result["status"],
            "idempotency_key_hmac": key_digest,
        })

    def dispatch(
        self,
        system: str,
        action: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        mode: str = "success",
    ) -> dict[str, Any]:
        """Return a normalized synthetic result; this method never performs I/O."""
        # F1: explicit allowlists prevent accidentally treating this as a generic URL/client.
        if not isinstance(system, str) or system not in SYSTEMS or not isinstance(action, str) or action not in ACTIONS:
            raise IntegrationSimulationError("system/action 无效")
        if not isinstance(payload, dict):
            raise IntegrationSimulationError("payload 必须是 JSON 对象")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise IntegrationSimulationError("必须提供长度不超过 200 的幂等键")
        if not isinstance(mode, str) or mode not in MODES:
            raise IntegrationSimulationError("不支持的模拟故障模式")

        fingerprint = self._payload_fingerprint(payload)
        key_digest = self._key_digest(system, action, idempotency_key)
        # Keep the raw key only on the call stack; the simulator retains its
        # process-secret HMAC as the lookup key and never stores caller text.
        storage_key = (system, action, key_digest)
        operation_id = f"SIM-{key_digest[:20]}"
        with self._lock:
            existing = self._entries.get(storage_key)
            if existing:
                # F5: exact replay is stable; reusing a key for different input is a conflict.
                if existing["fingerprint"] != fingerprint:
                    conflict = {
                        "operation_id": operation_id,
                        "system": system,
                        "action": action,
                        "status": "duplicate_or_conflict",
                        "idempotency_key_hmac": key_digest,
                    }
                    self._event(conflict, key_digest)
                    return conflict
                if existing["result"].get("status") != "retryable_failure":
                    return copy.deepcopy(existing["result"])

            result: dict[str, Any] = {
                "operation_id": operation_id,
                "system": system,
                "action": action,
                "idempotency_key_hmac": key_digest,
            }
            reconciled: dict[str, Any] | None = None
            if mode == "success":
                result.update(status="succeeded", external_reference=f"SIM-{system.upper()}-{key_digest[:12]}")
            elif mode == "timeout":
                # F2: transient failure keeps the operation unresolved and contains no response data.
                result.update(status="retryable_failure", failure_code="simulated_timeout")
            elif mode == "auth_failure":
                # F3: credentials are never accepted as inputs or reflected in diagnostics.
                result.update(status="permanent_failure", failure_code="simulated_auth_failure")
            elif mode == "rate_limited":
                # F4: expose a fixed bounded delay rather than trusting unverified provider data.
                result.update(status="retryable_failure", failure_code="simulated_rate_limit", retry_after_seconds=5)
            elif mode == "schema_changed":
                # F6: no raw provider response is kept when a required field contract changes.
                result.update(status="schema_changed", failure_code="required_field_contract_changed")
            elif mode == "accepted_timeout":
                # F7: remote acceptance is not business success until an explicit read-back.
                result.update(status="pending", failure_code="receipt_missing")
                reconciled = {
                    **result,
                    "status": "succeeded",
                    "external_reference": f"SIM-{system.upper()}-{key_digest[:12]}",
                }

            self._entries[storage_key] = {
                "fingerprint": fingerprint,
                "result": copy.deepcopy(result),
                "read_back_result": reconciled,
                "read_back_complete": False,
            }
            self._event(result, key_digest)
            return copy.deepcopy(result)

    def read_back(self, system: str, action: str, idempotency_key: str) -> dict[str, Any]:
        """Explicitly reconcile accepted-timeout operations; never auto-advance business state."""
        if not isinstance(system, str) or system not in SYSTEMS or not isinstance(action, str) or action not in ACTIONS:
            raise IntegrationSimulationError("system/action 无效")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise IntegrationSimulationError("幂等键无效")
        key_digest = self._key_digest(system, action, idempotency_key)
        storage_key = (system, action, key_digest)
        with self._lock:
            entry = self._entries.get(storage_key)
            if not entry:
                raise IntegrationSimulationError("模拟操作不存在")
            if entry["read_back_result"] is None:
                return copy.deepcopy(entry["result"])
            if not entry["read_back_complete"]:
                entry["result"] = copy.deepcopy(entry["read_back_result"])
                entry["read_back_complete"] = True
                self._event(entry["result"], key_digest)
            return copy.deepcopy(entry["result"])

    def audit_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._events)
