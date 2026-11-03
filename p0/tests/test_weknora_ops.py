import json
from pathlib import Path

import httpx
import pytest

from app import weknora
from app import standard_qa


def _record():
    return {
        "id": "std_20260101000000_abcdef",
        "revision": 3,
        "meta": {"standard_no": "Q/TEST 123-2024", "name": "测试标准", "project": "P1"},
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


def _install(monkeypatch, handler):
    cfg = weknora.WeKnoraConfig(base_url="http://weknora.test", api_key="sk-test", kb_id="kb-1")

    def factory(_cfg):
        return httpx.Client(
            base_url=cfg.base_url,
            transport=httpx.MockTransport(handler),
            headers={"X-API-Key": cfg.api_key},
        )

    monkeypatch.setattr(weknora, "config_from_env", lambda: cfg)
    monkeypatch.setattr(weknora, "_client", factory)
    monkeypatch.setattr(weknora, "INDEX_WAIT_SECONDS", 0.1)
    return cfg


def test_publish_reuses_existing_title_and_sets_metadata(monkeypatch):
    calls = {"post": 0, "manual_put": 0, "metadata_put": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/knowledge-bases/kb-1/knowledge":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "kn-1",
                            "title": "P0 | Q/TEST 123-2024 | 测试标准",
                            "parse_status": "completed",
                            "updated_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 100,
                },
            )
        if path == "/api/v1/knowledge/manual/kn-1":
            calls["manual_put"] += 1
            assert json.loads(request.content)["status"] == "publish"
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-1"}})
        if path == "/api/v1/knowledge/kn-1" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": {"parse_status": "completed"}})
        if path == "/api/v1/knowledge/kn-1" and request.method == "PUT":
            calls["metadata_put"] += 1
            payload = json.loads(request.content)
            assert payload["custom_metadata"]["p0_standard_id"] == "std_20260101000000_abcdef"
            assert payload["custom_metadata"]["p0_revision"] == 3
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-1"}})
        if path.endswith("/knowledge/manual"):
            calls["post"] += 1
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-new"}})
        raise AssertionError(request.method + " " + path)

    _install(monkeypatch, handler)
    record = _record()
    result = weknora.publish_standard(record, _chunks(record))
    assert result["knowledge_id"] == "kn-1"
    assert result["updated"] is True
    assert result["deduped"] is True
    assert result["metadata_updated"] is True
    assert calls == {"post": 0, "manual_put": 1, "metadata_put": 1}


def test_publish_creates_when_no_existing_title(monkeypatch):
    calls = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/knowledge-bases/kb-1/knowledge":
            return httpx.Response(200, json={"success": True, "data": [], "total": 0, "page": 1, "page_size": 100})
        if path == "/api/v1/knowledge-bases/kb-1/knowledge/manual":
            calls["post"] += 1
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-new"}})
        if path == "/api/v1/knowledge/kn-new" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": {"parse_status": "completed"}})
        if path == "/api/v1/knowledge/kn-new" and request.method == "PUT":
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-new"}})
        raise AssertionError(request.method + " " + path)

    _install(monkeypatch, handler)
    record = _record()
    result = weknora.publish_standard(record, _chunks(record))
    assert result["knowledge_id"] == "kn-new"
    assert result["deduped"] is False
    assert calls["post"] == 1


def test_ops_cleanup_plan_and_apply(monkeypatch):
    from scripts import weknora_ops

    status = {
        "kb": {"id": "kb-1", "name": "KB", "summary_model_id": "", "embedding_model_id": ""},
        "models": [],
        "knowledge": {
            "total": 3,
            "by_parse_status": {"completed": 2, "failed": 1},
            "duplicates": {
                "P0 | Q/TEST | 测试标准": [
                    {"id": "kn-new", "parse_status": "completed", "updated_at": "2026-01-02"},
                    {"id": "kn-old", "parse_status": "completed", "updated_at": "2026-01-01"},
                ]
            },
            "failed": [{"id": "kn-failed", "title": "P0 | broken", "parse_status": "failed"}],
            "pending": [],
        },
        "warnings": [],
    }
    monkeypatch.setattr(weknora_ops, "collect_status", lambda: status)
    monkeypatch.setattr(weknora_ops.weknora, "delete_knowledge", lambda knowledge_id: {"ok": True})
    plan = weknora_ops.cleanup(apply=False, delete_failed=True)
    assert {item["id"] for item in plan["plan"]} == {"kn-old", "kn-failed"}
    assert plan["deleted"] == []
    applied = weknora_ops.cleanup(apply=True, delete_failed=True)
    assert {item["id"] for item in applied["deleted"]} == {"kn-old", "kn-failed"}


def test_demo_eval_reproduces_refusal_and_hit_metrics():
    from scripts.run_weknora_eval import evaluate, markdown

    dataset = json.loads(
        (Path(__file__).resolve().parents[1] / "eval" / "weknora_qa.demo.json").read_text(encoding="utf-8")
    )
    report = evaluate(dataset, mode="keyword")
    assert report["total"] == len(dataset["cases"])
    assert report["refusal"]["accuracy"] == 1.0
    assert report["hit"]["ok"] >= 1
    assert report["citation_accuracy"] >= 0.0
    assert "库内命中" in markdown(report)


def test_keyword_refuses_unknown_standard_with_known_clause():
    dataset = json.loads(
        (Path(__file__).resolve().parents[1] / "eval" / "weknora_qa.demo.json").read_text(encoding="utf-8")
    )
    answer = standard_qa.answer_keyword("Q/DEMO 999-2026 的 5.1 是什么？", dataset["kbs"])
    assert answer["found"] is False
