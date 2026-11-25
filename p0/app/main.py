"""P0 API: report review, standard parsing, quote assistant and KB Q&A.

Endpoints are intentionally small and versioned under /api/v1 so the future
review workbenches can call them without coupling to parser internals.
"""
from __future__ import annotations

import hashlib
import base64
import binascii
import csv
import hmac
from collections import Counter
from io import StringIO
import json
import math
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
from . import business_rule_versions
from . import auth
from . import role_policy
from . import customer_registration
from . import customer_region_routing
from . import customer_quote_flow
from . import customer_order_flow
from . import customer_service_desk
from . import customer_service_preview
from . import customer_service_export
from . import customer_business_rules
from . import quote as quote_service
from . import report_batch
from . import crm, monitoring, quote_store, store, task_worker
from . import feedback_store, oidc, report_store, report_writer, report_inputs, model_readiness, partial_rules, rule_dsl, semantic_audit, standard_parse, standard_qa, standard_store, weknora
from . import visual_checks
from . import xlsx_writer
from .standard_extract import items_list, samples as samples_extract
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
    role: str = Field(min_length=1)
    display_name: str = ""
    department: str = ""
    projects: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    role: str | None = None
    active: bool | None = None
    display_name: str | None = None
    department: str | None = None
    projects: list[str] | None = None


class ReviewPermissionRequest(BaseModel):
    permissions: list[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1, max_length=500)


class OidcIdentityRequest(BaseModel):
    issuer: str = Field(min_length=8, max_length=512)
    subject: str = Field(default="", max_length=512)


class ResetPasswordRequest(BaseModel):
    new_password: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class S1QuotePreviewLine(BaseModel):
    item_name: str = Field(default="", max_length=500)
    standard: str = Field(default="", max_length=300)
    sample_name: str = Field(default="", max_length=500)
    quantity: float | None = Field(default=None, gt=0)
    unit: str = Field(default="", max_length=80)
    source_row: int | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class S1QuotePreviewRequest(BaseModel):
    project_name: str = Field(default="", max_length=300)
    review_id: str = Field(min_length=38, max_length=38)
    lines: list[S1QuotePreviewLine] = Field(min_length=1, max_length=200)


class S1PriceCandidateReviewRequest(S1QuotePreviewLine):
    project_name: str = Field(min_length=1, max_length=300)
    source_mode: str = Field(min_length=1, max_length=20)
    source_revision: str = Field(min_length=1, max_length=100)
    line_id: str = Field(min_length=1, max_length=120)
    selected_record_id: str = Field(min_length=1, max_length=200)


class CustomerBusinessRulePreviewRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=300)
    company_name: str = Field(min_length=1, max_length=300)
    subtotal: float = Field(ge=0, le=1_000_000_000)
    rush_requested: bool = False


class CustomerBusinessRuleReplayRequest(CustomerBusinessRulePreviewRequest):
    pass


class QuoteParseReviewRow(BaseModel):
    line_index: int = Field(ge=0, le=199)
    source_row: int = Field(ge=1)
    item_name: str = Field(default="", max_length=500)
    standard: str = Field(default="", max_length=300)
    sample_name: str = Field(default="", max_length=500)
    quantity: float | None = None
    unit: str = Field(default="", max_length=80)


def _is_public(path: str) -> bool:
    registration_demo = path.startswith("/api/v1/customer-service/demo/registrations/") or path == "/api/v1/customer-service/demo/registrations"
    order_demo = path.startswith("/api/v1/customer-service/demo/orders")
    conversation_demo = path.startswith("/api/v1/customer-service/demo/conversations")
    return registration_demo or order_demo or conversation_demo or path in PUBLIC_PATHS or path == "/api/v1/auth/login" or path.startswith(PUBLIC_PREFIXES)


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
        request.state.user = {
            "username": "test", "role": "admin", "display_name": "test",
            "active": True, "_auth_disabled_test": True,
        }
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE_NAME, "")
    user = auth.read_session(token) if token else None
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        return _login_redirect(request)
    if user.get("role") == "customer" and path != "/api/v1/auth/logout":
        if path.startswith("/api/"):
            return JSONResponse({"detail": "客户账号只能通过微信小程序访问业务服务"}, status_code=403)
        return PlainTextResponse("客户账号只能通过微信小程序访问业务服务", status_code=403)
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
    # P0_AUTH_DISABLED is a hermetic test-only mode (never set by systemd).
    # Preserve legacy integration tests without weakening production RBAC.
    if user.get("_auth_disabled_test"):
        request.state.action = action
        return user
    if not auth.has_permission(user, action):
        raise HTTPException(status_code=403, detail=f"需要权限：{action}")
    request.state.action = action
    return user


