from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal

import httpx
from dotenv import set_key
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import BusinessDB, latest_user_preview, now_ms
from app.knowledge import KnowledgeFile, KnowledgeScanResult, KnowledgeSource, KnowledgeStore
from app.litellm_client import LiteLLMClient
from app.observability import LangfuseClient
from app.python_runner import (
    PythonExecutionPool,
    PythonJob,
    PythonQueueFull,
    PythonQueueTimeout,
    PythonRunnerUnavailable,
    PythonStudentBusy,
    run_python_code,
)
from app.secret_store import SecretStore
from app.security import ClassroomAccess, SessionStore, SlidingWindowRateLimiter, StudentIdentity, StudentSessionStore
from app.system_control import system_control
from app.system_ops import (
    create_backup,
    launcher_log_tail,
    open_app_directory,
    open_local_directory,
    read_advanced_settings,
    remove_backup_file,
    save_restore_archive,
    system_status,
    update_advanced_settings,
)
from starlette.background import BackgroundTask


logger = logging.getLogger(__name__)
client = LiteLLMClient()
knowledge_store = KnowledgeStore(
    settings.knowledge_db_path,
    settings.knowledge_dir,
    max_upload_bytes=settings.max_upload_bytes,
    max_pdf_pages=settings.max_pdf_pages,
)
business_db = BusinessDB(
    settings.sqlite_db_path,
    log_max_records=settings.log_max_records,
    classroom_record_retention_days=settings.classroom_record_retention_days,
    classroom_record_max_records=settings.classroom_record_max_records,
    classroom_record_max_content_chars=settings.classroom_record_max_content_chars,
    write_queue_size=settings.db_write_queue_size,
    write_batch_size=settings.db_write_batch_size,
    write_flush_interval_ms=settings.db_write_flush_interval_ms,
    cleanup_interval_seconds=settings.db_cleanup_interval_seconds,
)
secret_store = SecretStore(settings.secret_store_path, mode=settings.secret_store_mode)
langfuse = LangfuseClient()
sessions = SessionStore(settings.session_ttl_seconds)
classroom_access = ClassroomAccess()
student_sessions = StudentSessionStore(settings.student_session_ttl_seconds)
rate_limiter = SlidingWindowRateLimiter()


class ModelConcurrencyLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._semaphore = asyncio.Semaphore(self.capacity)
        self._running = 0
        self._waiting = 0

    async def __aenter__(self) -> "ModelConcurrencyLimiter":
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        self._running += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._running -= 1
        self._semaphore.release()

    def stats(self) -> dict[str, int]:
        return {
            "running": self._running,
            "waiting": self._waiting,
            "capacity": self.capacity,
        }


model_semaphore = ModelConcurrencyLimiter(settings.model_max_concurrency)
python_pool = PythonExecutionPool(
    max_workers=settings.python_runner_max_concurrency,
    max_queue_size=settings.python_runner_max_queue,
    queue_timeout_seconds=settings.python_runner_queue_timeout_seconds,
)
python_record_tasks: set[asyncio.Task[None]] = set()

STRICT_KNOWLEDGE_MISS_MESSAGE = (
    "\u6839\u636e\u6559\u5e08\u5f53\u524d\u6302\u8f7d\u7684\u77e5\u8bc6\u5e93\uff0c\u6211\u6ca1\u6709\u627e\u5230\u4e0e\u8fd9\u4e2a\u95ee\u9898\u76f8\u5173\u7684\u8bfe\u5802\u8d44\u6599\u4f9d\u636e\u3002"
    "\u4e25\u683c\u77e5\u8bc6\u5e93\u6a21\u5f0f\u4e0b\uff0c\u6211\u4e0d\u80fd\u4f7f\u7528\u77e5\u8bc6\u5e93\u4ee5\u5916\u7684\u5185\u5bb9\u7ee7\u7eed\u56de\u7b54\u3002"
    "\u8bf7\u56de\u5230\u672c\u8282\u8bfe\u7684\u77e5\u8bc6\u70b9\u63d0\u95ee\uff0c\u6216\u8bf7\u8001\u5e08\u8865\u5145\u76f8\u5173\u8d44\u6599\u5230\u77e5\u8bc6\u5e93\u3002"
)

KNOWLEDGE_OVERVIEW_KEYWORDS = (
    "\u77e5\u8bc6\u5e93",
    "\u8d44\u6599\u5e93",
    "\u6709\u54ea\u4e9b\u5185\u5bb9",
    "\u6709\u4ec0\u4e48\u5185\u5bb9",
    "\u90fd\u6709\u4ec0\u4e48",
    "\u90fd\u6709\u54ea\u4e9b",
    "\u5f53\u524d\u8d44\u6599",
    "\u6302\u8f7d",
    "knowledge",
    "source",
    "materials",
    "files",
    "zhishiku",
)

GREETING_PATTERNS = (
    "hi",
    "hello",
    "\u4f60\u597d",
    "\u60a8\u597d",
    "\u65e9\u4e0a\u597d",
    "\u4e0b\u5348\u597d",
    "\u665a\u4e0a\u597d",
    "\u5728\u5417",
    "\u8c22\u8c22",
    "\u611f\u8c22",
    "ok",
    "\u597d\u7684",
    "\u597d",
    "\u55ef",
    "\u662f\u7684",
    "yes",
    "thanks",
    "thank you",
)

EXACT_GREETING_PATTERNS = {"ok", "yes", "\u597d\u7684", "\u597d", "\u55ef", "\u662f\u7684"}

STRICT_GREETING_MESSAGE = (
    "\u4f60\u597d\uff0c\u6211\u5728\u3002\u4f60\u53ef\u4ee5\u56f4\u7ed5\u5f53\u524d\u8bfe\u5802\u77e5\u8bc6\u5e93\u91cc\u7684\u5185\u5bb9\u63d0\u95ee\uff1b"
    "\u5982\u679c\u95ee\u9898\u8d85\u51fa\u8d44\u6599\u8303\u56f4\uff0c\u6211\u4f1a\u63d0\u9192\u4f60\u56de\u5230\u672c\u8282\u8bfe\u7684\u77e5\u8bc6\u70b9\u3002"
)

APPRECIATION_PATTERNS = (
    "\u8c22\u8c22",
    "\u611f\u8c22",
    "\u591a\u8c22",
    "\u8f9b\u82e6\u4e86",
    "\u4e0d\u9519",
    "\u771f\u4e0d\u9519",
    "\u592a\u597d\u4e86",
    "\u5f88\u597d",
    "\u660e\u767d\u4e86",
    "\u61c2\u4e86",
    "thanks",
    "thank you",
    "good",
    "great",
    "nice",
    "understood",
)

STRICT_APPRECIATION_MESSAGE = (
    "\u4e0d\u5ba2\u6c14\uff0c\u5f88\u9ad8\u5174\u80fd\u5e2e\u5230\u4f60\u3002\u4f60\u53ef\u4ee5\u7ee7\u7eed\u56f4\u7ed5\u672c\u8282\u8bfe\u5185\u5bb9\u63d0\u95ee\uff0c"
    "\u6211\u4f1a\u5c3d\u91cf\u7ed3\u5408\u5f53\u524d\u77e5\u8bc6\u5e93\u5e2e\u4f60\u68b3\u7406\u3002"
)

OPENAPI_TAGS = [
    {"name": "System", "description": "System status APIs."},
    {"name": "Auth", "description": "Teacher login APIs."},
    {"name": "Student Chat", "description": "Student chat APIs."},
    {"name": "OpenAI Compatible", "description": "OpenAI style APIs."},
    {"name": "Teacher Config", "description": "Teacher policy APIs."},
    {"name": "Classroom Records", "description": "Teacher-owned local classroom history."},
    {"name": "Admin", "description": "Admin management APIs."},
    {"name": "Model Catalog", "description": "Model catalog APIs."},
    {"name": "Knowledge", "description": "Knowledge base APIs."},
    {"name": "Other", "description": "Classroom utility APIs."},
]

API_DOCS = {
    ("GET", "/health"): ("Health", "Check whether EduGate is online."),
    ("POST", "/auth/login"): ("Login", "Login with teacher username and password."),
    ("POST", "/auth/local-session"): ("Local teacher session", "Open the portable teacher console from this computer."),
    ("GET", "/models"): ("List upstream models", "Read models from the configured upstream provider when available."),
    ("POST", "/chat"): ("Student chat", "Without teacher_id this uses open default; with teacher_id it uses that teacher policy."),
    ("POST", "/chat/stream"): ("Student stream chat", "POST + text/event-stream chat API."),
    ("POST", "/classroom/join"): (
        "Join classroom",
        "Silently exchange the classroom link token and stable browser device ID for a student session.",
    ),
    ("POST", "/v1/chat/completions"): ("OpenAI compatible chat", "Third-party client entry."),
    ("GET", "/config"): ("Get teacher config", "Read current login teacher policy."),
    ("POST", "/config/model"): ("Switch teacher model", "Switch current login teacher model."),
    ("POST", "/config/ai"): ("Switch AI", "Switch current login teacher AI availability."),
    ("PUT", "/config/scenarios/{scenario_id}"): ("Update teacher policy", "When scenario_id is default, update current login teacher policy."),
    ("GET", "/admin/dashboard"): ("Dashboard", "Admin dashboard."),
    ("GET", "/admin/logs"): ("Logs", "Admin request logs."),
    ("GET", "/admin/teachers"): ("Teachers", "List teacher accounts."),
    ("POST", "/admin/teachers"): ("Upsert teacher", "Create or update teacher account."),
    ("PATCH", "/admin/teachers/{username}/password"): ("Change password", "Change teacher password."),
    ("DELETE", "/admin/teachers/{username}"): ("Disable teacher", "Disable a teacher account."),
    ("DELETE", "/admin/teachers/{username}/hard-delete"): ("Delete teacher", "Hard delete a teacher account."),
    ("GET", "/admin/models"): ("Admin models", "Read model catalog."),
    ("POST", "/admin/models/discover"): (
        "Discover provider models",
        "Read available model IDs from one identified OpenAI-compatible provider.",
    ),
    ("POST", "/admin/models/batch-import"): (
        "Batch import models",
        "Import selected models under a provider-scoped local identity while retaining native upstream IDs.",
    ),
    ("POST", "/admin/models"): ("Upsert model", "Create or update model catalog item."),
    ("PATCH", "/admin/models/{model_id}"): ("Patch model", "Update model catalog item."),
    ("POST", "/admin/models/{model_id}/set-default"): ("Set current model", "Set current admin teacher policy model."),
    ("GET", "/admin/providers"): ("Providers", "Provider status."),
    ("DELETE", "/admin/providers/{provider_id}"): (
        "Delete provider",
        "Delete one provider and all of its models, optionally switching active references first.",
    ),
    ("POST", "/admin/providers/{name}/test"): ("Test provider", "Test provider connectivity."),
    ("GET", "/admin/sources"): ("Sources", "List knowledge sources."),
    ("POST", "/admin/sources"): ("Upsert source", "Create or update knowledge source."),
    ("POST", "/admin/session/source"): ("Set source", "Legacy compatibility API."),
    ("GET", "/model-catalog"): ("Model catalog", "Read model catalog."),
    ("POST", "/model-catalog"): ("Upsert model catalog", "Admin model catalog upsert."),
    ("DELETE", "/model-catalog/{model_id}"): (
        "Delete model catalog",
        "Delete a model, optionally switching active references to a replacement model atomically.",
    ),
    ("GET", "/knowledge/sources"): ("Knowledge sources", "List knowledge sources."),
    ("POST", "/knowledge/sources"): ("Upsert knowledge source", "Create or update knowledge source."),
    ("DELETE", "/knowledge/sources/{source_id}"): ("Delete knowledge source", "Delete source and indexes."),
    ("POST", "/knowledge/sources/{source_id}/open-folder"): (
        "Open knowledge folder",
        "Open a source's fixed local folder on the teacher computer.",
    ),
    ("POST", "/knowledge/sources/{source_id}/scan"): (
        "Scan knowledge folder",
        "Incrementally synchronize supported files in a source folder.",
    ),
    ("GET", "/knowledge/files"): ("Knowledge files", "List uploaded files."),
    ("POST", "/knowledge/files"): ("Upload knowledge file", "Upload txt, md, pdf and other files."),
    ("DELETE", "/knowledge/files/{file_id}"): ("Delete knowledge file", "Delete file and chunks."),
    ("POST", "/run_python"): ("Run Python", "Run small classroom Python examples."),
    ("POST", "/run_python/stream"): ("Stream Python", "Queue a classroom Python task and stream status and output as SSE."),
    ("GET", "/teacher/classroom-records"): ("Classroom records", "List local classroom sessions visible to the signed-in teacher."),
    ("GET", "/teacher/classroom-records/{run_id}"): ("Classroom record detail", "Read anonymous student turns for one classroom."),
    ("DELETE", "/teacher/classroom-records/{run_id}"): ("Delete classroom record", "Permanently delete one visible classroom record."),
}


