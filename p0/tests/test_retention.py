"""Audit CSV streaming and task retention helpers."""
from __future__ import annotations

from app import auth


def test_audit_csv_stream_matches_in_memory_export():
    entries = [
        {"at": "2026-11-03T08:00:00+00:00", "user": "alice", "role": "admin", "event": "login_success", "status": 200},
        {"at": "2026-11-03T08:01:00+00:00", "user": "=cmd", "role": "viewer", "event": "quote_downloaded", "status": 200},
    ]
    stream = "".join(auth.audit_csv_stream(entries))
    assert stream == "\ufeff" + auth.audit_csv(entries)
    assert "'=cmd" in stream


def test_audit_csv_stream_handles_empty_export():
    stream = list(auth.audit_csv_stream([]))
    assert len(stream) == 1
    assert stream[0].startswith("\ufeff")
    assert "\n" in stream[0]
