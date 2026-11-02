"""WeKnora client: the single knowledge base used by the P0 business system.

P0 owns parsing, review and business rules.  WeKnora owns publication,
retrieval and the citation index.  The integration deliberately uses only the
public REST API (v0.9):

* ``POST /api/v1/knowledge-bases/{kb}/knowledge/manual`` publishes one
  markdown document per standard;
* ``PUT  /api/v1/knowledge/manual/{id}`` refreshes an existing publication;
* ``POST /api/v1/knowledge-search`` is the retrieval primitive used for Q&A.

Each published paragraph starts with a machine-readable citation header so a
retrieved chunk can always be traced back to standard / clause / page:

    〔Q/JLY J7111074B-2021 | 条款 5.3.11 | 第 12 页 | 技术要求 | 驻车基本性能〕

If a chunk was split by WeKnora's chunker and lost its header, the caller can
fall back to matching the retrieved text against the local published chunks.
"""
from __future__ import annotations

import difflib
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TOP_K = int(os.getenv("P0_WEKNORA_TOP_K", "6"))
DEFAULT_MIN_SCORE = float(os.getenv("P0_WEKNORA_MIN_SCORE", "0.35"))
INDEX_WAIT_SECONDS = float(os.getenv("P0_WEKNORA_INDEX_WAIT", "30"))

_HEADER = re.compile(
    r"〔(?P<standard_no>[^|〕]+)\|\s*条款\s*(?P<clause>[^|〕]+)\|\s*第\s*(?P<page>\d+)\s*页"
    r"\s*\|\s*(?P<kind>[^|〕]+)\|\s*(?P<item_name>[^〕]+)〕"
)
_WS = re.compile(r"\s+")


class WeKnoraError(RuntimeError):
    """WeKnora returned an error or an unexpected response."""


class WeKnoraUnavailable(WeKnoraError):
    """WeKnora could not be reached or is not configured."""


@dataclass(frozen=True)
class WeKnoraConfig:
    base_url: str
    api_key: str
    kb_id: str
    timeout: float = 60.0


