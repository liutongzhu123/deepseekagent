#!/usr/bin/env python3
"""Supervisor 失败路径测试（对应 bin/supervisor.py 里的 F1–F10）。

这些用例的存在理由很具体：2026-11-08 的 queued 漏洞（F1）是上线后才被用户
发现的。凡是"判定要不要派单"的逻辑，都必须有一条测试对应它的反面情况。

跑法：python3 -m pytest tests/test_supervisor.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))


def load_supervisor():
    spec = importlib.util.spec_from_file_location("supervisor", ROOT / "bin" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sup = load_supervisor()


def backlog(tasks):
    return {"tasks": tasks}


def ticket(title, state="open"):
    return {"title": title, "state": state}


# ---------- F1：worker 正在启动时不能再派 ----------

def test_f1_no_dispatch_when_running_or_queued(monkeypatch):
    """running=0 但 queued=1 时，必须判定为"忙"，否则会并行改同一文件。"""
    monkeypatch.setattr(sup, "api_state", lambda: {"load": {"running": 0, "queued": 1}})
    monkeypatch.setattr(sup, "api_tickets", lambda: [])
    monkeypatch.setattr(sup, "pull_repo", lambda: True)
    monkeypatch.setattr(sup, "load_yaml", lambda _p: backlog([
        {"id": "T1", "title": "某任务", "status": "todo", "deps": []},
    ]))
    sent = []
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(sup, "read_json", lambda _p, d: d)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)

    sup.run(dry_run=True)
    assert sent == [], "queued>0 时不应该派单"


# ---------- 派单条件 ----------

def test_dispatch_only_when_idle(monkeypatch):
    monkeypatch.setattr(sup, "api_state", lambda: {"load": {"running": 0, "queued": 0}})
    monkeypatch.setattr(sup, "api_tickets", lambda: [])
    monkeypatch.setattr(sup, "pull_repo", lambda: True)
    monkeypatch.setattr(sup, "load_yaml", lambda _p: backlog([
        {"id": "T1", "title": "某任务", "status": "todo", "deps": []},
    ]))
    sent = []
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(sup, "read_json", lambda _p, d: d)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "handle_decisions", lambda _dry: None)

    sup.run(dry_run=True)
    assert len(sent) == 1 and "T1" in sent[0]


def test_deps_block_dispatch():
    """A 依赖 B：B 没完成时不能派 A，但 B 自己可以派。"""
    ng = backlog([
        {"id": "A", "status": "todo", "deps": ["B"]},
        {"id": "B", "status": "todo", "deps": []},
    ])
    task, _ = sup.next_task(ng, busy=set(), dispatched={})
    assert task is not None and task["id"] == "B"


def test_deps_all_done_unblocks():
    ng = backlog([
        {"id": "A", "status": "todo", "deps": ["B"]},
        {"id": "B", "status": "done", "deps": []},
    ])
    task, _ = sup.next_task(ng, busy=set(), dispatched={})
    assert task is not None and task["id"] == "A"


def test_deps_satisfied_dispatch():
    ng = backlog([
        {"id": "A", "status": "todo", "deps": ["B"]},
        {"id": "B", "status": "todo", "deps": []},
    ])
    task, _ = sup.next_task(ng, busy=set(), dispatched={})
    assert task is None or task["id"] == "B"


# ---------- F2/F9：重派与上限 ----------

def test_recent_dispatch_waits():
    """刚派过、还在等待窗口内 → 不再派。"""
    recent = (datetime.now(TZ) - timedelta(seconds=60)).isoformat(timespec="seconds")
    ng = backlog([{"id": "T1", "status": "todo", "deps": []}])
    task, _ = sup.next_task(ng, busy=set(), dispatched={"T1": {"attempts": 1, "last_at": recent}})
    assert task is None


def test_stale_dispatch_retries():
    """超过等待窗口且未达上限 → 允许重派。"""
    old = (datetime.now(TZ) - timedelta(seconds=sup.REDISPATCH_SECONDS + 60)).isoformat(timespec="seconds")
    ng = backlog([{"id": "T1", "status": "todo", "deps": []}])
    task, _ = sup.next_task(ng, busy=set(), dispatched={"T1": {"attempts": 1, "last_at": old}})
    assert task is not None


def test_max_attempts_stops():
    """达到尝试上限 → 不再重派（交给告警）。"""
    old = (datetime.now(TZ) - timedelta(days=1)).isoformat(timespec="seconds")
    ng = backlog([{"id": "T1", "status": "todo", "deps": []}])
    task, _ = sup.next_task(ng, busy=set(), dispatched={"T1": {"attempts": sup.MAX_ATTEMPTS, "last_at": old}})
    assert task is None


# ---------- 在跑的任务不重复派 ----------

def test_in_flight_task_not_dispatched():
    ng = backlog([{"id": "A_LOWCONF", "status": "todo", "deps": []}])
    busy = sup.in_flight([ticket("A_LOWCONF：规则生成只用高置信限值")], {"A_LOWCONF"})
    task, _ = sup.next_task(ng, busy=busy, dispatched={})
    assert task is None


def test_closed_ticket_is_not_in_flight():
    busy = sup.in_flight([ticket("A_LOWCONF：xxx", state="closed")], {"A_LOWCONF"})
    assert busy == set()


# ---------- 任务 id 匹配不能误伤 ----------

def test_task_id_boundary():
    ids = {"A1", "A10", "B2"}
    assert sup.contains_task("A10 标准分类", ids) == "A10"
    assert sup.contains_task("A1 空白页", ids) == "A1"
    assert sup.contains_task("A100 不存在的任务", ids) is None


def test_task_id_must_be_leading():
    """标题中间提到任务 id 不算（踩过的坑："解析覆盖率审计…（B5 暂停）" 被认成 B5）。"""
    ids = {"B5", "B4"}
    assert sup.contains_task("解析覆盖率审计：逐份统计（B5 暂停）", ids) is None
    assert sup.contains_task("多版式标准解析健壮性：修复（B4 暂停）", ids) is None
    assert sup.contains_task("B5 数值修约规则", ids) == "B5"
    assert sup.contains_task("B5：数值修约规则", ids) == "B5"


# ---------- F4：合并提交反查完成 ----------

def test_reconcile_marks_done(monkeypatch):
    monkeypatch.setattr(sup, "closed_completed", lambda: [
        {"title": "C1 表格脚注单位关联", "pr": 130},
    ])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: pr == 130)
    ng = backlog([
        {"id": "C1", "status": "doing", "title": "脚注单位"},
        {"id": "B7", "status": "todo", "title": "分类"},
    ])
    changed = sup.reconcile(ng)
    assert "C1" in changed
    assert ng["tasks"][0]["status"] == "done"
    assert ng["tasks"][1]["status"] == "todo"


def test_reconcile_ignores_done(monkeypatch):
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "C1 已完成", "pr": 130}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    ng = backlog([{"id": "C1", "status": "done"}])
    assert sup.reconcile(ng) == []


def test_reconcile_needs_merge_not_just_completion(monkeypatch):
    """工单说完成但 PR 没合并 → 不能标 done（否则台账会虚高）。"""
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "C1 表格脚注单位关联", "pr": 999}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: False)
    ng = backlog([{"id": "C1", "status": "doing"}])
    assert sup.reconcile(ng) == []
    assert ng["tasks"][0]["status"] == "doing"


def test_reconcile_without_pr_number(monkeypatch):
    """工单完成但报告里没写 PR 号 → 不动台账，等人看。"""
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "C1 xxx", "pr": None}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    ng = backlog([{"id": "C1", "status": "doing"}])
    assert sup.reconcile(ng) == []


# ---------- F5：台账拉取失败不派单 ----------

def test_no_dispatch_when_pull_fails(monkeypatch):
    monkeypatch.setattr(sup, "api_state", lambda: {"load": {"running": 0, "queued": 0}})
    monkeypatch.setattr(sup, "pull_repo", lambda: False)
    monkeypatch.setattr(sup, "load_yaml", lambda _p: backlog([
        {"id": "T1", "status": "todo", "deps": []},
    ]))
    sent = []
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(sup, "read_json", lambda _p, d: d)
    monkeypatch.setattr(sup, "handle_decisions", lambda _dry: None)
    sup.run(dry_run=True)
    assert sent == []


# ---------- F6：预算超限 ----------

def test_no_dispatch_when_budget_exceeded(monkeypatch):
    monkeypatch.setattr(sup, "read_json", lambda p, d: {"exceeded": True} if "budget" in str(p) else d)
    monkeypatch.setattr(sup, "api_state", lambda: {"load": {"running": 0, "queued": 0}})
    sent = []
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    sup.run(dry_run=True)
    assert sent == []


# ---------- F7：决策恢复失败要计数 ----------

def test_decision_retry_counts_and_alert(monkeypatch):
    store = {"decisions": [{"id": "DEC-001", "answer": "C", "options": {"C": "先修"},
                            "ticket": "t", "question": "q", "attempts": sup.MAX_ATTEMPTS - 1}]}
    written = {}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, data: written.update(data))
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: False)
    alerts = []
    monkeypatch.setattr(sup, "send_alert", lambda t, b, dry=False: alerts.append(t) or True)

    sup.handle_decisions(dry_run=False)
    assert store["decisions"][0]["attempts"] == sup.MAX_ATTEMPTS
    assert alerts, "达到上限应告警"


def test_decision_success_marks_resumed(monkeypatch):
    store = {"decisions": [{"id": "DEC-002", "answer": "A", "options": {"A": "隔离"},
                            "ticket": "t", "question": "q"}]}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, data: None)
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: True)
    monkeypatch.setattr(sup, "send_alert", lambda *a, **k: True)

    sup.handle_decisions(dry_run=False)
    assert store["decisions"][0]["resumed"] is True


# ---------- 派单文本包含关键约束 ----------

def test_f11_awaiting_merge_blocks_redispatch():
    """F11：已交出 PR 但没合并 → 不能重派（B8/B9 被重复派的真实原因）。"""
    ng = backlog([{"id": "B8", "status": "todo", "deps": []}])
    task, reason = sup.next_task(ng, busy=set(), dispatched={}, awaiting={"B8"})
    assert task is None
    assert "等 PR 合并" in reason


def test_awaiting_merge_detects_unmerged_pr(monkeypatch):
    monkeypatch.setattr(sup, "_ticket_rows", lambda: [
        {"title": "B8 标准版本管理与对比", "assignments": [
            {"report": {"summary": "B8 完成：PR #137 已就绪"}}]},
        {"title": "B9 检测项目清单提取", "assignments": [
            {"report": {"summary": "B9 完成：PR #138 已合并"}}]},
    ])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: pr == 138)
    waiting = sup.awaiting_merge({"B8", "B9"})
    assert waiting == {"B8": 137}, "只有 PR 未合并的 B8 应被拦住，且要带回 PR 号"


def test_f12_nag_when_two_pending(monkeypatch):
    """F12：两个以上 PR 待合并就该催办（#137/#138 被丢下的真实场景）。"""
    sent = []
    monkeypatch.setattr(sup, "read_json", lambda _p, d: {})
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    sup.nag_pending({"B8": 137, "B9": 138}, dry_run=False)
    assert sent and "#137" in sent[0] and "#138" in sent[0]


