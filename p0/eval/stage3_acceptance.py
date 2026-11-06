"""Stage-3 pilot acceptance harness: manifest + gold standard -> metrics.

The data contract follows ``docs/stage3-acceptance/templates/data-manifest.csv``:
every D3 row carries the masked report id, A/B/C class, expected judgment and
expected ``fail`` rule set.  The harness audits the matching file and produces
the per-report records plus the 3-class gate summary required by
``docs/stage3-acceptance/README.md`` §3–4.

The harness never invents numbers:

- a missing file, an unreadable report or an incomplete manifest makes the
  verdict ``blocked`` (exit code 2) and lists every gap;
- ``case_kind=synthetic`` rows are dry-run only and are excluded from the
  acceptance sample count;
- a key rule (R01/R04/R06) that returns ``skip`` marks the report as a data
  gap instead of a pass;
- the visual rule P06 never counts as ``pass`` without manual review.

Usage::

    cd p0
    python -m eval.stage3_acceptance \
      --manifest ../samples/stage3/manifest.csv \
      --samples ../samples/stage3 \
      --out ../docs/stage3-acceptance/acceptance-2026-11-20.md

The customer files stay outside the repository; only masked ids and metrics
are written to the output.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.docx_parser import ReportData, parse_docx  # noqa: E402
from app.rules import audit_report  # noqa: E402

MANIFEST_COLUMNS = (
    "data_id",
    "dataset",
    "class",
    "masked_id",
    "file_name",
    "expected_judgment",
    "expected_fail_rules",
    "case_kind",
)
ACCEPTANCE_DATASET = "D3"
ALLOWED_CLASSES = ("A", "B", "C")
CASE_KINDS = ("defect", "clean", "synthetic")
SAMPLE_KINDS = ("defect", "clean")
# A/B are the shipped BYD-odor variants; C needs the xg_change template that
# only D1 can define.  Until then a C row can never satisfy the template gate.
EXPECTED_TEMPLATES = {
    "A": ("BYD-odor", "OTS"),
    "B": ("BYD-odor", "摸底"),
    "C": ("XG", ""),
}
KEY_RULES = ("R01", "R04", "R06")
MAX_SECONDS = 60.0
GATE_REPORT_ACCURACY = 0.95
GATE_RECALL = 0.90
GATE_MIN_SAMPLES = 20
GATE_MAX_SAMPLES = 30


class ManifestError(ValueError):
    """The data manifest cannot be used for acceptance."""


@dataclass(frozen=True)
class Case:
    """One acceptance sample row from the manifest."""

    data_id: str
    report_class: str
    masked_id: str
    file_name: str
    expected_judgment: str
    expected_fail: frozenset[str]
    case_kind: str
    pages: int | None = None

    @property
    def counts_toward_gate(self) -> bool:
        return self.case_kind in SAMPLE_KINDS


@dataclass
class Outcome:
    """Audit result for one case, or the reason it could not be audited."""

    case: Case
    status: str  # ok | missing | error
    actual_fail: frozenset[str] = frozenset()
    rule_status: dict[str, str] = field(default_factory=dict)
    judgment: str = ""
    template: str = ""
    variant: str = ""
    seconds: float = 0.0
    gap: str = ""
    gaps: tuple[str, ...] = ()

    @property
    def set_matches(self) -> bool:
        return self.status == "ok" and self.actual_fail == self.case.expected_fail

    @property
    def conclusion(self) -> str:
        if self.status != "ok":
            return "未验收"
        if not self.set_matches:
            return "不通过"
        return "有条件通过" if self.gaps else "通过"


def _norm_rules(raw: str) -> frozenset[str]:
    for separator in ("；", "，", ","):
        raw = raw.replace(separator, ";")
    return frozenset(part.strip().upper() for part in raw.split(";") if part.strip())


def _read_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [name.strip() for name in (reader.fieldnames or [])]
        missing = [name for name in MANIFEST_COLUMNS if name not in headers]
        if missing:
            raise ManifestError(f"清单缺少列：{', '.join(missing)}")
        rows = []
        for raw in reader:
            normalized = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
            rows.append({name: normalized.get(name, "") for name in headers})
    return rows


def load_manifest(path: str | Path) -> list[Case]:
    """Parse and validate the D3 acceptance rows of a data manifest."""
    path = Path(path)
    if not path.is_file():
        raise ManifestError(f"清单不存在：{path}")
    cases: list[Case] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(_read_rows(path), start=2):
        if row["dataset"].upper() != ACCEPTANCE_DATASET:
            continue
        where = f"第 {index} 行"
        data_id = row["data_id"]
        if not data_id:
            raise ManifestError(f"{where} 缺少 data_id")
        if data_id in seen_ids:
            raise ManifestError(f"{where} data_id 重复：{data_id}")
        seen_ids.add(data_id)
        report_class = row["class"].upper()
        if report_class not in ALLOWED_CLASSES:
            raise ManifestError(f"{where} class 必须是 {'/'.join(ALLOWED_CLASSES)}，实际为 {row['class']!r}")
        case_kind = row["case_kind"].lower()
        if case_kind not in CASE_KINDS:
            raise ManifestError(f"{where} case_kind 必须是 {'/'.join(CASE_KINDS)}，实际为 {row['case_kind']!r}")
        if not row["file_name"]:
            raise ManifestError(f"{where} 缺少 file_name")
        expected_fail = _norm_rules(row["expected_fail_rules"])
        if case_kind == "clean" and expected_fail:
            raise ManifestError(f"{where} clean 样本不得填写 expected_fail_rules")
        if case_kind == "defect" and not expected_fail:
            raise ManifestError(f"{where} defect 样本必须填写 expected_fail_rules")
        pages = None
        if row.get("pages"):
            try:
                pages = int(row["pages"])
            except ValueError as exc:
                raise ManifestError(f"{where} pages 不是整数：{row['pages']!r}") from exc
        cases.append(
            Case(
                data_id=data_id,
                report_class=report_class,
                masked_id=row["masked_id"] or data_id,
                file_name=row["file_name"],
                expected_judgment=row["expected_judgment"],
                expected_fail=expected_fail,
                case_kind=case_kind,
                pages=pages,
            )
        )
    if not cases:
        raise ManifestError("清单中没有 dataset=D3 的验收样本")
    return cases


def load_report(path: Path) -> ReportData:
    """Dispatch docx/pdf through the same ReportData shape as the product."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".pdf":
        from app.report_pdf import parse_pdf_report  # lazy: OCR deps are optional locally

        return parse_pdf_report(path)
    raise ValueError(f"不支持的报告格式：{suffix}")


