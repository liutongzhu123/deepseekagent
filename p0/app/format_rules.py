"""Deterministic report format/integrity checks (stage A).

Read-only; return ``skip`` with a reason when data is insufficient.
"""
from __future__ import annotations

import re
import unicodedata

from .docx_parser import ReportData, _expand_table

FORMAT_RULES: tuple[tuple[str, str, str], ...] = (
    ("R12", "空白页/窜页/乱码", "页级文本密度+图片覆盖率判空白页；页脚页码断档判窜页；不可打印字符判乱码"),
    ("R13", "书签完整性", "书签开始/结束配对；目录超链接与 REF/PAGEREF 域指向的书签必须存在"),
    ("R14", "空缺项未填写", "表格必填项空值或占位符（___/XX）视为漏填；明确杠掉（/、—、不适用）视为合理留空"),
)

_PAGE_TEXT_MIN = 2
_MOJIBAKE = ("锟斤拷", "ï¿½", "â€", "Ã©", "Ã¤", "Ã¥")
_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}
_REQUIRED_LABELS = {
    "样品名称", "样品编号", "规格型号", "型号规格", "生产企业", "委托单位", "样品数量",
    "生产日期", "抽样日期", "送样日期", "检验依据", "检验项目", "检验结论", "签发日期",
    "检验日期", "试验日期", "设备编号", "设备名称", "溯源有效期", "报告编号",
}
_NA_VALUES = {"/", "／", "—", "——", "-", "－", "–", "无", "不适用", "n/a", "na", "不涉及"}
_TODO_VALUES = {"___", "____", "_____", "______", "…", "...", "待填", "待定", "未填", "□", "☐", "xx", "xxx", "xxxx", "xxxxx"}


def _finding(rule_id, title, status, severity, message, evidence="", suggestion="", page=1, section="全文", cell=""):
    from .rules import Finding  # local import keeps rules/format_rules acyclic

    return Finding(rule_id, title, status, severity, message, evidence, suggestion, {"page": max(1, int(page or 1)), "section": section, "cell": cell})


def _garbled(text: str) -> list[str]:
    return [f"U+{ord(c):04X}" for c in text if c not in "\n\r\t" and (c == "\ufffd" or unicodedata.category(c) in _BAD_CATEGORIES)]


def _page_problems(numbers: list[int]) -> tuple[list[str], int]:
    if not numbers:
        return [], 1
    problems, page, seen, duplicates = [], 1, set(), []
    for number in numbers:
        if number in seen and number not in duplicates:
            duplicates.append(number)
        seen.add(number)
    if duplicates:
        problems.append("页码重复：" + "、".join(map(str, duplicates)))
        page = duplicates[0]
    missing = sorted(set(range(1, max(numbers) + 1)) - seen)
    if missing:
        problems.append("页码断档：缺 " + "、".join(map(str, missing)))
        page = missing[0]
    if numbers != sorted(numbers):
        problems.append("页码乱序：" + "、".join(map(str, numbers)))
        page = 1
    return problems, page


def r12_pages(data: ReportData):
    title = "空白页/窜页/乱码"
    paragraphs = [line for line in data.paragraphs if line and line.strip()]
    if not data.pages and not data.page_numbers:
        return _finding("R12", title, "skip", "info", "未识别到页级内容或页码，无法检查")
    page_problems, page = _page_problems(list(data.page_numbers))
    blanks = [p for p in data.pages if int(p.get("chars") or 0) <= _PAGE_TEXT_MIN and not int(p.get("images") or 0)]
    garbled = _garbled("\n".join(paragraphs))
    mojibake = [m for m in _MOJIBAKE if m in "\n".join(paragraphs)]
    total = max(data.page_totals) if data.page_totals else 0
    problems, severity = [], "medium"
    if blanks:
        problems.append("空白页：" + "、".join(str(p.get("index")) for p in blanks))
        page, severity = int(blanks[0].get("index") or 1), "high"
    if page_problems:
        problems += page_problems
        severity = "high"
    if garbled:
        problems.append(f"乱码/不可打印字符 {len(garbled)} 个（{'、'.join(garbled[:5])}）")
        severity = "high" if len(garbled) >= 3 else "medium"
    if mojibake:
        problems.append("疑似编码乱码：" + "、".join(mojibake))
        severity = "high"
    if problems:
        evidence = f"页级切分={len(data.pages)}; 总页数={total or '未识别'}; 页脚页码={data.page_numbers or '未识别'}; 页级字符数={[p.get('chars', 0) for p in data.pages]}"
        return _finding("R12", title, "fail", severity, "；".join(problems), evidence, "检查分页符/页码域，删除空白页并用源文件重新导出", page=page, section="页面/页码/文本", cell="分页/页码/字符")
    if len(data.pages) <= 1 and total > 1:
        return _finding("R12", title, "skip", "info", f"未识别到分页符（页级切分 {len(data.pages)} 页 / 总页数 {total}）；页脚页码连续，但空白页无法分段判定", suggestion="如需自动判空白页，请保留 Word 分页符或导出 PDF")
    return _finding("R12", title, "pass", "info", f"页级 {len(data.pages)} 页、页码连续、未发现空白页或乱码", f"页级字符数={[p.get('chars', 0) for p in data.pages]}")


