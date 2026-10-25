"""Exercise persisted configuration across two independent server processes."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


def test_upstream_and_downstream_configuration_survive_process_restart(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    data = tmp_path / "persistent-data"
    env = {**os.environ,
        "EDUGATE_DATA_DIR": str(data), "RUNTIME_CONFIG_PATH": str(data / "runtime_config.json"),
        "SECRET_STORE_PATH": str(data / "secrets.json"), "EDUGATE_CONFIG_PATH": str(tmp_path / "test.env"),
        "EDUGATE_FRONTEND_DIR": str(root / "frontend"), "EDUGATE_MODE": "standalone",
        "EDUGATE_PORTABLE_MODE": "true", "PORTABLE_AUTO_LOGIN": "false", "ALLOW_LAN_ADMIN": "false",
        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "restart-test-password",
        "UPSTREAM_API_KEY": "", "UPSTREAM_BASE_URL": "", "PLATFORM_API_KEY": "",
        "SECRET_STORE_MODE": "portable", "EDUGATE_BACKEND_PORT": str(port),
        "EDUGATE_AUTO_OPEN": "false", "EDUGATE_SKIP_LEGACY_IMPORT": "true",
    }
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"]
    if os.getenv("EDUGATE_TEST_EXECUTABLE"):
        profile = tmp_path / "packaged-profile"
        data = profile / "data"
        config = profile / "config/edugate.env"
        config.parent.mkdir(parents=True)
        command = [os.environ["EDUGATE_TEST_EXECUTABLE"], "--portable-dir", str(profile), "--no-browser", "--no-exit-notice"]
        config.write_text("\n".join(f"{key}={json.dumps(value)}" for key, value in env.items() if key in {
            "EDUGATE_BACKEND_PORT", "PORTABLE_AUTO_LOGIN", "ALLOW_LAN_ADMIN", "ADMIN_USERNAME", "ADMIN_PASSWORD",
            "UPSTREAM_API_KEY", "UPSTREAM_BASE_URL", "PLATFORM_API_KEY", "SECRET_STORE_MODE",
        }), encoding="utf-8")

    @contextmanager
    def running():
        with (tmp_path / "server.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=root / "backend", env=env, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3) as api:
                    deadline = time.monotonic() + 25
                    while True:
                        assert process.poll() is None, "Server exited before becoming ready"
                        try:
                            if api.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        assert time.monotonic() < deadline, "Server did not start"
                        time.sleep(.1)
                    login = api.post("/auth/login", json={"username": "admin", "password": "restart-test-password"})
                    assert login.status_code == 200
                    api.headers["X-Admin-Token"] = login.json()["access_token"]
                    yield api
            finally:
                # Launcher children must also be stopped in the packaged validation.
                if process.poll() is None and os.getenv("EDUGATE_TEST_EXECUTABLE") and os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
                elif process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)

    with running() as api:
        original_key = api.get("/admin/system/platform-key").json()["api_key"]
        assert len(original_key) == 25
        saved = api.post("/admin/providers", json={"provider": "重启验证供应商", "base_url": "https://offline.example/v1", "api_key": "restart-upstream-key"})
        assert saved.status_code == 200
        provider_id = saved.json()["provider_id"]
    with running() as api:
        assert api.get("/admin/system/platform-key").json()["api_key"] == original_key
        assert api.get("/v1/models", headers={"Authorization": f"Bearer {original_key}"}).status_code == 200
        providers = api.get("/admin/providers").json()
        assert next(p for p in providers if p["id"] == provider_id)["name"] == "重启验证供应商"
        # Leaving Key empty after restart must reuse the persisted upstream credential.
        assert api.post("/admin/providers", json={"provider_id": provider_id, "provider": "重启验证供应商", "base_url": "https://offline.example/v1"}).status_code == 200
    secrets = json.loads((data / "secrets.json").read_text(encoding="utf-8"))["secrets"]
    assert secrets[f"provider:{provider_id}"] == "restart-upstream-key"
    assert secrets["system:platform_api_key"] == original_key
