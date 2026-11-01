from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.quote import fill_sales_quote, parse_dvp, sales_quote_preview, write_crm_xlsx
from app.report_writer import audit_markdown, batch_summary


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
    ws["B12"] = "Q/NEQ S1-125-2021"
    ws["C12"] = "零件总成散发性-座椅总成"
    ws["R12"] = 4000
    ws["S12"] = 2
    ws["T12"] = "袋子"
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


def test_parse_dvp_and_totals(tmp_path: Path) -> None:
    source = make_dvp(tmp_path / "JH11前排座椅DVP.xlsx")
    result = parse_dvp(source)
    assert result.sample_name == "JH11前排座椅"
    assert len(result.lines) == 2
    assert result.lines[0].unit == "席位/次"
    assert result.lines[1].unit == "次"
    assert result.summary()["subtotal"] == 2000 + 4000 * 2
    assert result.sales_amount == 6000


def test_generate_crm_and_sales_quote(tmp_path: Path) -> None:
    source = make_dvp(tmp_path / "JH11前排座椅DVP.xlsx")
    result = parse_dvp(source)
    crm = write_crm_xlsx(tmp_path / "crm.xlsx", result)
    workbook = load_workbook(crm)
    ws = workbook["CRM报价草稿"]
    assert ws["A1"].value == "样品名称"
    assert ws["B2"].value == "调角器的同步性能"
    assert ws["H2"].value == 2000

    template = make_sales_template(tmp_path / "template.xlsx")
    out = fill_sales_quote(template, result, tmp_path / "sales.xlsx")
    filled = load_workbook(out)
    ws = filled.active
    assert ws["C8"].value == 6000
    assert ws["C10"].value == "=C8-C9"
    assert "报价明细" in filled.sheetnames
    preview = sales_quote_preview(result)
    assert preview["最终价格"] == 6360


def test_report_markdown_and_batch_summary() -> None:
    result = {
        "file": "demo.docx",
        "summary": {"total": 2, "fail": 1, "warn": 0, "pass": 1, "skip": 0},
        "findings": [
            {
                "rule_id": "R01",
                "title": "页眉报告编号一致",
                "status": "fail",
                "severity": "high",
                "message": "正文页眉为空",
                "evidence": "封面=A; 正文=['']",
                "suggestion": "补齐页眉",
            },
            {
                "rule_id": "R02",
                "title": "检验均值计算",
                "status": "pass",
                "severity": "info",
                "message": "均值正确",
                "evidence": "3.8",
                "suggestion": "",
            },
        ],
        "data": {"cover_number": "A", "scores": [4.0, 4.0, 3.5]},
    }
    markdown = audit_markdown(result)
    assert "R01" in markdown and "建议退回修改" in markdown
    rows, rule_fail = batch_summary([result])
    assert rows[0]["fail"] == 1
    assert rule_fail["R01"] == 1
