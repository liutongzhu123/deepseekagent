"""PostgreSQL repository for knowledge-Q&A feedback.

One row per answer rating (helpful / not helpful).  The question and answer are
truncated by the API before they reach the store; citations keep the standard
id + clause so an operator can reproduce what was shown.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select

from . import db


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return db.utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value or "")


def create(record: dict) -> dict:
    db.ensure_schema()
    with db.engine().begin() as conn:
        conn.execute(
            insert(db.qa_feedback).values(
                id=record["id"],
                at=_as_datetime(record.get("at")),
                user_name=str(record.get("user", "")),
                role=str(record.get("role", "")),
                rating=str(record.get("rating", "")),
                reason=str(record.get("reason", "")),
                comment=str(record.get("comment", "")),
                question=str(record.get("question", "")),
                answer_excerpt=str(record.get("answer_excerpt", "")),
                standard_id=str(record.get("standard_id", "")),
                clause=str(record.get("clause", "")),
                retrieval_mode=str(record.get("retrieval_mode", "")),
                citations=record.get("citations") or [],
                project=str(record.get("project", "")),
            )
        )
    return record


def list_feedback() -> list[dict]:
    db.ensure_schema()
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.qa_feedback).order_by(db.qa_feedback.c.at.desc())
        ).mappings()
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "at": _iso(row["at"]),
                    "user": row["user_name"],
                    "role": row["role"],
                    "rating": row["rating"],
                    "reason": row["reason"],
                    "comment": row["comment"],
                    "question": row["question"],
                    "answer_excerpt": row["answer_excerpt"],
                    "standard_id": row["standard_id"],
                    "clause": row["clause"],
                    "retrieval_mode": row["retrieval_mode"],
                    "citations": row["citations"] or [],
                    "project": row["project"],
                }
            )
    return out