def _audit_event(request: Request, user: dict, event: str, **extra) -> bool:
    return auth.audit(
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
    except oidc.OidcError as exc:
        return _sso_failure("token", str(exc))
    user = auth.find_user_by_oidc(str(claims.get("iss", "")), str(claims.get("sub", "")))
    if not user or not user.get("active", True) or user.get("role") == "customer":
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
    return {"user": auth.public_user(_require_user(request))}


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


@app.put("/api/v1/auth/users/{username}/review-permissions")
def update_review_permissions(
    username: str, payload: ReviewPermissionRequest, request: Request
) -> dict:
    actor = _require_permission(request, "user:manage")
    try:
        permissions = role_policy.normalize_review_permissions(payload.permissions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.reason.strip():
        raise HTTPException(status_code=400, detail="逐账号授权必须填写原因")
    target = auth.find_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.get("role") not in {"business_manager", "admin", "technical"}:
        raise HTTPException(status_code=400, detail="审核权限只能授予已确认四角色中的员工账号")
    if username.lower() == actor["username"].lower():
        raise HTTPException(status_code=403, detail="管理员不能为本人授予审核例外权限")
    if not auth.audit({
        "user": actor["username"],
        "role": actor["role"],
        "event": "account_review_permissions_change_requested",
        "target": username,
        "permissions": permissions,
        "reason": payload.reason.strip(),
        "method": "PUT",
        "path": request.url.path,
        "status": 200,
        "ip": _client_ip(request),
    }):
        raise HTTPException(status_code=503, detail="审计记录写入失败，未更新审核权限")
    try:
        user = auth.set_review_permissions(
            username,
            permissions,
            granted_by=actor["username"],
            reason=payload.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user}


@app.put("/api/v1/auth/users/{username}/oidc-identity")
def bind_user_oidc_identity(
    username: str, payload: OidcIdentityRequest, request: Request
) -> dict:
    actor = _require_permission(request, "user:manage")
    if not payload.subject.strip():
        raise HTTPException(status_code=400, detail="OIDC subject 不能为空")
    target = auth.find_user(username)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.get("role") not in {"business_manager", "admin", "technical"}:
        raise HTTPException(status_code=400, detail="OIDC 身份只能绑定到已确认四角色中的员工账号")
    if not auth.audit({
        "user": actor["username"], "role": actor["role"],
        "event": "oidc_identity_bind_requested", "target": username,
        "issuer": payload.issuer.strip().rstrip("/"),
        "method": "PUT", "path": request.url.path, "status": 200,
        "ip": _client_ip(request),
    }):
        raise HTTPException(status_code=503, detail="审计记录写入失败，未绑定 OIDC 身份")
    try:
        user = auth.bind_oidc_identity(username, payload.issuer, payload.subject)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user}


@app.delete("/api/v1/auth/users/{username}/oidc-identity")
def unbind_user_oidc_identity(
    username: str, payload: OidcIdentityRequest, request: Request
) -> dict:
    actor = _require_permission(request, "user:manage")
    if not payload.issuer.strip():
        raise HTTPException(status_code=400, detail="OIDC issuer 不能为空")
    if not auth.audit({
        "user": actor["username"], "role": actor["role"],
        "event": "oidc_identity_unbind_requested", "target": username,
        "issuer": payload.issuer.strip().rstrip("/"),
        "method": "DELETE", "path": request.url.path, "status": 200,
        "ip": _client_ip(request),
    }):
        raise HTTPException(status_code=503, detail="审计记录写入失败，未解除 OIDC 身份")
    try:
        user = auth.unbind_oidc_identity(username, payload.issuer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        "model_routing": model_readiness.snapshot(),
    }


CONFIG_SECTION_ACTIONS = {
    section: f"config:manage:{section}" for section in admin_config.SECTIONS
}


def _require_config(request: Request, section: str = "") -> dict:
    """Admins keep all sections; each department edits only its own section."""
    if section and section not in admin_config.SECTIONS:
        raise HTTPException(status_code=400, detail=f"未知配置分区：{section}")
    user = _require_user(request)
    for name in ([section] if section else admin_config.SECTIONS):
        if auth.has_permission(user, CONFIG_SECTION_ACTIONS[name]):
            request.state.action = CONFIG_SECTION_ACTIONS[name]
            return user
    raise HTTPException(status_code=403, detail="需要规则配置权限")


def _redact_region_routing(value: dict, *, admin: bool) -> dict:
    """Remove employee directory mappings from non-admin business responses."""
    if admin:
        return value

    def scrub(item: Any) -> None:
        if isinstance(item, dict):
            item.pop("region_routing", None)
            for key in ("changed_fields", "changed_fields_vs_live"):
                fields = item.get(key)
                if isinstance(fields, list):
                    item[key] = [field for field in fields if field != "region_routing"]
            for nested in item.values():
                scrub(nested)
        elif isinstance(item, list):
            for nested in item:
                scrub(nested)

    scrub(value)
    return value


def _redact_customer_lists(value: dict, *, admin: bool) -> dict:
    """Keep blacklist and priority-customer lists in administrator responses only."""
    if admin:
        return value
    def scrub(item: Any) -> None:
        if isinstance(item, dict):
            item.pop("blacklist", None)
            item.pop("whitelist", None)
            for key in ("changed_fields", "changed_fields_vs_live"):
                fields = item.get(key)
                if isinstance(fields, list):
                    item[key] = [field for field in fields if field not in {"blacklist", "whitelist"}]
            for nested in item.values():
                scrub(nested)
        elif isinstance(item, list):
            for nested in item:
                scrub(nested)
    scrub(value)
    return value


def _redact_customer_risk_candidates(value: dict, *, admin: bool) -> dict:
    """Prevent business-draft replay from becoming a private-list oracle."""
    if admin:
        return value
    private_signals = {
        "blacklist_match", "whitelist_match", "whitelist_keyword_match",
        "whitelist_keyword_matches", "company_list_conflict",
    }
    def scrub(item: Any) -> None:
        if isinstance(item, dict):
            candidates = item.get("candidates")
            if isinstance(candidates, dict):
                for key in private_signals:
                    candidates.pop(key, None)
            for nested in item.values():
                scrub(nested)
        elif isinstance(item, list):
            for nested in item:
                scrub(nested)
    scrub(value)
    return value


@app.get("/api/v1/admin/config")
def get_admin_config(request: Request) -> dict:
    user = _require_config(request)
    result = admin_config.describe()
    if user.get("role") != "admin":
        result.pop("path", None)
        business = result.get("config", {}).get("business", {})
        if isinstance(business, dict):
            business.pop("region_routing", None)
        _redact_customer_lists(result, admin=False)
    return result


@app.get("/api/v1/config/effective")
def get_effective_config(request: Request) -> dict:
    """Read-only config for the module pages, without the internal file path."""
    user = _require_permission(request, "rules:read")
    result = admin_config.describe()
    result.pop("path", None)
    result.pop("sections", None)
    if isinstance(result.get("config"), dict):
        result["config"]["stage2"] = _stage2_catalog()
        # Region→manager routing data is employee directory information and is
        # only returned to administrators by the configuration API.
        business = result["config"].get("business")
        if isinstance(business, dict):
            business.pop("region_routing", None)
        _redact_customer_lists(result, admin=user.get("role") == "admin")
    return result


@app.put("/api/v1/admin/config/{section}")
def update_admin_config(section: str, payload: dict, request: Request) -> dict:
    user = _require_config(request, section)
    if section == "business" and user.get("role") != "admin" and "region_routing" in payload:
        raise HTTPException(status_code=403, detail="地区经理映射仅管理员可维护")
    if section == "business" and user.get("role") != "admin" and {"blacklist", "whitelist"}.intersection(payload):
        raise HTTPException(status_code=403, detail="黑白名单仅管理员可维护")
    expected_revision = None
    if section == "business" and user.get("role") == "admin" and {"blacklist", "whitelist"}.intersection(payload):
        current_snapshot = admin_config.describe()
        if current_snapshot.get("warning"):
            raise HTTPException(status_code=503, detail="业务配置不可用，已停止修改")
        expected_revision = payload.get("expected_revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise HTTPException(status_code=422, detail="提交黑白名单配置必须包含配置版本")
        if expected_revision != current_snapshot["config"].get("revision", 0):
            raise HTTPException(status_code=409, detail="业务配置已变化，请刷新后重试")
        before = current_snapshot["config"]["business"]
        try:
            candidate_payload = {key: value for key, value in payload.items() if key != "expected_revision"}
            requested = admin_config.validate_section(
                "business", admin_config._deep_merge(before, candidate_payload)
            )
        except admin_config.ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        old_blacklist = set(before["blacklist"]["companies"])
        new_blacklist = set(requested["blacklist"]["companies"])
        old_whitelist = set(before["whitelist"]["companies"])
        new_whitelist = set(requested["whitelist"]["companies"])
        old_keywords = set(before["whitelist"].get("keywords", []))
        new_keywords = set(requested["whitelist"].get("keywords", []))
        delta = {
            "blacklist_added": len(new_blacklist - old_blacklist),
            "blacklist_removed": len(old_blacklist - new_blacklist),
            "whitelist_added": len(new_whitelist - old_whitelist),
            "whitelist_removed": len(old_whitelist - new_whitelist),
            "priority_keywords_added": len(new_keywords - old_keywords),
            "priority_keywords_removed": len(old_keywords - new_keywords),
        }
        if any(delta.values()):
            if not _audit_event(request, user, "customer_business_lists_update_requested",
                                **delta, config_revision=expected_revision):
                raise HTTPException(status_code=503, detail="客户名单操作审计不可用，名单未修改")
    try:
        admin_config.update_section(section, payload, updated_by=user.get("username", ""),
                                    expected_revision=expected_revision)
    except admin_config.ConfigRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except admin_config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = admin_config.describe()
    if user.get("role") != "admin":
        business = result.get("config", {}).get("business", {})
        if isinstance(business, dict):
            for field in ("region_routing", "blacklist", "whitelist"):
                business.pop(field, None)
        _redact_customer_lists(result, admin=False)
    _audit_event(
        request, user, "admin_config_updated",
        section=section, revision=result["config"]["revision"],
    )
    return result


@app.post("/api/v1/admin/config/{section}/reset")
def reset_admin_config(section: str, request: Request, expected_revision: int | None = None) -> dict:
    user = _require_config(request, section)
    if section == "business":
        snapshot = admin_config.describe()
        if snapshot.get("warning"):
            raise HTTPException(status_code=503, detail="业务配置不可用，已停止重置")
        if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise HTTPException(status_code=422, detail="恢复业务默认值必须提交配置版本")
        if expected_revision != snapshot["config"].get("revision", 0):
            raise HTTPException(status_code=409, detail="业务配置已变化，请刷新后重试")
        if user.get("role") == "admin":
            business = snapshot["config"]["business"]
            if not _audit_event(
                request, user, "customer_business_config_reset_requested",
                blacklist_count=len(business["blacklist"]["companies"]),
                whitelist_count=len(business["whitelist"]["companies"]),
                priority_keyword_count=len(business["whitelist"].get("keywords", [])),
                config_revision=expected_revision,
            ):
                raise HTTPException(status_code=503, detail="业务名单重置审计不可用，配置未修改")
    try:
        admin_config.reset_section(
            section, updated_by=user.get("username", ""),
            preserve_fields=("region_routing", "blacklist", "whitelist")
            if section == "business" and user.get("role") != "admin" else (),
            expected_revision=expected_revision,
        )
    except admin_config.ConfigRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except admin_config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = admin_config.describe()
    if user.get("role") != "admin":
        business = result.get("config", {}).get("business", {})
        if isinstance(business, dict):
            business.pop("region_routing", None)
        _redact_customer_lists(result, admin=False)
    _audit_event(
        request, user, "admin_config_reset",
        section=section, revision=result["config"]["revision"],
    )
    return result


def _parse_blacklist_csv(content: str) -> list[str]:
    """Parse a one-column, bounded UTF-8 CSV; F1-F4 fail closed on bad input."""
    # F1 empty file/header-only -> reject; F2 malformed or multi-column -> reject;
    # F3 overlong rows/items/count -> reject; F4 spreadsheet formula prefixes
    # exported with an apostrophe are restored only on this trusted round trip.
    if not isinstance(content, str):
        raise ValueError("CSV 内容必须是文本")
    if len(content.encode("utf-8")) > 200_000:
        raise ValueError("CSV 文件超过 200 KB")
    try:
        rows = list(csv.reader(StringIO(content.lstrip("\ufeff"), newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError("CSV 格式错误") from exc
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("CSV 中没有企业名称")
    first = [cell.strip() for cell in rows[0]]
    if len(first) != 1:
        raise ValueError("黑名单 CSV 必须只有一列：企业名称")
    if first[0].casefold() in {"企业名称", "公司名称", "company_name"}:
        rows = rows[1:]
    companies = []
    for row_number, row in enumerate(rows, start=2):
        if len(row) != 1:
            raise ValueError(f"第 {row_number} 行必须只有一个企业名称")
        company = row[0].strip()
        if not company:
            continue
        if company.startswith("'") and len(company) > 1 and company[1] in "=+-@\t\r":
            company = company[1:]
        companies.append(company)
        if len(companies) > 500:
            raise ValueError("单次最多导入 500 家企业")
    if not companies:
        raise ValueError("CSV 中没有企业名称")
    return companies


@app.get("/api/v1/admin/config/business/blacklist.csv")
def export_business_blacklist(request: Request) -> Response:
    user = _require_admin(request)
    snapshot = admin_config.describe()
    if snapshot.get("warning"):
        raise HTTPException(status_code=503, detail="业务配置不可用，已停止导出")
    companies = snapshot["config"]["business"]["blacklist"]["companies"]
    if not _audit_event(request, user, "blacklist_export_requested", rows=len(companies),
                        config_revision=snapshot["config"].get("revision", 0)):
        raise HTTPException(status_code=503, detail="黑名单导出审计不可用")
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["企业名称"])
    for company in companies:
        value = str(company)
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            value = "'" + value
        writer.writerow([value])
    content = "\ufeff" + output.getvalue()
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=blacklist.csv"})


@app.post("/api/v1/admin/config/business/blacklist/import")
def import_business_blacklist(request: Request, payload: dict) -> dict:
    user = _require_admin(request)
    snapshot = admin_config.describe()
    if snapshot.get("warning"):
        raise HTTPException(status_code=503, detail="业务配置不可用，已停止导入")
    expected_revision = payload.get("expected_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise HTTPException(status_code=422, detail="必须提交当前业务配置版本")
    if expected_revision != snapshot["config"].get("revision", 0):
        raise HTTPException(status_code=409, detail="业务配置已变化，请刷新后重新导入")
    try:
        imported = _parse_blacklist_csv(payload.get("csv_text", ""))
        mode = payload.get("mode", "merge")
        if mode not in {"merge", "replace"}:
            raise ValueError("导入方式必须是 merge 或 replace")
        previous = snapshot["config"]["business"]["blacklist"]["companies"]
        companies = list(dict.fromkeys((previous + imported) if mode == "merge" else imported))
        # Reuse canonical config validation before recording or saving the mutation.
        admin_config.validate_section("business", snapshot["config"]["business"] | {
            "blacklist": {"companies": companies},
        })
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    added = len(set(companies) - set(previous))
    removed = len(set(previous) - set(companies))
    if not _audit_event(request, user, "blacklist_import_requested", mode=mode,
                        input_rows=len(imported), added=added, removed=removed,
                        config_revision=snapshot["config"].get("revision", 0)):
        raise HTTPException(status_code=503, detail="黑名单导入审计不可用，名单未修改")
    try:
        updated = admin_config.update_blacklist(
            companies, snapshot["config"].get("revision", 0), user.get("username", "")
        )
    except admin_config.ConfigRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except admin_config.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_event(request, user, "blacklist_imported", mode=mode, input_rows=len(imported),
                 added=added, removed=removed, company_count=len(companies),
                 config_revision=updated["revision"])
    result = admin_config.describe()
    result["blacklist_import"] = {
        "mode": mode, "input_rows": len(imported), "added": added,
        "removed": removed, "company_count": len(companies),
    }
    return result


@app.get("/api/v1/admin/config/business/drafts")
def list_business_rule_drafts(request: Request) -> dict:
    user = _require_config(request, "business")
    try:
        result = {"drafts": business_rule_versions.list_drafts(), "published": False}
    except business_rule_versions.DraftStoreError as exc:
        raise HTTPException(status_code=503, detail="业务规则草稿库不可用") from exc
    return _redact_region_routing(
        _redact_customer_lists(result, admin=user.get("role") == "admin"),
        admin=user.get("role") == "admin",
    )


@app.post("/api/v1/admin/config/business/drafts", status_code=201)
def create_business_rule_draft(request: Request, payload: dict) -> dict:
    user = _require_config(request, "business")
    if user.get("role") != "admin" and {"region_routing", "blacklist", "whitelist"}.intersection(payload):
        raise HTTPException(status_code=403, detail="地区经理映射和黑白名单仅管理员可维护")
    if not _audit_event(request, user, "customer_business_rule_draft_create_requested"):
        raise HTTPException(status_code=503, detail="规则草稿审计暂不可用")
    try:
        draft = business_rule_versions.create_draft(
            payload, created_by=user.get("username", "")
        )
    except business_rule_versions.DraftValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except business_rule_versions.DraftStoreError as exc:
        raise HTTPException(status_code=503, detail="规则草稿存储或现行配置不可用") from exc
    result = {"draft": draft, "published": False}
    return _redact_region_routing(_redact_customer_lists(result, admin=user.get("role") == "admin"), admin=user.get("role") == "admin")


@app.get("/api/v1/admin/config/business/drafts/{draft_id}")
def get_business_rule_draft(draft_id: str, request: Request) -> dict:
    user = _require_config(request, "business")
    try:
        draft = business_rule_versions.get_draft(draft_id)
    except business_rule_versions.DraftStoreError as exc:
        raise HTTPException(status_code=503, detail="业务规则草稿库不可用") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则草稿不存在") from exc
    result = {"draft": draft, "published": False}
    return _redact_region_routing(_redact_customer_lists(result, admin=user.get("role") == "admin"), admin=user.get("role") == "admin")


@app.get("/api/v1/admin/config/business/drafts/{draft_id}/compare")
def compare_business_rule_draft(draft_id: str, request: Request) -> dict:
    user = _require_config(request, "business")
    try:
        result = business_rule_versions.compare_to_live(draft_id)
    except business_rule_versions.DraftStoreError as exc:
        raise HTTPException(status_code=503, detail="业务规则草稿库或现行配置不可用") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则草稿不存在") from exc
    return _redact_region_routing(_redact_customer_lists(result, admin=user.get("role") == "admin"), admin=user.get("role") == "admin")


@app.post("/api/v1/admin/config/business/drafts/{draft_id}/replay")
def replay_business_rule_draft(
    draft_id: str, request: Request, payload: CustomerBusinessRuleReplayRequest
) -> dict:
    user = _require_config(request, "business")
    project_name = payload.project_name.strip()
    company_name = payload.company_name.strip()
    if not project_name or not company_name:
        raise HTTPException(status_code=422, detail="项目名称和企业名称不能为空")
    _assert_project_access(user, {"project": project_name})
    try:
        draft = business_rule_versions.get_draft(draft_id)
        comparison = business_rule_versions.compare_to_live(draft_id)
        result = customer_business_rules.evaluate_candidates(
            draft["business"],
            company_name=company_name,
            subtotal=payload.subtotal,
            rush_requested=payload.rush_requested,
        )
    except business_rule_versions.DraftStoreError as exc:
        raise HTTPException(status_code=503, detail="规则草稿或现行配置不可用") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则草稿不存在") from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=503, detail="规则草稿无效，已停止重放") from exc

    company_fingerprint = hmac.new(
        auth.state_secret(), company_name.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not _audit_event(
        request,
        user,
        "customer_business_rule_draft_replayed",
        project=project_name,
        draft_id=draft_id,
        company_fingerprint=company_fingerprint,
        subtotal=payload.subtotal,
        rush_requested=payload.rush_requested,
        result=result,
    ):
        raise HTTPException(status_code=503, detail="规则重放审计暂不可用，请稍后重试")
    response = {
        **result,
        "draft_id": draft_id,
        "base_revision": draft["base_revision"],
        "live_revision": comparison["live_revision"],
        "stale": comparison["stale"],
        "published": False,
        "scope": "read_only_draft_replay",
        "message": "仅用于内部规则版本复核；没有改变现行规则或生成正式业务结论",
    }
    return _redact_customer_risk_candidates(response, admin=user.get("role") == "admin")


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
    method_clause: str | None = None
    applicable_parts: list[str] | None = None
    reviewed: bool | None = None


class AddItemPatch(BaseModel):
    clause: str
    name: str
    requirement: str = ""
    method_clause: str = ""
    method: str = ""
    page: int | None = None


class ReviewRequest(BaseModel):
    items: list[ItemPatch] = Field(default_factory=list)
    additions: list[AddItemPatch] = Field(default_factory=list)
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


def _async_parse_wanted(data: bytes, suffix: str = ".pdf") -> bool:
    if not _task_switch("P0_TASKS_PARSE_ASYNC"):
        return False
    threshold = _async_parse_min_bytes()
    if threshold == 0 or len(data) >= threshold:
        return True
    # Scanned PDFs are often small; sample the text layer before deciding.
    return suffix == ".pdf" and standard_parse.looks_scanned(data)


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
    if not file.filename:
        raise HTTPException(status_code=400, detail=standard_parse.SUPPORTED_STANDARD_HINT)
    filename = file.filename
    suffix = Path(filename).suffix.lower()
    if suffix not in standard_parse.SUPPORTED_STANDARD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式：{suffix or '无扩展名'}；{standard_parse.SUPPORTED_STANDARD_HINT}",
        )
    data = await file.read()
    if len(data) > MAX_STANDARD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_STANDARD_BYTES // 1024 // 1024} MB",
        )
    if _async_parse_wanted(data, suffix):
        return _enqueue_parse(
            request, user, data=data, filename=filename,
            project=project, enable_ocr=enable_ocr,
        )
    try:
        parsed = standard_parse.parse_standard_document(
            data,
            filename,
            enable_ocr=enable_ocr,
            rule_config=admin_config.standard_config(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse standard: {exc}") from exc
    record = standard_store.create(
        parsed, data, filename, project=project, created_by=user["username"]
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
    # Read-time draft rules: low-confidence limits stay outside ``rules`` and
    # surface in ``pending`` so the workbench can mark them 待确认.
    payload = dict(record)
    payload["judgment_rules"] = rule_dsl.generate_judgment_rules(record.get("items") or [])
    payload["items_list"] = items_list.build(
        record.get("meta") or {}, record.get("items") or []
    )
    return payload


def _load_sample_guide(standard_id: str, request: Request) -> tuple[dict, dict]:
    """Load a standard and build its sample guide (also checks project access)."""
    user = _require_permission(request, "standard:read")
    try:
        record = standard_store.load(standard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    _assert_project_access(user, record.get("meta", {}))
    guide = samples_extract.build_sample_guide(
        record.get("samples") or [], record.get("items") or []
    )
    return user, guide


@app.get("/api/v1/standards/{standard_id}/sample-guide")
def get_sample_guide(standard_id: str, request: Request) -> dict:
    """Generate the sample-preparation guide from the parsed standard."""
    _user, guide = _load_sample_guide(standard_id, request)
    return {"standard_id": standard_id, "guide": guide}


@app.post("/api/v1/standards/{standard_id}/sample-guide/validate")
def validate_sample_guide(standard_id: str, payload: dict, request: Request) -> dict:
    """Check commission-order sample info (``order`` object) against the guide."""
    user, guide = _load_sample_guide(standard_id, request)
    result = samples_extract.check_sample_order(guide, payload.get("order") or payload)
    _audit_event(
        request, user, "sample_guide_validated",
        standard_id=standard_id, mismatches=len(result["mismatches"]),
    )
    return {"standard_id": standard_id, "guide": guide, **result}


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
            additions=[item.model_dump() for item in payload.additions],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="standard not found") from exc
    except weknora.WeKnoraError as exc:
        # 取消确认需要先把已发布内容下架；下架失败时整次复核不生效。
        raise HTTPException(status_code=502, detail=f"WeKnora 下架失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit_event(
        request, user, "standard_reviewed", standard_id=standard_id,
        revision=result.get("revision", 0), changes=len(payload.items),
        additions=len(payload.additions),
        samples=len(result.get("review_feedback") or []),
        published=bool(result.get("published", True)),
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
    # 未确认不入库：reject before queuing so an unconfirmed standard cannot
    # fail later inside the async worker.
    blocker = standard_store.publish_blocker(record)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)
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
        chunks=result.get("chunks", 0), items=result.get("items", 0),
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


def _quote_config_fingerprint(config: dict) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _quote_safe_filename(filename: str) -> str:
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def _issue_quote_parse_token(
    user: dict, source_sha256: str, filename: str, project: str, config: dict
) -> str:
    expires_at = int(time.time()) + 15 * 60
    payload = "\n".join((
        str(user.get("username", "")), source_sha256,
        _quote_safe_filename(filename), project, _quote_config_fingerprint(config),
        str(expires_at), secrets.token_urlsafe(12),
    ))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(auth.state_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_quote_parse_token(
    token: str, user: dict, source_sha256: str, filename: str, project: str, config: dict
) -> bool:
    if not token or len(token) > 2048:
        return False
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(auth.state_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(expected, signature):
            return False
        padded = encoded + "=" * (-len(encoded) % 4)
        parts = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split("\n")
        if len(parts) != 7:
            return False
        username, token_sha256, token_filename, token_project, config_sha256, expires_at, _nonce = parts
        return (
            secrets.compare_digest(
                username.encode("utf-8"), str(user.get("username", "")).encode("utf-8")
            )
            and secrets.compare_digest(token_sha256, source_sha256)
            and secrets.compare_digest(
                token_filename.encode("utf-8"), _quote_safe_filename(filename).encode("utf-8")
            )
            and secrets.compare_digest(token_project.encode("utf-8"), project.encode("utf-8"))
            and secrets.compare_digest(config_sha256, _quote_config_fingerprint(config))
            and int(expires_at) >= int(time.time())
        )
    except (ValueError, UnicodeDecodeError, TypeError, binascii.Error):
        return False


def _quote_input_name(file: UploadFile) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail=quote_service.SUPPORTED_INPUT_HINT)
    # The parser derives sample names and DVP behavior from the filename. Make
    # Windows and POSIX upload paths canonical before both parsing and signing.
    filename = _quote_safe_filename(file.filename).strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail=quote_service.SUPPORTED_INPUT_HINT)
    suffix = Path(filename).suffix.lower()
    if suffix not in quote_service.SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=400, detail=quote_service.SUPPORTED_INPUT_HINT)
    return filename


@app.post("/api/v1/quotes/parse")
async def parse_quote_file(
    request: Request, file: UploadFile = File(...), project: str = Form("")
) -> dict:
    user = _require_permission(request, "quote:parse")
    project = str(project or "").strip()
    if project:
        _assert_project_access(user, {"project": project})
    filename = _quote_input_name(file)
    data = await file.read()
    _quote_upload_bytes(data)
    try:
        quote_config = admin_config.quote_config()
        result = quote_service.parse_quote_bytes(
            data, filename, rule_config=quote_config
        ).to_dict()
        result["source_sha256"] = hashlib.sha256(data).hexdigest()
        result["parse_token"] = _issue_quote_parse_token(
            user, result["source_sha256"], filename, project, quote_config
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - API boundary
        raise HTTPException(status_code=422, detail=f"failed to parse quote input: {exc}") from exc


@app.post("/api/v1/quotes/review")
async def review_parsed_quote_file(
    request: Request,
    file: UploadFile = File(...),
    project: str = Form(...),
    source_sha256: str = Form(...),
    parse_token: str = Form(...),
    edits: str = Form("[]"),
) -> dict:
    # F1: permission/project scope is checked before parsing or audit writes.
    # F2: unsupported/corrupt/unparseable input returns 4xx with no review event.
    # F3: changed row count/order/source_row is rejected; source evidence is immutable.
    # F4: oversized/invalid field edits are rejected before audit or preview.
    # F5: failed audit persistence never reports a completed review.
    # F6: only non-price fields are editable; parser confidence/source row and source price stay untouched.
    # F7: this route never writes CRM or generates a customer quotation.
    user = _require_permission(request, "quote:parse")
    project = str(project or "").strip()
    if not project:
        raise HTTPException(status_code=422, detail="复核解析结果必须填写项目标识")
    _assert_project_access(user, {"project": project})
    filename = _quote_input_name(file)
    data = await file.read()
    _quote_upload_bytes(data)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256 or "") or not secrets.compare_digest(actual_sha256, source_sha256):
        raise HTTPException(status_code=409, detail="复核文件与最初解析文件不一致，请重新解析")
    quote_config = admin_config.quote_config()
    if not _valid_quote_parse_token(parse_token, user, actual_sha256, filename, project, quote_config):
        raise HTTPException(status_code=409, detail="首次解析凭证无效、已过期或报价规则已变化，请重新解析")
    try:
        parsed = quote_service.parse_quote_bytes(
            data, filename, rule_config=quote_config
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - keep parser failures inside the API boundary
        raise HTTPException(status_code=422, detail=f"解析失败，未记录复核：{exc}") from exc

    try:
        raw_edits = json.loads(edits)
        if not isinstance(raw_edits, list) or len(raw_edits) != len(parsed.lines):
            raise ValueError("复核行数与解析行数不一致，请重新解析")
        reviewed = [QuoteParseReviewRow.model_validate(row) for row in raw_edits]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "复核行数据格式无效") from exc

    lines = []
    audit_rows = []
    editable_fields = ("item_name", "standard", "sample_name", "quantity", "unit")
    for index, (source, revision) in enumerate(zip(parsed.lines, reviewed)):
        if revision.line_index != index or revision.source_row != source.source_row:
            raise HTTPException(status_code=409, detail="解析行顺序或来源行已变化，请重新解析文件")
        if revision.quantity is not None and (
            not math.isfinite(revision.quantity) or revision.quantity <= 0
        ):
            raise HTTPException(status_code=422, detail=f"第 {index + 1} 行数量必须大于 0 或留空待补")
        original = {
            "item_name": source.item_name,
            "standard": source.standard,
            "sample_name": source.sample_name,
            "quantity": source.quantity if source.quantity is not None and source.quantity > 0 else None,
            "unit": source.unit,
        }
        current = {field: getattr(revision, field) for field in editable_fields}
        changes = {
            field: {"before": original[field], "after": current[field]}
            for field in editable_fields if original[field] != current[field]
        }
        audit_rows.append({
            "line_index": index,
            "source_row": source.source_row,
            "parse_confidence": source.confidence,
            "reviewed_values": {
                field: {"before": original[field], "after": current[field]}
                for field in editable_fields
            },
            "changes": changes,
        })
        lines.append({
            **current,
            "source_row": source.source_row,
            "confidence": source.confidence,
        })

    review_id = "CS-QUOTE-PARSE-REVIEW-" + secrets.token_hex(8)
    recorded = _audit_event(
        request, user, "customer_service_quote_parse_reviewed",
        review_id=review_id, project=project, source_name=Path(filename).name,
        source_sha256=source_sha256, parse_method=parsed.parse_method,
        line_count=len(lines), reviewed_rows=audit_rows,
        effect="仅记录内部解析复核，不生成报价、不写回 CRM",
    )
    if not recorded:
        raise HTTPException(status_code=503, detail="复核结果未能写入审计记录，请稍后重试")
    return {
        "review_id": review_id,
        "recorded": True,
        "project": project,
        "source_name": Path(filename).name,
        "source_sha256": source_sha256,
        "parse_method": parsed.parse_method,
        "warnings": list(parsed.warnings),
        "lines": lines,
        "effect": "仅用于后续内部只读预览；不产生客户报价或 CRM 写入",
    }


def _verified_quote_parse_review(
    review_id: str, user: dict, project: str, lines: list[S1QuotePreviewLine]
) -> bool:
    """Match a preview request to the caller's exact, audited reviewed rows."""
    if not re.fullmatch(r"CS-QUOTE-PARSE-REVIEW-[a-f0-9]{16}", review_id or ""):
        return False
    try:
        events = auth.filter_audit(
            user=str(user.get("username", "")),
            event="customer_service_quote_parse_reviewed",
        )
    except Exception:  # noqa: BLE001 - audit lookup failures fail closed
        return False
    expected_fields = ("item_name", "standard", "sample_name", "quantity", "unit")
    for event in events:
        if event.get("review_id") != review_id or event.get("project") != project:
            continue
        rows = event.get("reviewed_rows")
        if not isinstance(rows, list) or len(rows) != len(lines):
            continue
        matched = True
        for index, (line, row) in enumerate(zip(lines, rows)):
            if (
                row.get("line_index") != index
                or row.get("source_row") != line.source_row
                or row.get("parse_confidence") != line.confidence
            ):
                matched = False
                break
            values = row.get("reviewed_values")
            if not isinstance(values, dict):
                matched = False
                break
            for field_name in expected_fields:
                pair = values.get(field_name)
                if not isinstance(pair, dict) or pair.get("after") != getattr(line, field_name):
                    matched = False
                    break
            if not matched:
                break
        if matched:
            return True
    return False


def _verified_quote_parse_line(
    review_id: str, user: dict, project: str, line_index: int, line: S1QuotePreviewLine
) -> bool:
    if line_index < 0 or not re.fullmatch(r"CS-QUOTE-PARSE-REVIEW-[a-f0-9]{16}", review_id or ""):
        return False
    try:
        events = auth.filter_audit(
            user=str(user.get("username", "")),
            event="customer_service_quote_parse_reviewed",
        )
    except Exception:  # noqa: BLE001 - audit lookup failures fail closed
        return False
    fields = ("item_name", "standard", "sample_name", "quantity", "unit")
    for event in events:
        if event.get("review_id") != review_id or event.get("project") != project:
            continue
        rows = event.get("reviewed_rows")
        if not isinstance(rows, list) or line_index >= len(rows):
            continue
        row = rows[line_index]
        if (
            row.get("line_index") != line_index
            or row.get("source_row") != line.source_row
            or row.get("parse_confidence") != line.confidence
        ):
            continue
        values = row.get("reviewed_values")
        if isinstance(values, dict) and all(
            isinstance(values.get(field), dict)
            and values[field].get("after") == getattr(line, field)
            for field in fields
        ):
            return True
    return False


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
        reconciliation = crm.reconcile_quote(
            result,
            project_scope=project if auth.is_restricted(user) else None,
        )
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
# S1 synthetic customer-service quote preview (internal, opt-in only)
# ---------------------------------------------------------------------------
def _require_customer_demo() -> None:
    if (not customer_registration.enabled()
            or os.getenv("P0_ENV", "").strip().lower() == "production"):
        raise HTTPException(status_code=404, detail="客户注册本地演示未启用")


def _require_customer_order_demo() -> None:
    enabled = os.getenv("P0_CUSTOMER_ORDER_DEMO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if (not enabled or not customer_registration.enabled()
            or os.getenv("P0_ENV", "").strip().lower() == "production"):
        raise HTTPException(status_code=404, detail="客户委托本地演示未启用")


@app.post("/api/v1/customer-service/demo/registrations", status_code=201)
def create_customer_demo_registration(request: Request, payload: dict) -> dict:
    _require_customer_demo()
    operation_key = request.headers.get("idempotency-key", "").strip()
    if not operation_key:
        raise HTTPException(status_code=400, detail="请提供登记操作编号")
    try:
        return customer_registration.create(payload, operation_key)
    except customer_registration.RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/v1/customer-service/demo/registrations/{registration_id}")
def update_customer_demo_registration(registration_id: str, request: Request, payload: dict) -> dict:
    _require_customer_demo()
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 登记更新操作编号")
    try:
        result, replayed = customer_registration.update_owned(
            registration_id, request.headers.get("x-demo-access-token", ""), payload, operation_id
        )
        return {**result, "idempotent_replay": replayed}
    except customer_registration.RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/customer-service/demo/registrations/{registration_id}/submit")
def submit_customer_demo_registration(registration_id: str, request: Request) -> dict:
    _require_customer_demo()
    try:
        return customer_registration.submit(
            registration_id, request.headers.get("x-demo-access-token", "")
        )
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/customer-service/demo/registrations/{registration_id}")
def get_customer_demo_registration(registration_id: str, request: Request) -> dict:
    _require_customer_demo()
    try:
        return customer_registration.get_owned(
            registration_id, request.headers.get("x-demo-access-token", "")
        )
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 403, detail=str(exc)) from exc


@app.get("/api/v1/customer-service/demo/admin/registrations")
def list_customer_demo_registrations(request: Request, status: str = "") -> dict:
    _require_customer_demo()
    _require_permission(request, "customer-service:registration:review")
    result = customer_registration.list_admin(status)
    config_snapshot = admin_config.describe()
    for registration in result.get("registrations", []):
        # F1: only the authorized business-manager queue can read customer records.
        # F2: invalid config is unknown, never an implicit no-risk result.
        # F3: exact matches and keywords are review hints; they never approve/reject.
        # F4: candidates stay in this employee endpoint and are not persisted to customer data.
        registration["rule_review"] = _registration_rule_review(
            registration.get("company_name", ""), config_snapshot
        )
        registration["assignment_preview"] = _registration_assignment_preview(
            registration.get("region", ""), config_snapshot
        )
    return result


def _registration_rule_review(company_name: str, snapshot: dict | None = None) -> dict:
    try:
        snapshot = snapshot or admin_config.describe()
        if snapshot.get("warning"):
            return {"status": "unavailable", "decision": "manual_review", "requires_human_review": True}
        config = snapshot["config"]
        candidates = customer_business_rules.evaluate_candidates(
            admin_config.business_config(config), company_name=company_name,
            subtotal=0, rush_requested=False,
        )["candidates"]
    except (KeyError, OSError, TypeError, ValueError, OverflowError):
        return {"status": "unavailable", "decision": "manual_review", "requires_human_review": True}
    return {
        "status": "candidate",
        "decision": "manual_review",
        "requires_human_review": True,
        "config_revision": config.get("revision", 0),
        "blacklist_match": candidates["blacklist_match"],
        "whitelist_match": candidates["whitelist_match"],
        "whitelist_keyword_match": candidates["whitelist_keyword_match"],
        "company_list_conflict": candidates["company_list_conflict"],
        "intermediary_keyword_match": bool(candidates["intermediary_keywords"]),
        "intermediary_block_configured": candidates["intermediary_block_configured"],
    }


def _registration_assignment_preview(region: str, snapshot: dict | None = None) -> dict:
    try:
        snapshot = snapshot or admin_config.describe()
        if snapshot.get("warning"):
            return {
                "status": "unavailable", "candidate_manager_id": "", "route_type": "none",
                "config_revision": None, "requires_human_review": True,
                "assigned": False, "notification_sent": False,
            }
        config = snapshot["config"]
        business = admin_config.business_config(config)
        return customer_region_routing.preview(
            region, business.get("region_routing", {}), config.get("revision", 0)
        )
    except (KeyError, OSError, TypeError, ValueError, OverflowError):
        return {
            "status": "unavailable", "candidate_manager_id": "", "route_type": "none",
            "config_revision": None, "requires_human_review": True,
            "assigned": False, "notification_sent": False,
        }


@app.post("/api/v1/customer-service/demo/admin/registrations/{registration_id}/review")
def review_customer_demo_registration(registration_id: str, request: Request, payload: dict) -> dict:
    # F1: only an authorized business manager can read and review the registration.
    # F2: an unknown registration is hidden as not found.
    # F3: a stale UI rule revision cannot be used to record a decision.
    # F4: invalid rules remain unavailable/manual review, never a clean match.
    # F5: the internal event retains the rule snapshot; customer history does not.
    # F6: revision validation and state write share the config file lock across workers.
    # F7: stale registration revisions/company names cannot reuse an old queue snapshot.
    # F8: malformed, fractional, boolean, and oversized rule revisions fail with 422.
    # F9: cross-process config writers cannot interleave between rule snapshot and review write.
    _require_customer_demo()
    reviewer = _require_permission(request, "customer-service:registration:review")
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 登记审核操作编号")
    decision = str(payload.get("decision", ""))
    reason = str(payload.get("reason", ""))
    operation_payload = {
        "decision": decision, "reason": reason,
        "rule_config_revision": payload.get("rule_config_revision"),
        "registration_revision": payload.get("registration_revision"),
    }
    try:
        replay = customer_registration.review_replay(registration_id, reviewer.get("username", ""), operation_id, operation_payload)
    except customer_registration.RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 503, detail=str(exc)) from exc
    if replay is not None:
        return {"demo": True, "registration": replay, "idempotent_replay": True}
    registrations = customer_registration.list_admin()["registrations"]
    target = next((item for item in registrations if item.get("registration_id") == registration_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="演示注册不存在")
    with admin_config._locked():
        config_snapshot = admin_config.describe()
        rule_review = _registration_rule_review(target.get("company_name", ""), config_snapshot)
        assignment_preview = _registration_assignment_preview(target.get("region", ""), config_snapshot)
        try:
            replay = customer_registration.review_replay(registration_id, reviewer.get("username", ""), operation_id, operation_payload)
        except customer_registration.RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except customer_registration.RegistrationError as exc:
            raise HTTPException(status_code=404 if "不存在" in str(exc) else 503, detail=str(exc)) from exc
        if replay is not None:
            return {"demo": True, "registration": replay, "idempotent_replay": True}
        if rule_review.get("status") != "candidate":
            raise HTTPException(status_code=503, detail="业务规则不可用，已暂停登记审核")
        expected_revision = payload.get("rule_config_revision")
        if isinstance(expected_revision, bool) or not (
            isinstance(expected_revision, int)
            or (isinstance(expected_revision, str) and len(expected_revision) <= 100 and re.fullmatch(r"[0-9]+", expected_revision))
        ):
            raise HTTPException(status_code=422, detail="必须提交当前规则版本，请刷新审核队列")
        try:
            expected_revision = int(expected_revision)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="规则版本格式无效，请刷新审核队列") from exc
        if expected_revision != rule_review.get("config_revision"):
            raise HTTPException(status_code=409, detail="业务规则已变化或不可用，请刷新审核队列后再审核")
        expected_registration_revision = payload.get("registration_revision")
        if isinstance(expected_registration_revision, bool) or not isinstance(expected_registration_revision, int):
            raise HTTPException(status_code=422, detail="必须提交登记资料版本，请刷新审核队列")
        try:
            result, replayed = customer_registration.review(
                registration_id, decision, reason,
                reviewer.get("username", ""),
                rule_review={
                    **rule_review,
                    "routing_status": assignment_preview["status"],
                    "routing_candidate_manager_id": assignment_preview["candidate_manager_id"],
                    "routing_route_type": assignment_preview["route_type"],
                    "routing_config_revision": assignment_preview["config_revision"],
                },
                expected_revision=expected_registration_revision,
                expected_company_name=target.get("company_name", ""),
                operation_id=operation_id, operation_payload=operation_payload, with_replay=True,
            )
        except customer_registration.RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except customer_registration.RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not replayed:
        _audit_event(request, reviewer, "customer_demo_registration_reviewed",
                     registration_id=registration_id, decision=result["status"],
                     rule_review=rule_review, assignment_preview=assignment_preview)
    return {"demo": True, "registration": result, "idempotent_replay": replayed}


@app.post("/api/v1/customer-service/demo/orders", status_code=201)
def create_customer_demo_order(request: Request, payload: dict) -> dict:
    _require_customer_order_demo()
    registration_id = str(payload.pop("registration_id", "")).strip()
    token = request.headers.get("x-demo-access-token", "")
    operation_id = request.headers.get("idempotency-key", "")
    if not registration_id or not token:
        raise HTTPException(status_code=403, detail="请先完成本地演示登记并提供登记凭证")
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 委托操作编号")
    try:
        order, replayed = customer_order_flow.create(payload, registration_id, token, operation_id)
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=409 if isinstance(exc, customer_order_flow.OrderFlowConflict) else 400, detail=str(exc)) from exc
    if not replayed:
        _audit_event(request, {"username": "customer-demo", "role": "customer"}, "customer_demo_order_submitted", order_id=order["order_id"])
    return {"demo": True, "idempotent_replay": replayed, "order": order}


@app.get("/api/v1/customer-service/demo/orders")
def list_customer_demo_orders(request: Request, registration_id: str = "") -> dict:
    _require_customer_order_demo()
    token = request.headers.get("x-demo-access-token", "")
    if not registration_id or not token:
        raise HTTPException(status_code=403, detail="请提供本地演示登记凭证")
    try:
        # Validate ownership through the registration store before reading orders.
        customer_registration.get_owned(registration_id, token)
        return {"demo": True, "orders": customer_order_flow.list_owned(token)}
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=403, detail="登记凭证无效") from exc
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/customer-service/demo/orders/{order_id}")
def get_customer_demo_order(order_id: str, request: Request, registration_id: str = "") -> dict:
    _require_customer_order_demo()
    token = request.headers.get("x-demo-access-token", "")
    if not registration_id or not token:
        raise HTTPException(status_code=403, detail="请提供本地演示登记凭证")
    try:
        customer_registration.get_owned(registration_id, token)
        return {"demo": True, "order": customer_order_flow.get_owned(order_id, token)}
    except customer_registration.RegistrationError as exc:
        raise HTTPException(status_code=403, detail="登记凭证无效") from exc
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=404, detail="演示委托不存在") from exc


@app.patch("/api/v1/customer-service/demo/orders/{order_id}")
def update_customer_demo_order(order_id: str, request: Request, payload: dict, registration_id: str = "") -> dict:
    _require_customer_order_demo()
    token = request.headers.get("x-demo-access-token", "")
    operation_id = request.headers.get("idempotency-key", "")
    if not registration_id or not token:
        raise HTTPException(status_code=403, detail="请提供本地演示登记凭证")
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 补件操作编号")
    try:
        order, replayed = customer_order_flow.update_owned(order_id, payload, registration_id, token, operation_id)
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=409 if isinstance(exc, customer_order_flow.OrderFlowConflict) else 400, detail=str(exc)) from exc
    if not replayed:
        _audit_event(request, {"username": "customer-demo", "role": "customer"}, "customer_demo_order_resubmitted", order_id=order_id)
    return {"demo": True, "idempotent_replay": replayed, "order": order}


