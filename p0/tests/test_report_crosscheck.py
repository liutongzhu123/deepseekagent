from pathlib import Path

from fastapi.testclient import TestClient

from app import main, report_store
from app.docx_parser import ReportData
from app.report_inputs import load_inputs
from app.rules import audit_report


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _inbox(tmp_path: Path) -> Path:
    _write(
        tmp_path / "task_orders.csv",
        "任务单号,报告编号,样品编号,客户,样品名称,检验项目\n"
        "YF25HNE8,QA17LX1LDT861,TS24LXJ8A-01,示例客户,气味样品,气味\n",
    )
    _write(
        tmp_path / "samples.csv",
        "样品编号,样品名称,样品数量,报告编号,委托单位\n"
        "TS24LXJ8A-01,气味样品,1件,QA17LX1LDT861,示例客户\n",
    )
    _write(
        tmp_path / "original_records.csv",
        "记录编号,报告编号,样品编号,检验日期,设备编号\n"
        "REC-001,QA17LX1LDT861,TS24LXJ8A-01,2025-03-03,NJ.18.91.662\n",
    )
    _write(
        tmp_path / "equipment.csv",
        "设备编号,设备名称,溯源有效期,状态\n"
        "NJ.18.91.662,袋式法环境舱,2026-12-03,正常\n",
    )
    return tmp_path


def _report(**overrides) -> ReportData:
    base = dict(
        file_name="sample.docx",
        cover_number="QA17LX1LDT861",
        sample_no="TS24LXJ8A-01",
        client="示例客户",
        sample_name="气味样品",
        sample_qty="1件",
        item="气味",
        test_start="2025-03-02",
        test_end="2025-03-03",
        scores=[4.0, 4.0, 3.5],
        limit=3.3,
        judgment="不符合",
        template_family="BYD-odor",
    )
    base.update(overrides)
    return ReportData(**base)


def _statuses(result: dict) -> dict[str, str]:
    return {item["rule_id"]: item["status"] for item in result["findings"]}


def test_crosscheck_passes_with_matching_inputs(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))

    result = audit_report(_report(), inputs=snapshot)
    statuses = _statuses(result)

    assert statuses["X01"] == "pass"
    assert statuses["X02"] == "pass"
    assert statuses["X03"] == "pass"
    assert statuses["X04"] == "skip"
    assert result["summary"]["total"] == 15


def test_crosscheck_flags_task_order_mismatch(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))

    result = audit_report(_report(client="错误客户"), inputs=snapshot)
    finding = next(item for item in result["findings"] if item["rule_id"] == "X01")

    assert finding["status"] == "fail"
    assert "委托单位" in finding["evidence"]


def test_crosscheck_skips_when_no_match(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))

    result = audit_report(_report(cover_number="NO-MATCH", sample_no="NO-MATCH"), inputs=snapshot)
    statuses = _statuses(result)

    assert statuses["X01"] == "skip"
    assert statuses["X02"] == "skip"
    assert statuses["X03"] == "skip"


def test_crosscheck_flags_record_date_outside_range(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))

    result = audit_report(_report(test_start="2025-03-04", test_end="2025-03-05"), inputs=snapshot)
    finding = next(item for item in result["findings"] if item["rule_id"] == "X03")

    assert finding["status"] == "fail"
    assert "检验日期" in finding["evidence"]


def test_no_inputs_keeps_eleven_rules():
    result = audit_report(_report())

    assert result["summary"]["total"] == 11
    assert all(item["rule_id"].startswith("R") for item in result["findings"])


def test_inputs_status_endpoint(tmp_path, monkeypatch):
    _inbox(tmp_path)
    monkeypatch.setenv("P0_REPORT_INPUTS_DIR", str(tmp_path))
    client = TestClient(main.app)

    response = client.get("/api/v1/reports/inputs/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["counts"]["task_orders"] == 1
    assert payload["errors"] == []


def test_audit_endpoint_includes_crosschecks(tmp_path, monkeypatch):
    _inbox(tmp_path)
    monkeypatch.setenv("P0_REPORT_INPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(report_store, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(main, "parse_docx", lambda path: _report())
    client = TestClient(main.app)

    response = client.post(
        "/api/v1/reports/audit",
        files={"file": ("sample.docx", b"x", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )

    assert response.status_code == 200
    payload = response.json()
    statuses = {item["rule_id"]: item["status"] for item in payload["findings"]}
    assert statuses["X01"] == "pass"
    assert payload["summary"]["total"] == 15


def test_crosscheck_equipment_passes(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))
    devices = [{"device_id": "NJ.18.91.662", "device_name": "袋式法环境舱", "valid_until": "2026-12-03"}]

    result = audit_report(_report(devices=devices), inputs=snapshot)
    finding = next(item for item in result["findings"] if item["rule_id"] == "X04")

    assert finding["status"] == "pass"
    assert "1 台" in finding["message"]


def test_crosscheck_equipment_expired_or_disabled(tmp_path):
    _write(
        tmp_path / "equipment.csv",
        "设备编号,设备名称,溯源有效期,状态\n"
        "NJ.18.91.662,袋式法环境舱,2024-01-01,停用\n",
    )
    snapshot = load_inputs(tmp_path)
    devices = [{"device_id": "NJ.18.91.662", "device_name": "袋式法环境舱", "valid_until": "2026-12-03"}]

    result = audit_report(_report(devices=devices), inputs=snapshot)
    finding = next(item for item in result["findings"] if item["rule_id"] == "X04")

    assert finding["status"] == "fail"
    assert "台账有效期" in finding["evidence"]
    assert "停用" in finding["evidence"]


def test_crosscheck_equipment_skips_without_ledger(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))
    devices = [{"device_id": "UNKNOWN", "device_name": "未知设备", "valid_until": "2026-12-03"}]

    result = audit_report(_report(devices=devices), inputs=snapshot)
    finding = next(item for item in result["findings"] if item["rule_id"] == "X04")

    assert finding["status"] == "skip"
