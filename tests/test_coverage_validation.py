"""Catalog validation guards F70–F76 before coverage numbers are reported."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest
import yaml

from scripts import coverage


def _catalog():
    requirements, backlog = coverage.load()
    return deepcopy(requirements), deepcopy(backlog)


def test_current_catalog_is_valid():
    assert coverage.validate_catalog(*_catalog()) == []


@pytest.mark.parametrize("catalog", [
    "tasks:\n  - id: SIM\n    note: first\n    note: second\n",
    "tasks:\n  - id: SIM\n    nested:\n      value: first\n      value: second\n",
])
def test_f76_duplicate_yaml_mapping_keys_are_rejected(catalog):
    with pytest.raises(yaml.constructor.ConstructorError, match="found duplicate key"):
        coverage.load_yaml(catalog)


def test_f70_unknown_coverage_reference_is_rejected():
    requirements, backlog = _catalog()
    backlog["tasks"][0]["covers"].append("RA-NOT-REAL")
    assert "R_BASE: 未知需求 ID RA-NOT-REAL" in coverage.validate_catalog(requirements, backlog)


def test_f71_unknown_status_is_rejected():
    requirements, backlog = _catalog()
    backlog["tasks"][0]["status"] = "finished"
    assert "R_BASE: 未知状态 'finished'" in coverage.validate_catalog(requirements, backlog)


def test_f71_unknown_coverage_status_is_rejected():
    requirements, backlog = _catalog()
    backlog["tasks"][0]["coverage_status"] = "finished"
    assert "R_BASE: 未知覆盖状态 'finished'" in coverage.validate_catalog(requirements, backlog)


def test_done_simulation_can_only_mark_requirement_partial():
    requirements = {"areas": [{"name": "A", "modules": [{"items": [{"id": "REQ-1", "text": "req"}]}]}]}
    backlog = {"tasks": [{"id": "SIM", "status": "done", "coverage_status": "partial", "covers": ["REQ-1"]}]}
    assert coverage.validate_catalog(requirements, backlog) == []
    assert coverage.coverage_map(backlog) == {"REQ-1": "partial"}


def test_customer_service_acceptance_matrix_lists_all_29_requirements():
    requirements, _ = _catalog()
    customer_ids = {
        item["id"]
        for area in requirements["areas"] if area.get("id") == "CS"
        for module in area["modules"]
        for item in module["items"]
    }
    matrix = Path("docs/customer-service-acceptance-matrix.md").read_text(encoding="utf-8")
    rows = [line for line in matrix.splitlines() if line.startswith("| CS-")]
    assert len(customer_ids) == 29
    assert len(rows) == 29
    assert {line.split("|", 2)[1].strip().split()[0] for line in rows} == customer_ids


def test_customer_waiting_tasks_record_owner_inputs_and_resume_conditions():
    _, backlog = _catalog()
    tasks = {task["id"]: task for task in backlog["tasks"]}
    external_tasks = {
        task_id for task_id, task in tasks.items()
        if task_id.startswith("CS_") and task["status"] == "waiting"
    } | {"CS_BASE", "BLOCK_PROD"}
    for task_id in external_tasks:
        task = tasks[task_id]
        assert task.get("waiting_on"), task_id
        assert task.get("external_owner"), task_id
        assert task.get("resume_when"), task_id


@pytest.mark.parametrize("catalog", ["requirements", "backlog"])
def test_f72_duplicate_ids_are_rejected(catalog):
    requirements, backlog = _catalog()
    if catalog == "requirements":
        items = requirements["areas"][0]["modules"][0]["items"]
        items.append(deepcopy(items[0]))
        expected = "重复需求 ID: RA-1.1"
    else:
        backlog["tasks"].append(deepcopy(backlog["tasks"][0]))
        expected = "重复任务 ID: R_BASE"
    assert expected in coverage.validate_catalog(requirements, backlog)


@pytest.mark.parametrize("dependency, expected", [
    ("NOT-A-TASK", "R_BASE: 未知依赖任务 NOT-A-TASK"),
    ("R_BASE", "R_BASE: 不能依赖自身"),
])
def test_f73_invalid_dependency_is_rejected(dependency, expected):
    requirements, backlog = _catalog()
    backlog["tasks"][0]["deps"] = [dependency]
    assert expected in coverage.validate_catalog(requirements, backlog)


@pytest.mark.parametrize("chain", [
    ("R_BASE", "A1"),
    ("R_BASE", "A1", "A2"),
])
def test_f74_dependency_cycle_reports_full_path(chain):
    requirements, backlog = _catalog()
    tasks = {task["id"]: task for task in backlog["tasks"]}
    for task_id, dep_id in zip(chain, chain[1:] + chain[:1]):
        tasks[task_id]["deps"] = [dep_id]
    expected = f"任务依赖成环: {' -> '.join(chain + chain[:1])}"
    assert expected in coverage.validate_catalog(requirements, backlog)


def test_f74_shared_dependency_without_cycle_is_valid():
    requirements, backlog = _catalog()
    tasks = {task["id"]: task for task in backlog["tasks"]}
    tasks["A1"]["deps"] = ["R_BASE"]
    tasks["A2"]["deps"] = ["R_BASE"]
    assert coverage.validate_catalog(requirements, backlog) == []


def test_f74_cycle_stops_cli_before_coverage(monkeypatch, capsys):
    requirements, backlog = _catalog()
    tasks = {task["id"]: task for task in backlog["tasks"]}
    tasks["R_BASE"]["deps"] = ["A1"]
    tasks["A1"]["deps"] = ["R_BASE"]
    monkeypatch.setattr(coverage, "load", lambda: (requirements, backlog))
    monkeypatch.setattr(sys, "argv", ["coverage.py"])
    assert coverage.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "任务依赖成环: R_BASE -> A1 -> R_BASE" in output.err


def test_main_fails_before_printing_coverage(monkeypatch, capsys):
    requirements, backlog = _catalog()
    backlog["tasks"][0]["covers"] = ["RA-NOT-REAL"]
    monkeypatch.setattr(coverage, "load", lambda: (requirements, backlog))
    monkeypatch.setattr(sys, "argv", ["coverage.py"])
    assert coverage.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "未知需求 ID RA-NOT-REAL" in output.err
