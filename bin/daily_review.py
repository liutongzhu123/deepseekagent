#!/usr/bin/env python3
"""每日代码质量巡检（INFRA_REVIEW）：扫当日 diff，报重复代码 / 死代码。

用法：python3 bin/daily_review.py [--repo PATH] [--notify]（每天一次；浅克隆先 unshallow）

口径（保守，宁少报不误报）：当日 diff = 当天 first-parent 提交区间（最早提交的父提交
.. HEAD）的净变更；重复代码 = 当日新增行参与的 ≥5 行连续等价块在 HEAD 全仓出现 ≥2 次；
死代码 = 当日新增的顶层函数/类，name 在 HEAD 全仓只出现 1 次（只有定义。装饰器/__all__/
test_/main 跳过，字符串与注释里的引用也算引用，宁可漏报）。只读：不改仓库、PR、台账。

失败清单（测试见 tests/test_daily_review.py）：F1 当天无提交→正常退出；F2 浅克隆边界
落在窗口内→退出码 2 提示 unshallow；F3 二进制/非源码→跳过；F4 git 失败→退出码 2；
F5 重复窗口→每对文件只报最早一处；F6 装饰器注册/__all__ 导出→不算死代码。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
SUFFIXES = (".py", ".js", ".ts", ".html")
SKIP_PARTS = {".git", "__pycache__", ".venv", "node_modules", "data", "deliverables"}
WINDOW, MIN_SIG, MIN_CHARS, MAX_FINDINGS = 5, 4, 80, 20
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SEND = Path(os.getenv("DAILY_REVIEW_SEND", "/opt/dsh-agent/bin/send-summary"))


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, errors="replace", timeout=300)


def is_code(path: str) -> bool:
    return path.endswith(SUFFIXES) and not (set(path.split("/")) & SKIP_PARTS)


def repo_root() -> Path:
    here = Path(__file__).resolve().parent.parent
    candidates = (os.getenv("DAILY_REVIEW_REPO"), here, "/opt/dsh-agent/clones/deepseekagent")
    return next((Path(c) for c in candidates if c and (Path(c) / ".git").exists()), Path.cwd())


def day_window(repo: Path, day: date) -> dict | None:
    start = datetime.combine(day, time.min, TZ)
    proc = git(repo, "rev-list", "--first-parent", "--since", start.isoformat(),
               "--until", (start + timedelta(days=1)).isoformat(), "HEAD")
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-list 失败：{proc.stderr.strip()}")
    shas = proc.stdout.split()
    if not shas:
        return None
    head, earliest = shas[0], shas[-1]
    base = git(repo, "rev-parse", "--verify", earliest + "^1").stdout.strip()
    if not base:
        if git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            raise RuntimeError(f"浅克隆边界落在当日窗口（{earliest[:7]}），"
                               "无法计算完整 diff；请先 git fetch --unshallow")
        base = EMPTY_TREE  # 当天第一个提交是根提交：用空树做基线
    return {"base": base, "head": head, "commits": len(shas)}


def parse_diff(text: str) -> dict[str, set[int]]:
    """统一 diff（-U0）→ {path: 新增行号集合}。"""
    files, path, line_no = {}, None, None
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
            if path:
                files.setdefault(path, set())
            line_no = None
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            line_no = int(match.group(1)) if match else None
        elif line.startswith("+") and path and line_no is not None:
            files[path].add(line_no)
            line_no += 1
        elif not line.startswith("-") and line_no is not None:
            line_no += 1
    return files


def load_sources(repo: Path, head: str) -> dict[str, list[str]]:
    listing = git(repo, "ls-tree", "-r", "--name-only", head)
    if listing.returncode != 0:
        raise RuntimeError(f"git ls-tree 失败：{listing.stderr.strip()}")
    sources = {}
    for path in filter(is_code, listing.stdout.splitlines()):
        show = git(repo, "show", f"{head}:{path}")
        if show.returncode == 0:
            sources[path] = show.stdout.splitlines()
    return sources


TRIVIAL = {"", "pass", "return", "break", "continue", "}", ")", "]", '"""', "'''",
           "else:", "try:", "finally:"}


def blocks(lines: list[str]):
    normed = [re.sub(r"\s+", " ", line.strip()) for line in lines]
    for index in range(len(normed) - WINDOW + 1):
        block = tuple(normed[index:index + WINDOW])
        meaningful = [line for line in block if line and line not in TRIVIAL
                      and not line.startswith(("#", "//", "/*", "*"))]
        if len(meaningful) < MIN_SIG or sum(map(len, meaningful)) < MIN_CHARS:
            continue
        if all(line.startswith(("import ", "from ")) for line in meaningful):
            continue
        yield index + 1, block


