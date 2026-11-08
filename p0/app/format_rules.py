"""Deterministic report format/integrity checks (stage A).

Read-only; return ``skip`` with a reason when data is insufficient.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .docx_parser import ReportData, _expand_table

FORMAT_RULES: tuple[tuple[str, str, str], ...] = (
    ("R12", "空白页/窜页/乱码", "页级文本密度+图片覆盖率判空白页；页脚页码断档判窜页；不可打印字符判乱码"),
    ("R13", "书签完整性", "书签开始/结束配对；目录超链接与 REF/PAGEREF 域指向的书签必须存在"),
    ("R14", "空缺项未填写", "表格必填项空值或占位符（___/XX）视为漏填；明确杠掉（/、—、不适用）视为合理留空"),
    ("R15", "章节/项目编号", "章节号（1、2；5.1、5.2）与项目编号序列检测断档与重复"),
    ("R16", "计量单位规范", "数值后的单位按白名单与大小写规则校验（MPa/kPa/℃/N·m 等）"),
    ("R17", "字体字号一致", "段落级字体/字号统计，找出偏离正文主体的段落并报出字体、字号与段落位置"),
    ("R18", "目录页码一致", "目录条目页码与正文标题所在页码比对，报出不一致的标题与两处页码"),
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
_SECTION_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})+)[\s、]\s*(\S.*)$")
_SINGLE_RE = re.compile(r"^(\d{1,2})[.、]\s*(\S.*)$")
_PAREN_RE = re.compile(r"^[（(](\d{1,2})[）)]\s*(\S.*)$")
_JUDGMENT_WORDS = ("符合", "不符合", "通过", "不通过", "≤", "≥", "±")
_UNIT_TOKEN = re.compile(r"(?<=\d)\s*([A-Za-zµμΩ°℃][A-Za-zµμΩ°℃0-9²³·/]*)")
_UNIT_UNITS = (
    "Pa", "kPa", "MPa", "hPa", "GPa", "℃", "°C", "N", "kN", "N·m", "kN·m",
    "%", "‰", "mm", "cm", "m", "km", "μm", "µm", "nm", "mm²", "m²", "m³", "cm³",
    "L", "mL", "μL", "µL", "mg", "g", "kg", "t", "s", "min", "h", "d", "Hz",
    "kHz", "MHz", "V", "kV", "mV", "A", "mA", "W", "kW", "MW", "Ω", "kΩ",
    "r/min", "rpm", "dB", "dB(A)", "ppm", "mol/L", "mg/L", "mg/m³", "g/m³",
    "μg/m³", "µg/m³", "J", "kJ",
)
_UNIT_CANONICAL: dict[str, str] = {}
for _unit in _UNIT_UNITS:
    _UNIT_CANONICAL.setdefault(_unit.lower(), _unit)


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
                header_row = (
                    row_index == 0
                    and len(grid) > 1
                    and len(row) >= 2
                    and all(not cell.strip() or _norm_cell(cell) in _REQUIRED_LABELS for cell in row)
                )
                if header_row:
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


def _unit_issue(token: str) -> str | None:
    if len(token) == 1 and token not in {"℃", "%", "‰"}:
        return None
    canonical = _UNIT_CANONICAL.get(token.lower())
    return canonical if canonical and token != canonical else None


def r16_units(data: ReportData):
    title = "计量单位规范"
    problems: list[str] = []
    scanned = 0
    for index, line in enumerate(data.paragraphs, start=1):
        for match in _UNIT_TOKEN.finditer(line):
            token = match.group(1)
            scanned += 1
            canonical = _unit_issue(token)
            if canonical:
                problems.append(f"第{index}段「{token}」应为「{canonical}」")
    if problems:
        return _finding(
            "R16", title, "fail", "medium",
            f"单位写法错误 {len(problems)} 处：" + "；".join(problems[:6]),
            f"数值单位={scanned}; 错误={len(problems)}",
            "按白名单统一单位大小写：MPa、kPa、℃、N·m 等",
            section="计量单位", cell="、".join(sorted({p.split("「")[1].split("」")[0] for p in problems}))[:80],
        )
    if scanned == 0:
        return _finding("R16", title, "skip", "info", "未识别到数值后的计量单位")
    return _finding(
        "R16", title, "pass", "info",
        f"扫描 {scanned} 个数值单位，未发现白名单内的大小写错误",
        section="计量单位", cell="单位",
    )


_TOC_LEADER = re.compile(r"(?:…{2,}|\.{4,}|·{4,})")
_NOTE_PREFIXES = ("注：", "注:", "备注：", "备注:", "说明：", "说明:", "注 ")
_COVER_LABELS = (
    "报告编号", "地址", "电话", "传真", "邮编", "邮政编码", "委托单位", "样品名称",
    "生产企业", "检验类别", "试验类型", "检验依据", "检验项目", "检验结论",
    "签发日期", "批准", "审核", "主检", "商标", "规格型号",
)


def _toc_like(text: str) -> bool:
    return bool(_TOC_LEADER.search(text))


def _cover_like(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if _toc_like(text):
        return True
    if re.search(r"\S\s{3,}\S", text) and ("：" in text or ":" in text):
        return True
    if len(compact) <= 30 and any(compact.startswith(label) for label in _COVER_LABELS):
        return True
    return False


def _body_start(styles: list[dict]) -> int:
    for item in styles:
        text = item["text"]
        if item["paragraph"] >= 10 and len(text) >= 40 and not _toc_like(text) and not _cover_like(text) and re.search(r"[。；，]", text):
            return item["paragraph"]
    return 0


def r17_fonts(data: ReportData):
    title = "字体字号一致"
    body_start = _body_start([item for item in data.paragraph_styles if item.get("font") and item.get("size")])
    styles = [
        item for item in data.paragraph_styles
        if item.get("font") and item.get("size") and len(item["text"]) >= 20 and not item.get("bold")
        and not _toc_like(item["text"]) and not _cover_like(item["text"])
        and not item["text"].lstrip().startswith(_NOTE_PREFIXES)
        and not (body_start and item["paragraph"] < body_start)
        and not (_SECTION_RE.match(item["text"]) or _SINGLE_RE.match(item["text"]) or _PAREN_RE.match(item["text"]))
    ]
    if len(styles) < 5:
        return _finding("R17", title, "skip", "info", f"正文样式样本不足（{len(styles)} 段），无法判定主体字体/字号")
    font_counts: dict[str, int] = {}
    size_counts: dict[float, int] = {}
    for item in styles:
        font_counts[item["font"]] = font_counts.get(item["font"], 0) + 1
        size_counts[item["size"]] = size_counts.get(item["size"], 0) + 1
    font_mode = max(font_counts, key=font_counts.get)
    size_mode = max(size_counts, key=size_counts.get)
    deviations = [
        item for item in styles
        if item["font"] != font_mode or abs(item["size"] - size_mode) > 0.5
    ]
    if deviations:
        detail = "；".join(
            f"段落{item['paragraph']}「{item['text'][:12]}」{item['font']} {item['size']:g}pt"
            for item in deviations[:5]
        )
        return _finding(
            "R17", title, "warn", "medium",
            f"{len(deviations)} 段偏离正文主体（{font_mode} {size_mode:g}pt）：{detail}",
            f"正文样本={len(styles)}; 主体={font_mode} {size_mode:g}pt; 偏离={len(deviations)}",
            "统一正文段落字体/字号；标题、表格、强调文字如刻意不同需人工确认",
            section="字体/字号", cell=f"段落 {deviations[0]['paragraph']}",
        )
    return _finding(
        "R17", title, "pass", "info",
        f"正文 {len(styles)} 段字体/字号一致（{font_mode} {size_mode:g}pt）",
        section="字体/字号", cell="正文",
    )


_TOC_PAGE_MARK = re.compile(r"第\s*(\d+)\s*页")
_TOC_TITLE_MAX = 60
_TOC_TRANSLATE = str.maketrans("：．（）", ".:()")


def _toc_norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").translate(_TOC_TRANSLATE).strip(" .:：")


def _toc_page_mark(line: str) -> int | None:
    """Printed page number on a footer/header line; plain "详见第 3 页" is ignored."""
    match = _TOC_PAGE_MARK.search(line or "")
    if not match:
        return None
    compact = re.sub(r"\s+", "", line)
    return int(match.group(1)) if "共" in compact or re.fullmatch(r"第\d+页", compact) else None


def _toc_line_pages(data: ReportData) -> list[list[int | None]]:
    numbers = list(data.page_numbers or [])
    fallback = numbers if len(numbers) == len(data.pages) and all(
        later > earlier for earlier, later in zip(numbers, numbers[1:])
    ) else []
    pages: list[list[int | None]] = []
    for index, page in enumerate(data.pages):
        lines = page.get("lines") or []
        marks = {position: number for position, line in enumerate(lines) if (number := _toc_page_mark(line))}
        if not marks:
            pages.append([fallback[index] if fallback else None] * len(lines))
        elif len(set(marks.values())) == 1:
            pages.append([next(iter(marks.values()))] * len(lines))
        else:  # one rendered page holding several printed pages (real drafts)
            current: int | None = None
            row: list[int | None] = []
            for position in range(len(lines)):
                current = marks.get(position, current)
                row.append(current)
            pages.append(row)
    return pages


def _toc_candidates(data: ReportData) -> list[tuple[str, int | None]]:
    line_pages = _toc_line_pages(data)
    candidates: list[tuple[str, int | None]] = []
    for page_index, page in enumerate(data.pages):
        for position, line in enumerate(page.get("lines") or []):
            text = _toc_norm(line)
            if not text or len(text) > _TOC_TITLE_MAX or _toc_like(line) or _toc_page_mark(line):
                continue
            if "报告编号" in text or text in {"目录", "检验报告"} or text.startswith("----"):
                continue
            candidates.append((text, line_pages[page_index][position]))
    return candidates


def _toc_actual_page(title: str, candidates: list[tuple[str, int | None]]) -> int | None:
    if len(title) < 2:
        return None
    for text, page in candidates:
        if page is not None and text == title:
            return page
    if len(title) >= 4:
        for text, page in candidates:
            if page is not None and (text.startswith(title) or title in text):
                return page
    return None


def r18_toc_pages(data: ReportData):
    # F1 无目录 -> skip；F2 无分页/页码标记 -> skip；F3 标题未定位只降覆盖不误报；
    # F4 同名标题取首个；F5 页码不一致 -> fail 并给标题+两处页码；
    # F6 一页合并多个打印页按页内标记顺序归属；F7 单标记整页归属；
    # F8 全半角空格/冒号差异归一化后比对；F9 目录标题被合并/带引导线时未定位跳过。
    title = "目录页码一致"
    if not data.toc_entries:
        return _finding("R18", title, "skip", "info", "未识别到目录条目，无法比对目录页码")
    candidates = _toc_candidates(data)
    if not candidates or all(page is None for _, page in candidates):
        return _finding("R18", title, "skip", "info", "未识别到可用的正文页码（缺分页或“第 N 页”标记），无法定位目录条目")
    problems: list[str] = []
    missing: list[str] = []
    located = 0
    first_page = 1
    for entry in data.toc_entries:
        entry_title = str(entry.get("title") or "").strip()
        actual = _toc_actual_page(_toc_norm(entry_title), candidates)
        if actual is None:
            missing.append(entry_title)
            continue
        located += 1
        declared = int(entry.get("page") or 0)
        if actual != declared:
            if not problems:
                first_page = actual
            problems.append(f"「{entry_title}」目录第 {declared} 页、正文第 {actual} 页")
    evidence = f"目录={len(data.toc_entries)}; 可定位={located}; 不一致={len(problems)}; 未定位={len(missing)}"
    if problems:
        return _finding(
            "R18", title, "fail", "medium",
            f"目录页码与正文不一致 {len(problems)} 处：" + "；".join(problems[:6]),
            evidence,
            "在 Word 中更新目录域（只更新页码），或按正文实际页码修正目录",
            page=first_page, section="目录/正文页码",
            cell="、".join(item.split("」")[0].lstrip("「") for item in problems[:6])[:80],
        )
    if not located:
        return _finding("R18", title, "skip", "info", f"目录 {len(data.toc_entries)} 条在正文中均未定位到对应标题，无法比对目录页码")
    return _finding(
        "R18", title, "pass", "info",
        f"目录 {len(data.toc_entries)} 条中 {located} 条可定位，页码与正文一致",
        evidence, section="目录/正文页码", cell="页码",
    )


# --- R22: 检测依据标准年代号 ------------------------------------------------
# Failure branches (each covered in p0/tests/test_format_standard_years.py):
# F1 检验依据为空 -> skip, never pass.
# F2 检验依据里没有标准号 -> skip (nothing to judge), never fail.
# F3 标准号带具体年代号 -> pass.
# F4 标准号缺年代号 -> fail，逐条列出缺失的标准号。
# F5 年代号位数不是 4 位（ASTM 允许 2 位）-> fail 格式不合法。
# F6 年代号不在 1900–2099 -> fail 格式不合法。
# F7 标准号前缀后直接跟字母（如 GBT 1037）-> fail 格式不合法。
# F8 多个标准号混合（合法/缺年代号/格式错）-> 只报问题项，计数准确。
# F9 同一标准号重复出现 -> 只报一次。
# F10 只检查检验依据字段（含“检验依据：”行与标题下一行回退），不扫正文其他引用；F11 名称/方案号/限值数字不误识别为标准号。
# F12 企业标准 Q/ 组织代号后隔空格、编号以字母开头（Q/JLY J7110109C）-> 识别并判缺年代号，不能 skip。
# F13 冒号只在紧跟数字时并进标准号（2021:2015 保留；2021：说明文字 或 结尾冒号 断掉），
#     否则合法标准号后接冒号会被误判格式不合法。
_BASIS_LABELS = ("检验依据", "检测依据")
_STD_DASHES = str.maketrans({
    "–": "-", "—": "-", "−": "-", "－": "-", "―": "-",
    "‐": "-", "‑": "-", "‒": "-", "⁃": "-", "﹘": "-",
    "⸺": "-", "⸻": "-",
})
_STD_TOKEN_TAIL = r"(?:[A-Za-z0-9.\-]|:(?=\d))*"
_STD_QT_RE = re.compile(
    r"(?<![A-Za-z0-9/])(?P<kind>Q|T)/[A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*"
    r"(?:[ \t]*(?:[A-Z]+)?\d" + _STD_TOKEN_TAIL + r")?"
)
_STD_GENERAL_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<prefix>GB/T|GB/Z|GBJ|GBZ|GJB|GB|DB\d{2}(?:/[TZ])?"
    r"|[A-Z]{2,3}/[TZ]"
    r"|(?:ISO|IEC|EN|DIN|JIS|ASTM|SAE|ANSI|UL|CSA|FMVSS|ECE|UN)(?![A-Za-z]))"
    r"[ \t]*(?P<body>[A-Za-z]{0,3}[ \t]?\d" + _STD_TOKEN_TAIL + r")"
)


def _norm_standard_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(_STD_DASHES)


def _basis_text(data: ReportData) -> str:
    basis = (data.basis or "").strip()
    if basis:
        return basis
    paragraphs = [line.strip() for line in data.paragraphs if line and line.strip()]
    for index, line in enumerate(paragraphs):
        for label in _BASIS_LABELS:
            match = re.match(rf"^{label}\s*[:：]\s*(\S.*)$", line)
            if match:
                return match.group(1).strip()
        if line in _BASIS_LABELS and index + 1 < len(paragraphs):
            return paragraphs[index + 1]
    return ""


def _standard_tokens(text: str) -> list[tuple[str, str, str]]:
    """Return (token, prefix, body) for standard numbers found in ``text``."""
    normalised = _norm_standard_text(text)
    found: list[tuple[int, str, str, str]] = []
    for match in _STD_QT_RE.finditer(normalised):
        token = match.group(0).strip()
        prefix = match.group("kind") + "/"
        found.append((match.start(), token, prefix, token[2:]))
    for match in _STD_GENERAL_RE.finditer(normalised):
        token = match.group(0).strip()
        found.append((match.start(), token, match.group("prefix"), match.group("body")))
    found.sort(key=lambda item: item[0])
    tokens: list[tuple[str, str, str]] = []
    last_end = -1
    for start, token, prefix, body in found:
        if start < last_end or not re.search(r"\d", token):
            continue
        tokens.append((token, prefix, body))
        last_end = start + len(token)
    return tokens


def _is_chinese_standard(prefix: str) -> bool:
    return prefix.startswith(("GB", "GJB", "DB")) or bool(re.fullmatch(r"[A-Z]{2,3}/[TZ]", prefix))


def _standard_issue(prefix: str, body: str) -> str:
    """Return an empty string for a legal token, otherwise the problem text."""
    body = (body or "").strip()
    if (prefix in {"ISO", "IEC", "EN", "DIN"} or _is_chinese_standard(prefix)) and body[:1].isalpha():
        return "标准号格式不合法（前缀后应为数字）"
    if prefix == "ASTM":
        body = re.sub(r"e\d+$", "", body)
    if ":" in body:
        year_part = body.rsplit(":", 1)[1]
        if re.fullmatch(r"\d{4}", year_part):
            if 1900 <= int(year_part) <= 2099:
                return ""
            return f"年代号 {year_part} 超出 1900–2099"
        if re.fullmatch(r"\d+", year_part):
            return "年代号应为 4 位数字"
        return "标准号格式不合法（年代号应为数字）"
    match = re.search(r"-(\d{4})$", body)
    if match:
        year = int(match.group(1))
        if 1900 <= year <= 2099:
            return ""
        return f"年代号 {year} 超出 1900–2099"
    if prefix == "ASTM" and re.search(r"-\d{2}$", body):
        return ""
    last = body.rsplit("-", 1)[-1]
    if _is_chinese_standard(prefix) and last.isdigit() and len(last) != 4:
        return "年代号应为 4 位数字"
    return "缺年代号"


def r22_standard_years(data: ReportData):
    title = "检测依据标准年代号"
    basis = _basis_text(data)
    if not basis:
        return _finding("R22", title, "skip", "info", "未识别到检验依据（检测依据）字段，无法检查标准年代号")
    tokens = _standard_tokens(basis)
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for item in tokens:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)
    if not unique:
        return _finding(
            "R22", title, "skip", "info",
            f"检验依据中未识别到标准号，无法检查年代号：{basis[:60]}",
            f"检验依据={basis[:80]}",
            section="检验依据", cell="标准号",
        )
    missing: list[str] = []
    invalid: list[str] = []
    for token, prefix, body in unique:
        issue = _standard_issue(prefix, body)
        if issue == "缺年代号":
            missing.append(token)
        elif issue:
            invalid.append(f"{token} {issue}")
    if missing or invalid:
        details = [f"{token} 缺年代号" for token in missing] + invalid
        return _finding(
            "R22", title, "fail", "medium",
            f"检测依据标准问题 {len(details)} 处：" + "；".join(details[:6]),
            f"检验依据={basis[:80]}; 标准号={len(unique)}; 缺年代号={len(missing)}; 格式不合法={len(invalid)}",
            "在标准号后补具体年代号（如 GB/T 1037-2021）；年代号用 4 位数字、以“-”连接",
            section="检验依据",
            cell="、".join((missing + invalid))[:80],
        )
    return _finding(
        "R22", title, "pass", "info",
        f"检测依据含 {len(unique)} 个标准号，均带具体年代号且格式合法",
        "标准号=" + "、".join(token for token, _, _ in unique[:10]),
        section="检验依据", cell="标准号",
    )


# --- R24: 样品编号连续性与重复性 -------------------------------------------
# Failure branches (each covered in p0/tests/test_format_sample_numbers.py):
# F1 没有任何可判定编号 -> skip，不冒充 pass。
# F2 只有 1 个编号 -> skip（无断号/重复可判）。
# F3 同前缀 01、02、04 -> fail，报“编号断档 缺 03”。
# F4 同一清单内同一编号出现多次 -> fail，报“编号重复”。
# F5 检验项目表里同一编号被多个项目复用 -> 不算重复。
# F6 不同前缀（TS…/QA…）分组判定，不互相制造断档。
# F7 范围写法 01～03 -> 展开为 01、02、03。
# F8 样品数量可解析且与编号个数不一致 -> fail；XX/空/非数字 -> 不判数量。
# F9 不同表/字段各自独立判重，跨表引用不报重复。
# F10 纯数字长串（日期/流水号）不当编号序列，避免误报。
# F11 全角/长横/连字符写法：NFKC 只把「－」折成 ASCII "-"，「—/–/−」与 Word 常见的
#     U+2010/U+2011 等仍是自身；先 NFKC、再统一折成 ASCII "-"，范围才可识别，同一
#     编号的不同横线写法也不会被当成两个（TS24LXJ8A-01－03 展开 3 个；…-01、…‐01 判重）。
# F12 「－」同时可能是前缀分隔符（TS24LXJ8A－01）与范围符（01－03）。折成 "-" 后
#     只在“左侧已是完整编号”时展开：左侧是纯数字，或左侧已含前缀分隔符（如
#     TS24LXJ8A-01）；否则按前缀分隔符处理。这样 TS24－27 仍是 1 个编号
#     （前缀 TS24-、序号 27），不会被误展开成 24..27。
# F13 范围展开上限 9999，与样品数量可解析上限一致：01～52 这类超过 50 个的正常
#     批量编号也要展开；只有超长/倒序范围不展开，避免异常输入产生巨量 token。
_SAMPLE_LINE = re.compile(r"^\s*[*＊]?\s*样品编号\s*[:：]\s*(.*)$")
_SAMPLE_SPLIT = re.compile(r"[、,，;；\s]+")
_SAMPLE_RANGE = re.compile(r"^(.+?)\s*(?:~|至)\s*(.+)$")
_SAMPLE_RANGE_MAX = 9999
_SAMPLE_DASHES = str.maketrans({
    "－": "-", "—": "-", "–": "-", "−": "-", "―": "-", "‒": "-",
    "‐": "-", "‑": "-", "⸺": "-", "⸻": "-", "⁃": "-", "﹣": "-",
    "～": "~", "〜": "~",
})
_SAMPLE_NUMBER = re.compile(r"^(?P<prefix>.*[^0-9])?(?P<num>\d+)$")
_SAMPLE_QTY = re.compile(r"^\s*(\d+)\s*(?:件|个|份|支|块|只|套|根|片|颗|瓶|袋|组)?\s*$")
_SAMPLE_ITEM_LABELS = {"检验项目", "试验项目", "检测项目", "测试项目"}


def _sample_label(text: str) -> str:
    return re.sub(r"^[*\s]+|[\s:：]+$", "", unicodedata.normalize("NFKC", text or "").strip())


def _sample_parts(token: str) -> tuple[str, str] | None:
    match = _SAMPLE_NUMBER.match(token)
    if not match or len(token) > 40:
        return None
    prefix, number = match.group("prefix") or "", match.group("num")
    if prefix and not re.search(r"[A-Za-z]", prefix):
        return None
    if not prefix and len(number) > 4:  # F10: 日期/流水号不当作编号序列
        return None
    return prefix, number


def _expand_range_pair(left_text: str, right_text: str) -> list[str] | None:
    left, right = _sample_parts(left_text.strip()), _sample_parts(right_text.strip())
    if not left or not right or (right[0] and right[0] != left[0]):
        return None
    start, end = int(left[1]), int(right[1])
    if not 0 <= end - start <= _SAMPLE_RANGE_MAX:  # F13
        return None
    return [f"{left[0]}{str(value).zfill(len(left[1]))}" for value in range(start, end + 1)]


def _expand_sample_range(part: str) -> list[str] | None:
    match = _SAMPLE_RANGE.match(part)
    if match:  # 显式范围符 ～/~/至
        return _expand_range_pair(match.group(1), match.group(2))
    # F12: "-" 只有落在已成型编号之后才是范围符，否则是编号前缀分隔符。
    for index in range(len(part) - 1, 0, -1):
        if part[index] != "-":
            continue
        left = part[:index].strip()
        if not (left.isdigit() or "-" in left):
            continue
        expanded = _expand_range_pair(left, part[index + 1:])
        if expanded:
            return expanded
    return None


def _sample_norm(text: str) -> str:
    """先 NFKC 再统一横线：U+FE58 等要先归一成「—」才能被映射兜住。"""
    return unicodedata.normalize("NFKC", text or "").translate(_SAMPLE_DASHES)


def _sample_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for part in _SAMPLE_SPLIT.split(_sample_norm(text)):
        part = part.strip()
        if not part:
            continue
        expanded = _expand_sample_range(part)
        if expanded:
            tokens.extend(expanded)
        elif _sample_parts(part):
            tokens.append(part)
    return tokens


def _sample_scopes(data: ReportData) -> list[tuple[str, list[str], bool]]:
    scopes: list[tuple[str, list[str], bool]] = []
    if (data.sample_no or "").strip():
        scopes.append(("样品编号字段", [data.sample_no], False))
    for number, table in enumerate(data.tables, start=1):
        for row_index, row in enumerate(table):
            column = next((index for index, cell in enumerate(row) if _sample_label(cell.text) == "样品编号"), None)
            if column is None:
                continue
            value = row[column + 1].text if column + 1 < len(row) else ""
            if _sample_tokens(value):
                scopes.append((f"表{number}样品编号", [value], False))
            else:
                reuse = any(_sample_label(cell.text) in _SAMPLE_ITEM_LABELS for cell in row)
                below = [below[column].text for below in table[row_index + 1:] if column < len(below)]
                if any(cell.strip() for cell in below):
                    scopes.append((f"表{number}样品编号列", below, reuse))
            break
    for line in data.paragraphs:
        match = _SAMPLE_LINE.match(line)
        value = match.group(1).strip() if match else ""
        if value and _sample_tokens(value):
            scopes.append(("正文样品编号", [value], False))
    return scopes


def _sample_issues(scopes: list[tuple[str, list[str], bool]]):
    issues: list[str] = []
    all_tokens: set[str] = set()
    groups: set[str] = set()
    occurrences = 0
    for source, values, reuse in scopes:
        cells = [_sample_tokens(value) for value in values]
        tokens = [token for cell in cells for token in cell]
        if not tokens:
            continue
        occurrences += len(tokens)
        all_tokens.update(tokens)
        if reuse:  # F5: 检验项目表跨行复用同一编号是正常的，只查单元格内重复
            counts = Counter(token for cell in cells for token in cell if cell.count(token) > 1)
        else:
            counts = Counter(tokens)
        for token, count in counts.items():
            if count > 1:
                issues.append(f"编号重复：{token}（{source}，{count} 次）")
        by_prefix: dict[str, list[str]] = {}
        for token in tokens:
            parsed = _sample_parts(token)
            if parsed:
                by_prefix.setdefault(parsed[0], []).append(parsed[1])
        for prefix, numbers in by_prefix.items():
            groups.add(prefix)
            unique = sorted({int(number) for number in numbers})
            if len(unique) < 2:
                continue
            missing = [value for value in range(unique[0], unique[-1] + 1) if value not in unique]
            if missing:
                width = max(len(number) for number in numbers)
                labels = [f"{prefix}{str(value).zfill(width)}" for value in missing[:5]]
                issues.append(f"编号断档：{prefix or '无前缀'} 缺 {'、'.join(labels)}")
    return list(dict.fromkeys(issues)), all_tokens, groups, occurrences


def _sample_quantity(text: str) -> int | None:
    match = _SAMPLE_QTY.match(unicodedata.normalize("NFKC", text or ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 9999 else None


def r24_sample_numbers(data: ReportData):
    title = "样品编号连续性与重复性"
    issues, all_tokens, groups, occurrences = _sample_issues(_sample_scopes(data))
    if not all_tokens:
        return _finding("R24", title, "skip", "info", "未识别到可判定的样品编号，无法检查断号或重复", section="样品信息", cell="样品编号")
    quantity = _sample_quantity(data.sample_qty)
    evidence = f"样品编号={len(all_tokens)}; 编号出现={occurrences}; 前缀组={len(groups)}"
    if quantity is not None:
        evidence += f"; 样品数量={quantity}"
        if quantity != len(all_tokens):
            issues.append(f"样品数量 {quantity} 与样品编号 {len(all_tokens)} 个不对应")
    if issues:
        return _finding("R24", title, "fail", "medium", f"样品编号问题 {len(issues)} 处：" + "；".join(issues[:6]), evidence, "按样品编号顺序补齐缺号、删除重复；核对样品数量与编号个数", section="样品信息", cell="样品编号/样品数量")
    if len(all_tokens) <= 1:
        return _finding("R24", title, "skip", "info", f"仅识别到 {len(all_tokens)} 个样品编号，无法检查断号或重复", evidence, section="样品信息", cell="样品编号")
    return _finding("R24", title, "pass", "info", f"样品编号 {len(all_tokens)} 个（{len(groups)} 组前缀），未发现断号或重复", evidence, section="样品信息", cell="样品编号")


# --- R25: 结论页无判定时结果表不得判定 -------------------------------------
# RA-1.6. Failure branches (each covered in p0/tests/test_judge_consistency.py):
# F1 没有检验结果表 -> skip，不能把“看不到”当成“没有判定”。
# F2 结果表没有「符合性判定」列 -> skip，无法确认结果表判定表述。
# F3 判定单元格为空或是杠掉写法（/、—、不适用）-> 视为无判定。
# F4 判定单元格出现判定词（符合/不符合、合格/不合格、通过/不通过、满足）-> 有判定。
# F5 结论页识别不到「检验结论」字段/标题 -> skip，避免把解析失败当成“无判定”。
# F6 结论页为空或杠掉 -> 无判定。
# F7 结论页出现判定词 -> 有判定；「符合性判定」「合格证」「通过率」是名词，不算判定。
# F8 结论页与结果表都有判定 -> pass（验收：两边都有判定不报）。
# F9 结论页无判定、结果表有判定 -> fail（验收：结论页无判定时结果表有判定即报出）。
# F10 两边都无判定 -> pass（约束成立）。
# F11 多样本结果表收集所有出现的判定值，去重后写入证据；不要求各行判定相同（R06 管数值一致性）。
_CONCLUSION_HEADING_RE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,2})*[.、]?\s*)?(检验结论|检测结论)\s*(?:[:：]\s*(.*))?$"
)
_JUDGMENT_NA = {"", "/", "／", "—", "——", "-", "－", "–", "无", "不适用", "n/a", "na", "不涉及", "未判定"}
_JUDGMENT_RE = re.compile(r"不符合|符合(?!性)|不通过|通过(?!率)|不合格|合格(?!证)|不满足|满足")


def _is_judgment_na(text: str) -> bool:
    norm = _norm_cell(text)
    return not norm or norm.lower() in _JUDGMENT_NA


def _has_judgment(text: str) -> bool:
    return bool(text) and not _is_judgment_na(text) and bool(_JUDGMENT_RE.search(text))


def _conclusion_text(data: ReportData) -> tuple[str, bool]:
    """Return (conclusion text, whether the conclusion page was identified)."""
    text = (data.conclusion or "").strip()
    if text:
        return text, True
    paragraphs = [line.strip() for line in data.paragraphs if line and line.strip()]
    for index, line in enumerate(paragraphs):
        match = _CONCLUSION_HEADING_RE.match(line)
        if not match:
            continue
        if match.group(2) and match.group(2).strip():
            return match.group(2).strip(), True
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        # F6: 「检验结论」下一行若是另一个字段标签（如“签发日期：”），说明结论内容为空。
        if following and not re.match(r"^[^：:]{1,12}\s*[:：]", following):
            return following, True
        return "", True
    return "", False


def _result_judgments(data: ReportData) -> tuple[list[str], bool]:
    """Return (judgment cell values, whether the 「符合性判定」 column exists)."""
    if not data.result_cells:
        return ([data.judgment.strip()], True) if (data.judgment or "").strip() else ([], False)
    header = [cell.strip() for cell in data.result_cells[0]]
    if "符合性判定" not in header:
        return [], False
    column = header.index("符合性判定")
    values = [
        row[column].strip()
        for row in data.result_cells[1:]
        if column < len(row) and row[column].strip()
    ]
    return values, True


def r25_judge_consistency(data: ReportData):
    title = "结论页无判定时结果表不得判定"
    if not data.result_cells and not (data.judgment or "").strip():
        return _finding("R25", title, "skip", "info", "未识别到检验结果表，无法检查结果表判定表述", section="检验结果表", cell="符合性判定")
    values, column_found = _result_judgments(data)
    if not column_found:
        return _finding("R25", title, "skip", "info", "检验结果表无「符合性判定」列，无法检查结果表判定表述", section="检验结果表", cell="符合性判定")
    conclusion, conclusion_found = _conclusion_text(data)
    if not conclusion_found:
        return _finding("R25", title, "skip", "info", "未识别到结论页「检验结论」字段，无法判断结论页是否有判定表述", section="检验结论", cell="检验结论")
    conclusion_judged = _has_judgment(conclusion)
    table_judgments = list(dict.fromkeys(value for value in values if _has_judgment(value)))
    evidence = f"结论={conclusion[:80] or '空'}; 符合性判定={'、'.join(values[:3]) or '空'}"
    if table_judgments and not conclusion_judged:
        return _finding(
            "R25", title, "fail", "medium",
            f"结论页无判定表述，但结果表符合性判定为「{'、'.join(table_judgments[:3])}」",
            evidence,
            "在结论页补充明确判定（符合/不符合），或删除结果表的符合性判定，使结论页与结果表一致",
            section="检验结论/检验结果表", cell="、".join(table_judgments[:3])[:80],
        )
    if table_judgments:
        return _finding(
            "R25", title, "pass", "info",
            f"结论页与结果表均有判定表述（结论「{conclusion[:30]}」；符合性判定「{'、'.join(table_judgments[:3])}」）",
            evidence, section="检验结论/检验结果表", cell="检验结论/符合性判定",
        )
    if conclusion_judged:
        return _finding(
            "R25", title, "pass", "info",
            "结论页有判定表述、结果表无判定表述，未出现结论页无判定而结果表判定的情况",
            evidence, section="检验结论/检验结果表", cell="检验结论",
        )
    return _finding(
        "R25", title, "pass", "info",
        "结论页与结果表均无判定表述，未出现结论页无判定而结果表判定的情况",
        evidence, section="检验结论/检验结果表", cell="检验结论/符合性判定",
    )


# --- R27: 原始记录版本有效性（RA-4.7） -------------------------------------
# 原始记录通过「检验依据」引用试验标准；引用过期版本号即“未使用最新有效版本”。
# Failure branches (each covered in p0/tests/test_raw_record_version.py):
# F1 未识别到检验依据字段 -> skip，不冒充 pass。
# F2 检验依据里没有标准号 -> skip。
# F3 引用的标准全部无年代号 -> skip（年代号缺失由 R22 负责，不重复判）。
# F4 引用的标准均不在最新有效版本表 -> skip，无依据不臆断。
# F5 引用版本早于已知最新版本 -> fail，逐条列出旧版本与最新版本。
# F6 引用版本等于或晚于最新版本 -> pass（最新版本不报；晚于表内版本按已升版处理）。
# F7 同一标准重复引用只报一次；旧、新版本同时出现时只报旧版本。
# F8 多个标准混合（过期/最新/未收录）-> 只报过期项，未收录项只计数。
# F9 全角/长横写法先按 R22 的归一化切分，同一版本不因横线写法重复或漏判。
# F10 企业标准编号里的 4 位数字（A1901）不当年份，只认末尾 -YYYY/:YYYY。
# F11 连字符变体（U+2010/U+2011/U+2012 等）先折成 ASCII "-"，版本号才能切出年份。
# F12 「Q/ 代号」后的空格与连字符视为同一写法（Q/BYDQ A1901.404 == Q/BYDQ-A1901.404），
#     版本键归一后再查表；同一标准的两种写法重复引用只报一次。
#
# 最新有效版本表来源：客户当前 12 份 BYD 草稿与 records.xlsx 的检验依据（2026-11-08 基线）。
# 只收录能从客户现行资料确认的版本；未收录标准不判，客户升版后在此更新一行即可。
_RAW_RECORD_LATEST_VERSIONS = {
    "Q/BYDQ-A1901.404": 2025,
    "Q/BYDQ-A1901.1373": 2018,
}


def _version_key(text: str) -> str:
    """Canonical key where dash and space separators are equivalent.

    Word writes the same enterprise standard as ``Q/BYDQ-A1901.404`` or
    ``Q/BYDQ A1901.404`` and may use U+2010/U+2011 hyphens; fold them all to
    one ASCII form before comparing with the registry.
    """
    return re.sub(r"[\s-]+", "-", _norm_standard_text(text))


_RAW_RECORD_LATEST = {_version_key(key): year for key, year in _RAW_RECORD_LATEST_VERSIONS.items()}


def _standard_year(prefix: str, body: str) -> int | None:
    """Return the 4-digit year at the end of a standard number, if any."""
    body = (body or "").strip()
    if prefix == "ASTM":
        body = re.sub(r"e\d+$", "", body)
    match = re.search(r"[-:](\d{4})$", body)
    if match:
        year = int(match.group(1))
        return year if 1900 <= year <= 2099 else None
    if prefix == "ASTM" and (match := re.search(r"-(\d{2})$", body)):
        return 2000 + int(match.group(1))
    return None


def _standard_version_key(prefix: str, body: str) -> str:
    """Normalise a standard number to ``prefix + number`` without its year."""
    body = re.sub(r"e\d+$", "", (body or "").strip())
    body = re.sub(r"[-:]\d{4}$", "", body)
    if prefix == "ASTM":
        body = re.sub(r"-\d{2}$", "", body)
    return _version_key(f"{prefix}{body}")


def _unique_tokens(tokens: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Deduplicate citations; dash/space spellings of one citation count once."""
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for token, prefix, body in tokens:
        key = _version_key(token)
        if key not in seen:
            seen.add(key)
            unique.append((token, prefix, body))
    return unique


