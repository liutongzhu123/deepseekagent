"""R15 chapter and item numbering sequence checks."""
from __future__ import annotations

import re

from ._shared import ReportData, _finding

_SECTION_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})+)[\s、]\s*(\S.*)$")
_SINGLE_RE = re.compile(r"^(\d{1,2})[.、]\s*(\S.*)$")
_PAREN_RE = re.compile(r"^[（(](\d{1,2})[）)]\s*(\S.*)$")
_JUDGMENT_WORDS = ("符合", "不符合", "通过", "不通过", "≤", "≥", "±")


def _heading_ok(text: str) -> bool:
    return (
        bool(re.search(r"[\u4e00-\u9fff]", text))
        and not text.startswith(_JUDGMENT_WORDS)
        and not text[:1].isdigit()
    )


def _numbered_items(data: ReportData):
    sections: list[tuple[list[int], int]] = []
    singles: list[tuple[int, int]] = []
    parens: list[tuple[int, int]] = []
    for index, raw in enumerate(data.paragraphs, start=1):
        text = raw.strip()
        if not text:
            continue
        if len(text) <= 40:
            match = _SECTION_RE.match(text)
            if match and _heading_ok(match.group(2)):
                sections.append(([int(part) for part in match.group(1).split(".")], index))
                continue
        match = _SINGLE_RE.match(text)
        if match and _heading_ok(match.group(2)):
            singles.append((int(match.group(1)), index))
            continue
        match = _PAREN_RE.match(text)
        if match:
            parens.append((int(match.group(1)), index))
    return sections, singles, parens


def _sequence_problems(items: list[tuple[int, int]]) -> list[str]:
    problems: list[str] = []
    for (previous, _), (current, paragraph) in zip(items, items[1:]):
        if current == previous + 1:
            continue
        if current == previous:
            problems.append(f"编号重复：{current}（段落 {paragraph}）")
        elif current > previous + 1:
            problems.append(f"编号断档：{previous}→{current}（段落 {paragraph}）")
    return problems


def r15_numbering(data: ReportData):
    title = "章节/项目编号"
    sections, singles, parens = _numbered_items(data)
    if not sections and not singles and not parens:
        return _finding("R15", title, "skip", "info", "未识别到章节号或项目编号")
    problems: list[str] = []
    by_parent: dict[str, list[tuple[int, int]]] = {}
    for parts, paragraph in sections:
        by_parent.setdefault(".".join(map(str, parts[:-1])), []).append((parts[-1], paragraph))
    for parent, children in by_parent.items():
        seen: set[int] = set()
        for child, paragraph in children:
            if child in seen:
                problems.append(f"章节号重复：{parent}.{child}（段落 {paragraph}）")
            seen.add(child)
        for child in sorted(set(range(1, max(seen) + 1)) - seen)[:3]:
            problems.append(f"章节号断档：缺 {parent}.{child}（段落 {children[0][1]}）")
    for label, items in (("项目", singles), ("括号项目", parens)):
        problems.extend(f"{label}{problem}" for problem in _sequence_problems(items))
    if problems:
        return _finding(
            "R15", title, "fail", "medium",
            "；".join(problems[:6]),
            f"章节号={len(sections)}; 单级编号={len(singles)}; 括号编号={len(parens)}; 问题={len(problems)}",
            "按章节/项目顺序补齐缺号或删除重复编号，并同步目录",
            section="章节/项目编号", cell="编号序列",
        )
    total = len(sections) + len(singles) + len(parens)
    return _finding(
        "R15", title, "pass", "info",
        f"章节/项目编号共 {total} 个，未发现断档或重复",
        section="章节/项目编号", cell="编号序列",
    )
