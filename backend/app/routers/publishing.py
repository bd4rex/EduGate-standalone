from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.core import classroom_access, published_pages, require_admin, settings


router = APIRouter()


@router.get("/admin/published-pages", dependencies=[Depends(require_admin)])
async def list_published_pages() -> dict[str, Any]:
    return await run_in_threadpool(published_pages.list_pages)


@router.post("/admin/published-pages", dependencies=[Depends(require_admin)])
async def publish_page(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    activate: bool = Form(default=True),
) -> dict[str, Any]:
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"网页文件超过 {settings.max_upload_bytes} 字节上限。",
        )
    operation = partial(
        published_pages.publish,
        filename=file.filename or "page.html",
        title=title,
        content=content,
        activate=activate,
    )
    return await run_in_threadpool(operation)


@router.post("/admin/published-pages/{page_id}/activate", dependencies=[Depends(require_admin)])
async def activate_published_page(page_id: str) -> dict[str, Any]:
    return await run_in_threadpool(published_pages.activate, page_id)


@router.post("/admin/published-pages/deactivate", dependencies=[Depends(require_admin)])
async def deactivate_published_page() -> dict[str, Any]:
    return await run_in_threadpool(published_pages.activate, None)


@router.delete("/admin/published-pages/{page_id}", dependencies=[Depends(require_admin)])
async def delete_published_page(page_id: str) -> dict[str, Any]:
    return await run_in_threadpool(published_pages.delete, page_id)


def _require_open_classroom(class_token: str | None) -> None:
    if not classroom_access.active():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Classroom is not active")
    if classroom_access.validated_token(class_token) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid classroom token")


@router.get("/published-pages/{page_id}/assets/{asset_path:path}", include_in_schema=True)
async def published_page_asset(page_id: str, asset_path: str) -> FileResponse:
    path, media_type = await run_in_threadpool(published_pages.asset_path, page_id, asset_path)
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/published-pages/{page_id}", include_in_schema=True)
async def published_page_document(
    page_id: str,
    x_class_token: str | None = Header(default=None, alias="X-Class-Token"),
) -> dict[str, str]:
    _require_open_classroom(x_class_token)
    return await run_in_threadpool(published_pages.get_active_document, page_id)
