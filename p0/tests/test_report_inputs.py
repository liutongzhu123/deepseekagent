from pathlib import Path

from openpyxl import Workbook

from app.report_inputs import load_inputs


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _inbox(tmp_path: Path) -> Path:
    _write(
        tmp_path / "task_orders.csv",
        "任务单号,报告编号,样品编号,客户,检验项目\n"
        "YF25HNE8,QA17LX1LDT861,TS24LXJ8A-01,示例客户,气味\n",
    )
    _write(
        tmp_path / "original_records.csv",
        "记录编号,报告编号,样品编号,检验日期,设备编号,检验结果\n"
        "REC-001,QA17LX1LDT861,TS24LXJ8A-01,2025-03-03,NJ.18.91.662,4.0/4.0/3.5\n",
    )
    _write(
        tmp_path / "samples.csv",
        "样品编号,样品名称,样品数量,报告编号,委托单位\n"
        "TS24LXJ8A-01,气味样品,1件,QA17LX1LDT861,示例客户\n",
    )
    _write(
        tmp_path / "equipment.csv",
        "设备编号,设备名称,溯源有效期,状态\n"
        "NJ.18.91.662,袋式法环境舱,2026-12-03,正常\n",
    )
    return tmp_path


def test_load_csv_inputs(tmp_path):
    snapshot = load_inputs(_inbox(tmp_path))

    assert snapshot.errors == []
    assert snapshot.counts() == {
        "task_orders": 1,
        "original_records": 1,
        "samples": 1,
        "equipment": 1,
    }
    assert snapshot.lookup("task_orders", "YF25HNE8")["report_no"] == "QA17LX1LDT861"
    assert snapshot.lookup("samples", "TS24LXJ8A-01")["quantity"] == "1件"
    assert snapshot.lookup("equipment", "NJ.18.91.662")["valid_until"] == "2026-12-03"
    assert snapshot.lookup("samples", "missing") is None


def test_missing_files_are_warnings(tmp_path):
    snapshot = load_inputs(tmp_path)

    assert snapshot.errors == []
    assert len(snapshot.warnings) == 4
    assert snapshot.counts() == {
        "task_orders": 0,
        "original_records": 0,
        "samples": 0,
        "equipment": 0,
    }


def test_missing_required_column_is_error(tmp_path):
    _write(tmp_path / "task_orders.csv", "报告编号,样品编号\nQA17LX1LDT861,TS24LXJ8A-01\n")

    snapshot = load_inputs(tmp_path)

    assert any("缺少必填列 order_no" in item for item in snapshot.errors)
    # the row is still readable but has no key, so it is skipped
    assert snapshot.counts()["task_orders"] == 0


def test_duplicate_key_and_unknown_column(tmp_path):
    _write(
        tmp_path / "samples.csv",
        "样品编号,样品名称,样品数量,未知列\n"
        "TS24LXJ8A-01,样品A,1件,x\n"
        "TS24LXJ8A-01,样品B,2件,y\n",
    )

    snapshot = load_inputs(tmp_path)

    status = snapshot.statuses["samples"]
    assert status["duplicates"] == {"TS24LXJ8A-01": 2}
    assert any("重复主键" in item for item in snapshot.warnings)
    assert any("未知列" in item for item in snapshot.warnings)
    assert snapshot.lookup("samples", "TS24LXJ8A-01")["sample_name"] == "样品A"


def test_xlsx_input(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["任务单号", "报告编号", "样品编号"])
    sheet.append(["YF25HNE8", "QA17LX1LDT861", "TS24LXJ8A-01"])
    workbook.save(tmp_path / "task_orders.xlsx")

    snapshot = load_inputs(tmp_path)

    assert snapshot.counts()["task_orders"] == 1
    assert snapshot.lookup("task_orders", "YF25HNE8")["sample_no"] == "TS24LXJ8A-01"


def test_directory_missing(tmp_path):
    snapshot = load_inputs(tmp_path / "not-there")

    assert snapshot.errors
    assert "目录不存在" in snapshot.errors[0]
