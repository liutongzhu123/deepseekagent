import csv

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import auth, crm, main
from app.quote import QuoteLine, QuoteResult


def _workbook(path, *, fallback_only=False):
    wb = openpyxl.Workbook()
    first = wb.active
    if fallback_only:
        first.title = "Sheet1"
        first.append(["样品名称", "项目名称", "检验项目", "标准", "单位", "数量", "含税单价"])
        first.append(["样品A", "项目A", "外观检查", "Q/TEST 001", "件", 2, 100])
        wb.save(path)
        return
    first.title = "项目"
    first.append(["项目编号", "项目名称", "客户", "状态", "负责人", "更新时间"])
    first.append(["P1", "项目A", "客户A", "进行中", "张三", "2026-01-01"])
    price = wb.create_sheet("价格")
    price.append(["项目编号", "项目名称", "样品名称", "检验项目", "标准", "科室", "单位", "数量", "含税单价", "折扣", "有效期至"])
    price.append(["P1", "项目A", "样品A", "外观检查", "Q/TEST 001", "材料室", "件", 2, 100, 60, "2026-12-31"])
    price.append(["P1", "项目A", "样品A", "密封性能", "Q/TEST 001", "材料室", "件", 1, 200, 60, "2026-12-31"])
    qual = wb.create_sheet("资质")
    qual.append(["资质名称", "证书编号", "适用范围", "有效期至", "状态"])
    qual.append(["CNAS", "CNAS-001", "材料", "2027-01-01", "有效"])
    schedule = wb.create_sheet("排期")
    schedule.append(["项目编号", "项目名称", "计划开始", "计划结束", "状态", "备注"])
    schedule.append(["P1", "项目A", "2026-02-01", "2026-02-10", "已排", ""])
    task = wb.create_sheet("任务单")
    task.append(["任务单号", "项目编号", "客户", "样品名称", "状态", "委托日期"])
    task.append(["T-001", "P1", "客户A", "样品A", "已委托", "2026-01-20"])
    sample = wb.create_sheet("样品")
    sample.append(["样品编号", "项目编号", "样品名称", "型号", "数量", "状态", "接收日期"])
    sample.append(["S-001", "P1", "样品A", "X", 2, "已接收", "2026-01-21"])
    wb.save(path)


