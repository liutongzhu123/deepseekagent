#!/usr/bin/env python3
"""需求覆盖率：从 BACKLOG 反推，不靠人工统计。

口径（刻意保守）：
  done    — 至少一个覆盖状态为 done 的任务（需求完整且有对应验收证据）
  partial — 本地模拟/部分实现；即使开发任务 status=done，也不代表完整需求验收
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
VALID_STATUSES = {"done", "partial", "doing", "waiting", "todo", "blocked"}

# F70: a misspelled covers ID must not disappear from the coverage report.
# F71: an unknown status must not silently become todo or crash during counting.
# F72: duplicate requirement/task IDs must not make the report ambiguous.
# F73: a missing dependency must not leave a task permanently undispatchable.
# F74: a multi-task dependency cycle must not leave every task waiting forever.
# F75: local-simulation task completion must not overstate full requirement readiness.
# F76: duplicate YAML mapping keys must not silently overwrite catalog values.


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys instead of keeping the last."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable mapping key", key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key {key!r}", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_yaml(text: str):
    """Load a YAML catalog with safe types and duplicate-key rejection."""
    return yaml.load(text, Loader=UniqueKeyLoader)


def validate_catalog(requirements: dict, backlog: dict) -> list[str]:
    """Return catalog errors before computing any coverage numbers."""
    errors: list[str] = []
    requirement_ids: set[str] = set()
    for area in requirements["areas"]:
        for module in area["modules"]:
            for item in module["items"]:
                req_id = item["id"]
                if req_id in requirement_ids:
                    errors.append(f"重复需求 ID: {req_id}")
                requirement_ids.add(req_id)

    tasks = backlog.get("tasks") or []
    task_ids: set[str] = set()
    for task in tasks:
        task_id = task["id"]
        if task_id in task_ids:
            errors.append(f"重复任务 ID: {task_id}")
        task_ids.add(task_id)

    for task in tasks:
        task_id = task["id"]
        if task.get("status") not in VALID_STATUSES:
            errors.append(f"{task_id}: 未知状态 {task.get('status')!r}")
        if task.get("coverage_status") is not None and task.get("coverage_status") not in VALID_STATUSES:
            errors.append(f"{task_id}: 未知覆盖状态 {task.get('coverage_status')!r}")
        for req_id in task.get("covers") or []:
            if req_id not in requirement_ids:
                errors.append(f"{task_id}: 未知需求 ID {req_id}")
        for dep_id in task.get("deps") or []:
            if dep_id not in task_ids:
                errors.append(f"{task_id}: 未知依赖任务 {dep_id}")
            elif dep_id == task_id:
                errors.append(f"{task_id}: 不能依赖自身")

    dependencies = {
        task["id"]: [dep for dep in task.get("deps") or [] if dep in task_ids and dep != task["id"]]
        for task in tasks
    }
    visited: set[str] = set()
    path: list[str] = []

    def visit(task_id: str) -> None:
        path.append(task_id)
        for dep_id in dependencies[task_id]:
            if dep_id in path:
                cycle = path[path.index(dep_id):] + [dep_id]
                errors.append(f"任务依赖成环: {' -> '.join(cycle)}")
            elif dep_id not in visited:
                visit(dep_id)
        path.pop()
        visited.add(task_id)

    for task in tasks:
        if task["id"] not in visited:
            visit(task["id"])
    return errors


def load():
    requirements = load_yaml(REQUIREMENTS.read_text(encoding="utf-8"))
    backlog = load_yaml(BACKLOG.read_text(encoding="utf-8"))
    return requirements, backlog


def coverage_map(backlog: dict) -> dict[str, str]:
    """需求条目 id -> done / doing / todo。

    同一需求被多个任务覆盖时取最好的状态（done > doing > todo）。
    """
    rank = {"done": 4, "partial": 3, "doing": 2, "waiting": 1, "todo": 0}
    status: dict[str, str] = {}
    for task in backlog.get("tasks") or []:
        # `status` tracks task delivery, while `coverage_status` tracks requirement
        # readiness. This keeps a finished local simulation from counting as a
        # fully accepted production requirement.
        state = str(task.get("coverage_status") or task.get("status") or "todo")
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
    errors = validate_catalog(requirements, backlog)
    if errors:
        for error in errors:
            print(f"台账校验失败: {error}", file=sys.stderr)
        return 1
    status = coverage_map(backlog)

    totals = {"done": 0, "partial": 0, "doing": 0, "waiting": 0, "todo": 0}
    print(f"{'领域':<18}{'条目':>6}{'完成':>6}{'部分':>6}{'进行中':>7}{'等外部':>7}{'未开始':>7}{'完成率':>8}")
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
