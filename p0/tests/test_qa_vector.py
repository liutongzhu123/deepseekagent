"""Vector-retrieval unit tests with a deterministic fake embedder.

These tests do not call bge-m3: they pin the retrieval guards, kind selection
and score gate.  The real bge-m3 before/after run is produced by
``scripts/run_kb_vector_demo.py``.
"""
from __future__ import annotations

import pytest

from app import standard_qa
from app.embeddings import EmbeddingUnavailable, VectorIndex

CHUNKS = [
    {
        "id": "std_test:5.1.1:req",
        "standard_id": "std_test",
        "standard_no": "Q/TEST 123-2024",
        "standard_name": "测试标准",
        "item_name": "外观检查",
        "clause": "5.1.1",
        "kind": "requirement",
        "kind_label": "技术要求/判定要求",
        "page": 8,
        "text": "外观应无裂纹、划痕等缺陷。",
    },
    {
        "id": "std_test:5.1.1:method",
        "standard_id": "std_test",
        "standard_no": "Q/TEST 123-2024",
        "standard_name": "测试标准",
        "item_name": "外观检查",
        "clause": "5.1.1",
        "kind": "method",
        "kind_label": "试验方法",
        "page": 12,
        "text": "目测检查，正常光照下进行。",
    },
    {
        "id": "std_test:6.3:req",
        "standard_id": "std_test",
        "standard_no": "Q/TEST 123-2024",
        "standard_name": "测试标准",
        "item_name": "密度",
        "clause": "6.3",
        "kind": "requirement",
        "kind_label": "技术要求/判定要求",
        "page": 10,
        "text": "密度为 0.92±0.02。",
    },
    {
        "id": "std_test:6.3:method",
        "standard_id": "std_test",
        "standard_no": "Q/TEST 123-2024",
        "standard_name": "测试标准",
        "item_name": "密度",
        "clause": "6.3",
        "kind": "method",
        "kind_label": "试验方法",
        "page": 20,
        "text": "按 GB/T 1033.1 中规定的方法 A 进行试验。",
    },
]


def make_kbs() -> list[dict]:
    return [
        {
            "standard_id": "std_test",
            "standard_no": "Q/TEST 123-2024",
            "standard_name": "测试标准",
            "chunks": CHUNKS,
        }
    ]


class FakeEmbedder:
    model = "fake-bge"

    def embed(self, texts):
        vectors = []
        for text in texts:
            if "密度" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "外观" in text or "表面" in text or "缺陷" in text:
                vectors.append([0.0, 1.0, 0.0])
            elif "气味" in text:
                vectors.append([0.0, 0.0, 1.0])
            else:
                vectors.append([0.2, 0.2, 0.2])
        return vectors


class FailingEmbedder:
    model = "offline"

    def embed(self, texts):
        raise EmbeddingUnavailable("service down")


def test_vector_finds_semantic_match_and_cites_clause(tmp_path):
    result = standard_qa.answer_vector(
        "零件表面不能有什么缺陷？",
        make_kbs(),
        FakeEmbedder(),
        cache_path=tmp_path / "vectors.json",
        min_score=0.9,
    )
    assert result["found"] is True
    assert result["retrieval"]["mode"] == "vector"
    assert result["citations"][0]["clause"] == "5.1.1"
    assert "外观" in result["answer"]


def test_vector_exact_clause_picks_method_for_method_question(tmp_path):
    result = standard_qa.answer_vector(
        "6.3 的试验方法是什么？",
        make_kbs(),
        FakeEmbedder(),
        cache_path=tmp_path / "vectors.json",
        min_score=0.9,
    )
    assert result["found"] is True
    assert [citation["kind"] for citation in result["citations"]] == ["method"]
    assert result["citations"][0]["page"] == 20


def test_vector_refuses_out_of_library_standard(tmp_path):
    result = standard_qa.answer_vector(
        "Q/SQR SS8-46-2021 中 5.1.1 的外观要求是什么？",
        make_kbs(),
        FakeEmbedder(),
        cache_path=tmp_path / "vectors.json",
        min_score=0.9,
    )
    assert result["found"] is False
    assert "没有标准" in result["answer"]


def test_vector_refuses_missing_clause(tmp_path):
    result = standard_qa.answer_vector(
        "5.9.9 的外观要求是什么？",
        make_kbs(),
        FakeEmbedder(),
        cache_path=tmp_path / "vectors.json",
        min_score=0.9,
    )
    assert result["found"] is False
    assert "没有条款" in result["answer"]


def test_vector_score_gate_refuses_unrelated_question(tmp_path):
    result = standard_qa.answer_vector(
        "电池包的针刺试验要求是什么？",
        make_kbs(),
        FakeEmbedder(),
        cache_path=tmp_path / "vectors.json",
        min_score=0.9,
    )
    assert result["found"] is False
    assert "低于阈值" in result["answer"]
    assert result["retrieval"]["top_score"] < 0.9


def test_vector_cache_roundtrip(tmp_path):
    cache = tmp_path / "vectors.json"
    first = VectorIndex.from_chunks(CHUNKS, FakeEmbedder(), cache_path=cache)
    assert cache.exists()
    second = VectorIndex.from_chunks(CHUNKS, FakeEmbedder(), cache_path=cache)
    assert second.dim == first.dim == 3
    assert [chunk["id"] for chunk in second.chunks] == [chunk["id"] for chunk in CHUNKS]


def test_auto_mode_falls_back_to_keyword_when_embedding_is_down():
    result = standard_qa.answer(
        "5.1.1 的外观要求是什么？",
        make_kbs(),
        mode="auto",
        embedder=FailingEmbedder(),
    )
    assert result["found"] is True
    assert result["retrieval"]["mode"] == "keyword-fallback"
    assert "service down" in result["retrieval"]["fallback_reason"]


def test_explicit_vector_mode_raises_when_embedding_is_down():
    with pytest.raises(standard_qa.VectorUnavailable):
        standard_qa.answer(
            "5.1.1 的外观要求是什么？",
            make_kbs(),
            mode="vector",
            embedder=FailingEmbedder(),
        )
