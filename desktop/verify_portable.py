"""Real-process portable acceptance checks with isolated teachers and a mock API.

No production profile or external model is used. --package must be a fresh,
data-free release folder. Results and synthetic profiles are kept for inspection.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.send_response(200)
        if request.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"portable classroom verified"}}]}\n\ndata: [DONE]\n\n')
        else:
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "portable classroom verified"}}]}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)


def wait_until(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result: return result
        except (OSError, ValueError, KeyError, httpx.HTTPError): pass
        time.sleep(.2)
    raise AssertionError("Timed out waiting for portable acceptance condition")


def read_json(path): return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--upgrade", type=Path)
    parser.add_argument("--keep-running", action="store_true", help="Leave a synthetic packaged classroom open for browser QA")
    parser.add_argument("--coverage", action="store_true", help="Collect source subprocess coverage (not frozen EXEs)")
    parser.add_argument("--with-model", action="store_true", help="Use the already prepared, offline build model for source acceptance")
    args = parser.parse_args()
    work = ROOT / "artifacts" / ("portable-acceptance-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    work.mkdir(parents=True, exist_ok=False)
    if args.package and any((args.package / name).exists() for name in ("data", "config")):
        raise SystemExit("Refusing to copy a package containing user data/config")
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        preferred_port = sock.getsockname()[1]
    fake_legacy = work / "computer-appdata" / "EduGate"
    fake_legacy.mkdir(parents=True)
    (fake_legacy / "runtime_config.json").write_text('{"foreign_teacher":true}', encoding="utf-8")
    poison = work / "foreign-data-do-not-touch"
    poison.mkdir()
    (poison / "sentinel").write_text("unchanged")
    env = {**os.environ, "LOCALAPPDATA": str(fake_legacy.parent), "EDUGATE_DATA_DIR": str(poison),
        "EDUGATE_CONFIG_PATH": str(poison / "wrong.env"), "SECRET_STORE_PATH": str(poison / "wrong-secrets.json"),
        "PLATFORM_API_KEY": "foreign-platform-key", "UPSTREAM_API_KEY": "foreign-upstream-key"}
    if args.package:
        env["PATH"] = os.pathsep.join([os.environ["SystemRoot"], str(Path(os.environ["SystemRoot"]) / "System32")])
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    processes = []
    checks = []

    def check(label, condition):
        assert condition, label
        checks.append(label)
        print("PASS: " + label, flush=True)

    def make_profile(name):
        directory = work / name
        if args.package: shutil.copytree(args.package, directory)
        (directory / "config").mkdir(parents=True)
        values = {"EDUGATE_BACKEND_PORT": str(preferred_port), "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "portable-test-password",
            "PORTABLE_AUTO_LOGIN": "true", "ALLOW_LAN_ADMIN": "false", "UPSTREAM_PROVIDER": name,
            "UPSTREAM_BASE_URL": f"http://127.0.0.1:{provider.server_port}/v1", "UPSTREAM_API_KEY": "test-only-" + hashlib.sha256(name.encode()).hexdigest()[:12],
            "DEFAULT_MODEL": "portable-mock", "KNOWLEDGE_SEMANTIC_ENABLED": "true" if args.package or args.with_model else "false",
            "LAN_ADVERTISE_IP": "192.0.2.111", "PYTHON_RUNNER_ENABLED": "true"}
        (directory / "config/edugate.env").write_text("\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()), encoding="utf-8")
        if args.with_model and not args.package:
            shutil.copytree(ROOT / "build/knowledge-model", directory / "data/knowledge_models")
        return directory

    def command(directory):
        if args.package: return [str(directory / "EduGate-Standalone.exe"), "--no-browser", "--no-exit-notice"]
        prefix = [sys.executable]
        if args.coverage:
            prefix += ["-m", "coverage", "run", "--parallel-mode", "--branch", "--source", str(ROOT / "backend/app"), "--data-file", str(work / ".coverage")]
        return prefix + [str(ROOT / "desktop/edugate_standalone.py"), "--portable-dir", str(directory), "--no-browser", "--no-exit-notice"]

    def spawn(directory):
        output = (work / ("process-" + str(len(processes)) + ".log")).open("w", encoding="utf-8")
        process = subprocess.Popen(command(directory), cwd=work, env=env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        output.close()
        processes.append(process)
        return process

    def connect(directory):
        info = wait_until(lambda: (lambda v: v if v.get("status") == "running" else None)(read_json(directory / "data/launcher-state.json")), 120)
        client = httpx.Client(base_url=f"http://127.0.0.1:{info['port']}", trust_env=False, timeout=90)
        token = client.post("/auth/local-session").json()["access_token"]
        client.headers["X-Admin-Token"] = token
        return client, info

    def stop(client, directory, process):
        run_id = read_json(directory / "data/launcher-state.json")["run_id"]
        response = client.post("/admin/system/action", json={"action": "shutdown"})
        assert response.status_code == 200, response.text
        process.wait(timeout=120)
        assert process.returncode == 0, (directory / "data/launcher.log").read_text(encoding="utf-8")[-4000:]
        if os.name == "nt":
            receipt = wait_until(lambda: (lambda v: v if v.get("run_id") == run_id else None)(read_json(directory / "data/last-exit.json")), 15)
            assert receipt["status"] == "saved_and_exited"
        client.close()

    try:
        a, b = make_profile("U盘 E 老师甲"), make_profile("U盘 老师乙")
        pa = spawn(a)
        ca, ia = connect(a)
        check("cold launch from a Chinese path and unrelated working directory", ca.get("/health").status_code == 200)
        check("foreign machine data and credentials are not imported", list(poison.iterdir()) == [poison / "sentinel"] and not (a / "data/.imported-from-localappdata").exists())
        check("stale LAN IP is not advertised on the new computer", ca.get("/admin/system/status").json()["local_ip"] != "192.0.2.111")
        if args.package:
            check("offline embedding runtime loads from the package without Python on PATH", (a / "data/knowledge_models/model.json").is_file() and ca.get("/knowledge/status").json()["model_ready"])
        duplicate = spawn(a)
        check("double-click reuses only the same portable instance", duplicate.wait(timeout=25) == 0 and read_json(a / "data/launcher-state.json")["run_id"] == ia["run_id"])
        pb = spawn(b)
        cb, ib = connect(b)
        check("two teachers use different ports and identities", ia["port"] != ib["port"] and ia["instance_id"] != ib["instance_id"])
        ca.put("/admin/system/platform-key", json={"api_key": "teacher-a-portable-platform-key"}).raise_for_status()
        ca.put("/config/scenarios/default", json={"system_prompt": "Teacher A portable lesson", "knowledge_source_id": "general"}).raise_for_status()
        ca.post("/knowledge/files", data={"source_id": "general"}, files={"file": ("teacher-a.txt", "老师甲的教材：IPv4 地址用于网络通信。".encode(), "text/plain")}).raise_for_status()
        page = ca.post("/admin/published-pages", files={"file": ("lesson.html", b"<html><head><title>Teacher A</title></head><body>Teacher A portable page</body></html>", "text/html")}, data={"title": "Teacher A", "activate": "true"})
        page.raise_for_status()
        classroom = ca.post("/admin/classroom/start").json()
        student = ca.post("/classroom/join", headers={"X-Class-Token": classroom["class_token"]}, json={"device_id": "acceptance-student"}).json()
        response = ca.post("/chat/stream", headers={"X-Student-Token": student["student_token"]}, json={"messages": [{"role": "user", "content": "What is IPv4?"}]})
        check("student streaming response reaches DONE", response.status_code == 200 and "[DONE]" in response.text and "portable classroom verified" in response.text)
        check("another teacher cannot see the lesson or knowledge files", cb.get("/config").json()["scenarios"]["default"]["system_prompt"] != "Teacher A portable lesson" and cb.get("/knowledge/files").json() == [])
        stop(ca, a, pa)
        check("shutdown is confirmed after process exit and queued records flush", read_json(a / "data/launcher-state.json")["saved"] is True)
        secret_bytes = (a / "data/secrets.json").read_bytes()
        new_a = work / "U盘 F 老师甲 新电脑"
        assert a.resolve().is_relative_to(work.resolve()) and new_a.resolve().is_relative_to(work.resolve())
        a.rename(new_a)
        a = new_a
        pa = spawn(a)
        ca, moved = connect(a)
        check("folder/drive-style relocation keeps identity and credentials", moved["instance_id"] == ia["instance_id"] and (a / "data/secrets.json").read_bytes() == secret_bytes)
        check("relocation preserves knowledge, page, policy and records", len(ca.get("/knowledge/files").json()) == 1 and len(ca.get("/admin/published-pages").json()["pages"]) == 1 and len(ca.get("/admin/classroom-records").json()["records"]) == 1 and ca.get("/config").json()["scenarios"]["default"]["system_prompt"] == "Teacher A portable lesson")
        if args.upgrade:
            with args.upgrade.open("rb") as archive:
                response = ca.post("/admin/system/upgrade", files={"file": (args.upgrade.name, archive, "application/zip")})
            assert response.status_code == 200, response.text
            pa.wait(timeout=120)
            ca.close()
            outcome = wait_until(lambda: read_json(a / "data/last-upgrade.json"), 120)
            check("offline upgrade replaces program and saves previous program", outcome["status"] == "upgraded" and Path(outcome["previous_program"]).is_dir())
            # The updater starts the new EXE; inspect its own state, never another teacher's health endpoint.
            wait_until(lambda: (lambda s: s.get("status") == "running" and s.get("run_id") != moved["run_id"])(read_json(a / "data/launcher-state.json")), 120)
            ca, upgraded = connect(a)
            check("upgrade preserves teacher key, lesson, files and classroom history", (a / "data/secrets.json").read_bytes() == secret_bytes and len(ca.get("/knowledge/files").json()) == 1 and len(ca.get("/admin/classroom-records").json()["records"]) == 1 and ca.get("/config").json()["scenarios"]["default"]["system_prompt"] == "Teacher A portable lesson")
            import psutil
            pa = psutil.Process(upgraded["pid"])
            # psutil's wait returns an exit status; normalize to the subprocess shape below.
            class Running:
                returncode = None
                def wait(self, timeout):
                    self.returncode = pa.wait(timeout=timeout)
                    return self.returncode
            exit_process = Running()
        else:
            exit_process = pa
        stop(cb, b, pb)
        if args.keep_running:
            info = read_json(a / "data/launcher-state.json")
            print("BROWSER_QA=" + f"http://127.0.0.1:{info['port']}/admin.html", flush=True)
            print("PROFILE=" + str(a), flush=True)
            # Provider can be omitted for UI-only acceptance after this command exits.
        else:
            stop(ca, a, exit_process)
        (work / "results.json").write_text(json.dumps({"passed": checks, "package": str(args.package or "source"), "profile": str(a)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("RESULTS=" + str(work / "results.json"), flush=True)
    finally:
        provider.shutdown()
        provider.server_close()
        if not args.keep_running:
            for process in processes:
                if process.poll() is None:
                    import psutil
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                        for child in children: child.terminate()
                        process.terminate()
                    except psutil.Error: pass


if __name__ == "__main__": main()
