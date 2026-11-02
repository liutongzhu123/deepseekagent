#!/usr/bin/env python3
"""Check or rebuild the JSON tree used by ``P0_STORE=json``.

During the transition PostgreSQL mode mirrors every write to the JSON tree, so a
rollback is usually just setting ``P0_STORE=json`` and restarting.  This script
proves the mirror is complete and can export the current database state when it
is not.

Examples::

    # verify that files and PostgreSQL agree (read-only)
    .venv/bin/python scripts/rollback_to_files.py --check

    # export PostgreSQL -> a fresh directory; swap it in after stopping the API
    .venv/bin/python scripts/rollback_to_files.py --export /opt/cq-ai-p0/data.rollback

Rollback steps (documented in ``docs/p1-data-layer/README.md``):

    systemctl stop cq-p0-api
    .venv/bin/python scripts/rollback_to_files.py --export /opt/cq-ai-p0/data.rollback
    mv /opt/cq-ai-p0/data /opt/cq-ai-p0/data.pg-era
    mv /opt/cq-ai-p0/data.rollback /opt/cq-ai-p0/data
    # remove P0_STORE / P0_DATABASE_URL / P0_OBJECT_STORE from .env, then:
    systemctl start cq-p0-api
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import config, migration  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.getenv("P0_DATA_DIR", ""))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--check", action="store_true", help="verify files against PostgreSQL")
    parser.add_argument("--export", default="", help="write the PostgreSQL state to this directory")
    parser.add_argument("--force", action="store_true", help="allow a non-empty export target")
    args = parser.parse_args(argv)
    if not args.check and not args.export:
        args.check = True

    data_dir = Path(args.data_dir or (Path(__file__).resolve().parents[1] / "data")).expanduser()
    os.environ.setdefault("P0_DATA_DIR", str(data_dir))
    if args.database_url:
        os.environ["P0_DATABASE_URL"] = args.database_url
    os.environ["P0_STORE"] = "postgres"
    config.reset_caches()

    if args.check:
        report = migration.verify(data_dir)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report.get("ok") else 1
    try:
        report = migration.export_to_files(Path(args.export), force=args.force)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