@app.post("/api/v1/customer-service/demo/orders/{order_id}/confirmation")
def confirm_customer_demo_order(order_id: str, request: Request, payload: dict, registration_id: str = "") -> dict:
    _require_customer_order_demo()
    token = request.headers.get("x-demo-access-token", "")
    if not registration_id or not token:
        raise HTTPException(status_code=403, detail="请提供本地演示登记凭证")
    try:
        order, replayed = customer_order_flow.confirm_owned(
            order_id, str(payload.get("decision", "")), str(payload.get("note", "")),
            registration_id, token,
        )
    except customer_order_flow.OrderFlowError as exc:
        status_code = 409 if isinstance(exc, customer_order_flow.OrderFlowConflict) else 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    _audit_event(
        request, {"username": "customer-demo", "role": "customer"},
        "customer_demo_order_confirmation_replayed" if replayed else "customer_demo_order_confirmed",
        order_id=order_id, decision=payload.get("decision", ""),
    )
    return {"demo": True, "idempotent_replay": replayed, "order": order}


@app.get("/api/v1/customer-service/demo/admin/orders")
def list_customer_demo_orders_admin(request: Request, status: str = "") -> dict:
    _require_customer_order_demo()
    _require_permission(request, "customer-service:order:review")
    if status not in {"", "submitted", "needs_info", "approved", "rejected", "customer_confirmed", "customer_declined"}:
        raise HTTPException(status_code=422, detail="委托状态筛选值无效")
    try:
        return {"demo": True, "orders": customer_order_flow.list_admin(status)}
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/customer-service/demo/admin/orders/{order_id}/review")
def review_customer_demo_order(order_id: str, request: Request, payload: dict) -> dict:
    _require_customer_order_demo()
    reviewer = _require_permission(request, "customer-service:order:review")
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 委托审核操作编号")
    expected_revision = payload.get("expected_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise HTTPException(status_code=422, detail="必须提交委托资料版本，请刷新审核队列")
    try:
        order, replayed = customer_order_flow.review(
            order_id, str(payload.get("decision", "")), str(payload.get("reason", "")),
            reviewer.get("username", ""), operation_id,
            expected_revision=expected_revision, with_replay=True,
        )
    except customer_order_flow.OrderFlowError as exc:
        raise HTTPException(status_code=409 if isinstance(exc, customer_order_flow.OrderFlowConflict) else 400, detail=str(exc)) from exc
    if not replayed:
        _audit_event(request, reviewer, "customer_demo_order_reviewed", order_id=order_id, decision=order["status"])
    return {"demo": True, "idempotent_replay": replayed, "order": order}


def _require_customer_service_desk_demo() -> None:
    enabled = os.getenv("P0_CUSTOMER_SERVICE_DESK_DEMO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if (not enabled or not customer_registration.enabled()
            or os.getenv("P0_ENV", "").strip().lower() == "production"):
        raise HTTPException(status_code=404, detail="客服会话本地演示未启用")


def _desk_error(exc: Exception) -> HTTPException:
    if isinstance(exc, customer_service_desk.DeskConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, customer_service_desk.DeskError):
        message = str(exc)
        if "不存在" in message:
            return HTTPException(status_code=404, detail=message)
        if "凭证" in message or "登记" in message:
            return HTTPException(status_code=403, detail=message)
        if "不可读" in message or "格式错误" in message:
            return HTTPException(status_code=503, detail=message)
        return HTTPException(status_code=400, detail=message)
    return HTTPException(status_code=503, detail="客服会话演示服务暂不可用")


@app.post("/api/v1/customer-service/demo/conversations", status_code=201)
def create_customer_demo_conversation(request: Request, payload: dict) -> dict:
    _require_customer_service_desk_demo()
    registration_id = str(payload.get("registration_id", "")).strip()
    token = request.headers.get("x-demo-access-token", "")
    try:
        conversation, replayed = customer_service_desk.create(
            registration_id, token, payload.get("text", ""),
            request.headers.get("idempotency-key", ""),
        )
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    _audit_event(
        request, {"username": "customer-demo", "role": "customer"},
        "customer_demo_conversation_replayed" if replayed else "customer_demo_conversation_created",
        conversation_id=conversation["conversation_id"],
    )
    return {"demo": True, "external_delivery": False, "idempotent_replay": replayed, "conversation": conversation}


@app.get("/api/v1/customer-service/demo/conversations")
def list_customer_demo_conversations(request: Request, registration_id: str = "") -> dict:
    _require_customer_service_desk_demo()
    try:
        rows = customer_service_desk.list_owned(registration_id, request.headers.get("x-demo-access-token", ""))
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    return {"demo": True, "external_delivery": False, "conversations": rows}


@app.get("/api/v1/customer-service/demo/conversations/{conversation_id}")
def get_customer_demo_conversation(conversation_id: str, request: Request, registration_id: str = "") -> dict:
    _require_customer_service_desk_demo()
    try:
        conversation = customer_service_desk.get_owned(
            conversation_id, registration_id, request.headers.get("x-demo-access-token", "")
        )
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    return {"demo": True, "external_delivery": False, "conversation": conversation}


@app.post("/api/v1/customer-service/demo/conversations/{conversation_id}/messages")
def add_customer_demo_conversation_message(conversation_id: str, request: Request, payload: dict, registration_id: str = "") -> dict:
    _require_customer_service_desk_demo()
    try:
        conversation, replayed = customer_service_desk.add_message(
            conversation_id, registration_id, request.headers.get("x-demo-access-token", ""),
            payload.get("text", ""), request.headers.get("idempotency-key", ""),
        )
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    _audit_event(
        request, {"username": "customer-demo", "role": "customer"},
        "customer_demo_conversation_message_replayed" if replayed else "customer_demo_conversation_message_added",
        conversation_id=conversation_id,
    )
    return {"demo": True, "external_delivery": False, "idempotent_replay": replayed, "conversation": conversation}


@app.get("/api/v1/customer-service/demo/admin/conversations")
def list_customer_demo_conversations_admin(request: Request, status: str = "") -> dict:
    _require_customer_service_desk_demo()
    user = _require_permission(request, "customer-service:conversation:manage")
    try:
        conversations = customer_service_desk.list_admin(status, reader=user.get("username", ""))
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    # Priority hints belong only to this admin endpoint. They are recomputed
    # from current rules and the conversation's registered company each read.
    snapshot = admin_config.describe()
    try:
        registrations = {row.get("registration_id"): row for row in
                         customer_registration.list_admin().get("registrations", [])}
    except customer_registration.RegistrationError:
        registrations = {}
    for conversation in conversations:
        registration = registrations.get(conversation.get("registration_id"))
        # Failing closed when the linked registration is missing or renamed
        # prevents a stale conversation label from receiving a VIP badge.
        linked_name = registration.get("company_name") if registration else ""
        display_name = conversation.get("company_name", "")
        if not linked_name or linked_name != display_name:
            review = {"status": "unavailable"}
        else:
            review = _registration_rule_review(display_name, snapshot)
        conversation["priority_preview"] = {
            "status": review.get("status", "unavailable"),
            "priority_candidate": bool(
                review.get("status") == "candidate"
                and (review.get("whitelist_match") or review.get("whitelist_keyword_match"))
            ),
            "whitelist_match": bool(review.get("whitelist_match", False)),
            "whitelist_keyword_match": bool(review.get("whitelist_keyword_match", False)),
            "requires_human_review": True,
            "config_revision": review.get("config_revision"),
        }
    return {"demo": True, "external_delivery": False, "conversations": conversations}


@app.post("/api/v1/customer-service/demo/admin/conversations/{conversation_id}/{action}")
def act_on_customer_demo_conversation(conversation_id: str, action: str, request: Request, payload: dict) -> dict:
    _require_customer_service_desk_demo()
    user = _require_permission(request, "customer-service:conversation:manage")
    actor = user.get("username", "")
    reason = str(payload.get("reason", ""))
    replayed = False
    operation_id = request.headers.get("idempotency-key", "").strip()
    if action in {"claim", "assign", "archive"} and not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 客服队列操作编号")
    try:
        if action == "read":
            through_message_count = payload.get("through_message_count")
            conversation = customer_service_desk.mark_read(
                conversation_id, actor, through_message_count
            )
        elif action == "claim":
            conversation, replayed = customer_service_desk.claim(
                conversation_id, actor, reason, operation_id, with_replay=True,
            )
        elif action == "reply":
            conversation, replayed = customer_service_desk.reply(
                conversation_id, actor, str(payload.get("text", "")),
                request.headers.get("idempotency-key", ""),
            )
        elif action == "assign":
            assignee = str(payload.get("assignee", "")).strip()
            def validate_assignee(_record):
                target = next((item for item in auth.load_users() if item.get("username") == assignee and item.get("active")), None)
                if not target or target.get("role") != "business_manager":
                    raise HTTPException(status_code=422, detail="客服队列仅能转交给启用中的业务经理账号")
            conversation, replayed = customer_service_desk.assign(
                conversation_id, actor, assignee, reason, operation_id,
                with_replay=True, validate_assignee=validate_assignee,
            )
        elif action == "archive":
            conversation, replayed = customer_service_desk.archive(
                conversation_id, actor, reason, operation_id, with_replay=True,
            )
        else:
            raise HTTPException(status_code=404, detail="客服会话操作不存在")
    except HTTPException:
        raise
    except customer_service_desk.DeskError as exc:
        raise _desk_error(exc) from exc
    if not replayed:
        _audit_event(
            request, user, f"customer_demo_conversation_{action}",
            conversation_id=conversation_id, assignee=conversation.get("assigned_to", ""),
            message_count=conversation.get("message_count"), unread_count=conversation.get("unread_count"),
            idempotent_replay=replayed,
        )
    return {"demo": True, "external_delivery": False, "idempotent_replay": replayed, "conversation": conversation}


def _require_s1_demo_preview(request: Request) -> None:
    _require_permission(request, "quote:parse")
    enabled = os.getenv("P0_CS_DEMO_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise HTTPException(status_code=404, detail="S1 合成报价预览未启用")


def _s1_demo_catalog() -> dict:
    catalog_path = ROOT / "demo-data" / "customer-service" / "catalog.json"
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="S1 合成目录不可用") from exc


def _s1_crm_catalog(snapshot: crm.CrmSnapshot, user: dict, project_token: str) -> dict:
    """Build a CRM preview catalog that cannot expose another project's rows."""
    catalog = customer_service_preview.crm_snapshot_catalog(snapshot)
    if auth.is_restricted(user):
        token = str(project_token or "").strip()
        projects = [
            project for project in snapshot.projects
            if token in {
                str(project.project_id or "").strip(),
                str(project.name or "").strip(),
            }
        ]
        if len(projects) != 1:
            raise customer_service_preview.CrmProjectScopeError(
                "项目标识无法在 CRM 项目清单中唯一匹配"
            )
        project = projects[0]
        catalog = customer_service_preview.scope_crm_catalog_to_project(
            catalog,
            project_id=project.project_id,
            project_name=project.name,
        )
    return catalog


@app.get("/api/v1/customer-service/demo/quote-preview")
def customer_service_demo_quote_preview(request: Request) -> dict:
    # F1: feature flag off -> do not expose fixture data.
    # F2: fixture missing or malformed -> return unavailable; never fabricate a fallback.
    # F3: fixture or any cited source is not explicitly synthetic/demo-only -> reject it.
    _require_s1_demo_preview(request)
    requests_path = ROOT / "demo-data" / "customer-service" / "quote-requests.example.json"
    try:
        requests = json.loads(requests_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="S1 合成询价样例不可用") from exc
    try:
        preview = customer_service_preview.resolve_preview(_s1_demo_catalog(), requests)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="S1 合成目录或询价样例无效") from exc
    return preview


