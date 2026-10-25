from __future__ import annotations

import json
import os
import re
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
from playwright.sync_api import Page, expect, sync_playwright


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
            if "classroom blacklist" in str(payload.get("messages", [{}])[0].get("content", "")):
                data = json.loads(payload["messages"][-1]["content"])
                text = data["text"]
                if data.get("kind") == "question":
                    try:
                        context = json.loads(text)
                    except json.JSONDecodeError:
                        context = None
                    if isinstance(context, dict) and "current_question" in context:
                        text = context["current_question"]
                        if re.search(r"再|继续|详细|further|another", text, re.I):
                            text += str(context.get("prior_context", ""))
                match = next((topic["label"] for topic in data["forbidden_topics"] if any(word in text for word in [topic["label"], *topic["aliases"]])), "")
                self._json_response({"choices":[{"message":{"content":json.dumps({"blocked":bool(match),"topic":match},ensure_ascii=False)}}]})
                return
            if "check whether teacher materials" in str(payload.get("messages", [{}])[0].get("content", "")):
                evidence = json.loads(payload["messages"][-1]["content"])
                match = next((item for item in evidence["evidence"] if "IPv6地址长度是128位" in item["text"]), None)
                result = {"supported": bool(match), "evidence": [{"id": match["id"], "quote": "IPv6地址长度是128位"}] if match else []}
                self._json_response({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]})
                return
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
            "PYTHON_RUNNER_ENABLED": "true",
            "CORS_ORIGINS": "",
            "KNOWLEDGE_SEMANTIC_ENABLED": os.getenv("EDUGATE_E2E_SEMANTIC", "false"),
        }
    )
    if os.getenv("EDUGATE_TEST_KNOWLEDGE_MODEL_DIR") and not os.getenv("EDUGATE_TEST_EXECUTABLE"):
        env["KNOWLEDGE_MODEL_DIR"] = os.environ["EDUGATE_TEST_KNOWLEDGE_MODEL_DIR"]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning"]
    if os.getenv("EDUGATE_TEST_EXECUTABLE"):
        executable = Path(os.environ["EDUGATE_TEST_EXECUTABLE"]).resolve(strict=True)
        env.update({"EDUGATE_BACKEND_PORT": str(app_port), "EDUGATE_AUTO_OPEN": "false", "EDUGATE_SKIP_LEGACY_IMPORT": "true"})
        # The packaged launcher reads its own config; keep it and all state isolated.
        profile = tmp_path / "packaged-profile"
        config = profile / "config" / "edugate.env"
        config.parent.mkdir(parents=True)
        config.write_text("\n".join(f"{key}={json.dumps(env[key])}" for key in (
            "EDUGATE_BACKEND_PORT", "PORTABLE_AUTO_LOGIN", "ALLOW_LAN_ADMIN", "ADMIN_USERNAME", "ADMIN_PASSWORD",
            "SECRET_STORE_MODE", "DEFAULT_MODEL", "UPSTREAM_PROVIDER", "UPSTREAM_BASE_URL", "UPSTREAM_API_KEY",
            "PYTHON_RUNNER_ENABLED", "CORS_ORIGINS",
        )), encoding="utf-8")
        env["EDUGATE_CONFIG_PATH"] = str(config)
        command = [str(executable), "--portable-dir", str(profile), "--no-browser", "--no-exit-notice"]
    process = subprocess.Popen(
        command,
        cwd=ROOT / "backend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    base_url = f"http://127.0.0.1:{app_port}"
    deadline = time.monotonic() + 120
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


def test_teacher_strategy_draft_survives_refresh_and_keyboard_tab_switch(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login_and_start_class(page, live_classroom)
            prompt = page.locator("#system-prompt")
            draft = "先听学生说明理由，再给出下一步提示。"
            prompt.fill(draft)
            expect(page.locator("#strategy-save-state")).to_have_text("有未保存的修改")
            with page.expect_response(lambda response: response.url.endswith("/admin/providers")) as response:
                page.locator("#reload-models").click()
            response.value.finished()
            expect(page.locator("#reload-models")).to_be_enabled()
            expect(prompt).to_have_value(draft)
            page.get_by_role("tab", name="控制", exact=True).focus()
            page.get_by_role("tab", name="控制", exact=True).press("ArrowRight")
            expect(page.get_by_role("tab", name="记录", exact=True)).to_have_attribute("aria-selected", "true")
            page.get_by_role("tab", name="记录", exact=True).press("Home")
            expect(prompt).to_have_value(draft)
            page.locator("#save-scenario").click()
            expect(page.locator("#strategy-save-state")).to_have_text("已保存")
            page.reload()
            expect(prompt).to_have_value(draft)
        finally:
            browser.close()


def test_browser_python_is_offline_isolated_unicode_and_interruptible(live_classroom: str, tmp_path: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            distributed = _login_and_start_class(page, live_classroom)
            token = urlsplit(distributed).fragment.split("=", 1)[1]
            page.goto(f"{live_classroom}/student.html#class_token={token}")
            page.locator("#demoApp").wait_for(state="visible")
            page.add_script_tag(url=f"{live_classroom}/assets/edugate-client.js")
            page.evaluate("token => { window.pythonClient = new EduGateClient({classToken: token}); }", token)
            external = []
            page.on("request", lambda request: external.append(request.url) if request.url.startswith("http") and not request.url.startswith(live_classroom + "/") else None)
            result = page.evaluate("async () => await pythonClient.runPython('print(\"中文课堂\")\\nimport math\\nprint(math.sqrt(9))')")
            assert result["stdout"] == "中文课堂\n3.0\n"
            assert result["exit_code"] == 0
            assert external == []
            for code, expected in [
                ('print("无换行", end="")', "无换行"),
                ('import sys\nsys.stdout.write("xyz"); sys.stdout.flush()', "xyz"),
                ('import os\nfor b in "中文".encode():\n    os.write(1, bytes([b]))', "中文"),
            ]:
                result = page.evaluate("async code => await pythonClient.runPython(code)", code)
                assert result["stdout"] == expected
                assert result["exit_code"] == 0
            result = page.evaluate("async () => await pythonClient.runPython('print(\"先输出\")\\nwhile True: pass')")
            assert result["timed_out"] is True
            assert result["stdout"] == "先输出\n"
            result = page.evaluate("async () => await pythonClient.runPython('print(\"超时前\", end=\"\", flush=True)\\nwhile True: pass')")
            assert result["stdout"] == "超时前"
            assert result["timed_out"] is True
            # Python's JS bridge must not inherit teacher origin/storage/API access.
            result = page.evaluate("async () => await pythonClient.runPython('import js\\ntry:\\n    await js.fetch(\"' + location.origin + '/auth/status\")\\n    print(\"NETWORK_ALLOWED\")\\nexcept Exception:\\n    print(\"NETWORK_BLOCKED\")')")
            assert "NETWORK_BLOCKED" in result["stdout"]
            result = page.evaluate("async () => await pythonClient.runPython('import js\\nresponse = await js.fetch(\"' + location.origin + '/assets/vendor/pyodide/pyodide-lock.json\")\\nprint(response.status)')")
            assert result["stdout"] == "200\n"
            canary = tmp_path / "teacher-only.txt"
            canary.write_text("teacher-only-test-marker", encoding="utf-8")
            code = f'try:\n    print(open({str(canary)!r}).read())\nexcept OSError:\n    print("TEACHER_FILE_UNAVAILABLE")\nwith open("/tmp/student.txt", "w") as f:\n    f.write("student virtual file")\nprint(open("/tmp/student.txt").read())'
            result = page.evaluate("async code => await pythonClient.runPython(code)", code)
            assert result["stdout"] == "TEACHER_FILE_UNAVAILABLE\nstudent virtual file\n"
            # The visible Demo UI uses the same browser runner and records its result.
            page.locator("#pythonCode").fill('print("Demo 中文运行")')
            page.locator("#runPythonBtn").click()
            expect(page.locator("#pythonOutput")).to_contain_text("Demo 中文运行", timeout=15000)
            expect(page.locator("#runPythonBtn")).to_be_enabled()
            assert external == []
        finally:
            browser.close()


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

            student.locator("#pythonCode").fill('print("same student")')
            student.locator("#runPythonBtn").click()
            expect(student.locator("#pythonOutput")).to_contain_text("same student", timeout=15000)
            expect(student.locator("#runPythonBtn")).to_be_enabled()
            assert teacher.evaluate('async () => (await request("/admin/classroom")).student_count') == 1

            student.locator("#chatInput").fill("E2E simulated question")
            student.locator("#sendBtn").click()
            student.locator("#chatHistory").filter(has_text=SIMULATED_ANSWER).wait_for(timeout=15_000)
            student.locator("#statusBar").filter(has_text="回答完成").wait_for()

            teacher.locator('[data-tab="records"]').click()
            teacher.locator("#records-view").wait_for(state="visible")
            teacher.locator("#records-view").filter(has_text="E2E simulated question").wait_for(timeout=15_000)
            teacher.locator("#records-view").filter(has_text=SIMULATED_ANSWER).wait_for()
            records = teacher.evaluate('async () => (await request("/admin/classroom-records")).records')
            assert records[0]["student_count"] == 1

            teacher.once("dialog", lambda dialog: dialog.accept())
            teacher.locator('[data-tab="control"]').click()
            teacher.locator("#end-class").click()
            teacher.locator("#classroom-state").filter(has_text="已暂停").wait_for()
            assert teacher.locator("#classroom-url").input_value() == distributed_url
            student.reload(wait_until="domcontentloaded")
            student.locator("#classroomGate").wait_for(state="visible")
            student.locator("#classroomGateMessage").filter(
                has_text="课堂尚未开始或已经结束"
            ).wait_for(timeout=10_000)

            teacher.locator("#start-class").click()
            teacher.locator("#classroom-state").filter(has_text="已开放").wait_for()
            assert teacher.locator("#classroom-url").input_value() == distributed_url
            student.reload(wait_until="domcontentloaded")
            student.locator("#statusBar").filter(has_text="课堂已连接").wait_for(timeout=10_000)

            assert teacher_errors == []
            assert student_errors == []
        finally:
            browser.close()


def test_advanced_edits_survive_poll_refresh_save_and_navigation(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            _login_and_start_class(page, live_classroom)
            page.locator('[data-tab="system"]').click()
            page.get_by_role("tab", name="高级设置", exact=True).click()
            field = page.locator('[data-setting-key="STUDENT_SESSION_TTL_SECONDS"]')
            field.fill("34567")
            checkbox = page.locator('[data-setting-key="PYTHON_RUNNER_ENABLED"]')
            checkbox.uncheck()
            page.evaluate('window.editedSettingNode = document.querySelector("[data-setting-key=STUDENT_SESSION_TTL_SECONDS]")')
            with page.expect_response(lambda response: response.url.endswith("/admin/system/status")):
                page.get_by_role("tab", name="运行状态", exact=True).click()
            expect(field).to_have_value("34567")
            expect(checkbox).not_to_be_checked()
            with page.expect_response(lambda response: response.url.endswith("/admin/system/settings")):
                page.locator("#refresh-system").click()
            page.get_by_role("tab", name="高级设置", exact=True).click()
            expect(field).to_have_value("34567")
            assert page.evaluate('editedSettingNode === document.querySelector("[data-setting-key=STUDENT_SESSION_TTL_SECONDS]")')
            with page.expect_response(lambda response: response.url.endswith("/admin/system/settings") and response.request.method == "PUT") as saved:
                page.locator("#save-advanced-settings").click()
            assert saved.value.json()["values"]["STUDENT_SESSION_TTL_SECONDS"] == 34567
            page.locator('[data-tab="control"]').click()
            page.locator('[data-tab="system"]').click()
            expect(field).to_have_value("34567")
            expect(checkbox).not_to_be_checked()
        finally:
            browser.close()


def test_published_page_python_bridge_and_session_renewal(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            teacher = browser.new_page()
            _login_and_start_class(teacher, live_classroom)
            teacher.locator('[data-tab="system"]').click()
            teacher.get_by_role("tab", name="网页发布", exact=True).click()
            teacher.locator("#published-page-title").fill("Python 网页测试")
            html = b'''<!doctype html><html><head></head><body><button id="run">Run</button><pre id="result"></pre><script>
            document.querySelector('#run').onclick = async () => {
              document.querySelector('#result').textContent = 'running';
              try { const result = await EduGate.runPython('print(6 * 7)'); document.querySelector('#result').textContent = result.stdout; }
              catch (error) { document.querySelector('#result').textContent = error.message; }
            };</script></body></html>'''
            teacher.locator("#published-page-file").set_input_files({"name": "python-lesson.html", "mimeType": "text/html", "buffer": html})
            teacher.locator("#publish-page").click()
            expect(teacher.locator("#published-page-list")).to_contain_text("Python 网页测试")
            teacher.locator('[data-tab="control"]').click()
            url = teacher.locator("#classroom-url").input_value()
            target = urlsplit(url)
            student = browser.new_page()
            student.goto(live_classroom + target.path + "?" + target.query + "#" + target.fragment)
            frame = student.frame_locator("#page-frame")
            frame.locator("#run").click()
            expect(frame.locator("#result")).to_have_text("42\n", timeout=15000)
            # A new class keeps the permanent entry but requires a fresh student session.
            teacher.once("dialog", lambda dialog: dialog.accept())
            teacher.locator("#end-class").click()
            expect(teacher.locator("#classroom-state")).to_contain_text("已暂停")
            teacher.locator("#start-class").click()
            expect(teacher.locator("#classroom-state")).to_contain_text("已开放")
            frame.locator("#run").click()
            expect(frame.locator("#result")).to_have_text("42\n", timeout=15000)
        finally:
            browser.close()


def test_settings_dropdowns_saved_provider_and_key_survive_reopening(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            _login_and_start_class(page, live_classroom)
            model_picker = page.get_by_role("combobox", name="选择课堂 AI 模型")
            model_picker.click()
            expect(page.get_by_role("option", name="browser-sim-model", exact=True)).to_have_attribute("aria-disabled", "false")
            model_picker.press("Escape")
            expect(page.get_by_role("listbox")).not_to_be_visible()
            source_picker = page.get_by_role("combobox", name="选择知识库", exact=True)
            source_picker.focus()
            source_picker.press("ArrowDown")
            expect(page.get_by_role("listbox")).to_be_visible()
            source_picker.press("Enter")
            expect(page.get_by_role("listbox")).not_to_be_visible()

            page.locator('[data-tab="system"]').click()
            summaries = page.get_by_role("tablist", name="系统设置分类").get_by_role("tab")
            expect(summaries).to_have_text(["运行状态", "模型服务", "网页发布", "接入配置", "高级设置", "运行日志", "备份与升级"])
            expect(page.locator("#system-view > .workspace-panel:not([hidden])")).to_have_count(1)
            page.get_by_role("tab", name="接入配置", exact=True).click()
            key = page.locator("#platform-api-key")
            expect(key).to_have_value(__import__("re").compile(r"eg_[\w-]{22}"))
            original = key.input_value()
            expect(key).to_have_attribute("type", "password")
            page.locator("#toggle-platform-key").click()
            expect(key).to_have_attribute("type", "text")
            expect(page.locator("#openai-example-panel")).not_to_have_attribute("open", "")
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.locator("#generate-platform-key").click()
            expect(key).to_have_value(original)
            key.fill("draft-value")
            page.get_by_role("tab", name="运行状态", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/admin/system/status")):
                page.locator("#refresh-system").click()
            page.get_by_role("tab", name="接入配置", exact=True).click()
            expect(key).to_have_value("draft-value")
            page.reload()
            page.locator('[data-tab="system"]').click()
            page.get_by_role("tab", name="接入配置", exact=True).click()
            expect(key).to_have_value(original)
            page.locator("#toggle-platform-key").click()
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#generate-platform-key").click()
            expect(key).not_to_have_value(original)
            regenerated = key.input_value()
            assert len(regenerated) == 25

            page.get_by_role("tab", name="模型服务", exact=True).click()
            upstream_url = page.locator("#model-api-url").input_value()
            page.locator("#add-model-provider").click()
            page.locator("#model-provider-name").fill("保存后再导入")
            page.locator("#model-api-url").fill(upstream_url)
            page.locator("#model-api-key").fill("browser-provider-key")
            page.locator("#save-model-provider").click()
            expect(page.locator("#provider-save-status")).to_contain_text("已保存到本机文件")
            page.reload()
            page.locator('[data-tab="system"]').click()
            expect(page.locator("#system-view > .workspace-panel:not([hidden])")).to_have_count(1)
            page.get_by_role("tab", name="接入配置", exact=True).click()
            expect(key).to_have_value(regenerated)
            expect(key).to_have_attribute("type", "password")
            page.get_by_role("tab", name="模型服务", exact=True).click()
            page.get_by_role("combobox", name="选择已配置供应商").click()
            page.get_by_role("option", name="保存后再导入（0 个模型）", exact=True).click()
            expect(page.locator("#model-api-url")).to_have_value(upstream_url)
            expect(page.locator("#model-api-key")).to_have_value("")
            page.locator("#discover-models").click()
            expect(page.locator("#model-discovery")).to_be_visible()
            page.locator('[data-discovered-model-id="browser-sim-model"]').check()
            page.locator("#import-discovered-models").click()
            expect(page.locator("#model-discovery")).to_be_hidden()
            page.locator('[data-tab="control"]').click()
            page.get_by_role("combobox", name="选择课堂 AI 模型").click()
            with page.expect_response(lambda response: response.url.endswith("/config/model")) as switched:
                page.get_by_role("option").last.click()
            assert switched.value.status == 200
            assert errors == []
        finally:
            browser.close()


def test_default_copied_embed_code_streams_in_published_page(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            teacher = browser.new_page()
            _login_and_start_class(teacher, live_classroom)
            teacher.locator('[data-tab="system"]').click()
            teacher.get_by_role("tab", name="接入配置", exact=True).click()
            expect(teacher.locator("#browser-cors-status")).to_have_text("课堂已开放 · 可使用")
            snippet = teacher.locator("#browser-embed-example").inner_text()
            assert "EduGate.ask" in snippet and "EduGateClient" not in snippet
            teacher.get_by_role("combobox", name="网页放在哪里").click()
            teacher.get_by_role("option", name="放在其他网站", exact=True).click()
            expect(teacher.locator("#browser-embed-example")).to_contain_text("edugate-client.js")
            expect(teacher.locator("#browser-external-help")).to_be_visible()
            teacher.get_by_role("tab", name="网页发布", exact=True).click()
            teacher.locator("#published-page-title").fill("嵌入代码实测")
            html = ('<!doctype html><html><head></head><body><button id="ask">提问</button><pre id="answer"></pre>' + snippet + '''<script>
              document.querySelector('#ask').onclick = async () => {
                const output = document.querySelector('#answer'); output.textContent = '';
                try { await askEduGate('What is AI?', text => { output.textContent += text; }); }
                catch(error) { output.textContent = error.message; }
              };
            </script></body></html>''').encode("utf-8")
            teacher.locator("#published-page-file").set_input_files({"name": "embed.html", "mimeType": "text/html", "buffer": html})
            teacher.locator("#publish-page").click()
            expect(teacher.locator("#published-page-list")).to_contain_text("嵌入代码实测")
            target = urlsplit(teacher.locator("#browser-distribution-url").input_value())
            assert target.path == "/published.html"
            student = browser.new_page()
            student.goto(live_classroom + target.path + "#" + target.fragment)
            frame = student.frame_locator("#page-frame")
            frame.locator("#ask").click()
            expect(frame.locator("#answer")).to_have_text(SIMULATED_ANSWER)
        finally:
            browser.close()


def test_knowledge_upload_scope_citations_and_class_end_in_browser(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            teacher = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            teacher.on("pageerror", lambda error: errors.append(str(error)))
            distributed = _login_and_start_class(teacher, live_classroom)
            teacher.locator('[data-tab="settings"]').click()
            teacher.locator("#knowledge-file").set_input_files({
                "name":"网络教材.md", "mimeType":"text/markdown",
                "buffer":"# IPv6\nIPv6地址长度是128位。\n\n# NAT\nNAT让多台内网设备共用一个公网地址。".encode("utf-8"),
            })
            teacher.locator("#upload-file").click()
            expect(teacher.locator("#file-list")).to_contain_text("网络教材.md", timeout=30000)
            teacher.get_by_role("button",name="解析预览",exact=True).click()
            expect(teacher.get_by_role("dialog",name="资料解析预览")).to_contain_text("IPv6地址长度是128位")
            teacher.get_by_role("dialog").get_by_role("button",name="关闭",exact=True).click()
            teacher.get_by_role("button",name="资料属性",exact=True).click()
            dialog = teacher.get_by_role("dialog",name="资料属性")
            dialog.get_by_role("textbox",name="教材版本",exact=True).fill("2026")
            dialog.get_by_role("button",name="保存资料属性",exact=True).click()
            expect(dialog).not_to_be_visible()
            teacher.get_by_role("tab", name="课堂范围", exact=True).click()
            teacher.locator("#knowledge-scope-edition").fill("2025")
            teacher.get_by_role("button", name="前往统一试问", exact=True).click()
            expect(teacher.locator("#knowledge-trial-notice")).to_contain_text("课堂范围有未保存的修改")
            expect(teacher.locator("#knowledge-search")).to_be_disabled()
            teacher.get_by_role("button", name="调整范围", exact=True).click()
            teacher.locator("#knowledge-save-scope").click()
            expect(teacher.locator("#knowledge-scope-state")).to_have_text("已应用")
            teacher.get_by_role("tab", name="黑名单与试问", exact=True).click()
            expect(teacher.locator("#knowledge-trial-scope")).to_contain_text("版本：2025")
            teacher.locator("#knowledge-question").fill("IPv6有多少位？")
            teacher.get_by_text("仅检查资料检索（对照）", exact=True).click()
            teacher.locator("#knowledge-search-evidence").click()
            expect(teacher.locator("#knowledge-evidence-results")).to_contain_text("未找到资料依据")
            teacher.get_by_role("tab", name="课堂范围", exact=True).click()
            teacher.locator("#knowledge-scope-edition").fill("2026")
            teacher.locator("#knowledge-scope-chapter").fill("IPv6")
            teacher.locator("#knowledge-save-scope").click()
            expect(teacher.locator("#knowledge-scope-note")).to_contain_text("保存后生效")
            teacher.get_by_role("tab", name="黑名单与试问", exact=True).click()
            expect(teacher.locator("#knowledge-trial-scope")).to_contain_text("版本：2026")
            teacher.locator("#knowledge-search-evidence").click()
            expect(teacher.locator("#knowledge-evidence-results")).to_contain_text("IPv6地址长度是128位")
            if os.getenv("EDUGATE_E2E_SEMANTIC") == "true":
                expect(teacher.locator("#knowledge-status")).to_contain_text("中文混合检索已就绪", timeout=30000)
            # Draft scope must survive polling without losing focus.
            teacher.get_by_role("tab", name="课堂范围", exact=True).click()
            teacher.locator("#knowledge-scope-grade").fill("草稿年级")
            with teacher.expect_response(lambda response: response.url.endswith("/knowledge/status")):
                pass
            expect(teacher.locator("#knowledge-scope-grade")).to_have_value("草稿年级")
            teacher.locator("#knowledge-scope-grade").fill("")
            teacher.get_by_role("tab", name="黑名单与试问", exact=True).click()
            teacher.locator("#knowledge-blacklist-topics").fill("游戏")
            teacher.locator("#knowledge-blacklist-enabled").check()
            teacher.locator("#knowledge-policy-save").click()
            expect(teacher.locator("#knowledge-policy-status")).to_contain_text("已启用 1 个禁答话题")
            teacher.locator("#knowledge-question").fill("游戏攻略")
            expect(teacher.locator("#knowledge-policy-state")).to_have_text("已保存")
            teacher.locator("#knowledge-search").click()
            expect(teacher.locator("#knowledge-search-results")).to_contain_text("命中：游戏")
            expect(teacher.locator("#knowledge-search-results")).to_contain_text("未继续检索或引用")
            teacher.locator("#knowledge-blacklist-topics").fill("游戏\n未保存的测试话题")
            expect(teacher.locator("#knowledge-search")).to_be_disabled()
            teacher.get_by_role("tab", name="课堂范围", exact=True).click()
            teacher.get_by_role("tab", name="黑名单与试问", exact=True).click()
            expect(teacher.locator("#knowledge-blacklist-topics")).to_have_value("游戏\n未保存的测试话题")
            teacher.get_by_role("button", name="撤销修改", exact=True).click()
            expect(teacher.locator("#knowledge-blacklist-topics")).to_have_value("游戏")
            teacher.locator('[data-tab="control"]').click()
            teacher.locator("#knowledge-strict").check()
            teacher.locator("#save-scenario").click()
            teacher.reload()
            expect(teacher.locator("#knowledge-strict")).to_be_checked()
            teacher.locator('[data-tab="settings"]').click()
            expect(teacher.locator("#knowledge-blacklist-topics")).to_have_value("游戏")
            teacher.locator('[data-tab="control"]').click()
            student = browser.new_page()
            token = urlsplit(distributed).fragment.split("=",1)[1]
            student.goto(f"{live_classroom}/student.html#class_token={token}")
            student.locator("#chatInput").fill("游戏攻略")
            student.locator("#sendBtn").click()
            expect(student.locator(".msg-ai .msg-bubble").last).to_contain_text("不在本节课允许讨论的范围", timeout=30000)
            expect(student.locator("#sendBtn")).to_be_enabled()
            student.locator("#chatInput").fill("IPv6有多少位？")
            student.locator("#sendBtn").click()
            expect(student.locator(".knowledge-sources")).to_contain_text("网络教材.md", timeout=30000)
            student.locator(".knowledge-sources button").first.click()
            expect(student.locator(".knowledge-citation-text")).to_contain_text("128位")
            teacher.once("dialog", lambda dialog: dialog.accept())
            teacher.locator("#end-class").click()
            expect(teacher.locator("#classroom-state")).to_contain_text("已暂停")
            student.locator(".knowledge-sources button").first.click()
            expect(student.locator(".knowledge-citation-text")).to_contain_text("无法查看依据")
            student.reload()
            expect(student.locator("#classroomGate")).to_be_visible(timeout=15000)
            assert errors == []
        finally:
            browser.close()


def test_teacher_full_trial_followup_reset_and_record_isolation(live_classroom: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            teacher = browser.new_page(viewport={"width":1366,"height":900})
            errors = []
            teacher.on("pageerror", lambda error: errors.append(str(error)))
            _login_and_start_class(teacher, live_classroom)
            teacher.once("dialog", lambda dialog: dialog.accept())
            teacher.locator("#end-class").click()
            expect(teacher.locator("#classroom-state")).to_contain_text("已暂停")
            teacher.locator('[data-tab="settings"]').click()
            teacher.get_by_role("tab", name="黑名单与试问", exact=True).click()
            teacher.locator("#knowledge-question").fill("Explain an IP address")
            teacher.locator("#knowledge-search").click()
            expect(teacher.locator(".trial-answer-text")).to_have_text(SIMULATED_ANSWER)
            expect(teacher.locator("#knowledge-question")).to_have_value("")
            teacher.locator("#knowledge-question").fill("Give another example")
            teacher.locator("#knowledge-question").press("Control+Enter")
            expect(teacher.locator(".trial-conversation-turn")).to_have_count(2)
            expect(teacher.locator("#knowledge-trial-round")).to_contain_text("已完成 2 轮")
            teacher.locator("#knowledge-trial-reset").click()
            expect(teacher.locator(".trial-conversation-turn")).to_have_count(0)
            teacher.locator("#knowledge-blacklist-topics").fill("游戏")
            teacher.locator("#knowledge-policy-save").click()
            expect(teacher.locator("#knowledge-policy-state")).to_have_text("已保存")
            teacher.locator("#knowledge-question").fill("游戏攻略")
            teacher.locator("#knowledge-search").click()
            expect(teacher.locator(".trial-answer-text")).to_contain_text("不在本节课允许讨论的范围")
            teacher.locator('[data-tab="records"]').click()
            expect(teacher.locator("#record-summary strong").nth(2)).to_have_text("0")
            assert errors == []
        finally:
            browser.close()
