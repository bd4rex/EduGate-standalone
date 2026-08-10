from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _normalize_prefix(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = f"/{value}"
    return value.rstrip("/")


def _default_data_dir() -> Path:
    configured = os.getenv("EDUGATE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home())
        return base / "EduGate"
    return Path.home() / ".local" / "share" / "EduGate"


DATA_DIR = _default_data_dir()


def _data_path(env_name: str, default_name: str) -> str:
    value = Path(os.getenv(env_name, default_name)).expanduser()
    return str(value if value.is_absolute() else DATA_DIR / value)


def _as_bool(name: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    app_name: str = "EduGate"
    deployment_mode: str = os.getenv("EDUGATE_MODE", "standalone")
    data_dir: str = str(DATA_DIR)
    frontend_dir: str = os.getenv("EDUGATE_FRONTEND_DIR", str(PROJECT_ROOT / "frontend"))
    litellm_base_url: str = _normalize_base_url(os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000"))
    litellm_api_prefix: str = _normalize_prefix(os.getenv("LITELLM_API_PREFIX", "/v1"))
    litellm_api_key: str | None = os.getenv("LITELLM_API_KEY") or None
    default_model: str = os.getenv("DEFAULT_MODEL", "deepseek-v4-flash")
    upstream_provider: str = os.getenv("UPSTREAM_PROVIDER", "OpenAI Compatible")
    upstream_base_url: str = _normalize_base_url(os.getenv("UPSTREAM_BASE_URL", ""))
    upstream_api_key: str | None = os.getenv("UPSTREAM_API_KEY") or None
    runtime_config_path: str = _data_path("RUNTIME_CONFIG_PATH", "runtime_config.json")
    secret_store_path: str = _data_path("SECRET_STORE_PATH", "secrets.json")
    knowledge_dir: str = _data_path("KNOWLEDGE_DIR", "knowledge_files")
    knowledge_db_path: str = _data_path("KNOWLEDGE_DB_PATH", "knowledge.sqlite3")
    knowledge_search_limit: int = int(os.getenv("KNOWLEDGE_SEARCH_LIMIT", "5"))
    sqlite_db_path: str = _data_path("EDUGATE_SQLITE_DB_PATH", "edugate.sqlite3")
    platform_api_key: str | None = os.getenv("PLATFORM_API_KEY") or None
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
    student_session_ttl_seconds: int = int(os.getenv("STUDENT_SESSION_TTL_SECONDS", "28800"))
    student_join_rate_limit: int = int(os.getenv("STUDENT_JOIN_RATE_LIMIT_PER_5_MINUTES", "256"))
    classroom_rate_limit: int = int(os.getenv("CLASSROOM_RATE_LIMIT_PER_MINUTE", "30"))
    login_rate_limit: int = int(os.getenv("LOGIN_RATE_LIMIT_PER_5_MINUTES", "10"))
    model_max_concurrency: int = int(os.getenv("MODEL_MAX_CONCURRENCY", "4"))
    langfuse_base_url: str | None = os.getenv("LANGFUSE_BASE_URL") or None
    langfuse_public_key: str | None = os.getenv("LANGFUSE_PUBLIC_KEY") or None
    langfuse_secret_key: str | None = os.getenv("LANGFUSE_SECRET_KEY") or None
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
    stream_read_timeout_seconds: float = float(os.getenv("STREAM_READ_TIMEOUT_SECONDS", "120"))
    stream_heartbeat_seconds: float = float(os.getenv("STREAM_HEARTBEAT_SECONDS", "15"))
    python_runner_enabled: bool = _as_bool("PYTHON_RUNNER_ENABLED", False)
    python_runner_timeout_seconds: float = float(os.getenv("PYTHON_RUNNER_TIMEOUT_SECONDS", "3"))
    python_runner_max_code_chars: int = int(os.getenv("PYTHON_RUNNER_MAX_CODE_CHARS", "6000"))
    python_runner_memory_mb: int = int(os.getenv("PYTHON_RUNNER_MEMORY_MB", "128"))
    python_runner_executable: str | None = os.getenv("PYTHON_RUNNER_EXECUTABLE") or None
    python_runner_max_concurrency: int = int(os.getenv("PYTHON_RUNNER_MAX_CONCURRENCY", "4"))
    python_runner_max_queue: int = int(os.getenv("PYTHON_RUNNER_MAX_QUEUE", "64"))
    python_runner_queue_timeout_seconds: float = float(os.getenv("PYTHON_RUNNER_QUEUE_TIMEOUT_SECONDS", "30"))
    classroom_recording_enabled: bool = _as_bool("CLASSROOM_RECORDING_ENABLED", True)
    classroom_record_retention_days: int = int(os.getenv("CLASSROOM_RECORD_RETENTION_DAYS", "30"))
    classroom_record_max_records: int = int(os.getenv("CLASSROOM_RECORD_MAX_RECORDS", "20000"))
    classroom_record_max_content_chars: int = int(os.getenv("CLASSROOM_RECORD_MAX_CONTENT_CHARS", "12000"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    max_pdf_pages: int = int(os.getenv("MAX_PDF_PAGES", "200"))
    log_message_preview: bool = _as_bool("LOG_MESSAGE_PREVIEW", False)
    log_max_records: int = int(os.getenv("LOG_MAX_RECORDS", "5000"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    )


settings = Settings()
