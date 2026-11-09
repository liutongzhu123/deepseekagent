#!/usr/bin/env python3
"""调度器存活检查（F65）：核心组件崩了必须有人知道。

2026-11-09 事故：supervisor 因为一个 UnboundLocalError 每 2 分钟崩一次，
持续 15 分钟——派单、记账、自同步全停，但**没有任何告警**：
  · events.log 停在 12:12，没人看
  · systemd 显示 activating，看着像在跑
  · 只有翻 journalctl 才看到 FAILURE

这个脚本只做一件事：发现"该动的东西没动"就告警。

检查项：
  1. events.log 超过 N 分钟没新行（默认 15 分钟）
  2. 最近 N 分钟内 supervisor 单元有 FAILURE
  3. 关键定时器不是 active

失败清单（tests/test_liveness.py）：
  F1 首次运行没有基线 → 只记基线，不告警
  F2 刚上线/刚重启，日志本来就旧 → 用 state 里的 last_seen 而不是文件 mtime
  F3 同一个问题重复告警 → 一小时内只报一次
  F4 发送失败 → 记录但不要崩
  F5 日志文件不存在 → 当作"从没动过"，告警
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.getenv("LIVENESS_ROOT", "/opt/dsh-agent"))
STATE_DIR = Path(os.getenv("LIVENESS_STATE_DIR", str(ROOT / "state")))
STATE = STATE_DIR / "liveness.json"
EVENTS = STATE_DIR / "events.log"
CHAT_ID = "oc_27a19e7cb8cf10396a5405058264ec85"
TZ = timezone(timedelta(hours=8))

STALE_MINUTES = int(os.getenv("LIVENESS_STALE_MINUTES", "15"))          # F2
COOLDOWN_SECONDS = int(os.getenv("LIVENESS_COOLDOWN_SECONDS", "3600"))  # F3
UNITS = ("dsh-supervisor", "dsh-deploy", "dsh-verify")
TIMERS = ("dsh-supervisor.timer", "dsh-deploy.timer", "dsh-verify.timer")


def now() -> datetime:
    return datetime.now(TZ)


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE)


def log(message: str) -> None:
    line = f"[{now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with (STATE_DIR / "liveness.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def last_event_time() -> datetime | None:
    """events.log 最后一行的时间戳。F5：文件不存在或没时间戳就返回 None。"""
    try:
        lines = EVENTS.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.startswith("["):
            continue
        try:
            return datetime.fromisoformat(line[1:line.index("]")])
        except Exception:
            continue
    return None


def unit_failures(unit: str, minutes: int = 15) -> bool:
    """最近 N 分钟内该 unit 是否 FAILURE。"""
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"-{minutes} min", "--no-pager"],
            capture_output=True, text=True, timeout=30)
        return "FAILURE" in (proc.stdout or "")
    except Exception:
        return False


def timer_inactive(timer: str) -> bool:
    try:
        proc = subprocess.run(["systemctl", "is-active", timer],
                              capture_output=True, text=True, timeout=15)
        return (proc.stdout or "").strip() != "active"
    except Exception:
        return False


def send(markdown: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[dry-run]\n{markdown}")
        return True
    try:
        proc = subprocess.run([str(ROOT / "bin" / "lark-user"), "im", "+messages-send",
                               "--chat-id", CHAT_ID, "--markdown", markdown],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except Exception:                     # F4
        return False


def collect_problems() -> list[str]:
    problems = []
    reference = last_event_time()
    if reference is None:
        problems.append(f"events.log 读不到时间戳（{'文件不存在' if not EVENTS.exists() else '内容异常'}）")
    else:
        age = (now() - reference).total_seconds() / 60
        if age > STALE_MINUTES:
            problems.append(f"events.log 已 {age:.0f} 分钟没有新记录（阈值 {STALE_MINUTES} 分钟）")
    for unit in UNITS:
        if unit_failures(unit):
            problems.append(f"{unit} 最近 15 分钟有 FAILURE")
    for timer in TIMERS:
        if timer_inactive(timer):
            problems.append(f"定时器 {timer} 不是 active")
    return problems


def run(dry_run: bool = False) -> int:
    state = read_state()

    # F1：首次运行只建基线
    if "last_check_at" not in state:
        state["last_check_at"] = now().isoformat(timespec="seconds")
        if not dry_run:
            write_state(state)
        log("建立存活检查基线")
        return 0

    problems = collect_problems()
    state["last_check_at"] = now().isoformat(timespec="seconds")

    if not problems:
        state.pop("alerted_at", None)
        state["last_ok_at"] = now().isoformat(timespec="seconds")
        if not dry_run:
            write_state(state)
        return 0

    # F3：冷却期内不重复告警
    last_alert = state.get("alerted_at")
    if last_alert:
        try:
            elapsed = (now() - datetime.fromisoformat(last_alert)).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                if not dry_run:
                    state["last_problems"] = problems
                    write_state(state)
                log(f"告警冷却中，问题 {len(problems)} 条不重复发")
                return 0
        except Exception:
            pass

    body = "\n".join(f"  · {item}" for item in problems)
    message = ("<at user_id=\"ou_6eb04be620d862edcc21755bc3f96443\"></at> "
               "**【巡检】调度器异常**\n\n"
               f"{body}\n\n"
               "排查：`journalctl -u dsh-supervisor --since '-30 min' | tail -30`")
    if send(message, dry_run):
        if not dry_run:
            state["alerted_at"] = now().isoformat(timespec="seconds")
            state["last_problems"] = problems
            write_state(state)
        log(f"已告警：{len(problems)} 条问题")
    else:
        log("告警发送失败")
        if not dry_run:
            state["last_problems"] = problems
            write_state(state)
    return 0


def status() -> int:
    state = read_state()
    problems = collect_problems()
    print("最近检查：", state.get("last_check_at"))
    print("最近正常：", state.get("last_ok_at"))
    print("告警时间：", state.get("alerted_at"))
    print("当前问题：", problems or "无")
    return 0


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "status":
        return status()
    return run(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
