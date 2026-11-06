import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import bench_weknora_retrieval as bench


def test_run_calls_both_endpoints_and_keeps_rerank_meta():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode() or "{}")
            seen.append({
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": body,
            })
            if self.path.endswith("/auth/login"):
                payload = {"success": True, "token": "t"}
            elif self.path.endswith("/hybrid-search"):
                payload = {"success": True, "data": [{"id": "h"}]}
            elif self.path.endswith("/knowledge-search"):
                payload = {
                    "success": True,
                    "data": [{"id": "r"}],
                    "meta": {"rerank": {
                        "model_id": "builtin-local-rerank",
                        "candidate_count": 50,
                        "top_score": 0.91,
                    }},
                }
            else:  # pragma: no cover - defensive
                self.send_error(404)
                return
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        token = bench.login(base_url, "/api/v1/auth/login", "a@b", "pw", 5.0, False)
        report = bench.run(
            base_url, "kb1", token,
            [{"id": "q01", "question": "5.1 要求是什么？"}],
            repeat=1,
        )
    finally:
        server.shutdown()

    assert token == "t"
    paths = [item["path"] for item in seen]
    assert paths[0] == "/api/v1/auth/login"
    assert "/api/v1/knowledge-bases/kb1/hybrid-search" in paths
    assert "/api/v1/knowledge-search" in paths
    assert all(item["auth"] == "Bearer t" for item in seen[1:])
    hybrid = next(item["body"] for item in seen if item["path"].endswith("hybrid-search"))
    rerank = next(item["body"] for item in seen if item["path"].endswith("knowledge-search"))
    assert hybrid["query_text"] == "5.1 要求是什么？"
    assert hybrid["match_count"] == 6
    assert rerank["knowledge_base_ids"] == ["kb1"]
    assert rerank["rerank"] == {"enabled": True, "top_k": 15}
    assert report["hybrid"]["n"] == 1
    assert report["hybrid"]["found_rate"] == 1.0
    assert report["rerank"]["n"] == 1
    assert report["rerank"]["rerank"]["model_id"] == "builtin-local-rerank"
    text = bench.markdown(report)
    assert "builtin-local-rerank" in text
    assert "rerank" in text


def test_login_401_raises_without_retrying():
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            attempts.append(self.path)
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        with pytest.raises(bench.AuthError):
            bench.login(base_url, "/api/v1/auth/login", "a@b", "pw", 5.0, False)
        with pytest.raises(bench.AuthError):
            bench.login(base_url, "/api/v1/auth/login", "a@b", "pw", 5.0, False)
    finally:
        server.shutdown()
    assert attempts == ["/api/v1/auth/login", "/api/v1/auth/login"]
