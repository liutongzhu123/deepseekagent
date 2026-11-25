"""Versioned, non-published drafts for customer-service business rules.

Failure paths: F1 invalid payloads fail before storage; F2 an invalid live
configuration blocks new drafts; F3 corrupt/unwritable draft storage or lock
initialization fails closed; F4 the draft cap is enforced under the
transaction lock; F5 local workers serialize append/read operations with
POSIX flock. Multi-host writes require a transactional shared database.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import threading
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import admin_config

_LOCK = threading.RLock()


@contextmanager
def _locked():
    """Serialize persisted draft operations across local workers."""
    with _LOCK:
        path = _path()
        descriptor = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            lock_path = path.with_name(path.name + ".lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise DraftStoreError("business rule draft store is unavailable") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases the advisory lock. A
                # cleanup error after a successful write must not turn the
                # response into an ambiguous retry that creates another draft.
                pass
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class DraftStoreError(ValueError):
    pass


class DraftValidationError(ValueError):
    pass


def _path() -> Path:
    return Path(admin_config.CONFIG_PATH).with_suffix(".business-drafts.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read() -> dict[str, Any]:
    path = _path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "drafts": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise DraftStoreError("business rule draft store is unavailable") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("drafts"), list)
    ):
        raise DraftStoreError("business rule draft store is invalid")
    return document


def _write(document: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    except OSError as exc:
        raise DraftStoreError("business rule draft store is not writable") from exc


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def create_draft(payload: dict[str, Any], *, created_by: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DraftStoreError("business draft must be an object")
    with _locked():
        snapshot = admin_config.describe()
        if snapshot.get("warning"):
            raise DraftStoreError("current business configuration is invalid")
        live = snapshot["config"]
        current = copy.deepcopy(live["business"])
        merged = admin_config._deep_merge(current, payload)
        try:
            candidate = admin_config.validate_section("business", merged)
        except admin_config.ConfigError as exc:
            raise DraftValidationError(str(exc)) from exc
        document = _read()
        if len(document["drafts"]) >= 500:
            raise DraftStoreError("business draft limit reached")
        draft = {
            "draft_id": "CS-BIZ-DRAFT-" + secrets.token_hex(10),
            "status": "draft",
            "base_revision": int(live.get("revision", 0)),
            "created_at": _now(),
            "created_by": str(created_by),
            "changed_fields": _changed_fields(current, candidate),
            "business": candidate,
        }
        document["drafts"].append(draft)
        _write(document)
        return copy.deepcopy(draft)


def list_drafts() -> list[dict[str, Any]]:
    with _locked():
        document = _read()
        return [
            {key: copy.deepcopy(value) for key, value in draft.items() if key != "business"}
            for draft in reversed(document["drafts"])
        ]


def get_draft(draft_id: str) -> dict[str, Any]:
    with _locked():
        document = _read()
        for draft in document["drafts"]:
            if draft.get("draft_id") == draft_id:
                return copy.deepcopy(draft)
    raise KeyError(draft_id)


def compare_to_live(draft_id: str) -> dict[str, Any]:
    draft = get_draft(draft_id)
    snapshot = admin_config.describe()
    if snapshot.get("warning"):
        raise DraftStoreError("current business configuration is invalid")
    current = snapshot["config"]
    live_business = current["business"]
    candidate = draft["business"]
    return {
        "draft_id": draft["draft_id"],
        "status": draft["status"],
        "base_revision": draft["base_revision"],
        "live_revision": int(current.get("revision", 0)),
        "stale": int(draft["base_revision"]) != int(current.get("revision", 0)),
        "changed_fields_vs_live": _changed_fields(live_business, candidate),
        "candidate": copy.deepcopy(candidate),
        "published": False,
    }
