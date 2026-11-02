#!/usr/bin/env python3
"""Compare the P0 keyword baseline with P1 bge-m3 vector retrieval.

Usage (on the P0 box, embedding service reachable):

  python3 scripts/run_kb_vector_demo.py \
    --standards-dir /opt/cq-ai-p0/data/standards \
    --questions questions.json \
    --out-dir deliverables/qa-vector \
    --embedding-url http://127.0.0.1:11434 \
    --model bge-m3

The questions JSON contains five in-library and two out-of-library questions
with ``expect_found``/``must_contain`` labels; keep real customer questions in
the ticket workspace, not in this public repository.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import standard_qa  # noqa: E402
from app.embeddings import OllamaEmbedder, VectorIndex  # noqa: E402

def load_kbs(directory: Path) -> list[dict]:
    kbs: list[dict] = []
    for path in sorted(directory.glob("*/kb.json")):
        try:
            kbs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return kbs


def verdict(question: dict, result: dict) -> dict:
    text = result.get("answer", "")
    contained = [term for term in question.get("must_contain", []) if term in text]
    if question["expect_found"]:
        passed = bool(result.get("found")) and len(contained) == len(question.get("must_contain", []))
    else:
        passed = not result.get("found") and "未找到" in text
    return {
        "passed": passed,
        "contained": contained,
        "top_clause": (result.get("citations") or [{}])[0].get("clause", ""),
        "citations": len(result.get("citations") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standards-dir", default=str(ROOT / "data" / "standards"))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--questions", required=True, help="questions JSON with expect_found/must_contain")
    parser.add_argument("--embedding-url", default=os.getenv("P0_EMBEDDING_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--model", default=os.getenv("P0_EMBEDDING_MODEL", "bge-m3"))
    parser.add_argument("--min-score", type=float, default=standard_qa.DEFAULT_MIN_SCORE)
    parser.add_argument("--top-k", type=int, default=standard_qa.DEFAULT_TOP_K)
    parser.add_argument("--cache", default=str(ROOT / "data" / "standards" / "kb_vectors.json"))
    parser.add_argument("--reset-cache", action="store_true")
    parser.add_argument("--keyword-only", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if not isinstance(questions, list) or not questions:
        print("questions JSON must be a non-empty list", file=sys.stderr)
        return 2
    kbs = load_kbs(Path(args.standards_dir))
    if not kbs:
        print(f"no published kb.json under {args.standards_dir}", file=sys.stderr)
        return 2
    chunks = [chunk for kb in kbs for chunk in kb.get("chunks", [])]

    embedder = None
    index = None
    if not args.keyword_only:
        os.environ["P0_EMBEDDING_URL"] = args.embedding_url
        os.environ["P0_EMBEDDING_MODEL"] = args.model
        embedder = OllamaEmbedder(base_url=args.embedding_url, model=args.model)
        print(f"building bge-m3 index for {len(chunks)} chunks (cache: {args.cache}) ...", flush=True)
        index = VectorIndex.from_chunks(
            chunks,
            embedder,
            cache_path=args.cache,
            reset=args.reset_cache,
        )

    rows: list[dict] = []
    for item in questions:
        keyword = standard_qa.answer_keyword(item["question"], kbs)
        row = {
            "id": item["id"],
            "question": item["question"],
            "kind": item["kind"],
            "expect_found": item["expect_found"],
            "keyword": {
                "result": keyword,
                "verdict": verdict(item, keyword),
                "top_score": keyword.get("retrieval", {}).get("top_score"),
            },
        }
        if index is not None and embedder is not None:
            vector = standard_qa.answer_vector(
                item["question"],
                kbs,
                embedder,
                cache_path=args.cache,
                min_score=args.min_score,
                top_k=args.top_k,
            )
            row["vector"] = {
                "result": vector,
                "verdict": verdict(item, vector),
                "top_score": vector.get("retrieval", {}).get("top_score"),
                "candidates": vector.get("retrieval", {}).get("candidates", []),
            }
        rows.append(row)

    keyword_pass = sum(1 for row in rows if row["keyword"]["verdict"]["passed"])
    vector_pass = sum(1 for row in rows if row.get("vector", {}).get("verdict", {}).get("passed"))
    improved = [
        row["id"]
        for row in rows
        if not row["keyword"]["verdict"]["passed"] and row.get("vector", {}).get("verdict", {}).get("passed")
    ]
    summary = {
        "standards": len(kbs),
        "chunks": len(chunks),
        "embedding_model": args.model,
        "embedding_url": args.embedding_url,
        "min_score": args.min_score,
        "top_k": args.top_k,
        "questions": len(rows),
        "keyword_pass": keyword_pass,
        "vector_pass": vector_pass,
        "vector_improved": improved,
        "cache": args.cache,
    }
    (out_dir / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "qa_compare.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# 知识问答：关键词 vs bge-m3 向量检索",
        "",
        f"- 标准：{len(kbs)} 份；知识片段：{len(chunks)} 个",
        f"- 向量模型：`{args.model}` @ `{args.embedding_url}`（现有 WeKnora 的 Ollama，免费本地）",
        f"- 相似度阈值：{args.min_score:.2f}；Top-K：{args.top_k}",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 问题数 | {len(rows)} |",
        f"| 关键词命中/正确拒答 | {keyword_pass}/{len(rows)} |",
        f"| 向量命中/正确拒答 | {vector_pass}/{len(rows)} |",
        f"| 向量相对关键词-improved | {', '.join(improved) or '无'} |",
        "",
        "## 逐题对比",
        "",
        "| # | 类型 | 预期 | 关键词 | 向量 | 备注 |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        expected = "命中" if row["expect_found"] else "拒答"
        keyword_ok = "✅" if row["keyword"]["verdict"]["passed"] else "❌"
        vector_ok = "✅" if row.get("vector", {}).get("verdict", {}).get("passed") else "❌"
        note = "向量改进" if row["id"] in improved else ""
        lines.append(
            f"| {row['id']} | {row['kind']} | {expected} | {keyword_ok} | {vector_ok} | {note} |"
        )

    lines += ["", "## 关键问题明细", ""]
    for row in rows:
        lines += [f"### {row['id']} {row['question']}", ""]
        keyword = row["keyword"]["result"]
        lines.append(f"- 关键词：found={keyword['found']}，top={keyword['retrieval'].get('top_score')}")
        if row.get("vector"):
            vector = row["vector"]["result"]
            lines.append(
                f"- 向量：found={vector['found']}，top={vector['retrieval'].get('top_score')}，"
                f"clause={(vector.get('citations') or [{}])[0].get('clause', '')}"
            )
            candidates = row["vector"]["candidates"][:3]
            if candidates:
                lines.append(
                    "- Top3："
                    + "；".join(
                        f"{candidate['clause']} {candidate['item_name']} ({candidate['score']:.4f})"
                        for candidate in candidates
                    )
                )
        lines.append("")
    lines += [
        "## 说明",
        "",
        "- 关键词基线是 P0 的 n-gram/条款打分；向量模式用 bge-m3 对「标准名+条款+项目名+正文」做 embedding。",
        "- 条款号精确命中与库外标准/库外条款仍走确定性守卫；语义相似度低于阈值的库外问题明确拒答。",
        "- 向量索引缓存在 `kb_vectors.json`，仅缓存 chunk id+文本哈希一致的向量。",
    ]
    (out_dir / "qa_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"out -> {out_dir}")
    return 0 if (vector_pass == len(rows) or args.keyword_only) else 1


if __name__ == "__main__":
    raise SystemExit(main())
