from __future__ import annotations

import os
from dataclasses import dataclass


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _normalize_prefix(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/")


def _env_default(name: str, standalone_default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:
        return value
    if os.getenv("EDUGATE_MODE", "standalone") == "standalone":
        return standalone_default
    return None


@dataclass
class Settings:
    app_name: str = "EduGate"
    deployment_mode: str = os.getenv("EDUGATE_MODE", "standalone")
    litellm_base_url: str = _normalize_base_url(
        os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")
    )
    litellm_api_prefix: str = _normalize_prefix(os.getenv("LITELLM_API_PREFIX", "/v1"))
    litellm_api_key: str | None = os.getenv("LITELLM_API_KEY")
    default_model: str = os.getenv("DEFAULT_MODEL", "deepseek-chat")
    upstream_provider: str = os.getenv("UPSTREAM_PROVIDER", "OpenAI Compatible")
    upstream_base_url: str = _normalize_base_url(os.getenv("UPSTREAM_BASE_URL", ""))
    upstream_api_key: str | None = os.getenv("UPSTREAM_API_KEY")
    admin_api_key: str | None = _env_default("ADMIN_API_KEY", "local-admin-token")
    runtime_config_path: str = os.getenv("RUNTIME_CONFIG_PATH", "runtime_config.json")
    knowledge_dir: str = os.getenv("KNOWLEDGE_DIR", "knowledge_files")
    knowledge_db_path: str = os.getenv("KNOWLEDGE_DB_PATH", "knowledge.sqlite3")
    knowledge_search_limit: int = int(os.getenv("KNOWLEDGE_SEARCH_LIMIT", "5"))
    database_url: str | None = os.getenv("DATABASE_URL")
    sqlite_db_path: str = os.getenv("EDUGATE_SQLITE_DB_PATH", "edugate.sqlite3")
    platform_api_key: str | None = os.getenv("PLATFORM_API_KEY")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str | None = _env_default("ADMIN_PASSWORD", "edugate")
    langfuse_base_url: str | None = os.getenv("LANGFUSE_BASE_URL")
    langfuse_public_key: str | None = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = os.getenv("LANGFUSE_SECRET_KEY")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
    python_runner_enabled: bool = os.getenv("PYTHON_RUNNER_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    python_runner_timeout_seconds: float = float(os.getenv("PYTHON_RUNNER_TIMEOUT_SECONDS", "3"))
    python_runner_max_code_chars: int = int(os.getenv("PYTHON_RUNNER_MAX_CODE_CHARS", "6000"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
        if origin.strip()
    )


settings = Settings()
