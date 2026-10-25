from __future__ import annotations

import logging
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import uuid
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
DATA_DIR = Path((None if getattr(sys, "frozen", False) else os.getenv("EDUGATE_DATA_DIR")) or APP_DIR / "data").resolve()
CONFIG_PATH = Path((None if getattr(sys, "frozen", False) else os.getenv("EDUGATE_CONFIG_PATH")) or APP_DIR / "config" / "edugate.env").resolve()
sys.path.insert(0, str(BACKEND_DIR))
from app.portable import DATA_PATHS, DirectoryLock, contained_directory, instance_id, portable_environment, sha256_file, write_json
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


def main(argv: list[str] | None = None) -> int:
    global APP_DIR, DATA_DIR, CONFIG_PATH, RESTORE_ARCHIVE
    parser = argparse.ArgumentParser(description="EduGate portable classroom")
    parser.add_argument("--portable-dir", type=Path, help="Explicit isolated profile directory (testing/advanced use)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-exit-notice", action="store_true")
    args = parser.parse_args(argv)
    if args.portable_dir:
        APP_DIR = args.portable_dir.resolve()
        DATA_DIR = APP_DIR / "data"
        CONFIG_PATH = APP_DIR / "config" / "edugate.env"
        RESTORE_ARCHIVE = DATA_DIR / "pending-restore.zip"
    if getattr(sys, "frozen", False) or args.portable_dir:
        contained_directory(APP_DIR, DATA_DIR)
        contained_directory(APP_DIR, CONFIG_PATH.parent)
        contained_directory(APP_DIR, CONFIG_PATH)
        for filename in (*DATA_PATHS.values(), "instance.json", "launcher-state.json"):
            contained_directory(APP_DIR, DATA_DIR / filename)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = DirectoryLock(DATA_DIR)
    if not lock.acquire():
        return reopen_own_instance(not args.no_browser)
    state_path = DATA_DIR / "launcher-state.json"
    run_id = uuid.uuid4().hex
    action = {"value": ""}
    result = 1
    safe = False
    identity = ""
    port = 0
    listener = None
    try:
        identity = instance_id(DATA_DIR)
        write_json(state_path, {"instance_id": identity, "run_id": run_id, "pid": os.getpid(), "status": "starting"})
        ensure_runtime_environment()
        configure_logging()
        logger = logging.getLogger("edugate.launcher")
        if missing_backend_modules():
            raise RuntimeError("运行环境不完整，请重新解压完整便携包（不要只复制 EXE）。")
        apply_pending_restore(logger)
        from dotenv import dotenv_values
        environment = portable_environment(dotenv_values(CONFIG_PATH, interpolate=False), app_dir=APP_DIR,
            data_dir=DATA_DIR, config_path=CONFIG_PATH, frontend_dir=FRONTEND_DIR)
        environment.update(EDUGATE_INSTANCE_ID=identity, EDUGATE_RUN_ID=run_id,
            EDUGATE_AUTO_OPEN="false" if args.no_browser else "true",
            EDUGATE_EXIT_NOTICE="false" if args.no_exit_notice else "true",
            EDUGATE_FROZEN="true" if getattr(sys, "frozen", False) else "false")
        os.environ.clear()
        os.environ.update(environment)
        # An explicit IP from yesterday's classroom must not advertise another host.
        from app.network import local_interfaces
        selected = os.getenv("LAN_ADVERTISE_IP", "")
        if selected and selected not in {item["ip"] for item in local_interfaces()}:
            os.environ["LAN_ADVERTISE_IP"] = ""
            os.environ["EDUGATE_NETWORK_NOTICE"] = "原学生入口 IP 不属于这台电脑，已自动选择当前网卡。"
        listener = reserve_listener(int(os.getenv("EDUGATE_BACKEND_PORT", "8000")))
        port = listener.getsockname()[1]
        os.environ["EDUGATE_BACKEND_PORT"] = str(port)
        write_json(state_path, {"instance_id": identity, "run_id": run_id, "pid": os.getpid(), "port": port, "status": "starting"})
        import uvicorn
        from app.main import app
        from app import state
        from app.system_control import system_control
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False))

        def prepare(action_name: str) -> None:
            if os.name == "nt" and action_name in {"shutdown", "upgrade"}:
                start_maintenance_helper(action_name, run_id, args)

        def request_action(action_name: str) -> None:
            action["value"] = action_name
            try:
                state.business_db.end_classroom_instance(state.classroom_access.classroom_id())
                write_json(state_path, {"instance_id": identity, "run_id": run_id, "pid": os.getpid(), "port": port, "status": "stopping"})
            except Exception as error:
                system_control.finish_shutdown(str(error))
                logger.exception("Could not finish classroom metadata")
            finally:
                state.classroom_access.end()
                state.student_sessions.revoke_all()
                server.should_exit = True

        system_control.bind(request_action, prepare)
        threading.Thread(target=open_when_ready,
            args=(f"http://127.0.0.1:{port}/health", f"http://127.0.0.1:{port}/admin.html", identity, run_id), daemon=True).start()
        logger.info("Portable classroom starting on port %s", port)
        try:
            server.run(sockets=[listener])
        finally:
            safe = system_control.shutdown_complete
            system_control.unbind()
        if not safe:
            raise RuntimeError(system_control.shutdown_error or "服务未能完成启动或保存，不能确认安全退出。")
        result = 0
    except BaseException as error:
        logging.getLogger("edugate.launcher").exception("Portable classroom failed")
        if not args.no_exit_notice and not action["value"]:
            show_notice("EduGate 未能启动", str(error) + "\n请查看当前 data 文件夹内的 launcher.log。", error=True)
    finally:
        if listener is not None:
            listener.close()
        write_json(state_path, {"instance_id": identity, "run_id": run_id, "pid": os.getpid(), "port": port,
            "status": "stopped" if safe else "failed", "saved": safe, "action": action["value"]})
        logging.shutdown()
        lock.release()
    if result == 0 and action["value"] == "restart":
        restart_process()
    return result