def _tag_for_path(path: str) -> str:
    if path == "/health":
        return "System"
    if path.startswith("/auth/"):
        return "Auth"
    if path in {"/chat", "/chat/stream"}:
        return "Student Chat"
    if path.startswith("/v1/"):
        return "OpenAI Compatible"
    if path.startswith("/config") or path == "/models":
        return "Teacher Config"
    if path.startswith("/teacher/classroom-records"):
        return "Classroom Records"
    if path.startswith("/admin/"):
        return "Admin"
    if path.startswith("/model-catalog"):
        return "Model Catalog"
    if path.startswith("/knowledge/"):
        return "Knowledge"
    if path in {"/run_python", "/run_python/stream"}:
        return "Other"
    return "System"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    business_db.init()
    business_db.seed_teacher(
        username=settings.admin_username,
        password=settings.admin_password,
        display_name="教师管理员",
        role="admin",
    )
    business_db.start_writer()
    if settings.portable_mode:
        classroom_access.end()
    await python_pool.start()
    try:
        yield
    finally:
        await python_pool.stop()
        if python_record_tasks:
            await asyncio.gather(*list(python_record_tasks), return_exceptions=True)
        await asyncio.to_thread(business_db.stop_writer)
        await asyncio.to_thread(business_db.checkpoint)
        await asyncio.to_thread(knowledge_store.checkpoint)
        await client.close()


app = FastAPI(
    title="EduGate API",
    summary="EduGate API",
    description=(
        "EduGate sits between student pages or third-party clients and the teacher-selected upstream model provider. "
        "Requests without teacher_id use open default. Requests with teacher_id use that teacher policy."
    ),
    version="1.5.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


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
            operation["tags"] = [_tag_for_path(path)]
            doc = API_DOCS.get((method.upper(), path))
            if doc:
                operation["summary"], operation["description"] = doc
            operation.setdefault("description", "EduGate API.")
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)


class ClientMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ClientMessage] = Field(..., min_length=1)
    scenario_id: str = Field(default="default", min_length=1)
    teacher_id: str | None = Field(default=None, min_length=1)


class V1ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[ClientMessage] = Field(..., min_length=1)
    model: str | None = None
    stream: bool = False
    scenario_id: str = Field(default="default", min_length=1)
    teacher_id: str | None = Field(default=None, min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)


class TeachingScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(default=settings.default_model, min_length=1)
    ai_enabled: bool = Field(default=True)
    system_prompt: str = ""
    temperature: float = Field(default=0.4, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    knowledge_source_id: str | None = None
    knowledge_strict: bool = Field(default=False)


def _provider_catalog_id(provider: str, base_url: str | None) -> str:
    identity = f"{provider.strip().casefold()}\0{(base_url or '').strip().rstrip('/').casefold()}"
    return f"provider-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _model_catalog_id(provider_id: str, upstream_model_id: str) -> str:
    identity = f"{provider_id}\0{upstream_model_id.strip()}"
    return f"model-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


class ModelCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    provider: str = "OpenAI Compatible"
    description: str = ""
    source: Literal["litellm", "openai_compatible"] = "openai_compatible"
    base_url: str | None = Field(default=None, min_length=1)
    provider_id: str | None = Field(default=None, min_length=1, max_length=80)
    upstream_model_id: str | None = Field(default=None, min_length=1, max_length=500)
    credential_id: str | None = None
    api_key: str | None = Field(default=None, min_length=1)


class ModelCatalogPublicItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    provider: str
    description: str = ""
    source: Literal["litellm", "openai_compatible"] = "openai_compatible"
    base_url: str | None = None
    provider_id: str
    upstream_model_id: str
    api_key_set: bool = False


class ModelProviderConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="OpenAI Compatible", min_length=1, max_length=120)
    provider_id: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str = Field(..., min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    credential_model_id: str | None = Field(default=None, min_length=1, max_length=300)


class ModelBatchImportRequest(ModelProviderConnectionRequest):
    model_ids: list[str] = Field(..., min_length=1, max_length=200)
    display_names: dict[str, str] = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)


class RuntimeConfigData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: dict[str, TeachingScenario] = Field(
        default_factory=lambda: {"default": TeachingScenario()}
    )
    teacher_policies: dict[str, TeachingScenario] = Field(default_factory=dict)
    model_catalog: dict[str, ModelCatalogItem] = Field(default_factory=dict)
    legacy_runtime_migration_complete: bool = False


class ModelSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., min_length=1)


class ScenarioUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1)
    ai_enabled: bool | None = None
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    knowledge_source_id: str | None = Field(default=None, min_length=1)
    knowledge_strict: bool | None = None


class KnowledgeSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str = ""


class AIEnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(default="admin", min_length=3, max_length=80)
    password: str = Field(..., min_length=10, max_length=200)
    display_name: str = Field(default="教师管理员", min_length=1, max_length=120)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=10, max_length=200)


class TeacherAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=10, max_length=200)
    display_name: str = Field(default="", max_length=120)
    role: Literal["teacher", "admin"] = "teacher"
    is_active: bool = True


class TeacherPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(..., min_length=10, max_length=200)


class PythonRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=settings.python_runner_max_code_chars)
    teacher_id: str | None = Field(default=None, min_length=1)


class PythonRunResponse(BaseModel):
    job_id: str
    worker_id: int
    queue_wait_ms: int
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int


class StudentJoinResponse(BaseModel):
    student_token: str
    student_session_id: str
    computer_name: str
    client_ip: str
    expires_in: int


class StudentJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    computer_name: str = Field(default="", max_length=80)
    device_id: str = Field(default="", max_length=128)


class SystemActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["restart", "shutdown"]


class AdvancedSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]


class PlatformKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, max_length=500)


class ConfigResponse(BaseModel):
    scenarios: dict[str, TeachingScenario]
    model_catalog: dict[str, ModelCatalogPublicItem]
    litellm_base_url: str
    litellm_api_prefix: str
    upstream_provider: str
    upstream_base_url: str
    knowledge_sources: list[KnowledgeSource]


def _teacher_policy_key(username: str) -> str:
    return username.strip().lower()


