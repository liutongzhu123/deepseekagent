"""B8: multiple revisions per standard plus 增/删/改 diff between versions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import standard_store, weknora


def parsed_standard(standard_no: str = "Q/TEST 002-2026") -> dict:
    return {
        "meta": {"standard_no": standard_no, "name": "版本演示标准", "pages": 2},
        "summary": {"items": 2},
        "items": [
            {"clause": "5.1", "name": "外观", "requirement": "外观无缺陷",
             "method_clause": "6.1", "method": "目测", "page": 1, "method_page": 2},
            {"clause": "5.2", "name": "尺寸", "requirement": "符合图纸要求",
             "method_clause": "6.2", "method": "卡尺", "page": 1, "method_page": 2},
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
    return SimpleNamespace(data_dir=data_dir)


def _create(standard_no: str = "Q/TEST 002-2026", parsed: dict | None = None) -> dict:
    return standard_store.create(
        parsed or parsed_standard(standard_no), b"%PDF-1.4 demo", "demo.pdf"
    )


def test_multiple_versions_are_stored_as_revision_snapshots(std_env):
    record = _create()
    standard_store.update_review(
        record["id"], [{"clause": "5.1", "requirement": "外观无划痕"}], reviewer="复核一"
    )
    standard_store.update_review(
        record["id"], [{"clause": "5.1", "requirement": "外观无油污"}], reviewer="复核二"
    )

    versions = standard_store.list_versions(record["id"])

    assert [version["version"] for version in versions] == [2, 1, 0]
    assert versions[0]["current"] is True
    assert (std_env.data_dir / record["id"] / "versions" / "2.json").exists()
    assert standard_store.version_record(record["id"], 0)["items"][0][
        "requirement"
    ] == "外观无缺陷"


def test_diff_items_marks_added_removed_and_modified(std_env):
    old_items = [
        {"clause": "5.1", "name": "外观", "requirement": "外观无缺陷"},
        {"clause": "5.2", "name": "尺寸", "requirement": "符合图纸要求"},
        {"clause": "5.3", "name": "重量", "requirement": "≤ 10 kg"},
    ]
    new_items = [
        {"clause": "5.1", "name": "外观", "requirement": "外观无划痕"},
        {"clause": "5.3", "name": "重量", "requirement": "≤ 10 kg"},
        {"clause": "5.4", "name": "标识", "requirement": "标识清晰"},
    ]

    diff = standard_store.diff_items(old_items, new_items)

    assert [entry["clause"] for entry in diff["added"]] == ["5.4"]
    assert [entry["clause"] for entry in diff["removed"]] == ["5.2"]
    assert [entry["clause"] for entry in diff["modified"]] == ["5.1"]
    assert diff["modified"][0]["fields"] == [
        {"field": "requirement", "label": "技术要求/判定要求",
         "before": "外观无缺陷", "after": "外观无划痕"}
    ]
    assert diff["counts"] == {"added": 1, "removed": 1, "modified": 1, "unchanged": 1}


def test_diff_versions_defaults_to_previous_revision(std_env):
    record = _create()
    standard_store.update_review(
        record["id"],
        [{"clause": "5.1", "requirement": "外观无划痕", "method": "目测并拍照"}],
        reviewer="复核人",
    )

    diff = standard_store.diff_versions(record["id"])

    assert (diff["kind"], diff["from_version"], diff["to_version"]) == ("revisions", 0, 1)
    assert [entry["clause"] for entry in diff["modified"]] == ["5.1"]
    assert {field["field"] for field in diff["modified"][0]["fields"]} == {
        "requirement", "method"
    }
    assert diff["report"].startswith("# 标准变更对比报告")
    assert "## 修改（1）" in diff["report"]
    assert "技术要求/判定要求：外观无缺陷 → 外观无划痕" in diff["report"]
    with pytest.raises(ValueError):
        standard_store.diff_versions(record["id"], 1, 1)


def test_diff_standards_between_two_editions(std_env):
    old = _create("Q/TEST 002-2015")
    new_parsed = parsed_standard("Q/TEST 002-2026")
    new_parsed["items"][0]["requirement"] = "外观无划痕"
    new_parsed["items"][1] = {
        "clause": "5.3", "name": "标识", "requirement": "标识清晰",
        "method_clause": "6.3", "method": "目测", "page": 2, "method_page": 2,
    }
    new = _create("Q/TEST 002-2026", new_parsed)

    diff = standard_store.diff_standards(old["id"], new["id"])

    assert diff["kind"] == "standards"
    assert diff["from"]["standard_no"] == "Q/TEST 002-2015"
    assert diff["to"]["standard_no"] == "Q/TEST 002-2026"
    assert diff["counts"] == {"added": 1, "removed": 1, "modified": 1, "unchanged": 0}
    for section in ("## 新增（1）", "## 删除（1）", "## 修改（1）"):
        assert section in diff["report"]
