import csv
import errno
import fcntl
import multiprocessing
import os
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import auth, crm, main
from app.quote import QuoteLine, QuoteResult


def _upsert_crm_row_from_worker(start, results, path, index):
    store = crm.BaseDataStore(Path(path))
    start.wait(timeout=30)
    try:
        store.upsert("prices", {
            "project_name": f"项目{index}", "item_name": f"项目{index}检查", "unit_price": index,
        }, updated_by=f"worker-{index}")
        results.put((index, "ok"))
    except Exception as exc:  # return worker failures to the parent assertion
        results.put((index, type(exc).__name__))


def _hold_crm_lock(ready, entered, release, path, worker_id):
    if worker_id != "holder":
        descriptor = os.open(Path(path).with_name(Path(path).name + ".lock"), os.O_RDWR)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                ready.put((worker_id, "blocked"))
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                ready.put((worker_id, "not_blocked"))
        finally:
            os.close(descriptor)
    else:
        ready.put((worker_id, "holder_ready"))
    with crm._base_locked(Path(path)):
        entered.put(worker_id)
        release.wait(timeout=30)


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


def _restricted_sales_client(monkeypatch, tmp_path, projects):
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(auth, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(auth, "USERS_PATH", tmp_path / "auth" / "users.json")
    monkeypatch.setattr(auth, "SECRET_PATH", tmp_path / "auth" / "session_secret")
    monkeypatch.setattr(auth, "AUDIT_PATH", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(auth, "INITIAL_PASSWORD_PATH", tmp_path / "auth" / "initial.txt")
    monkeypatch.setattr(auth, "_SECRET", None)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    auth.create_user("scoped-sales", "sales-password", role="sales", projects=projects)
    client = TestClient(main.app)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "scoped-sales", "password": "sales-password"},
    )
    assert response.status_code == 200
    return client


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


def test_scope_snapshot_hides_global_and_conflicting_records():
    snapshot = crm.CrmSnapshot(
        mode="excel",
        source="isolated",
        projects=[crm.CrmProject("P1", "项目A"), crm.CrmProject("P2", "项目B")],
        prices=[
            crm.CrmPrice("P1", "项目A", "样品A", "检验A", "S1", "", "件", 1, 100),
            crm.CrmPrice("P2", "项目B", "样品B", "检验B", "S1", "", "件", 1, 200),
            crm.CrmPrice("P1", "项目B", "冲突样品", "检验C", "S1", "", "件", 1, 300),
            crm.CrmPrice("P9", "未知项目", "孤立样品", "检验D", "S1", "", "件", 1, 400),
        ],
        qualifications=[crm.CrmQualification("全局资质", "CERT-G")],
        schedules=[crm.CrmSchedule("P1", "项目A"), crm.CrmSchedule("P2", "项目B")],
        task_orders=[crm.CrmTaskOrder("T-1", "P1"), crm.CrmTaskOrder("T-2", "P2"), crm.CrmTaskOrder("T-3")],
        samples=[crm.CrmSample("S-1", "P1"), crm.CrmSample("S-2", "P2"), crm.CrmSample("S-3")],
        name_mappings=[crm.CrmNameMapping("全局别名", "检验A")],
    )

    scoped = crm.scope_snapshot(snapshot, ["P1"])

    assert [row.project_id for row in scoped.projects] == ["P1"]
    assert [(row.project_id, row.item_name) for row in scoped.prices] == [("P1", "检验A")]
    assert [row.project_id for row in scoped.schedules] == ["P1"]
    assert [row.order_no for row in scoped.task_orders] == ["T-1"]
    assert [row.sample_no for row in scoped.samples] == ["S-1"]
    assert scoped.qualifications == []
    assert scoped.name_mappings == []
    assert scoped.counts() == {
        "projects": 1, "prices": 1, "qualifications": 0, "schedules": 1,
        "task_orders": 1, "samples": 1, "name_mappings": 0,
    }


