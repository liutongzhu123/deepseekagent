"""Citation-first retrieval over published standard chunks.

P1 replaces the P0 n-gram keyword scorer with bge-m3 vector retrieval while
keeping the same answer shape: every answer cites standard / clause / page and
refuses when the standard, clause or semantic match is not in the knowledge
base.  The keyword scorer is retained as a deterministic comparison baseline
and as an offline fallback when the embedding service is unavailable.

Retrieval guards (both modes):
  * an explicitly asked clause must exist in the published chunks;
  * an explicitly asked standard must be in the knowledge base;
  * a vector match below ``min_score`` is refused instead of answered.
"""
from __future__ import annotations

import os
import re

from . import query_rewrite, rewrite_cache, weknora
from .embeddings import (
    EmbeddingError,
    EmbeddingUnavailable,
    default_embedder,
    get_vector_index,
)

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CLAUSE = re.compile(r"\d+(?:\.\d+){1,3}")
_LATIN = re.compile(r"[a-z][a-z0-9./+-]{1,}")
_STANDARD = re.compile(
    r"(?:Q/[A-Z]{2,6}|QMAT|QSQR|QJD|SQR|VW|ISO|GB(?:/T)?|QC/T|JB/T)"
    r"\s*[A-Z]{0,4}\s*\d+(?:[A-Z]\d*)?(?:-\d{2,4})*(?:-\d{4})?",
    re.I,
)
_STOP_PHRASES = re.compile(
    r"(?:的)?(?:技术要求|判定要求|试验方法|检测方法|要求|是什么|有哪些|是多少|如何|怎么|标准|规定)"
)

_STOP_BIGRAMS = {
    "什么",
    "的是",
    "这个",
    "那个",
    "标准",
    "要求",
    "方法",
    "试验",
    "请问",
    "一下",
    "多少",
    "如何",
    "怎么",
    "哪些",
    "是否",
    "中有",
    "中的",
    "规定",
    "内容",
    "项目",
    "所有",
}

DEFAULT_MIN_SCORE = float(os.getenv("P0_VECTOR_MIN_SCORE", "0.60"))
DEFAULT_TOP_K = int(os.getenv("P0_VECTOR_TOP_K", "6"))

_VECTOR_UNAVAILABLE_MESSAGE = (
    "向量检索暂不可用：无法连接 bge-m3 embedding 服务。"
    "请设置 P0_EMBEDDING_URL（默认 http://127.0.0.1:11434），"
    "或改用关键词模式。"
)


class VectorUnavailable(EmbeddingUnavailable):
    """Explicit vector mode was requested but the embedding service failed."""


def _terms(text: str) -> tuple[set[str], set[str], set[str]]:
    text = text.lower()
    clauses = set(_CLAUSE.findall(text))
    latin = set(_LATIN.findall(text))
    bigrams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            bigrams.add(run)
            continue
        for index in range(len(run) - 1):
            gram = run[index : index + 2]
            if gram not in _STOP_BIGRAMS:
                bigrams.add(gram)
    return clauses, latin, bigrams


def _clause_candidates(question: str) -> set[str]:
    """Clause-like numbers only, so measurements such as 1.0 MPa stay text."""
    found: set[str] = set()
    for candidate in _CLAUSE.findall(question):
        parts = candidate.split(".")
        if len(parts) >= 3:
            found.add(candidate)
        elif len(parts) == 2 and parts[0] in {"5", "6", "7", "8", "9"}:
            found.add(candidate)
    return found


