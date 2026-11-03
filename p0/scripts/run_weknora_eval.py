#!/usr/bin/env python3
"""Reproducible Q&A evaluation for the WeKnora knowledge base (P1-3).

Dataset shape (JSON)::

    {
      "name": "...",
      "kbs": [ ... published kb documents ... ],   # optional with --from-store
      "cases": [
        {"id":"q01","question":"...","expect":"hit",
         "standard_no":"Q/DEMO 001-2026","clause":"5.1","tag":"direct"},
        {"id":"q02","question":"...","expect":"refuse","tag":"out_of_kb"}
      ]
    }

With ``--from-store`` the local published KBs (``standard_store.published_kbs``)
replace ``kbs``, so the same cases can run against production data.  ``--mode
auto`` uses WeKnora when configured, otherwise the keyword baseline; the demo
dataset therefore reproduces without the server.

The report records hit rate, refusal accuracy, citation accuracy and latency
(avg / p95), plus per-case results.  ``--strict`` exits non-zero on any miss, so
CI and release checks can gate on it.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import standard_qa, weknora  # noqa: E402


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percent / 100) * (len(ordered) - 1)))))
    return ordered[index]


def _citation_matches(citations: list[dict], expected_no: str, expected_clause: str) -> bool:
    for citation in citations:
        if expected_no and str(citation.get("standard_no") or "").strip() != expected_no:
            continue
        clause = str(citation.get("clause") or "").strip()
        if expected_clause and not (
            clause == expected_clause or clause.startswith(expected_clause + ".")
        ):
            continue
        return True
    return False


def evaluate(dataset: dict, *, mode: str = "auto", from_store: bool = False) -> dict:
    if from_store:
        from app import standard_store

        kbs = standard_store.published_kbs()
    else:
        kbs = dataset.get("kbs") or []
    chosen_mode = mode
    if mode == "auto":
        chosen_mode = "weknora" if weknora.configured() else "keyword"
    if chosen_mode not in {"weknora", "keyword"}:
        raise ValueError("mode must be auto/weknora/keyword")
    cases = dataset.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset has no cases")
    results: list[dict] = []
    latencies: list[float] = []
    for case in cases:
        question = str(case.get("question") or "").strip()
        expected = str(case.get("expect") or "hit").strip().lower()
        started = time.perf_counter()
        error = ""
        try:
            answer = standard_qa.answer(question, kbs, mode=chosen_mode)
        except weknora.WeKnoraUnavailable as exc:
            answer = {"found": False, "answer": f"WeKnora 不可用：{exc}", "citations": []}
            error = "weknora_unavailable"
        except Exception as exc:  # noqa: BLE001 - report per-case failures
            answer = {"found": False, "answer": f"{type(exc).__name__}: {exc}", "citations": []}
            error = "exception"
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies.append(elapsed_ms)
        found = bool(answer.get("found"))
        citations = answer.get("citations") or []
        expected_no = str(case.get("standard_no") or "").strip()
        expected_clause = str(case.get("clause") or "").strip()
        if expected == "refuse":
            ok = not found and not error
            citation_ok = not citations
        else:
            citation_ok = found and _citation_matches(citations, expected_no, expected_clause)
            ok = citation_ok and not error
        results.append(
            {
                "id": case.get("id", ""),
                "question": question,
                "expect": expected,
                "tag": case.get("tag", ""),
                "expected_standard_no": expected_no,
                "expected_clause": expected_clause,
                "found": found,
                "ok": ok,
                "citation_ok": citation_ok,
                "mode": (answer.get("retrieval") or {}).get("mode", chosen_mode),
                "top_score": (answer.get("retrieval") or {}).get("top_score"),
                "latency_ms": round(elapsed_ms, 1),
                "error": error,
                "answer": str(answer.get("answer") or "")[:500],
            }
        )

    hits = [item for item in results if item["expect"] == "hit"]
    refusals = [item for item in results if item["expect"] == "refuse"]
    hit_ok = sum(1 for item in hits if item["ok"])
    refusal_ok = sum(1 for item in refusals if item["ok"])
    by_tag: dict[str, dict] = {}
    for item in results:
        bucket = by_tag.setdefault(item["tag"] or "untagged", {"total": 0, "ok": 0})
        bucket["total"] += 1
        bucket["ok"] += 1 if item["ok"] else 0
    for bucket in by_tag.values():
        bucket["accuracy"] = round(bucket["ok"] / bucket["total"], 4) if bucket["total"] else 0.0
    return {
        "name": dataset.get("name", ""),
        "mode": chosen_mode,
        "kb_count": len(kbs),
        "chunk_count": sum(len(kb.get("chunks") or []) for kb in kbs),
        "total": len(results),
        "hit": {"total": len(hits), "ok": hit_ok, "accuracy": round(hit_ok / len(hits), 4) if hits else 0.0},
        "refusal": {
            "total": len(refusals),
            "ok": refusal_ok,
            "accuracy": round(refusal_ok / len(refusals), 4) if refusals else 0.0,
        },
        "citation_accuracy": round(hit_ok / len(hits), 4) if hits else 0.0,
        "latency_ms": {
            "avg": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "p95": round(_percentile(latencies, 95), 1),
        },
        "by_tag": by_tag,
        "results": results,
    }


def markdown(report: dict) -> str:
    lines = [
        f"# WeKnora Q&A 评测：{report['name']}",
        "",
        f"- 模式：`{report['mode']}`；知识库 {report['kb_count']} 份 / {report['chunk_count']} 片段",
        f"- 用例：{report['total']}",
        f"- 库内命中：{report['hit']['ok']}/{report['hit']['total']}（{report['hit']['accuracy']:.1%}）",
        f"- 库外拒答：{report['refusal']['ok']}/{report['refusal']['total']}（{report['refusal']['accuracy']:.1%}）",
        f"- 引用准确：{report['citation_accuracy']:.1%}",
        f"- 延迟：平均 {report['latency_ms']['avg']} ms / P95 {report['latency_ms']['p95']} ms",
    ]
    if report.get("by_tag"):
        lines.append("")
        lines.append("| 标签 | 正确/总数 | 准确率 |")
        lines.append("| --- | --- | --- |")
        for tag, bucket in sorted(report["by_tag"].items()):
            lines.append(f"| {tag} | {bucket['ok']}/{bucket['total']} | {bucket['accuracy']:.1%} |")
    failures = [item for item in report["results"] if not item["ok"]]
    lines.append("")
    lines.append("## 未通过用例" if failures else "## 结果：全部通过")
    for item in failures:
        lines.append(
            f"- `{item['id']}` {item['expect']} / {item['tag']}：{item['question']}"
            f"（found={item['found']}，citation_ok={item['citation_ok']}，error={item['error']}）"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--from-store", action="store_true", help="use locally published KBs instead of dataset.kbs")
    parser.add_argument("--mode", choices=["auto", "weknora", "keyword"], default="auto")
    parser.add_argument("--report", default="", help="write JSON report here")
    parser.add_argument("--markdown", default="", help="write Markdown report here")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any case fails")
    args = parser.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    try:
        report = evaluate(dataset, mode=args.mode, from_store=args.from_store)
    except (ValueError, weknora.WeKnoraError, weknora.WeKnoraUnavailable) as exc:
        print(f"评测失败：{exc}", file=sys.stderr)
        return 2
    markdown_text = markdown(report)
    print(markdown_text)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(markdown_text, encoding="utf-8")
    if args.strict and any(not item["ok"] for item in report["results"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
