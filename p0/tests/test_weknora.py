import httpx

from app import standard_qa, weknora


def _record():
    return {
        "id": "std_20260101000000_abcdef",
        "revision": 2,
        "meta": {"standard_no": "Q/TEST 123-2024", "name": "测试标准"},
        "items": [
            {
                "clause": "5.1",
                "name": "外观检查",
                "requirement": "外观应无缺陷。",
                "method": "目测检查。",
                "method_clause": "6.1",
                "page": 2,
                "method_page": 3,
            }
        ],
    }


def _chunks(record):
    return [
        {
            "id": "std_20260101000000_abcdef:5.1:req",
            "standard_id": record["id"],
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


def test_render_markdown_keeps_citation_header():
    record = _record()
    markdown = weknora.render_markdown(record, _chunks(record))
    assert "〔Q/TEST 123-2024 | 条款 5.1 | 第 2 页 | 技术要求 | 外观检查〕" in markdown
    header = weknora._parse_header(markdown)
    assert header == {
        "standard_no": "Q/TEST 123-2024",
        "clause": "5.1",
        "page": 2,
        "kind_label": "技术要求",
        "item_name": "外观检查",
    }


def test_weknora_search_maps_citations(monkeypatch):
    record = _record()
    chunks = _chunks(record)
    cfg = weknora.WeKnoraConfig(base_url="http://weknora.test", api_key="sk-test", kb_id="kb-1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/knowledge-bases/kb-1/hybrid-search"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "chunk-1",
                        "content": "〔Q/TEST 123-2024 | 条款 5.1 | 第 2 页 | 技术要求 | 外观检查〕\n外观应无缺陷。",
                        "knowledge_id": "kn-1",
                        "knowledge_title": "P0 | Q/TEST 123-2024 | 测试标准",
                        "score": 0.8,
                    }
                ],
                "meta": {"rerank": {"top_score": 0.9, "applied": True}},
            },
        )

    def factory(_cfg):
        return httpx.Client(
            base_url=cfg.base_url,
            transport=httpx.MockTransport(handler),
            headers={"X-API-Key": cfg.api_key},
        )

    monkeypatch.setattr(weknora, "config_from_env", lambda: cfg)
    monkeypatch.setattr(weknora, "_client", factory)
    result = weknora.search("外观要求是什么", chunks)
    assert result["top_score"] == 0.9
    citation = result["citations"][0]
    assert citation["clause"] == "5.1"
    assert citation["page"] == 2
    assert citation["standard_no"] == "Q/TEST 123-2024"
    assert citation["quote"] == "外观应无缺陷。"


def test_answer_weknora_refuses_unknown_clause(monkeypatch):
    record = _record()
    chunks = _chunks(record)
    kbs = [{"standard_no": "Q/TEST 123-2024", "standard_name": "测试标准", "chunks": chunks}]
    monkeypatch.setattr(weknora, "config_from_env", lambda: weknora.WeKnoraConfig("http://x", "k", "kb"))
    result = standard_qa.answer_weknora("5.9 是什么要求", kbs)
    assert result["found"] is False
    assert "5.9" in result["answer"]


def test_domain_guard_rejects_unrelated_questions(monkeypatch):
    record = _record()
    chunks = _chunks(record)
    kbs = [{"standard_no": "Q/TEST 123-2024", "standard_name": "测试标准", "chunks": chunks}]
    monkeypatch.setattr(weknora, "config_from_env", lambda: weknora.WeKnoraConfig("http://x", "k", "kb"))
    assert standard_qa.answer_weknora("今天天气怎么样", kbs)["found"] is False
    assert standard_qa.answer_weknora("ISO 9001 的采购要求是什么", kbs)["found"] is False
    assert standard_qa._domain_match("外观要求是什么", chunks) is True
    assert standard_qa._domain_match("今天天气怎么样", chunks) is False


def test_clause_only_question_skips_domain_guard(monkeypatch):
    record = _record()
    chunks = _chunks(record)
    kbs = [{"standard_no": "Q/TEST 123-2024", "standard_name": "测试标准", "chunks": chunks}]
    monkeypatch.setattr(weknora, "config_from_env", lambda: weknora.WeKnoraConfig("http://x", "k", "kb"))
    monkeypatch.setattr(
        weknora,
        "search",
        lambda *args, **kwargs: {
            "citations": [
                {
                    "standard_id": record["id"],
                    "standard_no": "Q/TEST 123-2024",
                    "standard_name": "测试标准",
                    "clause": "5.1",
                    "item_name": "外观检查",
                    "page": 2,
                    "kind": "requirement",
                    "kind_label": "技术要求/判定要求",
                    "quote": "外观应无缺陷。",
                    "weknora_chunk_id": "c1",
                    "weknora_knowledge_id": "k1",
                    "score": 0.8,
                    "source": "weknora",
                }
            ],
            "top_score": 0.8,
            "meta": {},
            "kb_id": "kb",
        },
    )
    result = standard_qa.answer_weknora("5.1", kbs)
    assert result["found"] is True
    assert result["citations"][0]["clause"] == "5.1"
