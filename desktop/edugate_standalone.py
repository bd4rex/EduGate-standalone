from __future__ import annotations

import logging
import os
import shutil
import socket
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


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()


def default_app_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return RESOURCE_DIR
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "EduGate").resolve()
    return Path(sys.executable).resolve().parent


APP_DIR = default_app_dir()
BACKEND_DIR = RESOURCE_DIR / "backend"
FRONTEND_DIR = RESOURCE_DIR / "frontend"
DATA_DIR = Path(os.getenv("EDUGATE_DATA_DIR") or APP_DIR / "data").resolve()
CONFIG_PATH = Path(os.getenv("EDUGATE_CONFIG_PATH") or APP_DIR / "config" / "edugate.env").resolve()
LEGACY_DATA_DIR = (
    Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home()) / "EduGate"
).resolve()
REQUIRED_BACKEND_MODULES = (
    "fastapi",
    "httpx",
    "uvicorn",
    "pydantic",
    "multipart",
    "pypdf",
    "psutil",
)
RESTORE_ARCHIVE = DATA_DIR / "pending-restore.zip"
RESTORABLE_FILES = {
    ".env",
    "config/edugate.env",
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
    restrict_macos_permissions()

    from dotenv import load_dotenv

    load_dotenv(CONFIG_PATH, override=True)
    configure_app_environment()

    configured_port = int(os.getenv("EDUGATE_BACKEND_PORT", "8000"))
    configured_health_url = f"http://127.0.0.1:{configured_port}/health"
    if healthcheck(configured_health_url):
        webbrowser.open(f"http://127.0.0.1:{configured_port}/admin.html")
        return 0
    port = first_available_port(configured_port)
    os.environ["EDUGATE_BACKEND_PORT"] = str(port)
    admin_url = f"http://127.0.0.1:{port}/admin.html"

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


def configure_app_environment() -> None:
    os.environ["EDUGATE_PORTABLE_MODE"] = "true"
    os.environ["EDUGATE_APP_DIR"] = str(APP_DIR)
    os.environ["EDUGATE_DATA_DIR"] = str(DATA_DIR)
    os.environ["EDUGATE_CONFIG_PATH"] = str(CONFIG_PATH)
    os.environ["EDUGATE_FRONTEND_DIR"] = str(FRONTEND_DIR)
    if (
        sys.platform == "darwin"
        and getattr(sys, "frozen", False)
        and not os.environ.get("PYTHON_RUNNER_EXECUTABLE")
    ):
        os.environ["PYTHON_RUNNER_EXECUTABLE"] = str(Path(sys.executable).resolve())


def ensure_runtime_environment() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    migrate_legacy_data()
    example_path = BACKEND_DIR / ".env.example"
    if not CONFIG_PATH.exists() and example_path.exists():
        shutil.copyfile(example_path, CONFIG_PATH)
    restrict_macos_permissions()


def restrict_macos_permissions() -> None:
    if sys.platform != "darwin":
        return
    for directory in {DATA_DIR, CONFIG_PATH.parent}:
        if directory.exists():
            directory.chmod(0o700)
    if CONFIG_PATH.exists():
        CONFIG_PATH.chmod(0o600)


def migrate_legacy_data() -> bool:
    if os.getenv("EDUGATE_SKIP_LEGACY_IMPORT", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if DATA_DIR == LEGACY_DATA_DIR or not LEGACY_DATA_DIR.exists():
        return False
    state_names = ("edugate.sqlite3", "knowledge.sqlite3", "runtime_config.json", "secrets.json")
    if any((DATA_DIR / name).exists() for name in state_names):
        return False
    copied = False
    for name in state_names:
        source = LEGACY_DATA_DIR / name
        if source.exists():
            shutil.copy2(source, DATA_DIR / name)
            copied = True
        for suffix in ("-wal", "-shm"):
            sidecar = LEGACY_DATA_DIR / f"{name}{suffix}"
            if sidecar.exists():
                shutil.copy2(sidecar, DATA_DIR / sidecar.name)
    legacy_knowledge = LEGACY_DATA_DIR / "knowledge_files"
    portable_knowledge = DATA_DIR / "knowledge_files"
    if legacy_knowledge.exists() and not portable_knowledge.exists():
        shutil.copytree(legacy_knowledge, portable_knowledge)
        copied = True
    legacy_published_pages = LEGACY_DATA_DIR / "published_pages"
    portable_published_pages = DATA_DIR / "published_pages"
    if legacy_published_pages.exists() and not portable_published_pages.exists():
        shutil.copytree(legacy_published_pages, portable_published_pages)
        copied = True
    legacy_env = LEGACY_DATA_DIR / ".env"
    if legacy_env.exists() and not CONFIG_PATH.exists():
        shutil.copy2(legacy_env, CONFIG_PATH)
        copied = True
    if copied:
        (DATA_DIR / ".imported-from-localappdata").write_text(
            str(LEGACY_DATA_DIR),
            encoding="utf-8",
        )
    return copied


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
            return response.status == 200 and response.headers.get("X-EduGate-App") == "EduGate"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def first_available_port(preferred: int, *, attempts: int = 20) -> int:
    for port in range(preferred, min(preferred + attempts, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"No available EduGate port found from {preferred}")


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
                    or member.filename.startswith("published_pages/")
                )
                if not allowed:
                    raise ValueError(f"Unsupported backup entry: {member.filename}")
                archive.extract(member, staging)
        for name in RESTORABLE_FILES:
            source = staging / name
            if source.exists():
                target = CONFIG_PATH if name in {".env", "config/edugate.env"} else DATA_DIR / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
        restored_knowledge = staging / "knowledge_files"
        if restored_knowledge.exists():
            current_knowledge = DATA_DIR / "knowledge_files"
            shutil.rmtree(current_knowledge, ignore_errors=True)
            shutil.move(str(restored_knowledge), str(current_knowledge))
        restored_published_pages = staging / "published_pages"
        if restored_published_pages.exists():
            current_published_pages = DATA_DIR / "published_pages"
            shutil.rmtree(current_published_pages, ignore_errors=True)
            shutil.move(str(restored_published_pages), str(current_published_pages))
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
        cwd=APP_DIR,
        env=os.environ.copy(),
        close_fds=True,
        creationflags=creationflags,
    )


def run_embedded_python() -> int:
    """Run the trusted student-runner bootstrap in an isolated app subprocess."""
    arguments = sys.argv[1:]
    if len(arguments) != 4 or arguments[:3] != ["-I", "-S", "-c"]:
        print("Unsupported embedded Python invocation", file=sys.stderr)
        return 2
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    exec(compile(arguments[3], "<edugate-student-runner>", "exec"), namespace, namespace)
    return 0


if __name__ == "__main__":
    if os.getenv("EDUGATE_STUDENT_RUNNER_MODE") == "1":
        raise SystemExit(run_embedded_python())
    raise SystemExit(main())