def config_from_env() -> WeKnoraConfig | None:
    base_url = os.getenv("P0_WEKNORA_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("P0_WEKNORA_API_KEY", "").strip()
    kb_id = os.getenv("P0_WEKNORA_KB_ID", "").strip()
    if not (base_url and api_key and kb_id):
        return None
    return WeKnoraConfig(
        base_url=base_url,
        api_key=api_key,
        kb_id=kb_id,
        timeout=float(os.getenv("P0_WEKNORA_TIMEOUT", "60")),
    )


def configured() -> bool:
    return config_from_env() is not None


def required() -> bool:
    return os.getenv("P0_WEKNORA_REQUIRED", "1" if configured() else "0").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _client(cfg: WeKnoraConfig) -> httpx.Client:
    return httpx.Client(
        base_url=cfg.base_url,
        headers={"X-API-Key": cfg.api_key, "Accept": "application/json"},
        timeout=httpx.Timeout(cfg.timeout),
    )


def _check(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("error") or payload.get("message") or payload.get("detail") or ""
        except ValueError:
            detail = response.text[:300]
        raise WeKnoraError(f"WeKnora HTTP {response.status_code}: {detail or response.reason_phrase}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeKnoraError("WeKnora 返回了非 JSON 响应") from exc
    if isinstance(payload, dict) and payload.get("success") is False:
        raise WeKnoraError(str(payload.get("error") or payload.get("message") or "WeKnora 返回失败"))
    return payload


def ping(cfg: WeKnoraConfig | None = None) -> dict:
    cfg = cfg or config_from_env()
    if cfg is None:
        raise WeKnoraUnavailable("未配置 WeKnora（P0_WEKNORA_BASE_URL / API_KEY / KB_ID）")
    try:
        with _client(cfg) as client:
            response = client.get("/api/v1/knowledge-bases/" + cfg.kb_id)
            payload = _check(response)
            data = payload.get("data") or {}
            return {
                "ok": True,
                "kb_id": cfg.kb_id,
                "kb_name": data.get("name", ""),
                "vector": (data.get("capabilities") or {}).get("vector", False),
                "keyword": (data.get("capabilities") or {}).get("keyword", False),
            }
    except (httpx.HTTPError, WeKnoraError) as exc:
        if isinstance(exc, WeKnoraUnavailable):
            raise
        raise WeKnoraUnavailable(str(exc)) from exc


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------
def _record_header(record: dict) -> str:
    meta = record.get("meta", {})
    standard_no = meta.get("standard_no") or record.get("id", "")
    name = meta.get("name") or ""
    revision = record.get("revision", 0)
    return f"# {standard_no} {name}\n\n> P0 业务发布 · 标准 ID：{record.get('id', '')} · 复核修订：v{revision}\n"


def render_markdown(record: dict, chunks: list[dict]) -> str:
    """Render deterministic, citation-prefixed markdown for one standard."""
    meta = record.get("meta", {})
    standard_no = meta.get("standard_no") or record.get("id", "")
    items = {item.get("clause", ""): item for item in record.get("items", [])}
    lines: list[str] = [_record_header(record)]
    last_clause = ""
    for chunk in chunks:
        clause = str(chunk.get("clause", "") or "").strip()
        item_name = str(chunk.get("item_name", "") or "").strip()
        kind = chunk.get("kind", "requirement")
        kind_label = "试验方法" if kind == "method" else "技术要求"
        page = chunk.get("page")
        page_text = str(page) if page not in (None, "") else "-"
        text = str(chunk.get("text", "") or "").strip()
        if not text:
            continue
        if clause != last_clause:
            item = items.get(clause, {})
            lines.append(f"\n## 条款 {clause} {item.get('name', item_name)}\n")
            last_clause = clause
        prefix = (
            f"〔{standard_no} | 条款 {clause} | 第 {page_text} 页 | {kind_label} | {item_name}〕"
        )
        # Keep the header and the source text in one paragraph so a 512-char
        # chunk keeps its provenance even after WeKnora's splitter runs.
        lines.append(f"{prefix}\n{text}\n")
    return "\n".join(lines).strip() + "\n"


def _knowledge_title(record: dict) -> str:
    meta = record.get("meta", {})
    return f"P0 | {meta.get('standard_no') or record.get('id', '')} | {meta.get('name', '')}".strip()


def _wait_for_index(client: httpx.Client, knowledge_id: str) -> str:
    deadline = time.monotonic() + INDEX_WAIT_SECONDS
    status = "pending"
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/knowledge/{knowledge_id}")
        payload = _check(response)
        data = payload.get("data") or {}
        status = data.get("parse_status") or status
        if status in {"completed", "failed", "cancelled"}:
            return status
        time.sleep(1.0)
    return status


def publish_standard(record: dict, chunks: list[dict], cfg: WeKnoraConfig | None = None) -> dict:
    """Create or update the standard's WeKnora knowledge document."""
    cfg = cfg or config_from_env()
    if cfg is None:
        raise WeKnoraUnavailable("未配置 WeKnora，无法发布到唯一知识底座")
    if not chunks:
        raise WeKnoraError("没有可发布的知识片段")
    content = render_markdown(record, chunks)
    title = _knowledge_title(record)
    previous = (record.get("weknora") or {}).get("knowledge_id") or ""
    try:
        with _client(cfg) as client:
            knowledge_id = ""
            updated = False
            if previous:
                response = client.put(
                    f"/api/v1/knowledge/manual/{previous}",
                    json={"title": title, "content": content, "status": "publish"},
                )
                if response.status_code == 404:
                    previous = ""
                else:
                    payload = _check(response)
                    knowledge_id = (payload.get("data") or {}).get("id") or previous
                    updated = True
            if not knowledge_id:
                response = client.post(
                    f"/api/v1/knowledge-bases/{cfg.kb_id}/knowledge/manual",
                    json={"title": title, "content": content, "status": "publish"},
                )
                payload = _check(response)
                knowledge_id = (payload.get("data") or {}).get("id") or ""
                if not knowledge_id:
                    raise WeKnoraError("WeKnora 未返回知识 ID")
            parse_status = _wait_for_index(client, knowledge_id)
            return {
                "ok": True,
                "kb_id": cfg.kb_id,
                "knowledge_id": knowledge_id,
                "title": title,
                "chars": len(content),
                "chunks": len(chunks),
                "updated": updated,
                "parse_status": parse_status,
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
    except httpx.HTTPError as exc:
        raise WeKnoraUnavailable(f"无法连接 WeKnora：{exc}") from exc


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
def _parse_header(content: str) -> dict | None:
    match = _HEADER.search(content or "")
    if not match:
        return None
    return {
        "standard_no": match.group("standard_no").strip(),
        "clause": match.group("clause").strip(),
        "page": int(match.group("page")),
        "kind_label": match.group("kind").strip(),
        "item_name": match.group("item_name").strip(),
    }


def _normalise(text: str) -> str:
    return _WS.sub("", text or "").lower()


def match_local_chunk(content: str, local_chunks: list[dict]) -> dict | None:
    """Attach exact P0 metadata to a WeKnora chunk by text similarity."""
    needle = _normalise(content)
    if not needle:
        return None
    best: tuple[float, dict] | None = None
    for chunk in local_chunks:
        haystack = _normalise(str(chunk.get("text", "")))
        if not haystack:
            continue
        if haystack in needle or needle in haystack:
            return chunk
        ratio = difflib.SequenceMatcher(None, needle[:600], haystack[:600]).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, chunk)
    return best[1] if best and best[0] >= 0.55 else None


def _citation_from_result(result: dict, local_chunks: list[dict], cfg: WeKnoraConfig) -> dict:
    content = str(result.get("content") or "")
    header = _parse_header(content)
    matched = match_local_chunk(content, local_chunks)
    if matched is not None:
        return {
            "standard_id": matched.get("standard_id", ""),
            "standard_no": matched.get("standard_no", ""),
            "standard_name": matched.get("standard_name", ""),
            "clause": matched.get("clause", ""),
            "item_name": matched.get("item_name", ""),
            "page": matched.get("page"),
            "kind": matched.get("kind", ""),
            "kind_label": matched.get("kind_label", ""),
            "quote": str(matched.get("text", "") or content)[:300],
            "weknora_chunk_id": result.get("id", ""),
            "weknora_knowledge_id": result.get("knowledge_id", ""),
            "score": float(result.get("score") or 0.0),
            "source": "weknora",
        }
    if header:
        return {
            "standard_id": "",
            "standard_no": header["standard_no"],
            "standard_name": result.get("knowledge_title", ""),
            "clause": header["clause"],
            "item_name": header["item_name"],
            "page": header["page"],
            "kind": "method" if header["kind_label"] == "试验方法" else "requirement",
            "kind_label": header["kind_label"],
            "quote": content[:300],
            "weknora_chunk_id": result.get("id", ""),
            "weknora_knowledge_id": result.get("knowledge_id", ""),
            "score": float(result.get("score") or 0.0),
            "source": "weknora",
        }
    return {
        "standard_id": "",
        "standard_no": "",
        "standard_name": result.get("knowledge_title", ""),
        "clause": "",
        "item_name": "",
        "page": None,
        "kind": "",
        "kind_label": "",
        "quote": content[:300],
        "weknora_chunk_id": result.get("id", ""),
        "weknora_knowledge_id": result.get("knowledge_id", ""),
        "score": float(result.get("score") or 0.0),
        "source": "weknora",
        "kb_id": cfg.kb_id,
    }


def search(
    question: str,
    local_chunks: list[dict],
    *,
    knowledge_ids: list[str] | None = None,
    top_k: int | None = None,
    cfg: WeKnoraConfig | None = None,
) -> dict:
    """Retrieve cited chunks from WeKnora for a question.

    Default path is the raw hybrid-search primitive (vector + keyword, no LLM
    rerank): it answers in a few hundred milliseconds.  Set
    ``P0_WEKNORA_RERANK=1`` to use the rerank-enabled ``/knowledge-search``
    endpoint instead (higher precision, but the rerank model is much slower).
    """
    cfg = cfg or config_from_env()
    if cfg is None:
        raise WeKnoraUnavailable("未配置 WeKnora")
    top_k = max(1, int(top_k or DEFAULT_TOP_K))
    use_rerank = os.getenv("P0_WEKNORA_RERANK", "0").lower() in {"1", "true", "on", "yes"}
    if use_rerank:
        path = "/api/v1/knowledge-search"
        body: dict[str, Any] = {
            "query": question,
            "knowledge_base_ids": [cfg.kb_id],
            "match_count": top_k,
        }
    else:
        path = f"/api/v1/knowledge-bases/{cfg.kb_id}/hybrid-search"
        body = {"query_text": question, "match_count": top_k}
    if knowledge_ids and len(knowledge_ids) <= 20:
        body["knowledge_ids"] = knowledge_ids
    try:
        with _client(cfg) as client:
            response = client.post(path, json=body)
            payload = _check(response)
    except httpx.HTTPError as exc:
        raise WeKnoraUnavailable(f"无法连接 WeKnora：{exc}") from exc
    results = payload.get("data") or []
    if not isinstance(results, list):
        raise WeKnoraError("WeKnora 检索响应格式异常")
    citations = [_citation_from_result(item, local_chunks, cfg) for item in results if isinstance(item, dict)]
    if knowledge_ids:
        wanted = set(knowledge_ids)
        citations = [
            citation
            for citation in citations
            if citation.get("weknora_knowledge_id") in wanted or not citation.get("weknora_knowledge_id")
        ]
    meta = payload.get("meta") or {}
    rerank = meta.get("rerank") or {}
    top_score = rerank.get("top_score")
    if top_score is None and citations:
        top_score = max(float(citation.get("score") or 0.0) for citation in citations)
    return {
        "citations": citations,
        "top_score": float(top_score) if top_score is not None else None,
        "meta": meta,
        "kb_id": cfg.kb_id,
        "rerank": use_rerank,
    }
