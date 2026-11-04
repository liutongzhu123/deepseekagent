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


def test_prompt_uses_configured_criteria():
    from app.semantic_audit import build_prompt

    cfg = _config(criteria={"S01": "只报错别字", "S02": "只报产品名", "S03": "只报结论", "S04": "只报项目"})
    system, instruction = build_prompt(cfg)

    assert "只报错别字" in instruction
    assert "只报产品名" in instruction
    assert "没有明确缺陷时 findings 必须为空数组" in instruction
    assert system


def test_duplicate_rule_findings_are_collapsed():
    content = json.dumps({"findings": [
        {"rule_id": "S01", "status": "warn", "message": "第一条", "evidence": "a"},
        {"rule_id": "S01", "status": "warn", "message": "第二条", "evidence": "b"},
    ]})
    result = audit_semantics("报告正文", config=_config(), post=_ok_post(content))

    assert result["status"] == "ok"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["layer"] == "semantic"


def test_prompt_payload_is_redacted():
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"findings":[]}'}}]}

    audit_semantics("VIN LSVAA4187N2N12345 电话 13800138000", config=_config(), post=fake_post)
    text = captured["payload"]["messages"][1]["content"]
    assert "LSVAA4187N2N12345" not in text
    assert "13800138000" not in text