def _data_gaps(data: ReportData, rule_status: dict[str, str]) -> tuple[str, ...]:
    """Data gaps that must not be reported as a pass."""
    gaps: list[str] = []
    for rule_id in KEY_RULES:
        if rule_status.get(rule_id) == "skip":
            gaps.append(f"关键规则 {rule_id} skip（数据缺口，不计通过）")
    if not (data.cover_number or any(str(item).strip() for item in data.header_numbers)):
        if rule_status.get("R01") == "pass":
            gaps.append("R01 编号字段为空却判 pass（缺数据沉默通过）")
    if not (data.test_start and data.test_end and data.approval_date):
        if rule_status.get("R04") == "pass":
            gaps.append("R04 日期字段不完整却判 pass（缺数据沉默通过）")
    if not data.judgment and rule_status.get("R06") == "pass":
        gaps.append("R06 判定字段为空却判 pass（缺数据沉默通过）")
    if rule_status.get("P06") == "pass":
        gaps.append("P06 视觉项在无人工复核时不得 pass")
    return tuple(gaps)


def run_case(case: Case, samples_dir: str | Path, inputs: object | None = None) -> Outcome:
    """Audit one case; never raises for a report-specific failure."""
    path = Path(samples_dir) / case.file_name
    if not path.is_file():
        return Outcome(case=case, status="missing", gap=f"文件未找到：{case.file_name}")
    started = time.perf_counter()
    try:
        data = load_report(path)
        result = audit_report(data, {}, inputs=inputs, stage2=True)
    except Exception as exc:  # noqa: BLE001 - report-level failures become gaps
        return Outcome(case=case, status="error", gap=f"{type(exc).__name__}: {exc}")
    elapsed = time.perf_counter() - started
    rule_status = {item["rule_id"]: item["status"] for item in result.get("findings", [])}
    value = result.get("data") or {}
    actual_fail = frozenset(rule for rule, status in rule_status.items() if status == "fail")
    return Outcome(
        case=case,
        status="ok",
        actual_fail=actual_fail,
        rule_status=rule_status,
        judgment=str(value.get("judgment") or data.judgment),
        template=str(value.get("template") or data.template_family),
        variant=str(data.template_variant),
        seconds=round(elapsed, 3),
        gaps=_data_gaps(data, rule_status),
    )


