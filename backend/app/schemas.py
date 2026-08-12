from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.knowledge import KnowledgeSource


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


class V1ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[ClientMessage] = Field(..., min_length=1)
    model: str | None = None
    stream: bool = False
    scenario_id: str = Field(default="default", min_length=1)
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


class PythonRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=settings.python_runner_max_code_chars)


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
