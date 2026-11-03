#!/usr/bin/env python3
"""P1-4 UAT smoke runner for a running P0 service.

The manifest keeps customer file paths out of the repository::

    {
      "name": "2026-11 UAT",
      "standards": [{"path": "/abs/std.pdf", "project": "UAT"}],
      "reports":   ["/abs/report.docx"],
      "quotes":    [{"path": "/abs/dvp.xlsx", "template": "/abs/sales.xlsx"}]
    }

The script logs in, uploads the samples, waits for 202 task completion when the
deployment has the async queue enabled, verifies export/review/publish, report
audit and quote self-check, checks empty/error paths and RBAC, then writes a
JSON + Markdown report.  ``--strict`` exits non-zero on any blocking failure.

Run against the local/staging service, never production during UAT:

    python scripts/uat_smoke.py --base-url http://127.0.0.1:8140 \\
        --manifest /path/to/uat-manifest.json --report uat-report.json \\
        --markdown uat-report.md --strict
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import httpx


class Uat:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(600.0), follow_redirects=True)
        self.cases: list[dict] = []
        self.notes: list[str] = []
        self.username = username
        self.password = password
        self.token_ok = False

    def close(self) -> None:
        self.client.close()

    def record(self, name: str, ok: bool, detail: str, **extra) -> bool:
        self.cases.append({"name": name, "ok": bool(ok), "detail": detail, **extra})
        return bool(ok)

    def login(self) -> bool:
        response = self.client.post(
            "/api/v1/auth/login", json={"username": self.username, "password": self.password}
        )
        self.token_ok = response.status_code == 200
        return self.record("登录", self.token_ok, f"HTTP {response.status_code}")

    def health(self) -> dict:
        response = self.client.get("/health")
        ok = response.status_code == 200
        payload = response.json() if ok else {}
        self.record("健康检查", ok, f"HTTP {response.status_code} version={payload.get('version', '')}")
        return payload

    def wait_task(self, task_id: str, timeout: float = 300.0) -> dict:
        deadline = time.time() + timeout
        payload: dict = {}
        while time.time() < deadline:
            response = self.client.get(f"/api/v1/tasks/{task_id}")
            if response.status_code != 200:
                return {"status": "http_error", "error": f"HTTP {response.status_code}"}
            payload = response.json()
            if payload.get("status") in {"succeeded", "failed", "cancelled"}:
                return payload
            time.sleep(0.5)
        return {"status": "timeout", "error": payload}

    def upload_standard(self, item: dict) -> dict | None:
        path = Path(item["path"])
        started = time.perf_counter()
        with path.open("rb") as handle:
            response = self.client.post(
                "/api/v1/standards/parse",
                files={"file": (path.name, handle, "application/pdf")},
                data={"project": item.get("project", "UAT")},
            )
        elapsed = time.perf_counter() - started
        if response.status_code not in {200, 202}:
            self.record(f"标准上传 {path.name}", False, f"HTTP {response.status_code}: {response.text[:200]}")
            return None
        payload = response.json()
        if response.status_code == 202 or payload.get("task_id"):
            task = self.wait_task(payload["task_id"])
            if task.get("status") != "succeeded":
                self.record(
                    f"标准解析 {path.name}", False,
                    f"task={task.get('status')} error={str(task.get('error'))[:200]}",
                )
                return None
            standard_id = (task.get("result") or {}).get("standard_id", "")
            elapsed = time.perf_counter() - started
        else:
            standard_id = payload.get("id", "")
        if not standard_id:
            self.record(f"标准解析 {path.name}", False, "响应没有 standard_id")
            return None
        detail = self.client.get(f"/api/v1/standards/{standard_id}")
        if detail.status_code != 200:
            self.record(f"标准详情 {path.name}", False, f"HTTP {detail.status_code}")
            return None
        record = detail.json()
        items = len(record.get("items") or [])
        export = self.client.get(f"/api/v1/standards/{standard_id}/export.xlsx")
        export_ok = export.status_code == 200 and export.content[:2] == b"PK" and len(export.content) > 1000
        self.record(
            f"标准解析 {path.name}", items > 0 and export_ok,
            f"{items} 项 / {record.get('meta', {}).get('pages', 0)} 页 / 导出 {len(export.content)} B / {elapsed:.1f}s",
            standard_id=standard_id, items=items, pages=record.get("meta", {}).get("pages", 0),
            seconds=round(elapsed, 1), export_bytes=len(export.content),
        )
        return record

    def review_and_publish(self, record: dict) -> None:
        items = record.get("items") or []
        if not items:
            self.notes.append(f"标准 {record.get('id')} 没有条目，跳过复核/发布")
            return
        item = items[0]
        revision_before = int(record.get("revision", 0))
        response = self.client.patch(
            f"/api/v1/standards/{record['id']}",
            json={
                "reviewer": "UAT",
                "items": [{
                    "clause": item.get("clause", ""),
                    "requirement": (item.get("requirement") or "") + "（UAT 复核）",
                    "method": item.get("method") or "",
                }],
            },
        )
        ok_review = response.status_code == 200
        detail = self.client.get(f"/api/v1/standards/{record['id']}")
        updated = detail.json() if detail.status_code == 200 else {}
        revision_after = int(updated.get("revision", 0))
        history = updated.get("review_history") or []
        self.record(
            "复核留痕", ok_review and revision_after > revision_before and bool(history),
            f"revision {revision_before}->{revision_after}, history={len(history)}",
        )
        publish = self.client.post(f"/api/v1/standards/{record['id']}/publish")
        if publish.status_code == 202:
            task = self.wait_task(publish.json()["task_id"])
            ok = task.get("status") == "succeeded"
            detail_text = f"task={task.get('status')} chunks={(task.get('result') or {}).get('chunks')}"
        else:
            ok = publish.status_code == 200
            detail_text = f"HTTP {publish.status_code} chunks={publish.json().get('chunks') if ok else ''}"
        self.record("发布知识库", ok, detail_text)

    def upload_report(self, path_text: str) -> None:
        path = Path(path_text)
        started = time.perf_counter()
        with path.open("rb") as handle:
            response = self.client.post(
                "/api/v1/reports/audit", files={"file": (path.name, handle, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
            )
        elapsed = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            self.record(f"报告审核 {path.name}", False, f"HTTP {response.status_code}: {response.text[:200]}")
            return
        payload = response.json()
        summary = payload.get("summary") or {}
        # A parsed report must at least produce the rule inventory; specific
        # business findings are recorded, not treated as UAT failures.
        ok = bool(payload.get("findings")) and "total" in summary
        self.record(
            f"报告审核 {path.name}", ok,
            f"fail={summary.get('fail')} warn={summary.get('warn')} pass={summary.get('pass')} {elapsed:.0f}ms",
            latency_ms=round(elapsed, 1),
        )

    def quote(self, item: dict) -> None:
        path = Path(item["path"])
        template = Path(item["template"]) if item.get("template") else None
        files = {
            "file": (path.name, path.read_bytes(), _mime(path)),
        }
        if template:
            files["template"] = (
                template.name,
                template.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        response = self.client.post("/api/v1/quotes/generate", files=files)
        if response.status_code != 200:
            self.record(f"报价 {path.name}", False, f"HTTP {response.status_code}: {response.text[:200]}")
            return
        payload = response.json()
        quote_id = payload.get("quote_id", "")
        crm = self.client.get(f"/api/v1/quotes/{quote_id}/crm.xlsx")
        sales = self.client.get(f"/api/v1/quotes/{quote_id}/sales.xlsx")
        verification = payload.get("sales_quote_verification") or {}
        summary = payload.get("summary") or {}
        priced = int(summary.get("priced_lines") or 0)
        ok = crm.status_code == 200 and crm.content[:2] == b"PK"
        lines = int(summary.get("lines") or 0)
        if priced > 0 and template and priced >= lines:
            ok = ok and sales.status_code == 200 and sales.content[:2] == b"PK"
            ok = ok and bool(verification.get("ok"))
        elif priced > 0 and template:
            # Mixed prices are a documented partial case: CRM draft is the
            # deliverable and the missing line is listed for follow-up.
            ok = ok and bool(payload.get("sales_quote_skipped_reason"))
        elif priced == 0:
            # DVP/测试大纲没有单价时，产品设计是只生成 CRM 草稿并提示补价；
            # 这不是报价失败，UAT 记为“符合预期但未生成销售表”。
            ok = ok and "缺少单价" in str(payload.get("sales_quote_skipped_reason", ""))
        self.record(
            f"报价 {path.name}", ok,
            f"{summary.get('lines')} 行/已定价 {priced}；"
            f"自检={verification.get('ok')} 跳过={payload.get('sales_quote_skipped_reason', '')}",
            quote_id=quote_id,
        )

    def negative_and_rbac(self) -> None:
        missing = self.client.get("/api/v1/standards/std_20260101000000_aaaaaa")
        self.record("空态/不存在标准", missing.status_code == 404, f"HTTP {missing.status_code}")
        bad = self.client.post(
            "/api/v1/standards/parse",
            files={"file": ("not-a-pdf.txt", b"hello", "text/plain")},
        )
        self.record("错误格式拒绝", bad.status_code == 400, f"HTTP {bad.status_code}")
        viewer = f"uat_view_{uuid.uuid4().hex[:6]}"
        created = self.client.post(
            "/api/v1/auth/users",
            json={"username": viewer, "password": "Uat-viewer-123", "role": "viewer"},
        )
        if created.status_code != 201:
            self.record("RBAC 只读账号创建", False, f"HTTP {created.status_code}: {created.text[:150]}")
            return
        with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(30.0)) as other:
            login = other.post(
                "/api/v1/auth/login", json={"username": viewer, "password": "Uat-viewer-123"}
            )
            forbidden = other.post(
                "/api/v1/standards/parse",
                files={"file": ("demo.pdf", b"%PDF-1.4 demo", "application/pdf")},
            )
        self.record(
            "RBAC viewer 403", login.status_code == 200 and forbidden.status_code == 403,
            f"login={login.status_code} parse={forbidden.status_code}",
        )

    def audit(self) -> None:
        response = self.client.get("/api/v1/audit/recent", params={"limit": 500})
        if response.status_code != 200:
            self.record("审计查询", False, f"HTTP {response.status_code}")
            return
        events = response.json().get("events") or []
        names = {str(event.get("event") or "") for event in events}
        expected = {"report_audited", "standard_parsed", "quote_generated"}
        found = names & expected
        self.record("审计覆盖", len(found) >= 2, f"events={sorted(found)}")


def _mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def markdown(report: dict) -> str:
    lines = [
        f"# P1-4 UAT 报告：{report['name']}",
        "",
        f"- 服务：`{report['base_url']}`，版本 `{report.get('version', '')}`",
        f"- 用例：{report['passed']}/{report['total']} 通过；阻塞失败 {report['failed']}",
        f"- 标准 {report['metrics'].get('standards', 0)} 份 / 报告 {report['metrics'].get('reports', 0)} 份 / 报价 {report['metrics'].get('quotes', 0)} 个",
        "",
        "| 用例 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for case in report["cases"]:
        lines.append(f"| {case['name']} | {'PASS' if case['ok'] else 'FAIL'} | {case['detail']} |")
    if report.get("notes"):
        lines.append("")
        lines.append("## 说明")
        lines.extend(f"- {note}" for note in report["notes"])
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--username", default=os.getenv("P0_UAT_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("P0_UAT_PASSWORD", ""))
    parser.add_argument("--report", default="", help="JSON report path")
    parser.add_argument("--markdown", default="", help="Markdown report path")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    if not args.password:
        print("需要 --password 或 P0_UAT_PASSWORD", file=__import__("sys").stderr)
        return 2
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    uat = Uat(args.base_url, args.username, args.password)
    started = time.time()
    try:
        health = uat.health()
        if not uat.login():
            raise RuntimeError("登录失败，无法继续 UAT")
        standards = []
        for item in manifest.get("standards") or []:
            record = uat.upload_standard(item)
            if record:
                standards.append(record)
        if standards:
            uat.review_and_publish(standards[0])
        for path in manifest.get("reports") or []:
            uat.upload_report(path)
        for item in manifest.get("quotes") or []:
            uat.quote(item)
        uat.negative_and_rbac()
        uat.audit()
    finally:
        uat.close()
    elapsed = time.time() - started
    cases = uat.cases
    failures = [case for case in cases if not case["ok"]]
    report = {
        "name": manifest.get("name", "P1-4 UAT"),
        "base_url": args.base_url,
        "version": health.get("version", "") if "health" in locals() else "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "elapsed_seconds": round(elapsed, 1),
        "total": len(cases),
        "passed": len(cases) - len(failures),
        "failed": len(failures),
        "metrics": {
            "standards": len(manifest.get("standards") or []),
            "reports": len(manifest.get("reports") or []),
            "quotes": len(manifest.get("quotes") or []),
        },
        "cases": cases,
        "notes": uat.notes,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(markdown(report))
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(markdown(report), encoding="utf-8")
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