def test_f12_no_nag_for_single_recent(monkeypatch):
    """只有一个、且刚交付 → 不打扰。"""
    sent = []
    monkeypatch.setattr(sup, "read_json", lambda _p, d: {})
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    sup.nag_pending({"B8": 137}, dry_run=False)
    assert sent == []


def test_f12_nag_when_single_overdue(monkeypatch):
    """只有一个但等了很久 → 也要催。"""
    old = (datetime.now(TZ) - timedelta(seconds=sup.NAG_AFTER_SECONDS + 60)).isoformat(timespec="seconds")
    sent = []
    monkeypatch.setattr(sup, "read_json", lambda _p, d: {"B8": {"pr": 137, "since": old}})
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    sup.nag_pending({"B8": 137}, dry_run=False)
    assert sent and "#137" in sent[0]


def test_f12_cooldown_suppresses_repeat(monkeypatch):
    """冷却期内不重复催。"""
    recent = (datetime.now(TZ) - timedelta(seconds=60)).isoformat(timespec="seconds")
    sent = []
    monkeypatch.setattr(sup, "read_json", lambda _p, d: {"_last_nag_at": recent})
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    sup.nag_pending({"B8": 137, "B9": 138}, dry_run=False)
    assert sent == []


def test_f13_records_reply_with_id(monkeypatch):
    """F13：飞书回 `DEC-001 C` → 记进决策表。"""
    store = {"decisions": [{"id": "DEC-001", "options": {"A": "甲", "C": "丙"}, "answer": None}]}
    written = {}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, data: written.update(data))
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=10: [
        {"sender": {"sender_type": "user"}, "content": "DEC-001 C"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.poll_decisions(dry_run=False) == 1
    assert store["decisions"][0]["answer"] == "C"


def test_f13_ignores_app_messages(monkeypatch):
    """agent（飞书里 sender_type=app）自己的消息不能当成人答复。

    2026-11-08 实测：飞书返回的 sender_type 只有 `user` 和 `app`，没有 `bot`。
    """
    store = {"decisions": [{"id": "DEC-001", "options": {"C": "丙"}, "answer": None}]}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=10: [
        {"sender": {"sender_type": "app"}, "content": "DEC-001 C"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.poll_decisions(dry_run=False) == 0
    assert store["decisions"][0]["answer"] is None


def test_f13_bare_letter_only_when_single_pending(monkeypatch):
    """裸回字母：只有唯一开放决策时才接受，多个决策时不猜。"""
    store = {"decisions": [
        {"id": "DEC-001", "options": {"A": "甲"}, "answer": None},
        {"id": "DEC-002", "options": {"A": "甲"}, "answer": None}]}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=10: [
        {"sender": {"sender_type": "user"}, "content": "A"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.poll_decisions(dry_run=False) == 0, "两个开放决策时不能猜"


def test_f13_rejects_invalid_option(monkeypatch):
    store = {"decisions": [{"id": "DEC-001", "options": {"A": "甲"}, "answer": None}]}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=10: [
        {"sender": {"sender_type": "user"}, "content": "DEC-001 Z"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.poll_decisions(dry_run=False) == 0
    assert store["decisions"][0]["answer"] is None


def test_f14_registers_decision_from_agent_message(monkeypatch):
    """F14：agent 按约定格式发 [DEC-001]，Supervisor 自动建决策。"""
    store = {"decisions": []}
    written = {}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, data: written.update(data))
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"},
         "content": "[DEC-001] 低置信限值怎么处理？\nA 只展示不进规则\nB 标注后进规则\nC 先修脚注单位\ntask: B6"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.auto_register_decisions(dry_run=False) == 1
    decision = store["decisions"][0]
    assert decision["id"] == "DEC-001"
    assert decision["options"] == {"A": "只展示不进规则", "B": "标注后进规则", "C": "先修脚注单位"}
    assert decision["ticket"] == "B6"


def test_f14_idempotent(monkeypatch):
    """同一个 DEC id 不能重复登记。"""
    store = {"decisions": [{"id": "DEC-001", "options": {"A": "甲", "B": "乙"}, "answer": None}]}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"},
         "content": "[DEC-001] 重复的问题？\nA 甲\nB 乙"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.auto_register_decisions(dry_run=False) == 0
    assert len(store["decisions"]) == 1


def test_f14_ignores_user_and_short_blocks(monkeypatch):
    """人发的消息不建决策；选项不足 2 个也不建。"""
    store = {"decisions": []}
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "user"}, "content": "[DEC-002] 人说的\nA 甲\nB 乙"},
        {"sender": {"sender_type": "app"}, "content": "[DEC-003] 只有一个选项\nA 甲"}])
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.auto_register_decisions(dry_run=False) == 0


def test_f14_then_reply_resumes(monkeypatch):
    """完整链路：agent 发决策 → 自动登记 → 人回 C → 记为答案。"""
    store = {"decisions": []}
    monkeypatch.setattr(sup, "write_json", lambda _p, _d: None)
    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    monkeypatch.setattr(sup, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"},
         "content": "[DEC-007] 选哪个？\nA 甲\nB 乙"}])
    assert sup.auto_register_decisions(dry_run=False) == 1

    monkeypatch.setattr(sup, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "user"}, "content": "DEC-007 B"}])
    assert sup.poll_decisions(dry_run=False) == 1
    assert store["decisions"][0]["answer"] == "B"


