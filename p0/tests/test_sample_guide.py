"""SP-8.2: sample-preparation guide generation and commission-order check."""
from fastapi.testclient import TestClient

from app import main, standard_store
from app.standard_extract.samples import build_sample_guide, check_sample_order

_ITEMS = [{"clause": "5.1", "name": "外观"}, {"clause": "5.2", "name": "密封性能"}]


def _entry(kind, clause="5.2", **values):
    return {"kind": kind, "value": None, "value_max": None, "unit": "", "text": None,
            "conditions": None, "clause": clause, "item_clause": clause,
            "raw": values.get("text", ""), **values}


def _samples():
    return [_entry("quantity", value=3, unit="件", raw="样品数量：3 件"),
            _entry("size", unit="mm", text="100mm×50mm×2mm"),
            _entry("pretreatment", text="试验前在 23℃±2℃下放置 24h")]


def _codes(result):
    return [mismatch["code"] for mismatch in result["mismatches"]]


def test_guide_groups_requirements_and_keeps_unowned_entries():
    samples = _samples() + [
        _entry("quantity", clause="", item_clause="", value=5, unit="套", raw="5套样品"),
        _entry("quantity", clause="9.9", item_clause="9.9", value=2, unit="件", raw="2件样品"),
    ]
    guide = build_sample_guide(samples, _ITEMS)
    by_clause = {item["item_clause"]: item for item in guide["items"]}
    assert by_clause["5.2"]["status"] == "ready"
    assert by_clause["5.1"]["status"] == "not_extracted"
    assert by_clause["5.2"]["steps"] == ["准备 3件样品", "试样尺寸：100mm×50mm×2mm",
                                         "预处理：试验前在 23℃±2℃下放置 24h"]
    assert by_clause["5.2"]["requirements"][0]["raw"] == "样品数量：3 件"
    # Unowned entries and entries whose owning item was removed both survive.
    assert guide["unassigned"]["steps"] == ["准备 5套样品", "准备 2件样品"]
    assert guide["summary"] == {"items": 2, "items_with_requirements": 1,
                                "requirements": 5, "unassigned_requirements": 2}


def test_quantity_and_units():
    guide = build_sample_guide(_samples(), _ITEMS)
    assert _codes(check_sample_order(guide, {"quantity": 2, "unit": "件", "model": "100×50×2mm"})) == ["quantity_short"]
    assert "少于标准要求 3件" in check_sample_order(guide, {"quantity": 2})["mismatches"][0]["message"]
    for quantity in (3, "3 件", 4):
        assert check_sample_order(guide, {"quantity": quantity, "unit": "件", "model": "100×50×2mm"})["mismatches"] == []
    assert _codes(check_sample_order(guide, {"quantity": 9, "unit": "套"})) == ["unit_mismatch"]
    assert check_sample_order(guide, {"quantity": 3, "unit": "个"})["mismatches"] == []


def test_per_item_quantity_and_multi_item_total():
    samples = _samples() + [_entry("quantity", clause="5.1", value=2, unit="件", raw="2 件样品")]
    guide = build_sample_guide(samples, _ITEMS)
    matched = {"items": [{"clause": "5.1", "quantity": 2, "unit": "件"},
                         {"name": "密封性能", "quantity": 3, "unit": "件"}]}
    assert check_sample_order(guide, matched)["mismatches"] == []
    matched["items"][1]["quantity"] = 1
    bad = check_sample_order(guide, matched)
    assert _codes(bad) == ["quantity_short"] and bad["mismatches"][0]["item_clause"] == "5.2"
    # A single total cannot be compared with two independent item requirements.
    total = check_sample_order(guide, {"quantity": 1, "unit": "件"})
    assert total["mismatches"] == [] and total["checked"]["quantity"] == 0


def test_size_check_skips_non_dimension_model():
    guide = build_sample_guide(_samples(), _ITEMS)
    assert _codes(check_sample_order(guide, {"model": "100×50×3mm"})) == ["size_mismatch"]
    assert check_sample_order(guide, {"model": "50×100×2mm"})["mismatches"] == []
    # Missing thickness is a real inconsistency, not a partial pass.
    assert _codes(check_sample_order(guide, {"model": "100×50mm"})) == ["size_mismatch"]
    skipped = check_sample_order(guide, {"model": "JLY-2026-001"})
    assert skipped["mismatches"] == [] and skipped["checked"]["size"] == 0


def test_pretreatment_and_missing_order_data():
    guide = build_sample_guide(_samples(), _ITEMS)
    assert _codes(check_sample_order(guide, {"pretreatment": "无需预处理"})) == ["pretreatment_missing"]
    assert check_sample_order(guide, {"pretreatment": "23℃下放置 24h"})["mismatches"] == []
    missing = check_sample_order(guide, {})
    assert missing["mismatches"] == [] and missing["ok"] is True
    assert missing["checked"] == {"quantity": 0, "size": 0, "pretreatment": 0}
    assert check_sample_order({"items": []}, {})["mismatches"] == []


def test_sample_guide_api(tmp_path, monkeypatch):
    data = tmp_path / "standards"
    monkeypatch.setattr(standard_store, "DATA_DIR", data)
    monkeypatch.setattr(standard_store, "INDEX_PATH", data / "kb_index.json")
    record = standard_store.create({
        "meta": {"standard_no": "Q/TEST 001-2026", "name": "样品指引演示标准"},
        "items": _ITEMS, "samples": _samples(), "summary": {"items": 2}, "warnings": [],
    }, b"%PDF-1.4 demo", "demo.pdf", project="P1", created_by="alice")
    client = TestClient(main.app)
    guide = client.get(f"/api/v1/standards/{record['id']}/sample-guide").json()["guide"]
    assert guide["summary"]["requirements"] == 3
    response = client.post(f"/api/v1/standards/{record['id']}/sample-guide/validate",
                           json={"order": {"quantity": 2, "unit": "件", "model": "100×50×2mm"}})
    assert response.status_code == 200
    assert [m["code"] for m in response.json()["mismatches"]] == ["quantity_short"]
    assert response.json()["guide"]["items"][1]["steps"][0] == "准备 3件样品"
    assert client.get("/api/v1/standards/std_missing/sample-guide").status_code == 404
