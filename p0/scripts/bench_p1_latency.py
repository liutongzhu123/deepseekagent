#!/usr/bin/env python3
"""Measure retrieval latency for the P1 QA path (T401 baseline / before-after).

The P0 ``/api/v1/standards/ask`` endpoint returns the answer JSON in one
response (no token stream), so the first user-visible byte arrives when the
response headers do.  This harness measures both:

* ``first_token_ms`` — time to the first byte / first visible content;
* ``latency_ms`` — time to the complete answer.

It runs a specified question set against either the in-process chain
(``--endpoint local``) or a live P0 deployment (``--endpoint http``), repeats
each question, and writes p50/p95 plus the feature flags that were active.

    # live baseline (preprod; credentials from env or flags)
    python scripts/bench_p1_latency.py --endpoint http \
      --base-url https://zhongqi.yunanht.com/p0 \
      --username "$P0_BENCH_USERNAME" --password "$P0_BENCH_PASSWORD" \
      --questions eval/p1_latency_questions.json --repeat 3 \
      --label baseline --json /tmp/baseline.json

    # after deployment
    python scripts/bench_p1_latency.py --endpoint http ... --label after --json /tmp/after.json
    python scripts/bench_p1_latency.py --compare /tmp/baseline.json /tmp/after.json \
      --markdown /tmp/compare.md
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
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
        "weknora_rerank": os.getenv("P0_WEKNORA_RERANK", "1"),
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


def _opener(base_url: str, timeout: float, username: str, password: str,
            insecure: bool = False):
    """Build an opener, logging in first when credentials are given.

    ``insecure`` skips TLS verification for the self-signed preprod host.
    """
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    if username and password:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/v1/auth/login",
            data=json.dumps({"username": username, "password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=timeout) as response:
            response.read()
    return opener


def _http_call(opener, base_url: str, question: str, timeout: float,
               token: str = "", mode: str = "weknora") -> tuple[float, float, dict]:
    """Return ``(first_byte_ms, complete_ms, payload)`` for one live ask."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/standards/ask",
        data=json.dumps({"question": question, "mode": mode}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        first_byte_ms = (time.perf_counter() - started) * 1000
        body = response.read()
    complete_ms = (time.perf_counter() - started) * 1000
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return first_byte_ms, complete_ms, payload


def run(dataset_path: str | Path, *, mode: str = "weknora", repeat: int = 1,
        from_store: bool = False, label: str = "", endpoint: str = "local",
        base_url: str = "", timeout: float = 60.0,
        username: str = "", password: str = "", token: str = "",
        insecure: bool = False) -> dict:
    cases = load_questions(dataset_path)
    kbs = standard_store.published_kbs() if from_store else []
    repeat = max(1, int(repeat))
    opener = _opener(base_url, timeout, username, password, insecure) if endpoint == "http" else None
    results: list[dict] = []
    first_tokens: list[float] = []
    latencies: list[float] = []
    for case in cases:
        for attempt in range(repeat):
            error = ""
            first_byte_ms = 0.0
            elapsed_ms = 0.0
            try:
                if endpoint == "http":
                    first_byte_ms, elapsed_ms, answer = _http_call(
                        opener, base_url, case["question"], timeout, token, mode)
                else:
                    started = time.perf_counter()
                    answer = standard_qa.answer(case["question"], kbs, mode=mode)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    first_byte_ms = elapsed_ms
            except Exception as exc:  # noqa: BLE001 - report per-case failures
                answer = {"found": False, "retrieval": {}}
                error = f"{type(exc).__name__}: {exc}"
                first_byte_ms = first_byte_ms or 0.0
                elapsed_ms = first_byte_ms
            first_tokens.append(first_byte_ms)
            latencies.append(elapsed_ms)
            results.append({
                "id": case["id"],
                "attempt": attempt + 1,
                "first_token_ms": round(first_byte_ms, 1),
                "latency_ms": round(elapsed_ms, 1),
                "found": bool(answer.get("found")),
                "retrieval_mode": (answer.get("retrieval") or {}).get("mode", mode),
                "query_rewrite": (answer.get("retrieval") or {}).get("query_rewrite"),
                "error": error,
            })
    return {
        "label": label,
        "mode": mode,
        "endpoint": endpoint,
        "base_url": base_url,
        "from_store": from_store,
        "kb_count": len(kbs),
        "chunk_count": sum(len(kb.get("chunks") or []) for kb in kbs),
        "repeat": repeat,
        "total": len(results),
        "config": _snapshot(),
        "first_token_ms": _stats(first_tokens),
        "latency_ms": _stats(latencies),
        "found_rate": round(sum(1 for item in results if item["found"]) / len(results), 4)
        if results else 0.0,
        "results": results,
    }


def _stats(values: list[float]) -> dict:
    return {
        "p50": round(_percentile(values, 50), 1),
        "p95": round(_percentile(values, 95), 1),
        "avg": round(statistics.fmean(values), 1) if values else 0.0,
        "min": round(min(values), 1) if values else 0.0,
        "max": round(max(values), 1) if values else 0.0,
    }


def markdown(report: dict) -> str:
    config = report.get("config") or {}
    lines = [
        f"### {report.get('label') or 'run'}（`{report.get('endpoint')}` / `{report.get('mode')}`）",
        "",
        f"- 知识库：{report.get('kb_count')} 份 / {report.get('chunk_count')} 片段；"
        f"用例 {report.get('total')} 次（repeat={report.get('repeat')}）",
        f"- 命中：{report.get('found_rate', 0):.1%}",
        f"- 首字/首字节延迟：P50 {report['first_token_ms']['p50']} ms / "
        f"P95 {report['first_token_ms']['p95']} ms / 平均 {report['first_token_ms']['avg']} ms",
        f"- 完整响应延迟：P50 {report['latency_ms']['p50']} ms / "
        f"P95 {report['latency_ms']['p95']} ms / 平均 {report['latency_ms']['avg']} ms",
        f"- 开关：rewrite={config.get('query_rewrite')} ({config.get('query_rewrite_model')})、"
        f"cache={config.get('query_rewrite_cache')}、"
        f"rerank={config.get('weknora_rerank')} top_k={config.get('rerank_top_k')}",
    ]
    if report.get("base_url"):
        lines.append(f"- 目标：{report['base_url']}")
    return "\n".join(lines)


def compare(before: dict, after: dict) -> str:
    def row(label: str, b, a):
        delta = (a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        sign = f"{delta:+.1f}" if delta is not None else "-"
        return f"| {label} | {b} | {a} | {sign} |"

    before_first = before.get("first_token_ms") or before["latency_ms"]
    after_first = after.get("first_token_ms") or after["latency_ms"]
    return "\n".join([
        "| 指标 | 改前 | 改后 | 变化 |",
        "|---|---|---|---|",
        row("P50 首字/首字节延迟 (ms)", before_first["p50"], after_first["p50"]),
        row("P95 首字/首字节延迟 (ms)", before_first["p95"], after_first["p95"]),
        row("P50 完整响应 (ms)", before["latency_ms"]["p50"], after["latency_ms"]["p50"]),
        row("P95 完整响应 (ms)", before["latency_ms"]["p95"], after["latency_ms"]["p95"]),
        row("命中率", before.get("found_rate"), after.get("found_rate")),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", help="JSON list of questions or {cases:[...]}")
    parser.add_argument("--from-store", action="store_true", help="use published KBs from the local store")
    parser.add_argument("--mode", default="weknora", choices=["auto", "weknora", "local", "vector", "keyword"])
    parser.add_argument("--endpoint", default="local", choices=["local", "http"])
    parser.add_argument("--base-url", default=os.getenv("P0_BENCH_BASE_URL", ""))
    parser.add_argument("--token", default=os.getenv("P0_BENCH_TOKEN", ""))
    parser.add_argument("--username", default=os.getenv("P0_BENCH_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("P0_BENCH_PASSWORD", ""))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (self-signed preprod)")
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
    if args.endpoint == "http" and not args.base_url:
        parser.error("--base-url is required with --endpoint http")
    report = run(
        args.questions, mode=args.mode, repeat=args.repeat, from_store=args.from_store,
        label=args.label, endpoint=args.endpoint, base_url=args.base_url,
        timeout=args.timeout, username=args.username, password=args.password,
        token=args.token, insecure=args.insecure,
    )
    text = markdown(report)
    print(text)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
