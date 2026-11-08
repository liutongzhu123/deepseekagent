"""Deterministic typo / 病句 checks (R21).

High-precision layer: a curated wrong→right dictionary (miswritten and
reversed two-character words), repeated function characters/words and repeated
sentence punctuation.  ``TERM_WHITELIST`` masks legitimate professional terms
and fixed reduplications before scanning; for example “检验检测” contains the
reversed string “验检” and must not be reported.  Every finding names the
1-based paragraph ordinal.  The model-based S01 layer remains the broad
complement for arbitrary wording and grammar.

Failure branches (each covered in ``tests/test_typo_rules.py``):
F1 no text paragraphs -> skip, never pass.
F2 dictionary typo -> fail at the paragraph.
F3 typo-like string inside a whitelisted term -> ignored.
F4 repeated function char/word -> warn.
F5 repeated sentence punctuation -> warn.
F6 one paragraph with several problems -> one finding with all of them.
F7 signature/table label lines are not flagged as repeated words.
F8 empty/whitespace paragraphs are ignored and not counted as scanned.
"""
from __future__ import annotations

import re

from .docx_parser import ReportData
from .rules import Finding

RULE_ID = "R21"
TITLE = "错别字与病句"

# Wrong -> right.  Only pairs whose wrong/reversed form is not a valid word:
# 产生/生产, 学科/科学 etc. are deliberately absent.  F2.
TYPOS: tuple[tuple[str, str], ...] = (
    ("检侧", "检测"), ("符和", "符合"), ("样吕", "样品"), ("样晶", "样品"),
    ("试险", "试验"), ("委拖", "委托"), ("另部件", "零部件"), ("气昧", "气味"),
    ("甲荃", "甲醛"), ("甲笨", "甲苯"), ("聚丙稀", "聚丙烯"), ("聚氯乙稀", "聚氯乙烯"),
    ("挥发姓", "挥发性"), ("按装", "安装"), ("既使", "即使"), ("做为", "作为"),
    ("融熔", "熔融"), ("蜜封", "密封"), ("计术", "技术"), ("报诰", "报告"),
    ("验检", "检验"), ("测检", "检测"), ("验试", "试验"), ("品样", "样品"),
    ("托委", "委托"), ("合符", "符合"), ("告报", "报告"), ("果结", "结果"),
    ("录记", "记录"), ("备设", "设备"), ("器仪", "仪器"), ("准标", "标准"),
    ("法方", "方法"), ("论结", "结论"), ("定判", "判定"), ("注备", "备注"),
    ("查检", "检查"), ("测监", "监测"), ("析分", "分析"), ("度温", "温度"),
)

# Professional terms / fixed reduplications that must not be reported.  F3.
TERM_WHITELIST: tuple[str, ...] = (
    "检验检测", "检测检验", "检验检测专用章", "检验检测机构",
    "往往", "一一对应", "不了了之", "是是非非", "和和美美", "觉得得到",
    "目的的确", "为了了解", "除了了解",
    "甲醛", "甲苯", "苯乙烯", "乙苯", "二甲苯", "苯系物", "总挥发性有机化合物",
    "挥发性有机化合物", "TVOC", "检出限", "平行样", "空白样", "恒温恒湿",
    "气味等级", "袋式法", "瓶式法", "微池法", "分光光度法", "气相色谱-质谱法",
    "零部件", "汽车内饰", "mg/m³", "℃", "MPa",
)

# Repeated adjacent words / function chars / punctuation (F4/F5).  Signature
# labels (主检/审核/批准) are absent so merged signature cells do not hit F7.
DUP_WORDS: tuple[str, ...] = (
    "进行", "通过", "符合", "检验", "检测", "试验", "样品", "报告", "结果",
    "要求", "判定", "测试", "检查", "监测", "分析", "记录", "设备", "仪器",
    "标准", "方法", "结论", "备注", "处理", "温度", "委托", "出具", "采用",
    "按照", "根据",
)
DUP_CHARS: tuple[str, ...] = ("的", "了")
DUP_PUNCT: tuple[str, ...] = ("，", "。", "；", "：", "、")

