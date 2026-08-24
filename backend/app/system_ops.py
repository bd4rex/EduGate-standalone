from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key
from fastapi import HTTPException, UploadFile, status

from app.config import settings


SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "EDUGATE_BACKEND_PORT": {"type": "int", "default": 8000, "min": 1024, "max": 65535, "restart": True},
    "SESSION_TTL_SECONDS": {"type": "int", "default": 28800, "min": 900, "max": 604800, "restart": True},
    "STUDENT_SESSION_TTL_SECONDS": {"type": "int", "default": 28800, "min": 900, "max": 604800, "restart": True},
    "STUDENT_JOIN_RATE_LIMIT_PER_5_MINUTES": {"type": "int", "default": 256, "min": 1, "max": 10000, "restart": True},
    "CLASSROOM_RATE_LIMIT_PER_MINUTE": {"type": "int", "default": 30, "min": 1, "max": 1000, "restart": True},
    "LOGIN_RATE_LIMIT_PER_5_MINUTES": {"type": "int", "default": 10, "min": 1, "max": 100, "restart": True},
    "ALLOW_LAN_ADMIN": {"type": "bool", "default": False, "restart": True},
    "ADMIN_ALLOWED_IPS": {"type": "str", "default": "", "restart": True},
    "MODEL_MAX_CONCURRENCY": {"type": "int", "default": 16, "min": 1, "max": 32, "restart": True},
    "REQUEST_TIMEOUT_SECONDS": {"type": "float", "default": 60, "min": 5, "max": 600, "restart": True},
    "STREAM_READ_TIMEOUT_SECONDS": {"type": "float", "default": 120, "min": 10, "max": 1800, "restart": True},
    "STREAM_HEARTBEAT_SECONDS": {"type": "float", "default": 15, "min": 2, "max": 120, "restart": True},
    "MAX_UPLOAD_BYTES": {"type": "int", "default": 26214400, "min": 1048576, "max": 1073741824, "restart": True},
    "MAX_PDF_PAGES": {"type": "int", "default": 200, "min": 1, "max": 5000, "restart": True},
    "LOG_MAX_RECORDS": {"type": "int", "default": 5000, "min": 100, "max": 100000, "restart": True},
    "LOG_MESSAGE_PREVIEW": {"type": "bool", "default": False, "restart": True},
    "DB_WRITE_QUEUE_SIZE": {"type": "int", "default": 4096, "min": 128, "max": 20000, "restart": True},
    "DB_WRITE_BATCH_SIZE": {"type": "int", "default": 100, "min": 1, "max": 500, "restart": True},
    "DB_WRITE_FLUSH_INTERVAL_MS": {"type": "int", "default": 20, "min": 1, "max": 1000, "restart": True},
    "DB_CLEANUP_INTERVAL_SECONDS": {"type": "int", "default": 300, "min": 10, "max": 3600, "restart": True},
    "PYTHON_RUNNER_ENABLED": {"type": "bool", "default": False, "restart": True},
    "PYTHON_RUNNER_TIMEOUT_SECONDS": {"type": "float", "default": 3, "min": 0.2, "max": 30, "restart": True},
    "PYTHON_RUNNER_MAX_CODE_CHARS": {"type": "int", "default": 6000, "min": 100, "max": 50000, "restart": True},
    "PYTHON_RUNNER_MEMORY_MB": {"type": "int", "default": 128, "min": 32, "max": 1024, "restart": True},
    "PYTHON_RUNNER_EXECUTABLE": {"type": "str", "default": "", "restart": True},
    "PYTHON_RUNNER_MAX_CONCURRENCY": {"type": "int", "default": 4, "min": 1, "max": 8, "restart": True},
    "PYTHON_RUNNER_MAX_QUEUE": {"type": "int", "default": 64, "min": 1, "max": 256, "restart": True},
    "PYTHON_RUNNER_QUEUE_TIMEOUT_SECONDS": {"type": "float", "default": 30, "min": 1, "max": 300, "restart": True},
    "CLASSROOM_RECORDING_ENABLED": {"type": "bool", "default": True, "restart": True},
    "CLASSROOM_RECORD_RETENTION_DAYS": {"type": "int", "default": 30, "min": 1, "max": 365, "restart": True},
    "CLASSROOM_RECORD_MAX_RECORDS": {"type": "int", "default": 20000, "min": 100, "max": 200000, "restart": True},
    "CLASSROOM_RECORD_MAX_CONTENT_CHARS": {"type": "int", "default": 12000, "min": 500, "max": 50000, "restart": True},
    "CORS_ORIGINS": {"type": "str", "default": "", "restart": True},
}

