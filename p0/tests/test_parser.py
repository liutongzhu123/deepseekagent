import os
from pathlib import Path

import pytest

from app.docx_parser import parse_docx
from app.rules import audit_report


def sample_dir() -> Path:
    return Path(os.getenv("P0_SAMPLE_DIR", "/data"))


@pytest.mark.skipif(not (sample_dir() / "ots袋子-100L.docx").exists(), reason="real sample not mounted")
def test_parse_real_ots_report():
    data = parse_docx(sample_dir() / "ots袋子-100L.docx")
    assert data.cover_number == "QA17LX1LDT861"
    assert data.sample_no == "TS24LXJ8A-01"
    assert data.scores == [4.0, 4.0, 3.5]
    assert data.reported_mean is None
    assert data.limit == 3.3
    assert data.notices == 8
    assert [item["device_id"] for item in data.devices] == ["NJ.18.8.2431", "NJ.18.91.662"]
    assert data.devices[1]["device_name"] == "袋式法环境舱"
    assert data.embedded_images == 0
    assert data.external_images > 0
    assert data.highlights > 0
    assert data.red_runs > 0


@pytest.mark.skipif(not (sample_dir() / "ots袋子-100L.docx").exists(), reason="real sample not mounted")
def test_audit_real_ots_report():
    result = audit_report(parse_docx(sample_dir() / "ots袋子-100L.docx"))
    statuses = {item["rule_id"]: item["status"] for item in result["findings"]}
    assert statuses["R01"] == "fail"
    assert statuses["R02"] == "fail"
    assert statuses["R03"] == "fail"
    assert statuses["R04"] == "fail"
    assert statuses["R09"] == "fail"
    assert statuses["R10"] == "fail"
    assert statuses["R08"] == "pass"
    assert statuses["R07"] == "pass"
