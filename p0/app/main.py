"""P0 API: report review, standard parsing, quote assistant and KB Q&A.

Endpoints are intentionally small and versioned under /api/v1 so the future
review workbenches can call them without coupling to parser internals.
"""
from __future__ import annotations

import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from . import ocr as ocr_service
from . import quote as quote_service
from . import report_writer, standard_qa, standard_store, weknora
from .docx_parser import parse_docx
from .pdf_text import extract_pages
from .rules import RULES, RecordInput, audit_report, validate_records
from .standard_parser import parse_standard
from .xlsx_writer import write_xlsx

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
QUOTE_DIR = Path(os.getenv("P0_QUOTE_DIR", ROOT / "data" / "quotes"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_STANDARD_BYTES = int(os.getenv("MAX_STANDARD_MB", "40")) * 1024 * 1024
ROOT_PATH = os.getenv("P0_ROOT_PATH", "").rstrip("/")
COOKIE_SECURE = os.getenv(
    "P0_COOKIE_SECURE", "1" if os.getenv("P0_ENV") == "production" else "0"
) not in {"0", "false", "off"}
PUBLIC_PATHS = {"/health", "/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/login",)
AUTH_DISABLED = os.getenv("P0_AUTH_DISABLED", "0").lower() in {"1", "true", "yes"}

AUTH_BOOTSTRAP = auth.bootstrap()

app = FastAPI(
    title="中汽零部件 P0 API",
    version="0.5.0",
    root_path=ROOT_PATH,
    description="Report review, standard parsing, quote draft and WeKnora-cited Q&A.",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Authentication, session and audit
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    display_name: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path == "/api/v1/auth/login" or path.startswith(PUBLIC_PREFIXES)


def _login_redirect(request: Request) -> RedirectResponse:
    target = (ROOT_PATH or "") + "/login"
    next_path = request.url.path
    if not next_path.startswith("/api/"):
        target += "?next=" + next_path
    return RedirectResponse(target, status_code=303)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)
    if AUTH_DISABLED:
        request.state.user = {"username": "test", "role": "admin", "display_name": "test", "active": True}
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE_NAME, "")
    user = auth.read_session(token) if token else None
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        return _login_redirect(request)
    request.state.user = user
    started = __import__("time").perf_counter()
    response = await call_next(request)
    if path.startswith("/api/"):
        auth.audit(
            {
                "user": user["username"],
                "role": user["role"],
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "ms": round((__import__("time").perf_counter() - started) * 1000, 1),
                "ip": _client_ip(request),
            }
        )
    return response


def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@app.get("/login")
def login_page(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token and auth.read_session(token):
        return RedirectResponse((ROOT_PATH or "") + "/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    username = (payload.username or "").strip()
    ip = _client_ip(request)
    key = f"{ip}|{username.lower()}"
    if auth.throttled(key):
        auth.audit({"user": username, "method": "POST", "path": "/api/v1/auth/login",
                    "status": 429, "ip": ip, "event": "login_throttled"})
        raise HTTPException(status_code=429, detail="登录尝试过多，请 5 分钟后再试")
    user = auth.authenticate(username, payload.password or "")
    if user is None:
        auth.record_attempt(key)
        auth.audit({"user": username, "method": "POST", "path": "/api/v1/auth/login",
                    "status": 401, "ip": ip, "event": "login_failed"})
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.create_session(user)
    response = JSONResponse({"user": user})
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path=ROOT_PATH or "/",
    )
    auth.audit({"user": username, "role": user["role"], "method": "POST",
                "path": "/api/v1/auth/login", "status": 200, "ip": ip,
                "event": "login_success"})
    return response


@app.post("/api/v1/auth/logout")
def logout(request: Request) -> JSONResponse:
    user = _require_user(request)
    auth.audit({"user": user["username"], "role": user["role"], "method": "POST",
                "path": "/api/v1/auth/logout", "status": 200, "ip": _client_ip(request)})
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path=ROOT_PATH or "/")
    return response


@app.get("/api/v1/auth/me")
def me(request: Request) -> dict:
    return {"user": _require_user(request)}


@app.get("/api/v1/auth/users")
def list_users(request: Request) -> dict:
    _require_admin(request)
    return {"users": [auth.public_user(user) for user in auth.load_users()]}


@app.post("/api/v1/auth/users", status_code=201)
def add_user(payload: CreateUserRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        user = auth.create_user(
            payload.username,
            payload.password,
            role=payload.role,
            display_name=payload.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user}


@app.post("/api/v1/auth/password")
def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
    user = _require_user(request)
    if auth.authenticate(user["username"], payload.old_password or "") is None:
        raise HTTPException(status_code=400, detail="原密码错误")
    try:
        auth.update_password(user["username"], payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/v1/audit/recent")
def audit_recent(request: Request, limit: int = 50) -> dict:
    _require_admin(request)
    return {"events": auth.recent_audit(limit)}


@app.get("/api/v1/system/status")
def system_status() -> dict:
    return {
        "version": app.version,
        "auth": {"enabled": True, "users": len(auth.load_users())},
        "weknora": {
            "configured": weknora.configured(),
            "required": weknora.required(),
            "kb_id": (weknora.config_from_env().kb_id if weknora.config_from_env() else ""),
        },
    }


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
    mode: str = "auto"
    min_score: float | None = None
    top_k: int | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "p0-report-review+standard-parsing+quote+weknora-qa",
        "version": app.version,
        "auth": "enabled",
        "weknora": "configured" if weknora.configured() else "missing",
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


@app.post("/api/v1/reports/audit.md")
async def audit_report_markdown(file: UploadFile = File(...)) -> PlainTextResponse:
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
        result = audit_report(parsed)
        return PlainTextResponse(
            report_writer.audit_markdown(result, {"file_name": file.filename}),
            media_type="text/markdown; charset=utf-8",
        )
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
async def parse_standard_file(
    file: UploadFile = File(...), enable_ocr: bool = True
) -> dict:
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
        page_confidence: list[float] | None = None
        ocr_meta: dict | None = None
        ocr_warning: str | None = None
        ocr_enabled = enable_ocr and os.getenv("P0_OCR_ENABLED", "1") not in {"0", "false", "off"}
        if sum(len(page) for page in pages) < 500 and ocr_enabled:
            try:
                dpi = int(os.getenv("P0_OCR_DPI", "220"))
                max_pages = int(os.getenv("P0_OCR_MAX_PAGES", "0")) or None
                ocr_result = ocr_service.ocr_pdf(data, dpi=dpi, max_pages=max_pages)
                pages = ocr_result.page_texts()
                page_confidence = ocr_result.page_confidences()
                ocr_meta = ocr_result.meta()
            except ocr_service.OcrUnavailable as exc:
                ocr_warning = f"扫描件 OCR 未启用：{exc}"
        parsed = parse_standard(
            pages,
            file.filename,
            pdf_bytes=data,
            page_confidence=page_confidence,
            ocr=ocr_meta,
        )
        if ocr_warning:
            parsed.setdefault("warnings", []).append(ocr_warning)
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
    except weknora.WeKnoraError as exc:
        raise HTTPException(status_code=502, detail=f"WeKnora 发布失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/standards/ask")
def ask_standard(payload: AskRequest) -> dict:
    kbs = standard_store.published_kbs(payload.standard_id)
    if payload.standard_id and not kbs:
        raise HTTPException(status_code=409, detail="该标准尚未发布到知识库")
    try:
        return standard_qa.answer(
            payload.question,
            kbs,
            mode=payload.mode,
            min_score=payload.min_score,
            top_k=payload.top_k,
        )
    except standard_qa.VectorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except weknora.WeKnoraError as exc:
        raise HTTPException(status_code=502, detail=f"WeKnora 检索失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Increment 3: quote assistant (DVP -> CRM draft + 销售报价表)
# ---------------------------------------------------------------------------
@app.post("/api/v1/quotes/parse")
async def parse_quote_file(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="only .xlsx DVP/test-plan files are supported")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        return quote_service.parse_dvp(tmp_path, source_name=file.filename).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse DVP: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/v1/quotes/generate")
async def generate_quote(
    file: UploadFile = File(...),
    template: UploadFile | None = File(None),
) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="only .xlsx DVP/test-plan files are supported")
    data = await file.read()
    tmp_path: str | None = None
    template_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        result = quote_service.parse_dvp(tmp_path, source_name=file.filename)

        quote_id = "q_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)
        directory = QUOTE_DIR / quote_id
        directory.mkdir(parents=True, exist_ok=True)
        crm_path = quote_service.write_crm_xlsx(directory / "CRM报价草稿.xlsx", result)
        files = {"crm": f"{ROOT_PATH}/api/v1/quotes/{quote_id}/crm.xlsx"}

        template_bytes = await template.read() if template is not None else b""
        if template_bytes:
            template_name = template.filename or "sales-template.xlsx"
            template_path = str(directory / template_name)
            Path(template_path).write_bytes(template_bytes)
        configured = os.getenv("P0_SALES_QUOTE_TEMPLATE", "")
        sales_verification = None
        if template_path or configured:
            sales_out = quote_service.fill_sales_quote(
                template_path or configured,
                result,
                directory / "销售报价表-生成.xlsx",
                {
                    "project_name": f"{result.sample_name} DVP 试验报价",
                    "contract": f"{result.sample_name} DVP（闭口合同）",
                },
            )
            sales_verification = quote_service.verify_sales_quote(sales_out, result)
            files["sales_quote"] = f"{ROOT_PATH}/api/v1/quotes/{quote_id}/sales.xlsx"
        return {
            "quote_id": quote_id,
            "summary": result.summary(),
            "preview": quote_service.sales_quote_preview(result),
            "files": files,
            "crm_file": str(crm_path),
            "sales_quote_verification": sales_verification,
        }
    except quote_service.QuoteSelfCheckError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to generate quote: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


_QUOTE_ID = re.compile(r"^q_[0-9]{14}_[0-9a-f]{6}$")


def _quote_file(quote_id: str, name: str) -> Path:
    if not _QUOTE_ID.fullmatch(quote_id):
        raise HTTPException(status_code=404, detail="quote not found")
    path = QUOTE_DIR / quote_id / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="quote file not found")
    return path


@app.get("/api/v1/quotes/{quote_id}/crm.xlsx")
def download_crm(quote_id: str) -> FileResponse:
    return FileResponse(
        _quote_file(quote_id, "CRM报价草稿.xlsx"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="CRM报价草稿.xlsx",
    )


@app.get("/api/v1/quotes/{quote_id}/sales.xlsx")
def download_sales_quote(quote_id: str) -> FileResponse:
    return FileResponse(
        _quote_file(quote_id, "销售报价表-生成.xlsx"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="销售报价表-生成.xlsx",
    )


# ---------------------------------------------------------------------------
# Increment 4: knowledge base rebuild + cited Q&A
# ---------------------------------------------------------------------------
@app.post("/api/v1/kb/rebuild")
def rebuild_kb() -> dict:
    index = standard_store.rebuild_index()
    return {key: index[key] for key in ("built_at", "standard_count", "chunk_count")} | {
        "standards": index.get("standards", [])
    }


@app.get("/api/v1/kb/stats")
def kb_stats() -> dict:
    return standard_store.index_stats()