MAX_RESTORE_BYTES = 1024 * 1024 * 1024
MAX_RESTORE_FILES = 10000


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def system_status(*, supervised: bool, started_at: float, platform_key_set: bool) -> dict[str, Any]:
    data_dir = Path(settings.data_dir)
    disk = shutil.disk_usage(data_dir)
    port = int(os.getenv("EDUGATE_BACKEND_PORT", "8000"))
    ip = local_ip()
    return {
        "status": "running",
        "supervised": supervised,
        "pid": os.getpid(),
        "portable_mode": settings.portable_mode,
        "app_dir": settings.app_dir,
        "uptime_seconds": max(0, int(time.time() - started_at)),
        "data_dir": str(data_dir),
        "config_path": settings.config_path,
        "frontend_dir": settings.frontend_dir,
        "port": port,
        "local_ip": ip,
        "admin_url": f"http://127.0.0.1:{port}/admin.html",
        "lan_base_url": f"http://{ip}:{port}",
        "openai_base_url": f"http://{ip}:{port}/v1",
        "openai_model": "edugate",
        "browser_sdk_url": f"http://{ip}:{port}/assets/edugate-client.js",
        "cors_origins": list(settings.cors_origins),
        "lan_admin_enabled": settings.allow_lan_admin,
        "lan_admin_allowed_ips": list(settings.admin_allowed_ips),
        "disk_free_bytes": disk.free,
        "platform_api_key_set": platform_key_set,
        "python_runner_enabled": settings.python_runner_enabled,
    }


def open_app_directory() -> dict[str, str]:
    return open_local_directory(
        Path(settings.app_dir),
        missing_detail="EduGate application directory does not exist",
    )


def open_local_directory(path: Path, *, missing_detail: str = "Directory does not exist") -> dict[str, str]:
    directory = path.resolve()
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail=missing_detail)
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["/usr/bin/open", str(directory)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            startfile = getattr(os, "startfile", None)
            if os.name != "nt" or not callable(startfile):
                raise HTTPException(
                    status_code=501,
                    detail="Opening a local directory is not supported on this platform",
                )
            startfile(str(directory))
    except (OSError, subprocess.CalledProcessError) as error:
        raise HTTPException(status_code=500, detail="Could not open the directory") from error
    return {"status": "opened", "path": str(directory)}


def read_advanced_settings() -> dict[str, Any]:
    env_path = Path(settings.config_path)
    values = dotenv_values(env_path)
    output = []
    for key, schema in SETTINGS_SCHEMA.items():
        raw = values.get(key) or os.getenv(key)
        value = schema["default"] if raw in {None, ""} else _coerce_value(key, raw, schema)
        output.append({"key": key, "value": value, **schema})
    return {"settings": output, "env_path": str(env_path), "restart_required": True}


def update_advanced_settings(values: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(values) - set(SETTINGS_SCHEMA))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported settings: {', '.join(unknown)}")
    env_path = Path(settings.config_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)
    normalized: dict[str, Any] = {}
    for key, raw in values.items():
        schema = SETTINGS_SCHEMA[key]
        value = _coerce_value(key, raw, schema)
        normalized[key] = value
        serialized = str(value).lower() if isinstance(value, bool) else str(value)
        set_key(str(env_path), key, serialized, quote_mode="never")
    return {"status": "saved", "values": normalized, "restart_required": True}


def launcher_log_tail(limit: int = 200) -> list[str]:
    path = Path(settings.data_dir) / "launcher.log"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-min(max(limit, 1), 1000) :]


