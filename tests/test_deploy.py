#!/usr/bin/env python3
"""部署自动化的失败路径测试（对应 bin/deploy.py 的 F17–F22）。

核心原则：**"测试跑不起来"绝不等于"通过"**，宁可少部署一次，也不要把坏版本推上去。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_deploy():
    spec = importlib.util.spec_from_file_location("deploy", ROOT / "bin" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dep = load_deploy()


@pytest.fixture(autouse=True)
def no_target_cleanup(monkeypatch):
    """所有用例默认不真的执行 clean_junk()——它会 ssh 到目标机跑 find -delete。

    2026-11-08 第二轮验证指出：调用 run() 的用例没有 mock clean_junk()，测试会
    真实连目标机执行 5 次 find -delete。用 autouse 兜底，新增 run() 用例也不会
    再误连目标机；需要断言调用顺序的用例自行覆盖本 fixture。
    """
    calls: list[str] = []
    monkeypatch.setattr(dep, "clean_junk", lambda: calls.append("clean"))
    return calls


# ---------- F19：幂等 ----------

def test_f19_same_sha_skipped():
    should, reason = dep.needs_deploy({"deployed_sha": "abc123", "status": "ok"}, "abc123")
    assert should is False and "已部署" in reason


def test_f19_new_sha_needs_deploy():
    should, _ = dep.needs_deploy({"deployed_sha": "abc123", "status": "ok"}, "def456")
    assert should is True


def test_f19_failed_state_retries():
    """上次失败 → 同一 sha 仍要重试。"""
    should, _ = dep.needs_deploy({"deployed_sha": "abc123", "status": "failed"}, "abc123")
    assert should is True


def test_unknown_sha_blocks_deploy():
    """拿不到最新 sha（fetch 失败）→ 不部署。"""
    should, reason = dep.needs_deploy({"deployed_sha": "abc"}, None)
    assert should is False and "拿不到" in reason


# ---------- F17：测试跑不起来 ≠ 通过 ----------

def test_f17_test_error_is_failure(monkeypatch):
    """健康通过但 pytest 报 error → 判失败（且说明是测试环节出的问题）。"""
    responses = iter([
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),        # restart
        type("R", (), {"returncode": 0, "stdout": "200", "stderr": ""})(),     # health
        type("R", (), {"returncode": 0, "stdout": "1 error in 0.5s", "stderr": ""})(),  # pytest
    ])
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=300: next(responses))
    ok, detail = dep.restart_and_check()
    assert ok is False and ("无法判定" in detail or "未通过" in detail)


def test_f17_no_passed_marker_is_failure(monkeypatch):
    """输出里既没有 passed 也没有 failed（比如被中断）→ 必须判失败。"""
    responses = iter([
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),        # restart
        type("R", (), {"returncode": 0, "stdout": "200", "stderr": ""})(),     # health
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),        # pytest 空输出
    ])
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=300: next(responses))
    ok, detail = dep.restart_and_check()
    assert ok is False


def test_f17_health_not_200(monkeypatch):
    """健康检查重试后仍不是 200 → 判失败。"""
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=300: type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(dep, "wait_healthy", lambda retries=20, delay=2: (False, "502"))
    ok, detail = dep.restart_and_check()
    assert ok is False and "502" in detail


def test_healthy_deploy_passes(monkeypatch):
    responses = iter([
        type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        type("R", (), {"returncode": 0, "stdout": "200", "stderr": ""})(),
        type("R", (), {"returncode": 0, "stdout": "385 passed, 33 skipped", "stderr": ""})(),
    ])
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=300: next(responses))
    ok, detail = dep.restart_and_check()
    assert ok is True and "385 passed" in detail


# ---------- F21：目标机不可达 ----------

def test_f21_unreachable_recorded(monkeypatch):
    monkeypatch.setattr(dep, "target_reachable", lambda: False)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "deadbeef")
    monkeypatch.setattr(dep, "ENABLED", True)
    written = {}
    monkeypatch.setattr(dep, "read_state", lambda: {})
    monkeypatch.setattr(dep, "write_state", lambda data: written.update(data))
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
    assert written.get("status") == "unreachable"
    assert "deployed_sha" not in written, "不可达时绝不能标记成已部署"


# ---------- F18：备份失败就别动 ----------

def test_f18_no_backup_aborts(monkeypatch):
    monkeypatch.setattr(dep, "target_reachable", lambda: True)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "deadbeef")
    monkeypatch.setattr(dep, "ENABLED", True)
    monkeypatch.setattr(dep, "backup_target", lambda tag: None)
    monkeypatch.setattr(dep, "deploy_payload", lambda sha: (_ for _ in ()).throw(
        AssertionError("没有备份就不能同步代码")))
    written = {}
    monkeypatch.setattr(dep, "read_state", lambda: {})
    monkeypatch.setattr(dep, "write_state", lambda data: written.update(data))
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
    assert written.get("status") == "failed"


# ---------- F20：同步失败记 failed，下一轮重试 ----------

def test_f20_payload_failure_marks_failed(monkeypatch):
    monkeypatch.setattr(dep, "target_reachable", lambda: True)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "deadbeef")
    monkeypatch.setattr(dep, "ENABLED", True)
    monkeypatch.setattr(dep, "backup_target", lambda tag: "/root/backup.tgz")
    monkeypatch.setattr(dep, "deploy_payload", lambda sha: False)
    written = {}
    monkeypatch.setattr(dep, "read_state", lambda: {})
    monkeypatch.setattr(dep, "write_state", lambda data: written.update(data))
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
    assert written.get("status") == "failed"
    assert "deployed_sha" not in written


# ---------- F18：校验失败要回滚 ----------

def test_f18_rollback_on_check_failure(monkeypatch):
    monkeypatch.setattr(dep, "target_reachable", lambda: True)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "cafebabe")
    monkeypatch.setattr(dep, "ENABLED", True)
    monkeypatch.setattr(dep, "backup_target", lambda tag: "/root/backup.tgz")
    monkeypatch.setattr(dep, "deploy_payload", lambda sha: True)
    monkeypatch.setattr(dep, "restart_and_check", lambda: (False, "健康检查返回 502"))
    rolled = {}
    monkeypatch.setattr(dep, "rollback", lambda path: rolled.update({"path": path}) or True)
    written = {}
    monkeypatch.setattr(dep, "read_state", lambda: {})
    monkeypatch.setattr(dep, "write_state", lambda data: written.update(data))
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
    assert rolled.get("path") == "/root/backup.tgz", "校验失败必须回滚"
    assert written.get("status") == "failed"
    assert written.get("rolled_back") is True


def test_f23_payload_uses_target_sha(monkeypatch):
    """F23：必须按 sha 打包（git archive <sha>），不能 tar 工作区。"""
    captured = {}

    def fake_sh(args, timeout=300):
        captured["cmd"] = args[-1]
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(dep, "sh", fake_sh)
    assert dep.deploy_payload("deadbeef") is True
    assert "git -C" in captured["cmd"] and "archive" in captured["cmd"]
    assert "deadbeef" in captured["cmd"]
    assert "strip-components=1" in captured["cmd"]


def test_f24_health_retries_until_ready(monkeypatch):
    """F24：前两次返回 000（还没起来），第三次 200 → 应判成功。"""
    codes = iter(["000", "000", "200"])
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=30: type(
        "R", (), {"returncode": 0, "stdout": next(codes), "stderr": ""})())
    monkeypatch.setattr(dep, "time", type("T", (), {"sleep": staticmethod(lambda _s: None)}))
    ok, detail = dep.wait_healthy(retries=5, delay=0)
    assert ok is True and detail == "200"


def test_f24_health_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(dep, "ssh", lambda cmd, timeout=30: type(
        "R", (), {"returncode": 0, "stdout": "000", "stderr": ""})())
    monkeypatch.setattr(dep, "time", type("T", (), {"sleep": staticmethod(lambda _s: None)}))
    ok, detail = dep.wait_healthy(retries=3, delay=0)
    assert ok is False and detail == "000"


# ---------- F40：目标机残留垃圾（._* / .DS_Store / __pycache__） ----------

def test_f40_clean_junk_runs_before_and_after_payload(monkeypatch):
    """回归：部署前后各清一次；移除 run() 里的 clean_junk() 调用本用例必须变红。"""
    order: list[str] = []
    monkeypatch.setattr(dep, "clean_junk", lambda: order.append("clean"))
    monkeypatch.setattr(dep, "target_reachable", lambda: True)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "cafebabe")
    monkeypatch.setattr(dep, "ENABLED", True)
    monkeypatch.setattr(dep, "backup_target", lambda tag: "/root/backup.tgz")
    monkeypatch.setattr(dep, "deploy_payload", lambda sha: order.append("payload") or True)
    monkeypatch.setattr(dep, "restart_and_check", lambda: (True, "385 passed"))
    monkeypatch.setattr(dep, "read_state", lambda: {"history": []})
    monkeypatch.setattr(dep, "write_state", lambda data: None)
    monkeypatch.setattr(dep, "tree_manifest", lambda: "manifest")
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)

    assert order == ["clean", "payload", "clean"], "部署前/解压后都必须清理目标机垃圾"


# ---------- 成功路径 ----------

def test_success_records_history(monkeypatch):
    monkeypatch.setattr(dep, "target_reachable", lambda: True)
    monkeypatch.setattr(dep, "local_main_sha", lambda: "cafebabe")
    monkeypatch.setattr(dep, "ENABLED", True)
    monkeypatch.setattr(dep, "backup_target", lambda tag: "/root/backup.tgz")
    monkeypatch.setattr(dep, "deploy_payload", lambda sha: True)
    monkeypatch.setattr(dep, "restart_and_check", lambda: (True, "385 passed"))
    written = {}
    monkeypatch.setattr(dep, "read_state", lambda: {"history": []})
    monkeypatch.setattr(dep, "write_state", lambda data: written.update(data))
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
    assert written.get("deployed_sha") == "cafebabe"
    assert written.get("status") == "ok"
    assert written["history"][-1]["sha"] == "cafebabe"


# ---------- 未启用时不碰目标机 ----------

def test_disabled_does_not_touch_target(monkeypatch):
    monkeypatch.setattr(dep, "local_main_sha", lambda: "cafebabe")
    monkeypatch.setattr(dep, "ENABLED", False)
    monkeypatch.setattr(dep, "target_reachable", lambda: (_ for _ in ()).throw(
        AssertionError("未启用时不该连目标机")))
    monkeypatch.setattr(dep, "read_state", lambda: {})
    monkeypatch.setattr(dep, "write_state", lambda _d: None)
    monkeypatch.setattr(dep, "log", lambda *_a, **_k: None)

    dep.run(dry_run=False)