def test_f15_merged_pr_not_awaiting(monkeypatch):
    """F15：GitHub 说 MERGED 就不是待合并（本地 git log 过期会误判）。"""
    monkeypatch.setattr(sup, "pr_state", lambda pr: "MERGED")
    assert sup.merge_commit_for(104) is True
    monkeypatch.setattr(sup, "pr_state", lambda pr: "OPEN")
    assert sup.merge_commit_for(104) is False


def test_f16_cooldown_survives_write(monkeypatch):
    """F16：冷却时间戳必须持久化，否则每轮都催（曾每 4 分钟刷一次）。"""
    recent = (datetime.now(TZ) - timedelta(seconds=60)).isoformat(timespec="seconds")
    store = {"_last_nag_at": recent}
    written = {}
    sent = []

    monkeypatch.setattr(sup, "read_json", lambda _p, d: store)
    monkeypatch.setattr(sup, "write_json", lambda _p, data: written.update(data))
    monkeypatch.setattr(sup, "pr_created_at", lambda pr: datetime.now(TZ) - timedelta(hours=1))
    monkeypatch.setattr(sup, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    sup.nag_pending({"T302": 104}, dry_run=False)
    assert sent == [], "冷却期内不该催"
    assert written.get("_last_nag_at") == recent, "冷却时间戳被写入覆盖掉了"


def test_f25_writes_status_back_to_file(tmp_path):
    """F25：reconcile 改了内存后必须真正写回文件，否则 git 永远 nothing to commit。"""
    path = tmp_path / "BACKLOG.yaml"
    path.write_text(
        "tasks:\n"
        "  - id: B10\n"
        "    title: 人工复核闭环\n"
        "    status: doing\n"
        "    size: M\n"
        "  - id: B11\n"
        "    status: todo\n", encoding="utf-8")

    assert sup.set_task_status(path, "B10", "done", 144) is True
    text = path.read_text(encoding="utf-8")
    assert "status: done" in text
    assert "pr: 144" in text
    assert text.index("status: done") < text.index("id: B11"), "只能改 B10 那一段"
    assert "status: todo" in text, "B11 不受影响"


def test_f25_missing_task_returns_false(tmp_path):
    path = tmp_path / "BACKLOG.yaml"
    path.write_text("tasks:\n  - id: X\n    status: todo\n", encoding="utf-8")
    assert sup.set_task_status(path, "NOPE", "done") is False


def test_f41_reconcile_keeps_partial(monkeypatch):
    """F41：人工标成 partial 的任务，不能被 reconcile 覆盖回 done。

    2026-11-08 实测：人工把 CS_BASE 标 partial，5 秒后被 reconcile 改回 done。
    """
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "CS_BASE 基础数据", "pr": 155}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    ng = backlog([
        {"id": "CS_BASE", "status": "partial"},
        {"id": "OTHER", "status": "doing"},
    ])
    changed = sup.reconcile(ng)
    assert "CS_BASE" not in changed, "不能覆盖人工判断"
    assert ng["tasks"][0]["status"] == "partial"


