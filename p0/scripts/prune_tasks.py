#!/usr/bin/env python3
"""Prune finished background tasks (P1 retention).

Queued/running tasks and unresolved leases are never deleted.  The default
dry-run prints how many rows are eligible; ``--apply`` removes them and writes
a ``tasks_pruned`` audit event.

    .venv/bin/python scripts/prune_tasks.py --days 30
    .venv/bin/python scripts/prune_tasks.py --days 30 --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth  # noqa: E402
from app.store import config, tasks  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--status",
        default="succeeded,failed,cancelled",
        help="comma separated finished statuses (queued/running are never pruned)",
    )
    parser.add_argument("--apply", action="store_true", help="delete rows (default is dry-run)")
    args = parser.parse_args(argv)
    if not config.postgres_enabled():
        print("P0_STORE must be postgres: the task queue lives in PostgreSQL", file=sys.stderr)
        return 2
    statuses = tuple(item.strip() for item in args.status.split(",") if item.strip())
    try:
        eligible = tasks.count_prunable(days=args.days, statuses=statuses)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    if not args.apply:
        print(f"dry-run: {eligible} finished task(s) older than {args.days} day(s) would be removed")
        return 0
    removed = tasks.prune(days=args.days, statuses=statuses)
    auth.audit(
        {
            "user": "system",
            "role": "system",
            "event": "tasks_pruned",
            "method": "CLI",
            "path": "scripts/prune_tasks.py",
            "status": 200,
            "rows": removed,
            "days": args.days,
        }
    )
    print(f"removed {removed} task(s) older than {args.days} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
