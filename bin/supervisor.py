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
  F11 任务已交付 PR 但还没合并 → 不能重派（2026-11-08 真实发生：B8/B9 各被派了 2 次）
  F12 待合并 PR 越积越多没人管 → 超阈值或等待过久要催办，不能默默换下一个任务
  F13 人对决策的答复没人读 → 每轮扫飞书，把 `DEC-00x` + 选项记进决策表并恢复原任务
  F14 agent 要人拍板时不登记 → 扫 agent 消息里的 `[DEC-00x]` 区块，自动建决策（幂等）
  F15 已合并的 PR 被反复催办 → 以 GitHub 的 PR state 为准，不信可能过期的本地 git log
  F16 催办没有冷却，每 2 分钟刷一次 → `_last_nag_at` 必须持久化（曾被自己的写入覆盖掉）
  F25 reconcile 只改了内存里的台账，没写回文件 → git add 永远 "nothing to commit"
  F26 提交时没配 git 身份 → detached HEAD 上 commit 会因 Author identity unknown 失败
  F27 台账回写被"agent 忙就返回"挡住 → 记账与催办都必须在负载判断之前
  F28 任务标了 doing 却没有工单也没有 PR → 永远不会被自动收尾（INFRA_BACKLOG 卡了一天）
  F29 状态文件只剩元字段（_last_nag_at）没有实际条目 → 无条目时应删掉整个文件
  F41 reconcile 把人工设的 partial/waiting 覆盖回 done（2026-11-08 实测：人工标 partial 5 秒后被改回 done）
      → 自动推断只能推进 todo/doing，不能覆盖人工判断的结果
  F44 验证判「拒绝」后任务仍停在 done（reconcile 只往前推、不往后退）
      → 每轮把「PR 被拒绝且状态为 done」的任务退回 todo，并记返工原因
  F42 reconcile 不看验证结论：PR 合并就标 done，即使双人验证已判「拒绝」
      （2026-11-08 实测：RA_STDVER 被验证拒绝并回退 todo，19:22 又被 reconcile 改回 done）
      → 标 done 前先查该 PR 的验证状态，reject 的保持 todo 并记日志
  F30 漂移检测只覆盖目标机的 p0/，漏掉服务器自己的 bin/ → Supervisor 自身代码也会与 main 分叉
      机制：每轮把 bin/*.py 与 origin/main 比对并自动同步，**但同步前先做语法检查**，
      防止把写坏的文件同步上来把自己跑挂
      （2026-11-08 真实发生：#137/#138 交付后被丢下，agent 直接去做了 B10）

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
AWAITING_STATE = STATE / "awaiting.json"

REDISPATCH_SECONDS = int(os.getenv("DISPATCH_RETRY_SECONDS", "600"))
MAX_ATTEMPTS = int(os.getenv("DISPATCH_MAX_ATTEMPTS", "3"))
COOLDOWN_SECONDS = int(os.getenv("DISPATCH_COOLDOWN_SECONDS", "180"))
NAG_MIN_COUNT = int(os.getenv("NAG_MIN_COUNT", "2"))        # 待合并达到几个就催
NAG_AFTER_SECONDS = int(os.getenv("NAG_AFTER_SECONDS", "1800"))  # 单个等超过多久就催
NAG_COOLDOWN = int(os.getenv("NAG_COOLDOWN", "1800"))      # 两次催办的最小间隔


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


def pr_state(pr: int | None) -> str:
    """PR 的真实状态（F15）。以 GitHub 为准，本地 git log 可能没拉最新。"""
    if not pr:
        return "UNKNOWN"
    try:
        proc = subprocess.run(["gh-agent", "pr", "view", str(pr), "--repo",
                               "liutongzhu123/deepseekagent", "--json", "state", "--jq", ".state"],
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ, "HOME": "/home/dshadmin"})
        return (proc.stdout or "").strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def merge_commit_for(pr: int | None) -> bool:
    """PR 是否已合并：先查 GitHub 状态，查不到再退回本地合并提交。"""
    if not pr:
        return False
    state = pr_state(pr)
    if state in ("MERGED", "OPEN", "CLOSED"):
        return state == "MERGED"
    try:
        proc = git(["log", "-n", "200", "--pretty=%s"])
        return proc.returncode == 0 and any(f"#{pr}" in line for line in proc.stdout.splitlines())
    except Exception:
        return False


# ---------- 业务逻辑 ----------

SELF_FILES = ("supervisor.py", "deploy.py", "decision.py", "budget.py", "heartbeat.py", "verify.py")


def _compiles(source: str) -> bool:
    try:
        compile(source, "<sync>", "exec")
        return True
    except SyntaxError:
        return False


def sync_self(dry_run: bool) -> list[str]:
    """F30：让服务器上的 bin/*.py 与 origin/main 保持一致。

    为什么需要：漂移检测只覆盖目标机的 app/config/static/scripts，而 Supervisor
    自己的代码在服务器的 bin/ 下。手工 scp 会让它和 main 分叉，且没人会发现。
    """
    updated = []
    for name in SELF_FILES:
        try:
            proc = git(["show", f"origin/main:bin/{name}"])
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        if not _compiles(proc.stdout):
            log(f"bin/{name} 在 main 上语法不通过，跳过同步（防止把自己跑挂）")
            continue
        target = BIN / name
        try:
            current = target.read_text(encoding="utf-8")
        except Exception:
            current = ""
        if current == proc.stdout:
            continue
        if dry_run:
            log(f"[dry-run] 将同步 bin/{name} ← main")
        else:
            try:
                target.write_text(proc.stdout, encoding="utf-8")
                os.chmod(target, 0o755)
                log(f"同步 bin/{name} ← main")
            except Exception as exc:
                log(f"同步 bin/{name} 失败：{exc}")
                continue
        updated.append(name)
    return updated


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


VERIFY_STATE = STATE / "verify.json"


def rejected_prs() -> set[int]:
    """F42：已被双人验证判「拒绝」的 PR。它们的任务不能算完成。"""
    data = read_json(VERIFY_STATE, {})
    rejected = set()
    for pr, item in (data.get("verified") or {}).items():
        if (item or {}).get("status") == "reject":
            try:
                rejected.add(int(pr))
            except (TypeError, ValueError):
                continue
    return rejected


def revert_rejected(backlog: dict) -> list[str]:
    """F44：验证拒绝 → 任务从 done 退回 todo。

    reconcile 只会把任务往前推（todo→done），没有回退能力。验证判拒绝之后，
    任务必须能被拉回来，否则返工永远不会发生。
    """
    rejected = rejected_prs()
    if not rejected:
        return []
    reverted = []
    for task in backlog.get("tasks") or []:
        if str(task.get("status")) != "done":
            continue
        pr = task.get("pr")
        if pr is None:
            continue
        try:
            pr = int(pr)
        except (TypeError, ValueError):
            continue
        if pr in rejected:
            task["status"] = "todo"
            task["rejected_pr"] = pr
            reverted.append(str(task.get("id")))
    return reverted


def reconcile(backlog: dict) -> list[str]:
    """F4：工单完成 + PR 已合并 → 标 done。返回被改动的任务 id。

    为什么不只看"提交信息里有没有任务 id"：2026-11-08 实测，agent 会把
    `C1` 缩写成 `C`，grep 匹配不到，任务就永远卡在 doing。
    所以改成两级确认：**工单 ended.outcome=completed** 且 **PR 号出现在 main 的合并提交里**。
    """
    tickets = closed_completed()
    if not tickets:
        return []
    rejected = rejected_prs()
    ids = task_ids(backlog)
    merged: dict[int, bool] = {}
    changed = []
    for task in backlog.get("tasks") or []:
        status = str(task.get("status"))
        # F41：partial/waiting 是人工判断，自动推断不许覆盖
        if status in ("done", "partial", "waiting"):
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
            if pr in rejected:
                log(f"任务 {tid} 的 PR #{pr} 验证未通过，保持 todo 待返工")
                continue
            if merged[pr]:
                task["status"] = "done"
                task["pr"] = pr
                changed.append(tid)
                break
    return changed


def commit_backlog(message: str) -> bool:
    """F26：显式带 git 身份；detached HEAD 上用 HEAD:main 推送。"""
    identity = ["-c", "user.name=supervisor", "-c", "user.email=supervisor@local"]
    try:
        if git(["add", "docs/BACKLOG.yaml"]).returncode != 0:
            return False
        if git(identity + ["commit", "-m", message]).returncode != 0:
            return False
        return git(["push", "origin", "HEAD:main"], timeout=120).returncode == 0
    except Exception:
        return False


def awaiting_merge(ids: set[str]) -> set[str]:
    """F11：已经交出 PR、但还没合并的任务。

    踩过的坑：任务交付 PR 后等待人合并，窗口期超过了重派间隔，Supervisor 以为
    "没在跑"就又派了一次，结果同一个任务被做了两遍（B8/B9 各被派 2 次）。
    所以重派前必须先看"有没有一个还没合并的 PR"。
    """
    waiting: dict[str, int] = {}
    for row in _ticket_rows():
        tid = contains_task(row.get("title") or "", ids)
        if not tid or tid in waiting:
            continue
        for assignment in row.get("assignments") or []:
            summary = (assignment.get("report") or {}).get("summary") or ""
            match = re.search(r"#(\d+)", summary)
            if not match:
                continue
            pr = int(match.group(1))
            if not merge_commit_for(pr):
                waiting[tid] = pr
                break
    return waiting


def pr_created_at(pr: int) -> datetime | None:
    """PR 的真实创建时间：催办里的"已等多久"必须按它算。

    踩过的坑：早先用"第一次观察到待合并"的时间，结果一个开了 1 天的 PR
    显示「已等 0 分钟」，把人误导了。
    """
    try:
        proc = subprocess.run(["gh-agent", "pr", "view", str(pr), "--repo",
                               "liutongzhu123/deepseekagent", "--json", "createdAt", "--jq", ".createdAt"],
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ, "HOME": "/home/dshadmin"})
        raw = proc.stdout.strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        return None


def nag_pending(waiting: dict[str, int], dry_run: bool) -> None:
    """F12：待合并 PR 过多或等太久 → 催办（有冷却，避免刷屏）。"""
    state = read_json(AWAITING_STATE, {})
    now_iso = now().isoformat(timespec="seconds")
    tracked: dict[str, dict] = {}
    for tid, pr in waiting.items():
        previous = state.get(tid) or {}
        tracked[tid] = {"pr": pr, "since": previous.get("since") or now_iso}
    if state.get("_last_nag_at"):          # F16：先继承冷却时间戳，别被下面的写入抹掉
        tracked["_last_nag_at"] = state["_last_nag_at"]
    if not dry_run:
        write_json(AWAITING_STATE, tracked)
    if not tracked:
        if not dry_run:
            try:
                AWAITING_STATE.unlink()      # F29：没有待合并项就别留空壳文件
            except OSError:
                pass
        return

    ages = {}
    for tid, info in tracked.items():
        if tid.startswith("_"):      # 跳过 _last_nag_at 这类元字段
            continue
        created = pr_created_at(info["pr"])
        if created:
            ages[tid] = max(0.0, (now() - created).total_seconds())
            tracked[tid]["since"] = created.isoformat(timespec="seconds")
        else:
            try:
                ages[tid] = (now() - datetime.fromisoformat(info["since"])).total_seconds()
            except Exception:
                ages[tid] = 0

    overdue = {tid: age for tid, age in ages.items() if age >= NAG_AFTER_SECONDS}
    real_count = len([t for t in tracked if not t.startswith("_")])
    if real_count < NAG_MIN_COUNT and not overdue:
        return

    last = state.get("_last_nag_at")
    if last:
        try:
            if (now() - datetime.fromisoformat(last)).total_seconds() < NAG_COOLDOWN:
                return
        except Exception:
            pass

    lines = [f"{AGENT_AT} 【催办】有 {real_count} 个 PR 已交付但还没合并", ""]
    for tid, info in tracked.items():
        if tid.startswith("_"):
            continue
        minutes = int(ages[tid] // 60)
        lines.append(f"- {tid} → PR #{info['pr']}（已等 {minutes} 分钟）")
    lines += ["", "请按自合命令处理，并把 merge 返回和 `pr view --json state` 贴进汇报：",
              "  `gh-agent pr merge <号> --repo liutongzhu123/deepseekagent --merge --delete-branch`"]
    if overdue:
        lines += ["", "其中最久的已超过 " + str(NAG_AFTER_SECONDS // 60) + " 分钟，优先处理。"]

    if send_as_user("\n".join(lines), dry_run):
        if not dry_run:
            tracked["_last_nag_at"] = now_iso
            write_json(AWAITING_STATE, tracked)
        log(f"{'[dry-run] ' if dry_run else ''}催办合并：{', '.join(f'{t}#{p}' for t, p in waiting.items())}")


def set_task_status(path: Path, task_id: str, status: str, pr: int | None = None) -> bool:
    """把某个任务的状态**按行**写回 YAML，保留注释与缩进（F25）。

    不用 yaml.safe_dump：那会把整个文件的注释和格式全部冲掉，diff 无法审阅。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        return False

    target = f"  - id: {task_id}"
    start = next((i for i, line in enumerate(lines) if line.rstrip("\n") == target), None)
    if start is None:
        return False

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("  - id: ") or (lines[i].strip() and not lines[i].startswith(("    ", "\n"))):
            end = i
            break

    changed = False
    for i in range(start, end):
        stripped = lines[i].strip()
        if stripped.startswith("status:"):
            lines[i] = f"    status: {status}\n"
            changed = True
            break
    if not changed:
        lines.insert(start + 1, f"    status: {status}\n")
        end += 1
    if pr is not None:
        if not any(lines[i].strip().startswith("pr:") for i in range(start, end)):
            lines.insert(start + 2, f"    pr: {pr}\n")
    try:
        path.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def stale_doing(backlog: dict, days: float = 1.0) -> list[str]:
    """F28：标着 doing 却既没有工单、也没有 PR 的任务。

    它们不会被 reconcile 收尾（reconcile 只认"工单完成 + PR 合并"），
    于是一直挂在那里。INFRA_BACKLOG 就是这样卡住的。
    """
    ids = task_ids(backlog)
    busy = in_flight(api_tickets(), ids)
    stuck = []
    for task in backlog.get("tasks") or []:
        if str(task.get("status")) != "doing":
            continue
        tid = str(task.get("id"))
        if task.get("pr") or tid in busy:
            continue
        stuck.append(tid)
    return stuck


def next_task(backlog: dict, busy: set[str], dispatched: dict,
              awaiting: set[str] | None = None) -> tuple[dict | None, str]:
    """挑下一个可派任务，同时给出"为什么没得派"的原因。"""
    done_ids = {str(t["id"]) for t in backlog.get("tasks") or [] if str(t.get("status")) == "done"}
    blocked_reasons = []
    for task in backlog.get("tasks") or []:
        tid = str(task.get("id"))
        if str(task.get("status")) != "todo":
            continue
        if tid in busy:
            continue
        if awaiting and tid in awaiting:
            blocked_reasons.append(f"{tid} 等 PR 合并")
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
    lines += [
        "",
        "要求：≤300 行、CI 绿、自己合；超阈值发卡片后**继续做下一件**（§10.3c）。",
        "自合命令（先跑 `gh-agent auth status` 确认已登录，禁止凭印象说不能合并）：",
        "  `gh-agent pr merge <PR号> --repo liutongzhu123/deepseekagent --merge --delete-branch`",
        "合并后汇报必须贴出 merge 命令返回 + `gh-agent pr view <PR号> --json state` 的结果。",
        "做完发三段式汇报。",
    ]
    return "\n".join(lines)


def feishu_recent(limit: int = 10) -> list[dict]:
    """读群最近消息（bot 身份）。F13 用它抓人对决策的答复。"""
    env = {**os.environ, "HOME": "/home/dshadmin",
           "LARKSUITE_CLI_CONFIG_DIR": "/home/dshadmin/.lark-cli-yunan-bot"}
    try:
        proc = subprocess.run(["/opt/dsh-agent/tools/node_modules/.bin/lark-cli",
                               "im", "+chat-messages-list", "--chat-id", CHAT_ID,
                               "--page-size", str(limit), "--order", "desc"],
                              capture_output=True, text=True, timeout=60, env=env)
    except Exception:
        return []
    raw = proc.stdout
    start = raw.find("{")
    if start < 0:
        return []
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError:
        return []
    return ((payload.get("data") or {}).get("messages")) or []


def poll_decisions(dry_run: bool) -> int:
    """F13：扫飞书答复 → 记进决策表。返回新记录的条数。

    只认人发的消息（bot 自己发的跳过）；有歧义不猜：
    - 带 `DEC-00x` 时按 id 对应
    - 只有单个开放决策且只回一个字母时才接受裸回复
    """
    data = read_json(DECISIONS, {"decisions": []})
    pending = [d for d in data.get("decisions") or [] if not d.get("answer")]
    if not pending:
        return 0

    by_id = {d["id"].upper(): d for d in pending}
    recorded = 0
    for message in feishu_recent():
        sender = (message.get("sender") or {}).get("sender_type") or message.get("sender_type") or ""
        if sender != "user":
            continue
        text = (message.get("content") or "").strip()
        if not text:
            continue
        match = re.search(r"DEC-(\d{3})", text, re.I)
        letters = re.findall(r"\b([A-Z])\b", text.replace(match.group(0), "") if match else text)
        if match:
            decision = by_id.get(f"DEC-{match.group(1)}")
            if not decision or not letters:
                continue
        else:
            if len(pending) != 1 or len(letters) != 1:
                continue
            decision = pending[0]
        choice = letters[0].upper()
        if choice not in (decision.get("options") or {}):
            continue
        if dry_run:
            log(f"[dry-run] 将记录 {decision['id']} = {choice}")
        else:
            decision["answer"] = choice
            decision["answered_at"] = now().isoformat(timespec="seconds")
            decision["answered_by"] = "feishu"
            log(f"记录决策答复 {decision['id']} = {choice}")
        recorded += 1
        break
    if recorded and not dry_run:
        write_json(DECISIONS, data)
    return recorded


DEC_BLOCK_RE = re.compile(r"\[DEC-(\d{3})\]\s*(.+)")
OPTION_LINE_RE = re.compile(r"^\s*([A-Z])\s*[.、:：]?\s*(\S.*)$")


def auto_register_decisions(dry_run: bool) -> int:
    """F14：从 agent 的飞书消息里自动建决策。

    约定格式（写进协议）：
        [DEC-001] 低置信限值怎么处理？
        A 只展示不进规则
        B 标注后进规则
        task: B6

    幂等：同 id 已存在就跳过；选项少于 2 个不建（避免误建）。
    """
    data = read_json(DECISIONS, {"decisions": []})
    known = {d["id"] for d in data.get("decisions") or []}
    created = 0
    for message in feishu_recent(15):
        sender = (message.get("sender") or {}).get("sender_type") or ""
        if sender != "app":          # 只认 agent 发的
            continue
        text = message.get("content") or ""
        match = DEC_BLOCK_RE.search(text)
        if not match:
            continue
        dec_id = f"DEC-{match.group(1)}"
        if dec_id in known:
            continue
        lines = text[match.start():].splitlines()
        question = match.group(2).strip()
        options: dict[str, str] = {}
        task = ""
        for line in lines[1:]:
            if line.strip().lower().startswith("task:"):
                task = line.split(":", 1)[1].strip()
                continue
            opt = OPTION_LINE_RE.match(line)
            if opt and opt.group(2).strip():
                # 选项文字允许很短（"是"/"否"这种），只要求非空
                options[opt.group(1)] = opt.group(2).strip()
        if len(options) < 2:
            continue
        data.setdefault("decisions", []).append({
            "id": dec_id, "ticket": task, "question": question, "options": options,
            "default_on_timeout": None, "timeout_hours": 12,
            "opened_at": now().isoformat(timespec="seconds"),
            "answer": None, "answered_at": None, "answered_by": None, "resumed": False,
        })
        known.add(dec_id)
        created += 1
        log(f"{'[dry-run] ' if dry_run else ''}自动登记决策 {dec_id}：{question[:40]}")
    if created and not dry_run:
        write_json(DECISIONS, data)
    return created


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

    auto_register_decisions(dry_run)  # F14：先把 agent 的决策登记进来
    poll_decisions(dry_run)    # F13：再读人的答复
    handle_decisions(dry_run)  # F7：再把答复转成恢复指令

    # F12 必须在"忙就返回"之前：agent 忙着做新任务时，最容易把旧 PR 丢下不管
    known_ids = task_ids(load_yaml(REPO / "docs" / "BACKLOG.yaml") or {"tasks": []})
    if known_ids:
        nag_pending(awaiting_merge(known_ids), dry_run)

    # F27：记账与催办都不该依赖 agent 忙不忙——agent 忙着做新任务时，台账最需要更新
    pulled = pull_repo()
    backlog = load_yaml(REPO / "docs" / "BACKLOG.yaml")
    if not backlog:
        log("台账读取失败，跳过本轮")
        return 0

    stuck = stale_doing(backlog)
    if stuck:
        log(f"任务标着 doing 但没有工单也没有 PR：{', '.join(stuck)}（需要人工确认完成或改回 todo）")

    sync_self(dry_run)                     # F30：自己的代码也跟 main 对齐

    reverted = revert_rejected(backlog)                # F44：先回退被拒绝的
    changed = reconcile(backlog)                       # 改的是内存
    if reverted:
        by_id2 = {str(t.get("id")): t for t in backlog.get("tasks") or []}
        rows = [set_task_status(REPO / "docs" / "BACKLOG.yaml", tid, "todo")
                for tid in reverted]
        if reverted and not dry_run and all(rows):
            commit_backlog(f"chore(backlog): 验证拒绝，回退 {', '.join(reverted)} 为 todo")
        log(f"{'[dry-run] ' if dry_run else ''}验证拒绝回退：{', '.join(reverted)}")
    if changed:
        written = []
        by_id = {str(t.get("id")): t for t in backlog.get("tasks") or []}
        for tid in changed:
            task = by_id.get(tid) or {}
            if set_task_status(REPO / "docs" / "BACKLOG.yaml", tid, "done", task.get("pr")):
                written.append(tid)                    # F25：写回文件
        if dry_run:
            log(f"[dry-run] 将更新台账：{', '.join(changed)}")
        elif written and commit_backlog(f"chore(backlog): 自动标记完成 {', '.join(written)}"):
            log(f"台账自动更新：{', '.join(written)} 标为 done")
        else:
            log(f"台账写回/提交失败：{', '.join(changed)}")

    load = (api_state().get("load") or {})
    if int(load.get("running") or 0) > 0 or int(load.get("queued") or 0) > 0:  # F1
        return 0
    if not pulled:  # F5：台账没拉新就不派单（但上面的记账已经做完）
        log("台账拉取失败，用本地缓存，本轮不派新任务")
        return 0

    ids = task_ids(backlog)
    busy = in_flight(api_tickets(), ids)
    awaiting = awaiting_merge(ids)
    dispatched = read_json(DISPATCHED, {})
    task, reason = next_task(backlog, busy, dispatched, set(awaiting))
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
