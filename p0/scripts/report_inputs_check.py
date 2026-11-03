#!/usr/bin/env python3
"""Check a local report-review inbox (Excel/CSV) without touching the network.

Usage:
    python scripts/report_inputs_check.py --dir /path/to/inbox
    python scripts/report_inputs_check.py --dir ./inbox --json

Exit code 0 when no errors; 1 when a file cannot be read or a required column
is missing.  Missing files are warnings because phase 0/1 may start with a
partial inbox.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.report_inputs import load_inputs


def _text_report(snapshot) -> str:
    lines = [f"数据接入检查：{snapshot.source_dir}", ""]
    for name, status in snapshot.statuses.items():
        label = status.get("label", name)
        file_name = status.get("file") or "未找到"
        lines.append(f"- {label}：{status.get('rows', 0)} 行（{file_name}）")
        if status.get("missing_required"):
            lines.append(f"  缺少必填列：{'、'.join(status['missing_required'])}")
        if status.get("duplicates"):
            sample = "、".join(list(status["duplicates"])[:5])
            lines.append(f"  重复主键 {len(status['duplicates'])} 个：{sample}")
        if status.get("skipped_empty_key"):
            lines.append(f"  跳过无主键行：{status['skipped_empty_key']}")
    if snapshot.warnings:
        lines += ["", "警告："] + [f"- {item}" for item in snapshot.warnings]
    if snapshot.errors:
        lines += ["", "错误："] + [f"- {item}" for item in snapshot.errors]
    lines += ["", "结论：" + ("存在错误，需修复后再接入" if snapshot.errors else "可接入（警告请业务确认）")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查报告审核本地数据收件目录")
    parser.add_argument("--dir", required=True, help="包含 task_orders/original_records/samples/equipment 的目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    snapshot = load_inputs(Path(args.dir))
    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_text_report(snapshot))
    return 1 if snapshot.errors else 0


if __name__ == "__main__":
    sys.exit(main())
