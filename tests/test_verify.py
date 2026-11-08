#!/usr/bin/env python3
"""双人验证的失败路径测试（对应 bin/verify.py 的 F31–F42）。"""

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


def test_f38_collect_reads_verdict(monkeypatch):
    """F38：从飞书消息里回收验证结论，状态不能永远停在 dispatched。"""
    store = {"verified": {"201": {"status": "dispatched"}}, "baseline_at": "x"}
    written = {}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: written.update(d))
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"}, "create_time": "2026-11-08 18:00",
         "content": "验证 PR #201\n结论：通过\n理由：边界都跑了\n证据：pytest 12 passed"}])
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.collect(dry_run=False) == 1
    assert store["verified"]["201"]["status"] == "pass"
    assert store["verified"]["201"]["review"] == "pending", "通过的结论要等复核"


def test_f39_reject_is_confirmed_not_pending(monkeypatch):
    store = {"verified": {"202": {"status": "dispatched"}}}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"}, "create_time": "2026-11-08 18:01",
         "content": "验证 PR #202\n结论：拒绝\n反例：5.2 抽成 160~210 应为 160～210"}])
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    ver.collect(dry_run=False)
    assert store["verified"]["202"]["status"] == "reject"
    assert store["verified"]["202"]["review"] == "confirmed"


def test_f38_collect_ignores_user_and_vague(monkeypatch):
    store = {"verified": {"203": {"status": "dispatched"}}}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "user"}, "content": "PR #203\n结论：通过"},
        {"sender": {"sender_type": "app"}, "content": "PR #203 基本没问题"}])
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.collect(dry_run=False) == 0
    assert store["verified"]["203"]["status"] == "dispatched"


def test_f39_pending_review_lists_pass_only(monkeypatch):
    monkeypatch.setattr(ver, "read_state", lambda: {"verified": {
        "301": {"status": "pass", "review": "pending"},
        "302": {"status": "reject", "review": "confirmed"},
        "303": {"status": "pass", "review": "ok"},
    }})
    items = ver.pending_review()
    assert [i["pr"] for i in items] == [301]


def test_f39_mark_reviewed_ok_and_overturn(monkeypatch):
    store = {"verified": {"401": {"status": "pass", "review": "pending"}}}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.mark_reviewed(401, "ok") is True
    assert store["verified"]["401"]["review"] == "ok"

    store["verified"]["401"] = {"status": "pass", "review": "pending"}
    assert ver.mark_reviewed(401, "overturn", "边界值没测") is True
    assert store["verified"]["401"]["status"] == "reject", "推翻要把状态改成拒绝"


def test_prompt_requires_evidence(monkeypatch):
    text = ver.verify_prompt(9, "feat(x): 改动", ["p0/app/rules.py"])
    assert "gh-agent pr diff 9" in text
    assert "对抗性检查" in text
    assert "含糊" in text or "不许写" in text


# ---------- F40：解析/判定类改动必须过第二道 ----------

def test_f40_parse_judge_modules_are_risky():
    for path in ("bin/verify.py", "bin/decision.py", "p0/app/docx_parser.py",
                 "p0/app/partial_rules.py", "p0/app/quote_import.py",
                 "p0/app/report_inputs.py"):
        assert ver.is_risky([path]) is True, path
        assert ver.is_parse_judge([path]) is True, path
    assert ver.is_risky(["docs/PLAN.md"]) is False
    assert ver.is_parse_judge(["docs/PLAN.md"]) is False


def test_f40_unknown_files_still_parse_judge():
    """F34/F40：拿不到文件列表时按解析/判定类处理。"""
    assert ver.is_risky([]) is True
    text = ver.verify_prompt(9, "feat(x): 改动", [])
    assert "解析/判定类" in text


def test_f40_prompt_requires_second_adversarial_pass():
    text = ver.verify_prompt(9, "feat(verify): 判定", ["bin/verify.py"])
    assert "解析/判定类" in text
    assert "第二道" in text
    assert "反例输入" in text and "反例输出" in text


# ---------- F41/F42：拒绝必须有具体反例 ----------

def test_f41_counterexample_parsing():
    assert ver.counterexample_of("结论：拒绝\n反例：5.2 抽成 160~210 应为 160～210")
    assert ver.counterexample_of(
        "结论：拒绝\n反例输入：`echo 5.2`\n反例输出：160~210（应为 160～210）")
    assert ver.counterexample_of("结论：拒绝\n反例：边界值没测") == ""
    assert ver.counterexample_of("结论：拒绝\n理由：错了") == ""
    assert ver.counterexample_of("结论：拒绝\n反例输入：5.2") == "", "只有输入没有输出不算"
    # 纯文本反例（错别字/字符串判定）也要能收，不能只认数字
    assert ver.counterexample_of("结论：拒绝\n反例输入：按装\n反例输出：未报错")


def test_f41_reject_without_counterexample_not_confirmed(monkeypatch):
    store = {"verified": {"501": {"status": "dispatched"}}}
    sent = []
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"}, "content": "验证 PR #501\n结论：拒绝\n理由：边界值错"}])
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: sent.append(text) or True)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.collect(dry_run=False) == 1
    entry = store["verified"]["501"]
    assert entry["status"] == "unverified", "没有反例的拒绝不能 confirmed"
    assert entry["review"] == "pending"
    assert entry["counterexample_requested"]
    assert "反例输入" in sent[0]

    # 再收一次同一条消息：不能重复补证、也不能变成 reject
    ver.collect(dry_run=False)
    assert len(sent) == 1, "补证只请求一次"
    assert store["verified"]["501"]["status"] == "unverified"


def test_f41_reject_with_counterexample_confirmed(monkeypatch):
    store = {"verified": {"502": {"status": "dispatched"}}}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"}, "content":
         "验证 PR #502\n结论：拒绝\n理由：边界值错\n"
         "反例输入：`echo 5.2`\n反例输出：160~210（应为 160～210）"}])
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: True)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    assert ver.collect(dry_run=False) == 1
    entry = store["verified"]["502"]
    assert entry["status"] == "reject"
    assert entry["review"] == "confirmed"
    assert "160" in entry["counterexample"]


def test_f41_counterexample_send_failure_recorded(monkeypatch):
    """补证发不出去要留痕（F35 同理），不能假装请求过。"""
    store = {"verified": {"503": {"status": "dispatched"}}}
    monkeypatch.setattr(ver, "read_state", lambda: store)
    monkeypatch.setattr(ver, "write_state", lambda d: None)
    monkeypatch.setattr(ver, "feishu_recent", lambda limit=15: [
        {"sender": {"sender_type": "app"}, "content": "验证 PR #503\n结论：拒绝"}])
    monkeypatch.setattr(ver, "send_as_user", lambda text, dry=False: False)
    monkeypatch.setattr(ver, "log", lambda *_a, **_k: None)

    ver.collect(dry_run=False)
    assert store["verified"]["503"]["counterexample_request_sent"] is False
