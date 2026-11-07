"""P0 API: report review, standard parsing, quote assistant and KB Q&A.

Endpoints are intentionally small and versioned under /api/v1 so the future
review workbenches can call them without coupling to parser internals.
"""
from __future__ import annotations

import hashlib
from collections import Counter
import logging
import os
import re
import secrets
import tempfile
import time
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
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin_config
from . import auth
from . import quote as quote_service
from . import report_batch
from . import crm, monitoring, quote_store, store, task_worker
from . import feedback_store, oidc, report_store, report_writer, report_inputs, partial_rules, rule_dsl, semantic_audit, standard_parse, standard_qa, standard_store, weknora
from . import visual_checks
from . import xlsx_writer
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
PUBLIC_PATHS = {
    "/health", "/metrics", "/login", "/favicon.ico", "/api/v1/auth/providers",
    "/api/v1/auth/oidc/start", "/api/v1/auth/oidc/callback",
}
PUBLIC_PREFIXES = ("/static/login",)
AUTH_DISABLED = os.getenv("P0_AUTH_DISABLED", "0").lower() in {"1", "true", "yes"}

AUTH_BOOTSTRAP = auth.bootstrap()
monitoring.configure_logging()


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
    version="0.8.4",
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
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or monitoring.new_request_id()
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        monitoring.METRICS.observe(request.method, request.url.path, 500, time.perf_counter() - started)
        raise
    elapsed = time.perf_counter() - started
    monitoring.METRICS.observe(request.method, request.url.path, response.status_code, elapsed)
    response.headers["X-Request-ID"] = request_id
    if os.getenv("P0_ACCESS_LOG", "1").lower() not in {"0", "false", "off"}:
        user = getattr(request.state, "user", {}) or {}
        logging.getLogger("p0.access").info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round(elapsed * 1000, 1),
                "user": user.get("username", ""),
            },
        )
    return response


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
    data = auth.provider_status()
    if data["oidc"].get("enabled"):
        data["oidc"]["start_url"] = f"{ROOT_PATH}/api/v1/auth/oidc/start"
    return data


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path=ROOT_PATH or "/",
    )


def _sso_failure(reason: str, detail: str = "") -> RedirectResponse:
    auth.audit(
        {
            "user": "",
            "role": "",
            "event": "sso_login_failed",
            "method": "GET",
            "path": "/api/v1/auth/oidc/callback",
            "status": 400,
            "reason": reason,
            "detail": detail[:200],
        }
    )
    target = (ROOT_PATH or "") + "/login?sso_error=" + url_quote(reason)
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(oidc.STATE_COOKIE, path=ROOT_PATH or "/")
    return response


@app.get("/api/v1/auth/oidc/start")
def oidc_start(request: Request, next: str = "/") -> Response:
    config = oidc.config(oidc.redirect_uri(request))
    if not config or not oidc.enabled():
        raise HTTPException(status_code=404, detail="SSO 未配置")
    try:
        document = oidc.discovery(config)
    except oidc.OidcError as exc:
        raise HTTPException(status_code=502, detail=f"SSO 发现失败：{exc}") from exc
    state = oidc.new_state(next)
    response = RedirectResponse(oidc.authorize_url(config, document, state), status_code=303)
    response.set_cookie(
        oidc.STATE_COOKIE,
        oidc.sign_state(state),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path=ROOT_PATH or "/",
    )
    return response


