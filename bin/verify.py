#!/usr/bin/env python3
"""双人验证（INFRA_VERIFY）：风险改动必须过第二道。

为什么需要：这一整天踩的坑（R13 误报、B4 零命中、覆盖率口径、台账没写回）有一个共同点——
**都是"自己说完成"，没人独立复核**。第二轮由**全新会话**做，它没有原任务的上下文，
必须自己重新取证，因此不会继承第一轮的错误假设。

失败清单（代码逐条对应，测试见 tests/test_verify.py）：

  F31 验证任务没跑起来 / 超时 → 记 unverified 并告警，**绝不当作通过**
  F32 同一个 PR 重复验证 → 幂等，已验证过就跳过
  F33 验证结论含糊（既不说通过也不说拒绝）→ 按未通过处理，转人工
  F34 风险改动路径判断错误 → 宁可多验，白名单可配
  F35 验证消息发不出去 → 记 failed，不静默丢
  F36 同一个 PR 因为合并提交和分支提交都带 #号被算两次 → 按 PR 号去重
  F37 首次运行会把历史 PR 全验一遍（几十个任务） → 建立基线，只验基线之后的
  F38 只派验证任务、却不回收结论 → 状态永远是 dispatched，等于没验证
      方案：从飞书消息里回收「结论：通过/拒绝」，写进状态
  F39 agent 判「通过」的结论不能直接信 → 标记为待人工/他模型复核（B 方案）
  F47 从提交标题里抓第一个 #号当 PR 号 → 实际常抓到"被引用的 PR"而不是"合并的 PR"
      （2026-11-08：`chore(backlog): … 验证 #149 …` 被当成 #149 的合并提交）
      → 只认 `Merge pull request #N` 这种权威合并提交
  F46 风险路径写成白名单，agent 新加的模块永远漏检（#167 改 visual_checks.py 被判"非风险"）
      → 按目录判定：p0/app/ 下任何 .py 都算风险
  F43 结论解析过严：只认「结论：拒绝」，而 agent 实际写「验证 #149：拒绝。」
      → 放宽为「冒号后紧跟通过/拒绝」，但仍要求有明确标签，避免把正文里的词当结论

用法：
    python3 bin/verify.py scan           # 找出需要验证的新合并 PR 并派验证任务
    python3 bin/verify.py status         # 看验证状态
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.getenv("VERIFY_ROOT", "/opt/dsh-agent"))
REPO = Path(os.getenv("VERIFY_REPO", str(ROOT / "clones" / "deepseekagent")))
STATE_DIR = Path(os.getenv("VERIFY_STATE_DIR", str(ROOT / "state")))
STATE = STATE_DIR / "verify.json"
CHAT_ID = "oc_27a19e7cb8cf10396a5405058264ec85"
AGENT_AT = '<at user_id="ou_6eb04be620d862edcc21755bc3f96443"></at>'
TZ = timezone(timedelta(hours=8))

# F34：这些路径的改动默认要过第二道（宁可多验）
MAX_PER_RUN = int(os.getenv("VERIFY_MAX_PER_RUN", "2"))

# F46：按**目录**判定风险，不列白名单。
# 白名单的问题是：agent 每加一个新模块（visual_checks/typo_rules/template_rules…），
# 名单就漏一次，漏掉的恰恰是新规则代码——最该被验证的东西。
RISKY_PREFIXES = ("p0/app/",)
RISKY_SUFFIXES = (".py",)


def now() -> datetime:
    return datetime.now(TZ)


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"verified": {}}


def write_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE)


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with (STATE_DIR / "verify.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def git(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO)] + args, capture_output=True, text=True, timeout=timeout)


def send_as_user(markdown: str, dry_run: bool = False) -> bool:
    """F35：发送失败要能被上层看见。"""
    if dry_run:
        print(f"[dry-run] 将发送:\n{markdown}\n")
        return True
    try:
        proc = subprocess.run([str(ROOT / "bin" / "lark-user"), "im", "+messages-send",
                               "--chat-id", CHAT_ID, "--markdown", markdown],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


def recent_merges(depth: int = 40) -> list[dict]:
    """最近合并进 main 的 PR：从合并提交里抽 #号与标题。"""
    try:
        proc = git(["log", "-n", str(depth), "--pretty=%H|%s|%cd", "--date=iso-strict"])
    except Exception:
        return []
    merges = []
    seen: set[int] = set()
    for line in proc.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        sha, subject, when = parts
        # F47：只认权威的合并提交，避免把"提交标题里提到的 PR"当成"合并的 PR"
        match = re.match(r"Merge pull request #(\d+)", subject)
        if not match:
            continue
        pr = int(match.group(1))
        if pr in seen:          # F36
            continue
        seen.add(pr)
        merges.append({"sha": sha, "pr": pr, "subject": subject, "at": when})
    return merges