def test_f41_reconcile_keeps_waiting(monkeypatch):
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "CS_QUOTE 报价", "pr": 300}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    ng = backlog([{"id": "CS_QUOTE", "status": "waiting"}])
    assert sup.reconcile(ng) == []
    assert ng["tasks"][0]["status"] == "waiting"


def test_f52_todo_task_with_rejected_pr_gets_reset(monkeypatch):
    """F52：任务本来就在 todo、次数已满，但只要 PR 被拒，就该重置并能派单。"""
    dispatched = {"RA_PHOTO_SEQ": {"attempts": 3, "last_at": "2026-11-08T20:55:31+08:00"}}
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo", "pr": 167}])
    rejected_now = {167}
    for item in ng["tasks"]:
        tid = str(item.get("id"))
        if str(item.get("status")) != "todo":
            continue
        pr = int(item["pr"])
        if pr in rejected_now and (dispatched.get(tid) or {}).get("attempts"):
            dispatched.pop(tid, None)
    task, _ = sup.next_task(ng, set(), dispatched, set())
    assert task is not None and task["id"] == "RA_PHOTO_SEQ"


def test_f53_rejected_reset_only_once(monkeypatch):
    """F53：同一个被拒 PR 只重置一次，不能每轮都清（否则会重复派单）。"""
    dispatched = {}
    rejected_now = {167}
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo", "pr": 167}])
    logs = []
    monkeypatch.setattr(sup, "log", lambda msg, *a, **k: logs.append(msg))

    def once():
        for item in ng["tasks"]:
            tid = str(item.get("id"))
            if str(item.get("status")) != "todo":
                continue
            pr = int(item["pr"])
            if pr not in rejected_now:
                continue
            record = dispatched.get(tid) or {}
            if record.get("reset_for") == pr:
                continue
            dispatched[tid] = {"attempts": 0, "last_at": "2026-11-08T22:50:00+08:00", "reset_for": pr}
            sup.log("reset %s" % tid)

    once(); once(); once()
    assert len(logs) == 1, "只应重置一次"


