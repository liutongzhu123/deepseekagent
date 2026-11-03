#!/usr/bin/env python3
"""Inspect a local CRM Excel/CSV export with the same adapter used by the API.

    python scripts/crm_inspect.py --mode excel --path /path/crm.xlsx
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crm  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="excel", choices=["excel", "csv", "off", "api"])
    parser.add_argument("--path", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--query", default="")
    args = parser.parse_args(argv)
    os.environ["P0_CRM_MODE"] = args.mode
    if args.path:
        os.environ["P0_CRM_PATH"] = args.path
    status = crm.status()
    output = {"status": status}
    if status.get("configured"):
        snapshot = crm.provider().snapshot()
        output["projects"] = crm.read_projects(snapshot)
        output["prices"] = crm.read_prices(snapshot, project=args.project, query=args.query)[:20]
        output["qualifications"] = crm.read_qualifications(snapshot)
        output["schedules"] = crm.read_schedules(snapshot)
    print(json.dumps(output, ensure_ascii=False, indent=1))
    return 0 if status.get("configured") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
