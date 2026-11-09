#!/usr/bin/env python3
"""调度器存活检查的失败路径测试（对应 bin/liveness.py 的 F1–F5）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("liveness", ROOT / "bin" / "liveness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = load()


def test_f1_first_run_only_baselines(tmp_path, monkeypatch):
    """F1：首次运行只建基线，不告警。"""
    monkeypatch.setattr(live, "STATE", tmp_path / "liveness.json")
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "collect_problems", lambda: ["不该被调用"])
    assert live.run(dry_run=False) == 0
    assert "last_check_at" in (tmp_path / "liveness.json").read_text(encoding="utf-8")


def test_f2_uses_event_timestamp(monkeypatch):
    """F2：用事件时间戳判断，不看文件 mtime。"""
    from datetime import timedelta
    old = (live.now() - timedelta(minutes=40)).isoformat(timespec="seconds")
    monkeypatch.setattr(live, "last_event_time", lambda: live.now() - timedelta(minutes=40))
    problems = live.collect_problems()
    assert any("没有新记录" in p for p in problems), "40 分钟没记录必须报"
    assert old  # 保留变量说明语义


def test_f5_missing_log_counts_as_stale(monkeypatch, tmp_path):
    """F5：日志不存在时按"从没动过"处理。"""
    monkeypatch.setattr(live, "EVENTS", tmp_path / "nope.log")
    problems = live.collect_problems()
    assert any("events.log" in p for p in problems)


def test_f3_cooldown_suppresses_repeat(tmp_path, monkeypatch):
    """F3：冷却期内不重复告警。"""
    state_file = tmp_path / "liveness.json"
    import json
    state_file.write_text(json.dumps({
        "last_check_at": live.now().isoformat(timespec="seconds"),
        "alerted_at": live.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")
    monkeypatch.setattr(live, "STATE", state_file)
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "collect_problems", lambda: ["问题X"])
    sent = []
    monkeypatch.setattr(live, "send", lambda text, dry=False: sent.append(text) or True)
    live.run(dry_run=False)
    assert sent == [], "冷却期内不应再发"


def test_f4_send_failure_does_not_crash(tmp_path, monkeypatch):
    """F4：发送失败要能记录且不抛异常。"""
    state_file = tmp_path / "liveness.json"
    import json
    state_file.write_text(json.dumps({"last_check_at": live.now().isoformat(timespec="seconds")}),
                          encoding="utf-8")
    monkeypatch.setattr(live, "STATE", state_file)
    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "collect_problems", lambda: ["问题Y"])
    monkeypatch.setattr(live, "send", lambda text, dry=False: False)
    monkeypatch.setattr(live, "log", lambda *_a, **_k: None)
    assert live.run(dry_run=False) == 0
    assert "问题Y" in state_file.read_text(encoding="utf-8")


def test_timer_check_flags_inactive(monkeypatch):
    monkeypatch.setattr(live, "timer_inactive", lambda timer: True)
    monkeypatch.setattr(live, "last_event_time", live.now)
    monkeypatch.setattr(live, "unit_failures", lambda unit: False)
    problems = live.collect_problems()
    assert len([p for p in problems if "定时器" in p]) >= 1