def test_f54_open_pr_blocks_dispatch(monkeypatch):
    """F54：任务已有未合并的 PR 时，不再派单。"""
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo"}])
    task, reason = sup.next_task(ng, set(), {}, set(), {"RA_PHOTO_SEQ": 181})
    assert task is None
    assert "未合并" in reason and "181" in reason


def test_f51_max_attempts_is_reported(monkeypatch):
    """F51：达到派单上限的任务必须出现在 blocked 原因里，不能静默消失。"""
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo"}])
    dispatched = {"RA_PHOTO_SEQ": {"attempts": 3, "last_at": "2026-11-08T20:55:31+08:00"}}
    task, reason = sup.next_task(ng, set(), dispatched, set())
    assert task is None
    assert "RA_PHOTO_SEQ" in reason, "必须说清是哪个任务卡住了"
    assert "3" in reason


def test_f51_rework_resets_attempts(monkeypatch):
    """F51：验证拒绝后返工，要重置派单次数，否则永远派不出去。"""
    monkeypatch.setattr(sup, "rejected_prs", lambda: {167})
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "done", "pr": 167}])
    reverted = sup.revert_rejected(ng)
    assert reverted == ["RA_PHOTO_SEQ"]
    dispatched = {"RA_PHOTO_SEQ": {"attempts": 3, "last_at": "2026-11-08T20:55:31+08:00"}}
    for tid in reverted:
        dispatched.pop(tid, None)
    task, reason = sup.next_task(ng, set(), dispatched, set())
    assert task is not None, "重置后必须能重新派单"


