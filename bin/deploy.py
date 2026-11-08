#!/usr/bin/env python3
"""部署自动化（INFRA_DEPLOY）：合并进 main 后自动部署到测试环境。

先在文件里列失败清单，代码逐条对应（测试见 tests/test_deploy.py）：

  F17 测试不通过、或测试根本跑不起来 → **不部署**（"跑不起来"绝不等于通过）
  F18 部署后健康检查或测试失败 → 自动回滚到部署前备份
  F19 同一个 commit 重复部署 → 幂等，记录已部署 sha 后直接跳过
  F20 部署中途失败/超时 → 状态记为 failed，下一轮重试（不标记成功）
  F21 目标机不可达 → 记录 unreachable，不改任何状态到"已部署"
  F22 两轮部署重叠 → 文件锁，抢不到锁直接退出
  F23 打包用的是工作区而不是目标 commit → 必须 `git archive <sha>`，否则可能部署旧代码
  F40 目标机残留 macOS 元数据（._*）与 __pycache__ → 解析配置时崩，
      导致部署后测试大面积失败并反复回滚。部署前后都要清理。
  F28 有人绕过部署器手工改目标机 → 代码与 main 分叉，下次部署会把它悄悄覆盖。
      机制：部署后记下目标机代码树的 manifest 指纹；每轮部署前先比对，
      不一致就记 drift 并强制重部署（**不再相信"上次部署成功"**）。
  F24 重启后立刻做健康检查会把"还没起来"当成"部署失败" → 必须重试等待
      （2026-11-08 真实发生：日志显示 16:10:51 重启、16:10:55 返回 200，而检查在 16:10:51 就打了 000）

状态文件：/opt/dsh-agent/state/deploy.json
  {"deployed_sha": "...", "deployed_at": "...", "status": "ok|failed|unreachable", "history": [...]}

用法：
    python3 bin/deploy.py check    # 看要不要部署
    python3 bin/deploy.py run      # 执行（DEPLOY_ENABLED=1 才真的动目标机）
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.getenv("DEPLOY_ROOT", "/opt/dsh-agent"))
REPO = Path(os.getenv("DEPLOY_REPO", str(ROOT / "clones" / "deepseekagent")))
STATE_DIR = Path(os.getenv("DEPLOY_STATE_DIR", str(ROOT / "state")))
STATE = STATE_DIR / "deploy.json"
LOCK = STATE_DIR / "deploy.lock"
LOG = STATE_DIR / "deploy.log"

TARGET_HOST = os.getenv("DEPLOY_HOST", "root@123.57.11.86")
TARGET_DIR = os.getenv("DEPLOY_TARGET_DIR", "/opt/cq-ai-p0")
KEY = os.getenv("DEPLOY_KEY", "/home/dshadmin/.ssh/cq-target.pem")
SERVICE = os.getenv("DEPLOY_SERVICE", "cq-p0-api.service")
HEALTH = os.getenv("DEPLOY_HEALTH", "http://127.0.0.1:8090/health")
ENABLED = os.getenv("DEPLOY_ENABLED", "0") == "1"
TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(TZ)


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


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


def sh(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ssh(command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return sh(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10", "-i", KEY, TARGET_HOST, command], timeout=timeout)


def target_reachable() -> bool:
    """F21：用一次真实命令判断可达，不看缓存。"""
    try:
        return ssh("echo ok", timeout=30).returncode == 0
    except Exception:
        return False


def local_main_sha() -> str | None:
    """main 的最新 sha；拉取失败返回 None（调用方据此不部署）。"""
    try:
        if sh(["git", "-C", str(REPO), "fetch", "--depth", "20", "origin", "main"], 120).returncode != 0:
            return None
        proc = sh(["git", "-C", str(REPO), "rev-parse", "origin/main"], 30)
        return proc.stdout.strip() or None
    except Exception:
        return None


def needs_deploy(state: dict, sha: str | None) -> tuple[bool, str]:
    """F19：同一 sha 不重复部署。"""
    if not sha:
        return False, "拿不到 main 的最新提交，不部署"
    if state.get("deployed_sha") == sha and state.get("status") == "ok":
        return False, f"{sha[:8]} 已部署，跳过"
    return True, f"准备部署 {sha[:8]}"


def clean_junk() -> None:
    """F40：清理目标机上的 macOS 元数据与字节码缓存。

    真实事故：/opt/cq-ai-p0 里累积了 282 个 ._* 文件（来自早期手工从 Mac 部署），
    其中 config/templates/._byd_odor.yaml 让配置加载抛 UnicodeDecodeError，
    177 个测试失败、部署连续回滚。
    """
    cmd = (f"cd {TARGET_DIR} && find . -name '._*' -delete 2>/dev/null; "
           f"find . -name '.DS_Store' -delete 2>/dev/null; "
           f"find . -name '__pycache__' -type d -prune -exec rm -rf {{}} + 2>/dev/null; "
           f"echo cleaned")
    try:
        ssh(cmd, 120)
    except Exception:
        pass


def deploy_payload(sha: str) -> bool:
    """把**指定 commit** 的 p0/ 目录流式送到目标机。

    F23：必须用 `git archive <sha>`，不能直接 tar 工作区——工作区可能停在任何分支上，
    那样会部署错的代码。data/ 是运行时目录、不在版本库里，因此不会被覆盖。
    """
    try:
        cmd = (f"git -C {REPO} archive --format=tar.gz {sha} p0 | "
               f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i {KEY} {TARGET_HOST} "
               f"'tar xzf - -C {TARGET_DIR} --strip-components=1'")
        return sh(["bash", "-c", cmd], 600).returncode == 0
    except Exception:
        return False


def backup_target(tag: str) -> str | None:
    """F18：部署前必备份，返回备份路径。"""
    path = f"/root/backup-cq-p0-{tag}.tgz"
    try:
        proc = ssh(f"tar czf {path} --exclude='cq-ai-p0/data' -C /opt cq-ai-p0 && ls -l {path}", 600)
        return path if proc.returncode == 0 else None
    except Exception:
        return None


def wait_healthy(retries: int = 20, delay: int = 2) -> tuple[bool, str]:
    """F24：重启后要**等**服务起来，不能立刻判死。

    实测：systemd 报 Started 之后还要 1–4 秒 uvicorn 才真正 listen。
    """
    last = ""
    for _ in range(retries):
        try:
            proc = ssh(f"curl -s -m 5 -o /dev/null -w '%{{http_code}}' {HEALTH}", 30)
            last = (proc.stdout or "").strip()
            if last == "200":
                return True, "200"
        except Exception as exc:
            last = f"异常：{exc}"
        time.sleep(delay)
    return False, last or "无响应"


TREE_PATHS = "app config static scripts"


def tree_manifest() -> str | None:
    """目标机 p0 代码树的内容指纹（F28）。

    只算版本库跟踪的四个目录，排除 __pycache__ 与 data/venv。
    指纹不一致 = 有人手工改过，或者部署没真正落地。
    """
    cmd = (f"cd {TARGET_DIR} && find {TREE_PATHS} -type f -not -path '*__pycache__*' "
           f"-not -name '*.pyc' -print0 2>/dev/null | sort -z | xargs -0 sha256sum 2>/dev/null "
           f"| sha256sum | cut -c1-64")
    try:
        proc = ssh(cmd, 120)
        value = (proc.stdout or "").strip()
        return value or None
    except Exception:
        return None


def restart_and_check() -> tuple[bool, str]:
    """重启 + 健康检查（重试等待）+ 测试。任何一步失败都算失败（F17：跑不起来≠通过）。"""
    try:
        if ssh(f"systemctl restart {SERVICE}", 120).returncode != 0:
            return False, "重启命令失败"
        healthy, code = wait_healthy()
        if not healthy:
            return False, f"健康检查返回 {code or '空'}"
        tests = ssh(f"cd {TARGET_DIR} && .venv/bin/python -m pytest -q 2>&1 | grep -E 'passed|failed|error' | tail -1", 600)
        tail = (tests.stdout or "").strip()
        if "failed" in tail.lower() or "error" in tail.lower():
            return False, f"测试未通过：{tail[:120]}"
        if "passed" not in tail:
            return False, f"测试结果无法判定：{tail[:120]}"
        return True, tail
    except Exception as exc:
        return False, f"检查过程异常：{exc}"


def rollback(backup: str) -> bool:
    """F18：回滚后同样要等健康检查，不能因为"还没起来"就报回滚失败。"""
    try:
        proc = ssh(f"tar xzf {backup} -C /opt && systemctl restart {SERVICE}", 600)
        if proc.returncode != 0:
            return False
        healthy, _ = wait_healthy()
        return healthy
    except Exception:
        return False


def run(dry_run: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)   # F22
    except BlockingIOError:
        print("上一轮部署还没结束，跳过")
        return 0

    state = read_state()
    sha = local_main_sha()

    # F28：先确认目标机还是上次部署的那份代码
    if state.get("status") == "ok" and state.get("manifest"):
        current = tree_manifest()
        if current and current != state["manifest"]:
            log(f"检测到部署漂移：目标机 manifest {current[:12]} ≠ 记录 {state['manifest'][:12]}")
            state["drift_detected_at"] = now().isoformat(timespec="seconds")
            state["status"] = "drift"
            write_state(state)

    should, reason = needs_deploy(state, sha)
    if not should:
        print(reason)
        return 0
    if not ENABLED:
        log(f"[未启用] {reason}（DEPLOY_ENABLED=0）")
        return 0
    if dry_run:
        log(f"[dry-run] {reason}")
        return 0

    if not target_reachable():                                    # F21
        state.update({"status": "unreachable", "last_attempt_at": now().isoformat(timespec="seconds")})
        write_state(state)
        log("目标机不可达，记录后退出（不标记已部署）")
        return 0

    tag = now().strftime("%m%d-%H%M")
    backup = backup_target(tag)
    if not backup:                                                # F18
        state.update({"status": "failed", "last_attempt_at": now().isoformat(timespec="seconds")})
        write_state(state)
        log("备份失败，放弃部署（没有回滚点不能动目标机）")
        return 0

    clean_junk()                                                  # F40：先清残留再部署
    if not deploy_payload(sha):                                   # F20
        state.update({"status": "failed", "last_attempt_at": now().isoformat(timespec="seconds"),
                      "backup": backup})
        write_state(state)
        log("同步代码失败，状态记为 failed，下一轮重试")
        return 0

    clean_junk()                                                  # F40：解压后再清一次
    ok, detail = restart_and_check()
    if not ok:
        restored = rollback(backup)
        state.update({"status": "failed", "last_attempt_at": now().isoformat(timespec="seconds"),
                      "backup": backup, "rolled_back": restored, "detail": detail})
        write_state(state)
        log(f"部署后校验失败（{detail}），已{'回滚' if restored else '尝试回滚但失败'}")
        return 0

    history = (state.get("history") or [])[-9:]
    history.append({"sha": sha, "at": now().isoformat(timespec="seconds"), "backup": backup})
    state.update({"deployed_sha": sha, "deployed_at": now().isoformat(timespec="seconds"),
                  "status": "ok", "detail": detail, "backup": backup, "history": history,
                  "manifest": tree_manifest()})
    write_state(state)
    log(f"部署成功 {sha[:8]}：{detail[:80]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "run"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "check":
        state = read_state()
        sha = local_main_sha()
        ok, reason = needs_deploy(state, sha)
        print(f"已部署: {str(state.get('deployed_sha'))[:8] or '-'} | 最新: {str(sha)[:8] or '-'}")
        print(f"{'需要部署' if ok else '无需部署'}：{reason}")
        return 0
    return run(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
