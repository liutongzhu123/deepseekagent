"""R30: compare the conclusion verdict with result-table verdicts."""
from __future__ import annotations

import re

from ..docx_parser import ReportData
from ._shared import _conclusion_text, _finding, _is_judgment_na, _result_judgments

# F1 missing conclusion/column -> skip; F2 unknown wording -> skip;
# F3 negated or mixed-polarity text -> skip; F4 unknown row values -> skip;
# F5 mixed table verdicts -> skip until the aggregation policy is confirmed;
# F6 opposite single-polarity outcomes -> fail, matching outcomes -> pass.
_OUTCOME_TOKEN_RE = re.compile(
    r"不符合|不通过|不合格|不满足|符合(?!性)|通过(?!率)|合格(?!证)|满足"
)
_NEGATED_OUTCOME_RE = re.compile(
    r"(?:尚未|尚无|未|没有|无|不存在)\s*"
    r"(?:发现|见|出现|检出|达到|任何|明确|明显)?\s*"
    r"(?:不符合|不通过|不合格|不满足|符合|通过|合格|满足)"
    r"|(?:不得|不应|不能|不可|无法)\s*"
    r"(?:判定|认定|认为|视为)?\s*(?:为|是)?\s*"
    r"(?:不符合|不通过|不合格|不满足|符合|通过|合格|满足)"
)


def _judgment_outcome(text: str) -> str | None:
    """Classify an explicit verdict; return None when its meaning is unclear."""
    text = (text or "").strip()
    if _is_judgment_na(text) or _NEGATED_OUTCOME_RE.search(text):
        return None
    tokens = [match.group(0) for match in _OUTCOME_TOKEN_RE.finditer(text)]
    polarities = {"negative" if token.startswith("不") else "positive" for token in tokens}
    return next(iter(polarities)) if len(polarities) == 1 else None


def r30_judgment_consistency(data: ReportData):
    """Compare the overall conclusion with every recognized result-table verdict."""
    title = "结论判定与结果表判定一致"
    conclusion, conclusion_found = _conclusion_text(data)
    if not conclusion_found:
        return _finding(
            "R30", title, "skip", "info", "未识别到结论页检验结论，无法核对判定一致性",
            section="检验结论", cell="检验结论",
        )
    values, column_found = _result_judgments(data)
    if not column_found:
        return _finding(
            "R30", title, "skip", "info", "未识别到结果表符合性判定列，无法核对判定一致性",
            section="检验结果表", cell="符合性判定",
        )

    conclusion_outcome = _judgment_outcome(conclusion)
    if conclusion_outcome is None:
        return _finding(
            "R30", title, "skip", "info", "结论页判定表述不明确，无法安全比较",
            f"结论={conclusion[:100] or '空'}", section="检验结论", cell="检验结论",
        )

    result_values = [value for value in values if not _is_judgment_na(value)]
    if not result_values:
        return _finding(
            "R30", title, "skip", "info", "结果表没有可比较的符合性判定",
            f"结论判定={conclusion_outcome}; 结果表判定=空",
            section="检验结果表", cell="符合性判定",
        )
    result_outcomes = [_judgment_outcome(value) for value in result_values]
    if any(outcome is None for outcome in result_outcomes):
        return _finding(
            "R30", title, "skip", "info", "结果表含无法识别的判定值，无法安全归并",
            f"结论判定={conclusion_outcome}; 结果表判定={'、'.join(result_values[:5])}",
            section="检验结果表", cell="符合性判定",
        )

    if len(set(result_outcomes)) > 1:
        return _finding(
            "R30", title, "skip", "info", "结果表同时有符合与不符合，缺少经确认的总体归并口径",
            f"结论={conclusion[:100]}; 结果行判定={'、'.join(dict.fromkeys(result_values[:5]))}",
            section="检验结果表", cell="符合性判定",
        )
    table_outcome = result_outcomes[0]
    evidence = (
        f"结论判定={conclusion_outcome}; 结果表总体判定={table_outcome}; "
        f"结果行判定={'、'.join(dict.fromkeys(result_values[:5]))}"
    )
    if conclusion_outcome != table_outcome:
        return _finding(
            "R30", title, "fail", "medium",
            f"结论页总体判定与结果表不一致（结论={conclusion_outcome}，结果表={table_outcome}）",
            evidence, "核对各检验项目判定，并同步修正结论页总体判定",
            section="检验结论/检验结果表", cell="检验结论/符合性判定",
        )
    return _finding(
        "R30", title, "pass", "info", "结论页总体判定与结果表判定一致",
        evidence, section="检验结论/检验结果表", cell="检验结论/符合性判定",
    )