class RuntimeConfig:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data = self._load()
        self._migrate_plaintext_secrets()
        if not self.data.legacy_runtime_migration_complete:
            self._migrate_open_default()
            self._ensure_standalone_default_model()
            self.data.legacy_runtime_migration_complete = True
            self.save()
        self._migrate_model_identities()

    def _load(self) -> RuntimeConfigData:
        if not self._path.exists():
            return RuntimeConfigData()
        return RuntimeConfigData.model_validate_json(self._path.read_text(encoding="utf-8"))

    def _migrate_open_default(self) -> bool:
        legacy_default = self.data.scenarios.get("default")
        changed = False
        if legacy_default and self._has_classroom_policy(legacy_default) and not self.data.teacher_policies:
            self.data.teacher_policies[_teacher_policy_key(settings.admin_username)] = legacy_default
            changed = True
        open_default = TeachingScenario()
        if legacy_default != open_default:
            self.data.scenarios["default"] = open_default
            changed = True
        return changed

    def _ensure_standalone_default_model(self) -> None:
        if settings.deployment_mode != "standalone":
            return
        if not settings.upstream_base_url and not settings.upstream_api_key:
            return
        current = self.data.model_catalog.get(settings.default_model)
        if current is not None:
            if current.description == "Local classroom default upstream model. Edit base_url and api_key before live use.":
                self.data.model_catalog[settings.default_model] = current.model_copy(
                    update={"description": "本地课堂默认上游模型，上课前请填写接口地址和 API 密钥。"}
                )
            return
        credential_id = f"model:{settings.default_model}"
        if settings.upstream_api_key:
            secret_store.set(credential_id, settings.upstream_api_key)
        self.data.model_catalog[settings.default_model] = ModelCatalogItem(
            id=settings.default_model,
            name=settings.default_model,
            provider=settings.upstream_provider,
            description="本地课堂默认上游模型，上课前请填写接口地址和 API 密钥。",
            source="openai_compatible",
            base_url=settings.upstream_base_url or None,
            credential_id=credential_id,
        )

    def _migrate_plaintext_secrets(self) -> None:
        changed = False
        for model_id, model in list(self.data.model_catalog.items()):
            credential_id = model.credential_id or f"model:{model_id}"
            if model.api_key:
                secret_store.set(credential_id, model.api_key)
                changed = True
            if model.credential_id != credential_id or model.api_key is not None:
                self.data.model_catalog[model_id] = model.model_copy(
                    update={"credential_id": credential_id, "api_key": None}
                )
                changed = True
        if changed:
            self.save()

    def _migrate_model_identities(self) -> None:
        changed = False
        for model_id, model in list(self.data.model_catalog.items()):
            provider_id = model.provider_id or _provider_catalog_id(model.provider, model.base_url)
            upstream_model_id = model.upstream_model_id or model.id
            if model.provider_id != provider_id or model.upstream_model_id != upstream_model_id:
                self.data.model_catalog[model_id] = model.model_copy(
                    update={
                        "provider_id": provider_id,
                        "upstream_model_id": upstream_model_id,
                    }
                )
                changed = True
        if changed:
            self.save()

    @staticmethod
    def _has_classroom_policy(scenario: TeachingScenario) -> bool:
        return any(
            [
                bool(scenario.system_prompt.strip()),
                scenario.knowledge_source_id is not None,
                scenario.knowledge_strict,
                not scenario.ai_enabled,
                scenario.temperature != TeachingScenario().temperature,
                scenario.max_tokens is not None,
            ]
        )

    def save(self) -> None:
        with self._lock:
            temp = self._path.with_suffix(self._path.suffix + ".tmp")
            payload = self.data.model_dump_json(indent=2)
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._path)

    def get_scenario(self, scenario_id: str) -> TeachingScenario:
        scenario = self.data.scenarios.get(scenario_id)
        if scenario is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown teaching scenario: {scenario_id}",
            )
        return scenario

    def get_teacher_policy(self, username: str) -> TeachingScenario:
        with self._lock:
            key = _teacher_policy_key(username)
            scenario = self.data.teacher_policies.get(key)
            if scenario is None:
                scenario = TeachingScenario()
                self.data.teacher_policies[key] = scenario
                self.save()
            return scenario

    def update_scenario(self, scenario_id: str, request: ScenarioUpdateRequest) -> TeachingScenario:
        with self._lock:
            current = self.data.scenarios.get(scenario_id, TeachingScenario())
            changes = request.model_dump(exclude_unset=True)
            updated = current.model_copy(update=changes)
            self.data.scenarios[scenario_id] = updated
            self.save()
            return updated

    def update_teacher_policy(self, username: str, request: ScenarioUpdateRequest) -> TeachingScenario:
        with self._lock:
            key = _teacher_policy_key(username)
            current = self.data.teacher_policies.get(key, TeachingScenario())
            changes = request.model_dump(exclude_unset=True)
            updated = current.model_copy(update=changes)
            self.data.teacher_policies[key] = updated
            self.save()
            return updated

    def upsert_model(self, request: ModelCatalogItem) -> ModelCatalogItem:
        if request.source == "openai_compatible" and not request.base_url:
            raise HTTPException(status_code=400, detail="base_url is required for OpenAI-compatible models")
        request = request.model_copy(
            update={
                "provider_id": request.provider_id or _provider_catalog_id(request.provider, request.base_url),
                "upstream_model_id": request.upstream_model_id or request.id,
            }
        )
        with self._lock:
            current = self.data.model_catalog.get(request.id)
            credential_id = (current.credential_id if current else None) or f"model:{request.id}"
            if request.api_key:
                secret_store.set(credential_id, request.api_key)
            request = request.model_copy(update={"credential_id": credential_id, "api_key": None})
            self.data.model_catalog[request.id] = request
            self.save()
            return request

    def upsert_models(self, requests: list[ModelCatalogItem]) -> list[ModelCatalogItem]:
        for request in requests:
            if request.source == "openai_compatible" and not request.base_url:
                raise HTTPException(status_code=400, detail="base_url is required for OpenAI-compatible models")
        with self._lock:
            models: list[ModelCatalogItem] = []
            for request in requests:
                request = request.model_copy(
                    update={
                        "provider_id": request.provider_id or _provider_catalog_id(request.provider, request.base_url),
                        "upstream_model_id": request.upstream_model_id or request.id,
                    }
                )
                current = self.data.model_catalog.get(request.id)
                credential_id = (current.credential_id if current else None) or f"model:{request.id}"
                if request.api_key:
                    secret_store.set(credential_id, request.api_key)
                model = request.model_copy(update={"credential_id": credential_id, "api_key": None})
                self.data.model_catalog[model.id] = model
                models.append(model)
            self.save()
            return models

    def find_provider_model(self, provider_id: str, upstream_model_id: str) -> ModelCatalogItem | None:
        normalized_upstream_id = upstream_model_id.strip()
        with self._lock:
            return next(
                (
                    model
                    for model in self.data.model_catalog.values()
                    if model.provider_id == provider_id
                    and (model.upstream_model_id or model.id) == normalized_upstream_id
                ),
                None,
            )

    def delete_model(self, model_id: str, *, replacement_model_id: str | None = None) -> list[str]:
        with self._lock:
            model = self.data.model_catalog.get(model_id)
            if model is None:
                return []
            references = [
                scenario_id
                for scenario_id, scenario in {
                    **self.data.scenarios,
                    **{f"teacher:{key}": value for key, value in self.data.teacher_policies.items()},
                }.items()
                if scenario.model == model_id
            ]
            if references and not replacement_model_id:
                raise HTTPException(
                    status_code=409,
                    detail="该模型仍被课堂配置使用。请先选择或导入另一个可用模型，再删除。",
                )
            if references:
                replacement = self.data.model_catalog.get(replacement_model_id or "")
                if replacement is None or replacement.id == model_id:
                    raise HTTPException(status_code=400, detail="替代模型不存在或与待删除模型相同。")
                for scenario_id, scenario in list(self.data.scenarios.items()):
                    if scenario.model == model_id:
                        self.data.scenarios[scenario_id] = scenario.model_copy(
                            update={"model": replacement.id}
                        )
                for username, scenario in list(self.data.teacher_policies.items()):
                    if scenario.model == model_id:
                        self.data.teacher_policies[username] = scenario.model_copy(
                            update={"model": replacement.id}
                        )
            self.data.model_catalog.pop(model_id)
            self.save()
            secret_store.delete(model.credential_id)
            return references

    def delete_provider(
        self,
        provider_id: str,
        *,
        replacement_model_id: str | None = None,
    ) -> tuple[list[str], list[str]]:
        with self._lock:
            provider_models = [
                model
                for model in self.data.model_catalog.values()
                if model.source == "openai_compatible"
                and (model.provider_id or _provider_catalog_id(model.provider, model.base_url)) == provider_id
            ]
            if not provider_models:
                return [], []

            model_ids = {model.id for model in provider_models}
            references = [
                scenario_id
                for scenario_id, scenario in {
                    **self.data.scenarios,
                    **{f"teacher:{key}": value for key, value in self.data.teacher_policies.items()},
                }.items()
                if scenario.model in model_ids
            ]
            replacement = None
            if replacement_model_id:
                replacement = self.data.model_catalog.get(replacement_model_id)
                if replacement is None or replacement.id in model_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="替代模型不存在，或仍属于待删除的供应商。",
                    )
            if references and replacement is None:
                raise HTTPException(
                    status_code=409,
                    detail="该供应商仍有模型被课堂配置使用。请先添加其他供应商的可用模型，再删除。",
                )
            if references and replacement is not None:
                for scenario_id, scenario in list(self.data.scenarios.items()):
                    if scenario.model in model_ids:
                        self.data.scenarios[scenario_id] = scenario.model_copy(
                            update={"model": replacement.id}
                        )
                for username, scenario in list(self.data.teacher_policies.items()):
                    if scenario.model in model_ids:
                        self.data.teacher_policies[username] = scenario.model_copy(
                            update={"model": replacement.id}
                        )

            credential_ids = {model.credential_id for model in provider_models if model.credential_id}
            for model_id in model_ids:
                self.data.model_catalog.pop(model_id, None)
            self.save()
            for credential_id in credential_ids:
                secret_store.delete(credential_id)
            return sorted(model_ids), references


runtime_config = RuntimeConfig(settings.runtime_config_path)


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


def _sync_portable_admin_password(username: str, password: str) -> None:
    if not settings.portable_mode or username != settings.admin_username:
        return
    config_path = Path(settings.config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)
    set_key(str(config_path), "ADMIN_PASSWORD", password, quote_mode="always")
    settings.admin_password = password


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    record = sessions.resolve(x_admin_token or "")
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired admin token")
    teacher = business_db.get_teacher(record.username)
    if not teacher or not teacher.get("is_active"):
        sessions.revoke(x_admin_token or "")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Teacher is inactive")
    return teacher


def require_super_admin(current_teacher: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if current_teacher.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required")
    return current_teacher


def _is_super_admin(teacher: dict[str, Any]) -> bool:
    return teacher.get("role") == "admin"


def _teacher_source_prefix(teacher: dict[str, Any]) -> str:
    return f"{teacher.get('username', '').strip().lower()}-"


def _can_access_source(source_id: str, teacher: dict[str, Any], *, write: bool = False) -> bool:
    if _is_super_admin(teacher):
        return True
    if source_id == "general":
        return not write
    return source_id.startswith(_teacher_source_prefix(teacher))


def _ensure_source_access(source_id: str, teacher: dict[str, Any], *, write: bool = False) -> None:
    if not _can_access_source(source_id, teacher, write=write):
        prefix = _teacher_source_prefix(teacher)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Teacher knowledge source id must start with '{prefix}'",
        )


