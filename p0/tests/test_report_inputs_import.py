from pathlib import Path

from app.report_inputs import import_inputs


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


def test_import_is_idempotent(tmp_path):
    inbox = _inbox(tmp_path)
    state = tmp_path / "imported.json"

    first = import_inputs(inbox, state)
    second = import_inputs(inbox, state)

    assert first["counts"] == {"success": 4, "skipped": 0, "failed": 0}
    assert second["counts"] == {"success": 0, "skipped": 4, "failed": 0}
    assert all(item["reason"] == "已导入" for item in second["skipped"])


def test_bad_row_does_not_break_batch(tmp_path):
    inbox = _inbox(tmp_path)
    _write(
        inbox / "samples.csv",
        "样品编号,样品名称,样品数量,报告编号,委托单位\n"
        "TS24LXJ8A-01,气味样品,1件,QA17LX1LDT861,示例客户\n"
        ",坏样品,1件,QA17LX1LDT861,示例客户\n",
    )

    result = import_inputs(inbox, tmp_path / "imported.json")

    assert result["counts"]["success"] == 4
    assert result["counts"]["failed"] == 1
    assert result["failed"][0]["row"] == 3
    assert "缺少主键" in result["failed"][0]["reason"]


def test_duplicate_in_batch_is_skipped(tmp_path):
    inbox = _inbox(tmp_path)
    _write(
        inbox / "equipment.csv",
        "设备编号,设备名称,溯源有效期,状态\n"
        "NJ.18.91.662,袋式法环境舱,2026-12-03,正常\n"
        "NJ.18.91.662,袋式法环境舱,2026-12-03,正常\n",
    )

    result = import_inputs(inbox, tmp_path / "imported.json")

    assert result["counts"]["success"] == 4
    assert result["counts"]["skipped"] == 1
    assert result["skipped"][0]["reason"] == "批次内重复"


def test_state_stores_keys_not_row_values(tmp_path):
    inbox = _inbox(tmp_path)
    state = tmp_path / "imported.json"
    import_inputs(inbox, state)

    text = state.read_text(encoding="utf-8")
    assert "TS24LXJ8A-01" in text
    assert "示例客户" not in text