@app.post("/api/v1/customer-service/demo/quote-preview/resolve")
def resolve_customer_service_demo_quote_preview(
    request: Request, payload: S1QuotePreviewRequest
) -> dict:
    # Uploaded source rows are parsed by the existing local quote parser first.
    # This endpoint receives only normalized fields and does not persist them.
    _require_s1_demo_preview(request)
    lines = []
    review_id = payload.review_id.strip()
    user = _require_permission(request, "quote:parse")
    project_name = payload.project_name.strip()
    if not project_name:
        raise HTTPException(status_code=422, detail="请提供解析复核时使用的项目")
    _assert_project_access(user, {"project": project_name})
    if not _verified_quote_parse_review(review_id, user, project_name, payload.lines):
        raise HTTPException(status_code=409, detail="未找到当前用户、项目和明细完全匹配的解析复核记录")
    for index, parsed in enumerate(payload.lines, start=1):
        row = parsed.model_dump()
        row["project_name"] = project_name
        row["line_id"] = f"PARSED-REVIEW-{review_id}-{row.get('source_row') or 'NA'}-{index}"
        lines.append(row)
    requests = {"dataset": {"kind": "parsed_quote", "demo_only": True}, "lines": lines}
    try:
        return customer_service_preview.resolve_preview(_s1_demo_catalog(), requests)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="S1 合成目录或解析字段无效") from exc