@app.get("/api/v1/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "") -> Response:
    config = oidc.config(oidc.redirect_uri(request))
    if not config or not oidc.enabled():
        return _sso_failure("not_configured")
    payload = oidc.read_state(request.cookies.get(oidc.STATE_COOKIE, ""))
    if not payload or not secrets.compare_digest(str(payload.get("s", "")), state or ""):
        return _sso_failure("state")
    if not code:
        return _sso_failure("code")
    try:
        document = oidc.discovery(config)
        tokens = oidc.exchange_code(config, document, code, payload["v"])
        claims = oidc.validate_id_token(config, document, tokens["id_token"], payload["n"])
        extra = {}
        if not oidc.identity_values(claims):
            extra = oidc.userinfo(config, document, tokens.get("access_token", ""))
    except oidc.OidcError as exc:
        return _sso_failure("token", str(exc))
    user = None
    for value in oidc.identity_values(claims, extra):
        user = auth.find_user_by_identity(value)
        if user:
            break
    if not user or not user.get("active", True):
        return _sso_failure("account")
    token = auth.create_session(user)
    target = (ROOT_PATH or "") + oidc.safe_next(payload.get("next"))
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, token)
    response.delete_cookie(oidc.STATE_COOKIE, path=ROOT_PATH or "/")
    auth.audit(
        {
            "user": user["username"],
            "role": user.get("role", ""),
            "event": "sso_login_success",
            "method": "GET",
            "path": "/api/v1/auth/oidc/callback",
            "status": 200,
            "ip": _client_ip(request),
        }
    )
    return response


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
    _set_session_cookie(response, token)
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
    filters = _audit_filters(user, role, event, method, path, status, date_from, date_to)
    total = auth.count_audit(**filters)
    _audit_event(request, actor, "audit_exported", rows=total)
    return StreamingResponse(
        auth.audit_csv_stream(auth.iter_filtered_audit(**filters)),
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
    if isinstance(result.get("config"), dict):
        result["config"]["stage2"] = _stage2_catalog()
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


class ReviewAction(BaseModel):
    rule_id: str
    status: str
    note: str = ""


class ItemPatch(BaseModel):
    clause: str
    name: str | None = None
    requirement: str | None = None
    method: str | None = None


class ReviewRequest(BaseModel):
    items: list[ItemPatch] = Field(default_factory=list)
    reviewer: str = ""


class RollbackRequest(BaseModel):
    version: int = Field(ge=0)
    reviewer: str = ""


class AskRequest(BaseModel):
    question: str
    standard_id: str | None = None
    mode: str = "auto"
    min_score: float | None = None
    top_k: int | None = None


class FeedbackRequest(BaseModel):
    question: str = ""
    answer: str = ""
    rating: str
    reason: str = ""
    comment: str = ""
    standard_id: str = ""
    clause: str = ""
    retrieval_mode: str = ""
    citations: list[dict] = Field(default_factory=list)


@app.get("/metrics")
def metrics(request: Request) -> PlainTextResponse:
    token = os.getenv("P0_METRICS_TOKEN", "")
    if token:
        supplied = request.headers.get("x-metrics-token") or request.query_params.get("token", "")
        if not secrets.compare_digest(str(supplied), token):
            raise HTTPException(status_code=403, detail="metrics token required")
    else:
        host = request.client.host if request.client else ""
        production = os.getenv("P0_ENV", "").strip().lower() == "production"
        if production or host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(
                status_code=403,
                detail="metrics endpoint requires P0_METRICS_TOKEN in production",
            )
    return PlainTextResponse(
        monitoring.METRICS.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/api/v1/system/monitor")
def system_monitor(request: Request) -> dict:
    _require_permission(request, "system:status")
    return monitoring.snapshot()


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
def _stage2_catalog() -> dict:
    """Read-only stage-2 rule metadata for the workbench and rules API."""
    pack = rule_dsl.load_pack()
    semantic = semantic_audit.load_config()
    return {
        "partial": {
            "rules": partial_rules.partial_rule_catalog(),
            "total": len(partial_rules.PARTIAL_RULES),
        },
        "dsl": {
            "version": pack.version,
            "total": len(pack.rules),
            "rules": rule_dsl.rule_catalog(pack),
            "errors": rule_dsl.validate_pack(),
        },
        "semantic": {
            "enabled": bool(semantic.enabled and semantic.api_key),
            "configured": bool(semantic.api_key),
            "model": semantic.model,
            "checks": semantic.checks,
        },
    }


@app.get("/api/v1/rules")
def list_rules(request: Request) -> dict:
    _require_permission(request, "rules:read")
    rules = rules_catalog(admin_config.report_config())
    return {
        "rules": rules,
        "enabled": sum(1 for rule in rules if rule["enabled"]),
        "total": len(rules),
        "stage2": _stage2_catalog(),
    }


@app.get("/api/v1/reports/inputs/status")
def report_inputs_status(request: Request) -> dict:
    _require_permission(request, "report:read")
    directory = os.getenv("P0_REPORT_INPUTS_DIR", "").strip()
    snapshot = _load_report_inputs()
    if snapshot is None:
        return {
            "configured": bool(directory),
            "source_dir": directory,
            "counts": {},
            "datasets": {},
            "warnings": [],
            "errors": [] if not directory else ["无法读取 P0_REPORT_INPUTS_DIR，报告审核将只运行报告内规则"],
        }
    payload = snapshot.to_dict()
    payload["configured"] = True
    return payload


_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_REPORT_ID = re.compile(r"^rp_[0-9]{14}_[0-9a-f]{6}$")
_REPORT_BATCH_ID = re.compile(r"^rb_[0-9]{14}_[0-9a-f]{6}$")
_REPORT_STATUS_LABELS = {"pass": "通过", "warn": "提示", "fail": "失败", "skip": "未检查"}


def _new_report_id(prefix: str) -> str:
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3)


async def _read_report_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="only .docx reports are supported in this slice")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    return data