def test_f50_reject_log_dedupe(monkeypatch):
    """F50：同一任务的同一个拒绝 PR，只记一次日志。"""
    logs = []
    seen = {}
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "RA_PHOTO_SEQ 照片", "pr": 151}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    monkeypatch.setattr(sup, "rejected_prs", lambda: {151})
    monkeypatch.setattr(sup, "read_json",
                        lambda path, default: dict(seen) if "reject_logged" in str(path) else default)
    monkeypatch.setattr(sup, "write_json",
                        lambda path, data: seen.update(data) if "reject_logged" in str(path) else None)
    monkeypatch.setattr(sup, "log", lambda msg, *a, **k: logs.append(msg))

    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo", "pr": 151}])
    sup.reconcile(ng)
    sup.reconcile(ng)
    sup.reconcile(ng)
    assert len(logs) == 1, "重复轮次不该重复记日志"


def test_f48_reconcile_prefers_task_pr(monkeypatch):
    """F48：台账里的 task.pr 比工单报告里的旧 PR 更权威。"""
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "RA_PHOTO_SEQ 照片序号", "pr": 151}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    monkeypatch.setattr(sup, "rejected_prs", lambda: {151})   # 旧 PR 被拒
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "doing", "pr": 167}])  # 新 PR 没被拒
    assert sup.reconcile(ng) == ["RA_PHOTO_SEQ"], "应认新 PR #167 并标 done"
    assert ng["tasks"][0]["status"] == "done"