def pr_files(pr: int) -> list[str]:
    try:
        proc = subprocess.run(["gh-agent", "pr", "view", str(pr), "--repo",
                               "liutongzhu123/deepseekagent", "--json", "files", "--jq", ".files[].path"],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "HOME": "/home/dshadmin"})
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def is_risky(files: list[str]) -> bool:
    """F34/F46：p0/app/ 下的 .py 都算风险；拿不到文件列表时也按风险处理。"""
    if not files:
        return True
    return any(path.startswith(RISKY_PREFIXES) and path.endswith(RISKY_SUFFIXES)
               for path in files)


def verdict_of(text: str) -> str:
    """F33/F43：把验证结论归一化。含糊 = unverified。

    真实消息形如「验证 #149：拒绝。」——所以标签可以是 结论/判定/验证，后面紧跟冒号。
    宁可判 unverified（转人工），也不要把正文里顺口提到的词当成结论。
    """
    text = text or ""
    pattern = r"(?:结论|判定|verdict|验证\s*#?\d{2,4})\s*[:：]\s*(?P<v>拒绝|不通过|reject|通过|pass)"
    match = re.search(pattern, text, re.I)
    if not match:
        return "unverified"
    return "reject" if match.group("v").lower() in ("拒绝", "不通过", "reject") else "pass"


def verify_prompt(pr: int, subject: str, files: list[str]) -> str:
    paths = "、".join(files[:6]) or "（未取到文件列表）"
    return f"""{AGENT_AT} **【代发】验证任务：PR #{pr}**

这是一个**独立的验证任务**，不是继续原来的开发。你此前没有做过这个 PR，请从头取证。

- PR：#{pr}（{subject[:60]}）
- 涉及：{paths}

**要求（按顺序做，不要跳步）**：
1. 用 `gh-agent pr diff {pr}` 看真实改动，不要只看标题
2. **自己设计 3 条对抗性检查**（专挑会出错的地方：边界值、误报、口径不一致）
3. 用真实样例跑，把**实际输出**贴出来（不是"应该没问题"）
4. 明确给结论，格式必须是：

```
结论：通过
理由：<一句话>
证据：<命令 + 关键输出>
```

或

```
结论：拒绝
理由：<一句话>
反例：<能复现的具体输入与输出>
```

**注意**：
- 只有"通过"和"拒绝"两种，**不许写"基本没问题"这类含糊结论**（会被当成未通过）
- 发现真问题就说拒绝，不要为了好看放水
- ≤300 行、自己合；如果判定拒绝，不要改代码，只出验证报告
"""


def feishu_recent(limit: int = 40) -> list[dict]:
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


PR_REF_RE = re.compile(r"#(\d{2,4})")


def collect(dry_run: bool = False) -> int:
    """F38：把 agent 的验证结论从飞书消息里收回来。"""
    state = read_state()
    verified = state.get("verified") or {}
    updated = 0
    for message in feishu_recent(40):
        sender = (message.get("sender") or {}).get("sender_type") or ""
        if sender != "app":
            continue
        text = message.get("content") or ""
        verdict = verdict_of(text)
        if verdict == "unverified":
            continue
        match = PR_REF_RE.search(text)
        if not match:
            continue
        pr = match.group(1)
        entry = verified.get(pr) or {}
        if entry.get("status") in ("pass", "reject"):
            continue
        entry.update({
            "status": verdict,
            "verdict_at": message.get("create_time") or now().isoformat(timespec="seconds"),
            # F39：通过也要人来复核——同模型的盲区是共享的
            "review": "pending" if verdict == "pass" else "confirmed",
            "evidence": text[:400],
        })
        verified[pr] = entry
        updated += 1
        log(f"{'[dry-run] ' if dry_run else ''}回收验证结论 PR #{pr} = {verdict}")
    if updated and not dry_run:
        state["verified"] = verified
        write_state(state)
    return updated


