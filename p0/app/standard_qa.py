"""Citation-first retrieval over published standard chunks (P0 baseline).

This is intentionally not a generative Q&A: it selects the clause/paragraph
that matches the question and returns it with standard number, clause and
page.  If nothing is close enough it says so instead of inventing content.
A later increment can swap this for the WeKnora RAG service; the API shape
stays the same.
"""
from __future__ import annotations

import re

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CLAUSE = re.compile(r"\d+(?:\.\d+){1,3}")
_LATIN = re.compile(r"[a-z][a-z0-9./+-]{1,}")
_STANDARD = re.compile(
    r"(?:Q/[A-Z]{2,6}|QMAT|QSQR|QJD|SQR|VW)\s*[A-Z]{0,4}\s*\d+(?:[A-Z]\d*)?(?:-\d{2,4})*(?:-\d{4})?",
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


def _std_key(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


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


def _focus(question: str) -> str:
    text = question
    for clause in sorted(_clause_candidates(question), key=len, reverse=True):
        text = text.replace(clause, " ")
    text = _STOP_PHRASES.sub(" ", text)
    text = re.sub(r"[？?，,。；;：:\s]", "", text)
    return text.lower()


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
    asks_method = "方法" in question or "怎么测" in question or "如何测" in question
    asks_requirement = any(word in question for word in ("要求", "判定", "限值", "指标", "标准值"))
    if asks_method and chunk.get("kind") == "method":
        return 5.0
    if asks_requirement and chunk.get("kind") == "requirement":
        return 5.0
    if not asks_method and not asks_requirement and chunk.get("kind") == "requirement":
        return 1.0
    return 0.0


def answer(question: str, kbs: list[dict]) -> dict:
    question = (question or "").strip()
    if not question:
        return {"found": False, "answer": "请输入问题。", "citations": []}
    if not kbs:
        return {
            "found": False,
            "answer": "未找到相关标准内容：知识库中还没有已发布的标准。",
            "citations": [],
        }

    terms = _terms(question)
    all_chunks = [chunk for kb in kbs for chunk in kb.get("chunks", [])]
    chunk_clauses = {chunk.get("clause", "") for chunk in all_chunks}
    clauses = _clause_candidates(question)
    clause_hit = any(
        clause == asked or clause.startswith(asked + ".")
        for asked in clauses
        for clause in chunk_clauses
    )
    if clauses and not clause_hit:
        asked = sorted(clauses)[0]
        return {
            "found": False,
            "answer": f"未找到相关标准内容：已发布标准中没有条款 {asked}。",
            "citations": [],
        }

    asked_standards = _STANDARD.findall(question)
    if asked_standards and not clause_hit:
        known = [_std_key(kb.get("standard_no", "")) for kb in kbs]
        matched = False
        for asked in asked_standards:
            key = _std_key(asked)
            for known_no in known:
                base = re.sub(r"-\d{4}$", "", known_no)
                if key and (key == known_no or key in known_no or (base and base in key)):
                    matched = True
        if not matched:
            return {
                "found": False,
                "answer": (
                    f"未找到相关标准内容：知识库中没有标准 {asked_standards[0]}。"
                    "系统不会凭记忆或常识编造标准条款。"
                ),
                "citations": [],
            }

    focus = _focus(question)
    candidates: list[tuple[float, dict]] = []
    for chunk in all_chunks:
        score = _score(terms, chunk) + _kind_bonus(question, chunk)
        name_key = re.sub(r"\s+", "", chunk.get("item_name", "")).lower()
        if len(focus) >= 2 and (focus in name_key or name_key in focus):
            score += 50.0
        candidates.append((score, chunk))
    if not candidates:
        return {
            "found": False,
            "answer": "未找到相关标准内容：已发布标准中没有可检索的条款。",
            "citations": [],
        }
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = candidates[0]
    if not clause_hit and best_score < 20.0:
        names = "、".join(kb.get("standard_name", "") for kb in kbs[:3])
        return {
            "found": False,
            "answer": (
                "未找到相关标准内容。当前已发布标准：" + (names or "无") +
                "。系统不会凭记忆或常识编造标准条款。"
            ),
            "citations": [],
        }

    # The best chunk and, if the question is not kind-specific, the companion
    # chunk of the same item (requirement + method) for a complete citation.
    chosen = [best]
    for score, chunk in candidates[1:]:
        if len(chosen) >= 2:
            break
        if chunk.get("clause") == best.get("clause"):
            if _kind_bonus(question, best) == 0 or chunk.get("kind") != best.get("kind"):
                chosen.append(chunk)
                break

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
    return {"found": True, "answer": "\n".join(lines), "citations": citations}
