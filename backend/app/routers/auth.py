from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core import (
    _client_ip,
    _is_loopback,
    _sync_portable_admin_password,
    business_db,
    rate_limiter,
    require_admin,
    require_admin_origin,
    sessions,
    settings,
)
from app.schemas import ChangePasswordRequest, LoginRequest, SetupRequest

router = APIRouter()


@router.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    return {
        "initialized": business_db.is_admin_initialized(settings.admin_username),
        "admin_username": settings.admin_username,
        "portable_mode": settings.portable_mode,
        "local_auto_login": settings.portable_auto_login,
        "lan_admin_enabled": settings.allow_lan_admin,
    }


@router.post("/auth/local-session")
async def local_teacher_session(request: Request) -> dict[str, Any]:
    if not settings.portable_mode or not settings.portable_auto_login:
        raise HTTPException(status_code=404, detail="Local automatic login is disabled")
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="Local automatic login is only available on this computer")
    teacher = business_db.get_teacher(settings.admin_username)
    if not teacher or not teacher.get("is_active") or teacher.get("role") != "admin":
        raise HTTPException(status_code=409, detail="Portable administrator is not available")
    token = sessions.issue(teacher["username"])
    return {
        "access_token": token,
        "token_type": "x-admin-token",
        "expires_in": settings.session_ttl_seconds,
        "teacher": teacher,
    }


@router.post("/auth/setup")
async def setup_admin(request: Request, payload: SetupRequest) -> dict[str, Any]:
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="Administrator setup is only allowed from this computer")
    if business_db.is_admin_initialized(settings.admin_username):
        raise HTTPException(status_code=409, detail="Administrator has already been initialized")
    if payload.username != settings.admin_username:
        raise HTTPException(
            status_code=400,
            detail=f"The administrator username must be {settings.admin_username!r}",
        )
    try:
        teacher = business_db.setup_admin(
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    token = sessions.issue(teacher["username"])
    return {
        "access_token": token,
        "token_type": "x-admin-token",
        "expires_in": settings.session_ttl_seconds,
        "teacher": teacher,
    }


@router.post("/auth/login")
async def login(http_request: Request, request: LoginRequest) -> dict[str, Any]:
    require_admin_origin(http_request)
    if not business_db.is_admin_initialized(settings.admin_username):
        raise HTTPException(status_code=409, detail="Administrator setup is required")
    rate_key = f"login:{_client_ip(http_request)}:{request.username.lower()}"
    if not rate_limiter.allow(rate_key, limit=settings.login_rate_limit, window_seconds=300):
        raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
    teacher = business_db.authenticate_teacher(request.username, request.password)
    if teacher is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access_token = sessions.issue(teacher["username"])
    return {
        "access_token": access_token,
        "token_type": "x-admin-token",
        "expires_in": settings.session_ttl_seconds,
        "teacher": teacher,
    }


@router.post("/auth/logout")
async def logout(
    current_teacher: dict[str, Any] = Depends(require_admin),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, str]:
    sessions.revoke(x_admin_token or "")
    return {"status": "logged_out", "username": current_teacher["username"]}


@router.post("/auth/password")
async def change_own_password(
    request: ChangePasswordRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    verified = business_db.authenticate_teacher(current_teacher["username"], request.current_password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    teacher = business_db.change_teacher_password(current_teacher["username"], request.new_password)
    _sync_portable_admin_password(current_teacher["username"], request.new_password)
    sessions.revoke_user(current_teacher["username"])
    token = sessions.issue(current_teacher["username"])
    return {
        "status": "password_changed",
        "access_token": token,
        "token_type": "x-admin-token",
        "expires_in": settings.session_ttl_seconds,
        "teacher": teacher,
    }
