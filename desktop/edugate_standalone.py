from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from importlib.util import find_spec
from pathlib import Path


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
BACKEND_DIR = RESOURCE_DIR / "backend"
FRONTEND_DIR = RESOURCE_DIR / "frontend"
DATA_DIR = Path(
    os.getenv("EDUGATE_DATA_DIR")
    or Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home()) / "EduGate"
)
REQUIRED_BACKEND_MODULES = ("fastapi", "httpx", "uvicorn", "pydantic", "multipart", "pypdf")
RESTORE_ARCHIVE = DATA_DIR / "pending-restore.zip"
RESTORABLE_FILES = {
    ".env",
    "edugate.sqlite3",
    "knowledge.sqlite3",
    "runtime_config.json",
    "secrets.json",
}
IGNORED_RESTORE_FILES = {"backup-info.json"}


def main() -> int:
    ensure_runtime_environment()
    configure_logging()
    logger = logging.getLogger("edugate.launcher")

    missing = missing_backend_modules()
    if missing:
        logger.error("Missing backend dependencies: %s", ", ".join(missing))
        return 2

    apply_pending_restore(logger)

    from dotenv import load_dotenv

    load_dotenv(DATA_DIR / ".env", override=True)
    os.environ["EDUGATE_DATA_DIR"] = str(DATA_DIR)
    os.environ["EDUGATE_FRONTEND_DIR"] = str(FRONTEND_DIR)

    port = int(os.getenv("EDUGATE_BACKEND_PORT", "8000"))
    admin_url = f"http://127.0.0.1:{port}/admin.html"
    if healthcheck(f"http://127.0.0.1:{port}/health"):
        webbrowser.open(admin_url)
        return 0

    sys.path.insert(0, str(BACKEND_DIR))
    import uvicorn
    from app.main import app
    from app.system_control import system_control

    desired_action = {"value": "shutdown"}
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            log_config=None,
            access_log=False,
        )
    )

    def request_action(action: str) -> None:
        desired_action["value"] = action
        server.should_exit = True

    system_control.bind(request_action)
    threading.Thread(
        target=open_when_ready,
        args=(f"http://127.0.0.1:{port}/health", admin_url),
        daemon=True,
    ).start()
    logger.info("EduGate web console starting on port %s", port)
    try:
        server.run()
    except BaseException:
        logger.exception("EduGate server stopped unexpectedly")
        return 1
    finally:
        system_control.unbind()

    if desired_action["value"] == "restart":
        logger.info("Restart requested from the web console")
        restart_process()
    logger.info("EduGate stopped")
    return 0


def ensure_runtime_environment() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env_path = DATA_DIR / ".env"
    example_path = BACKEND_DIR / ".env.example"
    if not env_path.exists() and example_path.exists():
        shutil.copyfile(example_path, env_path)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        filename=DATA_DIR / "launcher.log",
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def missing_backend_modules() -> list[str]:
    return [module for module in REQUIRED_BACKEND_MODULES if find_spec(module) is None]


def open_when_ready(health_url: str, admin_url: str) -> None:
    for _ in range(80):
        if healthcheck(health_url):
            if os.getenv("EDUGATE_AUTO_OPEN", "true").lower() in {"1", "true", "yes", "on"}:
                webbrowser.open(admin_url)
            return
        time.sleep(0.25)


def healthcheck(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def apply_pending_restore(logger: logging.Logger) -> None:
    if not RESTORE_ARCHIVE.exists():
        return
    staging = DATA_DIR / ".restore-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(RESTORE_ARCHIVE) as archive:
            for member in archive.infolist():
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"Unsafe backup path: {member.filename}")
                if member.is_dir():
                    continue
                allowed = (
                    member.filename in RESTORABLE_FILES
                    or member.filename in IGNORED_RESTORE_FILES
                    or member.filename.startswith("knowledge_files/")
                )
                if not allowed:
                    raise ValueError(f"Unsupported backup entry: {member.filename}")
                archive.extract(member, staging)
        for name in RESTORABLE_FILES:
            source = staging / name
            if source.exists():
                os.replace(source, DATA_DIR / name)
        restored_knowledge = staging / "knowledge_files"
        if restored_knowledge.exists():
            current_knowledge = DATA_DIR / "knowledge_files"
            shutil.rmtree(current_knowledge, ignore_errors=True)
            shutil.move(str(restored_knowledge), str(current_knowledge))
        logger.info("Pending backup restored successfully")
        RESTORE_ARCHIVE.unlink(missing_ok=True)
    except Exception:
        logger.exception("Pending backup restore failed")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def restart_process() -> None:
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        command = [str(pythonw if pythonw.exists() else executable), str(Path(__file__).resolve())]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        command,
        cwd=RESOURCE_DIR,
        env=os.environ.copy(),
        close_fds=True,
        creationflags=creationflags,
    )


if __name__ == "__main__":
    raise SystemExit(main())