def _load_report_inputs():
    """Load the optional local inbox; unavailable data must not block audit."""
    directory = os.getenv("P0_REPORT_INPUTS_DIR", "").strip()
    if not directory:
        return None
    try:
        return report_inputs.load_inputs(directory)
    except Exception:  # noqa: BLE001 - audit degrades to report-only rules
        return None


def _audit_report_bytes(
    data: bytes, filename: str, *, user: dict, project: str, batch_id: str = "", inputs=None
) -> dict:
    config = admin_config.load()
    rules_revision = int(config.get("revision", 0) or 0)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        parsed = parse_docx(tmp_path)
        parsed.file_name = filename
        if inputs is None:
            inputs = _load_report_inputs()
        result = audit_report(parsed, config.get("report") or {}, inputs=inputs, stage2=True)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    report_id = _new_report_id("rp_")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "id": report_id,
        "batch_id": batch_id,
        "project": project,
        "created_by": user.get("username", ""),
        "created_at": created_at,
        "file_name": filename,
        "file_size": len(data),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "template": result.get("template", ""),
        "rules_revision": rules_revision,
        "summary": dict(result.get("summary") or {}),
    }
    report_store.create(record, result, data)
    return {
        **result,
        "report_id": report_id,
        "project": project,
        "created_at": created_at,
        "batch_id": batch_id,
        "rules_revision": rules_revision,
    }


def _report_project(user: dict, project: str) -> str:
    project = (project or "").strip()
    if not auth.can_create_in_project(user, project):
        raise HTTPException(status_code=400, detail="受限账号必须填写有权限的项目标识")
    return project


@app.post("/api/v1/reports/audit")
async def audit_report_file(
    request: Request, file: UploadFile = File(...), project: str = Form("")
) -> dict:
    user = _require_permission(request, "report:audit")
    project = _report_project(user, project)
    data = await _read_report_upload(file)
    try:
        result = _audit_report_bytes(
            data, file.filename or "report.docx", user=user, project=project
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse report: {exc}") from exc
    summary = result.get("summary") or {}
    _audit_event(
        request, user, "report_audited", report_id=result["report_id"],
        file_name=file.filename, project=project,
        fail=summary.get("fail", 0), pass_count=summary.get("pass", 0),
    )
    return result


@app.post("/api/v1/reports/visual-check")
async def report_visual_check(request: Request, file: UploadFile = File(...)) -> dict:
    """Photo-dependent checks (duplicate photos / equipment label state).

    Off by default: set P0_VISUAL_CHECKS=1. Equipment-label detection additionally
    needs P0_VISUAL_LABEL_ENDPOINT and customer sample photos; until then it reports
    skipped:model_not_configured instead of pretending to pass.
    """
    _require_permission(request, "report:audit")
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="only .docx reports are supported in this slice")
    data = await file.read()
    try:
        return visual_checks.check_docx(data)
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"visual check failed: {exc}") from exc


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
        result = audit_report(parsed, admin_config.report_config(), inputs=_load_report_inputs(), stage2=True)
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


