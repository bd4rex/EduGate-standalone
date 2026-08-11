from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import core
from app.api_docs import API_DOCS, OPENAPI_TAGS, tag_for_path
from app.routers.auth import router as auth_router
from app.routers.chat import router as chat_router
from app.routers.classroom import router as classroom_router
from app.routers.config import router as config_router
from app.routers.knowledge import router as knowledge_router
from app.routers.models import router as models_router
from app.routers.python import router as python_router
from app.routers.system import router as system_router
from app.runtime_config import RuntimeConfig
from app.schemas import (
    ModelCatalogItem,
    RuntimeConfigData,
    ScenarioUpdateRequest,
    TeachingScenario,
)

# Stable imports for integrations and tests that historically imported these from app.main.
ModelConcurrencyLimiter = core.ModelConcurrencyLimiter
_sync_portable_admin_password = core._sync_portable_admin_password
_stream_with_heartbeat = core._stream_with_heartbeat
_chat_completion = core._chat_completion
_is_loopback = core._is_loopback
business_db = core.business_db
classroom_access = core.classroom_access
client = core.client
knowledge_store = core.knowledge_store
model_semaphore = core.model_semaphore
rate_limiter = core.rate_limiter
runtime_config = core.runtime_config
secret_store = core.secret_store
sessions = core.sessions
settings = core.settings
student_sessions = core.student_sessions

ADMIN_SURFACE_PATHS = {"/", "/admin.html", "/docs", "/redoc", "/openapi.json"}


app = FastAPI(
    title="EduGate API",
    summary="EduGate 单机课堂 API",
    description="教师在本机管理课堂，学生凭课堂链接加入，所有管理接口均要求教师会话。",
    version="2.0.0",
    lifespan=core.lifespan,
    openapi_tags=OPENAPI_TAGS,
)


@app.middleware("http")
async def restrict_teacher_surface(request: Request, call_next: Any) -> Response:
    if (
        request.url.path in ADMIN_SURFACE_PATHS
        and not settings.allow_lan_admin
        and not _is_loopback(request)
    ):
        return PlainTextResponse("教师管理端仅允许在运行 EduGate 的电脑上访问。", status_code=403)
    return await call_next(request)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(config_router)
app.include_router(system_router)
app.include_router(classroom_router)
app.include_router(models_router)
app.include_router(knowledge_router)
app.include_router(python_router)


@app.get("/health", include_in_schema=True)
async def health(response: Response) -> dict[str, str]:
    response.headers["X-EduGate-App"] = "EduGate"
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root_page() -> RedirectResponse:
    return RedirectResponse(url="/admin.html")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=OPENAPI_TAGS,
    )
    for path, methods in schema.get("paths", {}).items():
        for method, operation in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            operation["tags"] = [tag_for_path(path)]
            doc = API_DOCS.get((method.upper(), path))
            if doc:
                operation["summary"], operation["description"] = doc
            operation.setdefault("description", "EduGate API.")
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

frontend_path = Path(settings.frontend_dir)
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
