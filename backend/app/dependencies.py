from __future__ import annotations

import ipaddress
import secrets
from pathlib import Path
from typing import Any

from dotenv import set_key
from fastapi import Header, HTTPException, Request, status

from app import state
from app.security import StudentIdentity


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _computer_name(value: str | None, client_ip: str, device_id: str = "") -> str:
    cleaned = " ".join((value or "").split())[:80]
    if cleaned:
        return cleaned
    device_suffix = "".join(character for character in device_id if character.isalnum())[-6:].upper()
    return f"电脑-{device_suffix}" if device_suffix else f"电脑-{client_ip}"


def _is_loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address(_client_ip(request)).is_loopback
    except ValueError:
        return False


def _admin_origin_allowed(request: Request) -> bool:
    if _is_loopback(request):
        return True
    if not state.settings.allow_lan_admin:
        return False
    try:
        client_ip = ipaddress.ip_address(_client_ip(request))
        allowed_ips = {
            ipaddress.ip_address(value)
            for value in state.settings.admin_allowed_ips
        }
    except ValueError:
        return False
    return client_ip in allowed_ips


def require_admin_origin(request: Request) -> None:
    if not _admin_origin_allowed(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Teacher administration is limited to this computer or an allowed device IP",
        )


def _sync_portable_admin_password(username: str, password: str) -> None:
    if not state.settings.portable_mode or username != state.settings.admin_username:
        return
    config_path = Path(state.settings.config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)
    set_key(str(config_path), "ADMIN_PASSWORD", password, quote_mode="always")
    state.settings.admin_password = password


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    require_admin_origin(request)
    record = state.sessions.resolve(x_admin_token or "")
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired admin token")
    teacher = state.business_db.get_teacher(record.username)
    if not teacher or not teacher.get("is_active"):
        state.sessions.revoke(x_admin_token or "")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Teacher is inactive")
    return teacher


def require_platform_key(authorization: str | None = Header(default=None)) -> None:
    platform_api_key = state.secret_store.get("system:platform_api_key") or state.settings.platform_api_key
    if not platform_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OpenAI-compatible platform endpoint is disabled until PLATFORM_API_KEY is configured",
        )
    expected = f"Bearer {platform_api_key}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid platform API key")


def require_classroom_access(
    request: Request,
    x_class_token: str | None = Header(default=None, alias="X-Class-Token"),
    x_student_token: str | None = Header(default=None, alias="X-Student-Token"),
    x_computer_name: str | None = Header(default=None, alias="X-Computer-Name"),
    class_token: str | None = None,
) -> StudentIdentity:
    if not state.classroom_access.active():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Classroom is not active")
    if x_student_token:
        record = state.student_sessions.resolve(
            x_student_token,
            classroom_token=state.classroom_access.token(),
        )
        if record is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired student token")
        key = f"chat:student:{record.student_id}"
        if not state.rate_limiter.allow(key, limit=state.settings.classroom_rate_limit, window_seconds=60):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom request limit exceeded")
        return state.student_sessions.identity(record)
    token = x_class_token or class_token
    if not state.classroom_access.matches(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid classroom token")
    client_ip = _client_ip(request)
    key = f"chat:ip:{client_ip}"
    if not state.rate_limiter.allow(key, limit=state.settings.classroom_rate_limit, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom request limit exceeded")
    return StudentIdentity(
        student_id=state.classroom_access.legacy_student_id(client_ip),
        computer_name=_computer_name(x_computer_name, client_ip),
        client_ip=client_ip,
    )