def create_backup() -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="edugate-backup-"))
    archive_path = temp_dir / f"EduGate-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    data_dir = Path(settings.data_dir)
    db_snapshots: dict[str, Path] = {}
    for name, source_path in {
        "edugate.sqlite3": Path(settings.sqlite_db_path),
        "knowledge.sqlite3": Path(settings.knowledge_db_path),
    }.items():
        if source_path.exists():
            snapshot = temp_dir / name
            with sqlite3.connect(source_path) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            db_snapshots[name] = snapshot

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        config_path = Path(settings.config_path)
        if config_path.exists():
            archive.write(config_path, "config/edugate.env")
        for name in ("runtime_config.json", "secrets.json"):
            path = data_dir / name
            if path.exists():
                archive.write(path, name)
        for name, path in db_snapshots.items():
            archive.write(path, name)
        knowledge_dir = Path(settings.knowledge_dir)
        if knowledge_dir.exists():
            for path in knowledge_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, Path("knowledge_files") / path.relative_to(knowledge_dir))
        published_pages_dir = Path(settings.published_pages_dir)
        if published_pages_dir.exists():
            for path in published_pages_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, Path("published_pages") / path.relative_to(published_pages_dir))
        archive.writestr(
            "backup-info.json",
            json.dumps({"version": 3, "portable": settings.portable_mode, "created_at": time.time()}, ensure_ascii=False),
        )
    return archive_path


async def save_restore_archive(upload: UploadFile) -> Path:
    target = Path(settings.data_dir) / "pending-restore.zip"
    temp = target.with_suffix(".tmp")
    size = 0
    try:
        with temp.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESTORE_BYTES:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Backup exceeds 1 GB")
                handle.write(chunk)
        _validate_backup(temp)
        os.replace(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def remove_backup_file(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def _validate_backup(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_RESTORE_FILES:
                raise HTTPException(status_code=400, detail="Backup contains too many files")
            total_size = 0
            for member in members:
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise HTTPException(status_code=400, detail=f"Unsafe backup path: {member.filename}")
                allowed = (
                    member.filename in {".env", "config/edugate.env", "edugate.sqlite3", "knowledge.sqlite3", "runtime_config.json", "secrets.json", "backup-info.json"}
                    or member.filename.startswith("knowledge_files/")
                    or member.filename.startswith("published_pages/")
                )
                if not allowed:
                    raise HTTPException(status_code=400, detail=f"Unsupported backup entry: {member.filename}")
                total_size += member.file_size
                if total_size > MAX_RESTORE_BYTES:
                    raise HTTPException(status_code=400, detail="Expanded backup exceeds 1 GB")
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail="Invalid EduGate backup archive") from error


def _coerce_value(key: str, raw: Any, schema: dict[str, Any]) -> Any:
    kind = schema["type"]
    try:
        if kind == "bool":
            if isinstance(raw, bool):
                value = raw
            elif str(raw).lower() in {"1", "true", "yes", "on"}:
                value = True
            elif str(raw).lower() in {"0", "false", "no", "off"}:
                value = False
            else:
                raise ValueError
        elif kind == "int":
            value = int(raw)
        elif kind == "float":
            value = float(raw)
        else:
            value = str(raw).strip()
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"Invalid value for {key}") from error
    if "min" in schema and value < schema["min"]:
        raise HTTPException(status_code=400, detail=f"{key} must be at least {schema['min']}")
    if "max" in schema and value > schema["max"]:
        raise HTTPException(status_code=400, detail=f"{key} must be at most {schema['max']}")
    if key == "ADMIN_ALLOWED_IPS":
        try:
            value = ",".join(
                str(ipaddress.ip_address(item.strip()))
                for item in value.split(",")
                if item.strip()
            )
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="ADMIN_ALLOWED_IPS must contain exact IPv4 or IPv6 addresses separated by commas",
            ) from error
    return value
