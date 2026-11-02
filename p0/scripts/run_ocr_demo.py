#!/usr/bin/env python3
"""Run the scanned-standard OCR pipeline and write reviewable evidence.

Usage:

  python3 scripts/run_ocr_demo.py \
    --pdf "/path/to/scan.pdf" \
    --out-dir deliverables/ocr \
    --dpi 220

Outputs:
  ocr_pages.json    per-page text/lines/confidence
  result.json       parsed items with per-field confidence + review flags
  result.xlsx       3-column item sheet + 7-column method sheet + review sheet
  review_report.md  human-readable list of fields needing review
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ocr import OcrUnavailable, ocr_pdf  # noqa: E402
from app.standard_parser import parse_standard  # noqa: E402
from app.xlsx_writer import write_xlsx  # noqa: E402


def write_excel(path: Path, parsed: dict) -> Path:
    meta = parsed["meta"]
    items = parsed["items"]
    summary_rows = [["检验依据", "检验标准名称", "实检项目", "置信度", "需人工复核"]] + [
        [
            meta.get("standard_no", ""),
            meta.get("name", ""),
            item["name"],
            item.get("confidence", ""),
            "是" if item.get("review_required") else "否",
        ]
        for item in items
    ]
    detail_rows = [
        [
            "检验依据",
            "检验标准名称",
            "实检项目",
            "技术要求条款",
            "技术要求/判定要求",
            "试验方法条款",
            "试验方法（具体方法）",
        ]
    ] + [
        [
            meta.get("standard_no", ""),
            meta.get("name", ""),
            item["name"],
            item["clause"],
            item["requirement"],
            item["method_clause"],
            item["method"],
        ]
        for item in items
    ]
    review_rows = [["序号", "条款", "项目", "字段", "字段置信度", "需复核原因"]]
    for item in items:
        for finding in item.get("review_fields", []):
            review_rows.append(
                [
                    item["seq"],
                    item["clause"],
                    item["name"],
                    finding["field"],
                    finding["confidence"],
                    finding["reason"],
                ]
            )
    return write_xlsx(
        path,
        [
            ("实检项目", summary_rows, [16, 28, 34, 10, 12]),
            ("试验方法明细", detail_rows, [16, 28, 32, 12, 60, 12, 60]),
            ("复核清单", review_rows, [6, 12, 30, 16, 12, 46]),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dpi", type=int, default=220)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.9)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    data = pdf_path.read_bytes()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        ocr_result = ocr_pdf(data, dpi=args.dpi, max_pages=args.max_pages or None)
    except OcrUnavailable as exc:
        print(f"OCR unavailable: {exc}", file=sys.stderr)
        return 2
    ocr_meta = ocr_result.meta()
    (out_dir / "ocr_pages.json").write_text(
        json.dumps(ocr_result.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"OCR: {ocr_meta['pages']} pages, avg confidence {ocr_meta['avg_confidence']:.4f}", flush=True)

    parsed = parse_standard(
        ocr_result.page_texts(),
        pdf_path.name,
        ocr=ocr_meta,
        page_confidence=ocr_result.page_confidences(),
    )
    parsed["meta"]["source_file_name"] = pdf_path.name
    parsed["meta"]["source_bytes"] = len(data)
    parsed["meta"]["ocr"]["elapsed_seconds"] = round(time.time() - started, 1)
    (out_dir / "result.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_excel(out_dir / "result.xlsx", parsed)

    low_items = [item for item in parsed["items"] if item.get("review_required")]
    lines = [
        "# 扫描件 OCR 结构化结果与人工复核清单",
        "",
        f"- 输入：`{pdf_path.name}`（{len(data) / 1024 / 1024:.2f} MB）",
        f"- OCR：`{ocr_meta['engine']}`，{ocr_meta['pages']} 页，"
        f"平均置信度 **{ocr_meta['avg_confidence']:.4f}**，DPI {ocr_meta['dpi']}，"
        f"耗时 {ocr_meta['elapsed_ms'] / 1000:.1f}s",
        f"- 解析：**{parsed['summary']['items']}** 个检验项目；"
        f"有要求 {parsed['summary']['items_with_requirement']} 个，"
        f"有方法 {parsed['summary']['items_with_method']} 个，"
        f"条款可溯源 {parsed['summary']['clauses_traced']} 个",
        f"- 需人工复核：**{parsed['summary']['review_required_items']}** 个项目，"
        f"{parsed['summary']['review_fields']} 个字段级提示",
        f"- 低置信度页：{ocr_meta['low_confidence_pages'] or '无'}",
        "",
        "## 字段级复核清单",
        "",
        "| 条款 | 项目 | 字段 | 置信度 | 原因 |",
        "|---|---|---|---:|---|",
    ]
    for item in low_items:
        for finding in item.get("review_fields", []):
            lines.append(
                f"| {item['clause']} | {item['name']} | {finding['field']} | "
                f"{finding['confidence']:.3f} | {finding['reason']} |"
            )
    lines += [
        "",
        "## 解析告警",
        "",
        *[f"- {warning}" for warning in parsed.get("warnings", [])],
        "",
        "## 说明",
        "",
        "- OCR 运行在本地 `rapidocr_onnxruntime`（PP-OCR 模型），不调用云 OCR、不产生费用。",
        "- 置信度来自 OCR 行置信度；字段置信度再结合章节/项目识别是否完整。",
        "- 表格数值未做单元格级结构化，涉及表格的字段已标为需复核。",
        "",
    ]
    (out_dir / "review_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "ocr": ocr_meta,
                "summary": parsed["summary"],
                "mapping_source": parsed["mapping_source"],
                "warnings": parsed["warnings"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"out -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
