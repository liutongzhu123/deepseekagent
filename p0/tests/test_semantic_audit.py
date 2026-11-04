import json

from app.semantic_audit import SemanticConfig, audit_semantics, redact


def _config(**overrides) -> SemanticConfig:
    base = dict(enabled=True, api_key="test-key", base_url="https://example.invalid", timeout_seconds=1, max_chars=500)
    base.update(overrides)
    return SemanticConfig(**base)


def _ok_post(content: str):
    return lambda url, payload, headers, timeout: {"choices": [{"message": {"content": content}}]}


def test_semantic_disabled_does_not_call():
    result = audit_semantics("报告", config=SemanticConfig(enabled=False), post=lambda *args: (_ for _ in ()).throw(AssertionError("called")))

    assert result["status"] == "skipped"
    assert result["reason"] == "semantic_disabled"


def test_semantic_missing_key_degrades():
    result = audit_semantics("报告", config=_config(api_key=""))

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_api_key"


def test_semantic_parses_strict_json():
    content = json.dumps({"findings": [{"rule_id": "S03", "status": "warn", "severity": "low", "message": "结论表述可能不完整", "evidence": "结论", "suggestion": "补充"}]})
    result = audit_semantics("报告正文", config=_config(), post=_ok_post(content))

    assert result["status"] == "ok"
    assert result["findings"][0]["rule_id"] == "S03"


def test_semantic_invalid_json_is_error_not_pass():
    result = audit_semantics("报告正文", config=_config(), post=_ok_post("not json"))

    assert result["status"] == "error"
    assert "schema" in result["reason"]


def test_semantic_transport_error_is_error():
    def boom(*args):
        raise TimeoutError("timeout")

    result = audit_semantics("报告正文", config=_config(), post=boom)

    assert result["status"] == "error"
    assert "transport" in result["reason"]


def test_redact_personal_data():
    text = redact("VIN LSVAA4187N2N12345 电话 13800138000 邮箱 a@b.com")
    assert "LSVAA4187N2N12345" not in text
    assert "13800138000" not in text
    assert "a@b.com" not in text