def _report_export_rows(records: list[dict]) -> tuple[list[list[object]], list[list[object]]]:
    summary_rows: list[list[object]] = [
        ["文件", "项目", "审核人", "审核时间", "批次", "通过", "提示", "失败", "未检查", "已复核", "复核覆盖率"]
    ]
    finding_rows: list[list[object]] = [
        ["文件", "规则", "标题", "状态", "严重级别", "定位", "说明", "证据", "建议", "复核状态", "备注"]
    ]
    review_labels = {"accepted": "采纳", "ignored": "忽略", "false_positive": "误报"}
    for record in records:
        summary = record.get("summary") or {}
        result = record.get("result") or {}
        reviews = record.get("reviews") or {}
        findings = result.get("findings") or []
        reviewed = sum(1 for finding in findings if finding.get("rule_id") in reviews)
        coverage = f"{reviewed}/{len(findings)}" if findings else "0/0"
        summary_rows.append(
            [
                record.get("file_name", ""),
                record.get("project", ""),
                record.get("created_by", ""),
                record.get("created_at", ""),
                record.get("batch_id", "") or "—",
                summary.get("pass", 0),
                summary.get("warn", 0),
                summary.get("fail", 0),
                summary.get("skip", 0),
                reviewed,
                coverage,
            ]
        )
        for finding in findings:
            status = str(finding.get("status", ""))
            location = finding.get("location") or {}
            location_text = " / ".join(
                str(part)
                for part in (
                    f"p{location.get('page')}" if location.get("page") else "",
                    location.get("section", ""),
                    location.get("cell", ""),
                )
                if part
            )
            review = reviews.get(finding.get("rule_id"), {})
            finding_rows.append(
                [
                    record.get("file_name", ""),
                    finding.get("rule_id", ""),
                    finding.get("title", ""),
                    _REPORT_STATUS_LABELS.get(status, status),
                    finding.get("severity", ""),
                    location_text,
                    finding.get("message", ""),
                    finding.get("evidence", ""),
                    finding.get("suggestion", ""),
                    review_labels.get(review.get("status"), "待复核"),
                    review.get("note", ""),
                ]
            )
    return summary_rows, finding_rows


def _report_export_bytes(records: list[dict]) -> bytes:
    summary_rows, finding_rows = _report_export_rows(records)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report-review.xlsx"
        xlsx_writer.write_xlsx(
            path,
            [
                ("审核汇总", summary_rows, [32, 18, 14, 22, 22, 10, 10, 10, 10, 10, 12]),
                ("问题清单", finding_rows, [28, 10, 24, 10, 10, 28, 56, 56, 56, 10, 30]),
            ],
        )
        return path.read_bytes()


def _report_export_name(record: dict) -> str:
    stem = Path(record.get("file_name") or "report").stem or "report"
    return f"{stem}-审核结果.xlsx"