def _display_token(token: str) -> str:
    """Show enterprise standards in the canonical ``Q/CODE-NUMBER`` form."""
    return re.sub(r"^([QT]/[A-Za-z0-9]+)[ \t]+", r"\1-", token)


def r27_raw_record_version(data: ReportData):
    title = "原始记录版本有效性"
    basis = _basis_text(data)
    if not basis:
        return _finding("R27", title, "skip", "info", "未识别到检验依据（检测依据）字段，无法检查引用版本")
    tokens = _unique_tokens(_standard_tokens(basis))
    if not tokens:
        return _finding(
            "R27", title, "skip", "info", f"检验依据中未识别到标准号，无法检查版本：{basis[:60]}",
            f"检验依据={basis[:80]}", section="检验依据", cell="版本号",
        )
    expired: list[str] = []
    judged = 0
    unknown: list[str] = []
    no_year: list[str] = []
    for token, prefix, body in tokens:
        year = _standard_year(prefix, body)
        if year is None:
            no_year.append(token)
            continue
        latest = _RAW_RECORD_LATEST.get(_standard_version_key(prefix, body))
        if latest is None:
            unknown.append(token)
            continue
        judged += 1
        if year < latest:
            expired.append(f"{_display_token(token)}（最新 {latest}）")
    evidence = (
        f"检验依据={basis[:80]}; 引用标准={len(tokens)}; 可判定={judged}; "
        f"过期={len(expired)}; 未收录={len(unknown)}; 无年代号={len(no_year)}"
    )
    if expired:
        return _finding(
            "R27", title, "fail", "medium",
            f"原始记录引用了 {len(expired)} 个过期版本号：" + "；".join(expired[:6]),
            evidence,
            "按最新有效版本更新检验依据与原始记录中的标准版本号；客户升版后同步更新",
            section="检验依据", cell="、".join(expired)[:80],
        )
    if judged:
        message = f"引用的 {judged} 个标准版本均为最新有效版本"
        if unknown:
            message += f"（另有 {len(unknown)} 个标准未收录最新版本表，未判）"
        return _finding("R27", title, "pass", "info", message, evidence, section="检验依据", cell="版本号")
    if unknown:
        return _finding(
            "R27", title, "skip", "info",
            f"引用的 {len(unknown)} 个标准均不在最新有效版本表，无法判断是否过期："
            + "、".join(_display_token(token) for token in unknown[:4]),
            evidence, section="检验依据", cell="版本号",
        )
    return _finding(
        "R27", title, "skip", "info",
        f"引用的 {len(no_year)} 个标准均无年代号，无法判断版本（缺年代号由 R22 报出）："
        + "、".join(_display_token(token) for token in no_year[:4]),
        evidence, section="检验依据", cell="版本号",
    )