@app.post("/api/v1/customer-service/quote-preview/resolve")
def resolve_customer_service_crm_quote_preview(
    request: Request, payload: S1QuotePreviewRequest
) -> dict:
    user = _require_permission(request, "quote:parse")
    _require_permission(request, "crm:read")
    review_id = payload.review_id.strip()
    project_name = payload.project_name.strip()
    if not project_name:
        raise HTTPException(status_code=422, detail="CRM 报价预览必须提供检测项目")
    _assert_project_access(user, {"project": project_name})
    if not _verified_quote_parse_review(review_id, user, project_name, payload.lines):
        raise HTTPException(status_code=409, detail="未找到当前用户、项目和明细完全匹配的解析复核记录")
    parsed_lines = []
    for index, line in enumerate(payload.lines, start=1):
        row = line.model_dump()
        row["line_id"] = (f"PARSED-REVIEW-{review_id}-{row.get('source_row') or 'NA'}-{index}"
                           if review_id else f"PARSED-ROW-{row.get('source_row') or 'NA'}-{index}")
        parsed_lines.append(row)
    return _resolve_crm_quote_preview_for_user(
        user, project_name, parsed_lines
    )


@app.post("/api/v1/customer-service/quote-preview/export")
def export_customer_service_crm_quote_preview(
    request: Request, payload: S1QuotePreviewRequest
) -> Response:
    """Export an audited, project-scoped CRM preview as an internal-only workbook."""
    user = _require_permission(request, "quote:parse")
    _require_permission(request, "crm:read")
    review_id = payload.review_id.strip()
    project_name = payload.project_name.strip()
    if not project_name:
        raise HTTPException(status_code=422, detail="CRM 草稿必须提供检测项目")
    _assert_project_access(user, {"project": project_name})
    if not _verified_quote_parse_review(review_id, user, project_name, payload.lines):
        raise HTTPException(status_code=409, detail="未找到当前用户、项目和明细完全匹配的解析复核记录")
    parsed_lines = []
    for index, line in enumerate(payload.lines, start=1):
        row = line.model_dump()
        row["line_id"] = f"PARSED-REVIEW-{review_id}-{row.get('source_row') or 'NA'}-{index}"
        parsed_lines.append(row)
    preview = _resolve_crm_quote_preview_for_user(user, project_name, parsed_lines)
    try:
        workbook = customer_service_export.build_internal_review_workbook(project_name, review_id, preview)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="CRM 预览内容不满足内部草稿导出要求") from exc
    recorded = _audit_event(
        request, user, "customer_service_crm_review_workbook_exported",
        review_id=review_id, project=project_name,
        preview_id=preview.get("preview_id"), source_mode=preview.get("source_mode"),
        source_revision=preview.get("source_revision"), line_count=len(preview.get("lines") or []),
        effect="仅导出内部复核草稿；未生成正式报价、未写回 CRM",
    )
    if not recorded:
        raise HTTPException(status_code=503, detail="导出审计记录失败，未返回文件")
    filename = url_quote("CRM内部复核草稿.xlsx")
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
            "X-Quote-Preview-Id": str(preview.get("preview_id") or ""),
        },
    )


