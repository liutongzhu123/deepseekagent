"""Local-only model routing guards F75–F80 without customer data."""

import json
import urllib.request

import pytest

from app import embeddings, model_endpoint, model_readiness, query_rewrite, semantic_audit, weknora


@pytest.mark.parametrize("url", [
    "http://localhost:11434/v1", "http://127.0.0.1:11434", "http://10.2.3.4:8081",
    "http://172.16.0.1", "https://192.168.1.5", "http://[::1]:11434", "http://[fd00::1]",
])
def test_f77_local_ip_routes_are_accepted(url):
    assert model_endpoint.is_local_endpoint(url)


@pytest.mark.parametrize("url", [
    "https://api.deepseek.com", "http://8.8.8.8", "http://169.254.169.254",
    "http://127.0.0.1.evil.test", "http://127.0.0.1@evil.test",
    "http://localhost:bad", "file:///tmp/model", "http://localhost/path#remote",
])
def test_f76_f78_public_or_malformed_routes_are_rejected(url):
    assert not model_endpoint.is_local_endpoint(url)


def test_f77_internal_host_requires_exact_allowlist(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_HOSTS", "weknora-ollama, models.internal")
    assert model_endpoint.is_local_endpoint("http://weknora-ollama:11434")
    assert not model_endpoint.is_local_endpoint("https://models.internal/v1")
    assert not model_endpoint.is_local_endpoint("https://models.internal.evil.test/v1")


def test_f75_public_mode_preserves_existing_endpoint(monkeypatch):
    monkeypatch.setenv("P0_ENV", "test")
    monkeypatch.delenv("P0_LOCAL_MODEL_ONLY", raising=False)
    assert model_endpoint.endpoint_allowed("https://api.deepseek.com")


def test_f79_semantic_blocks_public_endpoint_before_post(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    cfg = semantic_audit.SemanticConfig(enabled=True, api_key="key", base_url="https://api.deepseek.com")
    result = semantic_audit.audit_semantics("客户报告", cfg, post=lambda *_: pytest.fail("network call"))
    assert (result["status"], result["reason"]) == ("error", "non_local_model_endpoint")


def test_f79_semantic_allows_local_endpoint(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    cfg = semantic_audit.SemanticConfig(enabled=True, api_key="local", base_url="http://127.0.0.1:11434/v1")
    result = semantic_audit.audit_semantics(
        "模拟报告", cfg,
        post=lambda *_: {"choices": [{"message": {"content": json.dumps({"findings": []})}}]},
    )
    assert result["status"] == "ok"


def test_f79_rewrite_blocks_public_endpoint_before_cache_or_transport(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    cfg = query_rewrite.RewriteConfig(enabled=True, api_key="key", base_url="https://api.deepseek.com")

    class Cache:
        def key(self, *_):
            pytest.fail("cache read")

    result = query_rewrite.rewrite("客户问题", config=cfg, cache=Cache(),
                                   transport=lambda *_: pytest.fail("network call"))
    assert (result.query, result.applied, result.reason) == ("客户问题", False, "non_local_model_endpoint")


def test_f79_weknora_invalid_config_cannot_create_client(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    monkeypatch.setenv("P0_WEKNORA_BASE_URL", "https://example.com")
    monkeypatch.setenv("P0_WEKNORA_API_KEY", "test")
    monkeypatch.setenv("P0_WEKNORA_KB_ID", "kb")
    monkeypatch.delenv("P0_WEKNORA_REQUIRED", raising=False)
    monkeypatch.setattr(weknora.httpx, "Client", lambda **_: pytest.fail("network client"))
    assert not weknora.configured()
    assert weknora.required()
    with pytest.raises(weknora.WeKnoraUnavailable, match="本地模型"):
        weknora._client(weknora.config_from_env())


def test_f79_embedding_rejects_public_endpoint_before_network(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    monkeypatch.setattr(model_endpoint, "open_model_request", lambda *_args, **_kwargs: pytest.fail("network"))
    client = embeddings.OllamaEmbedder(base_url="https://example.com")
    with pytest.raises(embeddings.EmbeddingUnavailable, match="本地模型"):
        client.embed(["模拟标准条文"])
    with pytest.raises(embeddings.EmbeddingUnavailable, match="本地模型"):
        client.health()


def test_f80_local_only_disables_proxy_and_redirects(monkeypatch):
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "1")
    handlers = []

    class Opener:
        def open(self, request, timeout):
            return (request, timeout)

    def fake_build_opener(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    assert model_endpoint.open_model_request("http://127.0.0.1", 3) == ("http://127.0.0.1", 3)
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
    redirect = next(h for h in handlers if isinstance(h, model_endpoint._NoRedirect))
    assert redirect.redirect_request(None, None, 307, "redirect", {}, "https://example.com") is None


def test_f81_production_cannot_disable_local_model_routing(monkeypatch):
    monkeypatch.setenv("P0_ENV", "production")
    monkeypatch.setenv("P0_LOCAL_MODEL_ONLY", "0")
    assert model_endpoint.local_only()
    assert not model_endpoint.endpoint_allowed("https://api.deepseek.com")
    assert model_endpoint.endpoint_allowed("http://127.0.0.1:11434/v1")

    semantic = semantic_audit.SemanticConfig(
        enabled=True, api_key="key", base_url="https://api.deepseek.com"
    )
    result = semantic_audit.audit_semantics(
        "客户资料", semantic, post=lambda *_: pytest.fail("public semantic request")
    )
    assert (result["status"], result["reason"]) == ("error", "non_local_model_endpoint")

    rewrite = query_rewrite.rewrite(
        "客户问题",
        config=query_rewrite.RewriteConfig(
            enabled=True, api_key="key", base_url="https://api.deepseek.com"
        ),
        transport=lambda *_: pytest.fail("public rewrite request"),
    )
    assert (rewrite.applied, rewrite.reason) == (False, "non_local_model_endpoint")

    monkeypatch.setenv("P0_WEKNORA_BASE_URL", "https://models.example.test")
    monkeypatch.setenv("P0_WEKNORA_API_KEY", "key")
    monkeypatch.setenv("P0_WEKNORA_KB_ID", "kb")
    monkeypatch.setattr(weknora.httpx, "Client", lambda **_: pytest.fail("public WeKnora request"))
    with pytest.raises(weknora.WeKnoraUnavailable, match="本地模型"):
        weknora._client(weknora.config_from_env())

    monkeypatch.setattr(model_endpoint, "open_model_request", lambda *_args, **_kwargs: pytest.fail("public embedding request"))
    with pytest.raises(embeddings.EmbeddingUnavailable, match="本地模型"):
        embeddings.OllamaEmbedder(base_url="https://models.example.test").embed(["客户资料"])

    monkeypatch.setenv("P0_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("P0_SEMANTIC_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("P0_QUERY_REWRITE", "1")
    monkeypatch.setenv("P0_QUERY_REWRITE_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("P0_QUERY_REWRITE_API_KEY", "key")
    monkeypatch.setenv("P0_EMBEDDING_URL", "https://models.example.test")
    readiness = model_readiness.snapshot()
    assert readiness["local_only"] is True
    assert readiness["semantic_review"]["state"] == "blocked_non_local_endpoint"
    assert readiness["query_rewrite"]["state"] == "blocked_non_local_endpoint"
    assert readiness["weknora"]["state"] == "blocked_non_local_endpoint"
    assert readiness["embeddings"]["state"] == "blocked_non_local_endpoint"