def test_f60_sync_uses_pr_titles(monkeypatch):
    """F60：关联要用 GitHub PR 标题，不能用 git 提交标题（合并提交里没标记）。"""
    monkeypatch.setattr(sup, "merged_task_prs", lambda limit=60: {"RA_VALUE_RANGE": 186})
    ng = backlog([{"id": "RA_VALUE_RANGE", "status": "todo", "pr": None}])
    updated = sup.sync_task_pr(ng)
    assert updated == ["RA_VALUE_RANGE"]
    assert ng["tasks"][0]["pr"] == 186


def test_f45_sync_task_pr_from_title(monkeypatch):
    """F45/F60：返工 PR 标题带 [任务ID] → task.pr 更新为新 PR。"""
    monkeypatch.setattr(sup, "merged_task_prs", lambda limit=60: {"RA_PHOTO_SEQ": 167})
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo", "pr": 151}])
    updated = sup.sync_task_pr(ng)
    assert updated == ["RA_PHOTO_SEQ"]
    assert ng["tasks"][0]["pr"] == 167, "必须指向新 PR"


def test_f45_ignores_unmarked_or_unknown(monkeypatch):
    monkeypatch.setattr(sup, "merged_task_prs", lambda limit=60: {"NOT_A_TASK": 201})
    ng = backlog([{"id": "RA_TEMPLATE", "status": "todo", "pr": 153}])
    assert sup.sync_task_pr(ng) == []
    assert ng["tasks"][0]["pr"] == 153


def test_f45_same_pr_not_duplicated(monkeypatch):
    monkeypatch.setattr(sup, "git", lambda args, timeout=120: type(
        "R", (), {"returncode": 0, "stdout": "abc|fix: [RA_TEMPLATE] (#168)\\n", "stderr": ""})())
    ng = backlog([{"id": "RA_TEMPLATE", "status": "todo", "pr": 168}])
    assert sup.sync_task_pr(ng) == []


def test_f44_revert_rejected_task(monkeypatch):
    """F44：验证拒绝后，done 的任务要退回 todo（reconcile 只会往前推）。"""
    monkeypatch.setattr(sup, "rejected_prs", lambda: {149})
    ng = backlog([
        {"id": "RA_STDVER", "status": "done", "pr": 149},
        {"id": "B7", "status": "done", "pr": 136},
    ])
    reverted = sup.revert_rejected(ng)
    assert reverted == ["RA_STDVER"]
    assert ng["tasks"][0]["status"] == "todo"
    assert ng["tasks"][1]["status"] == "done", "没被拒绝的不受影响"


def test_f44_no_rejections_no_change(monkeypatch):
    monkeypatch.setattr(sup, "rejected_prs", lambda: set())
    ng = backlog([{"id": "X", "status": "done", "pr": 1}])
    assert sup.revert_rejected(ng) == []


def test_f42_reconcile_skips_rejected_pr(monkeypatch):
    """F42：验证判「拒绝」的 PR，即使已合并也不能把任务标 done。

    2026-11-08 实测：RA_STDVER 被验证拒绝、回退 todo，19:22 又被 reconcile 改回 done。
    """
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "RA_STDVER 标准年代号", "pr": 149}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    monkeypatch.setattr(sup, "rejected_prs", lambda: {149})
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)
    ng = backlog([{"id": "RA_STDVER", "status": "todo"}])
    assert sup.reconcile(ng) == []
    assert ng["tasks"][0]["status"] == "todo", "验证拒绝的任务必须留在 todo"


def test_f42_reconcile_accepts_non_rejected(monkeypatch):
    monkeypatch.setattr(sup, "closed_completed", lambda: [{"title": "B7 分类", "pr": 136}])
    monkeypatch.setattr(sup, "merge_commit_for", lambda pr: True)
    monkeypatch.setattr(sup, "rejected_prs", lambda: set())
    ng = backlog([{"id": "B7", "status": "doing"}])
    assert sup.reconcile(ng) == ["B7"]