def _empty_group() -> dict:
    return {
        "samples": 0,
        "correct": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "key_tp": 0,
        "key_fn": 0,
        "clean_samples": 0,
        "clean_fp": 0,
        "template_checked": 0,
        "template_ok": 0,
        "gapped": 0,
        "max_seconds": 0.0,
        "seconds": [],
    }


def compute_metrics(cases: list[Case], outcomes: list[Outcome]) -> dict:
    """Aggregate TP/FP/FN and gate inputs per A/B/C class."""
    groups: dict[str, dict] = {key: _empty_group() for key in ALLOWED_CLASSES}
    missing = [item for item in outcomes if item.status != "ok"]
    for outcome in outcomes:
        if outcome.status != "ok" or not outcome.case.counts_toward_gate:
            continue
        group = groups[outcome.case.report_class]
        expected = outcome.case.expected_fail
        actual = outcome.actual_fail
        group["samples"] += 1
        if outcome.set_matches:
            group["correct"] += 1
        group["tp"] += len(expected & actual)
        group["fp"] += len(actual - expected)
        group["fn"] += len(expected - actual)
        for rule_id in KEY_RULES:
            if rule_id in expected:
                if rule_id in actual:
                    group["key_tp"] += 1
                else:
                    group["key_fn"] += 1
        if outcome.case.case_kind == "clean":
            group["clean_samples"] += 1
            group["clean_fp"] += len(actual)
        expected_family, expected_variant = EXPECTED_TEMPLATES[outcome.case.report_class]
        group["template_checked"] += 1
        if outcome.template == expected_family and outcome.variant == expected_variant:
            group["template_ok"] += 1
        if outcome.gaps:
            group["gapped"] += 1
        group["max_seconds"] = max(group["max_seconds"], outcome.seconds)
        group["seconds"].append(outcome.seconds)
    total = _empty_group()
    for key in ALLOWED_CLASSES:
        group = groups[key]
        for field_name in ("samples", "correct", "tp", "fp", "fn", "key_tp", "key_fn",
                           "clean_samples", "clean_fp", "template_checked", "template_ok", "gapped"):
            total[field_name] += group[field_name]
        total["max_seconds"] = max(total["max_seconds"], group["max_seconds"])
        total["seconds"].extend(group["seconds"])
    total["groups"] = groups
    total["missing"] = [item.case.data_id for item in missing if item.status == "missing"]
    total["errors"] = [
        {"data_id": item.case.data_id, "error": item.gap} for item in missing if item.status == "error"
    ]
    total["missing_files"] = [
        {"data_id": item.case.data_id, "file_name": item.case.file_name, "reason": item.gap}
        for item in missing
        if item.status == "missing"
    ]
    total["gaps"] = [
        {"data_id": item.case.data_id, "gaps": list(item.gaps)}
        for item in outcomes
        if item.gaps and item.case.counts_toward_gate
    ]
    total["excluded_synthetic"] = sum(1 for case in cases if not case.counts_toward_gate)
    total["silent_pass"] = sum(
        1 for item in total["gaps"] for gap in item["gaps"] if "缺数据沉默通过" in gap
    )
    total["visual_pass"] = sum(1 for item in total["gaps"] for gap in item["gaps"] if "P06 视觉项" in gap)
    total["gapped_reports"] = sum(1 for item in total["gaps"] if item["gaps"])
    return total


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _group_view(group: dict) -> dict:
    return {
        "samples": group["samples"],
        "accuracy": _ratio(group["correct"], group["samples"]),
        "recall": _ratio(group["tp"], group["tp"] + group["fn"]),
        "key_recall": _ratio(group["key_tp"], group["key_tp"] + group["key_fn"]),
        "clean_fp": group["clean_fp"],
        "template_accuracy": _ratio(group["template_ok"], group["template_checked"]),
        "max_seconds": round(group["max_seconds"], 3),
        "gapped": group["gapped"],
    }


