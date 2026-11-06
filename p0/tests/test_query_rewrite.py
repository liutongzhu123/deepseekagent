import pytest

from app import query_rewrite, standard_qa, weknora


def _cfg(**kwargs):
    return query_rewrite.RewriteConfig(**{"enabled": True, "api_key": "sk-test",
                                          "model": "deepseek-chat", **kwargs})


def test_disabled_keeps_original_without_calling_model(monkeypatch):
    monkeypatch.delenv("P0_QUERY_REWRITE", raising=False)
    result = query_rewrite.rewrite("这个要求是多少", transport=lambda *_args: pytest.fail("call"))
    assert (result.query, result.applied, result.reason) == ("这个要求是多少", False, "disabled")

def test_rewrite_uses_non_reasoning_model():
    seen = []
    result = query_rewrite.rewrite(
        "这个要求是多少", config=_cfg(),
        transport=lambda q, _c, model: seen.append((q, model)) or "5.1 的外观要求")
    assert seen == [("这个要求是多少", "deepseek-chat")]
    assert (result.query, result.applied, result.reason) == ("5.1 的外观要求", True, "rewritten")

def test_reasoning_model_is_replaced_and_reported():
    seen = []
    result = query_rewrite.rewrite(
        "外观要求", config=_cfg(model="deepseek-reasoner"),
        transport=lambda _q, _c, model: seen.append(model) or "外观要求")
    assert seen == ["deepseek-chat"]
    assert (result.model, result.reason) == ("deepseek-chat", "reasoning_model_replaced")


@pytest.mark.parametrize("config, transport, expected", [
    (_cfg(), lambda *_: (_ for _ in ()).throw(query_rewrite.RewriteError("down")), "error"),
    (_cfg(), lambda *_: "   ", "empty_output"),
    (_cfg(api_key=""), lambda *_: pytest.fail("call"), "no_api_key"),
])
def test_fallbacks_keep_the_original_query(config, transport, expected):
    result = query_rewrite.rewrite("外观要求", config=config, transport=transport)
    assert (result.query, result.applied) == ("外观要求", False)
    assert result.reason.startswith(expected)

def test_clean_output_and_reasoning_detection():
    assert query_rewrite.clean_output('改写： “5.1 的外观要求” ') == "5.1 的外观要求"
    assert query_rewrite.clean_output("5.1\n外观要求") == "5.1 外观要求"
    assert query_rewrite.clean_output("x" * 700) == "x" * 500
    assert query_rewrite.is_reasoning_model("o3-mini")
    assert not query_rewrite.is_reasoning_model("deepseek-chat")


def test_cache_hit_skips_model_and_broken_cache_degrades():
    store, calls = {}, []

    class Cache:
        def key(self, model, question):
            return f"{model}:{question}"

        def get(self, key):
            return store.get(key)

        def set(self, key, value):
            store[key] = value

    def transport(*_args):
        calls.append(1)
        return "5.1 的外观要求"

    first = query_rewrite.rewrite("这个要求", config=_cfg(), cache=Cache(), transport=transport)
    second = query_rewrite.rewrite("这个要求", config=_cfg(), cache=Cache(),
                                   transport=lambda *_: pytest.fail("cache miss"))
    assert (len(calls), first.cached, second.cached, second.reason) == (1, False, True, "cache_hit")

    class Broken:
        def key(self, model, question):
            raise RuntimeError("redis down")

    result = query_rewrite.rewrite("外观要求", config=_cfg(), cache=Broken(),
                                   transport=lambda *_: "外观检查要求")
    assert (result.query, result.applied) == ("外观检查要求", True)

def _kbs():
    chunk = {"id": "chunk-1", "standard_id": "std_1", "standard_no": "Q/TEST 123-2024",
             "standard_name": "测试标准", "item_name": "外观检查", "clause": "5.1",
             "kind": "requirement", "kind_label": "技术要求/判定要求", "page": 2,
             "text": "外观应无缺陷。"}
    return [{"standard_no": "Q/TEST 123-2024", "standard_name": "测试标准", "chunks": [chunk]}]


def test_answer_weknora_rewrites_before_search(monkeypatch):
    captured = {}

    def fake_search(question, *_args, **_kwargs):
        captured["question"] = question
        return {"citations": [{**_kbs()[0]["chunks"][0], "quote": "外观应无缺陷。",
                               "weknora_chunk_id": "c1", "score": 0.8, "source": "weknora"}],
                "top_score": 0.8, "meta": {}, "kb_id": "kb"}

    monkeypatch.setattr(
        query_rewrite, "rewrite",
        lambda question, cache=None: query_rewrite.RewriteResult(
            question, "5.1 外观检查的技术要求", True, model="deepseek-chat", reason="rewritten"))
    monkeypatch.setattr(weknora, "search", fake_search)
    monkeypatch.setattr(weknora, "config_from_env",
                        lambda: weknora.WeKnoraConfig("http://x", "k", "kb"))
    result = standard_qa.answer_weknora("5.1 的外观要求是什么", _kbs())
    assert captured["question"] == "5.1 外观检查的技术要求"
    assert result["found"] is True
    assert result["retrieval"]["query_rewrite"]["applied"] is True
