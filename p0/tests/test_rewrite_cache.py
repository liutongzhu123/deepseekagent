import pytest

from app import rewrite_cache, standard_qa, weknora


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl


def test_key_is_stable_and_scoped_by_model_and_question():
    cache = rewrite_cache.RewriteCache(FakeRedis())
    assert cache.key("m", "q") == cache.key("m", "q")
    assert cache.key("m", "q") != cache.key("m2", "q")
    assert cache.key("m", "q") != cache.key("m", "q2")


def test_set_uses_five_minute_ttl():
    client = FakeRedis()
    rewrite_cache.RewriteCache(client, ttl=300).set("k", "v")
    assert client.ttls["k"] == 300
    assert client.store["k"] == "v"


def test_get_decodes_bytes():
    class BytesRedis(FakeRedis):
        def get(self, key):
            return b"rewritten"

    assert rewrite_cache.RewriteCache(BytesRedis()).get("k") == "rewritten"


def test_cache_errors_degrade_to_miss():
    class Broken:
        def get(self, key):
            raise RuntimeError("redis down")

        def setex(self, *args):
            raise RuntimeError("redis down")

    cache = rewrite_cache.RewriteCache(Broken())
    assert cache.get("k") is None
    assert cache.set("k", "v") is None


def test_from_env_disabled_by_default(monkeypatch):
    monkeypatch.delenv("P0_QUERY_REWRITE_CACHE", raising=False)
    assert rewrite_cache.from_env() is None


def test_from_env_builds_configured_cache(monkeypatch):
    monkeypatch.setenv("P0_QUERY_REWRITE_CACHE", "1")
    monkeypatch.setenv("P0_REDIS_URL", "redis://127.0.0.1:6379/9")
    monkeypatch.setenv("P0_QUERY_REWRITE_CACHE_TTL", "300")
    cache = rewrite_cache.from_env()
    assert isinstance(cache, rewrite_cache.RewriteCache)
    assert cache.ttl == 300


def test_standard_answer_uses_env_cache(monkeypatch):
    sentinel = object()
    seen = {}
    monkeypatch.setattr(rewrite_cache, "from_env", lambda: sentinel)
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(
        standard_qa, "answer_weknora",
        lambda question, kbs, **kwargs: seen.setdefault("cache", kwargs.get("cache")) or {
            "found": False, "answer": "", "citations": [], "retrieval": {}})
    standard_qa.answer("5.1 的外观要求", [], mode="auto")
    assert seen["cache"] is sentinel
