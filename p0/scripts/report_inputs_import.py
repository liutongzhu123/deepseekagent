#!/usr/bin/env python3
"""Import the local report-review inbox with idempotent business keys."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report_inputs import import_inputs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入报告审核四类本地数据（幂等）")
    parser.add_argument("--dir", required=True, help="inbox 目录")
    parser.add_argument(
        "--state",
        default=os.getenv("P0_REPORT_INPUTS_STATE", str(ROOT / "data" / "report_inputs" / "imported.json")),
        help="导入状态 JSON（只存业务主键）",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    result = import_inputs(args.dir, args.state)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        counts = result["counts"]
        print(f"success={counts['success']} skipped={counts['skipped']} failed={counts['failed']}")
        for item in result["failed"][:20]:
            print(f"FAIL {item['dataset']} row={item['row']}: {item['reason']}")
        for item in result["skipped"][:20]:
            print(f"SKIP {item['dataset']} row={item['row']} key={item['key']}: {item['reason']}")
    return 1 if result["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
