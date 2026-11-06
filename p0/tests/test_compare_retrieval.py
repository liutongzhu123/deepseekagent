import json

from scripts import compare_retrieval


def _dataset():
    return {
        "cases": [
            {"id": "q1", "question": "5.1", "expect": "hit", "standard_no": "Q/T 1", "clause": "5.1"},
            {"id": "q2", "question": "5.2", "expect": "hit", "standard_no": "Q/T 1", "clause": "5.2"},
            {"id": "q3", "question": "无关问题", "expect": "refuse"},
        ]
    }


def test_evaluate_compares_rerank_quality(monkeypatch):
    def fake_answer(question, _kbs, *, mode, rerank=None):
        if question == "无关问题":
            found = not rerank
            return {"found": found, "citations": [{"standard_no": "Q/T 1", "clause": "5.9"}] if found else []}
        clause = "5.1" if question == "5.1" else "5.2"
        wrong = not rerank and question == "5.2"
        return {
            "found": True,
            "citations": [{"standard_no": "Q/T 1", "clause": "5.9" if wrong else clause}],
        }

    monkeypatch.setattr(compare_retrieval.standard_qa, "answer", fake_answer)
    hybrid = compare_retrieval.evaluate(_dataset(), rerank=False)
    rerank = compare_retrieval.evaluate(_dataset(), rerank=True)

    assert (hybrid["hit"]["ok"], hybrid["citation_ok"], hybrid["refusal"]["ok"]) == (1, 1, 0)
    assert (rerank["hit"]["ok"], rerank["citation_ok"], rerank["refusal"]["ok"]) == (2, 2, 1)
    text = compare_retrieval.markdown(hybrid, rerank)
    assert "hybrid-search" in text and "knowledge-search" in text
    assert "50.0%" in text and "100.0%" in text


def test_main_writes_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(
        compare_retrieval, "evaluate",
        lambda dataset, rerank, from_store=False, repeat=1: {
            "rerank": rerank, "kb_count": 0, "cases": 1, "runs": 1,
            "hit": {"total": 1, "ok": 1}, "refusal": {"total": 0, "ok": 0},
            "citation_ok": 1, "latency_ms": {"p50": 1.0, "p95": 1.0, "avg": 1.0},
            "results": [],
        },
    )
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps({"cases": [{"question": "q", "expect": "hit"}]}))
    report = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    assert compare_retrieval.main([
        "--dataset", str(dataset), "--report", str(report), "--markdown", str(markdown),
    ]) == 0
    assert "hybrid" in json.loads(report.read_text())
    assert "WeKnora 检索命中质量对比" in markdown.read_text()
