from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import BusinessDB, latest_user_preview, now_ms
from app.knowledge import KnowledgeFile, KnowledgeSource, KnowledgeStore
from app.litellm_client import LiteLLMClient
from app.observability import LangfuseClient
from app.python_runner import PythonRunnerUnavailable, run_python_code
from app.secret_store import SecretStore
from app.security import ClassroomAccess, SessionStore, SlidingWindowRateLimiter, StudentSessionStore
from app.system_control import system_control
from app.system_ops import (
    create_backup,
    launcher_log_tail,
    read_advanced_settings,
    remove_backup_file,
    save_restore_archive,
    system_status,
    update_advanced_settings,
)
from starlette.background import BackgroundTask


client = LiteLLMClient()
knowledge_store = KnowledgeStore(
    settings.knowledge_db_path,
    settings.knowledge_dir,
    max_upload_bytes=settings.max_upload_bytes,
    max_pdf_pages=settings.max_pdf_pages,
)
business_db = BusinessDB(settings.sqlite_db_path, log_max_records=settings.log_max_records)
secret_store = SecretStore(settings.secret_store_path)
langfuse = LangfuseClient()
sessions = SessionStore(settings.session_ttl_seconds)
classroom_access = ClassroomAccess()
student_sessions = StudentSessionStore(settings.student_session_ttl_seconds)
rate_limiter = SlidingWindowRateLimiter()
model_semaphore = asyncio.Semaphore(settings.model_max_concurrency)
python_semaphore = asyncio.Semaphore(1)

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
    {"name": "Admin", "description": "Admin management APIs."},
    {"name": "Model Catalog", "description": "Model catalog APIs."},
    {"name": "Knowledge", "description": "Knowledge base APIs."},
    {"name": "Other", "description": "Classroom utility APIs."},
]

