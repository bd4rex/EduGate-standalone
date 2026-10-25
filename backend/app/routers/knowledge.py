from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from fastapi.concurrency import run_in_threadpool

from app.core import (
    knowledge_store,
    open_local_directory,
    require_admin,
    runtime_config,
)
from app.knowledge import KnowledgeFile, KnowledgeScanResult, KnowledgeSource, KnowledgeMetadata, KnowledgeScope
from app.schemas import KnowledgeSourceRequest
from app.dependencies import require_classroom_access
from app.security import StudentIdentity
from app.knowledge_policy import KnowledgePolicy, POLICY_UNAVAILABLE_MESSAGE

router = APIRouter()


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=4000)
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    mode: str = "hybrid"
    limit: int = Field(default=5, ge=1, le=20)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    source_id: str | None = None


@router.get("/knowledge/status")
async def knowledge_status(current_teacher: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return await run_in_threadpool(knowledge_store.status)


@router.get("/knowledge/policy", response_model=KnowledgePolicy)
async def get_knowledge_policy(current_teacher: dict[str, Any] = Depends(require_admin)):
    return runtime_config.data.knowledge_policy


@router.put("/knowledge/policy", response_model=KnowledgePolicy)
async def set_knowledge_policy(request: KnowledgePolicy, current_teacher: dict[str, Any] = Depends(require_admin)):
    from app.chat_service import _POLICY_CACHE, _POLICY_CACHE_LOCK
    policy = runtime_config.update_knowledge_policy(request)
    with _POLICY_CACHE_LOCK:
        _POLICY_CACHE.clear()
    return policy


@router.post("/knowledge/search")
async def search_knowledge(request: SearchRequest, current_teacher: dict[str, Any] = Depends(require_admin)):
    from app.chat_service import _check_blacklist
    from app.schemas import ChatRequest
    knowledge_store.validate_scope(request.source_id, request.scope)
    probe = ChatRequest(messages=[{"role":"user","content":request.query}])
    if await _check_blacklist(probe, runtime_config.get_scenario("default")):
        message = probe._knowledge_policy.message if probe._policy_decision == "blocked" else POLICY_UNAVAILABLE_MESSAGE
        return {"query":request.query,"hits":[],"mode":"blocked","blocked":True,"block_reason":message,
            "policy_decision":probe._policy_decision,"blocked_topic":probe._policy_topic,"elapsed_ms":round(probe._policy_check_ms,2)}
    result = await run_in_threadpool(knowledge_store.search_details, request.source_id, request.query,
        limit=request.limit, scope=request.scope, mode=request.mode)
    if probe._knowledge_policy:
        from app.knowledge_policy import literal_topic
        result.hits = [hit for hit in result.hits if not literal_topic(f"{hit.filename}\n{hit.chapter}\n{hit.context_text or hit.text}", probe._knowledge_policy)]
    return result


@router.post("/knowledge/jobs", status_code=202)
async def knowledge_job(request: JobRequest, current_teacher: dict[str, Any] = Depends(require_admin)):
    return knowledge_store.start_job(request.action, request.source_id)


@router.patch("/knowledge/files/{file_id}/metadata", response_model=KnowledgeFile)
async def update_file_metadata(file_id: str, request: KnowledgeMetadata, current_teacher: dict[str, Any] = Depends(require_admin)):
    return await run_in_threadpool(knowledge_store.update_file_metadata, file_id, request)


@router.get("/knowledge/files/{file_id}/preview")
async def preview_file(file_id: str, offset: int = Query(default=0, ge=0), current_teacher: dict[str, Any] = Depends(require_admin)):
    return await run_in_threadpool(knowledge_store.preview_chunks, file_id, offset=offset)


@router.get("/knowledge/files/{file_id}/original")
async def original_file(file_id: str, current_teacher: dict[str, Any] = Depends(require_admin)):
    file = knowledge_store.get_file(file_id)
    with knowledge_store._connect() as conn:
        row = conn.execute("SELECT path FROM files WHERE id=?", (file_id,)).fetchone()
    path = knowledge_store._file_path(row["path"], file.source_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="原文件不存在，请扫描同步")
    return FileResponse(path, filename=file.filename, headers={"X-Content-Type-Options": "nosniff"})


@router.get("/knowledge/citations/{chunk_id}")
async def citation(chunk_id: str, version: str = "", student: StudentIdentity = Depends(require_classroom_access)):
    from app.chat_service import _classify_blacklist
    scenario = runtime_config.get_scenario("default")
    if not scenario.knowledge_source_id:
        raise HTTPException(status_code=404, detail="当前课堂未挂载资料")
    hit = await run_in_threadpool(knowledge_store.citation, chunk_id, scenario.knowledge_source_id,
        scenario.knowledge_scope, content_hash=version)
    policy = runtime_config.data.knowledge_policy.model_copy(deep=True)
    if policy.active:
        decision, _ = await _classify_blacklist(f"{hit.filename}\n{hit.chapter}\n{hit.context_text or hit.text}", policy, scenario.model, kind="references")
        if decision != "allowed":
            raise HTTPException(status_code=403 if decision == "blocked" else 503,
                detail=policy.message if decision == "blocked" else POLICY_UNAVAILABLE_MESSAGE)
    if student.lease and student.lease.revoked:
        raise HTTPException(status_code=403, detail="课堂已经结束")
    return hit


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
