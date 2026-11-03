"""Standard governance: revision snapshots, knowledge-base unpublish and rollback."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, standard_store, weknora


def parsed_standard() -> dict:
    return {
        "meta": {"standard_no": "Q/TEST 002-2026", "name": "治理演示标准", "pages": 2},
        "summary": {"items": 2},
        "items": [
            {
                "clause": "5.1",
                "name": "外观",
                "requirement": "外观无缺陷",
                "method_clause": "6.1",
                "method": "目测",
                "page": 1,
                "method_page": 2,
            },
            {
                "clause": "5.2",
                "name": "尺寸",
                "requirement": "符合图纸要求",
                "method_clause": "6.2",
                "method": "卡尺",
                "page": 1,
                "method_page": 2,
            },
        ],
        "warnings": [],
    }


@pytest.fixture()
def std_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "standards"
    monkeypatch.setattr(standard_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(standard_store, "INDEX_PATH", data_dir / "kb_index.json")
    monkeypatch.setattr(weknora, "configured", lambda: False)
    monkeypatch.setattr(weknora, "required", lambda: False)
    client = TestClient(main.app)
    return SimpleNamespace(client=client, data_dir=data_dir)


def _create(env) -> dict:
    return standard_store.create(
        parsed_standard(), b"%PDF-1.4 demo", "demo.pdf", project="P1", created_by="alice"
    )


def _review(standard_id: str, value: str, reviewer: str = "复核人") -> dict:
    return standard_store.update_review(
        standard_id,
        [{"clause": "5.1", "name": "外观", "requirement": value, "method": "目测"}],
        reviewer=reviewer,
    )


def test_review_writes_revision_snapshots(std_env):
    record = _create(std_env)
    standard_id = record["id"]
    assert (std_env.data_dir / standard_id / "versions" / "0.json").exists()

    _review(standard_id, "外观无缺陷（修订）")
    _review(standard_id, "外观无缺陷（二次修订）")
    current = standard_store.load(standard_id)
    assert current["revision"] == 2
    assert (std_env.data_dir / standard_id / "versions" / "2.json").exists()

    versions = standard_store.list_versions(standard_id)
    assert [item["version"] for item in versions] == [2, 1, 0]
    assert versions[0]["current"] is True
    assert versions[0]["rollbackable"] is False
    assert versions[-1]["rollbackable"] is True


def test_unpublish_removes_kb_and_allows_republish(std_env):
    record = _create(std_env)
    standard_id = record["id"]
    standard_store.publish(standard_id)
    published = standard_store.load(standard_id)
    assert published["published"] is True
    assert standard_store.published_kbs(standard_id)

    result = standard_store.unpublish(standard_id, "复核人")
    assert result["published"] is False
    assert result["removed_knowledge_ids"] == []
    unpublished = standard_store.load(standard_id)
    assert unpublished["published"] is False
    assert unpublished["published_at"] == ""
    assert unpublished["unpublished_at"]
    assert standard_store.published_kbs(standard_id) == []
    assert not (std_env.data_dir / standard_id / "kb.json").exists()

    standard_store.publish(standard_id)
    assert standard_store.load(standard_id)["published"] is True


def test_unpublish_deletes_weknora_knowledge(monkeypatch, std_env):
    record = _create(std_env)
    standard_id = record["id"]
    directory = std_env.data_dir / standard_id
    (directory / "kb.json").write_text(
        json.dumps(
            {
                "standard_id": standard_id,
                "weknora": {"knowledge_id": "kw_demo_1"},
                "chunks": [{"clause": "5.1"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(weknora, "delete_knowledge", lambda knowledge_id, cfg=None: calls.append(knowledge_id) or {})
    result = standard_store.unpublish(standard_id, "复核人")
    assert calls == ["kw_demo_1"]
    assert result["removed_knowledge_ids"] == ["kw_demo_1"]
    assert standard_store.published_kbs(standard_id) == []


def test_rollback_restores_earlier_items_as_new_revision(std_env):
    record = _create(std_env)
    standard_id = record["id"]
    original = standard_store.load(standard_id)["items"][0]["requirement"]
    _review(standard_id, "回滚前的新要求")
    assert standard_store.load(standard_id)["items"][0]["requirement"] == "回滚前的新要求"

    restored = standard_store.rollback(standard_id, 0, "复核人")
    assert restored["revision"] == 2
    assert restored["items"][0]["requirement"] == original
    assert restored["review_history"][-1]["action"] == "rollback"
    assert restored["review_history"][-1]["from_version"] == 0
    assert restored["published"] is False
    assert (std_env.data_dir / standard_id / "versions" / "2.json").exists()
    with pytest.raises(ValueError):
        standard_store.rollback(standard_id, 2, "复核人")


def test_versions_unpublish_and_rollback_api(std_env):
    record = _create(std_env)
    standard_id = record["id"]
    _review(standard_id, "回滚前的新要求")

    versions = std_env.client.get(f"/api/v1/standards/{standard_id}/versions")
    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()["versions"]] == [1, 0]

    current = std_env.client.post(
        f"/api/v1/standards/{standard_id}/rollback", json={"version": 1}
    )
    assert current.status_code == 409

    rolled = std_env.client.post(
        f"/api/v1/standards/{standard_id}/rollback", json={"version": 0}
    )
    assert rolled.status_code == 200
    assert rolled.json()["revision"] == 2
    assert rolled.json()["published"] is False

    standard_store.publish(standard_id)
    unpublished = std_env.client.post(f"/api/v1/standards/{standard_id}/unpublish")
    assert unpublished.status_code == 200
    assert unpublished.json()["published"] is False
    again = std_env.client.post(f"/api/v1/standards/{standard_id}/unpublish")
    assert again.status_code == 200

    missing = std_env.client.get("/api/v1/standards/std_20260101000000_abcdef/versions")
    assert missing.status_code == 404