@app.post("/api/v1/customer-service/business-rules/preview")
def preview_customer_business_rule_candidates(
    request: Request, payload: CustomerBusinessRulePreviewRequest
) -> dict:
    # F1: permission/project denial happens before reading business configuration.
    # F2: invalid configuration never falls back to default candidates.
    # F3: exact-list conflicts and heuristic matches remain review candidates only.
    # F4: audit persistence failure prevents returning an apparently traceable result.
    user = _require_permission(request, "quote:parse")
    project_name = payload.project_name.strip()
    company_name = payload.company_name.strip()
    if not project_name or not company_name:
        raise HTTPException(status_code=422, detail="项目名称和企业名称不能为空")
    _assert_project_access(user, {"project": project_name})

    try:
        snapshot = admin_config.describe()
        if snapshot.get("warning"):
            raise ValueError("invalid configuration")
        config = snapshot["config"]
        result = customer_business_rules.evaluate_candidates(
            admin_config.business_config(config),
            company_name=company_name,
            subtotal=payload.subtotal,
            rush_requested=payload.rush_requested,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=503, detail="业务规则配置不可用，已停止预览") from exc

    company_fingerprint = hmac.new(
        auth.state_secret(), company_name.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not _audit_event(
        request,
        user,
        "customer_business_rules_previewed",
        project=project_name,
        company_fingerprint=company_fingerprint,
        subtotal=payload.subtotal,
        rush_requested=payload.rush_requested,
        config_revision=config.get("revision", 0),
        result=result,
    ):
        raise HTTPException(status_code=503, detail="规则预览审计暂不可用，请稍后重试")

    response = {
        **result,
        "config_revision": config.get("revision", 0),
        "config_updated_at": config.get("updated_at", ""),
        "scope": "read_only_candidate",
        "message": "仅供内部人工复核；未生成报价、放行或拦截客户",
    }
    return _redact_customer_risk_candidates(response, admin=user.get("role") == "admin")


def _resolve_crm_quote_preview_for_user(user: dict, project_name: str, parsed_lines: list[dict]) -> dict:
    # F1: CRM preview switch off -> no data access.
    # F2: CRM mode off/api/invalid -> fail closed; never fall back to synthetic data.
    # F3: configured provider unavailable -> return unavailable without source paths or row data.
    # F4: unstructured CRM qualification scope -> return an opaque unknown requiring human review.
    project_name = str(project_name or "").strip()
    if not project_name:
        raise HTTPException(status_code=422, detail="CRM 报价预览必须提供检测项目")
    _assert_project_access(user, {"project": project_name})
    enabled = os.getenv("P0_CS_CRM_PREVIEW_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise HTTPException(status_code=404, detail="CRM 只读报价预览未启用")
    mode = os.getenv("P0_CRM_MODE", "off").strip().lower() or "off"
    if mode not in {"excel", "csv", "base"}:
        raise HTTPException(status_code=503, detail="CRM 只读预览只支持已配置的 Excel/CSV/基础库模式")
    try:
        snapshot = crm.provider().snapshot()
    except crm.CrmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"当前 CRM 只读数据不可用（mode={mode}）") from exc
    except Exception as exc:  # noqa: BLE001 - keep provider failures inside this read-only boundary
        raise HTTPException(status_code=503, detail=f"当前 CRM 只读数据读取失败（mode={mode}）") from exc
    if snapshot.mode != mode:
        raise HTTPException(status_code=503, detail="CRM 只读适配器模式与配置不一致")

    lines = []
    for index, parsed in enumerate(parsed_lines, start=1):
        try:
            row = S1QuotePreviewLine.model_validate(parsed).model_dump()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="报价明细格式无效") from exc
        row["project_name"] = project_name
        row["line_id"] = str(parsed.get("line_id") or f"PARSED-ROW-{row.get('source_row') or 'NA'}-{index}")
        lines.append(row)
    requests = {"dataset": {"kind": "parsed_quote", "demo_only": True}, "lines": lines}
    try:
        catalog = _s1_crm_catalog(snapshot, user, project_name)
        return customer_service_preview.resolve_preview(catalog, requests)
    except customer_service_preview.CrmProjectScopeError as exc:
        raise HTTPException(status_code=409, detail="CRM 项目映射冲突，请核对项目编号或名称") from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="CRM 只读数据或解析字段格式无效") from exc


