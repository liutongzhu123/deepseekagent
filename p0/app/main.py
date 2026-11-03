"""P0 API: report review, standard parsing, quote assistant and KB Q&A.

Endpoints are intentionally small and versioned under /api/v1 so the future
review workbenches can call them without coupling to parser internals.
"""
from __future__ import annotations

import os
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin_config
from . import auth
from . import quote as quote_service
from . import crm, quote_store, store, task_worker
from . import report_writer, standard_parse, standard_qa, standard_store, weknora
from .store import tasks
from .docx_parser import parse_docx
from .rules import RecordInput, audit_report, rules_catalog, validate_records

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
QUOTE_DIR = Path(os.getenv("P0_QUOTE_DIR", ROOT / "data" / "quotes"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_STANDARD_BYTES = int(os.getenv("MAX_STANDARD_MB", "40")) * 1024 * 1024
ROOT_PATH = os.getenv("P0_ROOT_PATH", "").rstrip("/")
COOKIE_SECURE = os.getenv(
    "P0_COOKIE_SECURE", "1" if os.getenv("P0_ENV") == "production" else "0"
) not in {"0", "false", "off"}
PUBLIC_PATHS = {"/health", "/login", "/favicon.ico", "/api/v1/auth/providers"}
PUBLIC_PREFIXES = ("/static/login",)
AUTH_DISABLED = os.getenv("P0_AUTH_DISABLED", "0").lower() in {"1", "true", "yes"}

AUTH_BOOTSTRAP = auth.bootstrap()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start the in-process worker with the API; stop it on shutdown."""
    task_worker.start_worker_if_enabled()
    try:
        yield
    finally:
        task_worker.stop()


app = FastAPI(
    lifespan=lifespan,
    title="中汽零部件 P1 API",
    version="0.7.3",
    root_path=ROOT_PATH,
    description="Multi-user report review, standard parsing, quote draft and WeKnora-cited Q&A.",
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
    department: str = ""
    projects: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    role: str | None = None
    active: bool | None = None
    display_name: str | None = None
    department: str | None = None
    projects: list[str] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = ""


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


def _attachment_header(filename: str) -> str:
    """RFC 5987 attachment header that keeps Chinese filenames intact."""
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_"
        for char in filename
    ) or "download"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{url_quote(filename, safe='')}"
    )


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
                "action": getattr(request.state, "action", ""),
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


def _require_permission(request: Request, action: str) -> dict:
    user = _require_user(request)
    if not auth.has_permission(user, action):
        raise HTTPException(status_code=403, detail=f"需要权限：{action}")
    request.state.action = action
    return user


def _audit_event(request: Request, user: dict, event: str, **extra) -> None:
    auth.audit(
        {
            "user": user.get("username", ""),
            "role": user.get("role", ""),
            "event": event,
            "ip": _client_ip(request),
            **extra,
        }
    )


def _record_project(meta: dict) -> tuple[str, str]:
    return (meta or {}).get("project", ""), (meta or {}).get("created_by", "")


def _assert_project_access(user: dict, meta: dict) -> None:
    project, created_by = _record_project(meta)
    if not auth.can_access_project(user, project, created_by):
        raise HTTPException(status_code=403, detail="无权访问该项目的数据")


def _visible_kbs(user: dict, standard_id: str | None = None) -> list[dict]:
    return [
        kb
        for kb in standard_store.published_kbs(standard_id)
        if auth.can_access_project(
            user, kb.get("project", ""), kb.get("created_by", "")
        )
    ]


@app.get("/login")
def login_page(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token and auth.read_session(token):
        return RedirectResponse((ROOT_PATH or "") + "/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/api/v1/auth/providers")
def auth_providers() -> dict:
    return auth.provider_status()


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, request: Request) -> JSONResponse:
    username = (payload.username or "").strip()
    ip = _client_ip(request)
    key = f"{ip}|{username.lower()}"
    if auth.throttled(key):
        auth.audit({"user": username, "method": "POST", "path": "/api/v1/auth/login",
                    "status": 429, "ip": ip, "event": "login_throttled"})
        raise HTTPException(status_code=429, detail="登录尝试过多，请 5 分钟后再试")
    try:
        user = auth.authenticate(username, payload.password or "")
    except auth.AccountLocked as exc:
        minutes = max(1, (exc.seconds + 59) // 60)
        auth.audit({"user": username, "method": "POST", "path": "/api/v1/auth/login",
                    "status": 423, "ip": ip, "event": "login_locked"})
        raise HTTPException(
            status_code=423,
            detail=f"账号已锁定，请 {minutes} 分钟后再试或联系管理员重置",
        ) from exc
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
    _audit_event(request, user, "logout")
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path=ROOT_PATH or "/")
    return response


@app.get("/api/v1/auth/me")
def me(request: Request) -> dict:
    return {"user": _require_user(request)}


@app.get("/api/v1/auth/users")
def list_users(request: Request) -> dict:
    _require_permission(request, "user:manage")
    return {"users": [auth.public_user(user) for user in auth.load_users()]}


@app.post("/api/v1/auth/users", status_code=201)
def add_user(payload: CreateUserRequest, request: Request) -> dict:
    actor = _require_permission(request, "user:manage")
    try:
        user = auth.create_user(
            payload.username,
            payload.password,
            role=payload.role,
            display_name=payload.display_name,
            department=payload.department,
            projects=payload.projects,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_event(request, actor, "user_created", target=user["username"], new_role=user["role"])
    return {"user": user}


@app.patch("/api/v1/auth/users/{username}")
def update_user(username: str, payload: UpdateUserRequest, request: Request) -> dict:
    actor = _require_permission(request, "user:manage")
    if username.lower() == actor["username"].lower():
        if payload.active is False:
            raise HTTPException(status_code=409, detail="不能停用当前登录的管理员")
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(status_code=409, detail="不能降低当前登录管理员的角色")
    try:
        user = auth.update_user(
            username,
            role=payload.role,
            active=payload.active,
            display_name=payload.display_name,
            department=payload.department,
            projects=payload.projects,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_event(
        request,
        actor,
        "user_updated",
        target=user["username"],
        new_role=user["role"],
        active=user["active"],
        projects=user["projects"],
    )
    return {"user": user}


@app.post("/api/v1/auth/users/{username}/reset-password")
def reset_user_password(
    username: str, payload: ResetPasswordRequest, request: Request, response: Response
) -> dict:
    actor = _require_permission(request, "user:manage")
    response.headers["Cache-Control"] = "no-store"
    try:
        result = auth.reset_password(username, payload.new_password or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_event(
        request, actor, "user_password_reset", target=result["username"],
        generated=result["generated"],
    )
    return result


@app.post("/api/v1/auth/password")
def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
    user = _require_user(request)
    raw_user = auth.find_user(user["username"])
    if raw_user is None or not auth.verify_password(
        payload.old_password or "", raw_user.get("password_hash", "")
    ):
        raise HTTPException(status_code=400, detail="原密码错误")
    try:
        auth.update_password(user["username"], payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_event(request, user, "password_changed")
    return {"ok": True}


def _audit_filters(
    user: str,
    role: str,
    event: str,
    method: str,
    path: str,
    status: int | None,
    date_from: str,
    date_to: str,
) -> dict:
    return {
        "user": user,
        "role": role,
        "event": event,
        "method": method,
        "path": path,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
    }


@app.get("/api/v1/audit/events")
def audit_events(
    request: Request,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    _require_permission(request, "audit:read")
    return auth.query_audit(
        **_audit_filters(user, role, event, method, path, status, date_from, date_to),
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/audit/export.csv")
def audit_export(
    request: Request,
    user: str = "",
    role: str = "",
    event: str = "",
    method: str = "",
    path: str = "",
    status: int | None = None,
    date_from: str = "",
    date_to: str = "",
) -> Response:
    actor = _require_permission(request, "audit:export")
    events = auth.filter_audit(
        **_audit_filters(user, role, event, method, path, status, date_from, date_to)
    )
    _audit_event(request, actor, "audit_exported", rows=len(events))
    return Response(
        content="\ufeff" + auth.audit_csv(events),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit.csv"},
    )


@app.get("/api/v1/audit/recent")
def audit_recent(request: Request, limit: int = 50) -> dict:
    _require_permission(request, "audit:read")
    return {"events": auth.recent_audit(limit)}


@app.get("/api/v1/system/status")
def system_status(request: Request) -> dict:
    _require_permission(request, "system:status")
    return {
        "version": app.version,
        "auth": {
            "enabled": True,
            "users": len(auth.load_users()),
            "roles": list(auth.ROLES),
            "providers": auth.provider_status(),
        },
        "weknora": {
            "configured": weknora.configured(),
            "required": weknora.required(),
            "kb_id": (weknora.config_from_env().kb_id if weknora.config_from_env() else ""),
        },
    }


@app.get("/api/v1/admin/config")
def get_admin_config(request: Request) -> dict:
    _require_permission(request, "config:read")
    return admin_config.describe()


@app.get("/api/v1/config/effective")
def get_effective_config(request: Request) -> dict:
    """Read-only config for the module pages, without the internal file path."""
    _require_permission(request, "rules:read")
    result = admin_config.describe()
    result.pop("path", None)
    result.pop("sections", None)
    return result


@app.put("/api/v1/admin/config/{section}")
def update_admin_config(section: str, payload: dict, request: Request) -> dict:
    user = _require_permission(request, "config:manage")
    try:
        admin_config.update_section(section, payload, updated_by=user.get("username", ""))
    except admin_config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = admin_config.describe()
    _audit_event(
        request, user, "admin_config_updated",
        section=section, revision=result["config"]["revision"],
    )
    return result


@app.post("/api/v1/admin/config/{section}/reset")
def reset_admin_config(section: str, request: Request) -> dict:
    user = _require_permission(request, "config:manage")
    try:
        admin_config.reset_section(section, updated_by=user.get("username", ""))
    except admin_config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = admin_config.describe()
    _audit_event(
        request, user, "admin_config_reset",
        section=section, revision=result["config"]["revision"],
    )
    return result


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
    payload = {
        "status": "ok",
        "service": "p0-report-review+standard-parsing+quote+weknora-qa",
        "version": app.version,
        "auth": "enabled",
        "weknora": "configured" if weknora.configured() else "missing",
        "store": store.config.store_mode(),
    }
    worker = task_worker.status()
    payload["tasks"] = "worker" if worker["running"] else ("enabled" if worker["enabled"] else "off")
    if store.config.postgres_enabled():
        payload["object_store"] = store.config.object_store_mode()
    return payload


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Increment 1: report review
# ---------------------------------------------------------------------------
@app.get("/api/v1/rules")
def list_rules(request: Request) -> dict:
    _require_permission(request, "rules:read")
    rules = rules_catalog(admin_config.report_config())
    return {
        "rules": rules,
        "enabled": sum(1 for rule in rules if rule["enabled"]),
        "total": len(rules),
    }


@app.post("/api/v1/reports/audit")
async def audit_report_file(request: Request, file: UploadFile = File(...)) -> dict:
    user = _require_permission(request, "report:audit")
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
        result = audit_report(parsed, admin_config.report_config())
        _audit_event(
            request, user, "report_audited", file_name=file.filename,
            fail=result.get("summary", {}).get("fail", 0),
            pass_count=result.get("summary", {}).get("pass", 0),
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse report: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/v1/reports/audit.md")
async def audit_report_markdown(request: Request, file: UploadFile = File(...)) -> PlainTextResponse:
    user = _require_permission(request, "report:audit")
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
        result = audit_report(parsed, admin_config.report_config())
        _audit_event(request, user, "report_audited_markdown", file_name=file.filename)
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
def validate_record_batch(payload: RecordsRequest, request: Request) -> dict:
    user = _require_permission(request, "report:audit")
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
    result = validate_records(records, admin_config.report_config())
    _audit_event(request, user, "records_validated", records=len(records))
    return result


# ---------------------------------------------------------------------------
# Increment 2: standard parsing
# ---------------------------------------------------------------------------
class AssignProjectRequest(BaseModel):
    project: str
    created_by: str | None = None


@app.patch("/api/v1/standards/{standard_id}/project")
def assign_standard_project(
    standard_id: str, payload: AssignProjectRequest, request: Request
) -> dict:
    """Tag a legacy standard with a project (P1 migration helper)."""
    user = _require_permission(request, "standard:project")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    project = (payload.project or "").strip()
    try:
        updated = standard_store.set_project(
            standard_id, project, created_by=payload.created_by
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _audit_event(
        request, user, "standard_project_assigned", standard_id=standard_id,
        project=project, previous=record.get("meta", {}).get("project", ""),
    )
    return updated


@app.get("/api/v1/standards")
def list_standards(request: Request) -> dict:
    user = _require_permission(request, "standard:read")
    return {
        "standards": [
            record
            for record in standard_store.list_standards()
            if auth.can_access_project(
                user, record.get("project", ""), record.get("created_by", "")
            )
        ]
    }


def _task_switch(env_name: str, default: str = "1") -> bool:
    if not tasks.enabled():
        return False
    value = os.getenv(env_name, default).strip().lower()
    return value not in {"0", "false", "off", "no"}


def _async_parse_min_bytes() -> int:
    try:
        megabytes = float(os.getenv("P0_TASKS_ASYNC_MIN_MB", "5"))
    except ValueError:
        megabytes = 5.0
    return max(0, int(megabytes * 1024 * 1024))


def _async_parse_wanted(data: bytes) -> bool:
    if not _task_switch("P0_TASKS_PARSE_ASYNC"):
        return False
    threshold = _async_parse_min_bytes()
    if threshold == 0 or len(data) >= threshold:
        return True
    # Scanned PDFs are often small; sample the text layer before deciding.
    return standard_parse.looks_scanned(data)


def _task_response(task: dict) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "task_id": task["id"],
            "status": task["status"],
            "status_url": f"{ROOT_PATH}/api/v1/tasks/{task['id']}",
        },
    )


def _enqueue_parse(
    request: Request,
    user: dict,
    *,
    data: bytes,
    filename: str,
    project: str,
    enable_ocr: bool,
) -> JSONResponse:
    from .store import objects

    payload = {
        "file_name": filename,
        "project": project,
        "enable_ocr": bool(enable_ocr),
        "created_by": user["username"],
    }
    task = tasks.create(
        "standard_parse",
        payload,
        created_by=user["username"],
        project=project,
        max_attempts=int(os.getenv("P0_TASKS_MAX_ATTEMPTS", "2")),
    )
    with store.db.engine().begin() as conn:
        file_record = objects.upsert_file(
            conn,
            owner_type="task",
            owner_id=task["id"],
            name="source.pdf",
            data=data,
            content_type="application/pdf",
        )
    task = tasks.set_payload(task["id"], {**payload, "file_id": file_record["id"]})
    _audit_event(
        request, user, "standard_parse_queued",
        task_id=task["id"], project=project, bytes=len(data),
    )
    return _task_response(task)


@app.post("/api/v1/standards/parse")
async def parse_standard_file(
    request: Request,
    file: UploadFile = File(...),
    enable_ocr: bool = True,
    project: str = Form(""),
) -> dict:
    user = _require_permission(request, "standard:parse")
    project = (project or "").strip()
    if not auth.can_create_in_project(user, project):
        raise HTTPException(
            status_code=400,
            detail="受限账号必须填写有权限的项目标识",
        )
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only .pdf standards are supported in this slice")
    data = await file.read()
    if len(data) > MAX_STANDARD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_STANDARD_BYTES // 1024 // 1024} MB",
        )
    if _async_parse_wanted(data):
        return _enqueue_parse(
            request, user, data=data, filename=file.filename,
            project=project, enable_ocr=enable_ocr,
        )
    try:
        parsed = standard_parse.parse_pdf_standard(
            data,
            file.filename,
            enable_ocr=enable_ocr,
            rule_config=admin_config.standard_config(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse standard: {exc}") from exc
    record = standard_store.create(
        parsed, data, file.filename, project=project, created_by=user["username"]
    )
    _audit_event(
        request, user, "standard_parsed", standard_id=record["id"],
        project=project, items=record.get("summary", {}).get("items", 0),
    )
    return record


@app.get("/api/v1/standards/{standard_id}")
def get_standard(standard_id: str, request: Request) -> dict:
    user = _require_permission(request, "standard:read")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    return record


@app.patch("/api/v1/standards/{standard_id}")
def review_standard(standard_id: str, payload: ReviewRequest, request: Request) -> dict:
    user = _require_permission(request, "standard:review")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    try:
        result = standard_store.update_review(
            standard_id,
            [item.model_dump() for item in payload.items],
            reviewer=payload.reviewer,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _audit_event(
        request, user, "standard_reviewed", standard_id=standard_id,
        revision=result.get("revision", 0), changes=len(payload.items),
    )
    return result


@app.get("/api/v1/standards/{standard_id}/export.xlsx")
def export_standard(standard_id: str, request: Request) -> FileResponse:
    user = _require_permission(request, "standard:export")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    if not record["items"]:
        raise HTTPException(status_code=409, detail="no items to export")
    meta = record["meta"]
    summary_rows = [["检验依据", "检验标准名称", "实检项目"]] + [
        [meta.get("standard_no", ""), meta.get("name", ""), item.get("name", "")]
        for item in record["items"]
    ]
    fields = [
        field
        for field in admin_config.standard_config()["fields"]
        if field.get("export")
    ]
    if not fields:
        fields = [
            {"key": "name", "label": "实检项目"},
            {"key": "clause", "label": "技术要求条款"},
            {"key": "requirement", "label": "技术要求/判定要求"},
            {"key": "method_clause", "label": "试验方法条款"},
            {"key": "method", "label": "试验方法（具体方法）"},
        ]
    field_widths = {
        "name": 32,
        "clause": 12,
        "requirement": 60,
        "method_clause": 12,
        "method": 60,
        "group": 20,
        "page": 10,
        "method_page": 10,
    }
    detail_rows = [
        ["检验依据", "检验标准名称"] + [field["label"] for field in fields]
    ] + [
        [meta.get("standard_no", ""), meta.get("name", "")]
        + [item.get(field["key"], "") for field in fields]
        for item in record["items"]
    ]
    data = standard_store.export_bytes(
        standard_id,
        [
            ("实检项目", summary_rows, [16, 28, 32]),
            (
                "试验方法明细",
                detail_rows,
                [16, 28] + [field_widths.get(field["key"], 16) for field in fields],
            ),
        ],
    )
    filename = standard_store.safe_filename(
        f"{meta.get('standard_no') or meta.get('name', 'standard')}-解析.xlsx"
    )
    _audit_event(request, user, "standard_exported", standard_id=standard_id)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment_header(filename)},
    )


@app.post("/api/v1/standards/{standard_id}/publish")
def publish_standard(standard_id: str, request: Request) -> dict:
    user = _require_permission(request, "standard:publish")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    if _task_switch("P0_TASKS_PUBLISH_ASYNC"):
        task = tasks.create(
            "standard_publish",
            {"standard_id": standard_id, "created_by": user["username"]},
            created_by=user["username"],
            project=record.get("meta", {}).get("project", ""),
            max_attempts=int(os.getenv("P0_TASKS_MAX_ATTEMPTS", "2")),
        )
        _audit_event(
            request, user, "standard_publish_queued",
            standard_id=standard_id, task_id=task["id"],
        )
        return _task_response(task)
    try:
        result = standard_store.publish(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    except weknora.WeKnoraError as exc:
        raise HTTPException(status_code=502, detail=f"WeKnora 发布失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit_event(
        request, user, "standard_published", standard_id=standard_id,
        chunks=result.get("chunks", 0),
    )
    return result


def _require_tasks_enabled() -> None:
    if not tasks.enabled():
        raise HTTPException(status_code=503, detail="后台任务队列未启用")


@app.get("/api/v1/tasks")
def list_tasks(request: Request, limit: int = 10) -> dict:
    _require_tasks_enabled()
    user = _require_permission(request, "standard:read")
    created_by = "" if user.get("role") == "admin" else user["username"]
    return {"tasks": tasks.list_recent(created_by=created_by, limit=limit)}


def _visible_task(task_id: str, user: dict) -> dict:
    try:
        task = tasks.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if user.get("role") == "admin":
        return task
    if task.get("created_by") != user.get("username"):
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("project") and not auth.can_access_project(
        user, task.get("project", ""), task.get("created_by", "")
    ):
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict:
    _require_tasks_enabled()
    user = _require_permission(request, "standard:read")
    return tasks.public(_visible_task(task_id, user))


@app.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict:
    _require_tasks_enabled()
    user = _require_permission(request, "standard:parse")
    _visible_task(task_id, user)
    task = tasks.cancel(task_id)
    _audit_event(request, user, "task_cancelled", task_id=task_id)
    return task


@app.post("/api/v1/tasks/{task_id}/retry")
def retry_task(task_id: str, request: Request) -> dict:
    _require_tasks_enabled()
    user = _require_permission(request, "standard:parse")
    _visible_task(task_id, user)
    try:
        task = tasks.retry(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit_event(request, user, "task_retried", task_id=task_id)
    return task


@app.post("/api/v1/standards/ask")
def ask_standard(payload: AskRequest, request: Request) -> dict:
    user = _require_permission(request, "standard:ask")
    if payload.standard_id:
        try:
            record = standard_store.load(payload.standard_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="standard not found") from exc
        _assert_project_access(user, record.get("meta", {}))
    kbs = _visible_kbs(user, payload.standard_id)
    if payload.standard_id and not kbs:
        raise HTTPException(status_code=409, detail="该标准尚未发布到知识库")
    try:
        answer = standard_qa.answer(
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
    _audit_event(
        request, user, "standard_asked", standard_id=payload.standard_id,
        mode=(answer.get("retrieval") or {}).get("mode", ""),
    )
    return answer


# ---------------------------------------------------------------------------
# Quote assistant: Excel / PDF / image input -> CRM draft + 销售报价表
# ---------------------------------------------------------------------------
def _quote_upload_bytes(data: bytes) -> None:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB",
        )


def _quote_input_name(file: UploadFile) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail=quote_service.SUPPORTED_INPUT_HINT)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in quote_service.SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=400, detail=quote_service.SUPPORTED_INPUT_HINT)
    return file.filename


@app.post("/api/v1/quotes/parse")
async def parse_quote_file(request: Request, file: UploadFile = File(...)) -> dict:
    _require_permission(request, "quote:parse")
    filename = _quote_input_name(file)
    data = await file.read()
    _quote_upload_bytes(data)
    try:
        return quote_service.parse_quote_bytes(
            data, filename, rule_config=admin_config.quote_config()
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse quote input: {exc}") from exc


@app.post("/api/v1/quotes/generate")
async def generate_quote(
    request: Request,
    file: UploadFile = File(...),
    template: UploadFile | None = File(None),
    project: str = Form(""),
) -> dict:
    user = _require_permission(request, "quote:generate")
    project = (project or "").strip()
    if not auth.can_create_in_project(user, project):
        raise HTTPException(
            status_code=400,
            detail="受限账号必须填写有权限的项目标识",
        )
    filename = _quote_input_name(file)
    if template is not None and template.filename:
        if Path(template.filename).suffix.lower() not in {".xlsx", ".xlsm"}:
            raise HTTPException(status_code=400, detail="销售报价表模板只支持 .xlsx / .xlsm")
    data = await file.read()
    _quote_upload_bytes(data)
    try:
        quote_cfg = admin_config.quote_config()
        result = quote_service.parse_quote_bytes(
            data, filename, rule_config=quote_cfg
        )
        reconciliation = crm.reconcile_quote(result)
        if os.getenv("P0_CRM_REQUIRED", "0").lower() in {"1", "true", "yes"} and not reconciliation.get("reconciled"):
            raise HTTPException(status_code=409, detail=f"CRM 对账未通过：{reconciliation}")

        quote_id = "q_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)
        directory = QUOTE_DIR / quote_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "quote_id": quote_id,
            "project": project,
            "created_by": user["username"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_name": filename,
            "parse_method": result.parse_method,
            "lines": len(result.lines),
            "priced_lines": result.priced_lines,
        }
        stored_files: dict[str, bytes] = {}
        crm_path = quote_service.write_crm_xlsx(
            directory / "CRM报价草稿.xlsx", result, quote_cfg["crm_columns"]
        )
        stored_files["CRM报价草稿.xlsx"] = Path(crm_path).read_bytes()
        files = {"crm": f"{ROOT_PATH}/api/v1/quotes/{quote_id}/crm.xlsx"}

        template_bytes = await template.read() if template is not None else b""
        template_path: str | None = None
        if template_bytes:
            template_name = template.filename or "sales-template.xlsx"
            template_path = str(directory / template_name)
            Path(template_path).write_bytes(template_bytes)
            stored_files[template_name] = template_bytes
        configured = os.getenv("P0_SALES_QUOTE_TEMPLATE", "")
        sales_verification = None
        sales_skipped_reason = ""
        template_source = template_path or configured
        if not template_source:
            sales_skipped_reason = "未提供销售报价表模板，只生成 CRM 草稿"
        elif not result.all_priced:
            sales_skipped_reason = (
                f"{result.unpriced_lines} 行缺少单价，未生成销售报价表；"
                "请在 CRM 草稿中补充单价后重试"
            )
        else:
            sales_out = quote_service.fill_sales_quote(
                template_source,
                result,
                directory / "销售报价表-生成.xlsx",
                {
                    "project_name": f"{result.sample_name} 试验报价",
                    "contract": f"{result.sample_name}（闭口合同）",
                },
            )
            sales_verification = quote_service.verify_sales_quote(sales_out, result)
            stored_files["销售报价表-生成.xlsx"] = Path(sales_out).read_bytes()
            files["sales_quote"] = f"{ROOT_PATH}/api/v1/quotes/{quote_id}/sales.xlsx"
        quote_store.create(meta, stored_files, base_dir=QUOTE_DIR)
        _audit_event(
            request, user, "quote_generated", quote_id=quote_id,
            project=project, lines=result.summary().get("lines", 0),
            crm_reconciled=bool(reconciliation.get("reconciled")),
        )
        return {
            "quote_id": quote_id,
            "project": project,
            "summary": result.summary(),
            "parse_meta": result.parse_meta,
            "preview": quote_service.sales_quote_preview(result),
            "files": files,
            "crm_file": str(crm_path),
            "sales_quote_verification": sales_verification,
            "sales_quote_skipped_reason": sales_skipped_reason,
            "crm_reconciliation": reconciliation,
        }
    except HTTPException:
        raise
    except quote_service.QuoteSelfCheckError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to generate quote: {exc}") from exc


_QUOTE_ID = re.compile(r"^q_[0-9]{14}_[0-9a-f]{6}$")


def _quote_meta(quote_id: str) -> dict:
    if not _QUOTE_ID.fullmatch(quote_id):
        raise HTTPException(status_code=404, detail="quote not found")
    meta = quote_store.load(quote_id, base_dir=QUOTE_DIR)
    return meta if isinstance(meta, dict) else {}


def _quote_download(quote_id: str, name: str, request: Request, filename: str) -> Response:
    user = _require_permission(request, "quote:download")
    if not _QUOTE_ID.fullmatch(quote_id):
        raise HTTPException(status_code=404, detail="quote not found")
    meta = _quote_meta(quote_id)
    _assert_project_access(user, meta)
    try:
        data = quote_store.read_file(quote_id, name, base_dir=QUOTE_DIR)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="quote file not found") from None
    _audit_event(request, user, "quote_downloaded", quote_id=quote_id, file_name=name)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment_header(filename)},
    )


@app.get("/api/v1/quotes/{quote_id}/crm.xlsx")
def download_crm(quote_id: str, request: Request) -> Response:
    return _quote_download(quote_id, "CRM报价草稿.xlsx", request, "CRM报价草稿.xlsx")


@app.get("/api/v1/quotes/{quote_id}/sales.xlsx")
def download_sales_quote(quote_id: str, request: Request) -> Response:
    return _quote_download(quote_id, "销售报价表-生成.xlsx", request, "销售报价表-生成.xlsx")


# ---------------------------------------------------------------------------
# P1-5 CRM read-only adapter (Excel/CSV bridge; API seam reserved)
# ---------------------------------------------------------------------------
def _crm_snapshot():
    try:
        return crm.provider().snapshot()
    except crm.CrmUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/crm/status")
def crm_status(request: Request) -> dict:
    _require_permission(request, "crm:read")
    return crm.status()


@app.get("/api/v1/crm/projects")
def crm_projects(request: Request) -> dict:
    _require_permission(request, "crm:read")
    snapshot = _crm_snapshot()
    return {"projects": crm.read_projects(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/prices")
def crm_prices(request: Request, project: str = "", q: str = "", limit: int = 200) -> dict:
    _require_permission(request, "crm:read")
    snapshot = _crm_snapshot()
    rows = crm.read_prices(snapshot, project=project, query=q)
    return {"prices": rows[: max(1, min(int(limit), 1000))], "total": len(rows), "counts": snapshot.counts()}


@app.get("/api/v1/crm/qualifications")
def crm_qualifications(request: Request) -> dict:
    _require_permission(request, "crm:read")
    snapshot = _crm_snapshot()
    return {"qualifications": crm.read_qualifications(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/schedules")
def crm_schedules(request: Request) -> dict:
    _require_permission(request, "crm:read")
    snapshot = _crm_snapshot()
    return {"schedules": crm.read_schedules(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/task-orders/{order_no}")
def crm_task_order(order_no: str, request: Request) -> dict:
    _require_permission(request, "crm:read")
    item = crm.lookup_task_order(_crm_snapshot(), order_no)
    if item is None:
        raise HTTPException(status_code=404, detail="task order not found")
    return item


@app.get("/api/v1/crm/samples/{sample_no}")
def crm_sample(sample_no: str, request: Request) -> dict:
    _require_permission(request, "crm:read")
    item = crm.lookup_sample(_crm_snapshot(), sample_no)
    if item is None:
        raise HTTPException(status_code=404, detail="sample not found")
    return item


# ---------------------------------------------------------------------------
# Increment 4: knowledge base rebuild + cited Q&A
# ---------------------------------------------------------------------------
@app.post("/api/v1/kb/rebuild")
def rebuild_kb(request: Request) -> dict:
    user = _require_permission(request, "kb:rebuild")
    index = standard_store.rebuild_index()
    _audit_event(
        request, user, "kb_rebuilt",
        standard_count=index.get("standard_count", 0),
        chunk_count=index.get("chunk_count", 0),
    )
    return {key: index[key] for key in ("built_at", "standard_count", "chunk_count")} | {
        "standards": index.get("standards", [])
    }


@app.get("/api/v1/kb/stats")
def kb_stats(request: Request) -> dict:
    user = _require_permission(request, "kb:stats")
    stats = standard_store.index_stats()
    visible = [
        record
        for record in stats.get("standards", [])
        if auth.can_access_project(
            user, record.get("project", ""), record.get("created_by", "")
        )
    ]
    if auth.is_restricted(user):
        stats["standards"] = visible
        stats["standard_count"] = len(visible)
        stats["chunk_count"] = sum(int(item.get("chunks", 0)) for item in visible)
    return stats