# --- R29: 产品名称异常识别（RA-5.2） ---------------------------------------
# 判定输入只有「样品名称」(data.sample_name) 与「检验项目」(data.item) 两个
# 字段；不读正文、不做语义推理，避免把合法组合误报。
# Failure branches (each covered in p0/tests/test_product_name.py):
# F1 样品名称为空 -> skip（空缺项由 R14 报出，不重复）。
# F2 样品名称是占位符/不适用值（XXXXX、XX、___、待填、—/–/－ 等横线）-> skip（R14 负责）。
# F3 样品名称是纯编号/代号（001、A-001、SN2024、2024-01、第001号样品、一号样品）-> fail。
# F4 样品名称只由检测项目词构成（气味、拉伸强度、外观检查、门板尺寸）-> fail：
#    与检验项目相同 -> “与检验项目相同”；与检验项目不同 -> “明显不符”。
# F5 样品名称只是通用词（样品/产品/零部件/总成/材料）-> fail，无法判定种类。
# F6 名称里还有检测项目词以外的产品/描述词（后视镜、袋子-100L、PP材料、气味瓶）-> pass。
# F7 英文/字母名称没有编号特征（Seat、PP）-> pass，按可能的产品名处理，不误报。
# F8 真实产品名与检验项目是否适配（后视镜做气味试验）不在本规则判定，理由见 README。
_TEST_ITEM_TERMS = tuple(sorted((
    "断裂伸长率", "压缩永久变形", "熔体流动速率", "挥发性有机物", "尺寸稳定性",
    "拉伸强度", "拉伸性能", "弯曲强度", "弯曲模量", "冲击强度", "撕裂强度",
    "粘合强度", "剥离强度", "耐刮擦性", "耐刮擦", "耐磨性", "耐磨",
    "耐候性", "耐候", "耐化学性", "耐化学", "耐湿热", "耐低温", "耐高温",
    "绝缘电阻", "耐电压", "熔融指数", "门板尺寸", "光泽度", "附着力",
    "挥发性", "雾化值", "甲醛", "重金属", "色差", "硬度", "密度", "灰分",
    "水分", "挥发分", "酸值", "粘度", "电导率", "温升", "振动", "噪声",
    "疲劳", "腐蚀", "燃烧", "阻燃性", "阻燃", "雾化", "老化", "盐雾",
    "密封性", "密封", "气密性", "水密性", "外观", "尺寸", "气味", "成分",
    "含量", "迁移", "析出", "voc", "ph",
), key=len, reverse=True))
# 只去掉两端的通用词/字段后缀，避免把“气味瓶”“座椅气味”里的产品词一起吞掉。
_NAME_WRAPPERS = (
    "试验件", "样品", "试样", "样件", "检测", "测试", "试验", "检验",
    "项目", "方法", "等级", "结果", "要求", "报告", "记录", "名称",
    "检查", "编号", "第", "号", "样", "值",
)
_GENERIC_NAMES = {
    "样品", "产品", "试样", "样件", "试验件", "零部件", "零件", "部件",
    "总成", "材料", "物品", "东西", "其他",
}
# R14 已把以下值当“未填写/占位符”报出，R29 只 skip，不重复报。
_NAME_PLACEHOLDERS = {
    "待填", "待定", "未填", "未知", "无", "不适用", "不涉及",
    "n/a", "na", "null", "none", "—", "-", "/", "□", "☐",
}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_PURE_ID_RE = re.compile(r"^(?=.*\d)[a-z0-9]+$")
# 横线各写法（–/—/－/‒/―/−/‐/‑/⸺）都表示“不适用”，先归一，避免同一语义落进不同分支。
_DASH_RE = re.compile(r"[‐‑‒–—―−⸺－]")
_NAME_SEPARATORS_RE = re.compile(r"[^0-9a-z\u3400-\u9fff]+")


