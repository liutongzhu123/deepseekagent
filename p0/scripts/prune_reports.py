#!/usr/bin/env python3
"""Prune stored report reviews older than the retention window (T303).

Works in both storage modes: ``P0_STORE=json`` scans ``P0_REPORT_DIR`` (default
``data/reports/<id>/{meta,result,source}.json|docx``) and ``P0_STORE=postgres``
deletes ``report_audits`` rows plus their object-store source and JSON mirror.

The default is a dry run.  ``--apply`` removes the records and writes a
``reports_pruned`` audit event.  This script is manual/timer driven; the HTTP
API never deletes history.

    .venv/bin/python scripts/prune_reports.py --days 180
    .venv/bin/python scripts/prune_reports.py --days 180 --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, report_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune report review history")
    parser.add_argument("--days", type=int, default=180, help="keep reports newer than N days")
    parser.add_argument("--apply", action="store_true", help="delete records (default is dry-run)")
    args = parser.parse_args(argv)
    if args.days < 1:
        print("FAIL: --days must be >= 1", file=sys.stderr)
        return 2
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    try:
        eligible = report_store.prunable_ids(cutoff)
    except Exception as exc:  # noqa: BLE001 - storage misconfiguration is a clean failure
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if not args.apply:
        print(f"dry-run: {len(eligible)} report(s) older than {args.days} day(s) would be removed")
        return 0
    removed = report_store.prune_before(cutoff)
    auth.audit(
        {
            "user": "system",
            "role": "system",
            "event": "reports_pruned",
            "method": "CLI",
            "path": "scripts/prune_reports.py",
            "status": 200,
            "rows": removed,
            "days": args.days,
        }
    )
    print(f"removed {removed} report(s) older than {args.days} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
