"""API smoke test for the quote-closure self-check wiring."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import main

client = TestClient(main.app)


def make_dvp(path: Path) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws.title = "5、手动座椅总成"
    ws["A3"] = "零件/总成：前排座椅"
    ws["C8"] = "试验项目"
    ws["R9"] = "单价"
    ws["S9"] = "数量"
    ws["T9"] = "单位"
    ws["B11"] = "Q/NEQ S8-17-2022"
    ws["C11"] = "调角器的同步性能"
    ws["R11"] = 2000
    ws["S11"] = 1
    ws["T11"] = "席位"
    workbook.save(path)
    return path


def make_sales_template(path: Path) -> Path:
    workbook = Workbook()
    ws = workbook.active
    ws["A1"] = "销售报价表"
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


def test_quote_generate_returns_verified_sales_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "QUOTE_DIR", tmp_path / "quotes")
    dvp = make_dvp(tmp_path / "DEMO1前排座椅DVP.xlsx")
    template = make_sales_template(tmp_path / "销售报价表.xlsx")

    with dvp.open("rb") as dvp_file, template.open("rb") as template_file:
        response = client.post(
            "/api/v1/quotes/generate",
            files={
                "file": (dvp.name, dvp_file, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "template": (
                    template.name,
                    template_file,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    verification = payload["sales_quote_verification"]
    assert verification["ok"] is True
    assert verification["cells"]["C9"] == 0
    assert verification["cells"]["C10"] == 1200
    assert verification["cells"]["C13"] == 1272
    assert verification["cells"]["C15"] == 1272
    assert payload["preview"]["最终价格"] == 1272
    assert "sales_quote" in payload["files"]