def r13_bookmarks(data: ReportData):
    title = "书签完整性"
    starts = [item for item in data.bookmarks if not item["name"].startswith("_GoBack")]
    refs = [item for item in data.bookmark_refs if not item["name"].startswith("_GoBack")]
    if not starts and not refs:
        return _finding("R13", title, "skip", "info", "未识别到书签或书签引用")
    defined = {item["name"] for item in starts}
    end_ids = set(data.bookmark_ends)
    counts: dict[str, int] = {}
    for item in starts:
        counts[item["name"]] = counts.get(item["name"], 0) + 1
    missing = [item for item in refs if item["name"] not in defined]
    unclosed = [item for item in starts if item["id"] not in end_ids]
    duplicates = [name for name, count in counts.items() if count > 1]
    problems: list[str] = []
    targets: list[str] = []
    for item in missing:
        problems.append(f"引用失效：{item['name']}（段落 {item['paragraph']}）")
        targets.append(item["name"])
    for item in unclosed:
        problems.append(f"书签未闭合：{item['name']}（段落 {item['paragraph']}）")
        targets.append(item["name"])
    for name in duplicates:
        problems.append(f"书签重名：{name}")
        targets.append(name)
    if problems:
        return _finding(
            "R13", title, "fail", "high" if missing else "medium", "；".join(problems[:6]),
            f"书签={len(starts)}; 引用={len(refs)}; 未闭合={len(unclosed)}; 失效引用={len(missing)}",
            "删除重复/失效书签，重新插入目录域或超链接；保留书签开始/结束配对",
            section="书签/目录", cell="、".join(dict.fromkeys(targets))[:80],
        )
    return _finding(
        "R13", title, "pass", "info",
        f"书签 {len(starts)} 个、内部引用 {len(refs)} 个，配对且指向有效",
        f"书签名={[item['name'] for item in starts][:8]}",
        section="书签/目录", cell="书签",
    )


def _norm_cell(text: str) -> str:
    return re.sub(r"[\s\*＊]+", "", text or "")


def _cell_issue(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "未填写"
    norm = _norm_cell(raw)
    if norm.lower() in _NA_VALUES:
        return ""
    if norm.lower() in _TODO_VALUES or (len(norm) >= 3 and set(norm) <= {"_"}):
        return "占位符未替换"
    return ""


def r14_empty_cells(data: ReportData):
    title = "空缺项未填写"
    if not data.tables:
        return _finding("R14", title, "skip", "info", "未识别到表格，无法检查空缺项")
    problems: list[str] = []
    targets: list[str] = []
    scanned = 0
    na_count = 0
    for table_index, table in enumerate(data.tables, start=1):
        grid = _expand_table(table)
        for row_index, row in enumerate(grid):
            for column_index, cell in enumerate(row):
                label = _norm_cell(cell)
                if label not in _REQUIRED_LABELS:
                    continue
                scanned += 1
                checks: list[tuple[int, str]] = []
                if row_index == 0 and len(grid) > 1:
                    checks.extend(
                        (rj + 1, grid[rj][column_index])
                        for rj in range(1, len(grid))
                        if column_index < len(grid[rj])
                    )
                elif column_index + 1 < len(row):
                    checks.append((row_index + 1, row[column_index + 1]))
                for row_no, value in checks:
                    raw = (value or "").strip()
                    if raw and _norm_cell(raw).lower() in _NA_VALUES:
                        na_count += 1
                        continue
                    issue = _cell_issue(value)
                    if issue:
                        problems.append(f"表{table_index} 第{row_no}行「{label}」{issue}")
                        targets.append(label)
    if problems:
        return _finding(
            "R14", title, "fail", "medium",
            f"必填项空缺 {len(problems)} 处：" + "；".join(problems[:6]),
            f"必填项={scanned}; 合理留空={na_count}; 问题={len(problems)}",
            "补齐必填项；确属不适用的单元格用 /、— 或“不适用”明确杠掉",
            section="表格必填项", cell="、".join(dict.fromkeys(targets))[:80],
        )
    if scanned == 0:
        return _finding("R14", title, "skip", "info", "未识别到必填项标签或表头，无法检查空缺项")
    return _finding(
        "R14", title, "pass", "info",
        f"扫描 {scanned} 个必填项，均已填写或明确杠掉",
        f"合理留空={na_count}",
        section="表格必填项", cell="必填项",
    )


def evaluate_format_rules(data: ReportData):
    """Run every stage-A format rule; callers apply admin overrides."""
    return [rule(data) for rule in _RULE_FUNCTIONS.values()]


_RULE_FUNCTIONS = {"R12": r12_pages, "R13": r13_bookmarks, "R14": r14_empty_cells}
