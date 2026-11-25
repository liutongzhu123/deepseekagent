import hashlib

import pytest

from app.integration_simulator import IntegrationSimulationError, IntegrationSimulator


def test_success_is_normalized_and_does_not_record_customer_payload():
    simulator = IntegrationSimulator()
    secret = "客户手机号-13800000000"
    result = simulator.dispatch("crm", "read_quote", {"phone": secret}, "key-success")

    assert result["status"] == "succeeded"
    assert result["external_reference"].startswith("SIM-CRM-")
    assert "idempotency_key_hmac" in result
    assert "idempotency_key_sha256" not in result
    events = simulator.audit_events()
    assert len(events) == 1
    assert secret not in str(events)
    assert "key-success" not in str(events)
    assert "payload" not in events[0]
    assert "idempotency_key_hmac" in events[0]
    assert secret not in result["operation_id"]


def test_action_names_are_fixed_and_operation_ids_are_scoped_to_full_operation():
    simulator = IntegrationSimulator()
    with pytest.raises(IntegrationSimulationError):
        simulator.dispatch("crm", "send_13800000000", {}, "shared-key")

    crm = simulator.dispatch("crm", "read_quote", {}, "shared-key")
    email = simulator.dispatch("email", "send_confirmation", {}, "shared-key")
    qcc = simulator.dispatch("qcc", "verify_company", {}, "shared-key")
    assert len({crm["operation_id"], email["operation_id"], qcc["operation_id"]}) == 3


def test_low_entropy_idempotency_key_is_not_a_plain_hash_in_audit():
    simulator = IntegrationSimulator()
    phone_number = "13800000000"
    result = simulator.dispatch("email", "send_confirmation", {}, phone_number)
    event = simulator.audit_events()[0]
    assert phone_number not in str(event)
    assert hashlib.sha256(phone_number.encode()).hexdigest() not in str(event)
    assert result["operation_id"] != f"SIM-{hashlib.sha256(phone_number.encode()).hexdigest()[:20]}"


def test_low_entropy_idempotency_key_is_not_retained_in_simulator_index():
    simulator = IntegrationSimulator()
    phone_number = "13800000000"
    simulator.dispatch("email", "send_confirmation", {"template": "demo"}, phone_number)

    retained = repr(simulator._entries)
    assert phone_number not in retained
    assert hashlib.sha256(phone_number.encode()).hexdigest() not in retained


@pytest.mark.parametrize(
    ("mode", "expected_status", "failure_code"),
    [
        ("timeout", "retryable_failure", "simulated_timeout"),
        ("auth_failure", "permanent_failure", "simulated_auth_failure"),
        ("rate_limited", "retryable_failure", "simulated_rate_limit"),
        ("schema_changed", "schema_changed", "required_field_contract_changed"),
    ],
)
def test_failure_modes_are_classified_without_raw_diagnostics(mode, expected_status, failure_code):
    simulator = IntegrationSimulator()
    result = simulator.dispatch("qcc", "verify_company", {"company": "合成企业"}, f"key-{mode}", mode=mode)
    assert result["status"] == expected_status
    assert result["failure_code"] == failure_code
    assert "合成企业" not in str(result)
    if mode == "rate_limited":
        assert result["retry_after_seconds"] == 5


def test_retryable_failure_can_retry_with_same_key_but_success_is_idempotent():
    simulator = IntegrationSimulator()
    payload = {"template": "demo"}
    failed = simulator.dispatch("email", "send_confirmation", payload, "stable-key", mode="timeout")
    retried = simulator.dispatch("email", "send_confirmation", payload, "stable-key", mode="success")
    replayed = simulator.dispatch("email", "send_confirmation", payload, "stable-key", mode="timeout")

    assert failed["status"] == "retryable_failure"
    assert retried["status"] == "succeeded"
    assert replayed == retried
    assert len(simulator.audit_events()) == 2


def test_same_key_with_changed_payload_is_a_conflict_and_does_not_replace_original():
    simulator = IntegrationSimulator()
    first = simulator.dispatch("wechat", "send_message", {"text": "A"}, "same-key")
    conflict = simulator.dispatch("wechat", "send_message", {"text": "B"}, "same-key")
    replay = simulator.dispatch("wechat", "send_message", {"text": "A"}, "same-key", mode="auth_failure")

    assert first["status"] == "succeeded"
    assert conflict["status"] == "duplicate_or_conflict"
    assert "idempotency_key_hmac" in first and "idempotency_key_hmac" in conflict
    assert "idempotency_key_sha256" not in first and "idempotency_key_sha256" not in conflict
    assert replay == first
    assert "A" not in str(simulator.audit_events())
    assert "B" not in str(simulator.audit_events())


def test_accepted_timeout_stays_pending_until_explicit_readback():
    simulator = IntegrationSimulator()
    pending = simulator.dispatch("wecom", "send_notice", {"body": "演示消息"}, "pending-key", mode="accepted_timeout")
    assert pending["status"] == "pending"
    assert "external_reference" not in pending

    reconciled = simulator.read_back("wecom", "send_notice", "pending-key")
    assert reconciled["status"] == "succeeded"
    assert reconciled["external_reference"].startswith("SIM-WECOM-")
    assert simulator.read_back("wecom", "send_notice", "pending-key") == reconciled
    assert [event["status"] for event in simulator.audit_events()] == ["pending", "succeeded"]
    assert "演示消息" not in str(simulator.audit_events())


def test_invalid_inputs_fail_before_recording_any_operation():
    simulator = IntegrationSimulator()
    with pytest.raises(IntegrationSimulationError):
        simulator.dispatch("unknown", "read", {}, "k")
    with pytest.raises(IntegrationSimulationError):
        simulator.dispatch("crm", "read", {"bad": float("nan")}, "k")
    with pytest.raises(IntegrationSimulationError):
        simulator.dispatch("crm", "read", {}, " ")
    assert simulator.audit_events() == []