def show_notice(title: str, message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 if error else 0x40)


def reopen_own_instance(open_browser: bool) -> int:
    for _ in range(60):
        try:
            info = json.loads((DATA_DIR / "launcher-state.json").read_text(encoding="utf-8"))
            if info.get("status") in {"stopping", "upgrading", "failed"}:
                break
            port = int(info["port"])
            if healthcheck(f"http://127.0.0.1:{port}/health", info["instance_id"], info["run_id"]):
                if open_browser:
                    webbrowser.open(f"http://127.0.0.1:{port}/admin.html")
                return 0
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.25)
    if open_browser:
        show_notice("EduGate 正在使用", "这份便携课堂正在启动、保存或升级，请稍候。不会连接其他老师的课堂。")
    return 3


def start_maintenance_helper(action: str, run_id: str, args: argparse.Namespace) -> None:
    """Use the OS PowerShell from a local temp file; it holds no USB binaries."""
    helper = RESOURCE_DIR / "desktop" / "portable_maintenance.ps1"
    job_dir = Path(tempfile.mkdtemp(prefix="edugate-exit-"))
    job = {"root": str(APP_DIR), "data": str(DATA_DIR), "pid": os.getpid(), "run_id": run_id,
        "action": action, "notice": not args.no_exit_notice, "ready": str(job_dir / "ready"),
        "result": str(job_dir / "result.json"), "no_browser": args.no_browser,
        "executable": str(Path(sys.executable).resolve())}
    encoded_job = base64.b64encode(json.dumps(job, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = "$job = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_job + "')) | ConvertFrom-Json\n" + helper.read_text(encoding="utf-8-sig")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = subprocess.Popen([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=job_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    for _ in range(100):
        if (job_dir / "ready").exists():
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError("未能启动退出确认程序；课堂保持运行，请联系管理员检查 PowerShell 策略。")


def configure_app_environment() -> None:
    os.environ["EDUGATE_PORTABLE_MODE"] = "true"
    os.environ["EDUGATE_APP_DIR"] = str(APP_DIR)
    os.environ["EDUGATE_DATA_DIR"] = str(DATA_DIR)
    os.environ["EDUGATE_CONFIG_PATH"] = str(CONFIG_PATH)
    os.environ["EDUGATE_FRONTEND_DIR"] = str(FRONTEND_DIR)


def ensure_runtime_environment() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Shared classroom computers may contain another teacher's legacy data.
    # Import is only allowed when explicitly requested; never on first launch.
    install_bundled_knowledge_model()
    example_path = BACKEND_DIR / ".env.example"
    if not CONFIG_PATH.exists() and example_path.exists():
        shutil.copyfile(example_path, CONFIG_PATH)
    restrict_macos_permissions()


def install_bundled_knowledge_model() -> None:
    """Copy bundled, public model assets into writable app data on first use."""
    bundled = RESOURCE_DIR / "knowledge_models"
    target = DATA_DIR / "knowledge_models"
    if not (bundled / "model.json").is_file():
        return
    manifest = json.loads((bundled / "model.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    for filename in ("model_optimized.onnx", "config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        if (target / filename).is_file() and sha256_file(target / filename) == manifest["files"][filename]:
            continue
        temporary = target / (filename + ".installing")
        shutil.copyfile(bundled / filename, temporary)
        if sha256_file(temporary) != manifest["files"][filename]:
            raise RuntimeError("随包模型校验失败，请重新解压完整便携包。")
        os.replace(temporary, target / filename)
    write_json(target / "model.json", manifest)


def restrict_macos_permissions() -> None:
    if sys.platform != "darwin":
        return
    for directory in {DATA_DIR, CONFIG_PATH.parent}:
        if directory.exists():
            directory.chmod(0o700)
    if CONFIG_PATH.exists():
        CONFIG_PATH.chmod(0o600)


def migrate_legacy_data() -> bool:
    if os.getenv("EDUGATE_IMPORT_LEGACY", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
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


def open_when_ready(health_url: str, admin_url: str, identity: str = "", run_id: str = "") -> None:
    for _ in range(480):
        if healthcheck(health_url, identity, run_id):
            if identity and run_id:
                write_json(DATA_DIR / "launcher-state.json", {"instance_id": identity, "run_id": run_id,
                    "pid": os.getpid(), "port": int(os.environ["EDUGATE_BACKEND_PORT"]), "status": "running"})
            if os.getenv("EDUGATE_AUTO_OPEN", "true").lower() in {"1", "true", "yes", "on"}:
                webbrowser.open(admin_url)
            return
        time.sleep(0.25)


def healthcheck(url: str, identity: str = "", run_id: str = "") -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            return (response.status == 200 and response.headers.get("X-EduGate-App") == "EduGate"
                and (not identity or response.headers.get("X-EduGate-Instance") == identity)
                and (not run_id or response.headers.get("X-EduGate-Run") == run_id))
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def first_available_port(preferred: int, *, attempts: int = 20) -> int:
    with reserve_listener(preferred, attempts=attempts) as listener:
        return listener.getsockname()[1]


def reserve_listener(preferred: int, *, attempts: int = 20) -> socket.socket:
    """Hold the port while imports/models start, including simultaneous launches."""
    for port in range(preferred, min(preferred + attempts, 65536)):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("0.0.0.0", port))
            return probe
        except OSError:
            probe.close()
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
        command = [sys.executable, *sys.argv[1:]]
    else:
        executable = Path(sys.executable)
        pythonw = executable.with_name("pythonw.exe")
        command = [str(pythonw if pythonw.exists() else executable), str(Path(__file__).resolve()), *sys.argv[1:]]
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


if __name__ == "__main__":
    raise SystemExit(main())