def require_platform_key(authorization: str | None = Header(default=None)) -> None:
    platform_api_key = secret_store.get("system:platform_api_key") or settings.platform_api_key
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
    if not classroom_access.active():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Classroom is not active")
    if x_student_token:
        record = student_sessions.resolve(
            x_student_token,
            classroom_token=classroom_access.token(),
        )
        if record is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired student token")
        key = f"chat:student:{record.student_id}"
        if not rate_limiter.allow(key, limit=settings.classroom_rate_limit, window_seconds=60):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom request limit exceeded")
        return student_sessions.identity(record)
    token = x_class_token or class_token
    if not classroom_access.matches(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid classroom token")
    client_ip = _client_ip(request)
    key = f"chat:ip:{client_ip}"
    if not rate_limiter.allow(key, limit=settings.classroom_rate_limit, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom request limit exceeded")
    return StudentIdentity(
        student_id=classroom_access.legacy_student_id(client_ip),
        computer_name=_computer_name(x_computer_name, client_ip),
        client_ip=client_ip,
    )


def _resolve_chat_context(request: ChatRequest) -> tuple[str, TeachingScenario, dict[str, Any] | None]:
    teacher = None
    scenario_id = request.scenario_id
    if request.teacher_id:
        teacher = business_db.get_teacher(request.teacher_id)
        if not teacher:
            raise HTTPException(status_code=404, detail="Unknown teacher")
        if not teacher.get("is_active"):
            raise HTTPException(status_code=403, detail="Teacher is inactive")
        scenario_id = f"teacher:{_teacher_policy_key(teacher['username'])}"
        scenario = runtime_config.get_teacher_policy(teacher["username"])
    else:
        scenario = runtime_config.get_scenario(scenario_id)
    if not scenario.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI service is disabled for the current teacher policy",
        )
    return scenario_id, scenario, teacher


def _build_chat_payload(
    request: ChatRequest,
    *,
    scenario: TeachingScenario | None = None,
    stream: bool = False,
    strict_topic_related: bool = False,
) -> dict[str, Any]:
    if scenario is None:
        _, scenario, _ = _resolve_chat_context(request)
    messages = [message.model_dump() for message in request.messages]
    if scenario.system_prompt.strip():
        messages.insert(0, {"role": "system", "content": scenario.system_prompt})
    knowledge_context = _build_knowledge_context(request, scenario, strict_topic_related=strict_topic_related)
    if knowledge_context:
        insert_at = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(insert_at, {"role": "system", "content": knowledge_context})

    payload: dict[str, Any] = {
        "model": scenario.model,
        "messages": messages,
        "temperature": scenario.temperature,
        "stream": stream,
    }
    if scenario.max_tokens is not None:
        payload["max_tokens"] = scenario.max_tokens
    return payload


def _knowledge_hits(request: ChatRequest, scenario: TeachingScenario):
    if not scenario.knowledge_source_id:
        return []
    latest_user_message = next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )
    return knowledge_store.search(
        scenario.knowledge_source_id,
        latest_user_message,
        limit=settings.knowledge_search_limit,
    )


def _is_strict_no_hit(request: ChatRequest, scenario: TeachingScenario) -> bool:
    return bool(scenario.knowledge_strict and scenario.knowledge_source_id and not _knowledge_hits(request, scenario))


async def _llm_topic_related(request: ChatRequest, scenario: TeachingScenario) -> bool:
    profile = _knowledge_source_summary(scenario.knowledge_source_id)
    payload = {
        "model": scenario.model,
        "temperature": 0,
        "max_tokens": 8,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a classroom knowledge-base topic gate. Only decide whether "
                    "the student question is related to the current knowledge-base topic. "
                    "Output only RELATED or UNRELATED. Do not answer the student question."
                ),
            },
            {
                "role": "user",
                "content": f"Knowledge base summary:\n{profile}\n\nStudent question: {_latest_user_text(request)}",
            },
        ],
    }
    try:
        response = await _chat_completion(payload)
    except Exception:
        return False
    content = (
        response.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
        .upper()
    )
    if "UNRELATED" in content:
        return False
    return "RELATED" in content

async def _strict_miss_decision(request: ChatRequest, scenario: TeachingScenario) -> tuple[bool, bool]:
    if not _is_strict_no_hit(request, scenario):
        return False, False
    if _is_light_social_message(request) or _is_knowledge_overview_question(request):
        return True, False
    overview = _knowledge_source_summary(scenario.knowledge_source_id)
    if "no searchable files" in overview or "does not exist" in overview or "No knowledge source" in overview:
        return True, False
    topic_related = await _llm_topic_related(request, scenario)
    return (not topic_related), topic_related


def _latest_user_text(request: ChatRequest) -> str:
    return next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )


def _is_knowledge_overview_question(request: ChatRequest) -> bool:
    text = _latest_user_text(request)
    return any(keyword in text for keyword in KNOWLEDGE_OVERVIEW_KEYWORDS)


def _is_light_social_message(request: ChatRequest) -> bool:
    text = _latest_user_text(request).strip().lower()
    normalized = text.strip("闂備線娼уΛ妤呭磻婵犲嫭顫曢柟鐑橆殕閺??,.闂?")
    if not normalized:
        return True
    if normalized in GREETING_PATTERNS:
        return True
    fuzzy_patterns = [pattern for pattern in GREETING_PATTERNS if pattern not in EXACT_GREETING_PATTERNS]
    return len(normalized) <= 12 and any(pattern in normalized for pattern in fuzzy_patterns)


def _is_appreciation_message(request: ChatRequest) -> bool:
    text = _latest_user_text(request).strip().lower()
    normalized = text.strip("闂備線娼уΛ妤呭磻婵犲嫭顫曢柟鐑橆殕閺??,.闂?")
    return 0 < len(normalized) <= 24 and any(pattern in normalized for pattern in APPRECIATION_PATTERNS)


def _knowledge_source_summary(source_id: str | None) -> str:
    if not source_id:
        return "No knowledge source is mounted."
    try:
        source = knowledge_store.get_source(source_id)
        files = knowledge_store.list_files(source_id)
    except HTTPException:
        return f"Mounted knowledge source `{source_id}` does not exist. Please choose another source."
    if not files:
        return (
            f"Mounted knowledge source is `{source.id}` ({source.name}), but it has no searchable files or chunks. "
            "Please upload materials first or switch to a source with files."
        )
    file_lines = [
        f"- {file.filename} ({file.chunk_count} chunks)"
        for file in files
    ]
    return "\n".join(
        [
            f"Mounted knowledge source: `{source.id}` ({source.name}).",
            "Indexed materials:",
            *file_lines,
            "You can ask questions around these materials.",
        ]
    )

def _strict_knowledge_message(request: ChatRequest, scenario: TeachingScenario) -> str:
    if _is_appreciation_message(request):
        return STRICT_APPRECIATION_MESSAGE
    if _is_light_social_message(request):
        return STRICT_GREETING_MESSAGE
    overview = _knowledge_source_summary(scenario.knowledge_source_id)
    if _is_knowledge_overview_question(request):
        return overview
    if "no searchable files" in overview or "does not exist" in overview or "No knowledge source" in overview:
        return overview
    return STRICT_KNOWLEDGE_MISS_MESSAGE


def _strict_knowledge_miss_response(request: ChatRequest, scenario: TeachingScenario) -> dict[str, Any]:
    message = _strict_knowledge_message(request, scenario)
    return {
        "id": f"chatcmpl-edugate-strict-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": scenario.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": message,
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _strict_knowledge_miss_sse(request: ChatRequest, scenario: TeachingScenario):
    message = _strict_knowledge_message(request, scenario)
    chunk = {
        "id": f"chatcmpl-edugate-strict-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": scenario.model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": message},
                "finish_reason": None,
            }
        ],
    }
    done = {
        **chunk,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def _public_model(model: ModelCatalogItem) -> ModelCatalogPublicItem:
    return ModelCatalogPublicItem(
        id=model.id,
        name=model.name,
        provider=model.provider,
        description=model.description,
        source=model.source,
        base_url=model.base_url,
        provider_id=model.provider_id or _provider_catalog_id(model.provider, model.base_url),
        upstream_model_id=model.upstream_model_id or model.id,
        api_key_set=secret_store.has(model.credential_id),
    )


def _public_model_catalog() -> dict[str, ModelCatalogPublicItem]:
    return {
        model_id: _public_model(model)
        for model_id, model in runtime_config.data.model_catalog.items()
    }


def _provider_api_key(request: ModelProviderConnectionRequest) -> tuple[str, bool]:
    if request.api_key:
        return request.api_key, False

    normalized_base_url = request.base_url.rstrip("/").casefold()
    requested_provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    candidates: list[ModelCatalogItem] = []
    if request.credential_model_id:
        model = runtime_config.data.model_catalog.get(request.credential_model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="找不到用于复用密钥的已有模型。")
        candidates.append(model)
    else:
        candidates.extend(runtime_config.data.model_catalog.values())

    for model in candidates:
        if model.source != "openai_compatible" or not model.base_url:
            continue
        if model.provider_id != requested_provider_id:
            continue
        if model.base_url.rstrip("/").casefold() != normalized_base_url:
            continue
        api_key = secret_store.get(model.credential_id)
        if api_key:
            return api_key, True
    raise HTTPException(status_code=400, detail="请填写 API Key，或先从同一接口地址的已有模型填入表单。")


async def _discover_provider_models(
    request: ModelProviderConnectionRequest,
) -> tuple[list[dict[str, str]], str, bool]:
    api_key, used_saved_key = _provider_api_key(request)
    try:
        models = await client.list_openai_models(base_url=request.base_url, api_key=api_key)
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300].strip() or error.response.reason_phrase
        raise HTTPException(
            status_code=502,
            detail=f"上游模型列表接口返回 HTTP {error.response.status_code}：{detail}",
        ) from error
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="获取上游模型列表超时。") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"无法连接上游模型列表接口：{error!s}") from error
    return models, api_key, used_saved_key


def _direct_openai_model(model_id: str) -> ModelCatalogItem | None:
    model = runtime_config.data.model_catalog.get(model_id)
    if model and model.source == "openai_compatible":
        return model
    return None


def _validate_model_selection(model_id: str) -> None:
    if settings.deployment_mode != "standalone":
        return
    model = runtime_config.data.model_catalog.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
    if model.source == "openai_compatible" and (
        not model.base_url or not secret_store.has(model.credential_id)
    ):
        raise HTTPException(status_code=400, detail=f"Model is not fully configured: {model_id}")


async def _chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    direct_model = _direct_openai_model(str(payload.get("model", "")))
    if direct_model:
        api_key = secret_store.get(direct_model.credential_id)
        if not direct_model.base_url or not api_key:
            raise HTTPException(
                status_code=503,
                detail=f"Direct OpenAI-compatible model is missing base_url or api_key: {direct_model.id}",
            )
        async with model_semaphore:
            return await client.openai_chat_completion(
                base_url=direct_model.base_url,
                api_key=api_key,
                payload={
                    **payload,
                    "model": direct_model.upstream_model_id or direct_model.id,
                },
            )
    async with model_semaphore:
        return await client.chat_completion(payload)


