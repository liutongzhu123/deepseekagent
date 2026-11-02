#!/usr/bin/env python3
"""Self-check the multi-format quote assistant against real local materials.

Usage:
  python3 scripts/run_quote_multiformat_demo.py \
    --files "/path/to/DVP.xlsx" "/path/to/报价单.pdf" "/path/to/询价.png" \
    --out-dir "/path/to/deliverables/quote-multiformat"

Or point it at a directory (a bounded number of supported files is used):
  python3 scripts/run_quote_multiformat_demo.py --inputs "/path/to/materials" --limit 12 ...

The report contains real item names and is therefore written only to the
local output directory, never to the repository.  The script itself has no
customer data and only exercises the public parser.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.quote import SUPPORTED_SUFFIXES, parse_quote  # noqa: E402

_SKIP_PARTS = {".git", "node_modules", "vendor", "__pycache__", ".venv"}


def collect_files(inputs: list[Path], limit: int) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.is_file():
            if item.suffix.lower() in SUPPORTED_SUFFIXES:
                files.append(item)
            continue
        for path in sorted(item.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            files.append(path)
            if len(files) >= limit:
                return files
    return files[:limit]


def run_one(path: Path) -> dict:
    started = time.perf_counter()
    record: dict = {
        "file": path.name,
        "size_kb": round(path.stat().st_size / 1024, 1),
        "extension": path.suffix.lower(),
    }
    try:
        result = parse_quote(path)
        record.update(
            {
                "ok": True,
                "method": result.parse_method,
                "sample": result.sample_name,
                "lines": len(result.lines),
                "priced_lines": result.priced_lines,
                "unpriced_lines": result.unpriced_lines,
                "all_priced": result.all_priced,
                "warnings": result.warnings,
                "items": [
                    {
                        "item": line.item_name,
                        "standard": line.standard,
                        "unit": line.unit,
                        "quantity": line.quantity,
                        "unit_price": line.unit_price,
                        "needs_price": line.needs_price,
                    }
                    for line in result.lines[:20]
                ],
            }
        )
    except Exception as exc:  # noqa: BLE001 - report each file independently
        record.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    record["elapsed_s"] = round(time.perf_counter() - started, 2)
    return record


def markdown(records: list[dict], root: str) -> str:
    lines = [
        "# 报价助手多格式解析自测",
        "",
        f"- 输入目录：`{root}`（真实客户材料，不提交仓库）",
        f"- 文件数：{len(records)}",
        "",
        "| 文件 | 格式 | 解析方式 | 明细行 | 含单价 | 耗时(s) | 结论 |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        if record.get("ok"):
            outcome = "通过" if record["all_priced"] else f"待补单价 {record['unpriced_lines']} 行"
            lines.append(
                f"| {record['file']} | {record['extension']} | {record['method']} | "
                f"{record['lines']} | {record['priced_lines']}/{record['lines']} | "
                f"{record['elapsed_s']} | {outcome} |"
            )
        else:
            lines.append(
                f"| {record['file']} | {record['extension']} | — | — | — | "
                f"{record['elapsed_s']} | 失败：{record.get('error', '')} |"
            )
    lines += ["", "## 逐文件明细", ""]
    for record in records:
        lines.append(f"### {record['file']}")
        if not record.get("ok"):
            lines.append(f"- 失败：{record.get('error', '')}")
            lines.append("")
            continue
        lines.append(f"- 解析方式：{record['method']}")
        lines.append(f"- 样品：{record['sample']}")
        lines.append(f"- 明细：{record['lines']} 行，含单价 {record['priced_lines']} 行")
        for warning in record.get("warnings", []):
            lines.append(f"- 提示：{warning}")
        lines.append("")
        lines.append("| 检验项目 | 标准 | 单位 | 数量 | 单价 | 状态 |")
        lines.append("|---|---|---|---|---|---|")
        for item in record.get("items", []):
            price = "待补单价" if item["needs_price"] else item["unit_price"]
            lines.append(
                f"| {item['item']} | {item['standard']} | {item['unit']} | "
                f"{item['quantity']} | {price} | {'待补' if item['needs_price'] else '已计价'} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="*", default=[], help="directories to scan")
    parser.add_argument("--files", nargs="*", default=[], help="explicit files")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    inputs = [Path(item).expanduser() for item in args.inputs + args.files]
    if not inputs:
        parser.error("provide --inputs and/or --files")
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files(inputs, args.limit)
    if not files:
        print("没有找到支持的报价文件", file=sys.stderr)
        return 2
    records = [run_one(path) for path in files]
    (out_dir / "selfcheck.json").write_text(
        json.dumps({"inputs": [str(item) for item in inputs], "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "selfcheck.md").write_text(
        markdown(records, str(inputs[0].parent if inputs[0].is_file() else inputs[0])),
        encoding="utf-8",
    )
    passed = sum(1 for record in records if record.get("ok"))
    priced = sum(1 for record in records if record.get("ok") and record["all_priced"])
    print(f"解析成功 {passed}/{len(records)} 个；其中全部含单价 {priced} 个；报告：{out_dir / 'selfcheck.md'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
