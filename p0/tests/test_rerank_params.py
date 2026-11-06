import json

import httpx

from app import weknora
from app.embeddings import OllamaEmbedder


def _chunks():
    return [
        {
            "id": "chunk-1",
            "standard_id": "std_1",
            "standard_no": "Q/TEST 123-2024",
            "standard_name": "测试标准",
            "item_name": "外观检查",
            "clause": "5.1",
            "kind": "requirement",
            "kind_label": "技术要求/判定要求",
            "page": 2,
            "text": "外观应无缺陷。",
        }
    ]


def _search(monkeypatch, top_k=6):
    captured = {}
    cfg = weknora.WeKnoraConfig(base_url="http://weknora.test", api_key="sk-test", kb_id="kb-1")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": []})

    monkeypatch.setattr(weknora, "config_from_env", lambda: cfg)
    monkeypatch.setattr(
        weknora,
        "_client",
        lambda _cfg: httpx.Client(base_url=cfg.base_url, transport=httpx.MockTransport(handler)),
    )
    return captured, weknora.search("5.1 外观要求", _chunks(), top_k=top_k)


def test_rerank_request_uses_15_top_k_and_30_candidates(monkeypatch):
    monkeypatch.setenv("P0_WEKNORA_RERANK", "1")
    monkeypatch.delenv("P0_WEKNORA_RERANK_TOP_K", raising=False)
    monkeypatch.delenv("P0_WEKNORA_RERANK_CANDIDATES", raising=False)
    captured, result = _search(monkeypatch)
    assert captured["path"] == "/api/v1/knowledge-search"
    assert captured["body"]["match_count"] == 30
    assert captured["body"]["rerank"] == {"enabled": True, "top_k": 15}
    assert result["rerank"] is True


def test_rerank_params_are_overridable(monkeypatch):
    monkeypatch.setenv("P0_WEKNORA_RERANK", "1")
    monkeypatch.setenv("P0_WEKNORA_RERANK_TOP_K", "12")
    monkeypatch.setenv("P0_WEKNORA_RERANK_CANDIDATES", "24")
    captured, _result = _search(monkeypatch)
    assert captured["body"]["match_count"] == 24
    assert captured["body"]["rerank"]["top_k"] == 12


def test_rerank_never_asks_for_fewer_than_the_caller_wants(monkeypatch):
    monkeypatch.setenv("P0_WEKNORA_RERANK", "1")
    monkeypatch.setenv("P0_WEKNORA_RERANK_CANDIDATES", "5")
    captured, _result = _search(monkeypatch, top_k=8)
    assert captured["body"]["rerank"]["top_k"] == 15
    assert captured["body"]["match_count"] == 15


def test_hybrid_path_keeps_plain_match_count(monkeypatch):
    monkeypatch.delenv("P0_WEKNORA_RERANK", raising=False)
    captured, result = _search(monkeypatch)
    assert captured["path"] == "/api/v1/knowledge-bases/kb-1/hybrid-search"
    assert captured["body"] == {"query_text": "5.1 外观要求", "match_count": 6}
    assert result["rerank"] is False


def test_embedding_batch_default_is_64(monkeypatch):
    monkeypatch.delenv("P0_EMBEDDING_BATCH", raising=False)
    assert OllamaEmbedder().batch_size == 64
