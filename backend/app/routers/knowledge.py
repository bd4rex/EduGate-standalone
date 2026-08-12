from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.core import (
    knowledge_store,
    open_local_directory,
    require_admin,
    runtime_config,
)
from app.knowledge import KnowledgeFile, KnowledgeScanResult, KnowledgeSource
from app.schemas import KnowledgeSourceRequest

router = APIRouter()


@router.get("/knowledge/sources", response_model=list[KnowledgeSource])
async def list_knowledge_sources(current_teacher: dict[str, Any] = Depends(require_admin)) -> list[KnowledgeSource]:
    return knowledge_store.list_sources()


@router.post("/knowledge/sources", response_model=KnowledgeSource)
async def upsert_knowledge_source(
    request: KnowledgeSourceRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeSource:
    return knowledge_store.upsert_source(request.id, request.name, request.description)


@router.delete("/knowledge/sources/{source_id}")
async def delete_knowledge_source(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    in_use = [
        scenario_id
        for scenario_id, scenario in runtime_config.data.scenarios.items()
        if scenario.knowledge_source_id == source_id
    ]
    if in_use:
        raise HTTPException(status_code=409, detail=f"Knowledge source is used by: {', '.join(in_use)}")
    knowledge_store.delete_source(source_id)
    return {"status": "deleted"}


@router.post("/knowledge/sources/{source_id}/open-folder")
async def open_knowledge_source_folder(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    directory = knowledge_store.source_directory(source_id)
    return await run_in_threadpool(
        open_local_directory,
        directory,
        missing_detail="Knowledge source directory does not exist",
    )


@router.post("/knowledge/sources/{source_id}/scan", response_model=KnowledgeScanResult)
async def scan_knowledge_source_folder(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeScanResult:
    return await run_in_threadpool(knowledge_store.scan_source, source_id)


@router.get("/knowledge/files", response_model=list[KnowledgeFile])
async def list_knowledge_files(
    source_id: str | None = None,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> list[KnowledgeFile]:
    if source_id:
        return knowledge_store.list_files(source_id)
    return knowledge_store.list_files()


@router.post("/knowledge/files", response_model=KnowledgeFile)
async def upload_knowledge_file(
    source_id: str = Form(default="general"),
    file: UploadFile = File(...),
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeFile:
    return await knowledge_store.add_file(source_id, file)


@router.delete("/knowledge/files/{file_id}")
async def delete_knowledge_file(
    file_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    knowledge_file = knowledge_store.get_file(file_id)
    knowledge_store.delete_file(file_id)
    return {"status": "deleted"}
