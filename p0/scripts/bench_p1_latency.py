#!/usr/bin/env python3
"""Measure first-result latency for the P1 QA path (T401 acceptance).

The P0 ``/api/v1/standards/ask`` endpoint returns the answer JSON in one
response (there is no token stream), so the first user-visible content arrives
with that response.  This harness measures that wall-clock time (called
"first-result" latency below) per question, repeated N times, and writes the
p50/p95 plus the feature flags that were active.

It does not invent a baseline: run it on the machine that has the five published
standards (``--from-store``) twice, with the flags off and on.

    # baseline
    P0_QUERY_REWRITE=0 P0_QUERY_REWRITE_CACHE=0 P0_WEKNORA_RERANK=0 \
      python scripts/bench_p1_latency.py --from-store --questions q.json \
      --repeat 3 --label baseline --json /tmp/baseline.json
    # after
    P0_QUERY_REWRITE=1 P0_QUERY_REWRITE_CACHE=1 P0_WEKNORA_RERANK=1 \
      python scripts/bench_p1_latency.py --from-store --questions q.json \
      --repeat 3 --label after --json /tmp/after.json
    # compare -> Markdown
    python scripts/bench_p1_latency.py --compare /tmp/baseline.json /tmp/after.json \
      --markdown docs/eval-p1-latency.md
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import standard_qa, standard_store  # noqa: E402


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percent / 100) * (len(ordered) - 1)))))
    return ordered[index]


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "on", "yes"}


def _snapshot() -> dict:
    return {
        "query_rewrite": _flag("P0_QUERY_REWRITE"),
        "query_rewrite_model": os.getenv("P0_QUERY_REWRITE_MODEL", "deepseek-chat"),
        "query_rewrite_cache": _flag("P0_QUERY_REWRITE_CACHE"),
        "redis_url": os.getenv("P0_REDIS_URL", ""),
        "weknora_rerank": _flag("P0_WEKNORA_RERANK"),
        "rerank_top_k": os.getenv("P0_WEKNORA_RERANK_TOP_K", "15"),
        "rerank_candidates": os.getenv("P0_WEKNORA_RERANK_CANDIDATES", "30"),
        "embedding_batch": os.getenv("P0_EMBEDDING_BATCH", "64"),
    }


def load_questions(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases") or []
    if not isinstance(data, list) or not data:
        raise ValueError("questions file must contain a non-empty list or {cases:[...]}")
    cases = []
    for item in data:
        if isinstance(item, str):
            item = {"question": item}
        question = str(item.get("question") or "").strip()
        if not question:
            raise ValueError("each case needs a non-empty question")
        cases.append({"id": str(item.get("id") or f"q{len(cases) + 1:02d}"), "question": question})
    return cases


def run(dataset_path: str | Path, *, mode: str = "weknora", repeat: int = 1,
        from_store: bool = False, label: str = "") -> dict:
    cases = load_questions(dataset_path)
    kbs = standard_store.published_kbs() if from_store else []
    repeat = max(1, int(repeat))
    results: list[dict] = []
    latencies: list[float] = []
    for case in cases:
        for attempt in range(repeat):
            started = time.perf_counter()
            error = ""
            try:
                answer = standard_qa.answer(case["question"], kbs, mode=mode)
            except Exception as exc:  # noqa: BLE001 - report per-case failures
                answer = {"found": False, "retrieval": {}}
                error = f"{type(exc).__name__}: {exc}"
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            results.append({
                "id": case["id"],
                "attempt": attempt + 1,
                "latency_ms": round(elapsed_ms, 1),
                "found": bool(answer.get("found")),
                "retrieval_mode": (answer.get("retrieval") or {}).get("mode", mode),
                "query_rewrite": (answer.get("retrieval") or {}).get("query_rewrite"),
                "error": error,
            })
    return {
        "label": label,
        "mode": mode,
        "from_store": from_store,
        "kb_count": len(kbs),
        "chunk_count": sum(len(kb.get("chunks") or []) for kb in kbs),
        "repeat": repeat,
        "total": len(results),
        "config": _snapshot(),
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
            "avg": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "min": round(min(latencies), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "found_rate": round(sum(1 for item in results if item["found"]) / len(results), 4)
        if results else 0.0,
        "results": results,
    }


def markdown(report: dict) -> str:
    config = report.get("config") or {}
    lines = [
        f"### {report.get('label') or 'run'}（`{report.get('mode')}`）",
        "",
        f"- 知识库：{report.get('kb_count')} 份 / {report.get('chunk_count')} 片段；"
        f"用例 {report.get('total')} 次（repeat={report.get('repeat')}）",
        f"- 命中：{report.get('found_rate', 0):.1%}",
        f"- 首结果延迟：P50 {report['latency_ms']['p50']} ms / "
        f"P95 {report['latency_ms']['p95']} ms / 平均 {report['latency_ms']['avg']} ms",
        f"- 开关：rewrite={config.get('query_rewrite')} ({config.get('query_rewrite_model')})、"
        f"cache={config.get('query_rewrite_cache')}、"
        f"rerank={config.get('weknora_rerank')} top_k={config.get('rerank_top_k')}",
    ]
    return "\n".join(lines)


def compare(before: dict, after: dict) -> str:
    def row(label: str, b, a):
        delta = (a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        sign = f"{delta:+.1f}" if delta is not None else "-"
        return f"| {label} | {b} | {a} | {sign} |"

    return "\n".join([
        "| 指标 | 改前 | 改后 | 变化 |",
        "|---|---|---|---|",
        row("P50 首结果延迟 (ms)", before["latency_ms"]["p50"], after["latency_ms"]["p50"]),
        row("P95 首结果延迟 (ms)", before["latency_ms"]["p95"], after["latency_ms"]["p95"]),
        row("平均首结果延迟 (ms)", before["latency_ms"]["avg"], after["latency_ms"]["avg"]),
        row("命中率", before.get("found_rate"), after.get("found_rate")),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", help="JSON list of questions or {cases:[...]}")
    parser.add_argument("--from-store", action="store_true", help="use published KBs from the local store")
    parser.add_argument("--mode", default="weknora", choices=["auto", "weknora", "local", "vector", "keyword"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label", default="")
    parser.add_argument("--json", dest="json_path", help="write the JSON report here")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="compare two JSON reports")
    parser.add_argument("--markdown", help="write Markdown here (compare mode)")
    args = parser.parse_args(argv)

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        text = compare(before, after)
        if args.markdown:
            Path(args.markdown).write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    if not args.questions:
        parser.error("--questions is required unless --compare is used")
    report = run(args.questions, mode=args.mode, repeat=args.repeat,
                 from_store=args.from_store, label=args.label)
    text = markdown(report)
    print(text)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
