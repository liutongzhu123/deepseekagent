import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import bench_p1_latency


def test_load_questions_accepts_a_list_and_cases(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps(["5.1 要求是什么？", {"id": "q2", "question": "外观怎么做？"}]))
    cases = bench_p1_latency.load_questions(path)
    assert cases == [
        {"id": "q01", "question": "5.1 要求是什么？"},
        {"id": "q2", "question": "外观怎么做？"},
    ]


def test_run_aggregates_latency_and_records_flags(tmp_path, monkeypatch):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"cases": [{"question": "5.1"}, {"question": "5.2"}]}))
    monkeypatch.setattr(bench_p1_latency, "_snapshot", lambda: {"query_rewrite": True})
    monkeypatch.setattr(
        bench_p1_latency.standard_qa, "answer",
        lambda question, kbs, mode: {
            "found": True,
            "retrieval": {"mode": mode, "query_rewrite": {"applied": True, "cached": False}},
        },
    )
    report = bench_p1_latency.run(path, repeat=2, label="after")
    assert report["total"] == 4
    assert report["found_rate"] == 1.0
    assert report["config"] == {"query_rewrite": True}
    assert report["latency_ms"]["p95"] >= report["latency_ms"]["p50"] >= 0
    assert report["first_token_ms"]["p95"] >= 0


def test_percentile_handles_empty_and_ordered_values():
    assert bench_p1_latency._percentile([], 95) == 0.0
    assert bench_p1_latency._percentile([10, 20, 30], 50) == 20
    assert bench_p1_latency._percentile([10, 20, 30], 95) == 30


def test_compare_renders_delta_and_markdown():
    before = {"latency_ms": {"p50": 1000, "p95": 2000}, "first_token_ms": {"p50": 900, "p95": 1800},
              "found_rate": 1.0}
    after = {"latency_ms": {"p50": 800, "p95": 1600}, "first_token_ms": {"p50": 700, "p95": 1400},
             "found_rate": 1.0}
    text = bench_p1_latency.compare(before, after)
    assert "P95 首字/首字节延迟 (ms) | 1800 | 1400 | -400.0" in text
    assert "P95 完整响应 (ms) | 2000 | 1600 | -400.0" in text


def test_http_endpoint_measures_first_byte(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"found": True, "retrieval": {"mode": "weknora"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        path = tmp_path / "q.json"
        path.write_text(json.dumps(["5.1 要求是什么？"]))
        report = bench_p1_latency.run(
            path, endpoint="http", base_url=f"http://127.0.0.1:{server.server_address[1]}",
            repeat=1, label="live",
        )
        assert report["endpoint"] == "http"
        assert report["found_rate"] == 1.0
        assert report["first_token_ms"]["p50"] >= 0
        assert report["latency_ms"]["p50"] >= report["first_token_ms"]["p50"]
    finally:
        server.shutdown()


def test_run_requires_questions(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        bench_p1_latency.load_questions(path)
