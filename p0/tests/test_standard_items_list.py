"""B9: inspection-item list (name / method clause / applicable parts)."""
from __future__ import annotations

from app import standard_store
from app.standard_extract import items_list
from app.standard_parser import parse_standard


def test_scope_parts_split_only_on_real_component_boundaries():
    assert items_list.extract_scope_parts([
        ("1 范围", 1), ("本标准适用于安全气囊电子控制单元及碰撞传感器。", 1)
    ]) == ["安全气囊电子控制单元", "碰撞传感器"]
    assert items_list.extract_scope_parts([
        ("本标准适用于汽车电气和电子零部件（包括以下电压等级的电子零部件）", 1)
    ]) == ["汽车电气和电子零部件"]


def test_parse_generates_reviewable_item_list():
    result = parse_standard([
        "Q/TEST 123-2024\n测试标准\n2024-01-01 发布",
        "1 范围\n本标准适用于安全气囊电子控制单元及碰撞传感器。",
        "5 技术要求\n5.1 外观检查\n外观应无缺陷。\n5.2 密封性能\n应密封。",
        "6 试验方法\n6.1 外观检查试验\n目测。\n6.2 密封性试验\n按上述要求试验。",
    ], "测试标准.pdf")
    checklist = result["items_list"]
    assert checklist["applicable_parts"] == ["安全气囊电子控制单元", "碰撞传感器"]
    assert [entry["method_no"] for entry in checklist["items"]] == ["6.1", "6.2"]
    assert all(entry["review_status"] == "pending" for entry in checklist["items"])
    assert checklist["summary"]["items_with_applicable_parts"] == 2
    assert checklist["summary"]["ready_for_store"] is False


def test_table_applicability_column_overrides_scope():
    items = [{
        "seq": 1, "clause": "5.1", "name": "外观检查", "method_clause": "6.1",
        "tables": [{
            "header": ["项目", "适用零件", "性能要求", "试验方法"],
            "rows": [["外观检查", "内饰", "无缺陷", "6.1"]],
        }],
    }]
    checklist, _summary = items_list.apply(
        {"standard_no": "Q/TEST 1", "name": "演示标准"},
        items, [("本标准适用于整车。", 1)],
    )
    assert items[0]["applicable_parts"] == ["内饰"]
    assert items[0]["applicable_parts_source"] == "table"
    assert checklist["items"][0]["applicable_parts"] == ["内饰"]


def test_review_edits_parts_and_marks_list_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")
    record = standard_store.create({
        "meta": {"standard_no": "Q/TEST 002-2026", "name": "复核演示标准"},
        "summary": {"items": 1},
        "items": [{
            "clause": "5.1", "name": "外观", "method_clause": "6.1",
            "applicable_parts": [], "applicable_parts_source": "none",
            "reviewed": False,
        }],
        "warnings": [],
    }, b"%PDF-1.4 demo", "demo.pdf")
    updated = standard_store.update_review(
        record["id"],
        [{
            "clause": "5.1", "method_clause": "6.1.1",
            "applicable_parts": ["保险杠总成", "翼子板"], "reviewed": True,
        }],
        reviewer="复核人",
    )
    item = updated["items"][0]
    assert item["applicable_parts"] == ["保险杠总成", "翼子板"]
    assert item["applicable_parts_source"] == "manual"
    assert item["method_clause"] == "6.1.1" and item["reviewed"] is True
    assert updated["summary"]["items_with_applicable_parts"] == 1
    checklist = items_list.build(updated["meta"], updated["items"])
    assert checklist["items"][0]["review_status"] == "confirmed"
    assert checklist["summary"]["ready_for_store"] is True