def _enqueue_report_batch(
    request: Request,
    user: dict,
    *,
    entries: list[tuple[str, bytes]],
    project: str,
    batch_id: str,
) -> JSONResponse:
    from .store import objects

    payload = {"batch_id": batch_id, "project": project, "created_by": user["username"], "files": []}
    task = tasks.create(
        "report_batch",
        payload,
        created_by=user["username"],
        project=project,
        max_attempts=int(os.getenv("P0_TASKS_MAX_ATTEMPTS", "2")),
    )
    metas = []
    with store.db.engine().begin() as conn:
        for index, (name, data) in enumerate(entries):
            record = objects.upsert_file(
                conn,
                owner_type="task",
                owner_id=task["id"],
                name=f"{index:03d}-{name}",
                data=data,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            metas.append({"file_id": record["id"], "file_name": name})
    task = tasks.set_payload(task["id"], {**payload, "files": metas})
    _audit_event(
        request, user, "report_batch_queued", task_id=task["id"],
        batch_id=batch_id, files=len(entries), project=project,
    )
    return _task_response(task)


@app.post("/api/v1/reports/batch")
async def audit_report_batch(
    request: Request, files: list[UploadFile] = File(...), project: str = Form("")
) -> dict:
    user = _require_permission(request, "report:audit")
    project = _report_project(user, project)
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一份报告")
    batch_max = int(os.getenv("P0_REPORT_BATCH_MAX", "20"))
    if len(files) > batch_max:
        raise HTTPException(status_code=400, detail=f"一次最多 {batch_max} 份报告")
    byte_limit = int(os.getenv("P0_REPORT_BATCH_MB", "100")) * 1024 * 1024
    entries: list[tuple[str, bytes | Exception]] = []
    consumed = 0
    for file in files:
        name = file.filename or "report.docx"
        try:
            data = await _read_report_upload(file)
            consumed += len(data)
            if consumed > byte_limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"批次总大小超过 {byte_limit // 1024 // 1024} MB",
                )
            entries.append((name, data))
        except HTTPException as exc:
            entries.append((name, exc))
    batch_id = _new_report_id("rb_")
    uploads = [(name, payload) for name, payload in entries if isinstance(payload, bytes)]
    if _task_switch("P0_TASKS_REPORT_BATCH_ASYNC", default="1") and len(uploads) == len(entries):
        return _enqueue_report_batch(
            request, user, entries=uploads, project=project, batch_id=batch_id
        )
    result = report_batch.run_batch(
        entries,
        user=user,
        project=project,
        batch_id=batch_id,
        audit=_audit_report_bytes,
        inputs=_load_report_inputs(),
    )
    _audit_event(
        request, user, "report_batch_audited", batch_id=batch_id,
        files=len(entries), stored=result["summary"]["stored"], project=project,
    )
    return result


@app.get("/api/v1/reports")
def list_report_history(
    request: Request, limit: int = 20, offset: int = 0, q: str = "", batch_id: str = ""
) -> dict:
    user = _require_permission(request, "report:read")
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    query = (q or "").strip().lower()
    rows = [
        row
        for row in report_store.list_reports()
        if auth.can_access_project(user, row.get("project", ""), row.get("created_by", ""))
    ]
    if query:
        rows = [
            row
            for row in rows
            if query in f"{row.get('file_name', '')} {row.get('project', '')}".lower()
        ]
    if batch_id:
        rows = [row for row in rows if row.get("batch_id") == batch_id]
    return {
        "reports": rows[offset:offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


_REVIEW_STATUSES = {"accepted", "ignored", "false_positive"}


def _review_coverage(record: dict, reviews: dict) -> dict:
    findings = (record.get("result") or {}).get("findings") or []
    reviewed = sum(1 for finding in findings if finding.get("rule_id") in reviews)
    return {"reviewed": reviewed, "total": len(findings), "covered": bool(findings) and reviewed == len(findings)}


@app.get("/api/v1/reports/{report_id}")
def get_report_record(report_id: str, request: Request) -> dict:
    user = _require_permission(request, "report:read")
    if not _REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="report not found")
    try:
        record = report_store.load(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    _assert_project_access(user, record)
    reviews = report_store.load_reviews(report_id)
    record["reviews"] = reviews
    record["review_coverage"] = _review_coverage(record, reviews)
    return record


@app.post("/api/v1/reports/{report_id}/reviews")
def review_report_finding(report_id: str, payload: ReviewAction, request: Request) -> dict:
    user = _require_permission(request, "report:read")
    if not _REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="report not found")
    if payload.status not in _REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="review status must be accepted/ignored/false_positive")
    try:
        record = report_store.load(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    _assert_project_access(user, record)
    findings = (record.get("result") or {}).get("findings") or []
    if not any(finding.get("rule_id") == payload.rule_id for finding in findings):
        raise HTTPException(status_code=404, detail="finding not found")
    reviews = report_store.load_reviews(report_id)
    reviews[payload.rule_id] = {
        "status": payload.status,
        "note": payload.note[:500],
        "by": user.get("username", ""),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    report_store.save_reviews(report_id, reviews)
    _audit_event(request, user, "report_reviewed", report_id=report_id, rule_id=payload.rule_id, status=payload.status)
    return {"reviews": reviews, "review_coverage": _review_coverage(record, reviews)}


@app.get("/api/v1/reports/{report_id}/source.docx")
def download_report_source(report_id: str, request: Request) -> Response:
    user = _require_permission(request, "report:read")
    if not _REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="report not found")
    try:
        record = report_store.load(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    _assert_project_access(user, record)
    try:
        data = report_store.read_source(report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="原始报告未留存") from None
    _audit_event(request, user, "report_source_downloaded", report_id=report_id)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": _attachment_header(record.get("file_name") or "report.docx")
        },
    )