def test_f30_sync_self_updates_from_main(monkeypatch, tmp_path):
    """F30：服务器 bin/ 与 main 不一致时要自动同步。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "budget.py").write_text("# old\n", encoding="utf-8")

    monkeypatch.setattr(sup, "BIN", bin_dir)
    monkeypatch.setattr(sup, "git", lambda args: type(
        "R", (), {"returncode": 0, "stdout": "# new\n", "stderr": ""})())
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    updated = sup.sync_self(dry_run=False)
    assert "budget.py" in updated
    assert (bin_dir / "budget.py").read_text(encoding="utf-8") == "# new\n"


def test_f30_sync_self_skips_identical(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "budget.py").write_text("# same\n", encoding="utf-8")

    monkeypatch.setattr(sup, "BIN", bin_dir)
    monkeypatch.setattr(sup, "git", lambda args: type(
        "R", (), {"returncode": 0, "stdout": "# same\n", "stderr": ""})())
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    updated = sup.sync_self(dry_run=False)
    assert "budget.py" not in updated, "内容相同不该重写"


def test_f30_sync_self_refuses_broken_syntax(monkeypatch, tmp_path):
    """main 上的文件语法不通过 → 不能同步，否则把自己跑挂。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "budget.py").write_text("# good\n", encoding="utf-8")

    monkeypatch.setattr(sup, "BIN", bin_dir)
    monkeypatch.setattr(sup, "git", lambda args: type(
        "R", (), {"returncode": 0, "stdout": "def broken(:\n", "stderr": ""})())
    monkeypatch.setattr(sup, "log", lambda *_a, **_k: None)

    assert sup.sync_self(dry_run=False) == []
    assert (bin_dir / "budget.py").read_text(encoding="utf-8") == "# good\n", "不应覆盖"


def test_dispatch_message_contains_acceptance_and_protocol():
    text = sup.dispatch_message({
        "id": "B7", "title": "标准类型分类", "covers": ["SP-10.1"],
        "files": ["p0/app/x.py"], "acceptance": ["误判率 <10%"], "note": "备注",
    }, attempt=2)
    assert "B7" in text and "SP-10.1" in text and "误判率 <10%" in text
    assert "第 2 次派发" in text
    assert "10.3c" in text, "派单必须带上『超阈值继续做下一件』的协议约束"
    assert "gh-agent pr merge" in text, "派单必须给出自合命令"
    assert "gh-agent auth status" in text, "派单必须先验证权限再下结论"


def test_f55_record_update_keeps_reset_for():
    """F55：派单更新 record 时必须保留 reset_for，否则 F53 形同虚设。"""
    record = {"attempts": 0, "last_at": "2026-11-08T23:08:00+08:00", "reset_for": 167}
    record = dict(record)
    record["attempts"] = int(record.get("attempts") or 0) + 1
    assert record.get("reset_for") == 167, "reset_for 不能丢"
    assert record["attempts"] == 1


def test_f56_verifying_pr_is_in_flight(monkeypatch):
    """F56：PR 已合并、验证还在跑时，不该再派同一任务。"""
    monkeypatch.setattr(sup, "rejected_prs", lambda: {167})
    monkeypatch.setattr(sup, "verifying_prs", lambda: {167})
    reset_happened = []
    ng = backlog([{"id": "RA_PHOTO_SEQ", "status": "todo", "pr": 167}])
    for item in ng["tasks"]:
        pr = int(item["pr"])
        if pr in sup.verifying_prs():
            continue
        reset_happened.append(pr)
    assert reset_happened == [], "验证中的 PR 不应触发重置/派单"


def test_f59_pr_line_can_be_overwritten(tmp_path):
    """F59：传入 pr 时必须覆盖已有的 pr 行，否则关联返工 PR 永远改不掉。"""
    path = tmp_path / "BACKLOG.yaml"
    path.write_text("  - id: T1\n    status: todo\n    pr: 181\n", encoding="utf-8")
    assert sup.set_task_status(path, "T1", "todo", 203) is True
    text = path.read_text(encoding="utf-8")
    assert "pr: 203" in text and "pr: 181" not in text
