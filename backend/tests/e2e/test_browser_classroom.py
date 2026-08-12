from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, sync_playwright


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_BROWSER_E2E") != "1",
        reason="set RUN_BROWSER_E2E=1 to run the real-browser simulation",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
ADMIN_PASSWORD = "browser-test-password"
SIMULATED_ANSWER = "E2E simulated classroom answer"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_response(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._json_response({"data": [{"id": "browser-sim-model", "owned_by": "EduGate E2E"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_error(404)
            return
        if not payload.get("stream"):
            self._json_response({"choices": [{"message": {"content": SIMULATED_ANSWER}}]})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        events = [
            {"choices": [{"delta": {"content": "E2E simulated "}}]},
            {"choices": [{"delta": {"content": "classroom answer"}}]},
        ]
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture()
def live_classroom(tmp_path: Path) -> Iterator[str]:
    provider_port = _free_port()
    provider = ThreadingHTTPServer(("127.0.0.1", provider_port), _ProviderHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()

    app_port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "EDUGATE_DATA_DIR": str(tmp_path / "browser-app-data"),
            "EDUGATE_FRONTEND_DIR": str(ROOT / "frontend"),
            "EDUGATE_MODE": "standalone",
            "EDUGATE_PORTABLE_MODE": "true",
            "PORTABLE_AUTO_LOGIN": "false",
            "ALLOW_LAN_ADMIN": "true",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "SECRET_STORE_MODE": "portable",
            "DEFAULT_MODEL": "browser-sim-model",
            "UPSTREAM_PROVIDER": "EduGate E2E Provider",
            "UPSTREAM_BASE_URL": f"http://127.0.0.1:{provider_port}/v1",
            "UPSTREAM_API_KEY": "browser-provider-key",
            "PYTHON_RUNNER_ENABLED": "false",
            "CORS_ORIGINS": "",
        }
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(app_port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT / "backend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    base_url = f"http://127.0.0.1:{app_port}"
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"EduGate exited before the E2E test started:\n{output}")
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("EduGate did not become healthy within 20 seconds")
        yield base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)


def _login_and_start_class(page: Page, base_url: str) -> str:
    page.goto(f"{base_url}/admin.html", wait_until="domcontentloaded")
    page.locator("#base-url").fill(base_url)
    page.locator("#admin-username").fill("admin")
    page.locator("#admin-password").fill(ADMIN_PASSWORD)
    page.locator("#login-button").click()
    page.locator("#control-view").wait_for(state="visible")
    page.locator("#current-model").filter(has_text="browser-sim-model").wait_for()
    page.locator("#start-class").click()
    page.locator("#classroom-state").filter(has_text="已开放").wait_for()
    return page.locator("#classroom-url").input_value()


def test_teacher_student_stream_record_and_revocation_in_a_real_browser(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            teacher_context = browser.new_context(viewport={"width": 1280, "height": 900})
            student_context = browser.new_context(viewport={"width": 1024, "height": 768})
            teacher = teacher_context.new_page()
            student = student_context.new_page()
            teacher_errors: list[str] = []
            student_errors: list[str] = []
            teacher.on("pageerror", lambda error: teacher_errors.append(str(error)))
            student.on("pageerror", lambda error: student_errors.append(str(error)))

            distributed_url = _login_and_start_class(teacher, live_classroom)
            fragment = urlsplit(distributed_url).fragment
            assert fragment.startswith("class_token=")
            student_url = f"{live_classroom}/student.html#{fragment}"
            student.goto(student_url, wait_until="domcontentloaded")
            student.locator("#statusBar").filter(has_text="课堂已连接").wait_for()

            student.locator("#chatInput").fill("E2E simulated question")
            student.locator("#sendBtn").click()
            student.locator("#chatHistory").filter(has_text=SIMULATED_ANSWER).wait_for(timeout=15_000)
            student.locator("#statusBar").filter(has_text="回答完成").wait_for()

            teacher.locator('[data-tab="records"]').click()
            teacher.locator("#records-view").wait_for(state="visible")
            teacher.locator("#records-view").filter(has_text="E2E simulated question").wait_for(timeout=15_000)
            teacher.locator("#records-view").filter(has_text=SIMULATED_ANSWER).wait_for()

            teacher.once("dialog", lambda dialog: dialog.accept())
            teacher.locator('[data-tab="control"]').click()
            teacher.locator("#end-class").click()
            teacher.locator("#classroom-state").filter(has_text="已暂停").wait_for()
            assert teacher.locator("#classroom-url").input_value() == distributed_url
            student.reload(wait_until="domcontentloaded")
            student.locator("#statusBar").filter(has_text="加入课堂失败").wait_for(timeout=10_000)

            teacher.locator("#start-class").click()
            teacher.locator("#classroom-state").filter(has_text="已开放").wait_for()
            assert teacher.locator("#classroom-url").input_value() == distributed_url
            student.reload(wait_until="domcontentloaded")
            student.locator("#statusBar").filter(has_text="课堂已连接").wait_for(timeout=10_000)

            assert teacher_errors == []
            assert student_errors == []
        finally:
            browser.close()
