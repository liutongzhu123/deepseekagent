"""R27 原始记录版本有效性（RA-4.7）。

失败场景 F1–F10 见 ``p0/app/format_rules.py``；验收两条：
引用过期版本号 -> fail；引用最新版本 -> pass（不报）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.docx_parser import ReportData, parse_docx
from app.rules import RULES, audit_report

SAMPLE_DIR = Path("/opt/dsh-agent/samples/byd")
EXPIRE_404 = "Q/BYDQ-A1901.404—2018《汽车零部件气味性试验方法》方案2"
LATEST_404 = "Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2"


def _r27(basis: str = "", paragraphs: list[str] | None = None) -> dict:
    data = ReportData(file_name="record.docx", basis=basis, paragraphs=paragraphs or [])
    return next(item for item in audit_report(data)["findings"] if item["rule_id"] == "R27")


def test_rule_is_in_catalog():
    assert (
        "R27",
        "原始记录版本有效性",
        "原始记录引用的检验依据标准版本应为最新有效版本；过期版本号报出，最新版本不报",
    ) in RULES


def test_expired_version_is_reported():
    finding = _r27(EXPIRE_404)

    assert finding["status"] == "fail"
    assert finding["severity"] == "medium"
    assert "Q/BYDQ-A1901.404-2018（最新 2025）" in finding["message"]
    assert "过期=1" in finding["evidence"]
    assert finding["location"]["section"] == "检验依据"


def test_latest_version_is_not_reported():
    finding = _r27(LATEST_404)

    assert finding["status"] == "pass"
    assert "最新有效版本" in finding["message"]
    assert "过期=0" in finding["evidence"]


def test_second_registered_standard_uses_its_own_latest_version():
    finding = _r27("Q/BYDQ-A1901.1373—2018《汽车零部件总成气味性试验方法》")

    assert finding["status"] == "pass"
    assert "可判定=1" in finding["evidence"]


def test_version_newer_than_registry_is_not_reported():
    finding = _r27("Q/BYDQ-A1901.404—2026《汽车零部件气味性试验方法》")

    assert finding["status"] == "pass"
    assert "过期=0" in finding["evidence"]


@pytest.mark.parametrize(
    "dash",
    ["—", "－", "–", "-", "―"],  # em dash / full-width / en dash / ascii / horizontal bar
)
def test_same_version_with_different_dashes_is_reported_once(dash):
    finding = _r27(f"Q/BYDQ-A1901.404{dash}2018《汽车零部件气味性试验方法》")

    assert finding["status"] == "fail"
    assert "引用标准=1; 可判定=1; 过期=1" in finding["evidence"]


def test_expired_and_latest_citation_only_reports_expired():
    finding = _r27(f"{EXPIRE_404}、{LATEST_404}")

    assert finding["status"] == "fail"
    assert "Q/BYDQ-A1901.404-2018（最新 2025）" in finding["message"]
    assert "引用标准=2" in finding["evidence"]
    assert "过期=1" in finding["evidence"]


def test_duplicate_citation_is_reported_once():
    finding = _r27(f"{EXPIRE_404}、{EXPIRE_404}")

    assert finding["status"] == "fail"
    assert "引用了 1 个过期版本号" in finding["message"]
    assert "引用标准=1" in finding["evidence"]


def test_mixed_known_and_unknown_standards_only_reports_expired():
    finding = _r27(f"{EXPIRE_404}、GB/T 1037-2021、Q/BYDQ-A1901.1373—2018")

    assert finding["status"] == "fail"
    assert "GB/T 1037" not in finding["message"]
    assert "Q/BYDQ-A1901.1373" not in finding["message"]
    assert "引用标准=3; 可判定=2; 过期=1; 未收录=1" in finding["evidence"]


def test_unknown_standard_is_skipped_not_reported():
    finding = _r27("GB/T 1037-2021、GB/T 2406-2009")

    assert finding["status"] == "skip"
    assert "不在最新有效版本表" in finding["message"]


def test_standard_without_year_is_skipped_not_reported():
    finding = _r27("Q/BYDQ-A1901.404《汽车零部件气味性试验方法》")

    assert finding["status"] == "skip"
    assert "无年代号" in finding["message"]
    assert "无年代号=1" in finding["evidence"]


def test_missing_basis_is_skipped():
    finding = _r27()

    assert finding["status"] == "skip"
    assert "未识别到检验依据" in finding["message"]


def test_no_standard_in_basis_is_skipped():
    finding = _r27("委托单位提供限值：气味等级≤3.3")

    assert finding["status"] == "skip"
    assert "未识别到标准号" in finding["message"]


def test_basis_label_paragraph_fallback():
    finding = _r27(paragraphs=["检验依据：" + EXPIRE_404])

    assert finding["status"] == "fail"
    assert "过期=1" in finding["evidence"]


def test_version_elsewhere_in_paragraphs_is_not_checked():
    finding = _r27(paragraphs=["备注：" + EXPIRE_404])

    assert finding["status"] == "skip"
    assert "未识别到检验依据" in finding["message"]


@pytest.mark.skipif(not SAMPLE_DIR.exists(), reason="真实草稿样例目录不存在")
def test_real_byd_drafts_have_no_expired_version():
    drafts = [path for path in sorted(SAMPLE_DIR.glob("*.docx")) if not path.name.startswith(".")]
    assert len(drafts) == 12
    statuses = {"pass": 0, "fail": 0, "skip": 0}
    for path in drafts:
        finding = next(
            item for item in audit_report(parse_docx(str(path)))["findings"] if item["rule_id"] == "R27"
        )
        statuses[finding["status"]] += 1
        assert "过期=0" in finding["evidence"]
    assert statuses == {"pass": 12, "fail": 0, "skip": 0}
