from pathlib import Path
from types import SimpleNamespace

from app import report_pdf
from app.rules import audit_report


def _text_pdf(path: Path, text: str = "Report No: QA-001") -> None:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def _statuses(result: dict) -> dict[str, str]:
    return {item["rule_id"]: item["status"] for item in result["findings"]}


def test_text_pdf_runs_rules_with_skip_not_pass(tmp_path):
    path = tmp_path / "report.pdf"
    _text_pdf(path)

    data = report_pdf.parse_pdf_report(path)
    statuses = _statuses(audit_report(data))

    assert data.source_format == "pdf"
    assert data.page_numbers == [1]
    for rule in ("R01", "R02", "R03", "R04"):
        assert statuses[rule] == "skip"


def test_scanned_pdf_uses_ocr_fallback(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    class FakeOcr:
        def page_texts(self):
            return ["扫描件文本，无报告编号"]

        def page_confidences(self):
            return [0.91]

    monkeypatch.setattr(report_pdf.pdf_text, "extract_pages", lambda data: [""])
    monkeypatch.setattr(report_pdf.ocr, "ocr_pdf", lambda data: FakeOcr())

    data = report_pdf.parse_pdf_report(path)
    statuses = _statuses(audit_report(data))

    assert data.source_format == "ocr"
    assert data.paragraphs == ["扫描件文本，无报告编号"]
    for rule in ("R01", "R02", "R03", "R04"):
        assert statuses[rule] == "skip"