_TERM_RE = re.compile(
    "|".join(re.escape(term) for term in sorted(TERM_WHITELIST, key=len, reverse=True)),
    re.IGNORECASE,
)


def _mask_terms(text: str) -> str:
    return _TERM_RE.sub(lambda match: " " * len(match.group(0)), text)


def _issues(text: str) -> list[tuple[str, str, int]]:
    """Return ``(status, description, occurrences)`` for one paragraph."""
    work = _mask_terms(text)
    issues: list[tuple[str, str, int]] = []
    for word in DUP_WORDS:  # F4: repeated word, e.g. 检验检验
        token = word * 2
        if count := work.count(token):
            issues.append(("warn", f"重复词「{token}」", count))
            work = work.replace(token, " " * len(token))
    for char in DUP_CHARS:  # F4: repeated function char, e.g. 的的
        token = char * 2
        if count := work.count(token):
            issues.append(("warn", f"重复字「{token}」", count))
            work = work.replace(token, " " * len(token))
    for punct in DUP_PUNCT:  # F5
        token = punct * 2
        if count := work.count(token):
            issues.append(("warn", f"重复标点「{token}」", count))
            work = work.replace(token, " " * len(token))
    for wrong, right in TYPOS:  # F2/F3 (whitelisted spans are already blank)
        if count := work.count(wrong):
            issues.append(("fail", f"错别字「{wrong}」应为「{right}」", count))
            work = work.replace(wrong, " " * len(wrong))
    return issues


def _describe(issues: list[tuple[str, str, int]]) -> str:
    return "；".join(
        f"{description}" + (f"×{count}" if count > 1 else "")
        for _, description, count in issues
    )


def _finding(status: str, severity: str, message: str, evidence: str = "", suggestion: str = "", paragraph: int = 0, page: int = 1) -> Finding:
    return Finding(
        RULE_ID, TITLE, status, severity, message, evidence, suggestion,
        {"page": max(1, page), "section": "正文段落", "cell": f"段落 {paragraph}" if paragraph else "全文"},
    )


def _page_for(text: str, data: ReportData) -> int:
    for page in data.pages:
        if any(text in (line or "") for line in page.get("lines") or []):
            return max(1, int(page.get("index") or 1))
    return 1


def evaluate_typo_rules(data: ReportData) -> list[Finding]:
    """Run R21; one finding per affected paragraph, pass/skip otherwise."""
    paragraphs = [(index, text) for index, text in enumerate(data.paragraphs, 1) if text and text.strip()]
    if not paragraphs:  # F1/F8
        return [_finding("skip", "info", "未识别到正文段落，无法检查错别字/病句", suggestion="保留可解析的正文后重试")]
    findings: list[Finding] = []
    scanned = 0
    for index, text in paragraphs:
        scanned += len(text)
        issues = _issues(text)
        if not issues:
            continue
        status = "fail" if any(state == "fail" for state, _, _ in issues) else "warn"
        label = "错别字" if status == "fail" else "疑似病句"
        findings.append(
            _finding(
                status, "medium" if status == "fail" else "low",
                f"段落 {index} 发现{label}：" + _describe(issues),
                text[:100] + ("…" if len(text) > 100 else ""),
                "按提示核对原文；属规范专业术语的写法可补充到白名单",
                index,
                _page_for(text, data),
            )
        )
        if len(findings) >= 20:  # keep a pathological draft from flooding the review list
            findings.append(_finding("warn", "low", "错别字/病句问题超过 20 段，其余段落未逐条列出", suggestion="先修已列出的段落，复核后再跑一次"))
            break
    if findings:
        return findings
    return [
        _finding(
            "pass", "info",
            f"扫描 {len(paragraphs)} 段、{scanned} 字，未发现词典内错别字或重复字词/标点",
            f"段落={len(paragraphs)}; 字符={scanned}; 词典={len(TYPOS)}; 白名单={len(TERM_WHITELIST)}",
        )
    ]
