#!/usr/bin/env python3
"""Compare WeKnora hybrid-search vs knowledge-search (rerank) hit quality (T404).

The QA chain now defaults to ``/knowledge-search`` with rerank.  This script runs
the same query set through both paths (``rerank=False`` = raw hybrid-search,
``rerank=True`` = knowledge-search + rerank) and compares hit / citation /
refusal quality and latency, so the rerank switch can be judged on data instead
of feel.

Dataset format matches ``scripts/run_weknora_eval.py``::

    {"cases":[{"id":"q01","question":"...","expect":"hit|refuse",
               "standard_no":"...","clause":"5.1","tag":"direct"}]}

Usage::

    python scripts/compare_retrieval.py --from-store \
      --dataset eval/weknora_qa.demo.json \
      --report /tmp/retrieval-compare.json --markdown /tmp/retrieval-compare.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import standard_qa, standard_store  # noqa: E402
from scripts.run_weknora_eval import _citation_matches  # noqa: E402


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percent / 100) * (len(ordered) - 1)))))
    return ordered[index]


def evaluate(dataset: dict, *, rerank: bool, from_store: bool = False,
             repeat: int = 1) -> dict:
    kbs = standard_store.published_kbs() if from_store else (dataset.get("kbs") or [])
    cases = dataset.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset has no cases")
    latencies: list[float] = []
    hits = refusals = hit_ok = refusal_ok = citation_ok = 0
    results: list[dict] = []
    for case in cases:
        question = str(case.get("question") or "").strip()
        expected = str(case.get("expect") or "hit").lower()
        expected_no = str(case.get("standard_no") or "").strip()
        expected_clause = str(case.get("clause") or "").strip()
        for _ in range(max(1, int(repeat))):
            started = time.perf_counter()
            error = ""
            try:
                answer = standard_qa.answer(question, kbs, mode="weknora", rerank=rerank)
            except Exception as exc:  # noqa: BLE001 - report per-case failures
                answer = {"found": False, "citations": []}
                error = f"{type(exc).__name__}: {exc}"
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            found = bool(answer.get("found"))
            citations = answer.get("citations") or []
            if expected == "refuse":
                refusals += 1
                ok = not found and not error
                refusal_ok += 1 if ok else 0
            else:
                hits += 1
                cited = found and _citation_matches(citations, expected_no, expected_clause)
                citation_ok += 1 if cited else 0
                ok = cited and not error
                hit_ok += 1 if ok else 0
            results.append({
                "id": case.get("id", ""),
                "question": question,
                "expect": expected,
                "ok": ok,
                "latency_ms": round(latency, 1),
                "error": error,
            })
    return {
        "rerank": rerank,
        "kb_count": len(kbs),
        "cases": len(cases),
        "runs": len(results),
        "hit": {"total": hits, "ok": hit_ok},
        "refusal": {"total": refusals, "ok": refusal_ok},
        "citation_ok": citation_ok,
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
            "avg": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        },
        "results": results,
    }


def _rate(part: int, total: int) -> str:
    return f"{part / total:.1%}" if total else "-"


def markdown(hybrid: dict, rerank: dict) -> str:
    return "\n".join([
        "| 指标 | hybrid-search（无 rerank） | knowledge-search（带 rerank） |",
        "|---|---|---|",
        f"| 库内命中率 | {_rate(hybrid['hit']['ok'], hybrid['hit']['total'])} | "
        f"{_rate(rerank['hit']['ok'], rerank['hit']['total'])} |",
        f"| 引用准确率 | {_rate(hybrid['citation_ok'], hybrid['hit']['total'])} | "
        f"{_rate(rerank['citation_ok'], rerank['hit']['total'])} |",
        f"| 库外拒答率 | {_rate(hybrid['refusal']['ok'], hybrid['refusal']['total'])} | "
        f"{_rate(rerank['refusal']['ok'], rerank['refusal']['total'])} |",
        f"| P50 延迟 (ms) | {hybrid['latency_ms']['p50']} | {rerank['latency_ms']['p50']} |",
        f"| P95 延迟 (ms) | {hybrid['latency_ms']['p95']} | {rerank['latency_ms']['p95']} |",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--from-store", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", help="write JSON here")
    parser.add_argument("--markdown", help="write Markdown comparison here")
    args = parser.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    hybrid = evaluate(dataset, rerank=False, from_store=args.from_store, repeat=args.repeat)
    rerank = evaluate(dataset, rerank=True, from_store=args.from_store, repeat=args.repeat)
    text = markdown(hybrid, rerank)
    print(text)
    if args.report:
        Path(args.report).write_text(
            json.dumps({"hybrid": hybrid, "rerank": rerank}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.markdown:
        Path(args.markdown).write_text("# WeKnora 检索命中质量对比（T404）\n\n" + text + "\n",
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
