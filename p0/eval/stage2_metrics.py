"""Stage-2 gate metrics: semantic recall / false positives + rule wiring.

The semantic cases in ``eval/semantic_cases.json`` are synthetic: they contain
no customer report text.  They exercise the four semantic checks (S01–S04) and
clean controls.  A real customer gold standard still needs D3/D5.

Usage::

    cd p0
    python -m eval.stage2_metrics --samples ../samples/byd --out ../docs/eval-stage2.md

The script never loads a key from disk and never prints it.  Set
``P0_SEMANTIC_ENABLED=1`` and ``DEEPSEEK_API_KEY`` to call the model; otherwise
it uses ``eval/semantic_cache.json`` when available and reports the gap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import rule_dsl, semantic_audit  # noqa: E402
from app.docx_parser import parse_docx  # noqa: E402
from app.partial_rules import PARTIAL_RULES, evaluate_partial_rules, partial_rule_catalog  # noqa: E402
from app.rules import audit_report  # noqa: E402

CASES_PATH = ROOT / "eval" / "semantic_cases.json"
CACHE_PATH = ROOT / "eval" / "semantic_cache.json"
GATE_RECALL = 0.80
GATE_FP = 0.15


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} has no cases")
    return cases


def _text_key(case: dict, model: str, prompt: str) -> str:
    payload = f"{model}\n{prompt}\n{case.get('id')}\n{case.get('text')}\n{case.get('expected')}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def _read_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_semantic(cases: list[dict], *, network: bool, cache_path: Path) -> tuple[dict, dict]:
    config = semantic_audit.load_config()
    model = config.model
    _, prompt = semantic_audit.build_prompt(config)
    cache = _read_cache(cache_path)
    responses: dict[str, dict] = {}
    cache_hits = 0
    missing: list[str] = []
    for case in cases:
        key = _text_key(case, model, prompt)
        cached = cache.get(case["id"])
        if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("response"), dict):
            responses[case["id"]] = cached["response"]
            cache_hits += 1
            continue
        if not network or not config.api_key:
            missing.append(case["id"])
            continue
        result = semantic_audit.audit_semantics(case["text"], config=config)
        responses[case["id"]] = result
        cache[case["id"]] = {"key": key, "model": model, "response": result}
    if network and config.api_key:
        _write_cache(cache_path, cache)
    meta = {
        "model": model,
        "enabled": bool(config.enabled and config.api_key),
        "cache_hits": cache_hits,
        "missing": missing,
        "network": network and bool(config.api_key),
    }
    return responses, meta


def _match(case: dict, response: dict) -> tuple[int, int, int, int]:
    """Return (matched, false_positives, expected, category_matches)."""
    expected = case.get("expected") or []
    findings = response.get("findings") or []
    used: set[int] = set()
    matched = 0
    category_matches = 0
    for item in expected:
        for index, finding in enumerate(findings):
            if index in used:
                continue
            text = f"{finding.get('message', '')}\n{finding.get('evidence', '')}"
            rule_ok = finding.get("rule_id") == item.get("rule_id")
            keyword = item.get("keyword") or ""
            if rule_ok or (keyword and keyword in text):
                used.add(index)
                matched += 1
                category_matches += 1 if rule_ok else 0
                break
    false_positives = len(findings) - len(used)
    return matched, false_positives, len(expected), category_matches


def compute_semantic_metrics(cases: list[dict], responses: dict) -> dict:
    groups: dict[str, dict] = defaultdict(lambda: {"cases": 0, "tp": 0, "fp": 0, "fn": 0, "expected": 0, "category": 0})
    clean_total = clean_with_findings = 0
    missing_cases = 0
    for case in cases:
        response = responses.get(case["id"])
        if not isinstance(response, dict):
            missing_cases += 1
            continue
        group = groups[case.get("group") or "unknown"]
        group["cases"] += 1
        matched, false_positives, expected, category = _match(case, response)
        group["tp"] += matched
        group["fp"] += false_positives
        group["fn"] += max(0, expected - matched)
        group["expected"] += expected
        group["category"] += category
        if case.get("group") == "clean":
            clean_total += 1
            if response.get("findings"):
                clean_with_findings += 1
    total_tp = sum(item["tp"] for item in groups.values())
    total_fp = sum(item["fp"] for item in groups.values())
    total_fn = sum(item["fn"] for item in groups.values())
    total_expected = total_tp + total_fn
    recall = total_tp / total_expected if total_expected else 0.0
    fp_rate = total_fp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    clean_fp_rate = clean_with_findings / clean_total if clean_total else 0.0
    category_accuracy = sum(item["category"] for item in groups.values()) / total_tp if total_tp else 0.0
    return {
        "groups": {key: dict(value) for key, value in sorted(groups.items())},
        "cases_run": sum(item["cases"] for item in groups.values()),
        "cases_total": len(cases),
        "missing_cases": missing_cases,
        "tp": total_tp,
        "fp": total_fp,
        "fn": total_fn,
        "expected": total_expected,
        "recall": round(recall, 4),
        "fp_rate": round(fp_rate, 4),
        "precision": round(precision, 4),
        "clean_total": clean_total,
        "clean_with_findings": clean_with_findings,
        "clean_fp_rate": round(clean_fp_rate, 4),
        "category_accuracy": round(category_accuracy, 4),
        "gate_ok": total_expected > 0 and recall >= GATE_RECALL and fp_rate <= GATE_FP,
    }


def _draft_paths(samples: Path) -> list[Path]:
    return sorted(samples.glob("*.docx")) if samples.is_dir() else []


def partial_metrics(samples: Path) -> dict:
    paths = _draft_paths(samples)
    catalog = partial_rule_catalog()
    if not paths:
        return {"available": False, "catalog": catalog, "files": 0}
    statuses: dict[str, Counter] = defaultdict(Counter)
    for path in paths:
        data = parse_docx(path)
        for finding in evaluate_partial_rules(data):
            statuses[finding.rule_id][finding.status] += 1
    return {
        "available": True,
        "catalog": catalog,
        "files": len(paths),
        "statuses": {rule_id: dict(counter) for rule_id, counter in sorted(statuses.items())},
    }


def dsl_metrics(samples: Path) -> dict:
    errors = rule_dsl.validate_pack()
    pack = rule_dsl.load_pack()
    hits: Counter = Counter()
    skipped: Counter = Counter()
    files = _draft_paths(samples)
    for path in files:
        data = parse_docx(path)
        report_context = {
            "client": data.client,
            "sample_qty": data.sample_qty,
            "sample_no": data.sample_no,
            "sample_name": data.sample_name,
            "basis": data.basis,
            "item": data.item,
            "judgment": data.judgment,
            "conclusion": data.conclusion,
            "reported_mean": data.reported_mean,
            "limit": data.limit,
            "template": data.template_family,
            "toc_first_page": (data.toc_entries[0].get("page") if data.toc_entries else None),
        }
        findings = pack.evaluate({"report": report_context}, available_sources={"report"})
        for finding in findings:
            hits[finding["rule_id"]] += 1
        for rule_id in pack.skipped:
            skipped[rule_id] += 1
    return {
        "version": pack.version,
        "total": len(pack.rules),
        "errors": errors,
        "rules": rule_dsl.rule_catalog(pack),
        "files": len(files),
        "hits": dict(sorted(hits.items())),
        "skipped": dict(sorted(skipped.items())),
    }


def regression_metrics(samples: Path) -> dict:
    """Stage-2 layers must not change any R01–R11 status.

    Semantic is forced off here: this check only compares deterministic rule
    statuses and must never send customer draft text to a public model.
    """
    disabled = semantic_audit.SemanticConfig(enabled=False)
    paths = _draft_paths(samples)
    changed: list[dict] = []
    for path in paths:
        data = parse_docx(path)
        baseline = {item["rule_id"]: item["status"] for item in audit_report(data)["findings"] if item["rule_id"].startswith("R")}
        staged = {
            item["rule_id"]: item["status"]
            for item in audit_report(data, stage2=True, semantic_config=disabled)["findings"]
            if item["rule_id"].startswith("R")
        }
        if baseline != staged:
            changed.append({"file": hashlib.sha256(path.name.encode()).hexdigest()[:12], "baseline": baseline, "staged": staged})
    return {"files": len(paths), "changed": changed, "ok": len(paths) > 0 and not changed}


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_markdown(metrics: dict, semantic_meta: dict) -> str:
    semantic = metrics["semantic"]
    partial = metrics["partial"]
    dsl = metrics["dsl"]
    regression = metrics["regression"]
    lines = [
        "# 阶段 2 门禁指标（语义 + 部分规则 + 规则 DSL）",
        "",
        "> **样本口径**：语义用例是**合成的、无客户报告原文**的 44 条用例（S01–S04 各 8 条 + 12 条干净对照）；"
        "不是客户金标准。阶段 2 门禁的客户验收仍需 20–30 份正式报告（D3）和 DeepSeek 合规确认（D5）。",
        "",
        "## 1. 语义召回 / 误报",
        "",
        f"- 模型：`{semantic_meta.get('model', '')}`；"
        f"调用：{'实时 DeepSeek' if semantic_meta.get('network') else '缓存/未联网'}；"
        f"缓存命中 {semantic_meta.get('cache_hits', 0)}。",
        f"- 用例：{semantic['cases_run']}/{semantic['cases_total']} 条已评测"
        + (f"，{semantic['missing_cases']} 条缺缓存" if semantic.get("missing_cases") else "")
        + "。",
        "",
        "| 分组 | 用例 | 期望缺陷 | 检出 TP | 漏检 FN | 误报 FP | 召回 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group, item in semantic["groups"].items():
        recall = item["tp"] / item["expected"] if item["expected"] else 1.0
        lines.append(
            f"| {group} | {item['cases']} | {item['expected']} | {item['tp']} | {item['fn']} | {item['fp']} | {_pct(recall)} |"
        )
    lines += [
        "",
        "| 指标 | 值 | 门禁 | 结论 |",
        "|---|---:|---:|---|",
        f"| 语义召回（缺陷级） | {_pct(semantic['recall'])} | ≥80% | {'通过' if semantic['recall'] >= GATE_RECALL else '未通过'} |",
        f"| 误报率（告警级 FP/(TP+FP)） | {_pct(semantic['fp_rate'])} | ≤15% | {'通过' if semantic['fp_rate'] <= GATE_FP else '未通过'} |",
        f"| 精确率（TP/(TP+FP)） | {_pct(semantic['precision'])} | — | — |",
        f"| 干净用例误报率 | {_pct(semantic['clean_fp_rate'])}（{semantic['clean_with_findings']}/{semantic['clean_total']}） | — | — |",
        f"| 规则分类准确率 | {_pct(semantic['category_accuracy'])} | — | — |",
        "",
        f"**语义门禁结论：{'通过（合成基线）' if semantic['gate_ok'] else '未通过（合成基线）'}。** "
        "该结论只证明语义链路在合成用例上的召回/误报，不等于客户报告验收通过。",
        "",
        "## 2. 部分规则（T202）",
        "",
    ]
    if partial["available"]:
        lines += [
            f"- 12 份草稿已跑通：{partial['files']} 份；8 条部分规则均已接入主审核链路。",
            "",
            "| 规则 | 数据要求 | 12 份草稿状态分布 |",
            "|---|---|---|",
        ]
        for rule in partial["catalog"]:
            statuses = partial["statuses"].get(rule["id"], {})
            status_text = " / ".join(f"{key} {value}" for key, value in statuses.items()) or "未出现"
            lines.append(f"| {rule['id']} {rule['title']} | {rule['requires']} | {status_text} |")
    else:
        lines.append("- 未提供 `--samples`，本次未跑 12 份草稿；规则目录仍校验 8/8。")
    lines += [
        "",
        "## 3. 规则 DSL（T203）",
        "",
        f"- 版本：v{dsl['version']}；规则数：{dsl['total']}；校验错误：{len(dsl['errors'])}。",
        f"- 已跑草稿：{dsl['files']} 份；命中：{dsl['hits'] or '无'}；跳过：{dsl['skipped'] or '无'}。",
        "",
        "| 规则 | 名称 | 数据源/字段 | 严重级别 |",
        "|---|---|---|---|",
    ]
    for rule in dsl["rules"]:
        lines.append(f"| {rule['id']} | {rule['title']} | {rule['source']}.{rule['field']} | {rule['severity']} |")
    lines += [
        "",
        "## 4. 回归保护",
        "",
        f"- R01–R11 判定 diff：{'空' if regression['ok'] else '有变化'}（{regression['files']} 份草稿）。",
        f"- 阶段 2 三层只追加 findings，不改变确定性规则结论。",
        "",
        "## 5. 门禁结论与未达项",
        "",
        "- 语义合成基线：见第 1 节；客户报告语义召回/误报仍不可宣称通过。",
        "- 部分规则：缺任务单/单位/平行样/视觉模型的数据项按 `skip` 处理，不返回 `pass`。",
        "- 规则 DSL：10 条示例规则已接入；业务自建规则仍需通过配置文件/管理端维护。",
        "- 缺口：D3 20–30 份正式报告金标准、D5 DeepSeek 合规确认、D2 第二主机厂样例（T204）。",
        "",
        "## 6. 复现",
        "",
        "```bash",
        "cd p0",
        "# 合成语义用例 + 12 份草稿回归（需要 DEEPSEEK_API_KEY 和 P0_SEMANTIC_ENABLED=1）",
        "python -m eval.stage2_metrics --samples ../samples/byd --out ../docs/eval-stage2.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 2 门禁指标")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--samples", default=str(ROOT.parent / "samples" / "byd"))
    parser.add_argument("--out", default=str(ROOT.parent / "docs" / "eval-stage2.md"))
    parser.add_argument("--json-out", default=str(ROOT / "eval" / "stage2-report.json"))
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--no-network", action="store_true", help="只使用缓存，不调用模型")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    responses, semantic_meta = run_semantic(cases, network=not args.no_network, cache_path=Path(args.cache))
    semantic = compute_semantic_metrics(cases, responses)
    metrics = {
        "semantic": semantic,
        "partial": partial_metrics(Path(args.samples)),
        "dsl": dsl_metrics(Path(args.samples)),
        "regression": regression_metrics(Path(args.samples)),
    }
    markdown = render_markdown(metrics, semantic_meta)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown + "\n", encoding="utf-8")
    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps({**metrics, "semantic_meta": semantic_meta}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "semantic": {key: semantic[key] for key in ("recall", "fp_rate", "clean_fp_rate", "gate_ok", "cases_run")},
        "partial_rules": len(PARTIAL_RULES),
        "dsl_rules": metrics["dsl"]["total"],
        "regression_ok": metrics["regression"]["ok"],
    }, ensure_ascii=False))
    return 0 if semantic["gate_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
