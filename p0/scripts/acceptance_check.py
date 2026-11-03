#!/usr/bin/env python3
"""Post-deploy P1 acceptance sweep (read-only after login).

Checks the release version, login/identity, the main read endpoints that back
the five workbenches and (for admins) the audit/feedback summaries.  Uploads,
publishing to WeKnora and business judgment stay manual; this script only
reports whether the deployed environment is wired up.

    .venv/bin/python scripts/acceptance_check.py \
      --base-url https://<DOMAIN>/p0 --expect-version 0.8.3 \
      --username admin --report /var/backups/cq-ai-p0/acceptance.md

Password comes from ``--password``, ``P0_ACCEPT_PASSWORD`` or a getpass prompt.
Exit code is 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _check(checks: list[dict], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run_checks(
    base_url: str,
    expect_version: str,
    username: str,
    password: str,
    *,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> dict:
    base = base_url.rstrip("/")
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    checks: list[dict] = []
    try:
        health = client.get(f"{base}/health")
        payload = {}
        try:
            payload = health.json()
        except ValueError:
            payload = {}
        _check(
            checks, "health",
            health.status_code == 200 and payload.get("status") == "ok",
            f"status={health.status_code} body={payload.get('status', '')}",
        )
        version = str(payload.get("version", ""))
        _check(checks, "version", version == expect_version, f"expected={expect_version} actual={version or '—'}")
        _check(checks, "auth", str(payload.get("auth", "")) == "enabled", f"auth={payload.get('auth', '—')}")

        login = client.post(
            f"{base}/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        _check(checks, "login", login.status_code == 200, f"status={login.status_code}")
        me = client.get(f"{base}/api/v1/auth/me")
        me_body = {}
        try:
            me_body = me.json()
        except ValueError:
            me_body = {}
        user = (me_body.get("user") or {}) if isinstance(me_body, dict) else {}
        _check(
            checks, "identity",
            me.status_code == 200 and user.get("username") == username,
            f"status={me.status_code} user={user.get('username', '—')} role={user.get('role', '—')}",
        )
        role = str(user.get("role", ""))

        read_checks = (
            ("standards", "/api/v1/standards", "standards"),
            ("reports", "/api/v1/reports", "reports"),
            ("quotes", "/api/v1/quotes", "quotes"),
        )
        for name, path, key in read_checks:
            response = client.get(f"{base}{path}")
            count = ""
            if response.status_code == 200:
                try:
                    body = response.json()
                    count = f"{len(body.get(key) or [])} item(s)"
                except ValueError:
                    count = "non-json"
            _check(checks, name, response.status_code == 200, f"status={response.status_code} {count}".strip())

        if role == "admin":
            for name, path in (
                ("audit", "/api/v1/audit/events?limit=1"),
                ("feedback", "/api/v1/kb/feedback?limit=1"),
                ("system", "/api/v1/system/status"),
            ):
                response = client.get(f"{base}{path}")
                _check(checks, name, response.status_code == 200, f"status={response.status_code}")
        else:
            _check(checks, "admin-endpoints", True, f"skipped (role={role})")

        logout = client.post(f"{base}/api/v1/auth/logout")
        _check(checks, "logout", logout.status_code in {200, 204}, f"status={logout.status_code}")
    except httpx.HTTPError as exc:
        _check(checks, "http", False, str(exc))
    finally:
        if owns_client:
            client.close()
    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "base_url": base,
        "expect_version": expect_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render_markdown(result: dict) -> str:
    lines = [
        "# P1 验收检查",
        "",
        f"- 时间：{result['generated_at']}",
        f"- 地址：{result['base_url']}",
        f"- 期望版本：{result['expect_version']}",
        f"- 结果：{'PASS' if result['ok'] else 'FAIL'}（{sum(1 for c in result['checks'] if c['ok'])}/{len(result['checks'])}）",
        "",
        "| 检查 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in result["checks"]:
        lines.append(f"| {item['name']} | {'PASS' if item['ok'] else 'FAIL'} | {item['detail']} |")
    lines.append("")
    lines.append("## 仍需人工验收")
    lines.append("")
    lines.append("- 五页签登录后的页面交互（上传、复核、导出、发布、问答）；")
    lines.append("- 报告/标准/报价真实样例与 WeKnora 发布链路；")
    lines.append("- UAT 脚本与客户材料（不在本脚本范围）。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expect-version", required=True)
    parser.add_argument("--username", default=os.getenv("P0_ACCEPT_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("P0_ACCEPT_PASSWORD", ""))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--report", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    password = args.password or getpass.getpass("password: ")
    result = run_checks(
        args.base_url, args.expect_version, args.username, password, timeout=args.timeout
    )
    markdown = render_markdown(result)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(markdown, encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        for item in result["checks"]:
            print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}: {item['detail']}")
        print("OK" if result["ok"] else "FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
