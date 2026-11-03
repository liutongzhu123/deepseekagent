"""Report workbench: history persistence, batch audit, export and project isolation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, main, report_store
from app.docx_parser import ReportData


def _report(file_name: str = "sample.docx") -> ReportData:
    return ReportData(
        file_name=file_name,
        cover_number="QA17LX1LDT861",
        header_numbers=["QA17LX1LDT861", "", "", "", ""],
        sample_name="XXXXX",
        model="XXXXX",
        supplier="XXXXX",
        receive_date="2024年02月24日",
        production_date="2024年11月25日",
        sample_date="2025年03月16日",
        sample_qty="XX",
        sample_no="TS24LXJ8A-01",
        basis="Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2",
        item="气味",
        conclusion="经检验，该样品气味的检验结果不符合委托单位提供限值：气味等级≤3.3的要求。 签发日期:",
        scores=[4.0, 4.0, 3.5],
        reported_mean=None,
        limit=3.3,
        judgment="不符合",
        device_valid="2026-11-24",
        page_totals=[5, 5, 5, 5, 5],
        page_numbers=[1, 2, 3, 4, 5],
        notices=8,
        highlights=33,
        red_runs=8,
        external_images=27,
        embedded_images=0,
        signature_text="批准： 审核： 主检：",
        test_start="2025年03月02日",
        test_end="2025年03月03日",
        template_family="BYD-odor",
    )


@pytest.fixture()
def report_env(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    monkeypatch.setattr(report_store, "REPORT_DIR", reports)
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    return SimpleNamespace(client=TestClient(main.app), reports=reports, tmp=tmp_path)


@pytest.fixture()
def auth_env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "AUDIT_RETENTION_DAYS", 180)
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1_000)
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(auth, "_LAST_PRUNE_DATE", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(report_store, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    return SimpleNamespace(client=TestClient(main.app), tmp=tmp_path)


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def test_audit_persists_history_with_source_and_export(report_env):
    client = report_env.client
    response = client.post("/api/v1/reports/audit", files={"file": ("气味报告.docx", b"fake-docx")})
    assert response.status_code == 200
    data = response.json()
    assert data["report_id"].startswith("rp_")
    assert data["summary"]["fail"] >= 1
    assert isinstance(data["rules_revision"], int)
    assert (report_env.reports / data["report_id"] / "source.docx").read_bytes() == b"fake-docx"

    history = client.get("/api/v1/reports")
    assert history.status_code == 200
    payload = history.json()
    assert payload["total"] == 1
    row = payload["reports"][0]
    assert row["file_name"] == "气味报告.docx"
    assert row["summary"]["fail"] >= 1
    assert row["batch_id"] == ""

    detail = client.get(f"/api/v1/reports/{data['report_id']}")
    assert detail.status_code == 200
    assert detail.json()["result"]["findings"]

    export = client.get(f"/api/v1/reports/{data['report_id']}/export.xlsx")
    assert export.status_code == 200
    assert export.content[:2] == b"PK"

    source = client.get(f"/api/v1/reports/{data['report_id']}/source.docx")
    assert source.status_code == 200
    assert source.content == b"fake-docx"


def test_batch_audit_keeps_good_files_and_reports_bad_ones(report_env):
    response = report_env.client.post(
        "/api/v1/reports/batch",
        files=[
            ("files", ("a.docx", b"fake-docx")),
            ("files", ("b.docx", b"fake-docx")),
            ("files", ("notes.txt", b"not-a-report")),
        ],
    )
    assert response.status_code == 200
    data = response.json()
    assert data["batch_id"].startswith("rb_")
    assert data["summary"]["stored"] == 2
    assert data["summary"]["failed"] == 1
    assert [item["ok"] for item in data["items"]] == [True, True, False]
    assert data["items"][2]["error"]
    assert all(item["report_id"].startswith("rp_") for item in data["items"] if item["ok"])

    export = report_env.client.get(f"/api/v1/reports/batch/{data['batch_id']}/export.xlsx")
    assert export.status_code == 200
    assert export.content[:2] == b"PK"

    history = report_env.client.get(f"/api/v1/reports?batch_id={data['batch_id']}").json()
    assert history["total"] == 2
    assert all(row["batch_id"] == data["batch_id"] for row in history["reports"])


def test_project_isolation_filters_history_and_detail(auth_env):
    auth.create_user("reviewer-p1", "review-pass-1", role="reviewer", projects=["P1"])
    auth.create_user("reviewer-p2", "review-pass-2", role="reviewer", projects=["P2"])
    auth.create_user("root", "root-pass-1", role="admin")

    rev1 = TestClient(main.app)
    rev2 = TestClient(main.app)
    admin = TestClient(main.app)
    _login(rev1, "reviewer-p1", "review-pass-1")
    _login(rev2, "reviewer-p2", "review-pass-2")
    _login(admin, "root", "root-pass-1")

    p1 = rev1.post(
        "/api/v1/reports/audit",
        files={"file": ("p1.docx", b"fake-docx")},
        data={"project": "P1"},
    )
    assert p1.status_code == 200
    assert p1.json()["project"] == "P1"

    # A project-scoped reviewer may not create a record in someone else's project.
    assert rev1.post(
        "/api/v1/reports/audit",
        files={"file": ("p2.docx", b"fake-docx")},
        data={"project": "P2"},
    ).status_code == 400

    p2 = admin.post(
        "/api/v1/reports/audit",
        files={"file": ("p2.docx", b"fake-docx")},
        data={"project": "P2"},
    )
    assert p2.status_code == 200

    rev1_rows = rev1.get("/api/v1/reports").json()
    assert rev1_rows["total"] == 1
    assert rev1_rows["reports"][0]["project"] == "P1"

    rev2_rows = rev2.get("/api/v1/reports").json()
    assert rev2_rows["total"] == 1
    assert rev2_rows["reports"][0]["project"] == "P2"

    assert rev2.get(f"/api/v1/reports/{p1.json()['report_id']}").status_code == 403
    assert rev2.get(f"/api/v1/reports/{p1.json()['report_id']}/export.xlsx").status_code == 403
    assert rev2.get(f"/api/v1/reports/{p1.json()['report_id']}/source.docx").status_code == 403
    assert rev1.get(f"/api/v1/reports/{p2.json()['report_id']}").status_code == 403
