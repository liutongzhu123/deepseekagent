"""SP-8.2: sample-preparation guide generation and commission-order check."""
from fastapi.testclient import TestClient

from app import main, standard_store
from app.standard_extract.samples import (
    build_sample_guide, check_sample_order, extract_samples)

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


def _skips(result, code=None):
    return [entry for entry in result["skipped"] if code is None or entry["code"] == code]


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
                         {"clause": "5.2", "quantity": 3, "unit": "件"}]}
    assert check_sample_order(guide, matched)["mismatches"] == []
    matched["items"][1]["quantity"] = 1
    bad = check_sample_order(guide, matched)
    assert _codes(bad) == ["quantity_short"] and bad["mismatches"][0]["item_clause"] == "5.2"
    # A single total cannot be compared with two independent item requirements.
    total = check_sample_order(guide, {"quantity": 1, "unit": "件"})
    assert total["mismatches"] == [] and total["checked"]["quantity"] == 0


def test_size_requirements_are_skipped_with_reason():
    """收窄：尺寸的或/且语义不判；含 #206 的四个曾误判输入。"""
    guide = build_sample_guide(_samples() + [_entry("size", text="200mm×100mm×3mm")], _ITEMS)
    for model in ("100×50×3mm", "50×100×2mm", "100×50mm"):
        result = check_sample_order(guide, {"model": model})
        assert result["mismatches"] == [] and result["checked"]["size"] == 0, model
        assert [e["raw"] for e in _skips(result, "size_not_judged")] == ["100mm×50mm×2mm", "200mm×100mm×3mm"]
    text = "5.2 DUT 所有待测表面应划分为 100mmx100mm 或 30mmx30mm 的方格。"
    guide = build_sample_guide(extract_samples([(text, 1)], []), [{"clause": "5.2", "name": "外观"}])
    for model in ("50mm×100mm", "100mm×200mm", "30mm×999mm", "100mm"):
        result = check_sample_order(guide, {"model": model})
        assert result["ok"] is True and result["mismatches"] == [], model
        skips = _skips(result, "unassigned_not_judged")
        assert [e["raw"] for e in skips] == ["100mmx100mm", "30mmx30mm"]
        assert all("#206" in e["reason"] and "或/且" in e["reason"] for e in skips)


def test_unassigned_requirements_are_skipped_with_reason():
    """收窄：未归属的数量要求不与委托单总量比对。"""
    guide = build_sample_guide([_entry("quantity", clause="", item_clause="", value=5, unit="套", raw="5套样品")], _ITEMS)
    short = check_sample_order(guide, {"quantity": 1, "unit": "套"})
    assert short["mismatches"] == [] and short["ok"] is True
    skips = _skips(short, "unassigned_not_judged")
    assert len(skips) == 1 and skips[0]["raw"] == "5套样品"
    assert skips[0]["item_name"] == "未归属要求" and "未归属" in skips[0]["reason"]
    assert [e["code"] for e in _skips(check_sample_order(guide, {"sample_no": "A-01"}), "sample_no_not_judged")] == ["sample_no_not_judged"]


def test_pretreatment_is_skipped_and_missing_data_is_not_a_mismatch():
    guide = build_sample_guide(_samples(), _ITEMS)
    result = check_sample_order(guide, {"pretreatment": "无需预处理"})
    assert result["mismatches"] == []
    assert [e["code"] for e in _skips(result, "pretreatment_not_judged")] == ["pretreatment_not_judged"]
    missing = check_sample_order(guide, {})
    assert missing["mismatches"] == [] and missing["checked"] == {"quantity": 0, "size": 0, "pretreatment": 0}
    assert [e["code"] for e in _skips(missing)] == ["size_not_judged", "pretreatment_not_judged"]
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
    body = response.json()
    assert [m["code"] for m in body["mismatches"]] == ["quantity_short"]
    assert [e["code"] for e in body["skipped"]] == ["size_not_judged", "pretreatment_not_judged"]
    assert body["guide"]["items"][1]["steps"][0] == "准备 3件样品"
    assert client.get("/api/v1/standards/std_missing/sample-guide").status_code == 404
