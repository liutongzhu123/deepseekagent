import json

import pytest

from scripts import bench_p1_latency


def test_load_questions_accepts_a_list_and_cases(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps(["5.1 要求是什么？", {"id": "q2", "question": "外观怎么做？"}]))
    cases = bench_p1_latency.load_questions(path)
    assert cases == [
        {"id": "q01", "question": "5.1 要求是什么？"},
        {"id": "q2", "question": "外观怎么做？"},
    ]


def test_run_aggregates_latency_and_records_flags(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"cases": [{"question": "5.1"}, {"question": "5.2"}]}))
    monkeypatch.setattr(bench_p1_latency, "_snapshot", lambda: {"query_rewrite": True})
    monkeypatch.setattr(
        bench_p1_latency.standard_qa, "answer",
        lambda question, kbs, mode: {
            "found": True,
            "retrieval": {"mode": mode, "query_rewrite": {"applied": True, "cached": False}},
        },
    )
    report = bench_p1_latency.run(path, repeat=2, label="after")
    assert report["total"] == 4
    assert report["found_rate"] == 1.0
    assert report["config"] == {"query_rewrite": True}
    assert report["latency_ms"]["p95"] >= report["latency_ms"]["p50"] >= 0


def test_percentile_handles_empty_and_ordered_values():
    assert bench_p1_latency._percentile([], 95) == 0.0
    assert bench_p1_latency._percentile([10, 20, 30], 50) == 20
    assert bench_p1_latency._percentile([10, 20, 30], 95) == 30


def test_compare_renders_delta_and_markdown():
    before = {"latency_ms": {"p50": 1000, "p95": 2000, "avg": 1500}, "found_rate": 1.0}
    after = {"latency_ms": {"p50": 800, "p95": 1600, "avg": 1200}, "found_rate": 1.0}
    text = bench_p1_latency.compare(before, after)
    assert "P95 首结果延迟 (ms) | 2000 | 1600 | -400.0" in text
    assert "### " not in text


def test_run_requires_questions(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        bench_p1_latency.load_questions(path)
