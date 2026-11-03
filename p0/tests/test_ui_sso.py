"""Static checks for the SSO entry on the login page."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


def test_login_page_has_sso_entry_and_error_mapping():
    html = read("login.html")
    for marker in ("ssoBlock", "ssoBtn", "使用企业 SSO 登录", "sso_error"):
        assert marker in html


def test_login_page_reads_provider_status():
    html = read("login.html")
    assert "api/v1/auth/providers" in html
    assert "start_url" in html
    assert "oidc" in html
