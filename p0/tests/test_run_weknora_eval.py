import pytest

from scripts import run_weknora_eval as evaluator


def _dataset():
    return {
        "name": "synthetic eval-mode test",
        "kbs": [{"chunks": [{"id": "c1"}]}],
        "cases": [
            {
                "id": "q1",
                "question": "演示要求是什么？",
                "expect": "hit",
                "standard_no": "Q/DEMO 001-2026",
                "clause": "5.1",
                "tag": "direct",
            }
        ],
    }


def _answer(mode):
    return {
        "found": True,
        "answer": "合成答案",
        "citations": [{"standard_no": "Q/DEMO 001-2026", "clause": "5.1"}],
        "retrieval": {"mode": mode, "top_score": 0.8},
    }


def test_explicit_weknora_mode_requires_configuration(monkeypatch):
    # F1: absent endpoint/key/KB must fail before any dataset case is evaluated.
    monkeypatch.setattr(evaluator.weknora, "configured", lambda: False)
    monkeypatch.setattr(
        evaluator.standard_qa,
        "answer",
        lambda *args, **kwargs: pytest.fail("evaluation must stop before query"),
    )

    with pytest.raises(ValueError, match="requires a configured WeKnora"):
        evaluator.evaluate(_dataset(), mode="weknora")


def test_cli_returns_configuration_error_for_explicit_weknora_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluator.weknora, "configured", lambda: False)
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text('{"name":"synthetic","cases":[{"question":"q"}]}', encoding="utf-8")

    result = evaluator.main(["--dataset", str(dataset_path), "--mode", "weknora"])

    assert result == 2
    assert "mode=weknora requires a configured WeKnora" in capsys.readouterr().err


def test_explicit_weknora_mode_rejects_runtime_fallback(monkeypatch):
    # F2: if the client returns a keyword fallback, it cannot count as a
    # WeKnora hit even when its answer and citation otherwise look correct.
    monkeypatch.setattr(evaluator.weknora, "configured", lambda: True)
    monkeypatch.setattr(
        evaluator.standard_qa,
        "answer",
        lambda *args, **kwargs: _answer("keyword-fallback"),
    )

    report = evaluator.evaluate(_dataset(), mode="weknora")

    assert report["requested_mode"] == "weknora"
    assert report["mode"] == "keyword-fallback"
    assert report["actual_modes"] == {"keyword-fallback": 1}
    assert report["results"][0]["error"] == "unexpected_backend"
    assert report["results"][0]["ok"] is False
    assert "实际模式：`keyword-fallback`" in evaluator.markdown(report)


def test_explicit_weknora_mode_rejects_missing_backend_metadata(monkeypatch):
    # Missing telemetry must not be inferred from the requested mode.
    monkeypatch.setattr(evaluator.weknora, "configured", lambda: True)
    monkeypatch.setattr(
        evaluator.standard_qa,
        "answer",
        lambda *args, **kwargs: {
            "found": True,
            "answer": "合成答案",
            "citations": [{"standard_no": "Q/DEMO 001-2026", "clause": "5.1"}],
        },
    )

    report = evaluator.evaluate(_dataset(), mode="weknora")

    assert report["mode"] == "unknown"
    assert report["actual_modes"] == {"unknown": 1}
    assert report["results"][0]["error"] == "unexpected_backend"
    assert report["results"][0]["ok"] is False


def test_auto_mode_reports_the_backend_actually_used(monkeypatch):
    # Auto mode permits the fallback, but the report must name it accurately.
    monkeypatch.setattr(evaluator.weknora, "configured", lambda: True)
    monkeypatch.setattr(
        evaluator.standard_qa,
        "answer",
        lambda *args, **kwargs: _answer("keyword-fallback"),
    )

    report = evaluator.evaluate(_dataset(), mode="auto")

    assert report["requested_mode"] == "auto"
    assert report["mode"] == "keyword-fallback"
    assert report["actual_modes"] == {"keyword-fallback": 1}
    assert report["results"][0]["ok"] is True
