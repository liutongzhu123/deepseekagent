"""Guard the agent CI paths and its server-state isolation (F67–F69)."""

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "agent-ci.yml"


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML uses YAML 1.1, where the GitHub Actions key "on" parses as True.
    data["events"] = data.get("on", data.get(True))
    return data


def test_f67_bin_and_root_tests_trigger_ci():
    events = _workflow()["events"]
    required = {"bin/**", "tests/**", ".github/workflows/agent-ci.yml"}
    for event in ("push", "pull_request"):
        assert required <= set(events[event]["paths"])
        assert "main" in events[event]["branches"]


def test_f68_server_state_is_isolated_and_f69_backlog_is_checked():
    workflow = _workflow()
    job = workflow["jobs"]["agent-tests"]
    for key in ("SUPERVISOR_STATE", "DEPLOY_STATE_DIR", "VERIFY_STATE_DIR", "LIVENESS_STATE_DIR"):
        assert job["env"][key].startswith("${{ runner.temp }}/")
    commands = [step.get("run", "") for step in job["steps"]]
    assert "python -m pytest -q tests" in commands
    assert "python scripts/coverage.py" in commands
    for event in ("push", "pull_request"):
        paths = set(workflow["events"][event]["paths"])
        assert {"docs/BACKLOG.yaml", "docs/requirements.yaml", "scripts/coverage.py"} <= paths