def test_scope_snapshot_rejects_duplicate_project_identity():
    snapshot = crm.CrmSnapshot(
        mode="excel",
        source="isolated",
        projects=[crm.CrmProject("P1", "项目A"), crm.CrmProject("P1", "项目B")],
        prices=[crm.CrmPrice("P1", "项目A", "样品A", "检验A", "S1", "", "件", 1, 100)],
    )

    scoped = crm.scope_snapshot(snapshot, ["P1"])

    assert scoped.projects == []
    assert scoped.prices == []


def test_scope_base_document_hides_unscoped_and_other_project_rows():
    document = {
        "prices": [
            {"project_id": "P1", "project_name": "项目A", "item_name": "检验A"},
            {"project_id": "P2", "project_name": "项目B", "item_name": "检验B"},
        ],
        "schedules": [{"project_id": "P1", "project_name": "项目A"}],
        "qualifications": [{"name": "全局资质"}],
        "name_mappings": [{"alias": "全局别名", "canonical": "检验A"}],
        "updated_by": "admin-secret",
        "version": 1,
    }

    scoped = crm.scope_base_document(document, ["P1"])

    assert scoped["prices"] == [document["prices"][0]]
    assert scoped["schedules"] == document["schedules"]
    assert scoped["qualifications"] == []
    assert scoped["name_mappings"] == []
    assert "updated_by" not in scoped
    assert scoped["version"] == 1