API_DOCS = {
    ("GET", "/health"): ("Health", "Check whether EduGate is online."),
    ("POST", "/auth/login"): ("Login", "Login with teacher username and password."),
    ("GET", "/models"): ("List upstream models", "Read models from the configured upstream provider when available."),
    ("POST", "/chat"): ("Student chat", "Without teacher_id this uses open default; with teacher_id it uses that teacher policy."),
    ("POST", "/chat/stream"): ("Student stream chat", "POST + text/event-stream chat API."),
    ("POST", "/classroom/join"): ("Join classroom", "Exchange the classroom link token for an anonymous student session."),
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
    ("POST", "/admin/models"): ("Upsert model", "Create or update model catalog item."),
    ("PATCH", "/admin/models/{model_id}"): ("Patch model", "Update model catalog item."),
    ("POST", "/admin/models/{model_id}/set-default"): ("Set current model", "Set current admin teacher policy model."),
    ("GET", "/admin/providers"): ("Providers", "Provider status."),
    ("POST", "/admin/providers/{name}/test"): ("Test provider", "Test provider connectivity."),
    ("GET", "/admin/sources"): ("Sources", "List knowledge sources."),
    ("POST", "/admin/sources"): ("Upsert source", "Create or update knowledge source."),
    ("POST", "/admin/session/source"): ("Set source", "Legacy compatibility API."),
    ("GET", "/model-catalog"): ("Model catalog", "Read model catalog."),
    ("POST", "/model-catalog"): ("Upsert model catalog", "Admin model catalog upsert."),
    ("DELETE", "/model-catalog/{model_id}"): ("Delete model catalog", "Delete model catalog item."),
    ("GET", "/knowledge/sources"): ("Knowledge sources", "List knowledge sources."),
    ("POST", "/knowledge/sources"): ("Upsert knowledge source", "Create or update knowledge source."),
    ("DELETE", "/knowledge/sources/{source_id}"): ("Delete knowledge source", "Delete source and indexes."),
    ("GET", "/knowledge/files"): ("Knowledge files", "List uploaded files."),
    ("POST", "/knowledge/files"): ("Upload knowledge file", "Upload txt, md, pdf and other files."),
    ("DELETE", "/knowledge/files/{file_id}"): ("Delete knowledge file", "Delete file and chunks."),
    ("POST", "/run_python"): ("Run Python", "Run small classroom Python examples."),
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
    if path.startswith("/admin/"):
        return "Admin"
    if path.startswith("/model-catalog"):
        return "Model Catalog"
    if path.startswith("/knowledge/"):
        return "Knowledge"
    if path == "/run_python":
        return "Other"
    return "System"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    business_db.init()
    try:
        yield
    finally:
        await client.close()


app = FastAPI(
    title="EduGate API",
    summary="EduGate API",
    description=(
        "EduGate sits between student pages or third-party clients and the teacher-selected upstream model provider. "
        "Requests without teacher_id use open default. Requests with teacher_id use that teacher policy."
    ),
    version="1.3.0",
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


class ModelCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    provider: str = "OpenAI Compatible"
    description: str = ""
    source: Literal["litellm", "openai_compatible"] = "openai_compatible"
    base_url: str | None = Field(default=None, min_length=1)
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
    api_key_set: bool = False


class RuntimeConfigData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: dict[str, TeachingScenario] = Field(
        default_factory=lambda: {"default": TeachingScenario()}
    )
    teacher_policies: dict[str, TeachingScenario] = Field(default_factory=dict)
    model_catalog: dict[str, ModelCatalogItem] = Field(default_factory=dict)


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


class PythonRunResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int


class StudentJoinResponse(BaseModel):
    student_token: str
    student_session_id: str
    expires_in: int


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
        if self._migrate_open_default():
            self.save()
        self._ensure_standalone_default_model()

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
        if settings.default_model in self.data.model_catalog:
            return
        credential_id = f"model:{settings.default_model}"
        if settings.upstream_api_key:
            secret_store.set(credential_id, settings.upstream_api_key)
        self.data.model_catalog[settings.default_model] = ModelCatalogItem(
            id=settings.default_model,
            name=settings.default_model,
            provider=settings.upstream_provider,
            description="Local classroom default upstream model. Edit base_url and api_key before live use.",
            source="openai_compatible",
            base_url=settings.upstream_base_url or None,
            credential_id=credential_id,
        )
        self.save()

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
        with self._lock:
            current = self.data.model_catalog.get(request.id)
            credential_id = (current.credential_id if current else None) or f"model:{request.id}"
            if request.api_key:
                secret_store.set(credential_id, request.api_key)
            request = request.model_copy(update={"credential_id": credential_id, "api_key": None})
            self.data.model_catalog[request.id] = request
            self.save()
            return request

    def delete_model(self, model_id: str) -> None:
        with self._lock:
            model = self.data.model_catalog.pop(model_id, None)
            if model:
                self.save()
                secret_store.delete(model.credential_id)


runtime_config = RuntimeConfig(settings.runtime_config_path)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address(_client_ip(request)).is_loopback
    except ValueError:
        return False


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
    class_token: str | None = None,
) -> str | None:
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
        return record.student_id
    token = x_class_token or class_token
    if not classroom_access.matches(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid classroom token")
    key = f"chat:ip:{_client_ip(request)}"
    if not rate_limiter.allow(key, limit=settings.classroom_rate_limit, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Classroom request limit exceeded")
    return None


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
        api_key_set=secret_store.has(model.credential_id),
    )


def _public_model_catalog() -> dict[str, ModelCatalogPublicItem]:
    return {
        model_id: _public_model(model)
        for model_id, model in runtime_config.data.model_catalog.items()
    }


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
                payload=payload,
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
                payload=payload,
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
):
    start = time.perf_counter()
    stream_chunks = 0
    stream_bytes = 0
    stream_done = False
    status_code = 200
    finish_reason = "ended_without_done"
    error_text: str | None = None

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

    try:
        async for chunk in _iterate_stream_bytes(source):
            stream_chunks += 1
            stream_bytes += len(chunk)
            if b"data: [DONE]" in chunk or b"data:[DONE]" in chunk:
                stream_done = True
                finish_reason = "done"
            elif b"event: error" in chunk:
                status_code = 502
                finish_reason = "upstream_error"
                error_text = chunk.decode("utf-8", errors="replace")[:1000]
                try:
                    data_part = error_text.split("data:", 1)[1].strip()
                    status_code = int(json.loads(data_part).get("status_code", status_code))
                except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                    pass
            yield chunk
    except asyncio.CancelledError:
        status_code = 499
        finish_reason = "client_disconnected"
        error_text = "Streaming response was cancelled before EduGate observed [DONE]."
        write_log()
        raise
    except Exception as error:
        status_code = 500
        finish_reason = "server_exception"
        error_text = f"{type(error).__name__}: {error!s}"
        write_log()
        raise
    else:
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


@app.post("/chat", dependencies=[Depends(require_classroom_access)])
async def chat(request: ChatRequest) -> dict[str, Any]:
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
        raise HTTPException(
            status_code=502,
            detail=f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        ) from error


@app.post("/chat/stream", dependencies=[Depends(require_classroom_access)])
async def chat_stream(request: ChatRequest) -> StreamingResponse:
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
    return await chat(chat_request)


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
    return system_status(
        supervised=system_control.supervised,
        started_at=system_control.started_at,
        platform_key_set=secret_store.has("system:platform_api_key") or bool(settings.platform_api_key),
    )


@app.get("/admin/system/launcher-logs", dependencies=[Depends(require_super_admin)])
async def admin_launcher_logs(limit: int = 200) -> dict[str, Any]:
    return {"lines": launcher_log_tail(limit)}


@app.get("/admin/system/settings", dependencies=[Depends(require_super_admin)])
async def admin_system_settings() -> dict[str, Any]:
    return read_advanced_settings()


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
    x_class_token: str | None = Header(default=None, alias="X-Class-Token"),
    x_student_token: str | None = Header(default=None, alias="X-Student-Token"),
    class_token: str | None = None,
) -> StudentJoinResponse:
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
    token, record = student_sessions.issue(
        current_classroom_token,
        existing_token=x_student_token,
    )
    return StudentJoinResponse(
        student_token=token,
        student_session_id=record.student_id,
        expires_in=max(0, int(record.expires_at - time.time())),
    )