def find_duplicates(sources: dict[str, list[str]], added: dict[str, set[int]]) -> list[dict]:
    index: dict[tuple, set] = {}
    for path, lines in sources.items():
        for start, block in blocks(lines):
            index.setdefault(block, set()).add((path, start))
    best: dict[tuple, dict] = {}
    for block, locations in index.items():
        new = sorted(loc for loc in locations
                     if loc[0] in added and loc[1] in added[loc[0]])
        other = next((loc for loc in sorted(locations) if loc not in new[:1]), None)
        if not new or other is None:
            continue
        key = (new[0][0], other[0])
        if key not in best or new[0][1] < best[key]["line"]:  # F5
            preview = next((line for line in block if line), "")[:80]
            best[key] = {"file": new[0][0], "line": new[0][1], "other_file": other[0],
                         "other_line": other[1], "preview": preview}
    return list(best.values())[:MAX_FINDINGS]


def find_dead(repo: Path, head: str, sources: dict[str, list[str]],
              added: dict[str, set[int]]) -> list[dict]:
    findings = []
    for path, lines in sources.items():
        hits = added.get(path) or set()
        if not hits or not path.endswith(".py"):
            continue
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if (node.lineno not in hits or node.decorator_list or name.startswith(("__", "test_"))
                    or name == "main"):  # F6
                continue
            grep = git(repo, "grep", "-w", "-o", "-I", name, head, "--", "*.py")
            if grep.returncode not in (0, 1):
                raise RuntimeError(f"git grep 失败：{grep.stderr.strip()}")
            if len(grep.stdout.splitlines()) == 1:  # 只有定义那一处
                findings.append({"file": path, "line": node.lineno, "name": name,
                                 "kind": "class" if isinstance(node, ast.ClassDef) else "function"})
    return findings[:MAX_FINDINGS]


def build_report(day: date, result: dict | None) -> str:
    lines = [f"# 每日代码质量巡检 {day.isoformat()}", ""]
    if result is None:
        return "\n".join(lines + ["今天没有 first-parent 提交，无需巡检。"])
    lines.append(f"- 范围：`{result['base'][:7]}..{result['head'][:7]}`，{result['commits']} 个提交，{result['files']} 个文件")
    lines += ["", f"## 重复代码（{len(result['duplicates'])}）", ""]
    lines += [f"- `{f['file']}:{f['line']}` ↔ `{f['other_file']}:{f['other_line']}`"
              f"：`{f['preview']}`" for f in result["duplicates"]] or ["- 未发现"]
    lines += ["", f"## 死代码（{len(result['dead_code'])}）", ""]
    lines += [f"- `{f['file']}:{f['line']}` {f['kind']} `{f['name']}` 全仓无引用"
              for f in result["dead_code"]] or ["- 未发现"]
    return "\n".join(lines)


def review(repo: Path, day: date) -> dict | None:
    info = day_window(repo, day)
    if info is None:
        return None
    diff = git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff",
               "--diff-filter=ACMR", info["base"], info["head"])
    if diff.returncode != 0:
        raise RuntimeError(f"git diff 失败：{diff.stderr.strip()}")
    added = {path: lines for path, lines in parse_diff(diff.stdout).items() if is_code(path)}
    sources = load_sources(repo, info["head"])
    return {**info, "files": len(added),
            "duplicates": find_duplicates(sources, added),
            "dead_code": find_dead(repo, info["head"], sources, added)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日代码质量巡检（重复代码 / 死代码）")
    parser.add_argument("--repo", default=None, help="仓库路径（默认自动探测）")
    parser.add_argument("--notify", action="store_true", help="把结果发到飞书")
    args = parser.parse_args(argv)
    day = datetime.now(TZ).date()
    try:
        result = review(Path(args.repo) if args.repo else repo_root(), day)
    except RuntimeError as exc:  # F2/F4：失败显式退出，不打印假报告
        print(f"巡检失败：{exc}", file=sys.stderr)
        return 2
    report = build_report(day, result)
    print(report)
    if args.notify:
        found = result or {"duplicates": [], "dead_code": []}
        title = (f"每日代码质量巡检 {day.isoformat()}：重复 {len(found['duplicates'])} / "
                 f"死代码 {len(found['dead_code'])}")
        try:
            return subprocess.run([str(SEND), "--title", title, "--body", report],
                                  capture_output=True, timeout=120).returncode
        except OSError as exc:
            print(f"发送失败：{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
