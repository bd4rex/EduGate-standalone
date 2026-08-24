from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.core import (
    ModelConcurrencyLimiter,
    business_db,
    create_backup,
    knowledge_store,
    langfuse,
    launcher_log_tail,
    model_semaphore,
    open_app_directory,
    python_pool,
    read_advanced_settings,
    remove_backup_file,
    require_admin,
    runtime_config,
    save_restore_archive,
    secret_store,
    settings,
    system_control,
    system_status,
    update_advanced_settings,
)
from app.schemas import AdvancedSettingsRequest, PlatformKeyRequest, SystemActionRequest

router = APIRouter()


@router.get("/admin/dashboard", dependencies=[Depends(require_admin)])
async def admin_dashboard() -> dict[str, Any]:
    return {
        **business_db.dashboard(),
        "scenario_count": len(runtime_config.data.scenarios),
        "model_catalog_count": len(runtime_config.data.model_catalog),
        "knowledge_source_count": len(knowledge_store.list_sources()),
        "knowledge_file_count": len(knowledge_store.list_files()),
        "langfuse_enabled": langfuse.enabled,
    }


@router.get("/admin/logs", dependencies=[Depends(require_admin)])
async def admin_logs(limit: int = 50) -> list[dict[str, Any]]:
    return business_db.list_logs(limit=min(max(limit, 1), 200))


@router.get("/admin/system/status", dependencies=[Depends(require_admin)])
async def admin_system_status() -> dict[str, Any]:
    return {
        **system_status(
        supervised=system_control.supervised,
        started_at=system_control.started_at,
        platform_key_set=secret_store.has("system:platform_api_key") or bool(settings.platform_api_key),
        ),
        "model_pool": (
            model_semaphore.stats()
            if isinstance(model_semaphore, ModelConcurrencyLimiter)
            else {
                "running": 0,
                "waiting": 0,
                "capacity": settings.model_max_concurrency,
            }
        ),
        "python_runner_pool": python_pool.stats(),
        "database_writer": business_db.writer_stats(),
    }


@router.get("/admin/system/launcher-logs", dependencies=[Depends(require_admin)])
async def admin_launcher_logs(limit: int = 200) -> dict[str, Any]:
    return {"lines": launcher_log_tail(limit)}


@router.get("/admin/system/settings", dependencies=[Depends(require_admin)])
async def admin_system_settings() -> dict[str, Any]:
    return read_advanced_settings()


@router.post("/admin/system/open-app-dir", dependencies=[Depends(require_admin)])
async def admin_open_app_directory() -> dict[str, str]:
    return await run_in_threadpool(open_app_directory)


@router.put("/admin/system/settings", dependencies=[Depends(require_admin)])
async def admin_update_system_settings(request: AdvancedSettingsRequest) -> dict[str, Any]:
    return update_advanced_settings(request.values)


@router.put("/admin/system/platform-key", dependencies=[Depends(require_admin)])
async def admin_update_platform_key(request: PlatformKeyRequest) -> dict[str, Any]:
    value = (request.api_key or "").strip()
    if value:
        secret_store.set("system:platform_api_key", value)
    else:
        secret_store.delete("system:platform_api_key")
    return {"status": "saved", "platform_api_key_set": bool(value)}


@router.post("/admin/system/platform-key/generate", dependencies=[Depends(require_admin)])
async def admin_generate_platform_key() -> dict[str, Any]:
    value = f"eg_{secrets.token_urlsafe(32)}"
    secret_store.set("system:platform_api_key", value)
    return {
        "status": "generated",
        "platform_api_key_set": True,
        "api_key": value,
    }


@router.delete("/admin/system/platform-key", dependencies=[Depends(require_admin)])
async def admin_delete_platform_key() -> dict[str, Any]:
    secret_store.delete("system:platform_api_key")
    return {"status": "disabled", "platform_api_key_set": False}


@router.get("/admin/system/backup", dependencies=[Depends(require_admin)])
async def admin_download_backup() -> FileResponse:
    path = await run_in_threadpool(create_backup)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        background=BackgroundTask(remove_backup_file, path),
    )


@router.post("/admin/system/restore", dependencies=[Depends(require_admin)])
async def admin_restore_backup(file: UploadFile = File(...)) -> dict[str, Any]:
    if not system_control.supervised:
        raise HTTPException(status_code=409, detail="Restore requires the EduGate supervised launcher")
    await save_restore_archive(file)
    if not system_control.request("restart"):
        raise HTTPException(status_code=409, detail="Could not schedule EduGate restart")
    return {"status": "restore_scheduled", "message": "EduGate will restart and restore the backup"}


@router.post("/admin/system/action", dependencies=[Depends(require_admin)])
async def admin_system_action(request: SystemActionRequest) -> dict[str, Any]:
    if not system_control.request(request.action):
        raise HTTPException(status_code=409, detail="System control requires the EduGate supervised launcher")
    return {"status": "scheduled", "action": request.action}