def evaluate_gate(metrics: dict) -> dict:
    """Turn the aggregate metrics into the README §3.2 gate table."""
    total = metrics["samples"]
    accuracy = _ratio(metrics["correct"], total)
    recall = _ratio(metrics["tp"], metrics["tp"] + metrics["fn"])
    key_recall = _ratio(metrics["key_tp"], metrics["key_tp"] + metrics["key_fn"])
    template_accuracy = _ratio(metrics["template_ok"], metrics["template_checked"])
    classes_present = all(metrics["groups"][key]["samples"] > 0 for key in ALLOWED_CLASSES)
    has_samples = total > 0
    checks = [
        {
            "name": "正式报告数量",
            "actual": f"{total} 份",
            "requirement": f"{GATE_MIN_SAMPLES}–{GATE_MAX_SAMPLES} 份",
            "ok": GATE_MIN_SAMPLES <= total <= GATE_MAX_SAMPLES,
        },
        {
            "name": "3 类报告",
            "actual": "A/B/C 均有样本" if classes_present else "缺类别",
            "requirement": "A/B/C 均通过",
            "ok": classes_present,
        },
        {
            "name": "报告级准确率",
            "actual": _pct(accuracy),
            "requirement": "≥95%",
            "ok": accuracy is not None and accuracy >= GATE_REPORT_ACCURACY,
        },
        {
            "name": "finding 召回",
            "actual": _pct(recall),
            "requirement": "≥90%",
            "ok": recall is not None and recall >= GATE_RECALL,
        },
        {
            "name": "关键规则召回",
            "actual": _pct(key_recall),
            "requirement": "R01/R04/R06 100%",
            "ok": key_recall == 1.0,
        },
        {
            "name": "模板分类",
            "actual": _pct(template_accuracy),
            "requirement": "100%",
            "ok": template_accuracy == 1.0,
        },
        {
            "name": "干净报告误报",
            "actual": f"{metrics['clean_fp']}（{metrics['clean_samples']} 份干净报告）",
            "requirement": "0",
            "ok": has_samples and metrics["clean_fp"] == 0,
        },
        {
            "name": "单份耗时",
            "actual": f"{metrics['max_seconds']:.1f}s" if has_samples else "—",
            "requirement": "≤60s",
            "ok": has_samples and metrics["max_seconds"] <= MAX_SECONDS,
        },
        {
            "name": "缺数据沉默通过",
            "actual": str(metrics["silent_pass"]),
            "requirement": "0",
            "ok": has_samples and metrics["silent_pass"] == 0,
        },
        {
            "name": "视觉项冒充通过",
            "actual": str(metrics["visual_pass"]),
            "requirement": "0",
            "ok": has_samples and metrics["visual_pass"] == 0,
        },
    ]
    missing_gap = bool(metrics["missing"] or metrics["errors"])
    sample_gap = total < GATE_MIN_SAMPLES or not classes_present
    if missing_gap or sample_gap:
        verdict = "blocked"
    elif all(item["ok"] for item in checks):
        verdict = "通过"
    else:
        verdict = "不通过"
    return {
        "verdict": verdict,
        "checks": checks,
        "semantic": {
            "status": "not_evaluated",
            "note": "语义缺陷级金标准不在清单中；D5 未确认时语义项不计入通过",
        },
        "reasons": [item["name"] for item in checks if not item["ok"]],
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def render_markdown(
    metrics: dict,
    gate: dict,
    cases: list[Case],
    outcomes: list[Outcome],
    *,
    manifest: str,
    samples_dir: str,
    now: str,
) -> str:
    groups = metrics["groups"]
    lines = [
        "# 阶段 3 验收记录（自动生成）",
        "",
        f"> 生成时间：{now}；清单：`{manifest}`；样例目录：`{samples_dir}`。",
        "> 只含假号与指标，不含客户原文；客户报告原文与签字件不入库。",
        "",
        "## 1. 结论",
        "",
        f"**{gate['verdict']}**",
        "",
    ]
    if gate["verdict"] == "blocked":
        lines += [
            f"样本不足或数据缺口：可用验收样本 {metrics['samples']} 份"
            f"（要求 {GATE_MIN_SAMPLES}–{GATE_MAX_SAMPLES} 份），"
            f"缺文件 {len(metrics['missing'])} 份，解析失败 {len(metrics['errors'])} 份。",
            "本结论为 blocked，不代表任何一类通过；补齐 D1/D3 后重跑。",
            "",
        ]
    elif gate["verdict"] == "不通过":
        lines += [f"未达门禁项：{'、'.join(gate['reasons']) or '见第 4 节'}。", ""]
    else:
        lines += ["3 类报告与各项门禁均满足；仍需客户业务专家复核并签字。", ""]

    lines += [
        "## 2. 数据概览",
        "",
        f"- 清单 D3 行：{len(cases)}；计入验收：{metrics['samples']}；合成/演练（不计入）：{metrics['excluded_synthetic']}。",
        f"- 缺文件：{len(metrics['missing'])}；解析失败：{len(metrics['errors'])}。",
        "",
    ]
    if metrics["missing_files"]:
        lines += ["| data_id | 文件 | 原因 |", "|---|---|---|"]
        for item in metrics["missing_files"]:
            lines.append(f"| {item['data_id']} | {item['file_name']} | {item['reason']} |")
        lines.append("")
    if metrics["errors"]:
        lines += ["| data_id | 错误 |", "|---|---|"]
        for item in metrics["errors"]:
            lines.append(f"| {item['data_id']} | {item['error']} |")
        lines.append("")

    lines += [
        "## 3. 逐份记录",
        "",
        "| data_id | 类别 | 类型 | 期望 fail | 实际 fail | 集合一致 | 判定 | 模板 | 耗时 | 单份结论 | 缺口 |",
        "|---|---|---|---|---|---|---|---|---:|---|---|",
    ]
    for outcome in outcomes:
        case = outcome.case
        if not case.counts_toward_gate:
            continue
        expected = ";".join(sorted(case.expected_fail)) or "—"
        actual = ";".join(sorted(outcome.actual_fail)) or "—"
        matched = "是" if outcome.set_matches else "否"
        template = f"{outcome.template}/{outcome.variant}" if outcome.template else "—"
        seconds = f"{outcome.seconds:.2f}" if outcome.status == "ok" else "—"
        gap = "；".join(outcome.gaps) if outcome.gaps else ("—" if outcome.status == "ok" else outcome.gap)
        lines.append(
            f"| {case.masked_id} | {case.report_class} | {case.case_kind} | {expected} | {actual} | "
            f"{matched} | {outcome.judgment or '—'} | {template} | {seconds} | {outcome.conclusion} | {gap} |"
        )
    lines.append("")

    lines += [
        "## 4. 3 类汇总",
        "",
        "| 类别 | 样本数 | 报告级准确率 | finding 召回 | R01/R04/R06 召回 | 干净报告误报 | 模板分类 | 最大耗时 | 数据缺口报告 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {"A": "A OTS", "B": "B 摸底", "C": "C XG 变更"}
    for key in ALLOWED_CLASSES:
        view = _group_view(groups[key])
        lines.append(
            f"| {labels[key]} | {view['samples']} | {_pct(view['accuracy'])} | {_pct(view['recall'])} | "
            f"{_pct(view['key_recall'])} | {view['clean_fp']} | {_pct(view['template_accuracy'])} | "
            f"{view['max_seconds']:.1f}s | {view['gapped']} |"
        )
    total_view = _group_view(metrics)
    lines.append(
        f"| 合计 | {total_view['samples']} | {_pct(total_view['accuracy'])} | {_pct(total_view['recall'])} | "
        f"{_pct(total_view['key_recall'])} | {total_view['clean_fp']} | {_pct(total_view['template_accuracy'])} | "
        f"{total_view['max_seconds']:.1f}s | {total_view['gapped']} |"
    )

    lines += [
        "",
        "## 5. 门禁核对",
        "",
        "| 门禁 | 要求 | 实际 | 是否满足 |",
        "|---|---|---|---|",
    ]
    for check in gate["checks"]:
        lines.append(f"| {check['name']} | {check['requirement']} | {check['actual']} | {'是' if check['ok'] else '否'} |")
    lines += [
        "| 语义召回（D5 开启） | ≥80% | 未评估 | 未启用/不计入 |",
        "| 语义误报（D5 开启） | ≤15% | 未评估 | 未启用/不计入 |",
        "| 客户签字 | 已签字 | — | 待客户 |",
        "",
        f"> 语义：{gate['semantic']['note']}。",
        "",
        "## 6. 缺口与未关闭问题",
        "",
    ]
    if metrics["gaps"]:
        lines += ["| data_id | 缺口 |", "|---|---|"]
        for item in metrics["gaps"]:
            lines.append(f"| {item['data_id']} | {'；'.join(item['gaps'])} |")
    else:
        lines.append("- 本次运行未发现缺数据沉默通过或视觉项冒充通过。")
    lines += [
        "",
        "- D1（XG 样例）、D2（第二主机厂）、D3（正式报告+金标准）、D4（四类输入）、D5（合规确认）到位前，",
        "  阶段 3 不得宣称通过，C 类不得计入 3 类通过。",
        "",
        "## 7. 复现",
        "",
        "```bash",
        "cd p0",
        f"python -m eval.stage3_acceptance --manifest {manifest} --samples {samples_dir}",
        "```",
        "",
    ]
    return "\n".join(lines)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(
    manifest: str | Path,
    samples_dir: str | Path,
    inputs: object | None = None,
) -> tuple[dict, dict, list[Case], list[Outcome]]:
    cases = load_manifest(manifest)
    outcomes = [run_case(case, samples_dir, inputs=inputs) for case in cases]
    metrics = compute_metrics(cases, outcomes)
    gate = evaluate_gate(metrics)
    return metrics, gate, cases, outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 3 试点验收：清单 + 金标准 → 指标与门禁")
    parser.add_argument("--manifest", required=True, help="data-manifest.csv 同列定义的数据清单")
    parser.add_argument("--samples", required=True, help="报告文件所在目录（仓库外）")
    parser.add_argument(
        "--out",
        default=str(ROOT.parent / "docs" / "stage3-acceptance" / f"acceptance-{date.today():%Y%m%d}.md"),
    )
    parser.add_argument("--json-out", default=str(ROOT / "eval" / "stage3-acceptance.json"))
    parser.add_argument("--inputs", default="", help="可选：D4 四类输入目录；未提供时 X/P 部分规则按 skip 计")
    args = parser.parse_args(argv)

    inputs = None
    if args.inputs:
        from app.report_inputs import load_inputs  # local import: optional data dependency

        try:
            inputs = load_inputs(args.inputs)
        except Exception as exc:  # noqa: BLE001 - missing D4 data must not fabricate a pass
            print(json.dumps({"verdict": "blocked", "error": f"输入目录不可用：{exc}"}, ensure_ascii=False))
            return 2

    try:
        metrics, gate, cases, outcomes = run(args.manifest, args.samples, inputs=inputs)
    except ManifestError as exc:
        print(json.dumps({"verdict": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2

    now = time.strftime("%Y-%m-%d %H:%M")
    markdown = render_markdown(
        metrics,
        gate,
        cases,
        outcomes,
        manifest=args.manifest,
        samples_dir=args.samples,
        now=now,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown + "\n", encoding="utf-8")
    _write_json(Path(args.json_out), {"generated_at": now, "manifest": args.manifest, **metrics, **gate})
    print(json.dumps({
        "verdict": gate["verdict"],
        "samples": metrics["samples"],
        "missing": len(metrics["missing"]),
        "errors": len(metrics["errors"]),
        "report_accuracy": _ratio(metrics["correct"], metrics["samples"]),
        "finding_recall": _ratio(metrics["tp"], metrics["tp"] + metrics["fn"]),
    }, ensure_ascii=False))
    if gate["verdict"] == "通过":
        return 0
    if gate["verdict"] == "不通过":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
