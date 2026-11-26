#!/usr/bin/env python3
"""Build a read-only preview for legacy P0 account role review.

This utility never changes the account store. It reads a deliberately small
CSV schema and emits a JSON preview to a new mode-0600 file outside the
repository. Every account and data scope remains subject to
person-by-person confirmation before any separate migration procedure.

CSV columns: username, role; optional: department, projects, active.
Passwords, email addresses, and unknown columns are rejected.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import stat
import sys
from pathlib import Path
from typing import TextIO


REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COLUMNS = {"username", "role"}
OPTIONAL_COLUMNS = {"department", "projects", "active"}
ALLOWED_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS
SUGGESTED_ROLES = {"admin": "admin", "sales": "business_manager"}
REVIEWER_ROLES = {"reviewer"}


def _parse_active(value: str | None) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes", "active"}:
        return True
    if normalized in {"false", "0", "no", "inactive"}:
        return False
    if not normalized:
        return None
    raise ValueError(f"active must be true/false or blank, got {value!r}")


def _split_projects(value: str | None) -> list[str]:
    projects: list[str] = []
    for item in (value or "").split(","):
        project = item.strip()
        if project and project not in projects:
            projects.append(project)
    return projects


def build_preview(source: TextIO) -> list[dict[str, object]]:
    reader = csv.DictReader(source)
    columns = reader.fieldnames
    if not columns:
        raise ValueError("CSV header is required")
    if len(columns) != len(set(columns)):
        raise ValueError("CSV contains duplicate column names")
    missing = REQUIRED_COLUMNS - set(columns)
    unknown = set(columns) - ALLOWED_COLUMNS
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unsupported columns: {', '.join(sorted(unknown))}")

    preview: list[dict[str, object]] = []
    seen_usernames: set[str] = set()
    for line_number, raw in enumerate(reader, start=2):
        if None in raw:
            raise ValueError(f"line {line_number}: row contains more fields than the header")
        username = (raw.get("username") or "").strip()
        legacy_role = (raw.get("role") or "").strip().lower()
        if not username or len(username) > 64:
            raise ValueError(f"line {line_number}: username must contain 1-64 characters")
        username_key = username.casefold()
        if username_key in seen_usernames:
            raise ValueError(f"line {line_number}: duplicate username {username!r}")
        seen_usernames.add(username_key)
        if not legacy_role:
            raise ValueError(f"line {line_number}: role is required")

        active = _parse_active(raw.get("active"))
        suggested_role = SUGGESTED_ROLES.get(legacy_role, "")
        if legacy_role in REVIEWER_ROLES:
            review_status = "must_confirm_per_account; no_grant_included"
        elif legacy_role in {"viewer"} or not suggested_role:
            review_status = "not_applicable; no_automatic_role_mapping"
        else:
            review_status = "not_applicable"

        if active is False:
            decision = "inactive_account_requires_separate_review"
        elif suggested_role:
            decision = "confirm_role_and_scope_per_account"
        else:
            decision = "manual_role_mapping_required"

        preview.append(
            {
                "username": username,
                "legacy_role": legacy_role,
                "suggested_role": suggested_role,
                "department_for_review": (raw.get("department") or "").strip(),
                "source_projects_for_review": _split_projects(raw.get("projects")),
                "scope_status": "must_confirm_per_account",
                "active_in_source": active,
                "decision": decision,
                "review_permission_status": review_status,
                "preview_only": True,
            }
        )
    if not preview:
        raise ValueError("CSV contains no account rows")
    return preview


def _write_new_private_file(path: Path, payload: str) -> None:
    resolved = path.expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError("preview output must be outside the repository")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(resolved, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
    except Exception:
        try:
            resolved.unlink()
        except OSError:
            pass
        raise


def _require_private_regular_input(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    details = resolved.stat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("input must be a regular file")
    if details.st_mode & 0o077:
        raise ValueError("input file must have no group/other permissions; run chmod 600")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="CSV export of legacy accounts")
    parser.add_argument(
        "--output", type=Path, required=True,
        help="write a new private JSON file outside the repository (mode 0600)",
    )
    args = parser.parse_args(argv)
    try:
        input_path = _require_private_regular_input(args.csv_file)
        with input_path.open("r", encoding="utf-8-sig", newline="") as source:
            preview = build_preview(source)
        payload = json.dumps(
            {"preview_only": True, "account_count": len(preview), "accounts": preview},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        _write_new_private_file(args.output, payload)
        print(f"只读预览已写入新文件（0600）：{args.output.expanduser().resolve()}")
        return 0
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"migration preview failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
