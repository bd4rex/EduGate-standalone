"""Portable-directory identity, locking and offline program-package validation.

This module deliberately has no application/config imports: the launcher uses it
before importing anything that can open a database or read credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath

PROGRAM_NAMES = frozenset({"EduGate-Standalone.exe", "_internal", "README.txt", "package.json"})
DATA_PATHS = {
    "RUNTIME_CONFIG_PATH": "runtime_config.json", "SECRET_STORE_PATH": "secrets.json",
    "EDUGATE_SQLITE_DB_PATH": "edugate.sqlite3", "KNOWLEDGE_DB_PATH": "knowledge.sqlite3",
    "KNOWLEDGE_DIR": "knowledge_files", "PUBLISHED_PAGES_DIR": "published_pages",
    "KNOWLEDGE_MODEL_DIR": "knowledge_models", "KNOWLEDGE_INDEX_DIR": "knowledge_index",
}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def instance_id(data_dir: Path) -> str:
    path = data_dir / "instance.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))["id"]
        return str(uuid.UUID(value))
    value = str(uuid.uuid4())
    write_json(path, {"id": value, "version": 1})
    return value


class DirectoryLock:
    """OS-owned byte lock; a crash releases it without trusting stale PID files."""
    def __init__(self, data_dir: Path, filename: str = ".edugate.lock"):
        self.path = data_dir / filename
        self.handle = None

    def acquire(self) -> bool:
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def contained_directory(root: Path, path: Path) -> Path:
    """Do not let a junction/symlink redirect a portable folder to another owner."""
    root = root.resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"便携目录不能指向程序文件夹之外：{path.name}")
    current = path
    while current != root:
        if current.exists() and (current.is_symlink() or getattr(current, "is_junction", lambda: False)()):
            raise ValueError(f"便携目录不能使用目录链接：{path.name}")
        current = current.parent
    return path


def portable_environment(values: dict, *, app_dir: Path, data_dir: Path, config_path: Path, frontend_dir: Path) -> dict[str, str]:
    """Use this folder's settings, never credentials or paths left by another app."""
    prefixes = ("EDUGATE_", "UPSTREAM_", "LITELLM_", "LANGFUSE_", "KNOWLEDGE_", "PYTHON_RUNNER_",
                "DB_", "CLASSROOM_", "MODEL_", "ADMIN_", "STUDENT_", "SESSION_", "LOGIN_", "LOG_", "SECRET_STORE_")
    names = {"PORTABLE_AUTO_LOGIN", "ALLOW_LAN_ADMIN", "CORS_ORIGINS", "RUNTIME_CONFIG_PATH",
             "DEFAULT_MODEL", "PLATFORM_API_KEY", "MAX_UPLOAD_BYTES", "MAX_PDF_PAGES", "LAN_ADVERTISE_IP",
             "REQUEST_TIMEOUT_SECONDS", "STREAM_READ_TIMEOUT_SECONDS", "STREAM_HEARTBEAT_SECONDS", "PUBLISHED_PAGES_DIR"}
    result = {key: value for key, value in os.environ.items() if key not in names and not key.startswith(prefixes)}
    result.update({key: str(value) for key, value in values.items() if value is not None})
    result.update({key: str(data_dir / filename) for key, filename in DATA_PATHS.items()})
    result.update(EDUGATE_MODE="standalone", EDUGATE_PORTABLE_MODE="true", SECRET_STORE_MODE="portable",
                  EDUGATE_APP_DIR=str(app_dir), EDUGATE_DATA_DIR=str(data_dir),
                  EDUGATE_CONFIG_PATH=str(config_path), EDUGATE_FRONTEND_DIR=str(frontend_dir))
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_manifest(directory: Path, version: str) -> dict:
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative == "package.json" or path.is_dir():
            continue
        if relative.split("/")[0] not in PROGRAM_NAMES or path.is_symlink():
            raise ValueError(f"发布包包含非程序文件：{relative}")
        files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {"product": "EduGate-Standalone", "format": 1, "platform": "windows-amd64", "version": version, "files": files}


def stage_upgrade(archive_path: Path, staging: Path) -> dict:
    """Accept only a complete, checksummed release. Never accept config or data."""
    with zipfile.ZipFile(archive_path) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > 30000 or sum(item.file_size for item in infos) > 4 * 1024**3:
            raise ValueError("升级包过大")
        if shutil.disk_usage(staging.parent).free < sum(item.file_size for item in infos) + 64 * 1024**2:
            raise ValueError("U 盘空间不足以解压升级包")
        names = [item.filename for item in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("升级包包含重名文件")
        if "package.json" not in names:
            raise ValueError("请选择 EduGate 程序升级 ZIP，不是课堂数据备份")
        if archive.getinfo("package.json").file_size > 10 * 1024**2:
            raise ValueError("升级清单过大")
        manifest = json.loads(archive.read("package.json"))
        if manifest.get("product") != "EduGate-Standalone" or manifest.get("format") != 1 or manifest.get("platform") != "windows-amd64":
            raise ValueError("升级包产品或平台不匹配")
        expected = manifest.get("files", {})
        if not isinstance(expected, dict) or set(expected) != set(names) - {"package.json"}:
            raise ValueError("升级包文件与清单不一致")
        if "EduGate-Standalone.exe" not in expected or not any(n.startswith("_internal/") for n in expected):
            raise ValueError("升级包缺少 EXE 或运行环境")
        for item in infos:
            path = PurePosixPath(item.filename)
            mode = (item.external_attr >> 16) & 0xFFFF
            if (path.is_absolute() or any(part in {"", ".", ".."} or ":" in part or part.endswith((".", " ")) for part in path.parts)
                    or "\\" in item.filename or path.parts[0] not in PROGRAM_NAMES or stat.S_ISLNK(mode)
                    or (path.parts[0] != "_internal" and len(path.parts) != 1)):
                raise ValueError(f"升级包路径不安全：{item.filename}")
            if item.filename != "package.json":
                entry = expected[item.filename]
                if not isinstance(entry, dict) or entry.get("size") != item.file_size or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                    raise ValueError("升级包校验信息不正确")
        staging.mkdir(parents=True, exist_ok=False)
        for item in infos:
            target = staging.joinpath(*PurePosixPath(item.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if item.filename != "package.json" and digest.hexdigest() != expected[item.filename]["sha256"]:
                raise ValueError(f"升级包文件校验失败：{item.filename}")
    return manifest
