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
_STD_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "－": "-", "―": "-"})
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
_SAMPLE_LINE = re.compile(r"^\s*[*＊]?\s*样品编号\s*[:：]\s*(.*)$")
_SAMPLE_SPLIT = re.compile(r"[、,，;；\s]+")
_SAMPLE_RANGE = re.compile(r"^(.+?)\s*(?:~|至)\s*(.+)$")
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
    if not 0 <= end - start <= 50:
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


def evaluate_format_rules(data: ReportData):
    """Run every stage-A format rule; callers apply admin overrides."""
    return [rule(data) for rule in _RULE_FUNCTIONS.values()]


_RULE_FUNCTIONS = {"R12": r12_pages, "R13": r13_bookmarks, "R14": r14_empty_cells, "R15": r15_numbering, "R16": r16_units, "R17": r17_fonts, "R18": r18_toc_pages, "R22": r22_standard_years, "R24": r24_sample_numbers}
