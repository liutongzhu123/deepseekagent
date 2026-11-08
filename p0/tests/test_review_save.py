"""SP-2.2: 检测项目人工复核确认后入库，未确认不入库.

Failure list (each covered below):
F1 未复核条目随发布进入知识库（本地 kb.json 或 WeKnora）
F2 只确认部分条目时发布成功 → 知识库静默缺条款
F3 复核修改/确认未落盘，重新加载后丢失
F4 取消确认后仍可发布
F5 已确认但条目没有技术要求/试验方法时发布空知识库
F6 异步发布先入队、后在 worker 里失败，用户看不到明确的 409
F7 重建索引绕过复核门禁，把未确认条目写回知识库
F8 已发布后取消确认，published/kb 未撤销，ask 仍引用未确认条款
F9 自动下架未删除 WeKnora 远端条目
F10 远端下架失败仍提交取消，本地/远端状态撕裂
F11 回滚只改 published 标志，kb/WeKnora 仍可被 ask 引用
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, standard_qa, standard_store, weknora


def _parsed() -> dict:
    items = [
        {"clause": "5.1", "name": "外观", "requirement": "外观无缺陷", "method_clause": "6.1",
         "method": "目测", "page": 1, "method_page": 2, "confidence": 0.6,
         "review_required": True, "reviewed": False,
         "review_fields": [{"field": "requirement", "confidence": 0.6, "reason": "OCR 置信度低于 0.90"}]},
        {"clause": "5.2", "name": "尺寸", "requirement": "符合图纸", "method_clause": "6.2",
         "method": "卡尺", "page": 1, "method_page": 2, "confidence": 0.9,
         "review_required": False, "reviewed": False, "review_fields": []},
    ]
    return {"meta": {"standard_no": "Q/TEST 220-2026", "name": "人工复核入库演示", "pages": 2},
            "summary": {"items": 2}, "items": items}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "standards"
    monkeypatch.setattr(standard_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(standard_store, "INDEX_PATH", data_dir / "kb_index.json")
    monkeypatch.setattr(weknora, "configured", lambda: False)
    monkeypatch.setattr(weknora, "required", lambda: False)
    record = standard_store.create(_parsed(), b"%PDF", "demo.pdf", project="P1")
    return SimpleNamespace(client=TestClient(main.app), data_dir=data_dir,
                           record=record, standard_id=record["id"])


def _confirm(standard_id: str, *clauses: str, reviewer: str = "复核人") -> dict:
    return standard_store.update_review(
        standard_id, [{"clause": clause, "reviewed": True} for clause in clauses], reviewer=reviewer)


def _chunk_clauses(standard_id: str) -> set[str]:
    kbs = standard_store.published_kbs(standard_id)
    assert kbs, "发布后应存在本地 kb 副本"
    return {chunk["clause"] for chunk in kbs[0]["chunks"]}


def test_review_edit_is_persisted_before_publish(env):
    """F3 / 验收「解析结果可人工修改」：修改写入记录并在重载后保持。"""
    updated = standard_store.update_review(
        env.standard_id, [{"clause": "5.1", "name": "外观（人工修正）",
                           "requirement": "外观无缺陷，无毛刺"}], reviewer="张复核")
    assert updated["revision"] == 1
    item = next(item for item in updated["items"] if item["clause"] == "5.1")
    assert item["requirement"] == "外观无缺陷，无毛刺"
    assert item["reviewed"] is True and item["reviewed_by"] == "张复核"
    assert any(entry["field"] == "requirement" for entry in updated["review_history"])
    saved = next(item for item in standard_store.load(env.standard_id)["items"]
                 if item["clause"] == "5.1")
    assert saved["requirement"] == "外观无缺陷，无毛刺" and saved["reviewed"] is True


def test_publish_rejects_unconfirmed_standard(env):
    """F1 / 验收「未确认不入库」：全部未确认时发布被拒，且不产生 kb。"""
    with pytest.raises(ValueError) as excinfo:
        standard_store.publish(env.standard_id)
    assert "2 个检测项目未人工复核确认" in str(excinfo.value)
    assert "未确认不入库" in str(excinfo.value)
    assert standard_store.published_kbs(env.standard_id) == []
    assert standard_store.load(env.standard_id)["published"] is False
    assert not (env.data_dir / env.standard_id / "kb.json").exists()


def test_publish_rejects_partially_confirmed_standard(env, monkeypatch):
    """F2 / 「未确认不入库」：部分确认也不得发布，避免知识库静默缺条款。"""
    _confirm(env.standard_id, "5.1")
    captured: list[list[dict]] = []
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(weknora, "publish_standard",
                        lambda record, chunks, cfg=None: captured.append(chunks) or {"knowledge_id": "kw_1"})
    with pytest.raises(ValueError) as excinfo:
        standard_store.publish(env.standard_id)
    assert "1 个检测项目" in str(excinfo.value)
    assert captured == []  # 未确认条目不得到达 WeKnora
    assert standard_store.published_kbs(env.standard_id) == []


def test_publish_after_all_confirmed_writes_every_clause(env, monkeypatch):
    """验收「确认后写入标准库」：全部确认后本地与 WeKnora 都拿到全部条款。"""
    captured: list[list[dict]] = []
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(weknora, "publish_standard",
                        lambda record, chunks, cfg=None: captured.append(chunks) or {"knowledge_id": "kw_1"})
    _confirm(env.standard_id, "5.1", "5.2")
    result = standard_store.publish(env.standard_id)
    assert result["published"] is True and result["items"] == 2 and result["chunks"] == 4
    assert _chunk_clauses(env.standard_id) == {"5.1", "5.2"}
    assert len(captured) == 1 and {chunk["clause"] for chunk in captured[0]} == {"5.1", "5.2"}
    assert standard_store.load(env.standard_id)["published"] is True


def test_cancelled_confirmation_auto_unpublishes(env):
    """F4/F8: 已发布后取消确认立即下架，ask 不再引用未确认条款。"""
    _confirm(env.standard_id, "5.1", "5.2")
    standard_store.publish(env.standard_id)
    standard_store.update_review(env.standard_id, [{"clause": "5.1", "reviewed": False}],
                                 reviewer="复核人")
    record = standard_store.load(env.standard_id)
    assert record["published"] is False and record["published_at"] == ""
    assert record["unpublished_at"]
    assert record["review_history"][-1]["action"] == "auto_unpublish"
    assert standard_store.published_kbs(env.standard_id) == []
    assert not (env.data_dir / env.standard_id / "kb.json").exists()
    answer = standard_qa.answer("外观要求是什么？", standard_store.published_kbs(env.standard_id))
    assert answer["found"] is False and answer["citations"] == []
    with pytest.raises(ValueError) as excinfo:
        standard_store.publish(env.standard_id)
    assert "5.1" in str(excinfo.value)


def test_cancel_deletes_weknora_entry(env, monkeypatch):
    """F9: 自动下架同时删除 WeKnora 远端条目，远端也不再引用。"""
    deleted: list[str] = []
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(weknora, "publish_standard",
                        lambda record, chunks, cfg=None: {"knowledge_id": "kw_1"})
    monkeypatch.setattr(weknora, "delete_knowledge",
                        lambda knowledge_id, cfg=None: deleted.append(knowledge_id) or {})
    _confirm(env.standard_id, "5.1", "5.2")
    standard_store.publish(env.standard_id)
    standard_store.update_review(env.standard_id, [{"clause": "5.2", "reviewed": False}],
                                 reviewer="复核人")
    assert deleted == ["kw_1"]
    assert standard_store.published_kbs(env.standard_id) == []


def test_cancel_fails_closed_when_weknora_down(env, monkeypatch):
    """F10: 远端下架失败时整次取消失效，本地仍保持已发布/已确认。"""
    monkeypatch.setattr(weknora, "configured", lambda: True)
    monkeypatch.setattr(weknora, "required", lambda: True)
    monkeypatch.setattr(weknora, "publish_standard",
                        lambda record, chunks, cfg=None: {"knowledge_id": "kw_1"})

    def _boom(knowledge_id, cfg=None):
        raise weknora.WeKnoraError("down")

    monkeypatch.setattr(weknora, "delete_knowledge", _boom)
    _confirm(env.standard_id, "5.1", "5.2")
    standard_store.publish(env.standard_id)
    response = env.client.patch(f"/api/v1/standards/{env.standard_id}", json={
        "reviewer": "复核人", "items": [{"clause": "5.1", "reviewed": False}]})
    assert response.status_code == 502 and "WeKnora 下架失败" in response.json()["detail"]
    record = standard_store.load(env.standard_id)
    assert record["published"] is True and record["published_at"]
    assert next(i for i in record["items"] if i["clause"] == "5.1")["reviewed"] is True
    assert standard_store.published_kbs(env.standard_id) != []


def test_rollback_removes_published_kb(env):
    """F11: 回滚真正下架，不留下 published=False 但仍可被 ask 引用的 kb。"""
    _confirm(env.standard_id, "5.1", "5.2")
    standard_store.publish(env.standard_id)
    standard_store.update_review(env.standard_id, [{"clause": "5.1", "requirement": "新要求"}],
                                 reviewer="复核人")
    standard_store.publish(env.standard_id)
    rolled = standard_store.rollback(env.standard_id, 0, "复核人")
    assert rolled["published"] is False and rolled["published_at"] == ""
    assert rolled["review_history"][-1]["action"] == "rollback"
    assert standard_store.published_kbs(env.standard_id) == []
    assert not (env.data_dir / env.standard_id / "kb.json").exists()


def test_confirmed_item_without_content_cannot_publish(env):
    """F5: 条目已确认但没有技术要求/试验方法时不得写入空知识库。"""
    standard_store.update_review(
        env.standard_id, [{"clause": "5.1", "requirement": "", "method": ""},
                          {"clause": "5.2", "requirement": "", "method": ""}], reviewer="复核人")
    with pytest.raises(ValueError) as excinfo:
        standard_store.publish(env.standard_id)
    assert "没有可发布的解析条目" in str(excinfo.value)
    assert standard_store.published_kbs(env.standard_id) == []


def test_publish_api_returns_409_until_all_confirmed(env):
    """F6 / 验收闭环：接口层在入队/发布前明确拒绝未确认标准。"""
    blocked = env.client.post(f"/api/v1/standards/{env.standard_id}/publish")
    assert blocked.status_code == 409 and "未确认不入库" in blocked.json()["detail"]
    reviewed = env.client.patch(f"/api/v1/standards/{env.standard_id}", json={
        "reviewer": "接口复核",
        "items": [{"clause": "5.1", "reviewed": True}, {"clause": "5.2", "reviewed": True}]})
    assert reviewed.status_code == 200
    published = env.client.post(f"/api/v1/standards/{env.standard_id}/publish")
    assert published.status_code == 200
    assert published.json()["chunks"] == 4 and published.json()["items"] == 2
    assert _chunk_clauses(env.standard_id) == {"5.1", "5.2"}


def test_publish_api_blocks_before_async_queue(env, monkeypatch):
    """F6: 异步开关下也在入队前返回 409，不排入必然失败的发布任务。"""
    monkeypatch.setattr(main, "_task_switch", lambda name, default="1": True)
    blocked = env.client.post(f"/api/v1/standards/{env.standard_id}/publish")
    assert blocked.status_code == 409 and "未确认不入库" in blocked.json()["detail"]
    assert standard_store.published_kbs(env.standard_id) == []


def test_rebuild_index_skips_unconfirmed_standards(env):
    """F7: 重建索引同样受复核门禁约束。"""
    skipped = standard_store.rebuild_index()
    assert skipped["chunk_count"] == 0
    assert standard_store.load(env.standard_id)["published"] is False
    assert not (env.data_dir / env.standard_id / "kb.json").exists()
    _confirm(env.standard_id, "5.1", "5.2")
    rebuilt = standard_store.rebuild_index()
    assert rebuilt["chunk_count"] == 4
    assert standard_store.load(env.standard_id)["published"] is True
    assert _chunk_clauses(env.standard_id) == {"5.1", "5.2"}
