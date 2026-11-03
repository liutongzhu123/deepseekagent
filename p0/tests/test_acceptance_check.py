"""Post-deploy acceptance sweep helpers."""
from __future__ import annotations

import httpx

from scripts import acceptance_check


def _mock(version="9.9.9", role="admin", login_status=200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "version": version, "auth": "enabled"})
        if path.endswith("/api/v1/auth/login"):
            if login_status != 200:
                return httpx.Response(login_status, json={"detail": "bad credentials"})
            return httpx.Response(
                200,
                json={"user": {"username": "tester", "role": role}},
                headers={"set-cookie": "p0_session=fake; Path=/"},
            )
        if path.endswith("/api/v1/auth/me"):
            return httpx.Response(200, json={"user": {"username": "tester", "role": role}})
        if path.endswith("/api/v1/standards"):
            return httpx.Response(200, json={"standards": [{"id": "std_1"}]})
        if path.endswith("/api/v1/reports"):
            return httpx.Response(200, json={"reports": []})
        if path.endswith("/api/v1/quotes"):
            return httpx.Response(200, json={"quotes": []})
        if path.endswith("/api/v1/auth/logout"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/api/v1/audit/events"):
            return httpx.Response(200, json={"events": []})
        if path.endswith("/api/v1/kb/feedback"):
            return httpx.Response(200, json={"totals": {}})
        if path.endswith("/api/v1/system/status"):
            return httpx.Response(200, json={"version": version})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_acceptance_passes_for_admin():
    result = acceptance_check.run_checks(
        "http://p0.local/p0", "9.9.9", "tester", "secret", client=_mock()
    )
    assert result["ok"] is True
    names = {item["name"] for item in result["checks"]}
    assert {"health", "version", "auth", "login", "identity", "standards", "audit", "feedback", "logout"} <= names


def test_acceptance_skips_admin_endpoints_for_reviewer():
    result = acceptance_check.run_checks(
        "http://p0.local/p0", "9.9.9", "tester", "secret", client=_mock(role="reviewer")
    )
    assert result["ok"] is True
    skipped = next(item for item in result["checks"] if item["name"] == "admin-endpoints")
    assert "skipped" in skipped["detail"]


def test_acceptance_fails_on_version_mismatch():
    result = acceptance_check.run_checks(
        "http://p0.local/p0", "9.9.9", "tester", "secret", client=_mock(version="9.9.8")
    )
    assert result["ok"] is False
    assert any(item["name"] == "version" and not item["ok"] for item in result["checks"])


def test_acceptance_fails_when_login_is_rejected():
    result = acceptance_check.run_checks(
        "http://p0.local/p0", "9.9.9", "tester", "bad", client=_mock(login_status=401)
    )
    assert result["ok"] is False
    assert any(item["name"] == "login" and not item["ok"] for item in result["checks"])


def test_acceptance_markdown_contains_manual_checks():
    result = acceptance_check.run_checks(
        "http://p0.local/p0", "9.9.9", "tester", "secret", client=_mock()
    )
    markdown = acceptance_check.render_markdown(result)
    assert "# P1 验收检查" in markdown
    assert "| health | PASS |" in markdown
    assert "仍需人工验收" in markdown
