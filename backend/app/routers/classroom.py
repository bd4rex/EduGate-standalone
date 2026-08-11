from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.core import (
    _client_ip,
    _computer_name,
    business_db,
    classroom_access,
    rate_limiter,
    require_admin,
    secret_store,
    settings,
    student_sessions,
)
from app.schemas import StudentJoinRequest, StudentJoinResponse

router = APIRouter()


@router.post("/classroom/join", response_model=StudentJoinResponse)
async def classroom_join(
    request: Request,
    join_request: StudentJoinRequest | None = None,
    x_class_token: str | None = Header(default=None, alias="X-Class-Token"),
    x_student_token: str | None = Header(default=None, alias="X-Student-Token"),
    class_token: str | None = None,
) -> StudentJoinResponse:
    if not classroom_access.active():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Classroom is not active")
    supplied_class_token = x_class_token or class_token
    current_classroom_token = classroom_access.validated_token(supplied_class_token)
    if current_classroom_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid classroom token")
    join_key = f"classroom-join:{_client_ip(request)}"
    if not rate_limiter.allow(
        join_key,
        limit=settings.student_join_rate_limit,
        window_seconds=300,
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom join limit exceeded")
    client_ip = _client_ip(request)
    device_id = (join_request.device_id if join_request else "").strip()
    token, record = student_sessions.issue(
        current_classroom_token,
        existing_token=x_student_token,
        computer_name=_computer_name(
            join_request.computer_name if join_request else "",
            client_ip,
            device_id,
        ),
        client_ip=client_ip,
        student_id=(
            classroom_access.legacy_student_id(f"device:{device_id}")
            if device_id
            else None
        ),
    )
    return StudentJoinResponse(
        student_token=token,
        student_session_id=record.student_id,
        computer_name=record.computer_name,
        client_ip=record.client_ip,
        expires_in=max(0, int(record.expires_at - time.time())),
    )


@router.get("/admin/classroom", dependencies=[Depends(require_admin)])
async def admin_classroom_access() -> dict[str, Any]:
    active = classroom_access.active()
    classroom_token = classroom_access.token()
    return {
        "active": active,
        "class_token": classroom_token,
        "classroom_id": classroom_access.classroom_id(),
        "student_count": (
            student_sessions.active_count(classroom_token=classroom_token)
            if active
            else 0
        ),
        "recording_enabled": settings.classroom_recording_enabled,
        "record_retention_days": settings.classroom_record_retention_days,
    }


@router.post("/admin/classroom/rotate", dependencies=[Depends(require_admin)])
async def rotate_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    token = classroom_access.rotate()
    secret_store.set("system:classroom_token", token)
    student_sessions.revoke_all()
    return {
        "active": classroom_access.active(),
        "class_token": token,
        "classroom_id": classroom_access.classroom_id(),
        "student_count": 0,
    }


@router.post("/admin/classroom/start", dependencies=[Depends(require_admin)])
async def start_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    token = classroom_access.start()
    student_sessions.revoke_all()
    return {
        "active": True,
        "class_token": token,
        "classroom_id": classroom_access.classroom_id(),
        "student_count": 0,
    }


@router.post("/admin/classroom/end", dependencies=[Depends(require_admin)])
async def end_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    classroom_access.end()
    student_sessions.revoke_all()
    return {
        "active": False,
        "class_token": classroom_access.token(),
        "classroom_id": classroom_access.classroom_id(),
        "student_count": 0,
    }


@router.get("/admin/classroom-records")
async def list_classroom_records(
    limit: int = 50,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "recording_enabled": settings.classroom_recording_enabled,
        "retention_days": settings.classroom_record_retention_days,
        "records": business_db.list_classroom_records(
            teacher_username=current_teacher["username"],
            limit=limit,
        ),
    }


@router.get("/admin/classroom-records/{run_id}")
async def get_classroom_record(
    run_id: str,
    limit: int = 1000,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    record = business_db.get_classroom_record(
        run_id,
        teacher_username=current_teacher["username"],
        limit=limit,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Classroom record not found")
    return record


@router.delete("/admin/classroom-records/{run_id}")
async def delete_classroom_record(
    run_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    deleted = business_db.delete_classroom_record(
        run_id,
        teacher_username=current_teacher["username"],
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Classroom record not found")
    return {"status": "deleted"}
