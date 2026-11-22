#!/usr/bin/env python3
"""Run X01-X04 against the repository's explicitly fictional demo records."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.docx_parser import ReportData
from app.report_inputs import load_inputs
from app.rules import audit_report


DEMO_INPUTS = ROOT / "demo" / "synthetic_report_inputs"


def main() -> int:
    """Print simulated cross-check findings; never contacts external systems."""
    snapshot = load_inputs(DEMO_INPUTS)
    report = ReportData(
        file_name="DEMO-RPT-2026-001.docx (simulated fields)",
        source_format="demo",
        cover_number="DEMO-RPT-2026-001",
        sample_no="DEMO-SMP-001",
        client="演示虚构客户",
        sample_name="车内气味测试片",
        sample_qty="1件",
        item="气味",
        test_start="2026-11-18",
        test_end="2026-11-18",
        devices=[{"device_id": "DEMO-EQ-001", "device_name": "演示环境舱"}],
    )
    result = audit_report(report, inputs=snapshot)
    findings = [item for item in result["findings"] if item["rule_id"].startswith("X")]

    print("模拟数据演示（虚构资料；不代表真实客户验收）")
    print(f"输入目录：{DEMO_INPUTS}")
    print(f"数据行数：{', '.join(f'{name}={count}' for name, count in snapshot.counts().items())}")
    for finding in findings:
        print(f"{finding['rule_id']} {finding['status'].upper()}: {finding['message']}")

    expected = {"X01", "X02", "X03", "X04"}
    passed = {item["rule_id"] for item in findings if item["status"] == "pass"}
    return 0 if not snapshot.errors and passed == expected and len(findings) == len(expected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