def _name_key(text: str) -> str:
    """Drop punctuation/symbols and case so ``A-001`` and ``Ａ００１`` compare equal."""
    return _NAME_SEPARATORS_RE.sub("", unicodedata.normalize("NFKC", str(text or "")).strip().lower())


def _product_name_residual(key: str) -> tuple[str, list[str]]:
    """Strip generic wrappers and test-item terms; return (residual, matched terms)."""
    residual = key
    matched: list[str] = []
    changed = True
    while changed and residual:
        changed = False
        for wrapper in _NAME_WRAPPERS:
            if residual.startswith(wrapper):
                residual = residual[len(wrapper):]
                changed = True
            elif residual.endswith(wrapper):
                residual = residual[:-len(wrapper)]
                changed = True
        for term in _TEST_ITEM_TERMS:
            if term in residual:
                matched.append(term)
                residual = residual.replace(term, "")
                changed = True
    return residual, matched


def r29_product_name(data: ReportData):
    title = "产品名称异常识别"
    name_text = str(data.sample_name or "").strip()
    item_text = str(data.item or "").strip()
    evidence = f"样品名称={name_text or '（空）'}; 检验项目={item_text or '（空）'}"
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name_text).lower())
    if not compact:
        return _finding(
            "R29", title, "skip", "info", "未识别到样品名称字段，无法判定产品种类（空缺项由 R14 报出）",
            evidence, section="样品名称", cell="样品名称",
        )
    if (compact in _NAME_PLACEHOLDERS or _DASH_RE.sub("-", compact) in _NAME_PLACEHOLDERS
            or re.fullmatch(r"-+", _DASH_RE.sub("-", compact))
            or re.fullmatch(r"x+", compact) or re.fullmatch(r"[_＿.·…]+", compact)):
        return _finding(
            "R29", title, "skip", "info", "样品名称为占位符/不适用值，无法判定产品种类（占位符由 R14 报出）",
            evidence, section="样品名称", cell=name_text[:40],
        )
    key = _name_key(name_text)
    suggestion = "填写可识别的产品名称（产品类别+型号），不要只写编号、代号、检测项目或通用词"
    if key in _GENERIC_NAMES:
        return _finding(
            "R29", title, "fail", "medium",
            f"样品名称「{name_text}」仅含通用词，无法判定产品种类",
            evidence, suggestion, section="样品名称", cell=name_text[:40],
        )
    residual, matched = _product_name_residual(key)
    if not residual and matched:
        item_key = _name_key(item_text)
        if item_key and all(term in item_key for term in matched):
            message = f"产品名称与检验项目相同（「{name_text}」），无法判定产品种类"
        elif item_text:
            message = f"产品名称「{name_text}」是检测项目名，与检验项目「{item_text}」明显不符，无法判定产品种类"
        else:
            message = f"样品名称「{name_text}」是检测项目名，无法判定产品种类"
        return _finding("R29", title, "fail", "medium", message, evidence, suggestion,
                        section="样品名称", cell=name_text[:40])
    if not residual:
        return _finding(
            "R29", title, "fail", "medium",
            f"样品名称「{name_text}」仅含通用词，无法判定产品种类",
            evidence, suggestion, section="样品名称", cell=name_text[:40],
        )
    if ((not _CJK_RE.search(residual) and _PURE_ID_RE.match(residual)
            and not re.search(r"[a-z]{4,}", residual))
            or re.fullmatch(r"[一二三四五六七八九十百千万零〇两]+", residual)):
        return _finding(
            "R29", title, "fail", "medium",
            f"样品名称「{name_text}」为纯编号/代号，无法判定产品种类",
            evidence, suggestion, section="样品名称", cell=name_text[:40],
        )
    return _finding(
        "R29", title, "pass", "info", f"样品名称「{name_text}」可识别产品种类",
        evidence + "; 判定=可识别", section="样品名称", cell=name_text[:40],
    )


def evaluate_format_rules(data: ReportData):
    """Run every stage-A format rule; callers apply admin overrides."""
    return [rule(data) for rule in _RULE_FUNCTIONS.values()]


_RULE_FUNCTIONS = {"R12": r12_pages, "R13": r13_bookmarks, "R14": r14_empty_cells, "R15": r15_numbering, "R16": r16_units, "R17": r17_fonts, "R18": r18_toc_pages, "R22": r22_standard_years, "R24": r24_sample_numbers, "R25": r25_judge_consistency, "R27": r27_raw_record_version, "R29": r29_product_name}