@app.get("/admin/classroom", dependencies=[Depends(require_admin)])
async def admin_classroom_access() -> dict[str, str]:
    return {"class_token": classroom_access.token()}


@app.post("/admin/classroom/rotate", dependencies=[Depends(require_admin)])
async def rotate_classroom_access() -> dict[str, str]:
    token = classroom_access.rotate()
    student_sessions.revoke_all()
    return {"class_token": token}


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


@app.post("/admin/models", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_super_admin)])
async def admin_upsert_model(request: ModelCatalogItem) -> ModelCatalogPublicItem:
    return _public_model(runtime_config.upsert_model(request))


@app.patch("/admin/models/{model_id}", response_model=ModelCatalogPublicItem, dependencies=[Depends(require_super_admin)])
async def admin_patch_model(model_id: str, request: ModelCatalogItem) -> ModelCatalogPublicItem:
    return _public_model(runtime_config.upsert_model(request.model_copy(update={"id": model_id})))


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
        configured_count = sum(
            1 for model in direct_models if model.base_url and secret_store.has(model.credential_id)
        )
        return [
            {
                "name": "openai_compatible",
                "status": "configured" if configured_count else "needs_configuration",
                "model_count": len(direct_models),
                "configured_model_count": configured_count,
            },
            {
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


@app.post("/admin/providers/{name}/test", dependencies=[Depends(require_super_admin)])
async def admin_test_provider(name: str) -> dict[str, Any]:
    if settings.deployment_mode == "standalone":
        model = runtime_config.data.model_catalog.get(name)
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
async def delete_model_catalog_item(model_id: str) -> dict[str, str]:
    in_use = [
        scenario_id
        for scenario_id, scenario in {
            **runtime_config.data.scenarios,
            **{f"teacher:{key}": value for key, value in runtime_config.data.teacher_policies.items()},
        }.items()
        if scenario.model == model_id
    ]
    if in_use:
        raise HTTPException(status_code=409, detail=f"Model is used by: {', '.join(in_use)}")
    runtime_config.delete_model(model_id)
    return {"status": "deleted"}


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


@app.post(
    "/run_python",
    response_model=PythonRunResponse,
    dependencies=[Depends(require_classroom_access)],
)
async def run_python(request: PythonRunRequest) -> PythonRunResponse:
    if not settings.python_runner_enabled:
        raise HTTPException(status_code=503, detail="Python runner is disabled")
    try:
        async with python_semaphore:
            result = await run_in_threadpool(
                run_python_code,
                request.code,
                timeout_seconds=settings.python_runner_timeout_seconds,
                memory_limit_mb=settings.python_runner_memory_mb,
                executable=settings.python_runner_executable,
            )
    except PythonRunnerUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return PythonRunResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


@app.get("/", include_in_schema=False)
async def root_page() -> RedirectResponse:
    return RedirectResponse(url="/admin.html")


frontend_path = Path(settings.frontend_dir)
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