def pending_review() -> list[dict]:
    """F39：等复核的项（agent 判通过、但我还没看过的）。"""
    state = read_state()
    items = []
    for pr, item in (state.get("verified") or {}).items():
        if item.get("status") == "pass" and item.get("review") == "pending":
            items.append({"pr": int(pr), **item})
    return sorted(items, key=lambda x: x.get("verdict_at") or "")


def mark_reviewed(pr: int, verdict: str, note: str = "") -> bool:
    """记录人工/他模型复核结论：ok 或 overturn（推翻）。"""
    if verdict not in ("ok", "overturn"):
        print("复核结论只能是 ok 或 overturn")
        return False
    state = read_state()
    entry = (state.get("verified") or {}).get(str(pr))
    if not entry:
        print(f"没有 PR #{pr} 的验证记录")
        return False
    entry["review"] = verdict
    entry["reviewed_at"] = now().isoformat(timespec="seconds")
    if note:
        entry["review_note"] = note
    if verdict == "overturn":
        entry["status"] = "reject"
    state["verified"][str(pr)] = entry
    write_state(state)
    log(f"复核 PR #{pr}：{verdict}{(' — ' + note) if note else ''}")
    return True


def scan(dry_run: bool) -> int:
    state = read_state()
    verified = state.get("verified") or {}

    # F37：首次运行建立基线——历史 PR 不逐个补验，只验基线之后的
    if not state.get("baseline_at"):
        existing = recent_merges()
        for merge in existing:
            verified.setdefault(str(merge["pr"]),
                                {"status": "skipped", "why": "基线之前的历史 PR",
                                 "at": now().isoformat(timespec="seconds")})
        state["baseline_at"] = now().isoformat(timespec="seconds")
        if not dry_run:
            state["verified"] = verified
            write_state(state)
        log(f"建立验证基线：已登记 {len(existing)} 个历史 PR（不补验）")
        return 0

    dispatched = 0
    for merge in recent_merges():
        if dispatched >= MAX_PER_RUN:                  # 每轮最多派 2 个，避免刷屏
            break
        pr = merge["pr"]
        if str(pr) in verified:
            continue                                   # F32 幂等
        files = pr_files(pr)
        if not is_risky(files):
            verified[str(pr)] = {"status": "skipped", "why": "非风险路径", "at": now().isoformat(timespec="seconds")}
            continue
        message = verify_prompt(pr, merge["subject"], files)
        if send_as_user(message, dry_run):
            if not dry_run:
                verified[str(pr)] = {"status": "dispatched",
                                     "at": now().isoformat(timespec="seconds"),
                                     "files": files[:10]}
            log(f"{'[dry-run] ' if dry_run else ''}派验证任务 PR #{pr}")
            dispatched += 1
        else:
            verified[str(pr)] = {"status": "failed", "at": now().isoformat(timespec="seconds")}   # F35
            log(f"验证任务发送失败 PR #{pr}")
    if dispatched and not dry_run:
        state["verified"] = verified
        write_state(state)
    return dispatched


def status() -> int:
    state = read_state()
    verified = state.get("verified") or {}
    if not verified:
        print("还没有验证记录")
        return 0
    counts: dict[str, int] = {}
    for item in verified.values():
        counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
    print("验证状态统计：", counts)
    for pr, item in sorted(verified.items(), key=lambda kv: kv[1].get("at", ""))[-8:]:
        print(f"  #{pr:>4} {item.get('status'):<10} {item.get('at', '')[:19]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["scan", "status", "collect", "pending", "review"])
    parser.add_argument("--pr", type=int)
    parser.add_argument("--verdict", choices=["ok", "overturn"])
    parser.add_argument("--note", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.command == "scan":
        return scan(args.dry_run)
    if args.command == "collect":
        return collect(args.dry_run)
    if args.command == "pending":
        items = pending_review()
        if not items:
            print("没有待复核项")
        for item in items:
            print(f"  #{item['pr']}  判定通过于 {item.get('verdict_at','')}  证据：{item.get('evidence','')[:80]}")
        return 0
    if args.command == "review":
        if not args.pr or not args.verdict:
            print("用法：verify.py review --pr 123 --verdict ok|overturn --note ...")
            return 2
        return 0 if mark_reviewed(args.pr, args.verdict, args.note) else 1
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
