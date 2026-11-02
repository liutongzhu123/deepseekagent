"""Multi-format quote input: Excel quote sheets, PDF tables, OCR rows, API."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import main, quote_import
from app.quote import (
    fill_sales_quote,
    parse_quote,
    parse_quote_bytes,
    write_crm_xlsx,
)

client = TestClient(main.app)


def make_quote_sheet(path: Path) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws.append(["序号", "样品名称", "检验标准", "检验项目", "单价（元）", "数量", "单位"])
    ws.append([1, "F610S座椅安全带提醒器总成", "测试大纲", "气味", 1200, 1, "材料"])
    ws.append([2, "F610S座椅安全带提醒器总成", "测试大纲", "雾化", 1000, 2, "材料"])
    workbook.save(path)
    return path


def make_section_price_sheet(path: Path) -> Path:
    """DVP-like sheet whose price header appears in a later section row."""
    workbook = Workbook()
    ws = workbook.active
    ws.title = "DVP"
    ws.append(["序号", "来源", "试验项目", "开发目标要求"])
    ws.append([1, "Q/NEQ S8-17-2022", "外观检查", "无裂纹"])
    ws.append(["三、材料法规项", "", "", "", "单价", "数量", "单位"])
    ws.append([2, "IEC 62321", "REACH-SVHC", "≤1000mg/kg", 1500, 2, "材料"])
    workbook.save(path)
    return path


def make_test_outline(path: Path) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws.append(["序号", "试验项目", "试验标准", "数量"])
    ws.append([1, "阻燃特性", "GB 8410", 5])
    ws.append([2, "气味", "Q/BAW S0009", 1])
    workbook.save(path)
    return path


def make_sales_template(path: Path) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws["A9"] = "产品金额（不含税）"
    ws["A10"] = "服务金额（不含税）"
    ws["C10"] = "=C8-C9"
    ws["C11"] = "=C9*B11"
    ws["C12"] = "=C10*B12"
    ws["C13"] = "=C8+C11+C12"
    ws["C15"] = "=C13+C14"
    ws["B11"] = 0.16
    ws["B12"] = 0.06
    workbook.save(path)
    return path


def test_parse_quote_sheet_with_prices(tmp_path: Path) -> None:
    path = make_quote_sheet(tmp_path / "客户报价单.xlsx")
    result = parse_quote(path)
    assert result.parse_method.startswith("Excel")
    assert result.sample_name == "F610S座椅安全带提醒器总成"
    assert len(result.lines) == 2
    first = result.lines[0]
    assert first.item_name == "气味"
    assert first.standard == "测试大纲"
    assert first.unit == "材料"
    assert first.quantity == 1
    assert first.unit_price == 1200
    assert first.needs_price is False
    assert result.all_priced
    assert result.subtotal == 3200


def test_section_price_header_after_blank_rows(tmp_path: Path) -> None:
    path = make_section_price_sheet(tmp_path / "带章节单价的DVP.xlsx")
    result = parse_quote(path)
    assert [line.item_name for line in result.lines] == ["外观检查", "REACH-SVHC"]
    assert result.lines[0].needs_price is True
    assert result.lines[1].needs_price is False
    assert result.lines[1].unit_price == 1500
    assert result.lines[1].quantity == 2
    assert result.lines[1].unit == "材料"
    assert result.priced_lines == 1
    assert result.unpriced_lines == 1


def test_test_outline_without_price_marks_rows_and_blank_crm_price(
    tmp_path: Path,
) -> None:
    path = make_test_outline(tmp_path / "客户测试大纲.xlsx")
    result = parse_quote(path)
    assert len(result.lines) == 2
    assert result.unpriced_lines == 2
    assert result.all_priced is False
    assert all(line.needs_price for line in result.lines)

    crm = write_crm_xlsx(tmp_path / "crm.xlsx", result)
    workbook = load_workbook(crm)
    ws = workbook["CRM报价草稿"]
    assert ws["H2"].value in ("", None)
    assert "待补单价" in ws["K2"].value
    summary = {row[0]: row[1] for row in workbook["报价汇总"].iter_rows(values_only=True) if row[1] is not None}
    assert summary["含单价行数"] == "0/2"

    with pytest.raises(ValueError, match="缺少单价"):
        fill_sales_quote(make_sales_template(tmp_path / "销售报价表.xlsx"), result, tmp_path / "sales.xlsx")


def test_pdf_table_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ["序号", "样品名称", "检验标准", "检验项目", "单价（元）", "数量", "单位"],
        ["1", "F610S", "测试大纲", "禁用物质-非金属8项", "1,500.00", "1", "材料"],
    ]

    def fake_grids(data: bytes):
        return [("第 1 页", rows)], 1

    monkeypatch.setattr(quote_import, "_pdf_table_grids", fake_grids)
    result = parse_quote_bytes(b"%PDF-1.7 fake", "报价单.pdf")
    assert result.parse_method == "PDF 文本表格"
    assert len(result.lines) == 1
    assert result.lines[0].unit_price == 1500
    assert result.lines[0].unit == "材料"


def _token(text: str, x0: float, x1: float, y0: float, y1: float) -> quote_import._Token:
    return quote_import._Token(text=text, x0=x0, x1=x1, y0=y0, y1=y1, confidence=0.96)


def test_ocr_tokens_become_quote_rows() -> None:
    tokens = [
        _token("序号", 5, 35, 10, 24),
        _token("试验项目", 60, 130, 10, 24),
        _token("试验标准", 190, 260, 10, 24),
        _token("单价", 320, 360, 10, 24),
        _token("数量", 400, 440, 10, 24),
        _token("单位", 500, 540, 10, 24),
        _token("1", 5, 20, 45, 60),
        _token("气味", 60, 110, 45, 60),
        _token("Q/BAW S0009", 190, 280, 45, 60),
        _token("1200", 320, 370, 45, 60),
        _token("1", 400, 420, 45, 60),
        _token("材料", 500, 540, 45, 60),
        _token("2", 5, 20, 80, 95),
        _token("雾化特性", 60, 130, 80, 95),
        _token("Q/BAW L0207", 190, 280, 80, 95),
        _token("1000", 320, 370, 80, 95),
        _token("2", 400, 420, 80, 95),
        _token("材料", 500, 540, 80, 95),
    ]
    rows = quote_import._rows_from_tokens(tokens)
    assert [row.item for row in rows] == ["气味", "雾化特性"]
    assert rows[0].standard == "Q/BAW S0009"
    assert rows[0].unit_price == 1200
    assert rows[0].quantity == 1
    assert rows[1].unit_price == 1000
    assert rows[1].quantity == 2
    assert rows[0].confidence and rows[0].confidence > 0.9


def test_parse_api_accepts_excel_and_rejects_other_formats(tmp_path: Path) -> None:
    quote_sheet = make_quote_sheet(tmp_path / "客户报价单.xlsx")
    with quote_sheet.open("rb") as handle:
        response = client.post(
            "/api/v1/quotes/parse",
            files={"file": (quote_sheet.name, handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["lines"] == 2
    assert payload["all_priced"] is True
    assert payload["parse_meta"]["parse_method"].startswith("Excel")

    (tmp_path / "说明.docx").write_bytes(b"not a quote")
    with (tmp_path / "说明.docx").open("rb") as handle:
        response = client.post(
            "/api/v1/quotes/parse",
            files={"file": ("说明.docx", handle, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
    assert response.status_code == 400
    assert "Excel" in response.json()["detail"]


def test_generate_skips_sales_quote_when_price_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "QUOTE_DIR", tmp_path / "quotes")
    outline = make_test_outline(tmp_path / "客户测试大纲.xlsx")
    template = make_sales_template(tmp_path / "销售报价表.xlsx")
    with outline.open("rb") as outline_file, template.open("rb") as template_file:
        response = client.post(
            "/api/v1/quotes/generate",
            files={
                "file": (outline.name, outline_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "template": (template.name, template_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["sales_quote_verification"] is None
    assert "缺少单价" in payload["sales_quote_skipped_reason"]
    assert "sales_quote" not in payload["files"]
    assert payload["summary"]["unpriced_lines"] == 2
    crm = Path(payload["crm_file"])
    assert crm.exists()

    priced = make_quote_sheet(tmp_path / "客户报价单.xlsx")
    with priced.open("rb") as priced_file, template.open("rb") as template_file:
        response = client.post(
            "/api/v1/quotes/generate",
            files={
                "file": (priced.name, priced_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "template": (template.name, template_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["sales_quote_skipped_reason"] == ""
    verification = payload["sales_quote_verification"]
    assert verification["ok"] is True
    assert verification["cells"]["C8"] == 1920
    assert verification["cells"]["C13"] == 2035.2
    assert "sales_quote" in payload["files"]
