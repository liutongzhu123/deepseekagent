#!/usr/bin/env python3
"""决策登记与飞书回复解析（目标模式第 2 步）。

解决的问题：agent 需要人拍板时会停下，现在靠人在飞书里手动回复+重新派活，
中间没人把它接回原任务。这个脚本把"决策"变成一条有 ID、可追踪、可匹配的记录。

用法：
    python3 bin/decision.py open   --ticket 63712f5c --question "低置信限值怎么处理？" \\
                                   --options "A=只展示不进规则,B=标注后进规则,C=先修脚注单位" \\
                                   --default A --hours 12
    python3 bin/decision.py list                 # 所有未答复决策
    python3 bin/decision.py answer DEC-003 C     # 手工登记答复
    python3 bin/decision.py poll                 # 扫飞书群，匹配 "DEC-003 C" 自动登记
    python3 bin/decision.py poll --resume        # 登记后打印应发给 agent 的恢复指令

答复格式（人只需要回这一句）：
    DEC-003 C
只开放一个决策时，也可以只回一个字母；有歧义就不猜，保持 pending 并提示。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "docs" / "decisions.json"
CHAT_ID = "oc_27a19e7cb8cf10396a5405058264ec85"
TZ = timezone(timedelta(hours=8))

DEC_RE = re.compile(r"\bDEC-(\d{3})\b", re.I)
CHOICE_RE = re.compile(r"\b([A-Z])\b")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load() -> dict:
    if not STORE.exists():
        return {"next_seq": 1, "decisions": []}
    return json.loads(STORE.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pending(data: dict) -> list[dict]:
    return [d for d in data["decisions"] if not d.get("answer")]


def cmd_open(args) -> int:
    data = load()
    seq = data.get("next_seq", 1)
    options = {}
    for chunk in args.options.split(","):
        key, _, label = chunk.partition("=")
        if not key.strip() or not label.strip():
            sys.exit(f"选项格式应为 A=说明,B=说明：{chunk!r}")
        options[key.strip().upper()] = label.strip()
    decision = {
        "id": f"DEC-{seq:03d}",
        "ticket": args.ticket,
        "question": args.question,
        "options": options,
        "default_on_timeout": (args.default or "").upper() or None,
        "timeout_hours": args.hours,
        "opened_at": now(),
        "answer": None,
        "answered_at": None,
        "answered_by": None,
        "resumed": False,
    }
    data["decisions"].append(decision)
    data["next_seq"] = seq + 1
    save(data)

    lines = [f"[{decision['id']}] {args.question}", ""]
    for key, label in options.items():
        lines.append(f"{key} {label}")
    if decision["default_on_timeout"]:
        lines.append(f"\n（{args.hours} 小时未答复，默认按 {decision['default_on_timeout']} 处理）")
    print("\n".join(lines))
    return 0


def cmd_list(args) -> int:
    data = load()
    items = pending(data)
    if not items:
        print("没有未答复的决策")
        return 0
    for d in items:
        opts = " / ".join(d["options"])
        print(f"{d['id']}  {d['question']}")
        print(f"    选项: {opts}")
        print(f"    工单: {d['ticket']}  打开于 {d['opened_at']}  默认 {d['default_on_timeout']}")
    return 0


def record(data: dict, decision: dict, choice: str, source: str) -> bool:
    choice = choice.strip().upper()
    if choice not in decision["options"]:
        return False
    decision["answer"] = choice
    decision["answered_at"] = now()
    decision["answered_by"] = source
    return True


def cmd_answer(args) -> int:
    data = load()
    target = next((d for d in data["decisions"] if d["id"].lower() == args.id.lower()), None)
    if not target:
        sys.exit(f"没有这个决策：{args.id}")
    if not record(data, target, args.choice, "cli"):
        sys.exit(f"选项 {args.choice} 不在 {target['options']} 里")
    save(data)
    print(f"{target['id']} = {target['answer']}")
    return 0


def fetch_messages(limit: int = 10) -> list[dict]:
    """读飞书群最近消息（只取文本）。"""
    cmd = [
        str(ROOT / "bin" / "lark-bot-yunan"), "im", "+chat-messages-list",
        "--chat-id", CHAT_ID, "--page-size", str(limit), "--order", "desc",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"读取飞书失败：{proc.stderr[:200]}", file=sys.stderr)
        return []
    raw = proc.stdout
    start = raw.find("{")
    if start < 0:
        return []
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError:
        return []
    return payload.get("data", {}).get("messages", []) or []


def cmd_poll(args) -> int:
    data = load()
    waiting = pending(data)
    if not waiting:
        print("没有未答复的决策")
        return 0

    by_id = {d["id"].upper(): d for d in waiting}
    answered: list[dict] = []
    for message in fetch_messages(args.limit):
        sender = (message.get("sender") or {}).get("sender_type") or ""
        if sender != "user":          # 只看人发的，忽略 bot / agent 自己的消息
            continue
        text = (message.get("content") or "").strip()
        if not text:
            continue

        match = DEC_RE.search(text)
        if match:
            decision = by_id.get(f"DEC-{match.group(1)}")
            if not decision:
                continue
            letters = CHOICE_RE.findall(text.replace(f"DEC-{match.group(1)}", ""))
        else:
            # 只回一个字母：仅在唯一开放决策时接受，避免歧义
            letters = CHOICE_RE.findall(text)
            if len(waiting) != 1 or len(letters) != 1:
                continue
            decision = waiting[0]

        for letter in letters:
            if record(data, decision, letter, "feishu"):
                answered.append(decision)
                break

    if answered:
        save(data)
        print(f"登记 {len(answered)} 条答复：")
        for d in answered:
            print(f"  {d['id']} = {d['answer']}  （工单 {d['ticket']}）")
        if args.resume:
            print("\n恢复指令（发给对应工单）：")
            for d in answered:
                print(f"  [{d['id']}] 决策结果：{d['answer']}（{d['options'][d['answer']]}）。"
                      f"请按此继续原任务，不要重新开始。")
    else:
        print("没有新答复")
        if waiting:
            print("仍在等：" + "、".join(d["id"] for d in waiting))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="决策登记与回复解析")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="登记一个待决策问题")
    p_open.add_argument("--ticket", required=True)
    p_open.add_argument("--question", required=True)
    p_open.add_argument("--options", required=True, help="A=说明,B=说明")
    p_open.add_argument("--default", default=None)
    p_open.add_argument("--hours", type=int, default=12)
    p_open.set_defaults(func=cmd_open)

    p_list = sub.add_parser("list", help="列出未答复决策")
    p_list.set_defaults(func=cmd_list)

    p_answer = sub.add_parser("answer", help="手工登记答复")
    p_answer.add_argument("id")
    p_answer.add_argument("choice")
    p_answer.set_defaults(func=cmd_answer)

    p_poll = sub.add_parser("poll", help="扫飞书群自动登记答复")
    p_poll.add_argument("--limit", type=int, default=10)
    p_poll.add_argument("--resume", action="store_true", help="同时打印恢复指令")
    p_poll.set_defaults(func=cmd_poll)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
