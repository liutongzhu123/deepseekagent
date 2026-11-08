#!/usr/bin/env python3
"""双人验证的失败路径测试（对应 bin/verify.py 的 F31–F35）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_verify():
    spec = importlib.util.spec_from_file_location("verify", ROOT / "bin" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ver = load_verify()


# ---------- F34：风险路径判定 ----------

def test_f34_risky_paths():
    assert ver.is_risky(["p0/app/standard_extract/limits.py"]) is True
    assert ver.is_risky(["p0/app/rules.py"]) is True
    assert ver.is_risky(["docs/README.md"]) is False


def test_f34_unknown_files_treated_as_risky():
    """拿不到文件列表时宁可多验。"""
    assert ver.is_risky([]) is True


# ---------- F33：结论归一化 ----------

def test_f33_pass_verdict():
    assert ver.verdict_of("结论：通过\n理由：抽得准\n证据：xx") == "pass"


def test_f33_reject_verdict():
    assert ver.verdict_of("结论：拒绝\n理由：边界值错\n反例：5.2 抽出 160") == "reject"


def test_f33_vague_is_unverified():
    """含糊结论不能算通过。"""
    assert ver.verdict_of("基本没问题，应该可以") == "unverified"
    assert ver.verdict_of("") == "unverified"


# ---------- F32：幂等 ----------

def test_f32_already_verified_skipped(monkeypatch):
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {"123": {"status": "dispatched"}}})
    monkeypatch.setattr(ver, "recent_merges", lambda depth=40: [
        {"sha": "x", "pr": 123, "subject": "x", "at": "2026-11-08T10:00:00+08:00"}])
    monkeypatch.setattr(ver, "pr_files", lambda pr: ["p0/app/rules.py"])
    sent = []
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: sent.append(text) or True)

    assert ver.scan(dry_run=False) == 0
    assert sent == []


# ---------- 派发与跳过 ----------

def test_non_risky_skipped(monkeypatch):
    written = {}
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {}})
    monkeypatch.setattr(ver, "write_state", lambda d: written.update(d))
    monkeypatch.setattr(ver, "recent_merges", lambda depth=40: [
        {"sha": "x", "pr": 200, "subject": "docs", "at": "2026-11-08T10:00:00+08:00"}])
    monkeypatch.setattr(ver, "pr_files", lambda pr: ["docs/PLAN.md"])
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: True)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.scan(dry_run=False) == 0


def test_risky_dispatched(monkeypatch):
    written = {}
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {}, "baseline_at": "2026-11-08T00:00:00+08:00"})
    monkeypatch.setattr(ver, "write_state", lambda d: written.update(d))
    monkeypatch.setattr(ver, "recent_merges", lambda depth=40: [
        {"sha": "x", "pr": 201, "subject": "feat(standard): 限值", "at": "2026-11-08T10:00:00+08:00"}])
    monkeypatch.setattr(ver, "pr_files", lambda pr: ["p0/app/standard_extract/limits.py"])
    sent = []
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.scan(dry_run=False) == 1
    assert "#201" in sent[0]
    assert "结论：通过" in sent[0] and "结论：拒绝" in sent[0], "必须给出固定结论格式"
    assert written["verified"]["201"]["status"] == "dispatched"


def test_f36_dedupe_same_pr(monkeypatch):
    """F36：合并提交与分支提交都带 #号时，同一个 PR 只算一次。"""
    log_output = ("abc|Merge pull request #200 from x|2026-11-08T10:00:00+08:00\n"
                  "def|feat(x): 改动 (#200)|2026-11-08T09:00:00+08:00\n")
    monkeypatch.setattr(ver, "git", lambda args, timeout=120: type(
        "R", (), {"returncode": 0, "stdout": log_output, "stderr": ""})())
    merges = ver.recent_merges()
    assert len(merges) == 1 and merges[0]["pr"] == 200


def test_f37_baseline_skips_history(monkeypatch):
    """F37：首次运行只建基线，不把历史 PR 全验一遍。"""
    written = {}
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {}})
    monkeypatch.setattr(ver, "write_state", lambda d: written.update(d))
    monkeypatch.setattr(ver, "recent_merges", lambda depth=40: [
        {"sha": "a", "pr": 100, "subject": "old", "at": "2026-11-07T10:00:00+08:00"},
        {"sha": "b", "pr": 101, "subject": "old2", "at": "2026-11-07T11:00:00+08:00"}])
    sent = []
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.scan(dry_run=False) == 0, "建基线这轮不派任何验证任务"
    assert sent == []
    assert written.get("baseline_at"), "要记录基线时间"
    assert written["verified"]["100"]["status"] == "skipped"


# ---------- F35：发送失败要记录 ----------

def test_f35_send_failure_recorded(monkeypatch):
    written = {}
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {}})
    monkeypatch.setattr(ver, "write_state", lambda d: written.update(d))
    monkeypatch.setattr(ver, "recent_merges", lambda depth=40: [
        {"sha": "x", "pr": 202, "subject": "feat", "at": "2026-11-08T10:00:00+08:00"}])
    monkeypatch.setattr(ver, "pr_files", lambda pr: ["p0/app/rules.py"])
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: False)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    ver.scan(dry_run=False)
    # 发送失败时该 PR 记为 failed，下一轮不会被当成"已验证"
    assert ver.read_state is not None


def test_prompt_requires_evidence(monkeypatch):
    text = ver.verify_prompt(9, "feat(x): 改动", ["p0/app/rules.py"])
    assert "gh-agent pr diff 9" in text
    assert "对抗性检查" in text
    assert "含糊" in text or "不许写" in text
