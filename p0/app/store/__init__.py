"""P1 data layer: PostgreSQL + MinIO behind the existing P0 API.

``P0_STORE=json`` keeps the original file-backed behaviour (the default);
``P0_STORE=postgres`` routes users, audit events, standards and quotes through
this package.  ``P0_OBJECT_STORE`` selects a local directory or MinIO for
source and generated files.  The JSON tree is still mirrored during the
transition so a rollback is a single environment switch.
"""
from . import audit, config, db, feedback, objects, quotes, reports, standards, users

__all__ = ["audit", "config", "db", "feedback", "objects", "quotes", "reports", "standards", "users"]
