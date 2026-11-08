import json
import zipfile
from pathlib import Path

import pytest

from app import standard_qa, standard_store
from app.standard_parser import parse_standard
from app.xlsx_writer import write_xlsx


def synthetic_pages() -> list[str]:
    cover = "\n".join(
        [
            "Q/TEST 123-2024",
            "测试标准",
            "2024-01-01 发布",
            "2024-02-01 实施",
        ]
    )
    requirements = "\n".join(
        [
            "5  技术要求 Technical requirements",
            "5.1  外观检查 Appearance inspection",
            "外观应无缺陷。",
            "5.2  密封性能 Tightness",
            "密封性应良好。",
        ]
    )
    methods = "\n".join(
        [
            "6  试验方法 Test methods",
            "6.1  外观检查试验 Appearance test",
            "目测检查。",
            "6.2  密封性试验 Tightness test",
            "按方法进行。",
        ]
    )
    table = "5.1 6.1\n5.2 6.2"
    return [cover, requirements, methods, table]


def test_parse_standard_extracts_items_and_pages():
    result = parse_standard(synthetic_pages(), "测试标准.pdf")
    assert result["meta"]["standard_no"] == "Q/TEST 123-2024"
    assert result["meta"]["name"] == "测试标准"
    assert result["summary"]["items"] == 2
    assert result["summary"]["clauses_traced"] == 2
    first, second = result["items"]
    assert first["clause"] == "5.1"
    assert first["method_clause"] == "6.1"
    assert first["page"] == 2
    assert first["method_page"] == 3
    assert "外观应无缺陷" in first["requirement"]
    assert "目测检查" in first["method"]
    assert second["clause"] == "5.2"
    assert result["mapping_source"] == "item-table"


def test_scanned_pdf_reports_without_items():
    result = parse_standard(["", "扫描件无文本层"], "scan.pdf")
    assert result["summary"]["items"] == 0
    assert any("扫描件" in warning for warning in result["warnings"])


def chinese_method_only_pages() -> list[str]:
    cover = "\n".join(
        [
            "Q/DEMO 002-2026",
            "汽车座椅用泡沫材料雾化性能的测定",
            "2026-01-01 发布",
        ]
    )
    body = "\n".join(
        [
            "1 范围",
            "本标准适用于演示标准。",
            "2 规范性引用文件",
            "GB/T 8170 数字修约规则。",
            "3 术语",
            "3.1 雾化",
            "雾化是指挥发性成分凝结的现象。",
            "4 试验原理",
            "通过加热试样并在玻璃板上冷凝后测量光泽度。",
            "5 试验设备",
            "5.1 雾化测试仪",
            "由加热装置和冷却系统两部分组成。",
            "6 试验方法",
            "6.1 试样的准备",
            "6.1.1 试样必须取自制造后存放48h以上的零件。",
            "6.1.2 试样应在标准环境中放置24h。",
            "6.2 清洁",
            "6.2.1 将玻璃板清洗干净。",
            "6.3 试验过程",
            "6.3.1 将试样放入烧杯。",
            "6.3.2 加热并冷却后测量光泽度。",
            "7 结果表示",
            "7.1 按公式计算雾化值。",
        ]
    )
    return [cover, body]


def test_chinese_only_method_standard_is_extracted_for_review():
    result = parse_standard(chinese_method_only_pages(), "演示标准.pdf")
    assert result["mapping_source"] == "method-only"
    # "5 试验设备" must not be mistaken for the requirements section.
    assert [item["clause"] for item in result["items"]] == ["6.1", "6.2", "6.3"]
    assert result["summary"]["items"] == 3
    assert result["summary"]["review_required_items"] == 3
    assert all(item["review_required"] for item in result["items"])
    assert all(item["method"] for item in result["items"])
    assert all(item["requirement"] == "" for item in result["items"])
    assert result["items"][2]["method"].startswith("将试样放入烧杯")
    assert any("未识别到「技术要求」章节" in warning for warning in result["warnings"])


def test_pure_chinese_headings_are_detected_but_body_sentences_are_not():
    from app.standard_parser import _clean_lines, _detect_headings

    lines = _clean_lines(
        [
            "6 试验方法\n6.1 试样的准备\n6.1.1 试样必须取自制造后存放48h以上的零件。",
            "6.2 清洁\n6.2.1 将玻璃板清洗干净。",
        ],
        "",
    )
    headings = _detect_headings(lines)
    assert [heading.num for heading in headings] == ["6", "6.1", "6.2"]


