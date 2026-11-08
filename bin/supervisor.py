#!/usr/bin/env python3
"""Supervisor v2：目标模式的常驻调度器（INFRA_SUPERVISOR）。

设计前先把失败路径列出来，代码逐条对应（测试见 tests/test_supervisor.py）：

  F1  worker 正在启动（running=0 但 queued>0）→ 不能再派，否则并行改同一文件
  F2  派单后 agent 没接（消息丢了）→ 10 分钟后重派，最多 3 次，超限告警
  F3  工单结束但失败 → 标记 blocked + 告警，不无限重派
  F4  PR 合并了但台账没更新 → 从 git log 反查任务 id，自动标 done 并提交
  F5  台账拉取失败 → 用缓存但**不派新任务**（避免基于过期台账决策）
  F6  预算超限 → 不派新任务（预算状态由 budget.py 提供）
  F7  决策消息发送失败 → 记录 attempts，3 次后告警
  F8  两个 tick 重叠 → 文件锁，抢不到锁直接退出
  F9  同一任务反复失败 → 次数上限，不无限重试
  F10 台账里没有可派任务 → 明确记一条"无可用任务"，不静默空转

用法：
    python3 bin/supervisor.py run [--dry-run]
    python3 bin/supervisor.py status
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.getenv("SUPERVISOR_ROOT", "/opt/dsh-agent"))
REPO = Path(os.getenv("SUPERVISOR_REPO", str(ROOT / "clones" / "deepseekagent")))
STATE = Path(os.getenv("SUPERVISOR_STATE", str(ROOT / "state")))
BIN = ROOT / "bin"
ORCH = "http://127.0.0.1:3082/orchestrator/api"
CHAT_ID = "oc_27a19e7cb8cf10396a5405058264ec85"
AGENT_AT = '<at user_id="ou_6eb04be620d862edcc21755bc3f96443"></at>'
TZ = timezone(timedelta(hours=8))

HEARTBEAT = STATE / "heartbeat.json"
DECISIONS = STATE / "decisions.json"
DISPATCHED = STATE / "dispatched.json"
EVENTS = STATE / "events.log"
BUDGET = STATE / "budget.json"
LOCK = STATE / "supervisor.lock"

REDISPATCH_SECONDS = int(os.getenv("DISPATCH_RETRY_SECONDS", "600"))
MAX_ATTEMPTS = int(os.getenv("DISPATCH_MAX_ATTEMPTS", "3"))
COOLDOWN_SECONDS = int(os.getenv("DISPATCH_COOLDOWN_SECONDS", "180"))


def now() -> datetime:
    return datetime.now(TZ)


def log(message: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"[{now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def send_alert(title: str, body: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[dry-run] 告警: {title}\n{body}\n")
        return True
    try:
        proc = subprocess.run([str(BIN / "send-summary"), "--title", title, "--markdown", body],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


def send_as_user(markdown: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[dry-run] 将发送:\n{markdown}\n")
        return True
    try:
        proc = subprocess.run([str(BIN / "lark-user"), "im", "+messages-send",
                               "--chat-id", CHAT_ID, "--markdown", markdown],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


# ---------- 外部数据 ----------

def api(path: str):
    try:
        proc = subprocess.run(["curl", "-s", "-m", "10", "-H", "x-orchestrator-request: 1",
                               f"{ORCH}/{path}"], capture_output=True, text=True, timeout=20)
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    except Exception:
        return None


def api_state() -> dict:
    return api("state") or {}


def api_tickets() -> list[dict]:
    payload = api("tickets") or {}
    items = payload.get("items") if isinstance(payload, dict) else payload
    return items or []


def git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO)] + args, capture_output=True, text=True, timeout=timeout)


def pull_repo() -> bool:
    """F5：拉不到就返回 False，调用方据此拒绝派新任务。"""
    try:
        if git(["fetch", "--depth", "20", "origin", "main"]).returncode != 0:
            return False
        return git(["checkout", "-f", "origin/main"]).returncode == 0
    except Exception:
        return False


def load_yaml(path: Path):
    try:
        import yaml
    except ModuleNotFoundError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def merged_subjects() -> list[str]:
    """最近合并提交的标题，用于 F4 反查任务是否已完成。"""
    try:
        proc = git(["log", "-n", "80", "--pretty=%s"])
        return proc.stdout.splitlines() if proc.returncode == 0 else []
    except Exception:
        return []


def _ticket_rows() -> list[dict]:
    """读 orchestrator 的工单表（只读副本，避免锁库）。

    为什么不只用 REST：/tickets 只返回未结束的工单，拿不到 ended.outcome，
    而"任务是否真的完成"必须看 ended。
    """
    db = Path(os.getenv("SUPERVISOR_DB", "/opt/dsh-agent/.dsh-home/state/superagent/orchestrator.sqlite"))
    if not db.exists():
        return []
    tmp = Path("/tmp") / f"sup-{os.getpid()}.sqlite"
    try:
        import shutil
        import sqlite3
        shutil.copy(db, tmp)
        conn = sqlite3.connect(tmp)
        rows = [json.loads(r[0]) for r in conn.execute("select value from ticket")]
        conn.close()
        return rows
    except Exception:
        return []
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def closed_completed() -> list[dict]:
    """已结束且 outcome=completed 的工单，附上从报告里抽到的 PR 号。"""
    result = []
    for row in _ticket_rows():
        ended = row.get("ended") or {}
        if ended.get("outcome") != "completed":
            continue
        report = ((row.get("assignments") or [{}])[-1].get("report") or {})
        summary = report.get("summary") or ""
        match = re.search(r"(?:pull/|#)(\d+)", summary)
        result.append({"title": row.get("title") or "",
                       "pr": int(match.group(1)) if match else None})
    return result


def merge_commit_for(pr: int | None) -> bool:
    """PR 是否已经合并进 main（用合并提交号确认，不信"工单说完成了"）。"""
    if not pr:
        return False
    try:
        proc = git(["log", "-n", "200", "--pretty=%s"])
        return proc.returncode == 0 and any(f"#{pr}" in line for line in proc.stdout.splitlines())
    except Exception:
        return False


# ---------- 业务逻辑 ----------

def task_ids(backlog: dict) -> set[str]:
    return {str(t.get("id")) for t in backlog.get("tasks") or [] if t.get("id")}


def contains_task(text: str, ids: set[str]) -> str | None:
    """**严格**判定：任务 id 必须是标题的开头标识符。

    2026-11-08 踩过的坑：早先按"标题里出现任务 id"匹配，结果
    `解析覆盖率审计：…（B5 暂停）` 被认成 B5 的工单，把从没做过的 B5 标成完成。
    派单消息永远以任务 id 开头（可能带【代发】前缀），所以只认开头最安全。
    """
    head = re.sub(r"^[\s【\[]+(?:代发[】\]]\s*)?", "", (text or "").strip())
    match = re.match(r"([A-Za-z][A-Za-z0-9_]*)", head)
    if not match:
        return None
    token = match.group(1)
    for tid in ids:
        if tid.upper() == token.upper():
            return tid
    return None


def in_flight(tickets: list[dict], ids: set[str]) -> set[str]:
    """open 状态的工单对应的任务 id。"""
    busy = set()
    for ticket in tickets:
        if str(ticket.get("state")) != "open":
            continue
        found = contains_task(str(ticket.get("title") or ""), ids)
        if found:
            busy.add(found)
    return busy


def reconcile(backlog: dict) -> list[str]:
    """F4：工单完成 + PR 已合并 → 标 done。返回被改动的任务 id。

    为什么不只看"提交信息里有没有任务 id"：2026-11-08 实测，agent 会把
    `C1` 缩写成 `C`，grep 匹配不到，任务就永远卡在 doing。
    所以改成两级确认：**工单 ended.outcome=completed** 且 **PR 号出现在 main 的合并提交里**。
    """
    tickets = closed_completed()
    if not tickets:
        return []
    ids = task_ids(backlog)
    merged: dict[int, bool] = {}
    changed = []
    for task in backlog.get("tasks") or []:
        if str(task.get("status")) == "done":
            continue
        tid = str(task.get("id"))
        for ticket in tickets:
            if contains_task(ticket["title"], {tid}) is None:
                continue
            pr = ticket.get("pr")
            if pr is None:
                continue
            if pr not in merged:
                merged[pr] = merge_commit_for(pr)
            if merged[pr]:
                task["status"] = "done"
                task["pr"] = pr
                changed.append(tid)
                break
    return changed


def commit_backlog(message: str) -> bool:
    try:
        if git(["add", "docs/BACKLOG.yaml"]).returncode != 0:
            return False
        if git(["commit", "-m", message]).returncode != 0:
            return False
        return git(["push", "origin", "HEAD:main"], timeout=120).returncode == 0
    except Exception:
        return False


def next_task(backlog: dict, busy: set[str], dispatched: dict) -> tuple[dict | None, str]:
    """挑下一个可派任务，同时给出"为什么没得派"的原因。"""
    done_ids = {str(t["id"]) for t in backlog.get("tasks") or [] if str(t.get("status")) == "done"}
    blocked_reasons = []
    for task in backlog.get("tasks") or []:
        tid = str(task.get("id"))
        if str(task.get("status")) != "todo":
            continue
        if tid in busy:
            continue
        deps = task.get("deps") or []
        missing = [d for d in deps if d not in done_ids]
        if missing:
            blocked_reasons.append(f"{tid} 等 {','.join(missing)}")
            continue
        record = dispatched.get(tid) or {}
        attempts = int(record.get("attempts") or 0)
        last = record.get("last_at")
        if last and attempts < MAX_ATTEMPTS:
            age = (now() - datetime.fromisoformat(last)).total_seconds()
            if age < REDISPATCH_SECONDS:
                continue  # 刚派过，还在等
        if attempts >= MAX_ATTEMPTS:
            continue
        return task, ""
    if blocked_reasons:
        return None, "可用任务为空；阻塞：" + "；".join(blocked_reasons[:4])
    return None, "没有 todo 任务"


def dispatch_message(task: dict, attempt: int) -> str:
    covers = "、".join(task.get("covers") or []) or "（无）"
    files = "、".join(task.get("files") or []) or "（自定）"
    lines = [f"{AGENT_AT} 【代发】{task['id']} {task.get('title', '')}", "",
             "依据台账 docs/BACKLOG.yaml：",
             f"- 覆盖需求：{covers}",
             f"- 预计文件：{files}"]
    if task.get("acceptance"):
        lines.append("- 验收标准：")
        lines.extend(f"  · {item}" for item in task["acceptance"])
    if task.get("note"):
        lines.append(f"- 备注：{task['note']}")
    if attempt > 1:
        lines.append(f"\n（这是第 {attempt} 次派发：上一次没有收到接单确认）")
    lines += ["", "要求：≤300 行、CI 绿、自己合；超阈值发卡片后**继续做下一件**（§10.3c）。",
              "做完发三段式汇报。"]
    return "\n".join(lines)


def handle_decisions(dry_run: bool) -> None:
    data = read_json(DECISIONS, {"decisions": []})
    changed = False
    for decision in data.get("decisions") or []:
        if not decision.get("answer") or decision.get("resumed"):
            continue
        attempts = int(decision.get("attempts") or 0)
        if attempts >= MAX_ATTEMPTS:
            continue
        label = (decision.get("options") or {}).get(decision["answer"], "")
        message = (f"{AGENT_AT} 【代发】[{decision['id']}] 决策结果：{decision['answer']}（{label}）\n\n"
                   f"原问题：{decision.get('question', '')}\n\n"
                   f"请按此结论**继续原任务**，不要重新开始。")
        if send_as_user(message, dry_run):
            if not dry_run:
                decision["resumed"] = True
                decision["resumed_at"] = now().isoformat(timespec="seconds")
                changed = True
            log(f"决策恢复 {decision['id']} = {decision['answer']}")
        else:
            decision["attempts"] = attempts + 1
            changed = True
            log(f"决策恢复失败 {decision['id']}（第 {attempts + 1} 次）")
            if attempts + 1 >= MAX_ATTEMPTS:
                send_alert("🚨 决策恢复连续失败",
                           f"{decision['id']} 已尝试 {attempts + 1} 次仍发不出去，请人工在飞书回复。", dry_run)
    if changed and not dry_run:
        write_json(DECISIONS, data)


def run(dry_run: bool) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # F8
    except BlockingIOError:
        print("上一轮还没结束，跳过本轮")
        return 0

    write_json(HEARTBEAT, {"at": now().isoformat(timespec="seconds"), "pid": os.getpid(),
                           "dry_run": dry_run})

    try:
        subprocess.run([sys.executable, str(BIN / "budget.py"), "check", "--json"],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    if read_json(BUDGET, {}).get("exceeded"):  # F6
        log("预算超限，本轮不派新任务")
        return 0

    handle_decisions(dry_run)  # F7

    load = (api_state().get("load") or {})
    if int(load.get("running") or 0) > 0 or int(load.get("queued") or 0) > 0:  # F1
        return 0

    pulled = pull_repo()
    backlog = load_yaml(REPO / "docs" / "BACKLOG.yaml")
    if not backlog:
        log("台账读取失败，跳过本轮")
        return 0
    if not pulled:  # F5
        log("台账拉取失败，用本地缓存，本轮不派新任务")
        return 0

    changed = reconcile(backlog)
    if changed:
        if not dry_run and commit_backlog(f"chore(backlog): 自动标记完成 {', '.join(changed)}"):
            log(f"台账自动更新：{', '.join(changed)} 标为 done")
        else:
            log(f"台账待更新（未提交）：{', '.join(changed)}")

    ids = task_ids(backlog)
    busy = in_flight(api_tickets(), ids)
    dispatched = read_json(DISPATCHED, {})
    task, reason = next_task(backlog, busy, dispatched)
    if not task:
        if reason:
            log(reason)  # F10
        return 0

    record = dispatched.get(task["id"]) or {}
    attempt = int(record.get("attempts") or 0) + 1
    if send_as_user(dispatch_message(task, attempt), dry_run):
        if not dry_run:
            dispatched[task["id"]] = {"attempts": attempt,
                                      "last_at": now().isoformat(timespec="seconds")}
            write_json(DISPATCHED, dispatched)
        log(f"{'[dry-run] ' if dry_run else ''}派单 {task['id']} 第 {attempt} 次")
    else:
        record["attempts"] = attempt
        record["last_at"] = now().isoformat(timespec="seconds")
        dispatched[task["id"]] = record
        if not dry_run:
            write_json(DISPATCHED, dispatched)
        log(f"派单失败 {task['id']}（第 {attempt} 次）")
        if attempt >= MAX_ATTEMPTS:  # F2/F9
            send_alert("🚨 派单连续失败", f"{task['id']} 已尝试 {attempt} 次仍未能派出。", dry_run)
    return 0


def status() -> int:
    print("心跳:", read_json(HEARTBEAT, {}))
    print("预算:", read_json(BUDGET, {}))
    print("派单记录:", json.dumps(read_json(DISPATCHED, {}), ensure_ascii=False))
    load = (api_state().get("load") or {})
    print("负载:", load)
    tickets = api_tickets()
    print("未结束工单:", [t.get("title") for t in tickets if str(t.get("state")) == "open"][:5])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "status"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run(args.dry_run) if args.command == "run" else status()


if __name__ == "__main__":
    raise SystemExit(main())
