"""File-store to PostgreSQL/object-store migration, reconciliation and rollback.

The migration is deliberately conservative:

* the JSON tree is never deleted or moved; PostgreSQL mode also mirrors new
  writes back to it, so ``P0_STORE=json`` remains a valid rollback;
* every import is idempotent (users/standards/quotes upsert by id, audit rows
  are tagged with their JSONL sequence number);
* ``verify`` compares counts, ids, document bytes and sampled hashes in both
  directions and returns a machine-readable report used by the CLI and tests;
* ``export_to_files`` can rebuild the JSON tree from PostgreSQL when a rollback
  must include writes made after the original migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import audit, db, objects, quotes, standards, users

STANDARD_ID_RE = re.compile(r"^std_[0-9]{14}_[0-9a-f]{6}$")
QUOTE_ID_RE = re.compile(r"^q_[0-9]{14}_[0-9a-f]{6}$")


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _users_path(data_dir: Path) -> Path:
    return Path(os.getenv("P0_AUTH_USERS", data_dir / "auth" / "users.json"))


def _audit_path(data_dir: Path) -> Path:
    return Path(os.getenv("P0_AUDIT_LOG", data_dir / "audit" / "audit.jsonl"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


def _audit_lines(path: Path) -> list[tuple[int, dict]]:
    if not path.exists():
        return []
    out: list[tuple[int, dict]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append((index, record))
    return out


def _standard_dirs(data_dir: Path) -> list[Path]:
    root = data_dir / "standards"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and STANDARD_ID_RE.fullmatch(path.name) and (path / "result.json").exists()
    )


def _quote_dirs(data_dir: Path) -> list[Path]:
    root = data_dir / "quotes"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and QUOTE_ID_RE.fullmatch(path.name) and (path / "meta.json").exists()
    )


def plan(data_dir: Path) -> dict:
    """Describe what a migration would import, without touching the database."""
    user_rows = (_json(_users_path(data_dir), {}) or {}).get("users", [])
    standard_rows = _standard_dirs(data_dir)
    quote_rows = _quote_dirs(data_dir)
    audit_rows = _audit_lines(_audit_path(data_dir))
    return {
        "data_dir": str(data_dir),
        "users": len([user for user in user_rows if user.get("username")]),
        "audit_events": len(audit_rows),
        "standards": len(standard_rows),
        "standard_sources": sum(1 for path in standard_rows if (path / "source.pdf").exists()),
        "quotes": len(quote_rows),
        "quote_files": sum(
            len([name for name in path.iterdir() if name.is_file() and name.name not in {"meta.json"}])
            for path in quote_rows
        ),
    }


def migrate(data_dir: Path, dry_run: bool = False) -> dict:
    """Import the file layout into PostgreSQL + object storage."""
    counts = plan(data_dir)
    if dry_run:
        return {**counts, "dry_run": True, "imported": {}}

    db.ensure_schema()
    imported = {"users": 0, "audit_events": 0, "standards": 0, "quotes": 0, "errors": []}

    user_rows = [
        user
        for user in ((_json(_users_path(data_dir), {}) or {}).get("users", []) or [])
        if isinstance(user, dict) and user.get("username")
    ]
    users.save_users(user_rows)
    imported["users"] = len(user_rows)

    for seq, record in _audit_lines(_audit_path(data_dir)):
        if audit.import_entry(record, "audit.jsonl", seq):
            imported["audit_events"] += 1

    for directory in _standard_dirs(data_dir):
        try:
            record = _json(directory / "result.json")
            if not isinstance(record, dict) or record.get("id") != directory.name:
                raise ValueError("result.json id mismatch")
            source_path = directory / "source.pdf"
            if not source_path.exists():
                raise ValueError("source.pdf missing")
            kb_path = directory / "kb.json"
            export_path = directory / "export.xlsx"
            standards.import_record(
                record,
                source_path.read_bytes(),
                _json(kb_path) if kb_path.exists() else None,
                export_path.read_bytes() if export_path.exists() else None,
            )
            imported["standards"] += 1
        except Exception as exc:  # noqa: BLE001 - report and continue
            imported["errors"].append(f"standard {directory.name}: {exc}")

    for directory in _quote_dirs(data_dir):
        try:
            meta = _json(directory / "meta.json")
            if not isinstance(meta, dict) or meta.get("quote_id") != directory.name:
                raise ValueError("meta.json quote_id mismatch")
            files = {
                path.name: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file() and path.name not in {"meta.json"} and not path.name.endswith(".tmp")
            }
            quotes.import_record(meta, files)
            imported["quotes"] += 1
        except Exception as exc:  # noqa: BLE001 - report and continue
            imported["errors"].append(f"quote {directory.name}: {exc}")

    return {**counts, "dry_run": False, "imported": imported}


def _object_bytes(owner_type: str, owner_id: str, name: str) -> bytes | None:
    with db.engine().connect() as conn:
        record = objects.find_file(conn, owner_type, owner_id, name)
        if record is None:
            return None
        _, data = objects.read_file(conn, record["id"])
        return data


def _compare(label: str, left, right, diffs: list[str], limit: int = 20) -> None:
    if left == right:
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                diffs.append(f"{label}.{key}: file={left.get(key)!r} db={right.get(key)!r}"[:400])
                if len(diffs) >= limit:
                    return
        return
    diffs.append(f"{label}: mismatch"[:400])


def verify(data_dir: Path, sample_limit: int = 20) -> dict:
    """Compare the file tree with PostgreSQL/object storage in both directions."""
    db.ensure_schema()
    diffs: list[str] = []
    report = plan(data_dir)

    file_users = [
        user
        for user in ((_json(_users_path(data_dir), {}) or {}).get("users", []) or [])
        if isinstance(user, dict) and user.get("username")
    ]
    db_users = users.load_users()
    file_names = sorted(user.get("username", "") for user in file_users)
    db_names = sorted(user.get("username", "") for user in db_users)
    if file_names != db_names:
        diffs.append(f"users ids: file={file_names!r} db={db_names!r}"[:400])
    by_name = {user["username"]: user for user in db_users}
    for user in file_users:
        _compare(f"user[{user['username']}]", user, by_name.get(user["username"], {}), diffs, sample_limit)

    file_audit = _audit_lines(_audit_path(data_dir))
    db_audit = audit.all_entries()
    file_docs = sorted(json.dumps(record, ensure_ascii=False, sort_keys=True) for _, record in file_audit)
    db_docs = sorted(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in db_audit)
    if len(file_docs) != len(db_docs):
        diffs.append(f"audit count: file={len(file_docs)} db={len(db_docs)}")
    else:
        for index, (left, right) in enumerate(zip(file_docs, db_docs)):
            if left != right:
                diffs.append(f"audit[{index + 1}]: file={left[:180]} db={right[:180]}")
                if len(diffs) >= sample_limit:
                    break

    file_standards = {path.name: path for path in _standard_dirs(data_dir)}
    db_standards = {row["id"] for row in standards.list_standards()}
    missing = sorted(set(file_standards) - db_standards)
    extra = sorted(db_standards - set(file_standards))
    if missing:
        diffs.append(f"standards missing in db: {missing[:sample_limit]!r}")
    if extra:
        diffs.append(f"standards extra in db: {extra[:sample_limit]!r}")
    for standard_id, directory in file_standards.items():
        if standard_id not in db_standards:
            continue
        record = _json(directory / "result.json")
        loaded = standards.load(standard_id)
        _compare(f"standard[{standard_id}]", record, loaded, diffs, sample_limit)
        file_kb = _json(directory / "kb.json") if (directory / "kb.json").exists() else None
        _compare(f"standard[{standard_id}].kb", file_kb, standards.get_kb(standard_id), diffs, sample_limit)
        if (directory / "source.pdf").exists():
            source = _object_bytes("standard", standard_id, "source.pdf")
            if source is None:
                diffs.append(f"standard[{standard_id}].source missing in object store")
            elif _sha(source) != _file_sha(directory / "source.pdf"):
                diffs.append(f"standard[{standard_id}].source hash mismatch")
        if (directory / "export.xlsx").exists():
            export = _object_bytes("standard", standard_id, "export.xlsx")
            if export is None:
                diffs.append(f"standard[{standard_id}].export missing in object store")
            elif _sha(export) != _file_sha(directory / "export.xlsx"):
                diffs.append(f"standard[{standard_id}].export hash mismatch")

    file_quotes = {path.name: path for path in _quote_dirs(data_dir)}
    db_quote_ids = {quote_id for quote_id in quotes.all_ids()}
    missing = sorted(set(file_quotes) - db_quote_ids)
    extra = sorted(db_quote_ids - set(file_quotes))
    if missing:
        diffs.append(f"quotes missing in db: {missing[:sample_limit]!r}")
    if extra:
        diffs.append(f"quotes extra in db: {extra[:sample_limit]!r}")
    for quote_id, directory in file_quotes.items():
        if quote_id not in db_quote_ids:
            continue
        _compare(
            f"quote[{quote_id}]",
            _json(directory / "meta.json"),
            quotes.load(quote_id),
            diffs,
            sample_limit,
        )
        file_names = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.name not in {"meta.json"} and not path.name.endswith(".tmp")
        )
        db_names = quotes.file_names(quote_id)
        if file_names != db_names:
            diffs.append(f"quote[{quote_id}] files: file={file_names!r} db={db_names!r}"[:400])
        for name in file_names:
            stored = _object_bytes("quote", quote_id, name)
            if stored is None:
                diffs.append(f"quote[{quote_id}]/{name} missing in object store")
            elif _sha(stored) != _file_sha(directory / name):
                diffs.append(f"quote[{quote_id}]/{name} hash mismatch")

    report.update(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": not diffs,
            "diffs": diffs,
            "db_counts": {
                "users": len(db_users),
                "audit_events": len(audit.all_entries()),
                "standards": len(db_standards),
                "quotes": len(db_quote_ids),
            },
        }
    )
    return report


def export_to_files(target: Path, force: bool = False) -> dict:
    """Write the current PostgreSQL state back into the P0 JSON/file layout."""
    db.ensure_schema()
    target = Path(target)
    if target.exists() and any(target.iterdir()) and not force:
        raise RuntimeError(f"target directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    counts = {"users": 0, "audit_events": 0, "standards": 0, "quotes": 0}

    (target / "auth").mkdir(parents=True, exist_ok=True)
    user_rows = users.load_users()
    (target / "auth" / "users.json").write_text(
        json.dumps({"users": user_rows}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    counts["users"] = len(user_rows)

    audit_rows = audit.all_entries()
    (target / "audit").mkdir(parents=True, exist_ok=True)
    with (target / "audit" / "audit.jsonl").open("w", encoding="utf-8") as handle:
        for record in audit_rows:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    counts["audit_events"] = len(audit_rows)

    for standard_id in [row["id"] for row in standards.list_standards()]:
        directory = target / "standards" / standard_id
        directory.mkdir(parents=True, exist_ok=True)
        record = standards.load(standard_id)
        (directory / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        kb = standards.get_kb(standard_id)
        if kb is not None:
            (directory / "kb.json").write_text(
                json.dumps(kb, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        source = _object_bytes("standard", standard_id, "source.pdf")
        if source is not None:
            (directory / "source.pdf").write_bytes(source)
        export = _object_bytes("standard", standard_id, "export.xlsx")
        if export is not None:
            (directory / "export.xlsx").write_bytes(export)
        counts["standards"] += 1

    for quote_id in quotes.all_ids():
        directory = target / "quotes" / quote_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = quotes.load(quote_id)
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        for name, data in quotes.files_for(quote_id).items():
            (directory / name).write_bytes(data)
        counts["quotes"] += 1

    return {**counts, "target": str(target), "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def replace_tree(source: Path, target: Path) -> Path:
    """Move an exported tree into place, keeping the previous tree aside."""
    source = Path(source)
    target = Path(target)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(target.name + f".pre-rollback-{stamp}")
    if target.exists():
        shutil.move(str(target), str(backup))
    shutil.move(str(source), str(target))
    return backup
