"""Query rewriting before retrieval (T401a).

Turns a conversational question into one standalone retrieval query.  Uses a
**non-reasoning** chat model (``deepseek-chat`` by default) and never asks for
thinking output; off by default (``P0_QUERY_REWRITE=1``), and any failure falls
back to the original question.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
SYSTEM_PROMPT = (
    "你是标准知识库的检索查询改写器。把用户问题改写为一条独立、可检索的中文查询："
    "补全省略的主语，消解代词，保留原问题中的标准号、条款号和关键术语；"
    "不要回答问题，不要解释，不要添加原文没有的条款或标准号。只输出改写后的查询本身。"
)
_REASONING = ("reasoner", "reasoning", "o1", "o3", "o4", "qwq", "thinking")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_PREFIX = re.compile(r"^(?:改写(?:后)?(?:的)?(?:查询|问题)?\s*[:：]\s*)", re.I)
_WS = re.compile(r"\s+")


class RewriteError(RuntimeError):
    """The rewrite model could not be called or returned unusable output."""

@dataclass(frozen=True)
class RewriteConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = 3.0
    max_tokens: int = 96
    max_chars: int = 500


@dataclass(frozen=True)
class RewriteResult:
    original: str
    query: str
    applied: bool
    cached: bool = False
    model: str = ""
    reason: str = ""

    def metadata(self) -> dict:
        return {"applied": self.applied, "cached": self.cached,
                "model": self.model, "reason": self.reason}


def _env(name: str, default):
    value = os.getenv(name)
    if value is None:
        return default
    if isinstance(default, bool):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    try:
        return type(default)(value)
    except ValueError:
        return default

def config_from_env() -> RewriteConfig:
    return RewriteConfig(
        enabled=_env("P0_QUERY_REWRITE", False),
        api_key=os.getenv("P0_QUERY_REWRITE_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")).strip(),
        base_url=os.getenv("P0_QUERY_REWRITE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
        model=os.getenv("P0_QUERY_REWRITE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        timeout=_env("P0_QUERY_REWRITE_TIMEOUT", 3.0),
        max_tokens=_env("P0_QUERY_REWRITE_MAX_TOKENS", 96),
        max_chars=_env("P0_QUERY_REWRITE_MAX_CHARS", 500),
    )


def is_reasoning_model(model: str) -> bool:
    return any(marker in (model or "").lower() for marker in _REASONING)


def clean_output(text: str, max_chars: int = 500) -> str:
    value = _PREFIX.sub("", (text or "").strip().translate(_QUOTES)).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return _WS.sub(" ", value).strip()[:max_chars].rstrip()


def _http_transport(question: str, cfg: RewriteConfig, model: str) -> str:
    request = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": question}],
            "temperature": 0, "max_tokens": cfg.max_tokens, "stream": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RewriteError(f"改写模型不可用：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RewriteError(f"改写模型返回非 JSON：{exc}") from exc
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RewriteError("改写模型响应缺少 choices[0].message.content") from exc


Transport = Callable[[str, RewriteConfig, str], str]


def rewrite(question: str, *, config: RewriteConfig | None = None,
            cache=None, transport: Transport | None = None) -> RewriteResult:
    """Rewrite ``question``; always return a usable query.

    ``cache`` needs ``key(model, question)`` / ``get(key)`` / ``set(key, value)``.
    """
    original = (question or "").strip()
    cfg = config or config_from_env()
    if not original:
        return RewriteResult("", "", False, reason="empty_question")
    if not cfg.enabled:
        return RewriteResult(original, original, False, reason="disabled")
    if not cfg.api_key:
        return RewriteResult(original, original, False, reason="no_api_key")

    model, reason = cfg.model, ""
    if is_reasoning_model(model):
        model, reason = DEFAULT_MODEL, "reasoning_model_replaced"
    cache_key = ""
    if cache is not None:
        try:
            cache_key = cache.key(model, original)
            cached = cache.get(cache_key)
        except Exception:  # noqa: BLE001 - cache is best effort
            cached = None
        if cached:
            return RewriteResult(original, str(cached), True, True, model, reason or "cache_hit")

    try:
        raw = (transport or _http_transport)(original, cfg, model)
    except Exception as exc:  # noqa: BLE001 - fall back to the original query
        return RewriteResult(original, original, False, False, model,
                             reason or f"error: {type(exc).__name__}")
    cleaned = clean_output(raw, cfg.max_chars)
    if not cleaned:
        return RewriteResult(original, original, False, False, model, reason or "empty_output")
    if cache is not None and cache_key:
        try:
            cache.set(cache_key, cleaned)
        except Exception:  # noqa: BLE001 - a failed cache write must not fail Q&A
            pass
    return RewriteResult(original, cleaned, True, False, model, reason or "rewritten")