def test_section_aliases_are_recognised():
    from app.standard_parser import _clean_lines, _detect_headings, _section_number

    lines = _clean_lines(["5 技术条件\n5.1 外观\n6 检验方法\n6.1 外观检查"], "")
    headings = _detect_headings(lines)
    assert _section_number(headings, "技术要求") == "5"
    assert _section_number(headings, "试验方法") == "6"


def test_xlsx_writer_roundtrip(tmp_path):
    path = tmp_path / "out.xlsx"
    write_xlsx(
        path,
        [
            ("实检项目", [["检验依据", "名称"], ["Q/TEST", "测试"]], [16, 20]),
            ("明细", [["条款", "要求"], ["5.1", "外观应无缺陷"]], [10, 40]),
        ],
    )
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet2.xml" in names
        sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        assert "外观应无缺陷" in sheet
        assert 't="inlineStr"' in sheet
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        assert "实检项目" in workbook and "明细" in workbook


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(standard_store, "DATA_DIR", tmp_path / "standards")
    parsed = parse_standard(synthetic_pages(), "测试标准.pdf")
    record = standard_store.create(parsed, b"%PDF-fake", "测试标准.pdf")
    return record


def test_review_publish_and_ask(store):
    # 未确认不入库：两个条款都确认后才能发布（SP-2.2）。
    updated = standard_store.update_review(
        store["id"],
        [{"clause": "5.1", "name": "外观（复核）", "requirement": "外观不得有裂纹。"},
         {"clause": "5.2", "reviewed": True}],
        reviewer="测试复核人",
    )
    assert updated["revision"] == 1
    item = next(item for item in updated["items"] if item["clause"] == "5.1")
    assert item["name"] == "外观（复核）"
    assert item["reviewed"] is True
    assert standard_store.load(store["id"])["revision"] == 1

    published = standard_store.publish(store["id"])
    assert published["chunks"] == 4
    kbs = standard_store.published_kbs(store["id"])
    assert kbs and kbs[0]["standard_no"] == "Q/TEST 123-2024"

    answer = standard_qa.answer("5.1 的外观要求是什么？", kbs, mode="keyword")
    assert answer["found"] is True
    assert answer["citations"][0]["clause"] == "5.1"
    assert answer["citations"][0]["page"] == 2
    assert "外观" in answer["answer"]

    method = standard_qa.answer("5.1 的试验方法是什么？", kbs, mode="keyword")
    assert method["found"] is True
    assert method["citations"][0]["kind"] == "method"


def test_ask_returns_not_found_instead_of_fabricating(store):
    standard_store.update_review(
        store["id"],
        [{"clause": "5.1", "reviewed": True}, {"clause": "5.2", "reviewed": True}],
        reviewer="测试复核人",
    )
    standard_store.publish(store["id"])
    kbs = standard_store.published_kbs(store["id"])
    answer = standard_qa.answer("航空航天材料的疲劳寿命要求是什么？", kbs, mode="keyword")
    assert answer["found"] is False
    assert "未找到" in answer["answer"]
    assert answer["citations"] == []


def test_list_standards(store):
    listing = standard_store.list_standards()
    assert len(listing) == 1
    assert listing[0]["id"] == store["id"]
    assert listing[0]["items"] == 2
    assert listing[0]["published"] is False


def test_export_path_is_inside_standard_directory(store):
    path = standard_store.export_path(store["id"])
    assert path.name == "export.xlsx"
    assert standard_store.safe_filename("Q/TEST 123-2024-解析.xlsx") == "Q-TEST 123-2024-解析.xlsx"


def test_table_cell_cleanup_for_merged_performance_tables():
    from app.standard_parser import _readable_requirement, _table_item_name

    assert _table_item_name("悬臂梁缺口冲击强度（-30℃），Notched Izod", "6.8.3") == "悬臂梁缺口冲击强度（-30℃）"
    assert _table_item_name("2：气味性/Odor", "6.10") == "气味性"
    assert _readable_requirement(["0.92±0.02；天；0.92±0.02；0.90±0.02"]) == "0.92±0.02；0.90±0.02"


def test_table_style_standard_falls_back_to_method_candidates():
    """Requirement tables without clause headings must not yield an empty parse."""
    pages = [
        "Q/TEST 456-2026\n汽车材料性能要求\n2026-01-01 发布",
        "3 技术要求\n表 1 试验要求\n分类 适用零件 性能要求 试验方法\n方法 A 擦拭法 内饰 外观无变化 6.1",
        "6 试验方法\n6.1 擦拭法\n试样应从正常产品上截取。\n6.2 线性摩擦法\n按 ISO 105-X12 进行。",
    ]
    result = parse_standard(pages, "table-only.pdf")
    assert result["summary"]["items"] >= 1
    assert result["mapping_source"] == "method-only-fallback"
    assert any("表格型标准" in warning for warning in result["warnings"])
