"""B10: manual review saves corrections and keeps them as feedback samples."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, standard_store


def _parsed() -> dict:
    return {
        "meta": {"standard_no": "Q/TEST 110-2026", "name": "复核闭环演示", "pages": 2},
        "summary": {"items": 2},
        "items": [
            {"clause": "5.1", "name": "外观", "requirement": "外观无缺陷", "method_clause": "6.1",
             "method": "目测", "page": 1, "method_page": 2, "confidence": 0.6,
             "review_required": True, "reviewed": False,
             "review_fields": [{"field": "requirement", "confidence": 0.6, "reason": "OCR 置信度低于 0.90"}]},
            {"clause": "5.2", "name": "尺寸", "requirement": "符合图纸", "method_clause": "6.2",
             "method": "卡尺", "page": 1, "method_page": 2, "confidence": 0.9,
             "review_required": False, "reviewed": False, "review_fields": []},
        ],
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "standards"
    monkeypatch.setattr(standard_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(standard_store, "INDEX_PATH", data_dir / "kb_index.json")
    return SimpleNamespace(
        client=TestClient(main.app),
        record=standard_store.create(_parsed(), b"%PDF", "demo.pdf", project="P1"),
    )


def test_correction_is_saved_with_before_after_sample(env):
    updated = standard_store.update_review(
        env.record["id"],
        [{"clause": "5.1", "name": "外观", "requirement": "外观无缺陷，无毛刺", "method": "目测"}],
        reviewer="张复核")
    item, sample = updated["items"][0], updated["review_feedback"][0]
    assert item["reviewed"] is True and item["review_fields"] == []
    assert (sample["action"], sample["field"]) == ("correct", "requirement")
    assert (sample["before"], sample["after"]) == ("外观无缺陷", "外观无缺陷，无毛刺")
    assert sample["reason"] == "OCR 置信度低于 0.90" and sample["confidence"] == 0.6
    assert standard_store.load(env.record["id"])["review_feedback"][0]["id"] == sample["id"]


def test_confirm_and_supplement_are_kept_as_samples(env):
    updated = standard_store.update_review(
        env.record["id"], [{"clause": "5.1", "reviewed": True}], reviewer="李复核",
        additions=[{"clause": "5.3", "name": "耐湿热", "requirement": "按 6.3", "method": "湿热箱"}])
    assert updated["items"][0]["reviewed"] is True
    assert (updated["items"][-1]["clause"], updated["items"][-1]["source"]) == ("5.3", "manual")
    assert [s["action"] for s in updated["review_feedback"]] == ["confirm", "supplement"]
    assert updated["revision"] == 1 and updated["summary"]["feedback_samples"] == 2
    with pytest.raises(ValueError):
        standard_store.update_review(env.record["id"], [],
                                     additions=[{"clause": "5.3", "name": "重复条款"}])


def test_review_api_returns_persisted_feedback(env):
    response = env.client.patch(f"/api/v1/standards/{env.record['id']}", json={
        "reviewer": "接口复核",
        "items": [{"clause": "5.2", "requirement": "符合图纸（人工修正）"}],
        "additions": [{"clause": "5.3", "name": "标识", "requirement": "标识清晰"}]})
    assert response.status_code == 200
    data = response.json()
    assert data["revision"] == 1 and data["summary"]["feedback_samples"] == 2
    assert [s["action"] for s in data["review_feedback"]] == ["correct", "supplement"]
    assert len(env.client.get(f"/api/v1/standards/{env.record['id']}").json()["review_feedback"]) == 2
    duplicate = env.client.patch(f"/api/v1/standards/{env.record['id']}", json={"additions": [{"clause": "5.1", "name": "重复"}]})
    assert duplicate.status_code == 409


def test_review_ui_exposes_confirm_supplement_and_samples():
    root = Path(__file__).resolve().parents[1]
    html, js = ((root / "static" / name).read_text(encoding="utf-8") for name in ("index.html", "ui.js"))
    assert all(f'id="{name}"' in html for name in ("confirmBtn", "addItemBtn", "addForm", "reviewFeedbackList"))
    assert all(marker in js for marker in ("reviewed: !item.reviewed", "additions:", "renderReviewFeedback"))