@app.get("/api/v1/reports/{report_id}/export.xlsx")
def export_report_record(report_id: str, request: Request) -> Response:
    user = _require_permission(request, "report:read")
    if not _REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="report not found")
    try:
        record = report_store.load(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    _assert_project_access(user, record)
    record["reviews"] = report_store.load_reviews(report_id)
    data = _report_export_bytes([record])
    _audit_event(request, user, "report_exported", report_id=report_id)
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": _attachment_header(_report_export_name(record))},
    )


@app.get("/api/v1/reports/batch/{batch_id}/export.xlsx")
def export_report_batch(batch_id: str, request: Request) -> Response:
    user = _require_permission(request, "report:read")
    if not _REPORT_BATCH_ID.fullmatch(batch_id):
        raise HTTPException(status_code=404, detail="batch not found")
    rows = [
        row
        for row in report_store.list_reports()
        if row.get("batch_id") == batch_id
        and auth.can_access_project(user, row.get("project", ""), row.get("created_by", ""))
    ]
    records: list[dict] = []
    for row in rows:
        try:
            record = report_store.load(row["id"])
            record["reviews"] = report_store.load_reviews(row["id"])
            records.append(record)
        except KeyError:
            continue
    if not records:
        raise HTTPException(status_code=404, detail="batch not found")
    data = _report_export_bytes(records)
    _audit_event(request, user, "report_batch_exported", batch_id=batch_id, reports=len(records))
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": _attachment_header(f"报告批量审核-{batch_id}.xlsx")
        },
    )


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


@app.get("/api/v1/standards/{standard_id}/versions")
def list_standard_versions(standard_id: str, request: Request) -> dict:
    user = _require_permission(request, "standard:read")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    try:
        versions = standard_store.list_versions(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    return {"versions": versions}


@app.post("/api/v1/standards/{standard_id}/unpublish")
def unpublish_standard(standard_id: str, request: Request) -> dict:
    user = _require_permission(request, "standard:publish")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    actor = user.get("display_name") or user["username"]
    try:
        result = standard_store.unpublish(standard_id, actor)
    except weknora.WeKnoraError as exc:
        raise HTTPException(status_code=502, detail=f"WeKnora 下架失败：{exc}") from exc
    _audit_event(
        request, user, "standard_unpublished", standard_id=standard_id,
        removed=len(result.get("removed_knowledge_ids") or []),
    )
    return result


@app.post("/api/v1/standards/{standard_id}/rollback")
def rollback_standard(
    standard_id: str, payload: RollbackRequest, request: Request
) -> dict:
    user = _require_permission(request, "standard:review")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    current = int(record.get("revision", 0) or 0)
    if payload.version >= current:
        raise HTTPException(status_code=409, detail="只能回滚到更早的修订")
    reviewer = payload.reviewer or user.get("display_name") or user["username"]
    was_published = bool(record.get("published"))
    try:
        standard_store.version_record(standard_id, payload.version)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="该修订没有可回滚的快照") from exc
    try:
        if was_published:
            standard_store.unpublish(standard_id, reviewer)
        updated = standard_store.rollback(standard_id, payload.version, reviewer)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="该修订没有可回滚的快照") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except weknora.WeKnoraError as exc:
        raise HTTPException(status_code=502, detail=f"WeKnora 下架失败：{exc}") from exc
    _audit_event(
        request, user, "standard_rolled_back", standard_id=standard_id,
        from_version=payload.version, revision=updated.get("revision", 0),
        unpublished=was_published,
    )
    return updated


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


