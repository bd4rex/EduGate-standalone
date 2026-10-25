from __future__ import annotations

import secrets
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
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
from app.dependencies import effective_platform_key
from app.portable import DirectoryLock, contained_directory, stage_upgrade, write_json

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
        platform_key_set=bool(effective_platform_key()),
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
        "python_execution": {"location": "browser", "enabled": settings.python_runner_enabled, "server_execution": False},
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


@router.get("/admin/system/platform-key", dependencies=[Depends(require_admin)])
async def admin_read_platform_key(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"api_key": effective_platform_key() or ""}


@router.put("/admin/system/platform-key", dependencies=[Depends(require_admin)])
async def admin_update_platform_key(request: PlatformKeyRequest) -> dict[str, Any]:
    value = (request.api_key or "").strip()
    secret_store.set("system:platform_api_key", value)
    return {"status": "saved", "platform_api_key_set": bool(value)}


@router.post("/admin/system/platform-key/generate", dependencies=[Depends(require_admin)])
async def admin_generate_platform_key(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    value = f"eg_{secrets.token_urlsafe(16)}"
    secret_store.set("system:platform_api_key", value)
    return {
        "status": "generated",
        "platform_api_key_set": True,
        "api_key": value,
    }


@router.delete("/admin/system/platform-key", dependencies=[Depends(require_admin)])
async def admin_delete_platform_key() -> dict[str, Any]:
    secret_store.set("system:platform_api_key", "")
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
    try:
        accepted = system_control.request(request.action)
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not accepted:
        raise HTTPException(status_code=409, detail="服务未由启动器管理，或已有退出操作正在执行。")
    return {"status": "scheduled", "action": request.action}


@router.post("/admin/system/upgrade", dependencies=[Depends(require_admin)])
async def admin_program_upgrade(file: UploadFile = File(...)) -> dict[str, Any]:
    if os.name != "nt" or os.getenv("EDUGATE_FROZEN") != "true" or not system_control.supervised:
        raise HTTPException(status_code=409, detail="请在 Windows EXE 便携版中使用程序升级。")
    if system_control.pending_action:
        raise HTTPException(status_code=409, detail="已有退出或升级操作正在执行。")
    root = Path(settings.app_dir).resolve()
    data = contained_directory(root, Path(settings.data_dir))
    pending = data / "pending-upgrade.json"
    # Exclusive claim also serializes concurrent upload/validation requests.
    claim = DirectoryLock(data, ".upgrade-upload.lock")
    if not claim.acquire():
        raise HTTPException(status_code=409, detail="另一个升级包正在处理，请稍候。")
    stage = data / "updates" / uuid.uuid4().hex
    archive = stage.with_suffix(".zip")
    scheduled = False
    created_pending = False
    try:
        if pending.exists():
            raise HTTPException(status_code=409, detail="存在尚未完成的升级，请先查看 data/last-upgrade.json。")
        stage.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with archive.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024**3:
                    raise ValueError("升级压缩包不能超过 2 GB")
                output.write(chunk)
        if shutil.disk_usage(data).free < size * 3:
            raise ValueError("U 盘剩余空间不足，请为解压和旧版程序保留更多空间。")
        manifest = await run_in_threadpool(stage_upgrade, archive, stage)
        write_json(pending, {"staging": stage.relative_to(data).as_posix(), "version": manifest["version"]})
        created_pending = True
        if not system_control.request("upgrade"):
            raise RuntimeError("未能安排升级，当前程序保持运行。")
        scheduled = True
        return {"status": "scheduled", "version": manifest["version"], "message": "正在保存课堂，退出后替换程序；config 和 data 保持不变。"}
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        claim.release()
        archive.unlink(missing_ok=True)
        if not scheduled:
            if created_pending:
                pending.unlink(missing_ok=True)
            if stage.exists():
                contained_directory(data, stage)
                await run_in_threadpool(shutil.rmtree, stage)
