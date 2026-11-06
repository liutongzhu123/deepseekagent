"""Redis cache for query rewrites (T401b).

The same question is asked repeatedly in practice (retry, several reviewers,
the same question against the same standards).  Caching the *rewrite* result for
five minutes removes the model call from the second and later asks while leaving
retrieval/answer fresh.

The cache is best effort by construction: a missing Redis, a connection error or
a bad payload degrades to "no cache hit" and the caller still rewrites (or falls
back to the original question).  It never raises into the Q&A path.
"""
from __future__ import annotations

import hashlib
import os

DEFAULT_TTL = 300
DEFAULT_PREFIX = "p0:query-rewrite:"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "on", "yes"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class RewriteCache:
    """A tiny Redis facade; ``client`` only needs ``get`` / ``setex``."""

    def __init__(self, client, ttl: int = DEFAULT_TTL, prefix: str = DEFAULT_PREFIX):
        self.client = client
        self.ttl = max(1, int(ttl))
        self.prefix = prefix

    def key(self, model: str, question: str) -> str:
        digest = hashlib.sha256(f"{model}\x00{question}".encode("utf-8")).hexdigest()
        return f"{self.prefix}{digest}"

    def get(self, key: str) -> str | None:
        try:
            value = self.client.get(key)
        except Exception:  # noqa: BLE001 - cache is best effort
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return value or None

    def set(self, key: str, value: str) -> None:
        try:
            self.client.setex(key, self.ttl, value)
        except Exception:  # noqa: BLE001 - a failed write must not fail Q&A
            return None


def from_env() -> RewriteCache | None:
    """Return the configured cache, or ``None`` when disabled/unavailable.

    The Redis client is lazy: no connection is opened until the first get/set.
    """
    if not _env_bool("P0_QUERY_REWRITE_CACHE", False):
        return None
    url = os.getenv("P0_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
    if not url:
        return None
    try:
        import redis  # type: ignore
    except ImportError:
        return None
    client = redis.Redis.from_url(
        url, socket_connect_timeout=0.2, socket_timeout=0.2, decode_responses=True
    )
    return RewriteCache(client, ttl=_env_int("P0_QUERY_REWRITE_CACHE_TTL", DEFAULT_TTL))
