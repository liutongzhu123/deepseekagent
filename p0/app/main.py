"""P0 API: report review (increment 1) + standard parsing (increment 2).

Endpoints are intentionally small and versioned under /api/v1 so the future
review workbenches can call them without coupling to parser internals.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import standard_qa, standard_store
from .docx_parser import parse_docx
from .pdf_text import extract_pages
from .rules import RULES, RecordInput, audit_report, validate_records
from .standard_parser import parse_standard
from .xlsx_writer import write_xlsx

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_STANDARD_BYTES = int(os.getenv("MAX_STANDARD_MB", "40")) * 1024 * 1024

app = FastAPI(
    title="中汽零部件 P0 API",
    version="0.2.0",
    description="Deterministic report review and standard parsing for the P0 slice.",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class RecordIn(BaseModel):
    task_no: str = ""
    report_no: str = ""
    sample_no: str = ""
    limit: float | None = None
    scores: list[float] = Field(default_factory=list)
    reported_mean: float | None = None
    test_date: str = ""
    eval_date: str = ""
    judgment: str = ""


class RecordsRequest(BaseModel):
    records: list[RecordIn]


class ItemPatch(BaseModel):
    clause: str
    name: str | None = None
    requirement: str | None = None
    method: str | None = None


class ReviewRequest(BaseModel):
    items: list[ItemPatch] = Field(default_factory=list)
    reviewer: str = ""


class AskRequest(BaseModel):
    question: str
    standard_id: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "p0-report-review+standard-parsing",
        "version": app.version,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Increment 1: report review
# ---------------------------------------------------------------------------
@app.get("/api/v1/rules")
def list_rules() -> dict:
    return {"rules": [{"id": rid, "title": title, "description": desc} for rid, title, desc in RULES]}


@app.post("/api/v1/reports/audit")
async def audit_report_file(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="only .docx reports are supported in this slice")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        parsed = parse_docx(tmp_path)
        parsed.file_name = file.filename
        return audit_report(parsed)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse report: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/v1/records/validate")
def validate_record_batch(payload: RecordsRequest) -> dict:
    records = [
        RecordInput(
            task_no=item.task_no,
            report_no=item.report_no,
            sample_no=item.sample_no,
            limit=item.limit,
            scores=item.scores,
            reported_mean=item.reported_mean,
            test_date=item.test_date,
            eval_date=item.eval_date,
            judgment=item.judgment,
        )
        for item in payload.records
    ]
    return validate_records(records)


# ---------------------------------------------------------------------------
# Increment 2: standard parsing
# ---------------------------------------------------------------------------
@app.get("/api/v1/standards")
def list_standards() -> dict:
    return {"standards": standard_store.list_standards()}


@app.post("/api/v1/standards/parse")
async def parse_standard_file(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only .pdf standards are supported in this slice")
    data = await file.read()
    if len(data) > MAX_STANDARD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_STANDARD_BYTES // 1024 // 1024} MB",
        )
    try:
        pages = extract_pages(data)
        parsed = parse_standard(pages, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse standard: {exc}") from exc
    return standard_store.create(parsed, data, file.filename)


@app.get("/api/v1/standards/{standard_id}")
def get_standard(standard_id: str) -> dict:
    try:
        return standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc


@app.patch("/api/v1/standards/{standard_id}")
def review_standard(standard_id: str, payload: ReviewRequest) -> dict:
    try:
        return standard_store.update_review(
            standard_id,
            [item.model_dump() for item in payload.items],
            reviewer=payload.reviewer,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc


@app.get("/api/v1/standards/{standard_id}/export.xlsx")
def export_standard(standard_id: str) -> FileResponse:
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    if not record["items"]:
        raise HTTPException(status_code=409, detail="no items to export")
    meta = record["meta"]
    summary_rows = [["检验依据", "检验标准名称", "实检项目"]] + [
        [meta.get("standard_no", ""), meta.get("name", ""), item["name"]]
        for item in record["items"]
    ]
    detail_rows = [
        [
            "检验依据",
            "检验标准名称",
            "实检项目",
            "技术要求条款",
            "技术要求/判定要求",
            "试验方法条款",
            "试验方法（具体方法）",
        ]
    ] + [
        [
            meta.get("standard_no", ""),
            meta.get("name", ""),
            item["name"],
            item["clause"],
            item["requirement"],
            item["method_clause"],
            item["method"],
        ]
        for item in record["items"]
    ]
    path = standard_store.export_path(standard_id)
    write_xlsx(
        path,
        [
            ("实检项目", summary_rows, [16, 28, 32]),
            ("试验方法明细", detail_rows, [16, 28, 32, 12, 60, 12, 60]),
        ],
    )
    filename = standard_store.safe_filename(
        f"{meta.get('standard_no') or meta.get('name', 'standard')}-解析.xlsx"
    )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.post("/api/v1/standards/{standard_id}/publish")
def publish_standard(standard_id: str) -> dict:
    try:
        return standard_store.publish(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/standards/ask")
def ask_standard(payload: AskRequest) -> dict:
    kbs = standard_store.published_kbs(payload.standard_id)
    if payload.standard_id and not kbs:
        raise HTTPException(status_code=409, detail="该标准尚未发布到知识库")
    return standard_qa.answer(payload.question, kbs)
