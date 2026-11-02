#!/usr/bin/env python3
"""Run the P0 report-review demo over real odour report drafts.

Usage:
  python3 scripts/run_report_demo.py \
    --reports-dir "/path/to/reports" \
    --out-dir "/path/to/deliverables/report_review"
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.docx_parser import parse_docx  # noqa: E402
from app.report_writer import audit_markdown, batch_markdown, batch_summary  # noqa: E402
from app.rules import RULES, audit_report  # noqa: E402

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sample", default="", help="file name to use for the sample audit report; default first found")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for path in sorted(reports_dir.rglob("*.docx")):
        if path.name.startswith(".~"):
            continue
        try:
            data = parse_docx(path)
            data.file_name = str(path.relative_to(reports_dir))
            results.append(audit_report(data))
        except Exception as exc:  # noqa: BLE001 - batch boundary
            print(f"skip {path}: {exc}")

    if not results:
        print("no docx reports found")
        return 1

    (out_dir / "audit_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows, rule_fail = batch_summary(results)
    (out_dir / "batch_summary.md").write_text(batch_markdown(rows, rule_fail, RULES), encoding="utf-8")
    with (out_dir / "batch_summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    sample = next((result for result in results if Path(result["file"]).name == args.sample), results[0])
    decisions = {
        "R01": "示例：确认，补齐各页页眉编号",
        "R02": "示例：确认，补填检验均值 3.8",
        "R03": "示例：确认，替换 XXXXX/XXXX 占位符",
        "R04": "示例：确认，核对送样日期（应为 2025-02-24）",
        "R09": "示例：确认，签发前清除高亮/红字",
        "R10": "示例：确认，嵌入签名/印章图片",
    }
    (out_dir / "audit_report_sample.md").write_text(
        audit_markdown(sample, {"file_name": sample["file"]}, decisions), encoding="utf-8"
    )
    (out_dir / "review_notes.md").write_text(
        "\n".join(
            [
                "# 报告审核人工复核说明（示例）",
                "",
                "样本：气味 OTS 草稿（5 页）。",
                "",
                "| 规则 | 规则结论 | 复核方式 | 复核结论 |",
                "|---|---|---|---|",
                "| R01 | 正文页眉报告编号为空 | 直接读取 word/document.xml 的页面段落 | 真实缺陷：编号只在封面 |",
                "| R02 | 检验均值空 | 读取结果表：评价员 4.0/4.0/3.5，均值单元格为空 | 真实缺陷：应为 3.8 |",
                "| R03 | 摘要页 XXXXX vs 样品表 XXXX | 对比两张表的样品名称/型号/生产企业/数量/生产日期 | 真实缺陷：占位符未替换 |",
                "| R04 | 生产 2024-11-25 晚于送样 2024-02-24 | 读取摘要/样品表日期 | 真实缺陷：送样日期疑似应为 2025-02-24 |",
                "| R09 | 高亮 33 处、红字 8 处 | 统计 w:highlight / w:color | 真实草稿标记，签发前需处理 |",
                "| R10 | 外部图片链接 21 个、嵌入图片 0 个 | 统计 INCLUDEPICTURE 与 word/media | 真实缺陷：签名/印章未嵌入 |",
                "",
                "> 示例审核员意见仅用于演示流程，不代表真实签字；正式报告须由授权审核员/签字人确认。",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"audited {len(results)} drafts")
    print("rule fails:", json.dumps(rule_fail, ensure_ascii=False))
    print("sample:", sample["file"], "fail=", sample["summary"]["fail"])
    print(f"out -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
