"""Quote history: persistence, list API, project isolation and UI wiring."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import auth, main, quote_store


def _workbook_bytes() -> bytes:
    workbook = Workbook()
    ws = workbook.active
    ws.append(["序号", "样品名称", "检验标准", "检验项目", "单价（元）", "数量", "单位"])
    ws.append([1, "F610S座椅安全带提醒器总成", "测试大纲", "气味", 1200, 1, "材料"])
    ws.append([2, "F610S座椅安全带提醒器总成", "测试大纲", "雾化", 1000, 2, "材料"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _seed(
    base_dir,
    quote_id: str,
    project: str,
    source: str = "demo.xlsx",
    created_at: str = "2026-11-03T10:00:00+00:00",
) -> dict:
    meta = {
        "quote_id": quote_id,
        "project": project,
        "created_by": "alice",
        "created_at": created_at,
        "source_name": source,
        "parse_method": "Excel 表格",
        "lines": 2,
        "priced_lines": 2,
        "summary": {"lines": 2, "priced_lines": 2, "all_priced": True},
        "all_priced": True,
        "has_sales_quote": True,
        "verification_ok": True,
        "crm": {"enabled": False, "mode": "off", "reconciled": True, "matched": 0},
    }
    quote_store.create(meta, {"CRM报价草稿.xlsx": b"PK"}, base_dir=base_dir)
    return meta


@pytest.fixture()
def quote_env(monkeypatch, tmp_path):
    quote_dir = tmp_path / "quotes"
    monkeypatch.setattr(quote_store, "QUOTE_DIR", quote_dir)
    monkeypatch.setattr(main, "QUOTE_DIR", quote_dir)
    return SimpleNamespace(client=TestClient(main.app), dir=quote_dir, tmp=tmp_path)


@pytest.fixture()
def auth_env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    quote_dir = tmp_path / "quotes"
    monkeypatch.setattr(quote_store, "QUOTE_DIR", quote_dir)
    monkeypatch.setattr(main, "QUOTE_DIR", quote_dir)
    return SimpleNamespace(client=TestClient(main.app), dir=quote_dir, tmp=tmp_path)


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def test_list_quotes_sorted_and_searchable(quote_env):
    _seed(quote_env.dir, "q_20261103100000_aaaaaa", "P1", "DVP-甲.xlsx")
    _seed(
        quote_env.dir,
        "q_20261103110000_bbbbbb",
        "P2",
        "DVP-乙.xlsx",
        created_at="2026-11-03T11:00:00+00:00",
    )
    payload = quote_env.client.get("/api/v1/quotes").json()
    assert payload["total"] == 2
    assert [row["quote_id"] for row in payload["quotes"]] == [
        "q_20261103110000_bbbbbb",
        "q_20261103100000_aaaaaa",
    ]
    filtered = quote_env.client.get("/api/v1/quotes?q=甲").json()
    assert filtered["total"] == 1
    assert filtered["quotes"][0]["source_name"] == "DVP-甲.xlsx"


def test_generate_quote_persists_history_metadata(quote_env):
    response = quote_env.client.post(
        "/api/v1/quotes/generate",
        files={
            "file": (
                "demo.xlsx",
                _workbook_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200
    generated = response.json()
    payload = quote_env.client.get("/api/v1/quotes").json()
    assert payload["total"] == 1
    row = payload["quotes"][0]
    assert row["quote_id"] == generated["quote_id"]
    assert row["source_name"] == "demo.xlsx"
    assert row["lines"] == 2
    assert row["has_sales_quote"] is False
    assert row["crm"]["mode"] == "off"
    assert row["summary"]["priced_lines"] == 2


def test_quote_history_is_project_filtered(auth_env):
    auth.create_user("sales-p1", "sales-pass-1", role="sales", projects=["P1"])
    auth.create_user("sales-p2", "sales-pass-2", role="sales", projects=["P2"])
    _seed(auth_env.dir, "q_20261103100000_aaaaaa", "P1")
    _seed(auth_env.dir, "q_20261103110000_bbbbbb", "P2")

    p1 = TestClient(main.app)
    p2 = TestClient(main.app)
    _login(p1, "sales-p1", "sales-pass-1")
    _login(p2, "sales-p2", "sales-pass-2")

    assert [row["quote_id"] for row in p1.get("/api/v1/quotes").json()["quotes"]] == [
        "q_20261103100000_aaaaaa"
    ]
    assert [row["quote_id"] for row in p2.get("/api/v1/quotes").json()["quotes"]] == [
        "q_20261103110000_bbbbbb"
    ]
    # Cross-project download stays 403 through the existing guard.
    assert p2.get("/api/v1/quotes/q_20261103100000_aaaaaa/crm.xlsx").status_code == 403
