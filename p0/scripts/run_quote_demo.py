#!/usr/bin/env python3
"""Run the P0 quote-assistant demo on a real front-seat DVP.

Usage:
  python3 scripts/run_quote_demo.py \
    --dvp "/path/to/<DVP>.xlsx" \
    --crm-gold "/path/to/CRM报价批量导入模板.xlsx" \
    --sales-template "/path/to/销售报价表.xlsx" \
    --out-dir "/path/to/deliverables/quote"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

from app.quote import (  # noqa: E402
    CRM_HEADERS,
    fill_sales_quote,
    parse_dvp,
    sales_quote_preview,
    write_crm_xlsx,
)


def _norm(value: object) -> str:
    import re

    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_gold(path: Path, sample_name: str) -> list[dict]:
    workbook = load_workbook(path, data_only=True)
    sheet = workbook["Sheet1"]
    rows = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or row[0] != sample_name:
            continue
        rows.append(
            {
                "item": str(row[1] or "").strip().lstrip("/").replace("\n", " "),
                "standard": row[3],
                "unit": row[5],
                "qty": row[6],
                "price": row[7],
                "discount": row[9],
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dvp", required=True)
    parser.add_argument("--crm-gold", required=True)
    parser.add_argument("--sales-template", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = parse_dvp(args.dvp)
    (out_dir / "quote_lines.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    crm_path = write_crm_xlsx(out_dir / "CRM报价草稿.xlsx", result)
    sales_path = fill_sales_quote(
        args.sales_template,
        result,
        out_dir / "销售报价表-生成.xlsx",
        {
            "project_name": f"{result.sample_name} DVP 试验报价",
            "contract": f"{result.sample_name} DVP（闭口合同）",
            "applicant": "业务经理",
            "region": "华北",
        },
    )

    gold = load_gold(Path(args.crm_gold), result.sample_name)
    compared = min(len(gold), len(result.lines))
    name_ok = price_ok = qty_ok = unit_ok = 0
    differences: list[dict] = []
    for index in range(compared):
        line = result.lines[index]
        expected = gold[index]
        same_name = _norm(line.item_name) == _norm(expected["item"])
        same_price = float(line.unit_price) == float(expected["price"])
        same_qty = float(line.quantity) == float(expected["qty"])
        same_unit = line.unit == expected["unit"]
        name_ok += same_name
        price_ok += same_price
        qty_ok += same_qty
        unit_ok += same_unit
        if not (same_name and same_price and same_qty and same_unit):
            differences.append(
                {
                    "row": index + 1,
                    "ours": [line.item_name, line.unit_price, line.quantity, line.unit],
                    "gold": [expected["item"], expected["price"], expected["qty"], expected["unit"]],
                }
            )

    summary = result.summary()
    preview = sales_quote_preview(result)
    metrics = {
        "gold_rows": len(gold),
        "generated_rows": len(result.lines),
        "name_match": name_ok,
        "price_match": price_ok,
        "qty_match": qty_ok,
        "unit_match": unit_ok,
        "differences": differences,
    }
    (out_dir / "quote_eval.json").write_text(
        json.dumps({"summary": summary, "metrics": metrics, "preview": preview}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# 报价助手 · 完整报价生成（P0）",
        "",
        f"- 输入：`{Path(args.dvp).name}`（{result.sample_name} DVP，{len(result.lines)} 行）",
        f"- 输出：CRM 报价草稿 + 销售报价表 + 报价明细",
        f"- 取价规则：单价/数量/单位全部来自 DVP 的 `单价/数量/单位` 列；折扣 {result.discount:.0f}% 为销售政策参数；未调用模型生成价格",
        "",
        "## 1. 报价汇总",
        "",
        "| 项目 | 金额（元） |",
        "|---|---:|",
        f"| DVP 合计（未折扣） | {result.subtotal:,.2f} |",
        f"| 折扣 | {result.discount:.0f}% |",
        f"| 销售报价（不含税） | {result.sales_amount:,.2f} |",
        f"| 产品金额（不含税） | 0.00 |",
        f"| 服务金额（不含税） | {result.sales_amount:,.2f} |",
        f"| 产品税率 / 服务税率 | 16% / 6% |",
        f"| 税价合计 | {result.total_with_tax:,.2f} |",
        f"| 产生费用 | 0.00 |",
        f"| 最终价格 | {result.total_with_tax:,.2f} |",
        "",
        "## 2. 与真实 CRM 报价比对",
        "",
        f"- 真实 CRM 中 {result.sample_name} 共 {len(gold)} 行；本工具生成 {len(result.lines)} 行。",
        f"- 项目名称一致 {name_ok}/{compared}，含税单价一致 {price_ok}/{compared}，数量一致 {qty_ok}/{compared}，单位映射一致 {unit_ok}/{compared}。",
        "- 价格/数量 100% 来自 DVP 输入列；单位按字典映射（席位→席位/次、袋子→次）。",
        "",
        "## 3. 报价明细",
        "",
        "| # | 试验项目 | 科室 | 单位 | 数量 | 含税单价 | 折扣 | 折后金额 |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for index, line in enumerate(result.lines, start=1):
        lines.append(
            f"| {index} | {line.item_name} | {line.department} | {line.unit} | {line.quantity:g} | "
            f"{line.unit_price:,.2f} | {line.discount:.0f}% | {line.quantity * line.unit_price * line.discount / 100:,.2f} |"
        )
    lines += [
        "",
        "## 4. 交付物",
        "",
        f"- CRM 报价草稿：`{crm_path}`",
        f"- 销售报价表（已填）：`{sales_path}`",
        f"- 结构化明细：`{out_dir / 'quote_lines.json'}`",
        f"- 比对指标：`{out_dir / 'quote_eval.json'}`",
        "",
        "## 5. 说明与限制",
        "",
        "- 销售报价表保留原模板公式（C10/C11/C12/C13/C15），并新增「报价明细」sheet 便于核对。",
        "- 本案例为纯试验服务，产品金额按 0 处理；服务税率 6%。",
        "- 折扣 60% 来自销售政策/合同参数，不在 DVP 中；P0 不自动推断折扣。",
        "- 未做 CRM 写回、审批流和客户侧自动报价；这些不在 P0 范围。",
        "",
    ]
    (out_dir / "quote_report.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"summary": summary, "metrics": metrics, "preview": preview}, ensure_ascii=False, indent=1))
    print(f"out -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
