#!/usr/bin/env python3
"""Benchmark WeKnora hybrid-search vs knowledge-search (rerank) directly.

P0's ``/api/v1/standards/ask`` uses its own ``mode=semantic`` path and has no
per-request rerank switch.  To compare the two WeKnora retrieval primitives,
log in for a JWT and call:

* ``POST /api/v1/knowledge-bases/{kb}/hybrid-search`` — raw hybrid recall, no
  rerank;
* ``POST /api/v1/knowledge-search`` with ``rerank.enabled=true`` — the
  knowledge-search endpoint with the tenant's builtin rerank model.

Credentials come from flags or the ``WEKNORA_*`` variables documented in the
deployment secrets file; they are never written to the report.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class AuthError(RuntimeError):
    """The WeKnora login or search request was rejected (401/429)."""


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percent / 100) * (len(ordered) - 1)))))
    return ordered[index]


def _stats(values: list[float]) -> dict:
    return {
        "p50": round(_percentile(values, 50), 1),
        "p95": round(_percentile(values, 95), 1),
        "avg": round(statistics.fmean(values), 1) if values else 0.0,
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


def _opener(insecure: bool):
    handlers = []
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _post(opener, url: str, body: dict, token: str = "", timeout: float = 180.0):
    """Return ``(first_byte_ms, complete_ms, payload)`` for one POST."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            first_byte_ms = (time.perf_counter() - started) * 1000
            raw = response.read()
            complete_ms = (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 429}:
            raise AuthError(f"WeKnora HTTP {exc.code}: {exc.reason}") from exc
        raise
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return first_byte_ms, complete_ms, payload


def login(base_url: str, login_path: str, email: str, password: str,
          timeout: float, insecure: bool) -> str:
    url = base_url.rstrip("/") + "/" + login_path.lstrip("/")
    opener = _opener(insecure)
    request = urllib.request.Request(
        url, data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AuthError(f"WeKnora 登录 HTTP {exc.code}: {exc.reason}") from exc
    token = str(payload.get("token") or "")
    if not payload.get("success") or not token:
        raise AuthError(f"WeKnora 登录失败：{payload.get('message') or 'no token'}")
    return token


def run(base_url: str, kb_id: str, token: str, cases: list[dict], *,
        arms: tuple[str, ...] = ("hybrid", "rerank"), repeat: int = 2,
        hybrid_count: int = 6, rerank_count: int = 30, rerank_top_k: int = 15,
        timeout: float = 180.0, insecure: bool = False) -> dict:
    opener = _opener(insecure)
    results: list[dict] = []
    for arm in arms:
        for case in cases:
            for attempt in range(max(1, int(repeat))):
                if arm == "hybrid":
                    url = f"{base_url.rstrip('/')}/api/v1/knowledge-bases/{kb_id}/hybrid-search"
                    body = {"query_text": case["question"], "match_count": hybrid_count}
                elif arm == "rerank":
                    url = f"{base_url.rstrip('/')}/api/v1/knowledge-search"
                    body = {
                        "query": case["question"],
                        "knowledge_base_ids": [kb_id],
                        "match_count": rerank_count,
                        "rerank": {"enabled": True, "top_k": rerank_top_k},
                    }
                else:
                    raise ValueError(f"unknown arm: {arm}")
                first_ms = total_ms = 0.0
                payload = {}
                error = ""
                try:
                    first_ms, total_ms, payload = _post(opener, url, body, token, timeout)
                except AuthError:
                    raise
                except Exception as exc:  # noqa: BLE001 - report per-case failures
                    error = f"{type(exc).__name__}: {exc}"
                results.append({
                    "arm": arm,
                    "id": case["id"],
                    "attempt": attempt + 1,
                    "first_token_ms": round(first_ms, 1),
                    "latency_ms": round(total_ms, 1),
                    "found": bool(payload.get("data")),
                    "rerank": (payload.get("meta") or {}).get("rerank"),
                    "error": error,
                })
    report = {
        "base_url": base_url,
        "kb_id": kb_id,
        "arms": list(arms),
        "results": results,
    }
    for arm in arms:
        rows = [row for row in results if row["arm"] == arm]
        first = [row["first_token_ms"] for row in rows if not row["error"]]
        total = [row["latency_ms"] for row in rows if not row["error"]]
        report[arm] = {
            "n": len(rows),
            "errors": sum(1 for row in rows if row["error"]),
            "found_rate": round(sum(1 for row in rows if row["found"]) / len(rows), 4) if rows else 0.0,
            "first_token_ms": _stats(first),
            "latency_ms": _stats(total),
            "rerank": next((row["rerank"] for row in rows if row.get("rerank")), None),
        }
    return report


def markdown(report: dict) -> str:
    lines = [
        "| 组 | n | 命中 | P50 首字 | P95 首字 | P50 完整 | P95 完整 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in report["arms"]:
        row = report[arm]
        lines.append(
            f"| {arm} | {row['n']} | {row['found_rate']:.1%} | "
            f"{row['first_token_ms']['p50']} ms | {row['first_token_ms']['p95']} ms | "
            f"{row['latency_ms']['p50']} ms | {row['latency_ms']['p95']} ms |"
        )
    rerank = report.get("rerank", {}).get("rerank") or {}
    if rerank:
        lines.append("")
        lines.append(
            f"- rerank：model={rerank.get('model_id')}、"
            f"candidate_count={rerank.get('candidate_count')}、"
            f"top_score={rerank.get('top_score')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--base-url", default=os.getenv("WEKNORA_BASE_URL", ""))
    parser.add_argument("--login-path", default=os.getenv("WEKNORA_LOGIN_PATH", "/api/v1/auth/login"))
    parser.add_argument("--email", default=os.getenv("WEKNORA_ADMIN_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("WEKNORA_ADMIN_PASSWORD", ""))
    parser.add_argument("--kb-id", default=os.getenv("WEKNORA_KB_ID", ""))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed)")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)
    if not (args.base_url and args.email and args.password and args.kb_id):
        parser.error("--base-url/--email/--password/--kb-id are required (or set WEKNORA_*)")
    try:
        cases = load_questions(args.questions)
        token = login(args.base_url, args.login_path, args.email, args.password,
                      args.timeout, args.insecure)
        report = run(args.base_url, args.kb_id, token, cases, repeat=args.repeat,
                     timeout=args.timeout, insecure=args.insecure)
    except AuthError as exc:
        print(f"auth stop: {exc}", file=sys.stderr)
        return 2
    print(markdown(report))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