@app.post("/api/v1/customer-service/quote-preview/price-reviews")
def record_customer_service_price_candidate_review(
    request: Request, payload: S1PriceCandidateReviewRequest
) -> dict:
    # F1: disabled feature, unsupported mode, or unavailable source -> reject without a review entry.
    # F2: source revision changed since preview -> require a fresh preview before recording.
    # F3: selected record is not a current ambiguous candidate -> reject tampered/stale selection.
    # F4: audit persistence fails -> do not claim the human review was recorded.
    # F5: successful review records an internal opinion only; it never generates a quote or CRM write.
    # F6: price-candidate decisions must reference the exact line in this user's audited parse review.
    user = _require_permission(request, "quote:parse")
    _require_permission(request, "crm:read")
    project_name = payload.project_name.strip()
    if not project_name:
        raise HTTPException(status_code=422, detail="价格候选复核必须提供检测项目")
    _assert_project_access(user, {"project": project_name})
    parsed_line_id = re.fullmatch(
        r"PARSED-REVIEW-(CS-QUOTE-PARSE-REVIEW-[a-f0-9]{16})-(\d+)-(\d+)",
        payload.line_id,
    )
    if not parsed_line_id:
        raise HTTPException(status_code=409, detail="价格候选复核必须引用已复核的询价行")
    parse_review_id, source_row_text, preview_index_text = parsed_line_id.groups()
    parsed_line = S1QuotePreviewLine.model_validate(
        payload.model_dump(exclude={
            "project_name", "source_mode", "source_revision", "line_id", "selected_record_id",
        })
    )
    if (
        str(parsed_line.source_row) != source_row_text
        or not _verified_quote_parse_line(
            parse_review_id, user, project_name, int(preview_index_text) - 1, parsed_line
        )
    ):
        raise HTTPException(status_code=409, detail="询价行与当前用户的解析复核记录不一致")
    enabled = os.getenv("P0_CS_CRM_PREVIEW_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise HTTPException(status_code=404, detail="CRM 只读报价预览未启用")
    mode = os.getenv("P0_CRM_MODE", "off").strip().lower() or "off"
    if mode not in {"excel", "csv", "base"} or payload.source_mode != mode:
        raise HTTPException(status_code=409, detail="CRM 数据源模式已变化，请重新预览")
    try:
        snapshot = crm.provider().snapshot()
        catalog = _s1_crm_catalog(snapshot, user, project_name)
    except customer_service_preview.CrmProjectScopeError as exc:
        raise HTTPException(status_code=409, detail="CRM 项目映射冲突，请重新核对项目编号或名称") from exc
    except crm.CrmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"当前 CRM 只读数据不可用（mode={mode}）") from exc
    except Exception as exc:  # noqa: BLE001 - fail closed at the adapter boundary
        raise HTTPException(status_code=503, detail=f"当前 CRM 只读数据读取失败（mode={mode}）") from exc
    if snapshot.mode != mode:
        raise HTTPException(status_code=503, detail="CRM 只读适配器模式与配置不一致")
    if catalog["dataset"].get("revision") != payload.source_revision:
        raise HTTPException(status_code=409, detail="CRM 数据已变化，请重新预览后再记录复核意见")

    line = payload.model_dump(exclude={"project_name", "source_mode", "source_revision", "line_id", "selected_record_id"})
    line["project_name"] = project_name
    line["line_id"] = payload.line_id
    try:
        preview = customer_service_preview.resolve_preview(
            catalog,
            {"dataset": {"kind": "parsed_quote", "demo_only": True}, "lines": [line]},
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail="当前询价字段无法重新匹配，请重新预览") from exc
    result_line = preview["lines"][0]
    candidates = result_line.get("price_candidates") or []
    candidate_ids = {candidate.get("record_id") for candidate in candidates}
    if result_line.get("price_status") != "ambiguous" or payload.selected_record_id not in candidate_ids:
        raise HTTPException(status_code=409, detail="所选记录已不是当前有效候选，请重新预览")

    input_fingerprint = hashlib.sha256(
        json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    review_id = "CS-PRICE-REVIEW-" + secrets.token_hex(8)
    recorded = _audit_event(
        request,
        user,
        "customer_service_price_candidate_reviewed",
        review_id=review_id,
        source_mode=mode,
        source_revision=payload.source_revision,
        line_id=payload.line_id,
        input_fingerprint=input_fingerprint,
        selected_record_id=payload.selected_record_id,
        review_action="selected_for_internal_review",
    )
    if not recorded:
        raise HTTPException(status_code=503, detail="审核意见未能写入审计记录，请稍后重试")
    return {
        "review_id": review_id,
        "recorded": True,
        "source_mode": mode,
        "source_revision": payload.source_revision,
        "selected_record_id": payload.selected_record_id,
        "effect": "仅记录内部审核意见；不确认报价、不写回 CRM",
    }


def _require_quote_flow() -> None:
    enabled = os.getenv("P0_CS_QUOTE_FLOW_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not enabled or os.getenv("P0_ENV", "").strip().lower() == "production":
        raise HTTPException(status_code=404, detail="本地询价人工接手流程未启用")


def _quote_flow_error(exc: Exception) -> HTTPException:
    if isinstance(exc, customer_quote_flow.QuoteFlowConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, customer_quote_flow.QuoteFlowError):
        if "不存在" in str(exc):
            return HTTPException(status_code=404, detail=str(exc))
        if "存储" in str(exc) or "格式错误" in str(exc):
            return HTTPException(status_code=503, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail="询价工单暂不可用")


def _customer_quote_inquiry(inquiry_id: str, user: dict) -> dict:
    try:
        record = customer_quote_flow.get(inquiry_id)
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if user.get("role") != "admin" and record.get("created_by") != user.get("username") and record.get("assigned_to") != user.get("username"):
        raise HTTPException(status_code=404, detail="询价工单不存在")
    if not auth.can_access_project(user, record.get("project_name", ""), record.get("created_by", "")):
        raise HTTPException(status_code=404, detail="询价工单不存在")
    return record


@app.post("/api/v1/customer-service/quote-inquiries", status_code=201)
def create_customer_quote_inquiry(request: Request, payload: dict) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "quote:parse")
    project = str(payload.get("project_name") or "").strip()
    if not project:
        raise HTTPException(status_code=422, detail="检测项目不能为空")
    _assert_project_access(user, {"project": project})
    operation_id = request.headers.get("Idempotency-Key", "")
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 询价创建操作编号")
    try:
        record, replayed = customer_quote_flow.create(
            payload, user.get("username", ""), operation_id, with_replay=True,
        )
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if not replayed:
        _audit_event(request, user, "customer_quote_inquiry_created", inquiry_id=record["inquiry_id"], project=project)
    return {"demo": True, "inquiry": record, "idempotent_replay": replayed}


@app.get("/api/v1/customer-service/quote-inquiries")
def list_customer_quote_inquiries(request: Request, status: str = "") -> dict:
    _require_quote_flow()
    user = _require_permission(request, "quote:parse")
    allowed_statuses = {"", "draft", "submitted", "need_info", "preview", "needs_human", "resolved"}
    if status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="询价状态筛选值无效")
    try:
        rows = customer_quote_flow.list_records(status=status)
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if user.get("role") != "admin":
        rows = [row for row in rows if row.get("created_by") == user.get("username") or row.get("assigned_to") == user.get("username")]
    rows = [row for row in rows if auth.can_access_project(user, row.get("project_name", ""), row.get("created_by", ""))]
    return {"demo": True, "inquiries": rows}


@app.get("/api/v1/customer-service/quote-inquiries/admin/queue")
def list_customer_quote_inquiry_queue(request: Request, status: str = "") -> dict:
    _require_quote_flow()
    user = _require_permission(request, "customer-service:quote:manage")
    allowed_statuses = {"", "draft", "submitted", "need_info", "preview", "needs_human", "resolved"}
    if status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="询价状态筛选值无效")
    try:
        rows = customer_quote_flow.list_records(status=status)
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if user.get("role") != "admin":
        rows = [
            row for row in rows
            if auth.can_access_project(user, row.get("project_name", ""), row.get("created_by", ""))
        ]
    return {"demo": True, "inquiries": rows}


@app.get("/api/v1/customer-service/quote-inquiries/{inquiry_id}")
def get_customer_quote_inquiry(inquiry_id: str, request: Request) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "quote:parse")
    return {"demo": True, "inquiry": _customer_quote_inquiry(inquiry_id, user)}


@app.patch("/api/v1/customer-service/quote-inquiries/{inquiry_id}")
def update_customer_quote_inquiry(inquiry_id: str, request: Request, payload: dict) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "quote:parse")
    record = _customer_quote_inquiry(inquiry_id, user)
    _assert_project_access(user, {"project": record["project_name"]})
    try:
        updated = customer_quote_flow.update(inquiry_id, payload, user.get("username", ""))
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    _audit_event(request, user, "customer_quote_inquiry_updated", inquiry_id=inquiry_id)
    return {"demo": True, "inquiry": updated}


@app.post("/api/v1/customer-service/quote-inquiries/{inquiry_id}/submit")
def submit_customer_quote_inquiry(inquiry_id: str, request: Request) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "quote:parse")
    _require_permission(request, "crm:read")
    record = _customer_quote_inquiry(inquiry_id, user)
    _assert_project_access(user, {"project": record["project_name"]})
    failure_code = ""
    preview = None
    try:
        preview = _resolve_crm_quote_preview_for_user(user, record["project_name"], record["lines"])
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        failure_code = {
            404: "preview_disabled", 409: "project_conflict", 503: "source_unavailable",
            422: "preview_invalid",
        }.get(exc.status_code, "")
        if not failure_code:
            raise
    try:
        updated = customer_quote_flow.submit(
            inquiry_id, user.get("username", ""), preview=preview, failure_code=failure_code,
        )
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    _audit_event(request, user, "customer_quote_inquiry_submitted", inquiry_id=inquiry_id,
                 status=updated["status"], source_mode=(preview or {}).get("source_mode", ""))
    return {"demo": True, "inquiry": updated}


