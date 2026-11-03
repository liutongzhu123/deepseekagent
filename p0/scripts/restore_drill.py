#!/usr/bin/env python3
"""P1-6 restore drill: verify file and data-layer backups, optionally restore files.

    # verify the latest archives (read-only, no service stop)
    python scripts/restore_drill.py --backup-dir /var/backups/cq-ai-p0

    # also restore the latest file snapshot into a temporary directory
    python scripts/restore_drill.py --backup-dir /var/backups/cq-ai-p0 --restore-files

The data-layer manifest is verified with ``restore_store.py --verify-only``;
a full PostgreSQL restore still requires a disposable database and is documented
in ``docs/p1-ops-security/README.md``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(argv: list[str]) -> dict:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return {
        "command": " ".join(argv),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-4000:],
    }


def latest(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern), reverse=True)
    return files[0] if files else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--file-backup", default="")
    parser.add_argument("--store-manifest", default="")
    parser.add_argument("--restore-files", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    backup_dir = Path(args.backup_dir).expanduser()
    report: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup_dir": str(backup_dir),
        "checks": [],
    }
    file_backup = (
        Path(args.file_backup).expanduser()
        if args.file_backup
        else latest(backup_dir, "p0-data-*.tar.gz")
    )
    store_manifest = (
        Path(args.store_manifest).expanduser()
        if args.store_manifest
        else latest(backup_dir, "p0-store-*.json")
    )
    if file_backup and file_backup.exists():
        result = run([sys.executable, str(ROOT / "scripts" / "restore_data.py"), str(file_backup), "--verify-only"])
        report["checks"].append({"name": "file_snapshot", "archive": file_backup.name, **result})
        if args.restore_files and result["returncode"] == 0:
            with tempfile.TemporaryDirectory(prefix="p0-restore-drill-") as tmp:
                restored = run(
                    [sys.executable, str(ROOT / "scripts" / "restore_data.py"), str(file_backup), "--data-dir", tmp]
                )
                files = sum(1 for path in Path(tmp).rglob("*") if path.is_file())
                report["checks"].append(
                    {"name": "file_restore", "target": tmp, "files": files, **restored}
                )
    else:
        report["checks"].append({"name": "file_snapshot", "returncode": 1, "stderr": "archive not found"})
    if store_manifest and store_manifest.exists():
        result = run(
            [sys.executable, str(ROOT / "scripts" / "restore_store.py"), "--backup-dir", str(backup_dir), "--manifest", str(store_manifest), "--verify-only"]
        )
        report["checks"].append({"name": "data_layer_snapshot", "manifest": store_manifest.name, **result})
    else:
        report["checks"].append({"name": "data_layer_snapshot", "returncode": 1, "stderr": "manifest not found"})
    report["ok"] = all(check["returncode"] == 0 for check in report["checks"])
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
