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
from fastapi.staticfiles import StaticFiles

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
from app.runtime_config import RuntimeConfig
from app.schemas import (
    ChatRequest,
    ModelBatchImportRequest,
    ModelCatalogItem,
    ModelCatalogPublicItem,
    ModelProviderConnectionRequest,
    ScenarioUpdateRequest,
    TeachingScenario,
)


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
runtime_config = RuntimeConfig(settings.runtime_config_path, secret_store=secret_store)
langfuse = LangfuseClient()
sessions = SessionStore(settings.session_ttl_seconds)
_persisted_classroom_token = secret_store.get("system:classroom_token")
classroom_access = ClassroomAccess(_persisted_classroom_token)
if not _persisted_classroom_token:
    secret_store.set("system:classroom_token", classroom_access.token())
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
