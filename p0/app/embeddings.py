"""Free/local embedding client and a small vector index for standard Q&A.

The P1 knowledge-base increment replaces the P0 n-gram keyword scorer with
bge-m3 vector retrieval.  The model is served by the existing WeKnora
deployment's Ollama service (``bge-m3``); no paid API is used.  The client is
plain ``urllib`` so the P0 server keeps working without new HTTP packages.

Configuration (environment variables):

``P0_EMBEDDING_URL``
    Ollama base URL, default ``http://127.0.0.1:11434``.  Set to ``off`` to
    disable vector retrieval and fall back to the keyword scorer.
``P0_EMBEDDING_MODEL``
    Embedding model name, default ``bge-m3``.
``P0_EMBEDDING_TIMEOUT``
    Per-request timeout in seconds, default 300.
``P0_EMBEDDING_BATCH``
    Number of chunks per request, default 64.
``P0_VECTOR_CACHE``
    Optional path of the on-disk vector cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STANDARD_DIR = Path(os.getenv("P0_STANDARD_DIR", str(ROOT / "data" / "standards")))
DEFAULT_CACHE_PATH = Path(
    os.getenv("P0_VECTOR_CACHE", str(DEFAULT_STANDARD_DIR / "kb_vectors.json"))
)
CACHE_VERSION = 1
EMBED_TEXT_LIMIT = 480


class EmbeddingError(RuntimeError):
    """Base error for the embedding/vector layer."""


class EmbeddingUnavailable(EmbeddingError):
    """The embedding service is not configured or cannot be reached."""


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover - protocol
        ...


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class OllamaEmbedder:
    """Minimal Ollama ``/api/embed`` client with bounded retries."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        batch_size: int | None = None,
    ):
        self.base_url = (base_url or os.getenv("P0_EMBEDDING_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = model or os.getenv("P0_EMBEDDING_MODEL", "bge-m3")
        self.timeout = timeout if timeout is not None else _env_float("P0_EMBEDDING_TIMEOUT", 300.0)
        self.batch_size = max(1, batch_size if batch_size is not None else _env_int("P0_EMBEDDING_BATCH", 64))

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                vectors = data.get("embeddings")
                if vectors is None and "embedding" in data:
                    vectors = [data["embedding"]]
                if not isinstance(vectors, list) or len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"embedding 返回数量不符：请求 {len(texts)} 条，返回 {len(vectors) if isinstance(vectors, list) else '未知'}"
                    )
                if vectors and not vectors[0]:
                    raise EmbeddingError("embedding 返回空向量")
                return [[float(value) for value in vector] for vector in vectors]
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
            except json.JSONDecodeError as exc:
                raise EmbeddingError(f"embedding 返回非 JSON：{exc}") from exc
        raise EmbeddingUnavailable(
            f"无法连接 embedding 服务 {self.base_url}（模型 {self.model}）：{last_error}"
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._post(texts[start : start + self.batch_size]))
        dims = {len(vector) for vector in vectors}
        if len(dims) != 1:
            raise EmbeddingError(f"embedding 维度不一致：{sorted(dims)}")
        return vectors

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EmbeddingUnavailable(f"embedding 服务不可用：{exc}") from exc
        names = [item.get("name", "") for item in data.get("models", [])]
        installed = any(name == self.model or name.startswith(self.model + ":") for name in names)
        if not installed:
            raise EmbeddingUnavailable(f"Ollama 未安装模型 {self.model}")
        return {"status": "ok", "base_url": self.base_url, "model": self.model, "models": names}


def default_embedder() -> OllamaEmbedder | None:
    """Return the configured embedder, or ``None`` when disabled."""
    if os.getenv("P0_EMBEDDING_URL", "").strip().lower() in {"off", "none", "disabled"}:
        return None
    return OllamaEmbedder()


def chunk_embedding_text(chunk: dict) -> str:
    """Build the text sent to bge-m3 for one clause chunk.

    Including the item name and clause matters more than a long body: many
    material-requirement chunks are just a number (``0.92±0.02``), which has
    almost no semantic signal on its own.
    """
    text = str(chunk.get("text", ""))[:EMBED_TEXT_LIMIT]
    return (
        f"{chunk.get('standard_name', '')} "
        f"{chunk.get('standard_no', '')} "
        f"第{chunk.get('clause', '')}条 "
        f"{chunk.get('item_name', '')} "
        f"{chunk.get('kind_label', '')}：{text}"
    ).strip()


def _chunk_sha(chunk: dict) -> str:
    return hashlib.sha1(chunk_embedding_text(chunk).encode("utf-8")).hexdigest()


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        # A read-only data directory must not break Q&A.
        pass


class VectorIndex:
    """Clause-chunk vectors plus a cached on-disk copy."""

    def __init__(self, model: str, chunks: list[dict], vectors: list[list[float]], built_at: str = ""):
        if len(chunks) != len(vectors):
            raise EmbeddingError("chunk 数量与向量数量不一致")
        self.model = model
        self.chunks = chunks
        self.vectors = vectors
        self.built_at = built_at
        self.by_id = {chunk["id"]: vector for chunk, vector in zip(chunks, vectors)}
        self.dim = len(vectors[0]) if vectors else 0

    @classmethod
    def from_chunks(
        cls,
        chunks: list[dict],
        embedder: Embedder,
        cache_path: str | Path | None = None,
        reset: bool = False,
    ) -> "VectorIndex":
        if not chunks:
            return cls(embedder.model, [], [], time.strftime("%Y-%m-%dT%H:%M:%S"))
        cache = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        wanted = {chunk["id"]: _chunk_sha(chunk) for chunk in chunks}
        if not reset:
            cached = _read_cache(cache)
            if (
                cached
                and cached.get("version") == CACHE_VERSION
                and cached.get("model") == embedder.model
                and isinstance(cached.get("entries"), list)
            ):
                entries = {entry.get("id"): entry for entry in cached["entries"]}
                if all(
                    entries.get(chunk_id) is not None
                    and entries[chunk_id].get("sha") == wanted[chunk_id]
                    and isinstance(entries[chunk_id].get("vector"), list)
                    for chunk_id in wanted
                ):
                    vectors = [entries[chunk["id"]]["vector"] for chunk in chunks]
                    if len({len(vector) for vector in vectors}) == 1:
                        return cls(embedder.model, chunks, vectors, cached.get("built_at", ""))

        vectors = embedder.embed([chunk_embedding_text(chunk) for chunk in chunks])
        built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_cache(
            cache,
            {
                "version": CACHE_VERSION,
                "model": embedder.model,
                "dim": len(vectors[0]) if vectors else 0,
                "built_at": built_at,
                "entries": [
                    {"id": chunk["id"], "sha": wanted[chunk["id"]], "vector": vector}
                    for chunk, vector in zip(chunks, vectors)
                ],
            },
        )
        return cls(embedder.model, chunks, vectors, built_at)

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 6,
        chunk_ids: set[str] | None = None,
    ) -> list[tuple[float, dict]]:
        scored: list[tuple[float, dict]] = []
        for chunk, vector in zip(self.chunks, self.vectors):
            if chunk_ids is not None and chunk["id"] not in chunk_ids:
                continue
            scored.append((cosine(query_vector, vector), chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[: max(1, top_k)]


_INDEX_CACHE: dict[tuple, VectorIndex] = {}
_INDEX_LOCK = threading.RLock()


def chunks_signature(chunks: list[dict]) -> str:
    digest = hashlib.sha1()
    for chunk in chunks:
        digest.update(chunk["id"].encode("utf-8"))
        digest.update(_chunk_sha(chunk).encode("utf-8"))
    return digest.hexdigest()


def get_vector_index(
    chunks: list[dict],
    embedder: Embedder,
    cache_path: str | Path | None = None,
    reset: bool = False,
) -> VectorIndex:
    """Return a process-cached index so the API does not re-read it per call."""
    cache = str(Path(cache_path) if cache_path else DEFAULT_CACHE_PATH)
    key = (embedder.model, chunks_signature(chunks), cache)
    with _INDEX_LOCK:
        if not reset and key in _INDEX_CACHE:
            return _INDEX_CACHE[key]
    index = VectorIndex.from_chunks(chunks, embedder, cache_path=cache_path, reset=reset)
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = index
    return index