def test_project_scoped_crm_endpoints_filter_rows_counts_and_lookups(tmp_path, monkeypatch):
    path = tmp_path / "crm-scoped.xlsx"
    _workbook(path)
    workbook = openpyxl.load_workbook(path)
    workbook["项目"].append(["P2", "项目B", "客户B", "进行中", "李四", "2026-01-02"])
    workbook["价格"].append(["P2", "项目B", "样品B", "检验B", "Q/TEST 001", "材料室", "件", 1, 900, 60, "2026-12-31"])
    workbook["价格"].append(["P1", "项目B", "冲突样品", "冲突项目", "Q/TEST 001", "材料室", "件", 1, 9999, 60, "2026-12-31"])
    workbook["价格"].append(["P9", "未知项目", "孤立样品", "孤立项目", "Q/TEST 001", "材料室", "件", 1, 8888, 60, "2026-12-31"])
    workbook["排期"].append(["P2", "项目B", "2026-03-01", "2026-03-10", "已排", ""])
    workbook["任务单"].append(["T-002", "P2", "客户B", "样品B", "已委托", "2026-02-20"])
    workbook["任务单"].append(["T-003", "P9", "客户X", "孤立样品", "已委托", "2026-02-20"])
    workbook["样品"].append(["S-002", "P2", "样品B", "Y", 1, "已接收", "2026-02-21"])
    workbook["样品"].append(["S-003", "P9", "孤立样品", "Z", 1, "已接收", "2026-02-21"])
    workbook.save(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    base_path = tmp_path / "base.json"
    base_store = crm.BaseDataStore(base_path)
    base_store.upsert("prices", {"project_id": "P1", "project_name": "项目A", "item_name": "本地A", "unit_price": 100}, updated_by="admin-secret")
    base_store.upsert("prices", {"project_id": "P2", "project_name": "项目B", "item_name": "本地B", "unit_price": 900}, updated_by="admin-secret")
    base_store.upsert("schedules", {"project_id": "P1", "project_name": "项目A", "end_date": "2026-12-31"}, updated_by="admin-secret")
    base_store.upsert("qualifications", {"name": "全局资质", "certificate_no": "CERT-G"}, updated_by="admin-secret")
    base_store.upsert("name_mappings", {"alias": "全局别名", "canonical": "本地A"}, updated_by="admin-secret")
    monkeypatch.setenv("P0_CRM_BASE_PATH", str(base_path))
    client = _restricted_sales_client(monkeypatch, tmp_path, ["P1"])

    status = client.get("/api/v1/crm/status").json()
    projects = client.get("/api/v1/crm/projects").json()
    prices = client.get("/api/v1/crm/prices").json()
    qualifications = client.get("/api/v1/crm/qualifications").json()
    schedules = client.get("/api/v1/crm/schedules").json()
    base_data = client.get("/api/v1/crm/base-data").json()

    assert status["counts"] == projects["counts"] == {
        "projects": 1, "prices": 2, "qualifications": 0, "schedules": 1,
        "task_orders": 1, "samples": 1, "name_mappings": 0,
    }
    assert status["source"] == "CRM 只读适配器"
    assert str(path) not in str(status)
    assert [project["project_id"] for project in projects["projects"]] == ["P1"]
    assert prices["total"] == 2
    assert all(price["project_id"] == "P1" for price in prices["prices"])
    assert qualifications["qualifications"] == [] and qualifications["counts"]["qualifications"] == 0
    assert [row["project_id"] for row in schedules["schedules"]] == ["P1"]
    assert base_data["counts"] == {"prices": 1, "schedules": 1, "qualifications": 0, "name_mappings": 0}
    assert [row["item_name"] for row in base_data["prices"]] == ["本地A"]
    assert base_data["qualifications"] == [] and base_data["name_mappings"] == []
    assert "updated_by" not in base_data
    assert client.get("/api/v1/crm/task-orders/T-002").status_code == 404
    assert client.get("/api/v1/crm/task-orders/T-003").status_code == 404
    assert client.get("/api/v1/crm/samples/S-002").status_code == 404
    assert client.get("/api/v1/crm/samples/S-003").status_code == 404
    assert client.get("/api/v1/crm/task-orders/T-001").status_code == 200
    assert client.get("/api/v1/crm/samples/S-001").status_code == 200
    assert client.post(
        "/api/v1/crm/base-data/prices",
        json={"project_id": "P1", "project_name": "项目A", "item_name": "有权维护", "unit_price": "100"},
    ).status_code == 200
    assert client.post(
        "/api/v1/crm/base-data/prices",
        json={"project_id": "P2", "project_name": "项目B", "item_name": "越权写", "unit_price": "100"},
    ).status_code == 403
    assert client.get("/api/v1/crm/base-data/history").status_code == 403
    assert client.post("/api/v1/crm/base-data/rollback", json={"version": 1, "reason": "no"}).status_code == 403


def test_project_scoped_crm_errors_do_not_expose_source_path(monkeypatch, tmp_path):
    client = _restricted_sales_client(monkeypatch, tmp_path, ["P1"])
    secret_path = "/private/customer/crm-export.xlsx"

    class BrokenProvider:
        def snapshot(self):
            raise crm.CrmUnavailable(f"missing {secret_path}")

    monkeypatch.setattr(main.crm, "provider", lambda: BrokenProvider())
    status = client.get("/api/v1/crm/status")
    prices = client.get("/api/v1/crm/prices")

    assert status.status_code == 200 and secret_path not in status.text
    assert status.json()["counts"]["prices"] == 0
    assert prices.status_code == 503 and secret_path not in prices.text


def test_sales_user_without_project_grants_keeps_legacy_unrestricted_reads(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    client = _restricted_sales_client(monkeypatch, tmp_path, [])

    projects = client.get("/api/v1/crm/projects").json()
    qualifications = client.get("/api/v1/crm/qualifications").json()
    prices = client.get("/api/v1/crm/prices").json()

    assert projects["counts"]["projects"] == 1
    assert prices["total"] == 2
    assert qualifications["qualifications"][0]["certificate_no"] == "CNAS-001"


# CS-1.1..1.4: local four-library maintenance, name mapping and CRM sync
def _base(tmp_path):
    return crm.BaseDataStore(tmp_path / "base.json")


def test_base_library_lifecycle_and_validation(tmp_path, monkeypatch):
    store = _base(tmp_path)
    price = {"project_name": "中汽研", "sample_name": "样品A", "item_name": "外观检查", "unit_price": "100"}
    assert store.upsert("prices", price)["action"] == "created"
    with pytest.raises(crm.BaseDataConflict):
        store.upsert("prices", price | {"unit_price": 120})
    assert store.upsert("prices", price | {"unit_price": 120, "force_replace": True})["action"] == "updated"
    store.upsert("schedules", {"project_name": "中汽研", "start_date": "2026-02-01"})
    store.upsert("qualifications", {"name": "CNAS", "certificate_no": "CNAS-001"})
    store.upsert("name_mappings", {"alias": "中汽研", "canonical": "中汽研汽车检验中心"})
    snapshot = store.snapshot()
    assert crm.resolve_project(snapshot, "中汽研") == "中汽研"
    pending = next(row for row in store.read()["name_mappings"] if row["alias"] == "中汽研")
    # The admin UI binds approvals to a complete normalized snapshot; legacy
    # rows may omit optional note/source fields, which the UI sends as empty text.
    pending = pending | {"note": pending.get("note", ""), "source": pending.get("source", "")}
    store.approve_name_mapping(
        "中汽研", "reviewer", expected=pending, source="审核单-001", reason="客户映射审批"
    )
    snapshot = store.snapshot()
    assert crm.resolve_project(snapshot, "中汽研") == "中汽研汽车检验中心"
    assert crm.read_prices(snapshot, project="中汽研")[0]["unit_price"] == 120
    assert store.delete("prices", price) and not store.delete("prices", price)
    for bad in ({"project_name": "P"}, {"project_name": "P", "item_name": "I", "unit_price": "abc"}):
        with pytest.raises(ValueError):
            store.upsert("prices", bad)
    for bad in (
        price | {"quantity": 0}, price | {"unit_price": -1},
        price | {"discount": 101}, price | {"valid_until": "2026/12/31"},
    ):
        with pytest.raises(ValueError):
            store.upsert("prices", bad)
    with pytest.raises(ValueError):
        store.upsert("schedules", {"project_name": "P", "start_date": "2026-03-01", "end_date": "2026-02-01"})
    with pytest.raises(ValueError):
        store.upsert("bogus", {})
    store.path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(crm.CrmUnavailable):
        store.snapshot()
    with pytest.raises(crm.CrmUnavailable):
        store.upsert("qualifications", {"name": "CNAS"})


def test_changed_approved_mapping_returns_to_pending_and_stale_approval_is_rejected(tmp_path):
    store = _base(tmp_path)
    store.upsert("name_mappings", {"alias": "简称", "canonical": "项目全称", "note": "初始"},
                 source="合成资料", reason="首次录入")
    stale = store.read()["name_mappings"][0]
    store.approve_name_mapping("简称", "reviewer", expected=stale,
                               source="审批单-1", reason="确认项目映射")
    assert crm.resolve_project(store.snapshot(), "简称") == "项目全称"

    with pytest.raises(crm.BaseDataConflict):
        store.upsert("name_mappings", {"alias": "简称", "canonical": "项目全称", "note": "变更说明"},
                     source="合成资料-2", reason="更新映射说明")
    store.upsert("name_mappings", {"alias": "简称", "canonical": "项目全称", "note": "变更说明",
                                   "force_replace": True},
                 source="合成资料-2", reason="更新映射说明")
    current = store.read()["name_mappings"][0]
    assert current["review_status"] == "pending"
    assert current["approved_by"] == "" and current["approved_at"] == ""
    assert crm.resolve_project(store.snapshot(), "简称") == "简称"
    with pytest.raises(crm.BaseDataConflict):
        store.approve_name_mapping("简称", "reviewer-2", expected=stale,
                                   source="审批单-2", reason="旧页面审批尝试")
    assert store.read()["name_mappings"][0]["review_status"] == "pending"


def test_mapping_approval_requires_snapshot_source_and_reason(tmp_path):
    store = _base(tmp_path)
    store.upsert("name_mappings", {"alias": "简称", "canonical": "项目全称"},
                 source="导入清单", reason="初始")
    pending = store.read()["name_mappings"][0]
    for source, reason in (("", "审批原因"), ("审批来源", ""), ("x" * 257, "审批原因"), ("审批来源", "x" * 501)):
        with pytest.raises(ValueError):
            store.approve_name_mapping("简称", "reviewer", expected=pending,
                                       source=source, reason=reason)
    assert store.read()["name_mappings"][0]["review_status"] == "pending"


def test_base_price_numbers_are_strict_and_normalized(tmp_path):
    store = _base(tmp_path)
    for field, bad in (("unit_price", "100abc"), ("quantity", "2件"),
                       ("discount", "60%"), ("unit_price", "1e309"),
                       ("unit_price", "1e-999"), ("unit_price", "9007199254740993"),
                       ("unit_price", "0.1234567890123456789"),
                       ("unit_price", 9007199254740993), ("unit_price", 10 ** 10000),
                       ("quantity", "NaN"), ("discount", True)):
        with pytest.raises(ValueError):
            store.upsert("prices", {"project_name": "项目", "item_name": "试验", field: bad})
    store.upsert("prices", {"project_name": "项目", "item_name": "试验",
                             "unit_price": "1e2", "quantity": "2.5", "discount": "60"})
    row = store.read()["prices"][0]
    assert row["unit_price"] == 100.0 and row["quantity"] == 2.5 and row["discount"] == 60.0


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
    assert second["errors"] == [] and second["conflicts"] == 0
    with pytest.raises(crm.CrmUnavailable):
        crm.sync_base_data(crm.CrmSnapshot(mode="off", source=""), store)


def test_expired_prices_and_qualifications_are_not_current_matches():
    snapshot = crm.CrmSnapshot(
        mode="base", source="",
        prices=[
            crm.CrmPrice("P1", "项目A", "样品A", "有效", "S", "", "件", 1, 100, valid_until="2099-12-31"),
            crm.CrmPrice("P1", "项目A", "样品A", "过期", "S", "", "件", 1, 100, valid_until="2000-01-01"),
        ],
        qualifications=[
            crm.CrmQualification("有效资质", "A", valid_until="2099-12-31"),
            crm.CrmQualification("过期资质", "B", valid_until="2000-01-01"),
        ],
    )
    assert [row["item_name"] for row in crm.read_prices(snapshot)] == ["有效"]
    assert [row["name"] for row in crm.read_qualifications(snapshot)] == ["有效资质"]


def test_base_sync_reports_row_conflicts_without_overwriting(tmp_path, monkeypatch):
    path = tmp_path / "crm.xlsx"
    _workbook(path)
    monkeypatch.setenv("P0_CRM_MODE", "excel")
    monkeypatch.setenv("P0_CRM_PATH", str(path))
    store = _base(tmp_path)
    store.upsert("prices", {
        "project_id": "P1", "project_name": "项目A", "sample_name": "样品A",
        "item_name": "外观检查", "standard": "Q/TEST 001", "unit_price": 999,
    })
    result = crm.sync_base_data(crm.provider().snapshot(), store)
    assert result["conflicts"] == 1 and result["skipped"] == 1
    assert result["errors"][0]["kind"] == "prices" and "force_replace" in result["errors"][0]["error"]
    stored = store.read()["prices"]
    assert next(row for row in stored if row["item_name"] == "外观检查")["unit_price"] == 999


def test_base_data_endpoints_and_admin_permission(tmp_path, monkeypatch):
    store = _base(tmp_path)
    monkeypatch.setenv("P0_CRM_BASE_PATH", str(store.path))
    client = TestClient(main.app)
    price = {"project_name": "项目A", "item_name": "外观检查", "unit_price": "100"}
    assert client.post("/api/v1/crm/base-data/prices", json=price).json()["action"] == "created"
    numeric_json = client.post("/api/v1/crm/base-data/prices", json={
        "project_name": "项目A", "item_name": "数值类型不接受", "unit_price": 0.12345678901234568,
    })
    assert numeric_json.status_code == 400 and "十进制字符串" in numeric_json.json()["detail"]
    lost_precision = client.post("/api/v1/crm/base-data/prices", json={
        "project_name": "项目A", "item_name": "精度边界", "unit_price": "0.1234567890123456789",
    })
    assert lost_precision.status_code == 400 and "精度" in lost_precision.json()["detail"]
    data = client.get("/api/v1/crm/base-data").json()
    assert data["counts"]["prices"] == 1 and data["prices"][0]["item_name"] == "外观检查"
    assert "_history" not in data and "updated_by" not in data
    initial_version = data["version"]
    conflict = client.post("/api/v1/crm/base-data/prices", json=price | {"unit_price": "120"})
    assert conflict.status_code == 409
    assert client.post("/api/v1/crm/base-data/prices", json=price | {"unit_price": "120", "force_replace": True}).status_code == 200
    assert client.post("/api/v1/crm/base-data/name_mappings", json={"alias": "别名", "canonical": "项目A"}).status_code == 200
    snapshot = crm.BaseDataStore().snapshot()
    assert crm.resolve_project(snapshot, "别名") == "别名"
    expected = crm.BaseDataStore().read()["name_mappings"][0]
    expected = expected | {"note": expected.get("note", ""), "source": expected.get("source", "")}
    missing_reason = client.post("/api/v1/crm/base-data/name-mappings/approve", json={
        "alias": "别名", "expected": expected, "source": "审核清单",
    })
    assert missing_reason.status_code == 400
    approved = client.post("/api/v1/crm/base-data/name-mappings/approve", json={
        "alias": "别名", "expected": expected, "source": "审核清单", "reason": "确认该映射",
    })
    assert approved.status_code == 200 and approved.json()["item"]["approved_by"]
    assert crm.resolve_project(crm.BaseDataStore().snapshot(), "别名") == "项目A"
    history = client.get("/api/v1/crm/base-data/history").json()["history"]
    assert history[-1]["action"] == "mapping_approved" and "snapshot" not in history[-1]
    rollback = client.post("/api/v1/crm/base-data/rollback", json={"version": initial_version, "reason": "撤销误导入"})
    assert rollback.status_code == 200
    assert crm.BaseDataStore().read()["prices"][0]["unit_price"] == 100
    assert client.request("DELETE", "/api/v1/crm/base-data/prices", json=price).status_code == 200
    assert not auth.has_permission({"role": "business_manager"}, "crm:base:manage")
    assert auth.has_permission({"role": "business_manager"}, "crm:prices:manage")
    assert auth.has_permission({"role": "admin"}, "crm:base:manage")


def test_base_data_concurrent_workers_preserve_rows_and_versions(tmp_path):
    path = tmp_path / "crm" / "base.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_upsert_crm_row_from_worker, args=(start, results, str(path), index))
        for index in range(1, 9)
    ]
    started = []
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        start.set()
        outcomes = [results.get(timeout=30) for _ in workers]
    finally:
        start.set()
        for worker in started:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            assert not worker.is_alive()
        results.close()
    assert all(worker.exitcode == 0 for worker in started)

    document = crm.BaseDataStore(path).read()
    assert all(status == "ok" for _, status in outcomes)
    assert document["version"] == len(workers)
    assert {row["project_name"] for row in document["prices"]} == {
        f"项目{index}" for index in range(1, 9)
    }
    assert len(document["_history"]) == len(workers)


def test_base_data_lock_failure_rejects_mutation(monkeypatch, tmp_path):
    path = tmp_path / "crm" / "base.json"
    store = crm.BaseDataStore(path)

    def fail_lock(*args):
        raise OSError("lock unavailable")

    monkeypatch.setattr(crm.fcntl, "flock", fail_lock)
    with pytest.raises(crm.CrmUnavailable, match="基础数据锁不可用"):
        store.upsert("prices", {"project_name": "项目A", "item_name": "测试项"})
    assert not path.exists()


def test_base_data_lock_excludes_other_processes_deterministically(tmp_path):
    path = tmp_path / "crm" / "base.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    entered = context.Queue()
    release = context.Event()
    holder = context.Process(target=_hold_crm_lock, args=(ready, entered, release, str(path), "holder"))
    waiters = [
        context.Process(target=_hold_crm_lock, args=(ready, entered, release, str(path), f"waiter-{index}"))
        for index in range(2)
    ]
    holder.start()
    started = [holder]
    try:
        assert ready.get(timeout=30) == ("holder", "holder_ready")
        assert entered.get(timeout=30) == "holder"
        for worker in waiters:
            worker.start()
            started.append(worker)
        outcomes = {ready.get(timeout=30), ready.get(timeout=30)}
        assert outcomes == {("waiter-0", "blocked"), ("waiter-1", "blocked")}
        release.set()
        later_entries = {entered.get(timeout=30), entered.get(timeout=30)}
        assert later_entries == {"waiter-0", "waiter-1"}
    finally:
        release.set()
        for worker in started:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
            assert not worker.is_alive()
        ready.close()
        entered.close()
    assert all(worker.exitcode == 0 for worker in started)
