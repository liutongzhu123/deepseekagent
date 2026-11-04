"""Stage-1 baseline metrics from the POC label set.

Important: the 12 BYD drafts are 12 template variants of one report, and the
labels come from the POC defect list, not an independent 20–30 report gold
standard.  These numbers are a regression baseline, not customer acceptance.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from app.docx_parser import parse_docx
from app.rules import RULES, RecordInput, audit_report, validate_records
from eval.stage1_regression import DRAFT_FAIL_RULES, load_records

DRAFT_RULES = tuple(rule for rule, _, _ in RULES)
RECORD_RULES = ("REC-01", "REC-02", "REC-03", "REC-04")
KEY_RULES = {"R01": "编号", "R04": "日期", "R06": "结论", "REC-03": "判定"}


def _draft_cases(samples_dir: str | Path) -> list[tuple[str, set[str], set[str]]]:
    cases = []
    for index, path in enumerate(sorted(Path(samples_dir).glob("*.docx")), start=1):
        result = audit_report(parse_docx(path))
        predicted = {item["rule_id"] for item in result["findings"] if item["status"] == "fail"}
        cases.append((f"draft-{index:02d}", set(DRAFT_FAIL_RULES), predicted))
    return cases


def _draft_r06_cases(samples_dir: str | Path) -> list[tuple[str, set[str], set[str]]]:
    """Synthetic conclusion mutants: 12 drafts all judge 不符合, flip to 符合."""
    cases = []
    for index, path in enumerate(sorted(Path(samples_dir).glob("*.docx")), start=1):
        data = parse_docx(path)
        mutated = dataclasses.replace(data, judgment="符合", conclusion="经检验，该样品符合要求")
        predicted = {item["rule_id"] for item in audit_report(mutated)["findings"] if item["status"] == "fail"}
        cases.append((f"draft-r06-mutant-{index:02d}", {"R06"}, predicted))
    return cases


def _record_cases(records: list[RecordInput]) -> list[tuple[str, set[str], set[str]]]:
    cases: list[tuple[str, set[str], set[str]]] = []
    for index, record in enumerate(records, start=1):
        cases.append((f"clean-{index}", set(), {item["rule_id"] for item in validate_records([record])["findings"]}))
    for index, record in enumerate(records, start=1):
        mean = dataclasses.replace(record, reported_mean=round((record.reported_mean or 0) + 0.1, 1))
        cases.append((f"mutant-mean-{index}", {"REC-01"}, {item["rule_id"] for item in validate_records([mean])["findings"]}))
        flipped = "符合" if record.judgment != "符合" else "不符合"
        cases.append((f"mutant-judgment-{index}", {"REC-03"}, {item["rule_id"] for item in validate_records([dataclasses.replace(record, judgment=flipped)])["findings"]}))
        day = date.fromisoformat(record.test_date) - timedelta(days=1)
        cases.append((f"mutant-date-{index}", {"REC-02"}, {item["rule_id"] for item in validate_records([dataclasses.replace(record, eval_date=day.isoformat())])["findings"]}))
        cases.append((f"mutant-duplicate-{index}", {"REC-04"}, {item["rule_id"] for item in validate_records([*records, record])["findings"]}))
    return cases


def _prf(cases: Iterable[tuple[str, set[str], set[str]]], rule: str) -> dict:
    tp = fp = fn = tn = 0
    for _, expected, predicted in cases:
        in_expected, in_predicted = rule in expected, rule in predicted
        if in_expected and in_predicted:
            tp += 1
        elif in_predicted:
            fp += 1
        elif in_expected:
            fn += 1
        else:
            tn += 1
    total = tp + fp + fn + tn
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
    }


def compute_metrics(samples_dir: str | Path, records: list[RecordInput]) -> dict:
    draft_cases = _draft_cases(samples_dir)
    record_cases = _record_cases(records)
    per_rule: dict[str, dict] = {}
    r06_cases = _draft_r06_cases(samples_dir)
    for rule in DRAFT_RULES:
        cases = draft_cases + (r06_cases if rule == "R06" else [])
        per_rule[rule] = {"group": "draft", **_prf(cases, rule)}
    for rule in RECORD_RULES:
        per_rule[rule] = {"group": "record", **_prf(record_cases, rule)}

    all_cells = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for item in per_rule.values():
        for key in all_cells:
            all_cells[key] += item[key]
    total = sum(all_cells.values())
    overall = {
        **all_cells,
        "precision": round(all_cells["tp"] / (all_cells["tp"] + all_cells["fp"]), 4) if all_cells["tp"] + all_cells["fp"] else 1.0,
        "recall": round(all_cells["tp"] / (all_cells["tp"] + all_cells["fn"]), 4) if all_cells["tp"] + all_cells["fn"] else 1.0,
        "accuracy": round((all_cells["tp"] + all_cells["tn"]) / total, 4) if total else 0.0,
    }
    key_recall = {rule: per_rule[rule]["recall"] for rule in KEY_RULES}
    return {
        "per_rule": per_rule,
        "overall": overall,
        "key_recall": key_recall,
        "gate_ok": all(value == 1.0 for value in key_recall.values()),
        "draft_cases": len(draft_cases),
        "r06_cases": len(r06_cases),
        "record_cases": len(record_cases),
    }


def render_markdown(metrics: dict, records_path: str) -> str:
    titles = {rule: title for rule, title, _ in RULES}
    titles.update({"REC-01": "记录均值", "REC-02": "记录日期", "REC-03": "记录判定", "REC-04": "样品编号唯一"})
    lines = [
        "# 阶段 1 指标评测（基线）",
        "",
        "> **样本口径**：12 份比亚迪草稿是**同一报告编号的 12 个模板变体**，不是 12 份独立报告；"
        "标签来自 POC 缺陷清单。4 条真实记录来自 POC 审核视图，16 个变异由这 4 条记录生成。"
        "本报告是回归基线，不能替代 20–30 份正式报告金标准。",
        "",
        "## 1. 每规则 precision / recall",
        "",
        "| 规则 | 检查项 | 样本组 | TP | FP | FN | TN | Precision | Recall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for rule, item in metrics["per_rule"].items():
        lines.append(
            f"| {rule} | {titles.get(rule, rule)} | {item['group']} | {item['tp']} | {item['fp']} | {item['fn']} | {item['tn']} | {item['precision']:.2%} | {item['recall']:.2%} |"
        )
    lines += [
        "",
        "## 2. 关键规则召回",
        "",
        "| 关键项 | 规则 | 召回 |",
        "|---|---|---|",
    ]
    for rule, label in KEY_RULES.items():
        lines.append(f"| {label} | {rule} | {metrics['key_recall'][rule]:.2%} |")
    overall = metrics["overall"]
    lines += [
        "",
        "## 3. 总体指标",
        "",
        f"- 样本：{metrics['draft_cases']} 个草稿用例 + {metrics.get('r06_cases', 0)} 个结论变异 + {metrics['record_cases']} 个记录用例",
        f"- 总体 precision：{overall['precision']:.2%}",
        f"- 总体 recall：{overall['recall']:.2%}",
        f"- 总体 accuracy：{overall['accuracy']:.2%}",
        f"- 四条关键规则召回 100%：{'通过' if metrics['gate_ok'] else '未通过'}",
        f"- 记录来源：`{Path(records_path).name}`",
        "",
        "## 4. 未达项与数据需求",
        "",
        "- 缺 20–30 份正式报告（含签章/照片/最终版）作为独立金标准；当前指标不能宣称通过客户验收。",
        "- T105 仍缺 XG 变更报告 2–3 份、第二主机厂报告 5 份+，模板识别未覆盖。",
        "- 视觉类（照片重复、设备标签、印章位置）未接入模型，仍标「未自动审核」。",
        "",
        "## 5. 复现",
        "",
        "```bash",
        "cd p0",
        "python -m eval.stage1_metrics --samples ../samples/byd \\",
        "  --records \"/path/to/(散发四项报告 比亚迪)草本出具及审核视图.xlsx\" \\",
        "  --out ../docs/eval-stage1.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 基线指标")
    parser.add_argument("--samples", default=str(Path(__file__).resolve().parents[2] / "samples" / "byd"))
    parser.add_argument("--records", required=True)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "docs" / "eval-stage1.md"))
    args = parser.parse_args()
    metrics = compute_metrics(args.samples, load_records(args.records))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(metrics, args.records), encoding="utf-8")
    print(json.dumps({"overall": metrics["overall"], "key_recall": metrics["key_recall"], "gate_ok": metrics["gate_ok"]}, ensure_ascii=False))
    return 0 if metrics["gate_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
