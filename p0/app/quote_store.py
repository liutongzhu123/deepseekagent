"""Quote persistence seam: file directory (P0) or PostgreSQL + object store.

``app.main`` used to write ``data/quotes/<id>/`` directly.  Routing those
operations through this module keeps the JSON layout byte-for-byte compatible
while allowing ``P0_STORE=postgres`` to store the metadata row and workbooks in
the P1 data layer.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import store

ROOT = Path(__file__).resolve().parent.parent
QUOTE_DIR = Path(os.getenv("P0_QUOTE_DIR", ROOT / "data" / "quotes"))
_ID_RE = re.compile(r"^q_[0-9]{14}_[0-9a-f]{6}$")


def _dir(quote_id: str, base_dir: Path | None = None) -> Path:
    if not _ID_RE.fullmatch(quote_id or ""):
        raise KeyError(quote_id)
    return (base_dir or QUOTE_DIR) / quote_id


def create(meta: dict, files: dict[str, bytes], base_dir: Path | None = None) -> dict:
    if store.config.postgres_enabled():
        return store.quotes.create(meta, files)
    directory = _dir(meta["quote_id"], base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return meta


def load(quote_id: str, base_dir: Path | None = None) -> dict:
    if store.config.postgres_enabled():
        return store.quotes.load(quote_id)
    path = _dir(quote_id, base_dir) / "meta.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_file(quote_id: str, name: str, base_dir: Path | None = None) -> bytes:
    if store.config.postgres_enabled():
        return store.quotes.read_file(quote_id, name)
    path = _dir(quote_id, base_dir) / name
    if not path.exists():
        raise KeyError(name)
    return path.read_bytes()