@app.post("/api/v1/customer-service/quote-inquiries/admin/{inquiry_id}/claim")
def claim_customer_quote_inquiry(inquiry_id: str, request: Request, payload: dict) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "customer-service:quote:manage")
    _customer_quote_inquiry(inquiry_id, user)
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 询价接单操作编号")
    try:
        updated, replayed = customer_quote_flow.claim(
            inquiry_id, user.get("username", ""), str(payload.get("reason", "")),
            operation_id, with_replay=True,
        )
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if not replayed:
        _audit_event(request, user, "customer_quote_inquiry_claimed", inquiry_id=inquiry_id)
    return {"demo": True, "inquiry": updated, "idempotent_replay": replayed}


@app.post("/api/v1/customer-service/quote-inquiries/admin/{inquiry_id}/assign")
def assign_customer_quote_inquiry(inquiry_id: str, request: Request, payload: dict) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "customer-service:quote:manage")
    record = _customer_quote_inquiry(inquiry_id, user)
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 询价转派操作编号")
    assignee = str(payload.get("assignee", "")).strip()
    reason = str(payload.get("reason", ""))
    def validate_assignee(current_record):
        target = next((item for item in auth.load_users() if item.get("username") == assignee and item.get("active")), None)
        if not target or target.get("role") != "business_manager":
            raise HTTPException(status_code=422, detail="转派对象必须是启用中的业务经理账号")
        if not auth.can_access_project(target, current_record.get("project_name", ""), current_record.get("created_by", "")):
            raise HTTPException(status_code=422, detail="转派对象没有该项目授权")
    try:
        updated = customer_quote_flow.assign(
            inquiry_id, user.get("username", ""), assignee, reason,
            operation_id, with_replay=True, validate_assignee=validate_assignee,
        )
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    inquiry, replayed = updated
    if not replayed:
        _audit_event(request, user, "customer_quote_inquiry_assigned", inquiry_id=inquiry_id, assignee=assignee)
    return {"demo": True, "inquiry": inquiry, "idempotent_replay": replayed}


@app.post("/api/v1/customer-service/quote-inquiries/admin/{inquiry_id}/resolve")
def resolve_customer_quote_inquiry(inquiry_id: str, request: Request, payload: dict) -> dict:
    _require_quote_flow()
    user = _require_permission(request, "customer-service:quote:manage")
    _customer_quote_inquiry(inquiry_id, user)
    operation_id = request.headers.get("idempotency-key", "").strip()
    if not operation_id:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 询价关闭操作编号")
    try:
        updated, replayed = customer_quote_flow.resolve(
            inquiry_id, user.get("username", ""), str(payload.get("reason", "")),
            operation_id, with_replay=True,
        )
    except customer_quote_flow.QuoteFlowError as exc:
        raise _quote_flow_error(exc) from exc
    if not replayed:
        _audit_event(request, user, "customer_quote_inquiry_resolved", inquiry_id=inquiry_id)
    return {"demo": True, "inquiry": updated, "idempotent_replay": replayed}


# ---------------------------------------------------------------------------
# P1-5 CRM read-only adapter (Excel/CSV bridge; API seam reserved)
# ---------------------------------------------------------------------------
def _crm_snapshot():
    try:
        return crm.provider().snapshot()
    except crm.CrmUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _crm_scoped_snapshot(snapshot: crm.CrmSnapshot, user: dict) -> crm.CrmSnapshot:
    if not auth.is_restricted(user):
        return snapshot
    return crm.scope_snapshot(snapshot, auth.normalize_projects(user.get("projects")))


def _crm_snapshot_for_user(user: dict) -> crm.CrmSnapshot:
    try:
        return _crm_scoped_snapshot(_crm_snapshot(), user)
    except HTTPException as exc:
        if auth.is_restricted(user):
            raise HTTPException(status_code=503, detail="CRM 只读数据不可用") from exc
        raise


@app.get("/api/v1/crm/status")
def crm_status(request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    if not auth.is_restricted(user):
        return crm.status()
    mode = os.getenv("P0_CRM_MODE", "off").strip().lower() or "off"
    try:
        snapshot = _crm_snapshot_for_user(user)
    except Exception:  # noqa: BLE001 - restricted status must not expose source paths/errors
        return {
            "configured": False,
            "mode": mode,
            "error": "CRM 只读数据不可用",
            "counts": crm.CrmSnapshot(mode=mode, source="").counts(),
        }
    return {
        "configured": True,
        "mode": snapshot.mode,
        "source": "CRM 只读适配器",
        "counts": snapshot.counts(),
        "warnings": snapshot.warnings,
    }


@app.get("/api/v1/crm/projects")
def crm_projects(request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    return {"projects": crm.read_projects(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/prices")
def crm_prices(request: Request, project: str = "", q: str = "", limit: int = 200) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    rows = crm.read_prices(snapshot, project=project, query=q)
    return {"prices": rows[: max(1, min(int(limit), 1000))], "total": len(rows), "counts": snapshot.counts()}


@app.get("/api/v1/crm/qualifications")
def crm_qualifications(request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    return {"qualifications": crm.read_qualifications(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/schedules")
def crm_schedules(request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    return {"schedules": crm.read_schedules(snapshot), "counts": snapshot.counts()}


@app.get("/api/v1/crm/task-orders/{order_no}")
def crm_task_order(order_no: str, request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    item = crm.lookup_task_order(snapshot, order_no)
    if item is None:
        raise HTTPException(status_code=404, detail="task order not found")
    return item


@app.get("/api/v1/crm/samples/{sample_no}")
def crm_sample(sample_no: str, request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    snapshot = _crm_snapshot_for_user(user)
    item = crm.lookup_sample(snapshot, sample_no)
    if item is None:
        raise HTTPException(status_code=404, detail="sample not found")
    return item


# CS-1.1..1.4 admin-maintained base libraries and CRM-to-local sync
def _crm_base_error(exc: Exception) -> HTTPException:
    if isinstance(exc, crm.BaseDataConflict):
        return HTTPException(409, detail=str(exc))
    return HTTPException(503 if isinstance(exc, crm.CrmUnavailable) else 400, detail=str(exc))


def _require_crm_base_write(request: Request, kind: str = "") -> dict:
    """Keep CRM price maintenance separate from administrator-owned base libraries."""
    user = _require_user(request)
    if kind == "prices" and auth.has_permission(user, "crm:prices:manage"):
        request.state.action = "crm:prices:manage"
        return user
    if kind == "schedules" and auth.has_permission(user, "schedule:manage"):
        request.state.action = "schedule:manage"
        return user
    if auth.has_permission(user, "crm:base:manage"):
        request.state.action = "crm:base:manage"
        return user
    required = "crm:prices:manage" if kind == "prices" else "crm:base:manage"
    raise HTTPException(status_code=403, detail=f"需要权限：{required}")


@app.get("/api/v1/crm/base-data/schedules")
def crm_base_schedules(request: Request) -> dict:
    user = _require_user(request)
    action = "schedule:read" if auth.has_permission(user, "schedule:read") else "crm:read"
    _require_permission(request, action)
    try:
        document = crm.BaseDataStore().read()
        return {
            "schedules": document["schedules"],
            "version": document.get("version", 0),
            "updated_at": document.get("updated_at", ""),
        }
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc


@app.get("/api/v1/crm/base-data")
def crm_base_data(request: Request) -> dict:
    user = _require_permission(request, "crm:read")
    try:
        document = crm.BaseDataStore().read()
        if auth.is_restricted(user):
            document = crm.scope_base_document(
                document, auth.normalize_projects(user.get("projects"))
            )
        return {**{kind: document[kind] for kind in crm.BASE_KINDS},
                "version": document.get("version", 0), "updated_at": document.get("updated_at", ""),
                "counts": {kind: len(document[kind]) for kind in crm.BASE_KINDS}}
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc


@app.post("/api/v1/crm/base-data/sync")
def crm_base_data_sync(request: Request) -> dict:
    user = _require_crm_base_write(request)
    try:
        result = crm.sync_base_data(_crm_snapshot(), updated_by=user.get("username", ""))
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc
    _audit_event(request, user, "crm_base_data_sync", **result)
    return result


@app.get("/api/v1/crm/base-data/history")
def crm_base_data_history(request: Request) -> dict:
    _require_crm_base_write(request)
    store = crm.BaseDataStore()
    return {"version": store.read().get("version", 0), "history": store.history()}


@app.post("/api/v1/crm/base-data/name-mappings/approve")
def crm_base_data_approve_mapping(request: Request, payload: dict) -> dict:
    user = _require_crm_base_write(request)
    try:
        item = crm.BaseDataStore().approve_name_mapping(
            str(payload.get("alias", "")), user.get("username", ""),
            expected=payload.get("expected"),
            source=payload.get("source", ""), reason=payload.get("reason", ""),
        )
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc
    _audit_event(request, user, "crm_base_mapping_approved", alias=item["alias"], source=item.get("source", ""))
    return {"approved": True, "item": item}


@app.post("/api/v1/crm/base-data/rollback")
def crm_base_data_rollback(request: Request, payload: dict) -> dict:
    user = _require_crm_base_write(request)
    try:
        version = crm.BaseDataStore().rollback(
            int(payload.get("version", -1)), user.get("username", ""),
            reason=str(payload.get("reason", "")),
        )
    except (TypeError, ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc
    _audit_event(request, user, "crm_base_data_rollback", target_version=payload.get("version"), version=version)
    return {"rolled_back": True, "version": version, "target_version": int(payload["version"])}


@app.post("/api/v1/crm/base-data/{kind}")
def crm_base_data_upsert(kind: str, request: Request, payload: dict) -> dict:
    user = _require_crm_base_write(request, kind)
    if kind == "prices" and auth.is_restricted(user):
        project = str(payload.get("project_id") or payload.get("project_name") or "").strip()
        if not auth.can_access_project(user, project):
            raise HTTPException(status_code=403, detail="无权维护该项目的价格数据")
    try:
        if kind == "prices":
            # JSON numbers are decoded to binary floats before this handler;
            # require decimal strings to retain the caller's exact input.
            for field in ("quantity", "unit_price", "discount"):
                if field in payload and not isinstance(payload[field], str):
                    raise ValueError(f"{field} 必须以十进制字符串提交，避免 JSON 浮点精度损失")
        result = crm.BaseDataStore().upsert(
            kind, payload, updated_by=user.get("username", ""), source=str(payload.get("source", "")),
            reason=str(payload.get("reason", "")),
        )
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc
    return result


@app.delete("/api/v1/crm/base-data/{kind}")
def crm_base_data_delete(kind: str, request: Request, payload: dict) -> dict:
    user = _require_crm_base_write(request, kind)
    if kind == "prices" and auth.is_restricted(user):
        project = str(payload.get("project_id") or payload.get("project_name") or "").strip()
        if not auth.can_access_project(user, project):
            raise HTTPException(status_code=403, detail="无权维护该项目的价格数据")
    try:
        deleted = crm.BaseDataStore().delete(kind, payload, updated_by=user.get("username", ""),
                                             source=str(payload.get("source", "")),
                                             reason=str(payload.get("reason", "")))
    except (ValueError, crm.CrmUnavailable) as exc:
        raise _crm_base_error(exc) from exc
    if not deleted:
        raise HTTPException(404, "基础数据条目不存在")
    return {"deleted": True, "kind": kind}


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