_FEEDBACK_RATINGS = {"helpful", "not_helpful"}


def _feedback_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@app.post("/api/v1/standards/ask/feedback")
def record_ask_feedback(payload: FeedbackRequest, request: Request) -> dict:
    user = _require_permission(request, "standard:ask")
    rating = (payload.rating or "").strip()
    if rating not in _FEEDBACK_RATINGS:
        raise HTTPException(status_code=422, detail="rating 必须是 helpful 或 not_helpful")
    project = ""
    if payload.standard_id:
        try:
            standard = standard_store.load(payload.standard_id)
        except KeyError:
            project = ""
        else:
            _assert_project_access(user, standard.get("meta", {}))
            project = (standard.get("meta") or {}).get("project", "")
    citations: list[dict] = []
    for item in (payload.citations or [])[:10]:
        if not isinstance(item, dict):
            continue
        standard_id = str(item.get("standard_id", ""))[:64]
        clause = str(item.get("clause", ""))[:64]
        if standard_id or clause:
            citations.append({"standard_id": standard_id, "clause": clause})
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "id": "fb_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(3),
        "at": at,
        "user": user.get("username", ""),
        "role": user.get("role", ""),
        "rating": rating,
        "reason": _feedback_text(payload.reason, 64),
        "comment": _feedback_text(payload.comment, 500),
        "question": _feedback_text(payload.question, 1000),
        "answer_excerpt": _feedback_text(payload.answer, 2000),
        "standard_id": _feedback_text(payload.standard_id, 64),
        "clause": _feedback_text(payload.clause, 128),
        "retrieval_mode": _feedback_text(payload.retrieval_mode, 32),
        "citations": citations,
        "project": project,
    }
    feedback_store.create(record)
    _audit_event(
        request, user, "standard_ask_feedback", rating=rating,
        standard_id=record["standard_id"], clause=record["clause"],
    )
    return {"id": record["id"], "at": record["at"], "rating": rating}


@app.get("/api/v1/kb/feedback")
def list_qa_feedback(request: Request, limit: int = 20, offset: int = 0) -> dict:
    _require_permission(request, "feedback:read")
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    rows = feedback_store.list_feedback()
    helpful = sum(1 for row in rows if row.get("rating") == "helpful")
    not_helpful = sum(1 for row in rows if row.get("rating") == "not_helpful")
    reasons = Counter(
        str(row.get("reason", ""))
        for row in rows
        if row.get("rating") == "not_helpful" and row.get("reason")
    )
    return {
        "totals": {"total": len(rows), "helpful": helpful, "not_helpful": not_helpful},
        "reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common(6)],
        "items": rows[offset:offset + limit],
        "limit": limit,
        "offset": offset,
    }


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
        meta.update(
            {
                "summary": result.summary(),
                "all_priced": bool(result.all_priced),
                "has_sales_quote": bool(sales_verification),
                "verification_ok": bool(sales_verification and sales_verification.get("ok")),
                "sales_skipped_reason": sales_skipped_reason,
                "crm": {
                    "mode": reconciliation.get("mode", "off"),
                    "reconciled": bool(reconciliation.get("reconciled")),
                    "matched": int(reconciliation.get("matched", 0) or 0),
                    "missing": len(reconciliation.get("missing") or []),
                    "price_mismatch": len(reconciliation.get("price_mismatch") or []),
                },
            }
        )
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


@app.get("/api/v1/quotes")
def list_quote_history(
    request: Request, limit: int = 20, offset: int = 0, q: str = ""
) -> dict:
    user = _require_permission(request, "quote:download")
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    query = (q or "").strip().lower()
    rows = [
        row
        for row in quote_store.list_quotes(base_dir=QUOTE_DIR)
        if auth.can_access_project(user, row.get("project", ""), row.get("created_by", ""))
    ]
    if query:
        rows = [
            row
            for row in rows
            if query in f"{row.get('source_name', '')} {row.get('project', '')}".lower()
        ]
    return {
        "quotes": rows[offset:offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


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