def test_excel_snapshot_and_lookups(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    snapshot = crm.provider().snapshot()
    assert snapshot.counts() == {
        "projects": 1,
        "prices": 2,
        "qualifications": 1,
        "schedules": 1,
        "task_orders": 1,
        "samples": 1,
        "name_mappings": 0,
    }
    assert crm.lookup_task_order(snapshot, "t-001")["customer"] == "客户A"
    assert crm.lookup_sample(snapshot, "S-001")["model"] == "X"
    assert crm.read_prices(snapshot, project="P1", query="密封")[0]["unit_price"] == 200


def test_fallback_first_sheet_price_list(tmp_path, monkeypatch):
    path = tmp_path / "template.xlsx"
    _workbook(path, fallback_only=True)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    snapshot = crm.provider().snapshot()
    assert len(snapshot.prices) == 1
    assert snapshot.prices[0].item_name == "外观检查"
    assert any("价格" in warning for warning in snapshot.warnings)


def test_csv_provider(tmp_path, monkeypatch):
    path = tmp_path / "crm.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["样品名称", "项目名称", "检验项目", "标准", "单位", "数量", "含税单价"])
        writer.writerow(["样品A", "项目A", "外观检查", "Q/TEST 001", "件", 2, 100])
    monkeypatch.setenv("P0_CRM_MODE", "csv")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    snapshot = crm.provider().snapshot()
    assert snapshot.counts()["prices"] == 1


def test_api_seam_reports_not_wired(monkeypatch):
    monkeypatch.setenv("P0_CRM_MODE", "api")
    monkeypatch.setenv("P0_CRM_API_BASE_URL", "https://crm.example.test")
    status = crm.status()
    assert status["configured"] is False
    assert "尚未接入" in status["error"]


def test_reconcile_quote_reports_missing_mismatch_and_price_available(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    result = QuoteResult(
        source_file="quote.xlsx",
        sample_name="样品A",
        lines=[
            QuoteLine("样品A", "外观检查", "Q/TEST 001", "", "件", 2, 100, source_row=2),
            QuoteLine("样品A", "密封性能", "Q/TEST 001", "", "件", 1, 250, source_row=3),
            QuoteLine("样品A", "缺失项", "Q/TEST 999", "", "件", 1, 10, source_row=4),
            QuoteLine("样品A", "外观检查", "Q/TEST 001", "", "件", 1, 0, source_row=5, needs_price=True),
        ],
    )
    report = crm.reconcile_quote(result)
    assert report["enabled"] is True
    assert report["reconciled"] is False
    assert report["matched"] == 3
    assert [item["item_name"] for item in report["missing"]] == ["缺失项"]
    assert report["price_mismatch"][0]["quote_unit_price"] == 250
    assert report["price_available"][0]["crm_unit_price"] == 100


def test_reconcile_off_is_backward_compatible(monkeypatch):
    monkeypatch.setenv("P0_CRM_MODE", "off")
    result = QuoteResult(source_file="q", sample_name="s", lines=[])
    report = crm.reconcile_quote(result)
    assert report["enabled"] is False
    assert report["reconciled"] is True


def test_crm_endpoints(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    monkeypatch.setenv("P0_CRM_BASE_PATH", str(tmp_path / "base.json"))
    client = TestClient(main.app)
    assert client.get("/api/v1/crm/status").json()["configured"] is True
    projects = client.get("/api/v1/crm/projects").json()
    assert projects["counts"]["projects"] == 1
    prices = client.get("/api/v1/crm/prices", params={"q": "外观"}).json()
    assert prices["total"] == 1
    assert client.get("/api/v1/crm/task-orders/T-001").json()["order_no"] == "T-001"
    assert client.get("/api/v1/crm/samples/nope").status_code == 404
    assert client.post("/api/v1/crm/base-data/sync").json()["created"] == 4


# CS-1.1..1.4: local four-library maintenance, name mapping and CRM sync
def _base(tmp_path):
    return crm.BaseDataStore(tmp_path / "base.json")


def test_base_library_lifecycle_and_validation(tmp_path, monkeypatch):
    store = _base(tmp_path)
    price = {"project_name": "中汽研", "sample_name": "样品A", "item_name": "外观检查", "unit_price": "100"}
    assert store.upsert("prices", price)["action"] == "created"
    assert store.upsert("prices", price | {"unit_price": 120})["action"] == "updated"
    store.upsert("schedules", {"project_name": "中汽研", "start_date": "2026-02-01"})
    store.upsert("qualifications", {"name": "CNAS", "certificate_no": "CNAS-001"})
    store.upsert("name_mappings", {"alias": "中汽研", "canonical": "中汽研汽车检验中心"})
    snapshot = store.snapshot()
    assert crm.resolve_project(snapshot, "中汽研") == "中汽研汽车检验中心"
    assert crm.read_prices(snapshot, project="中汽研")[0]["unit_price"] == 120
    assert store.delete("prices", price) and not store.delete("prices", price)
    for bad in ({"project_name": "P"}, {"project_name": "P", "item_name": "I", "unit_price": "abc"}):
        with pytest.raises(ValueError):
            store.upsert("prices", bad)
    with pytest.raises(ValueError):
        store.upsert("bogus", {})
    store.path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(crm.CrmUnavailable):
        store.snapshot()
    with pytest.raises(crm.CrmUnavailable):
        store.upsert("qualifications", {"name": "CNAS"})


def test_base_sync_is_idempotent_and_rejects_off(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    store = _base(tmp_path)
    store.upsert("name_mappings", {"alias": "项目A", "canonical": "项目A（全称）"})
    first = crm.sync_base_data(crm.provider().snapshot(), store)
    assert (first["created"], first["updated"]) == (4, 0)
    second = crm.sync_base_data(crm.provider().snapshot(), store)
    assert (second["created"], second["updated"]) == (0, 4)
    with pytest.raises(crm.CrmUnavailable):
        crm.sync_base_data(crm.CrmSnapshot(mode="off", source=""), store)


def test_base_data_endpoints_and_admin_permission(tmp_path, monkeypatch):
    store = _base(tmp_path)
    monkeypatch.setenv("P0_CRM_BASE_PATH", str(store.path))
    client = TestClient(main.app)
    price = {"project_name": "项目A", "item_name": "外观检查", "unit_price": 100}
    assert client.post("/api/v1/crm/base-data/prices", json=price).json()["action"] == "created"
    data = client.get("/api/v1/crm/base-data").json()
    assert data["counts"]["prices"] == 1 and data["prices"][0]["item_name"] == "外观检查"
    assert client.request("DELETE", "/api/v1/crm/base-data/prices", json=price).status_code == 200
    assert auth.PERMISSIONS["crm:write"] == ("admin",)