def _std_key(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _standard_matches(asked: str, known: list[str]) -> bool:
    key = _std_key(asked)
    if not key:
        return False
    for known_no in known:
        known_key = _std_key(known_no)
        base = re.sub(r"-\d{4}$", "", known_key)
        if key == known_key or key in known_key or known_key in key:
            return True
        if base and base in key:
            return True
    return False


def _focus(question: str) -> str:
    text = _CLAUSE.sub(" ", question)
    text = _STOP_PHRASES.sub(" ", text)
    text = re.sub(r"[？?，,。；;：:\s]", "", text)
    return text.lower()


def _domain_terms(text: str) -> set[str]:
    """CJK bigrams + latin tokens after the citation/stop boilerplate is gone."""
    _clauses, latin, bigrams = _terms(_focus(text))
    return bigrams | {token for token in latin if len(token) >= 2}


def _domain_match(question: str, pool: list[dict]) -> bool:
    """Cheap domain guard before paying for WeKnora retrieval.

    The hybrid retriever always returns *some* nearest chunk, so a completely
    unrelated question (ISO 9001, weather, contracts) would otherwise be
    answered from a visibly unrelated clause.  A question must share either an
    item-name substring or at least two content terms with the published pool.
    """
    focus = _focus(question)
    if len(focus) < 2:
        return False
    for chunk in pool:
        name = re.sub(r"\s+", "", str(chunk.get("item_name", "")).lower())
        if len(name) >= 2 and (name in focus or focus in name):
            return True
    question_terms = _domain_terms(question)
    if not question_terms:
        return False
    known: set[str] = set()
    for chunk in pool:
        known |= _domain_terms(str(chunk.get("item_name", "")))
        known |= _domain_terms(str(chunk.get("text", "")))
    return len(question_terms & known) >= 2


def _score(question: tuple[set[str], set[str], set[str]], chunk: dict) -> float:
    q_clauses, q_latin, q_grams = question
    name = chunk.get("item_name", "")
    text = chunk.get("text", "")
    _, _, name_grams = _terms(name)
    _, _, text_grams = _terms(text)
    score = 0.0
    if chunk.get("clause") in q_clauses:
        score += 100.0
    score += min(len(q_grams & name_grams), 6) * 6.0
    score += min(len(q_grams & text_grams), 20) * 1.0
    if "epb" in q_latin and "epb" in (name + text).lower():
        score += 5.0
    return score


def _kind_bonus(question: str, chunk: dict) -> float:
    asks_method = _asks_method(question)
    asks_requirement = _asks_requirement(question)
    if asks_method and chunk.get("kind") == "method":
        return 5.0
    if asks_requirement and chunk.get("kind") == "requirement":
        return 5.0
    if not asks_method and not asks_requirement and chunk.get("kind") == "requirement":
        return 1.0
    return 0.0


def _asks_method(question: str) -> bool:
    return any(word in question for word in ("方法", "怎么测", "如何测", "试验步骤", "测试步骤"))


def _asks_requirement(question: str) -> bool:
    return any(word in question for word in ("要求", "判定", "限值", "指标", "标准值", "多少"))


def _candidate_meta(score: float, chunk: dict) -> dict:
    return {
        "chunk_id": chunk.get("id"),
        "standard_no": chunk.get("standard_no"),
        "standard_name": chunk.get("standard_name"),
        "clause": chunk.get("clause"),
        "item_name": chunk.get("item_name"),
        "kind": chunk.get("kind"),
        "page": chunk.get("page"),
        "score": round(float(score), 4),
    }


def _refusal(message: str, mode: str, **retrieval) -> dict:
    return {
        "found": False,
        "answer": message,
        "citations": [],
        "retrieval": {"mode": mode, **retrieval},
    }


def _answer_payload(best: dict, chosen: list[dict], retrieval: dict) -> dict:
    lines = [
        f"《{best.get('standard_name', '')}》{best.get('clause', '')} "
        f"{best.get('item_name', '')}"
    ]
    for chunk in chosen:
        lines.append(
            f"【{chunk.get('kind_label', '')}】第 {chunk.get('page')} 页："
            f"{chunk.get('text', '')}"
        )
    citations = [
        {
            "standard_id": chunk.get("standard_id"),
            "standard_no": chunk.get("standard_no"),
            "standard_name": chunk.get("standard_name"),
            "clause": chunk.get("clause"),
            "item_name": chunk.get("item_name"),
            "page": chunk.get("page"),
            "kind": chunk.get("kind"),
            "kind_label": chunk.get("kind_label"),
            "quote": chunk.get("text", "")[:220],
        }
        for chunk in chosen
    ]
    return {"found": True, "answer": "\n".join(lines), "citations": citations, "retrieval": retrieval}


def answer_keyword(question: str, kbs: list[dict]) -> dict:
    """P0 keyword baseline (kept for before/after comparison and fallback)."""
    question = (question or "").strip()
    if not question:
        return _refusal("请输入问题。", "keyword")
    if not kbs:
        return _refusal("未找到相关标准内容：知识库中还没有已发布的标准。", "keyword")

    terms = _terms(question)
    all_chunks = [chunk for kb in kbs for chunk in kb.get("chunks", [])]
    chunk_clauses = {chunk.get("clause", "") for chunk in all_chunks}
    clause_candidates = _clause_candidates(question)
    clause_hit = any(
        clause == asked or clause.startswith(asked + ".")
        for asked in clause_candidates
        for clause in chunk_clauses
    )
    if clause_candidates and not clause_hit:
        asked = sorted(clause_candidates)[0]
        return _refusal(
            f"未找到相关标准内容：已发布标准中没有条款 {asked}。",
            "keyword",
            clause_filter=sorted(clause_candidates),
        )

    asked_standards = _STANDARD.findall(question)
    if asked_standards:
        known = [_std_key(kb.get("standard_no", "")) for kb in kbs]
        matched = any(_standard_matches(asked, known) for asked in asked_standards)
        if not matched:
            return _refusal(
                (
                    f"未找到相关标准内容：知识库中没有标准 {asked_standards[0]}。"
                    "系统不会凭记忆或常识编造标准条款。"
                ),
                "keyword",
                standard_filter=asked_standards,
            )

    focus = _focus(question)
    candidates: list[tuple[float, dict]] = []
    for chunk in all_chunks:
        score = _score(terms, chunk) + _kind_bonus(question, chunk)
        name_key = re.sub(r"\s+", "", chunk.get("item_name", "")).lower()
        if len(focus) >= 2 and (focus in name_key or name_key in focus):
            score += 50.0
        candidates.append((score, chunk))
    if not candidates:
        return _refusal("未找到相关标准内容：已发布标准中没有可检索的条款。", "keyword")
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = candidates[0]
    if not clause_hit and best_score < 20.0:
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return _refusal(
            (
                "未找到相关标准内容。当前已发布标准：" + (names or "无") +
                "。系统不会凭记忆或常识编造标准条款。"
            ),
            "keyword",
            top_score=round(float(best_score), 4),
            candidates=[_candidate_meta(score, chunk) for score, chunk in candidates[:5]],
        )

    chosen = [best]
    for _score_value, chunk in candidates[1:]:
        if len(chosen) >= 2:
            break
        if chunk.get("clause") == best.get("clause"):
            if _kind_bonus(question, best) == 0 or chunk.get("kind") != best.get("kind"):
                chosen.append(chunk)
                break
    retrieval = {
        "mode": "keyword",
        "top_score": round(float(best_score), 4),
        "candidates": [_candidate_meta(score, chunk) for score, chunk in candidates[:5]],
    }
    return _answer_payload(best, chosen, retrieval)


def answer_vector(
    question: str,
    kbs: list[dict],
    embedder,
    cache_path=None,
    min_score: float | None = None,
    top_k: int | None = None,
    cache=None,
) -> dict:
    """bge-m3 vector retrieval with clause/standard guards and a score gate."""
    question = (question or "").strip()
    min_score = DEFAULT_MIN_SCORE if min_score is None else float(min_score)
    top_k = max(1, DEFAULT_TOP_K if top_k is None else int(top_k))
    if not question:
        return _refusal("请输入问题。", "vector", model=getattr(embedder, "model", ""))
    if not kbs:
        return _refusal(
            "未找到相关标准内容：知识库中还没有已发布的标准。",
            "vector",
            model=getattr(embedder, "model", ""),
        )

    all_chunks = [chunk for kb in kbs for chunk in kb.get("chunks", [])]
    clauses = _clause_candidates(question)
    clause_chunks: list[dict] = []
    if clauses:
        clause_chunks = [
            chunk
            for chunk in all_chunks
            if any(
                chunk.get("clause") == asked or str(chunk.get("clause", "")).startswith(asked + ".")
                for asked in clauses
            )
        ]
        if not clause_chunks:
            asked = sorted(clauses)[0]
            return _refusal(
                f"未找到相关标准内容：已发布标准中没有条款 {asked}。",
                "vector",
                model=getattr(embedder, "model", ""),
                clause_filter=sorted(clauses),
            )

    asked_standards = _STANDARD.findall(question)
    allowed_standards: set[str] = set()
    if asked_standards:
        known = [kb.get("standard_no", "") for kb in kbs]
        matched = [asked for asked in asked_standards if _standard_matches(asked, known)]
        if not matched:
            return _refusal(
                (
                    f"未找到相关标准内容：知识库中没有标准 {asked_standards[0]}。"
                    "系统不会凭记忆或常识编造标准条款。"
                ),
                "vector",
                model=getattr(embedder, "model", ""),
                standard_filter=asked_standards,
            )
        for asked in matched:
            for known_no in known:
                if _standard_matches(asked, [known_no]):
                    allowed_standards.add(_std_key(known_no))

    pool = clause_chunks or all_chunks
    if allowed_standards:
        pool = [chunk for chunk in pool if _std_key(chunk.get("standard_no", "")) in allowed_standards]
    if not pool:
        return _refusal(
            "未找到相关标准内容：问题指定的标准与条款组合不在知识库中。",
            "vector",
            model=getattr(embedder, "model", ""),
            clause_filter=sorted(clauses),
            standard_filter=asked_standards,
        )

    rewrite = query_rewrite.rewrite(question, cache=cache)
    index = get_vector_index(all_chunks, embedder, cache_path=cache_path)
    query_vector = embedder.embed([rewrite.query])[0]
    scored = index.search(query_vector, top_k=top_k, chunk_ids={chunk["id"] for chunk in pool})
    if not scored:
        return _refusal("未找到相关标准内容：已发布标准中没有可检索的条款。", "vector")
    best_score, best = scored[0]
    candidates_meta = [_candidate_meta(score, chunk) for score, chunk in scored]
    retrieval_base = {
        "mode": "vector",
        "model": getattr(embedder, "model", ""),
        "min_score": min_score,
        "top_score": round(float(best_score), 4),
        "candidates": candidates_meta,
        "query_rewrite": rewrite.metadata(),
    }
    if clauses:
        retrieval_base["clause_filter"] = sorted(clauses)
    if asked_standards:
        retrieval_base["standard_filter"] = asked_standards

    if not clauses and best_score < min_score:
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return _refusal(
            (
                "未找到相关标准内容。当前已发布标准：" + (names or "无") +
                f"。最相近条款的相似度 {best_score:.3f} 低于阈值 {min_score:.2f}，"
                "系统不会凭记忆或常识编造标准条款。"
            ),
            "vector",
            **{key: value for key, value in retrieval_base.items() if key != "mode"},
        )

    best_clause = best.get("clause")
    if _asks_method(question) and best.get("kind") != "method":
        same_clause_methods = [
            (score, chunk)
            for score, chunk in scored
            if chunk.get("clause") == best_clause and chunk.get("kind") == "method"
        ]
        if same_clause_methods:
            best_score, best = same_clause_methods[0]
    elif _asks_requirement(question) and best.get("kind") != "requirement":
        same_clause_requirements = [
            (score, chunk)
            for score, chunk in scored
            if chunk.get("clause") == best_clause and chunk.get("kind") == "requirement"
        ]
        if same_clause_requirements:
            best_score, best = same_clause_requirements[0]

    chosen = [best]
    if not _asks_method(question) and not _asks_requirement(question):
        for _score_value, chunk in scored:
            if chunk.get("id") == best.get("id"):
                continue
            if chunk.get("clause") == best.get("clause") and chunk.get("kind") != best.get("kind"):
                chosen.append(chunk)
                break

    retrieval = {
        **retrieval_base,
        "top_score": round(float(best_score), 4),
    }
    return _answer_payload(best, chosen, retrieval)


def answer_weknora(
    question: str,
    kbs: list[dict],
    *,
    min_score: float | None = None,
    top_k: int | None = None,
    cache=None,
    rerank: bool | None = None,
) -> dict:
    """Answer from WeKnora retrieval with the same citation/refusal contract.

    The local published chunks are used for exact clause/page metadata and for
    the guard rails (a clause or standard that was never published is refused
    before asking WeKnora, so a stale remote index cannot invent it).
    """
    question = (question or "").strip()
    if not question:
        return _refusal("请输入问题。", "weknora")
    if not kbs:
        return _refusal("未找到相关标准内容：知识库中还没有已发布的标准。", "weknora")
    if not weknora.configured():
        raise weknora.WeKnoraUnavailable("未配置 WeKnora 知识底座")

    min_score = weknora.DEFAULT_MIN_SCORE if min_score is None else float(min_score)
    all_chunks = [chunk for kb in kbs for chunk in kb.get("chunks", [])]
    clause_candidates = _clause_candidates(question)
    clause_chunks: list[dict] = []
    if clause_candidates:
        clause_chunks = [
            chunk
            for chunk in all_chunks
            if any(
                str(chunk.get("clause", "")) == asked
                or str(chunk.get("clause", "")).startswith(asked + ".")
                for asked in clause_candidates
            )
        ]
        if not clause_chunks:
            asked = sorted(clause_candidates)[0]
            return _refusal(
                f"未找到相关标准内容：已发布标准中没有条款 {asked}。",
                "weknora",
                clause_filter=sorted(clause_candidates),
            )

    asked_standards = _STANDARD.findall(question)
    allowed_standards: set[str] = set()
    if asked_standards:
        known = [kb.get("standard_no", "") for kb in kbs]
        matched = [asked for asked in asked_standards if _standard_matches(asked, known)]
        if not matched:
            return _refusal(
                (
                    f"未找到相关标准内容：知识库中没有标准 {asked_standards[0]}。"
                    "系统不会凭记忆或常识编造标准条款。"
                ),
                "weknora",
                standard_filter=asked_standards,
            )
        for asked in matched:
            for known_no in known:
                if _standard_matches(asked, [known_no]):
                    allowed_standards.add(_std_key(known_no))

    pool = clause_chunks or all_chunks
    if allowed_standards:
        pool = [chunk for chunk in pool if _std_key(chunk.get("standard_no", "")) in allowed_standards]
    if not pool:
        return _refusal(
            "未找到相关标准内容：问题指定的标准与条款组合不在知识库中。",
            "weknora",
            clause_filter=sorted(clause_candidates),
            standard_filter=asked_standards,
        )
    if not clause_candidates and not asked_standards and not _domain_match(question, pool):
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return _refusal(
            (
                "未找到相关标准内容：问题与已发布标准的条款/项目没有可核实的对应关系。"
                "当前已发布标准：" + (names or "无") +
                "。系统不会凭记忆或常识编造标准条款。"
            ),
            "weknora",
            domain_guard="no_overlap",
        )

    knowledge_ids = [
        knowledge_id
        for kb in kbs
        if (
            not allowed_standards
            or _std_key(kb.get("standard_no", "")) in allowed_standards
        )
        for knowledge_id in [(kb.get("weknora") or {}).get("knowledge_id")]
        if knowledge_id
    ]
    rewrite = query_rewrite.rewrite(question, cache=cache)
    result = weknora.search(
        rewrite.query,
        pool,
        knowledge_ids=knowledge_ids,
        top_k=max(1, top_k or DEFAULT_TOP_K),
        rerank=rerank,
    )
    citations = result.get("citations") or []
    filtered = []
    for citation in citations:
        clause = str(citation.get("clause", ""))
        if clause_candidates and not any(
            clause == asked or clause.startswith(asked + ".")
            for asked in clause_candidates
        ):
            continue
        if allowed_standards and _std_key(citation.get("standard_no", "")) not in allowed_standards:
            continue
        filtered.append(citation)
    citations = filtered
    if not citations:
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return _refusal(
            "未找到相关标准内容。当前已发布标准：" + (names or "无") +
            "。系统不会凭记忆或常识编造标准条款。",
            "weknora",
            kb_id=result.get("kb_id"),
            query_rewrite=rewrite.metadata(),
        )

    top_score = result.get("top_score")
    if top_score is not None and top_score < min_score and not clause_candidates:
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return _refusal(
            (
                "未找到相关标准内容。当前已发布标准：" + (names or "无") +
                f"。最相近条款的相关度 {top_score:.3f} 低于阈值 {min_score:.2f}，"
                "系统不会凭记忆或常识编造标准条款。"
            ),
            "weknora",
            top_score=top_score,
            min_score=min_score,
            kb_id=result.get("kb_id"),
            query_rewrite=rewrite.metadata(),
        )

    best = citations[0]
    lines = [
        f"《{best.get('standard_name') or best.get('standard_no', '')}》"
        f"{best.get('clause', '')} {best.get('item_name', '')}".strip()
    ]
    for citation in citations:
        page = citation.get("page")
        page_text = f"第 {page} 页" if page is not None else "页码未提供"
        lines.append(
            f"【{citation.get('kind_label', '知识片段')}】{page_text}："
            f"{citation.get('quote', '')}"
        )
    return {
        "found": True,
        "answer": "\n".join(lines),
        "citations": citations,
        "retrieval": {
            "mode": "weknora",
            "kb_id": result.get("kb_id"),
            "top_score": top_score,
            "min_score": min_score,
            "knowledge_ids": knowledge_ids,
            "query_rewrite": rewrite.metadata(),
            "rerank": bool(result.get("rerank")),
        },
    }



def answer(
    question: str,
    kbs: list[dict],
    *,
    mode: str = "auto",
    embedder=None,
    cache_path=None,
    min_score: float | None = None,
    top_k: int | None = None,
    cache=None,
    rerank: bool | None = None,
) -> dict:
    """Answer with WeKnora (default), the local vector index, or keyword mode.

    ``mode``:
    * ``auto`` / ``weknora`` / ``hybrid`` – WeKnora retrieval; when WeKnora is
      required (the production default) an outage is surfaced instead of being
      hidden behind a different knowledge base;
    * ``local`` / ``vector`` – the P1 bge-m3 local index (debug/offline);
    * ``keyword`` – the deterministic P0 baseline (comparison only).

    ``cache`` is the optional query-rewrite cache (see
    ``query_rewrite.rewrite``); only WeKnora/local retrieval uses rewriting.
    ``rerank`` overrides the WeKnora rerank switch for this call (``None`` keeps
    the ``P0_WEKNORA_RERANK`` environment default).
    """
    if mode in {"weknora", "hybrid"}:
        mode = "auto"
    if mode not in {"auto", "local", "vector", "keyword"}:
        raise ValueError(f"unknown retrieval mode: {mode}")
    if mode == "keyword":
        return answer_keyword(question, kbs)
    if cache is None:
        cache = rewrite_cache.from_env()
    if mode == "auto":
        if weknora.configured():
            try:
                return answer_weknora(
                    question, kbs, min_score=min_score, top_k=top_k, cache=cache, rerank=rerank
                )
            except weknora.WeKnoraUnavailable as exc:
                if weknora.required():
                    raise VectorUnavailable(f"WeKnora 检索暂不可用：{exc}") from exc
        elif weknora.required():
            raise VectorUnavailable("WeKnora 是唯一知识底座，但尚未配置连接")
    try:
        client = embedder or default_embedder()
        if client is None:
            raise EmbeddingUnavailable("P0_EMBEDDING_URL is disabled")
        result = answer_vector(
            question,
            kbs,
            client,
            cache_path=cache_path,
            min_score=min_score,
            top_k=top_k,
            cache=cache,
        )
        if mode == "auto":
            result["retrieval"]["mode"] = "local-fallback"
        return result
    except EmbeddingError as exc:
        if mode in {"local", "vector"}:
            raise VectorUnavailable(f"{_VECTOR_UNAVAILABLE_MESSAGE}（{exc}）") from exc
        result = answer_keyword(question, kbs)
        result["retrieval"]["mode"] = "keyword-fallback"
        result["retrieval"]["fallback_reason"] = str(exc)
        return result
