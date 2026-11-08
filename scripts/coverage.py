#!/usr/bin/env python3
"""需求覆盖率：从 BACKLOG 反推，不靠人工统计。

口径（刻意保守）：
  done    — 至少一个 done 任务覆盖（功能完整、真实验证过）
  partial — 骨架/模拟实现：结构在、接口预留，但没有真实数据或接口验证
  doing   — 只有 doing 任务覆盖（在做，但还没完成）
  waiting — 卡在客户数据/外部依赖上，不是我们能推进的
  todo    — 只有 todo/blocked 任务覆盖，或无人覆盖

用法：
    python3 scripts/coverage.py            # 汇总 + 每个领域的百分比
    python3 scripts/coverage.py --detail   # 连未覆盖条目一起打印
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 pyyaml：pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "docs" / "requirements.yaml"
BACKLOG = ROOT / "docs" / "BACKLOG.yaml"


def load():
    requirements = yaml.safe_load(REQUIREMENTS.read_text(encoding="utf-8"))
    backlog = yaml.safe_load(BACKLOG.read_text(encoding="utf-8"))
    return requirements, backlog


def coverage_map(backlog: dict) -> dict[str, str]:
    """需求条目 id -> done / doing / todo。

    同一需求被多个任务覆盖时取最好的状态（done > doing > todo）。
    """
    rank = {"done": 4, "partial": 3, "doing": 2, "waiting": 1, "todo": 0}
    status: dict[str, str] = {}
    for task in backlog.get("tasks") or []:
        state = str(task.get("status") or "todo")
        # blocked 视为 todo：阻塞不等于在做；waiting 单独统计（等外部依赖）
        if state == "blocked":
            state = "todo"
        for req in task.get("covers") or []:
            current = status.get(req)
            if current is None or rank.get(state, 0) > rank.get(current, 0):
                status[req] = state
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail", action="store_true", help="打印未覆盖条目")
    args = parser.parse_args()

    requirements, backlog = load()
    status = coverage_map(backlog)

    totals = {"done": 0, "partial": 0, "doing": 0, "waiting": 0, "todo": 0}
    print(f"{'领域':<18}{'条目':>6}{'完成':>6}{'骨架':>6}{'进行中':>7}{'等外部':>7}{'未开始':>7}{'完成率':>8}")
    print("-" * 72)

    uncovered: list[tuple[str, str, str]] = []
    for area in requirements["areas"]:
        counts = {"done": 0, "partial": 0, "doing": 0, "waiting": 0, "todo": 0}
        for module in area["modules"]:
            for item in module["items"]:
                state = status.get(item["id"], "todo")
                counts[state] += 1
                if state == "todo":
                    uncovered.append((area["name"], item["id"], item["text"]))
        total = sum(counts.values())
        rate = counts["done"] / total * 100 if total else 0.0
        for key in totals:
            totals[key] += counts[key]
        print(f"{area['name']:<18}{total:>6}{counts['done']:>6}{counts['partial']:>6}{counts['doing']:>7}"
              f"{counts['waiting']:>7}{counts['todo']:>7}{rate:>7.1f}%")

    total = sum(totals.values())
    rate = totals["done"] / total * 100 if total else 0.0
    print("-" * 72)
    print(f"{'合计':<18}{total:>6}{totals['done']:>6}{totals['partial']:>6}{totals['doing']:>7}"
          f"{totals['waiting']:>7}{totals['todo']:>7}{rate:>7.1f}%")

    blocked = [t for t in backlog.get("tasks") or [] if t.get("status") == "blocked"]
    if blocked:
        print("\n阻塞中的任务：")
        for task in blocked:
            print(f"  - {task['id']}: {task.get('title', '')}")

    if args.detail:
        print(f"\n未覆盖条目（{len(uncovered)} 条）：")
        for area, req_id, text in uncovered:
            print(f"  [{area}] {req_id}  {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