async def _stream_chat_completion(payload: dict[str, Any]):
    direct_model = _direct_openai_model(str(payload.get("model", "")))
    if direct_model:
        api_key = secret_store.get(direct_model.credential_id)
        if not direct_model.base_url or not api_key:
            event = {
                "status_code": 503,
                "detail": f"Direct OpenAI-compatible model is missing base_url or api_key: {direct_model.id}",
            }
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        async with model_semaphore:
            async for chunk in client.stream_openai_chat_completion(
                base_url=direct_model.base_url,
                api_key=api_key,
                payload={
                    **payload,
                    "model": direct_model.upstream_model_id or direct_model.id,
                },
            ):
                yield chunk
        return
    async with model_semaphore:
        async for chunk in client.stream_chat_completion(payload):
            yield chunk


def _build_knowledge_context(
    request: ChatRequest,
    scenario: TeachingScenario,
    *,
    strict_topic_related: bool = False,
) -> str | None:
    if not scenario.knowledge_source_id:
        return None
    hits = _knowledge_hits(request, scenario)
    if not hits and not scenario.knowledge_strict:
        return None

    rules = [
        "The following content comes from the teacher-selected knowledge base. Prefer these snippets when answering.",
        "If the snippets are insufficient, say the materials do not provide enough evidence and guide the student with questions.",
        "Do not invent citations that are not in the knowledge base.",
    ]
    if scenario.knowledge_strict:
        rules.append("Strict mode: if the knowledge base is insufficient, do not complete the answer with outside knowledge.")
    snippets = [
        f"[{index}] Source: {hit.filename}; Chunk: {hit.text}"
        for index, hit in enumerate(hits, start=1)
    ]
    if not snippets:
        if strict_topic_related:
            snippets = [
                "The LLM topic gate judged this question related to the current knowledge-base topic, but keyword search found no exact chunk.",
                _knowledge_source_summary(scenario.knowledge_source_id),
                "Continue answering around the current knowledge-base topic and do not expand to unrelated topics.",
            ]
        else:
            snippets = ["No knowledge-base snippet matched the student question."]
    return "\n".join([*rules, "", "Knowledge-base snippets:", *snippets])

def _to_http_exception(error: httpx.HTTPStatusError) -> HTTPException:
    try:
        detail: Any = error.response.json()
    except ValueError:
        detail = error.response.text
    return HTTPException(status_code=error.response.status_code, detail=detail)


def _message_preview(messages: list[Any]) -> str | None:
    if not settings.log_message_preview:
        return None
    return latest_user_preview(messages)


def _latest_user_content(messages: list[Any]) -> str:
    return latest_user_preview(messages, limit=settings.classroom_record_max_content_chars)


def _chat_response_content(response: dict[str, Any] | None) -> str:
    if not response:
        return ""
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str):
        content = choice.get("text") if isinstance(choice.get("text"), str) else ""
    return content[: settings.classroom_record_max_content_chars]


def _record_classroom_turn(
    *,
    teacher_id: str | None,
    student: StudentIdentity,
    kind: str,
    input_content: str,
    output_content: str,
    status_code: int,
    latency_ms: int,
    queue_wait_ms: int | None = None,
    timed_out: bool | None = None,
) -> None:
    if not settings.classroom_recording_enabled or not teacher_id or not student.student_id:
        return
    try:
        business_db.record_classroom_turn(
            classroom_instance_id=classroom_access.classroom_id(),
            teacher_username=teacher_id,
            student_session_id=student.student_id,
            computer_name=student.computer_name,
            client_ip=student.client_ip,
            kind=kind,
            input_content=input_content,
            output_content=output_content,
            status_code=status_code,
            latency_ms=latency_ms,
            queue_wait_ms=queue_wait_ms,
            timed_out=timed_out,
        )
    except Exception as error:
        logger.warning("Failed to write classroom record: %s", error)


def _consume_sse_events(
    buffer: str,
) -> tuple[str, list[str], bool, list[dict[str, Any]]]:
    content: list[str] = []
    stream_done = False
    errors: list[dict[str, Any]] = []
    while True:
        boundary_index = buffer.find("\n\n")
        boundary_length = 2
        crlf_index = buffer.find("\r\n\r\n")
        if crlf_index >= 0 and (boundary_index < 0 or crlf_index < boundary_index):
            boundary_index = crlf_index
            boundary_length = 4
        if boundary_index < 0:
            break
        block = buffer[:boundary_index]
        buffer = buffer[boundary_index + boundary_length :]
        data = "\n".join(
            line.split(":", 1)[1].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        event_name = next(
            (
                line.split(":", 1)[1].strip()
                for line in block.splitlines()
                if line.startswith("event:")
            ),
            "",
        )
        if not data:
            continue
        if data == "[DONE]":
            stream_done = True
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event_name == "error":
            errors.append(payload if isinstance(payload, dict) else {"detail": payload})
            continue
        choice = (payload.get("choices") or [{}])[0]
        value = (choice.get("delta") or {}).get("content")
        if not isinstance(value, str):
            value = (choice.get("message") or {}).get("content")
        if not isinstance(value, str):
            value = choice.get("text")
        if isinstance(value, str):
            content.append(value)
    return buffer, content, stream_done, errors


async def _trace_chat_result(
    *,
    route: str,
    request: ChatRequest,
    scenario: TeachingScenario,
    effective_scenario_id: str,
    teacher_id: str | None,
    response: dict[str, Any] | None,
    status_code: int,
    latency_ms: int,
    error: str | None = None,
) -> None:
    usage = response.get("usage") if response else None
    business_db.log_request(
        route=route,
        scenario_id=effective_scenario_id,
        teacher_id=teacher_id,
        model=scenario.model,
        knowledge_source_id=scenario.knowledge_source_id,
        user_message_preview=_message_preview(request.messages),
        status_code=status_code,
        latency_ms=latency_ms,
        usage=usage,
        error=error,
    )
    output = None
    if settings.log_message_preview and response:
        output = response.get("choices", [{}])[0].get("message", {}).get("content")
    await langfuse.trace_chat(
        name=route,
        input_text=_message_preview(request.messages),
        output_text=output,
        metadata={
            "route": route,
            "scenario_id": effective_scenario_id,
            "teacher_id": teacher_id,
            "model": scenario.model,
            "knowledge_source_id": scenario.knowledge_source_id,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error,
        },
        usage=usage,
    )


async def _stream_with_errors(payload: dict[str, Any]):
    try:
        async for chunk in _stream_with_heartbeat(_stream_chat_completion(payload)):
            yield chunk
    except httpx.HTTPStatusError as error:
        try:
            detail: Any = error.response.json()
        except ValueError:
            detail = error.response.text
        event = {"status_code": error.response.status_code, "detail": detail}
        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    except httpx.HTTPError as error:
        event = {
            "status_code": 502,
            "detail": f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        }
        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


async def _stream_with_heartbeat(source: AsyncIterator[bytes]):
    iterator = source.__aiter__()
    pending: asyncio.Task[bytes] | None = None
    try:
        pending = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait({pending}, timeout=settings.stream_heartbeat_seconds)
            if not done:
                yield b": edugate-keep-alive\n\n"
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            yield chunk
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


async def _iterate_stream_bytes(source: AsyncIterator[bytes] | Iterable[bytes]):
    if hasattr(source, "__aiter__"):
        async for chunk in source:
            yield chunk
        return
    for chunk in source:
        yield chunk


async def _stream_with_completion_log(
    source: AsyncIterator[bytes] | Iterable[bytes],
    *,
    route: str,
    request: ChatRequest,
    scenario: TeachingScenario,
    effective_scenario_id: str,
    teacher_id: str | None,
    student: StudentIdentity,
):
    start = time.perf_counter()
    stream_chunks = 0
    stream_bytes = 0
    stream_done = False
    status_code = 200
    finish_reason = "ended_without_done"
    error_text: str | None = None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    event_buffer = ""
    assistant_parts: list[str] = []
    assistant_length = 0
    decoder_finalized = False

    def collect_events(decoded_text: str) -> None:
        nonlocal event_buffer, assistant_length, stream_done
        nonlocal status_code, finish_reason, error_text
        event_buffer += decoded_text
        event_buffer, extracted, observed_done, errors = _consume_sse_events(event_buffer)
        for content in extracted:
            remaining = settings.classroom_record_max_content_chars - assistant_length
            if remaining <= 0:
                break
            accepted = content[:remaining]
            assistant_parts.append(accepted)
            assistant_length += len(accepted)
        if observed_done:
            stream_done = True
            status_code = 200
            finish_reason = "done"
        for event in errors:
            try:
                status_code = int(event.get("status_code", 502))
            except (TypeError, ValueError):
                status_code = 502
            finish_reason = "upstream_error"
            detail = event.get("detail", event)
            error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            error_text = error_text[:1000]

    def flush_pending_events() -> None:
        nonlocal decoder_finalized
        if decoder_finalized:
            return
        decoder_finalized = True
        collect_events(decoder.decode(b"", final=True) + "\n\n")

    def write_log() -> None:
        business_db.log_request(
            route=route,
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=status_code,
            latency_ms=now_ms(start),
            stream_done=stream_done,
            stream_chunks=stream_chunks,
            stream_bytes=stream_bytes,
            stream_duration_ms=now_ms(start),
            stream_finish_reason=finish_reason,
            error=error_text,
        )
        output = "".join(assistant_parts)
        if not output and error_text:
            output = error_text
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=output,
            status_code=status_code,
            latency_ms=now_ms(start),
        )

    try:
        async for chunk in _iterate_stream_bytes(source):
            stream_chunks += 1
            stream_bytes += len(chunk)
            collect_events(decoder.decode(chunk))
            yield chunk
    except asyncio.CancelledError:
        status_code = 499
        finish_reason = "client_disconnected"
        error_text = "Streaming response was cancelled before EduGate observed [DONE]."
        flush_pending_events()
        write_log()
        raise
    except Exception as error:
        status_code = 500
        finish_reason = "server_exception"
        error_text = f"{type(error).__name__}: {error!s}"
        flush_pending_events()
        write_log()
        raise
    else:
        flush_pending_events()
        if stream_done:
            finish_reason = "done"
            status_code = 200
        elif finish_reason == "ended_without_done":
            error_text = "Stream ended before EduGate observed [DONE]."
        write_log()


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    response.headers["X-EduGate-App"] = "EduGate"
    return {"status": "ok"}


@app.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    return {
        "initialized": business_db.is_admin_initialized(settings.admin_username),
        "admin_username": settings.admin_username,
        "portable_mode": settings.portable_mode,
        "local_auto_login": settings.portable_auto_login,
    }


