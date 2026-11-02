#!/usr/bin/env python3
"""Import the P0 JSON/file tree into PostgreSQL + MinIO (or the local object store).

Run with the API stopped, after a verified file snapshot::

    .venv/bin/python scripts/backup_data.py --data-dir /opt/cq-ai-p0/data
    systemctl stop cq-p0-api
    P0_DATABASE_URL=postgresql+psycopg://... \
      .venv/bin/python scripts/migrate_to_postgres.py --data-dir /opt/cq-ai-p0/data
    .venv/bin/python scripts/migrate_to_postgres.py --data-dir /opt/cq-ai-p0/data --verify-only

The command imports users, audit events, standards (record, revisions, clauses,
source/export objects) and quotes, then reconciles the database and object store
against the files.  It never deletes the source tree.  Re-running it is safe:
records upsert by id and audit rows are tagged with their JSONL line number.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import config, migration  # noqa: E402


def _print_report(report: dict) -> None:
    counts = report.get("db_counts") or report.get("imported") or report
    print(json.dumps(counts, ensure_ascii=False, indent=1))
    if report.get("errors"):
        print("errors:", file=sys.stderr)
        for item in report["errors"]:
            print(f"  - {item}", file=sys.stderr)
    if "diffs" in report:
        if report.get("ok"):
            print("reconciliation: OK")
        else:
            print("reconciliation: DIFFERENCES", file=sys.stderr)
            for item in report["diffs"]:
                print(f"  - {item}", file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.getenv("P0_DATA_DIR", ""), help="P0 data directory")
    parser.add_argument("--database-url", default="", help="override P0_DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="count files only")
    parser.add_argument("--verify-only", action="store_true", help="reconcile without importing")
    parser.add_argument("--report", default="", help="write the JSON report to this file")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir or (Path(__file__).resolve().parents[1] / "data")).expanduser()
    if not data_dir.exists():
        print(f"data directory not found: {data_dir}", file=sys.stderr)
        return 2
    os.environ.setdefault("P0_DATA_DIR", str(data_dir))
    if args.database_url:
        os.environ["P0_DATABASE_URL"] = args.database_url
    os.environ["P0_STORE"] = "postgres"
    config.reset_caches()

    if not args.dry_run and not os.getenv("P0_DATABASE_URL", "").strip():
        print("P0_DATABASE_URL (or --database-url) is required", file=sys.stderr)
        return 2

    if args.dry_run:
        report = migration.plan(data_dir)
    elif args.verify_only:
        report = migration.verify(data_dir)
    else:
        report = migration.migrate(data_dir)
        report["verification"] = migration.verify(data_dir)
        report["ok"] = not report["imported"]["errors"] and report["verification"].get("ok")
        report["diffs"] = report["verification"].get("diffs", [])
    _print_report(report)
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if report.get("imported", {}).get("errors"):
        return 1
    if "diffs" in report and not report.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
