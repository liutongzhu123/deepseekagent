"""R17 body font and size consistency checks."""
from __future__ import annotations

import re

from ._shared import ReportData, _finding, _toc_like
from .numbering import _PAREN_RE, _SECTION_RE, _SINGLE_RE

_NOTE_PREFIXES = ("注：", "注:", "备注：", "备注:", "说明：", "说明:", "注 ")
_COVER_LABELS = (
    "报告编号", "地址", "电话", "传真", "邮编", "邮政编码", "委托单位", "样品名称",
    "生产企业", "检验类别", "试验类型", "检验依据", "检验项目", "检验结论",
    "签发日期", "批准", "审核", "主检", "商标", "规格型号",
)


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