@app.post("/auth/local-session")
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


@app.post("/auth/setup")
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


@app.post("/auth/login")
async def login(http_request: Request, request: LoginRequest) -> dict[str, Any]:
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


@app.post("/auth/logout")
async def logout(
    current_teacher: dict[str, Any] = Depends(require_admin),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, str]:
    sessions.revoke(x_admin_token or "")
    return {"status": "logged_out", "username": current_teacher["username"]}


@app.post("/auth/password")
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


@app.get("/models", dependencies=[Depends(require_super_admin)])
async def models() -> dict[str, Any]:
    if settings.deployment_mode == "standalone":
        return {
            "object": "list",
            "data": [
                {
                    "id": model.id,
                    "object": "model",
                    "owned_by": model.provider,
                    "provider_id": model.provider_id,
                    "upstream_model_id": model.upstream_model_id or model.id,
                    "source": model.source,
                    "base_url": model.base_url,
                    "api_key_set": secret_store.has(model.credential_id),
                }
                for model in runtime_config.data.model_catalog.values()
            ],
        }
    try:
        return await client.list_models()
    except httpx.HTTPStatusError as error:
        raise _to_http_exception(error) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        ) from error


@app.post("/chat")
async def chat(
    request: ChatRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> dict[str, Any]:
    effective_scenario_id, scenario, teacher = _resolve_chat_context(request)
    teacher_id = teacher["username"] if teacher else request.teacher_id
    start = time.perf_counter()
    try:
        should_block, strict_topic_related = await _strict_miss_decision(request, scenario)
        if should_block:
            response = _strict_knowledge_miss_response(request, scenario)
        else:
            response = await _chat_completion(
                _build_chat_payload(
                    request,
                    scenario=scenario,
                    strict_topic_related=strict_topic_related,
                )
            )
        await _trace_chat_result(
            route="/chat",
            request=request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            response=response,
            status_code=200,
            latency_ms=now_ms(start),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=_chat_response_content(response),
            status_code=200,
            latency_ms=now_ms(start),
        )
        return response
    except httpx.HTTPStatusError as error:
        latency = now_ms(start)
        business_db.log_request(
            route="/chat",
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=error.response.status_code,
            latency_ms=latency,
            error=str(error),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=str(error),
            status_code=error.response.status_code,
            latency_ms=latency,
        )
        raise _to_http_exception(error) from error
    except httpx.HTTPError as error:
        latency = now_ms(start)
        business_db.log_request(
            route="/chat",
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=502,
            latency_ms=latency,
            error=str(error),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=str(error),
            status_code=502,
            latency_ms=latency,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        ) from error


@app.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> StreamingResponse:
    effective_scenario_id, scenario, teacher = _resolve_chat_context(request)
    teacher_id = teacher["username"] if teacher else request.teacher_id
    should_block, strict_topic_related = await _strict_miss_decision(request, scenario)
    if should_block:
        return StreamingResponse(
            _stream_with_completion_log(
                _strict_knowledge_miss_sse(request, scenario),
                route="/chat/stream",
                request=request,
                scenario=scenario,
                effective_scenario_id=effective_scenario_id,
                teacher_id=teacher_id,
                student=student,
            ),
            media_type="text/event-stream",
        )
    payload = _build_chat_payload(
        request,
        scenario=scenario,
        stream=True,
        strict_topic_related=strict_topic_related,
    )
    return StreamingResponse(
        _stream_with_completion_log(
            _stream_with_errors(payload),
            route="/chat/stream",
            request=request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            student=student,
        ),
        media_type="text/event-stream",
    )


@app.post(
    "/v1/chat/completions",
    dependencies=[Depends(require_platform_key)],
    response_model=None,
)
async def v1_chat_completions(request: V1ChatCompletionRequest):
    chat_request = ChatRequest(
        messages=request.messages,
        scenario_id=request.scenario_id,
        teacher_id=request.teacher_id,
    )
    if request.stream:
        _, scenario, _ = _resolve_chat_context(chat_request)
        should_block, strict_topic_related = await _strict_miss_decision(chat_request, scenario)
        if should_block:
            return StreamingResponse(_strict_knowledge_miss_sse(chat_request, scenario), media_type="text/event-stream")
        payload = _build_chat_payload(
            chat_request,
            scenario=scenario,
            stream=True,
            strict_topic_related=strict_topic_related,
        )
        return StreamingResponse(_stream_with_errors(payload), media_type="text/event-stream")
    return await chat(chat_request, student=StudentIdentity(student_id="", computer_name="", client_ip=""))


def _config_response(current_teacher: dict[str, Any]) -> ConfigResponse:
    sources = knowledge_store.list_sources()
    if not _is_super_admin(current_teacher):
        sources = [source for source in sources if _can_access_source(source.id, current_teacher)]
    scenario = runtime_config.get_teacher_policy(current_teacher["username"])
    return ConfigResponse(
        scenarios={"default": scenario},
        model_catalog=_public_model_catalog(),
        litellm_base_url=settings.litellm_base_url,
        litellm_api_prefix=settings.litellm_api_prefix,
        upstream_provider=settings.upstream_provider,
        upstream_base_url=settings.upstream_base_url,
        knowledge_sources=sources,
    )


@app.get("/config", response_model=ConfigResponse)
async def get_config(current_teacher: dict[str, Any] = Depends(require_admin)) -> ConfigResponse:
    return _config_response(current_teacher)


@app.post("/config/model", response_model=ConfigResponse)
async def switch_default_model(
    request: ModelSwitchRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> ConfigResponse:
    _validate_model_selection(request.model)
    runtime_config.update_teacher_policy(current_teacher["username"], ScenarioUpdateRequest(model=request.model))
    return _config_response(current_teacher)


@app.post("/config/ai", response_model=ConfigResponse)
async def set_ai_enabled(
    request: AIEnableRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> ConfigResponse:
    runtime_config.update_teacher_policy(current_teacher["username"], ScenarioUpdateRequest(ai_enabled=request.enabled))
    return _config_response(current_teacher)


@app.put("/config/scenarios/{scenario_id}", response_model=TeachingScenario)
async def update_scenario(
    scenario_id: str,
    request: ScenarioUpdateRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> TeachingScenario:
    if request.knowledge_source_id:
        knowledge_store.get_source(request.knowledge_source_id)
    if scenario_id == "default":
        return runtime_config.update_teacher_policy(current_teacher["username"], request)
    if not _is_super_admin(current_teacher):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update named scenarios")
    return runtime_config.update_scenario(scenario_id, request)


@app.get("/admin/dashboard", dependencies=[Depends(require_super_admin)])
async def admin_dashboard() -> dict[str, Any]:
    return {
        **business_db.dashboard(),
        "scenario_count": len(runtime_config.data.scenarios),
        "model_catalog_count": len(runtime_config.data.model_catalog),
        "knowledge_source_count": len(knowledge_store.list_sources()),
        "knowledge_file_count": len(knowledge_store.list_files()),
        "teacher_count": len(business_db.list_teachers()),
        "langfuse_enabled": langfuse.enabled,
    }


@app.get("/admin/logs", dependencies=[Depends(require_super_admin)])
async def admin_logs(limit: int = 50) -> list[dict[str, Any]]:
    return business_db.list_logs(limit=min(max(limit, 1), 200))


@app.get("/admin/system/status", dependencies=[Depends(require_super_admin)])
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


@app.get("/admin/system/launcher-logs", dependencies=[Depends(require_super_admin)])
async def admin_launcher_logs(limit: int = 200) -> dict[str, Any]:
    return {"lines": launcher_log_tail(limit)}


@app.get("/admin/system/settings", dependencies=[Depends(require_super_admin)])
async def admin_system_settings() -> dict[str, Any]:
    return read_advanced_settings()


@app.post("/admin/system/open-app-dir", dependencies=[Depends(require_super_admin)])
async def admin_open_app_directory() -> dict[str, str]:
    return await run_in_threadpool(open_app_directory)


@app.put("/admin/system/settings", dependencies=[Depends(require_super_admin)])
async def admin_update_system_settings(request: AdvancedSettingsRequest) -> dict[str, Any]:
    return update_advanced_settings(request.values)


@app.put("/admin/system/platform-key", dependencies=[Depends(require_super_admin)])
async def admin_update_platform_key(request: PlatformKeyRequest) -> dict[str, Any]:
    value = (request.api_key or "").strip()
    if value:
        secret_store.set("system:platform_api_key", value)
    else:
        secret_store.delete("system:platform_api_key")
    return {"status": "saved", "platform_api_key_set": bool(value)}


@app.get("/admin/system/backup", dependencies=[Depends(require_super_admin)])
async def admin_download_backup() -> FileResponse:
    path = await run_in_threadpool(create_backup)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        background=BackgroundTask(remove_backup_file, path),
    )


@app.post("/admin/system/restore", dependencies=[Depends(require_super_admin)])
async def admin_restore_backup(file: UploadFile = File(...)) -> dict[str, Any]:
    if not system_control.supervised:
        raise HTTPException(status_code=409, detail="Restore requires the EduGate supervised launcher")
    await save_restore_archive(file)
    if not system_control.request("restart"):
        raise HTTPException(status_code=409, detail="Could not schedule EduGate restart")
    return {"status": "restore_scheduled", "message": "EduGate will restart and restore the backup"}


@app.post("/admin/system/action", dependencies=[Depends(require_super_admin)])
async def admin_system_action(request: SystemActionRequest) -> dict[str, Any]:
    if not system_control.request(request.action):
        raise HTTPException(status_code=409, detail="System control requires the EduGate supervised launcher")
    return {"status": "scheduled", "action": request.action}


@app.post("/classroom/join", response_model=StudentJoinResponse)
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


@app.get("/admin/classroom", dependencies=[Depends(require_admin)])
async def admin_classroom_access() -> dict[str, Any]:
    active = classroom_access.active()
    return {
        "active": active,
        "class_token": classroom_access.token() if active else "",
        "classroom_id": classroom_access.classroom_id(),
        "recording_enabled": settings.classroom_recording_enabled,
        "record_retention_days": settings.classroom_record_retention_days,
    }


@app.post("/admin/classroom/rotate", dependencies=[Depends(require_admin)])
async def rotate_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    token = classroom_access.rotate()
    student_sessions.revoke_all()
    return {"active": True, "class_token": token, "classroom_id": classroom_access.classroom_id()}


@app.post("/admin/classroom/start", dependencies=[Depends(require_admin)])
async def start_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    token = classroom_access.start()
    student_sessions.revoke_all()
    return {"active": True, "class_token": token, "classroom_id": classroom_access.classroom_id()}


@app.post("/admin/classroom/end", dependencies=[Depends(require_admin)])
async def end_classroom_access() -> dict[str, Any]:
    business_db.end_classroom_instance(classroom_access.classroom_id())
    classroom_access.end()
    student_sessions.revoke_all()
    return {"active": False, "class_token": "", "classroom_id": classroom_access.classroom_id()}


def _record_teacher_scope(current_teacher: dict[str, Any]) -> str | None:
    return None if _is_super_admin(current_teacher) else current_teacher["username"]


@app.get("/teacher/classroom-records")
async def list_classroom_records(
    limit: int = 50,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "recording_enabled": settings.classroom_recording_enabled,
        "retention_days": settings.classroom_record_retention_days,
        "records": business_db.list_classroom_records(
            teacher_username=_record_teacher_scope(current_teacher),
            limit=limit,
        ),
    }


@app.get("/teacher/classroom-records/{run_id}")
async def get_classroom_record(
    run_id: str,
    limit: int = 1000,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    record = business_db.get_classroom_record(
        run_id,
        teacher_username=_record_teacher_scope(current_teacher),
        limit=limit,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Classroom record not found")
    return record


@app.delete("/teacher/classroom-records/{run_id}")
async def delete_classroom_record(
    run_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    deleted = business_db.delete_classroom_record(
        run_id,
        teacher_username=_record_teacher_scope(current_teacher),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Classroom record not found")
    return {"status": "deleted"}


@app.get("/admin/teachers")
async def admin_list_teachers(current_teacher: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    if not _is_super_admin(current_teacher):
        return [current_teacher]
    return business_db.list_teachers()


@app.post("/admin/teachers", dependencies=[Depends(require_super_admin)])
async def admin_upsert_teacher(request: TeacherAccountRequest) -> dict[str, Any]:
    if request.username == settings.admin_username and (request.role != "admin" or not request.is_active):
        raise HTTPException(status_code=400, detail="The primary administrator must remain an active admin")
    try:
        return business_db.upsert_teacher(
            username=request.username,
            password=request.password,
            display_name=request.display_name,
            role=request.role,
            is_active=request.is_active,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.patch("/admin/teachers/{username}/password", dependencies=[Depends(require_super_admin)])
async def admin_update_teacher_password(username: str, request: TeacherPasswordRequest) -> dict[str, Any]:
    try:
        teacher = business_db.update_teacher_password(username, request.password)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not teacher:
        raise HTTPException(status_code=404, detail=f"Unknown teacher: {username}")
    _sync_portable_admin_password(username, request.password)
    sessions.revoke_user(username)
    return teacher


@app.delete("/admin/teachers/{username}", dependencies=[Depends(require_super_admin)])
async def admin_disable_teacher(username: str) -> dict[str, Any]:
    if username == settings.admin_username:
        raise HTTPException(status_code=400, detail="The primary administrator cannot be disabled")
    try:
        teacher = business_db.set_teacher_active(username, False)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not teacher:
        raise HTTPException(status_code=404, detail=f"Unknown teacher: {username}")
    sessions.revoke_user(username)
    return teacher


@app.delete("/admin/teachers/{username}/hard-delete", dependencies=[Depends(require_super_admin)])
async def admin_delete_teacher(username: str) -> dict[str, Any]:
    if username == settings.admin_username:
        raise HTTPException(status_code=400, detail="The environment admin account cannot be deleted")
    try:
        teacher = business_db.delete_teacher(username)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not teacher:
        raise HTTPException(status_code=404, detail=f"Unknown teacher: {username}")
    sessions.revoke_user(username)
    return {"status": "deleted", "teacher": teacher}


@app.get("/admin/models", response_model=list[ModelCatalogPublicItem], dependencies=[Depends(require_super_admin)])
async def admin_models() -> list[ModelCatalogPublicItem]:
    return [_public_model(model) for model in runtime_config.data.model_catalog.values()]


@app.post("/admin/models/discover", dependencies=[Depends(require_super_admin)])
async def admin_discover_models(request: ModelProviderConnectionRequest) -> dict[str, Any]:
    models, _, used_saved_key = await _discover_provider_models(request)
    provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    return {
        "models": models,
        "model_count": len(models),
        "provider_id": provider_id,
        "used_saved_api_key": used_saved_key,
    }


@app.post("/admin/models/batch-import", dependencies=[Depends(require_super_admin)])
async def admin_batch_import_models(request: ModelBatchImportRequest) -> dict[str, Any]:
    discovered, api_key, used_saved_key = await _discover_provider_models(request)
    available_ids = {item["id"] for item in discovered}
    selected_ids = list(dict.fromkeys(model_id.strip() for model_id in request.model_ids if model_id.strip()))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个要导入的模型。")
    unknown_ids = [model_id for model_id in selected_ids if model_id not in available_ids]
    if unknown_ids:
        raise HTTPException(
            status_code=400,
            detail=f"上游模型列表中不存在：{', '.join(unknown_ids[:10])}",
        )
    description = request.description.strip() or "从上游 /models 批量导入"
    provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    model_requests: list[ModelCatalogItem] = []
    for upstream_model_id in selected_ids:
        existing = runtime_config.find_provider_model(provider_id, upstream_model_id)
        model_requests.append(
            ModelCatalogItem(
                id=existing.id if existing else _model_catalog_id(provider_id, upstream_model_id),
                name=(
                    " ".join(request.display_names.get(upstream_model_id, "").split())[:120]
                    or upstream_model_id
                ),
                provider=request.provider.strip(),
                provider_id=provider_id,
                upstream_model_id=upstream_model_id,
                description=description,
                source="openai_compatible",
                base_url=request.base_url.strip(),
                api_key=api_key,
            )
        )
    models = runtime_config.upsert_models(model_requests)
    return {
        "status": "imported",
        "imported_count": len(models),
        "provider_id": provider_id,
        "used_saved_api_key": used_saved_key,
        "models": [_public_model(model).model_dump() for model in models],
    }


@app.post("/admin/models", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_super_admin)])
async def admin_upsert_model(request: ModelCatalogItem) -> ModelCatalogPublicItem:
    return _public_model(runtime_config.upsert_model(request))


@app.patch("/admin/models/{model_id}", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_super_admin)])
async def admin_patch_model(model_id: str, request: ModelCatalogItem) -> ModelCatalogPublicItem:
    current = runtime_config.data.model_catalog.get(model_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"找不到模型：{model_id}")
    return _public_model(
        runtime_config.upsert_model(
            request.model_copy(
                update={
                    "id": model_id,
                    "provider_id": current.provider_id,
                    "upstream_model_id": current.upstream_model_id or current.id,
                }
            )
        )
    )


@app.post("/admin/models/{model_id}/set-default", response_model=ConfigResponse, dependencies=[Depends(require_super_admin)])
async def admin_set_default_model(
    model_id: str,
    current_teacher: dict[str, Any] = Depends(require_super_admin),
) -> ConfigResponse:
    _validate_model_selection(model_id)
    runtime_config.update_teacher_policy(current_teacher["username"], ScenarioUpdateRequest(model=model_id))
    return _config_response(current_teacher)


@app.get("/admin/providers", dependencies=[Depends(require_super_admin)])
async def admin_providers() -> list[dict[str, Any]]:
    if settings.deployment_mode == "standalone":
        direct_models = [
            model for model in runtime_config.data.model_catalog.values()
            if model.source == "openai_compatible"
        ]
        provider_groups: dict[str, list[ModelCatalogItem]] = {}
        for model in direct_models:
            provider_id = model.provider_id or _provider_catalog_id(model.provider, model.base_url)
            provider_groups.setdefault(provider_id, []).append(model)
        providers = []
        for provider_id, models_for_provider in provider_groups.items():
            configured_count = sum(
                1
                for model in models_for_provider
                if model.base_url and secret_store.has(model.credential_id)
            )
            representative = models_for_provider[0]
            providers.append(
                {
                    "id": provider_id,
                    "name": representative.provider,
                    "status": "configured" if configured_count else "needs_configuration",
                    "base_url": representative.base_url,
                    "model_count": len(models_for_provider),
                    "configured_model_count": configured_count,
                }
            )
        providers.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return [
            *providers,
            {
                "id": "langfuse",
                "name": "langfuse",
                "status": "configured" if langfuse.enabled else "not_configured",
                "base_url": settings.langfuse_base_url,
            },
        ]
    try:
        data = await client.list_models()
        available = True
        model_count = len(data.get("data", []))
    except httpx.HTTPError:
        available = False
        model_count = 0
    return [
        {
            "name": "litellm",
            "status": "online" if available else "offline",
            "model_count": model_count,
            "base_url": settings.litellm_base_url,
        },
        {
            "name": "langfuse",
            "status": "configured" if langfuse.enabled else "not_configured",
            "base_url": settings.langfuse_base_url,
        },
    ]


@app.delete("/admin/providers/{provider_id}", dependencies=[Depends(require_super_admin)])
async def admin_delete_provider(
    provider_id: str,
    replacement_model_id: str | None = None,
) -> dict[str, Any]:
    deleted_model_ids, references = runtime_config.delete_provider(
        provider_id,
        replacement_model_id=replacement_model_id,
    )
    if not deleted_model_ids:
        raise HTTPException(status_code=404, detail=f"找不到供应商：{provider_id}")
    return {
        "status": "deleted",
        "provider_id": provider_id,
        "deleted_model_ids": deleted_model_ids,
        "deleted_model_count": len(deleted_model_ids),
        "replacement_model_id": replacement_model_id if references else None,
        "replaced_references": references,
    }


@app.post("/admin/providers/{name}/test", dependencies=[Depends(require_super_admin)])
async def admin_test_provider(name: str) -> dict[str, Any]:
    if settings.deployment_mode == "standalone":
        model = runtime_config.data.model_catalog.get(name)
        if model is None:
            model = next(
                (
                    item
                    for item in runtime_config.data.model_catalog.values()
                    if item.provider_id == name
                ),
                None,
            )
        if name.lower() == "openai_compatible":
            direct_models = [
                item for item in runtime_config.data.model_catalog.values()
                if item.source == "openai_compatible"
            ]
            configured = [
                item.id for item in direct_models
                if item.base_url and secret_store.has(item.credential_id)
            ]
            return {
                "name": name,
                "ok": bool(configured),
                "configured_models": configured,
                "model_count": len(direct_models),
            }
        if model and model.source == "openai_compatible":
            api_key = secret_store.get(model.credential_id)
            if not model.base_url or not api_key:
                return {"name": name, "ok": False, "error": "Base URL or API Key is missing"}
            try:
                result = await client.probe_openai_provider(base_url=model.base_url, api_key=api_key)
                return {"name": name, "base_url": model.base_url, **result}
            except httpx.HTTPStatusError as error:
                return {
                    "name": name,
                    "ok": False,
                    "status_code": error.response.status_code,
                    "error": error.response.text[:500] or error.response.reason_phrase,
                }
            except httpx.TimeoutException:
                return {"name": name, "ok": False, "error": "Provider request timed out"}
            except httpx.HTTPError as error:
                return {"name": name, "ok": False, "error": str(error)}
        if name.lower() == "langfuse":
            return {"name": name, "ok": langfuse.enabled}
        raise HTTPException(status_code=404, detail=f"Unknown provider or model: {name}")
    if name.lower() == "litellm":
        try:
            data = await client.list_models()
            return {"name": name, "ok": True, "model_count": len(data.get("data", []))}
        except httpx.HTTPError as error:
            return {"name": name, "ok": False, "error": str(error)}
    if name.lower() == "langfuse":
        return {"name": name, "ok": langfuse.enabled}
    raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")


@app.get("/admin/sources", response_model=list[KnowledgeSource])
async def admin_sources(current_teacher: dict[str, Any] = Depends(require_admin)) -> list[KnowledgeSource]:
    sources = knowledge_store.list_sources()
    if _is_super_admin(current_teacher):
        return sources
    return [source for source in sources if _can_access_source(source.id, current_teacher)]


@app.post("/admin/sources", response_model=KnowledgeSource)
async def admin_upsert_source(
    request: KnowledgeSourceRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeSource:
    _ensure_source_access(request.id, current_teacher, write=True)
    return knowledge_store.upsert_source(request.id, request.name, request.description)


@app.post("/admin/session/source", response_model=ConfigResponse, dependencies=[Depends(require_super_admin)])
async def admin_set_session_source(
    request: dict[str, str],
    current_teacher: dict[str, Any] = Depends(require_super_admin),
) -> ConfigResponse:
    source_id = request.get("source_id")
    scenario_id = request.get("scenario_id", "default")
    if not source_id:
        raise HTTPException(status_code=400, detail="source_id is required")
    knowledge_store.get_source(source_id)
    runtime_config.update_scenario(scenario_id, ScenarioUpdateRequest(knowledge_source_id=source_id))
    return _config_response(current_teacher)


@app.get("/model-catalog", response_model=list[ModelCatalogPublicItem], dependencies=[Depends(require_admin)])
async def list_model_catalog() -> list[ModelCatalogPublicItem]:
    return [_public_model(model) for model in runtime_config.data.model_catalog.values()]


@app.post("/model-catalog", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_super_admin)])
async def upsert_model_catalog_item(request: ModelCatalogItem) -> ModelCatalogPublicItem:
    return _public_model(runtime_config.upsert_model(request))


@app.delete("/model-catalog/{model_id}", dependencies=[Depends(require_super_admin)])
async def delete_model_catalog_item(
    model_id: str,
    replacement_model_id: str | None = None,
) -> dict[str, Any]:
    if model_id not in runtime_config.data.model_catalog:
        raise HTTPException(status_code=404, detail=f"找不到模型：{model_id}")
    references = runtime_config.delete_model(
        model_id,
        replacement_model_id=replacement_model_id,
    )
    return {
        "status": "deleted",
        "replacement_model_id": replacement_model_id if references else None,
        "replaced_references": references,
    }


@app.get("/knowledge/sources", response_model=list[KnowledgeSource])
async def list_knowledge_sources(current_teacher: dict[str, Any] = Depends(require_admin)) -> list[KnowledgeSource]:
    sources = knowledge_store.list_sources()
    if _is_super_admin(current_teacher):
        return sources
    return [source for source in sources if _can_access_source(source.id, current_teacher)]


@app.post("/knowledge/sources", response_model=KnowledgeSource)
async def upsert_knowledge_source(
    request: KnowledgeSourceRequest,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeSource:
    _ensure_source_access(request.id, current_teacher, write=True)
    return knowledge_store.upsert_source(request.id, request.name, request.description)


@app.delete("/knowledge/sources/{source_id}")
async def delete_knowledge_source(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    _ensure_source_access(source_id, current_teacher, write=True)
    in_use = [
        scenario_id
        for scenario_id, scenario in {
            **runtime_config.data.scenarios,
            **{f"teacher:{key}": value for key, value in runtime_config.data.teacher_policies.items()},
        }.items()
        if scenario.knowledge_source_id == source_id
    ]
    if in_use:
        raise HTTPException(status_code=409, detail=f"Knowledge source is used by: {', '.join(in_use)}")
    knowledge_store.delete_source(source_id)
    return {"status": "deleted"}


@app.post("/knowledge/sources/{source_id}/open-folder")
async def open_knowledge_source_folder(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    _ensure_source_access(source_id, current_teacher, write=True)
    directory = knowledge_store.source_directory(source_id)
    return await run_in_threadpool(
        open_local_directory,
        directory,
        missing_detail="Knowledge source directory does not exist",
    )


@app.post("/knowledge/sources/{source_id}/scan", response_model=KnowledgeScanResult)
async def scan_knowledge_source_folder(
    source_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeScanResult:
    _ensure_source_access(source_id, current_teacher, write=True)
    return await run_in_threadpool(knowledge_store.scan_source, source_id)


@app.get("/knowledge/files", response_model=list[KnowledgeFile])
async def list_knowledge_files(
    source_id: str | None = None,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> list[KnowledgeFile]:
    if source_id:
        _ensure_source_access(source_id, current_teacher)
        return knowledge_store.list_files(source_id)
    files = knowledge_store.list_files()
    if _is_super_admin(current_teacher):
        return files
    prefix = _teacher_source_prefix(current_teacher)
    return [file for file in files if file.source_id.startswith(prefix)]


@app.post("/knowledge/files", response_model=KnowledgeFile)
async def upload_knowledge_file(
    source_id: str = Form(default="general"),
    file: UploadFile = File(...),
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> KnowledgeFile:
    _ensure_source_access(source_id, current_teacher, write=True)
    return await knowledge_store.add_file(source_id, file)


@app.delete("/knowledge/files/{file_id}")
async def delete_knowledge_file(
    file_id: str,
    current_teacher: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    knowledge_file = knowledge_store.get_file(file_id)
    _ensure_source_access(knowledge_file.source_id, current_teacher, write=True)
    knowledge_store.delete_file(file_id)
    return {"status": "deleted"}


def _python_http_error(error: Exception) -> HTTPException:
    if isinstance(error, PythonStudentBusy):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (PythonQueueFull, PythonQueueTimeout)):
        return HTTPException(status_code=429, detail=str(error))
    return HTTPException(status_code=503, detail=str(error))


async def _python_sse_events(job: PythonJob):
    iterator = python_pool.iter_events(job).__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        pending = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait({pending}, timeout=settings.stream_heartbeat_seconds)
            if not done:
                yield b": edugate-python-keep-alive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                break
            event_name = item["event"]
            payload = json.dumps(item["data"], ensure_ascii=False)
            yield f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
            if event_name in {"done", "error"}:
                break
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


async def _submit_python_job(request: PythonRunRequest, student: StudentIdentity) -> PythonJob:
    if not settings.python_runner_enabled:
        raise HTTPException(status_code=503, detail="Python runner is disabled")
    try:
        job = await python_pool.submit(
            request.code,
            student_id=student.student_id,
            runner=run_python_code,
            timeout_seconds=settings.python_runner_timeout_seconds,
            memory_limit_mb=settings.python_runner_memory_mb,
            executable=settings.python_runner_executable,
        )
        _track_python_record(job, request=request, student=student)
        return job
    except (PythonRunnerUnavailable, PythonStudentBusy, PythonQueueFull, PythonQueueTimeout) as error:
        raise _python_http_error(error) from error


def _track_python_record(job: PythonJob, *, request: PythonRunRequest, student: StudentIdentity) -> None:
    if not settings.classroom_recording_enabled or not request.teacher_id:
        return
    teacher = business_db.get_teacher(request.teacher_id)
    if not teacher or not teacher.get("is_active"):
        return

    async def monitor() -> None:
        try:
            pooled = await asyncio.shield(job.future)
        except Exception as error:
            if isinstance(error, PythonRunnerUnavailable):
                status_code = 503
            elif isinstance(error, (PythonQueueFull, PythonQueueTimeout, PythonStudentBusy)):
                status_code = 429
            else:
                status_code = 500
            _record_classroom_turn(
                teacher_id=request.teacher_id,
                student=student,
                kind="python",
                input_content=request.code,
                output_content=str(error),
                status_code=status_code,
                latency_ms=int((time.monotonic() - job.submitted_at) * 1000),
            )
            return
        result = pooled.result
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(result.stderr)
        _record_classroom_turn(
            teacher_id=request.teacher_id,
            student=student,
            kind="python",
            input_content=request.code,
            output_content="\n".join(output_parts),
            status_code=200,
            latency_ms=result.duration_ms + pooled.queue_wait_ms,
            queue_wait_ms=pooled.queue_wait_ms,
            timed_out=result.timed_out,
        )

    task = asyncio.create_task(monitor(), name=f"record-python-{job.id}")
    python_record_tasks.add(task)
    task.add_done_callback(python_record_tasks.discard)


@app.post("/run_python", response_model=PythonRunResponse)
async def run_python(
    request: PythonRunRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> PythonRunResponse:
    job = await _submit_python_job(request, student)
    try:
        pooled = await job.future
    except (PythonRunnerUnavailable, PythonStudentBusy, PythonQueueFull, PythonQueueTimeout) as error:
        raise _python_http_error(error) from error
    result = pooled.result
    return PythonRunResponse(
        job_id=pooled.job_id,
        worker_id=pooled.worker_id,
        queue_wait_ms=pooled.queue_wait_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


@app.post("/run_python/stream")
async def run_python_stream(
    request: PythonRunRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> StreamingResponse:
    job = await _submit_python_job(request, student)
    return StreamingResponse(
        _python_sse_events(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", include_in_schema=False)
async def root_page() -> RedirectResponse:
    return RedirectResponse(url="/admin.html")


frontend_path = Path(settings.frontend_dir)
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
