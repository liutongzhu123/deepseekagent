from app.docx_parser import ReportData
from app.rules import RecordInput, audit_report, validate_records


def data(**kwargs) -> ReportData:
    base = dict(
        file_name="sample.docx",
        cover_number="QA17LX1LDT861",
        header_numbers=["QA17LX1LDT861", "", "", "", ""],
        sample_name="XXXXX",
        model="XXXXX",
        supplier="XXXXX",
        receive_date="2024年02月24日",
        production_date="2024年11月25日",
        sample_date="2025年03月16日",
        sample_qty="XX",
        sample_no="TS24LXJ8A-01",
        basis="Q/BYDQ-A1901.404—2025《汽车零部件气味性试验方法》方案2",
        item="气味",
        conclusion="经检验，该样品气味的检验结果不符合委托单位提供限值：气味等级≤3.3的要求。 签发日期:",
        scores=[4.0, 4.0, 3.5],
        reported_mean=None,
        limit=3.3,
        judgment="不符合",
        device_valid="2026-11-24",
        page_totals=[5, 5, 5, 5, 5],
        page_numbers=[1, 2, 3, 4, 5],
        notices=8,
        highlights=33,
        red_runs=8,
        external_images=27,
        embedded_images=0,
        signature_text="批准： 审核： 主检：",
        test_start="2025年03月02日",
        test_end="2025年03月03日",
        template_family="BYD-odor",
    )
    base.update(kwargs)
    return ReportData(**base)


def test_audit_reports_known_defects():
    result = audit_report(data())
    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert by_id["R01"]["status"] == "fail"  # body numbers blank -> but our synthetic has numbers
    # In this synthetic case the header is complete; adjust expectation below.
    assert by_id["R02"]["status"] == "fail"  # mean missing
    assert by_id["R04"]["status"] == "fail"  # sample dates out of order
    assert by_id["R09"]["status"] == "fail"
    assert by_id["R10"]["status"] == "fail"
    assert by_id["R08"]["status"] == "pass"


def test_header_blank():
    result = audit_report(data(header_numbers=["QA17LX1LDT861", "", "", "", ""]))
    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert by_id["R01"]["status"] == "fail"


def test_mean_correct():
    result = audit_report(data(reported_mean=3.8, highlights=0, red_runs=0, external_images=0, embedded_images=1))
    by_id = {item["rule_id"]: item for item in result["findings"]}
    assert by_id["R02"]["status"] == "pass"
    assert by_id["R09"]["status"] == "pass"
    assert by_id["R10"]["status"] == "pass"


def test_record_validation():
    records = [
        RecordInput(sample_no="A", limit=3.5, scores=[3.5, 4.0, 4.0], reported_mean=3.8, test_date="2026-01-08", eval_date="2026-01-08", judgment="不符合"),
        RecordInput(sample_no="B", limit=3.5, scores=[3.5, 3.5, 4.0], reported_mean=3.8, test_date="2026-01-08", eval_date="2026-01-07", judgment="符合"),
    ]
    result = validate_records(records)
    ids = [item["rule_id"] for item in result["findings"]]
    assert "REC-01" in ids  # B mean mismatch
    assert "REC-02" in ids  # B eval before test
    assert "REC-03" in ids  # B judgment mismatch
