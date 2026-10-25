from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.dependencies import require_admin
from app.portable import package_manifest, stage_upgrade, write_json
from app.routers import system
from test_portable import archive_fixture


@pytest.fixture
def upgrade_client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(system.settings, "app_dir", str(tmp_path))
    monkeypatch.setattr(system.settings, "data_dir", str(data))
    monkeypatch.setenv("EDUGATE_FROZEN", "true")
    monkeypatch.setattr("app.dependencies._client_ip", lambda request: "127.0.0.1")
    app.dependency_overrides[require_admin] = lambda: {"username": "admin", "role": "admin"}
    system.system_control.bind(lambda action: None)
    monkeypatch.setattr(system.system_control, "request", lambda action: True)
    try:
        yield TestClient(app), data
    finally:
        app.dependency_overrides.pop(require_admin, None)
        system.system_control.unbind()


def test_upgrade_http_validates_and_schedules_only_program_files(upgrade_client, tmp_path):
    client, data = upgrade_client
    (data / "secrets.json").write_text("teacher-secret")
    path = tmp_path / "release.zip"
    archive_fixture(path)
    response = client.post("/admin/system/upgrade", files={"file": ("release.zip", path.read_bytes())})
    assert response.status_code == 200
    pending = json.loads((data / "pending-upgrade.json").read_text())
    assert (data / pending["staging"] / "EduGate-Standalone.exe").exists()
    assert (data / "secrets.json").read_text() == "teacher-secret"


@pytest.mark.parametrize("contents", [b"not a zip", b"PK\x03\x04broken"])
def test_invalid_upload_does_not_stop_the_class_or_leave_a_pending_upgrade(upgrade_client, contents):
    client, data = upgrade_client
    response = client.post("/admin/system/upgrade", files={"file": ("release.zip", contents)})
    assert response.status_code == 400
    assert not (data / "pending-upgrade.json").exists()
    assert system.system_control.pending_action is None


def test_preexisting_pending_upgrade_is_not_deleted_by_another_upload(upgrade_client):
    client, data = upgrade_client
    (data / "pending-upgrade.json").write_text('{"staging":"previous"}')
    response = client.post("/admin/system/upgrade", files={"file": ("release.zip", b"bad")})
    assert response.status_code == 409
    assert json.loads((data / "pending-upgrade.json").read_text())["staging"] == "previous"


def test_failed_exit_helper_cancels_staging_without_touching_program(upgrade_client, tmp_path, monkeypatch):
    client, data = upgrade_client
    path = tmp_path / "release.zip"
    archive_fixture(path)
    monkeypatch.setattr(system.system_control, "request", lambda action: False)
    response = client.post("/admin/system/upgrade", files={"file": ("release.zip", path.read_bytes())})
    assert response.status_code == 400
    assert not (data / "pending-upgrade.json").exists()
    assert not list((data / "updates").iterdir())


def test_source_mode_cannot_install_an_executable_package(upgrade_client, monkeypatch):
    client, _ = upgrade_client
    monkeypatch.setenv("EDUGATE_FROZEN", "false")
    assert client.post("/admin/system/upgrade", files={"file": ("release.zip", b"bad")}).status_code == 409


def test_program_manifest_does_not_allow_accidental_credential_distribution(tmp_path):
    (tmp_path / "EduGate-Standalone.exe").write_bytes(b"exe")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal/runtime.txt").write_bytes(b"runtime")
    manifest = package_manifest(tmp_path, "test")
    assert set(manifest["files"]) == {"EduGate-Standalone.exe", "_internal/runtime.txt"}
    (tmp_path / "config").mkdir()
    (tmp_path / "config/edugate.env").write_text("ADMIN_PASSWORD=private")
    with pytest.raises(ValueError): package_manifest(tmp_path, "test")


@pytest.mark.skipif(os.name != "nt", reason="Windows maintenance process")
@pytest.mark.parametrize("saved", [False, True])
def test_maintenance_never_reports_success_before_a_clean_process_exit(tmp_path, saved):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".edugate.lock").write_bytes(b"0")
    write_json(data / "launcher-state.json", {"run_id": "test-run", "saved": saved, "status": "stopped" if saved else "failed"})
    parent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(2)"], creationflags=subprocess.CREATE_NO_WINDOW)
    job = {"root": str(tmp_path), "data": str(data), "pid": parent.pid, "run_id": "test-run", "action": "shutdown",
        "notice": False, "ready": str(tmp_path / "ready"), "result": str(tmp_path / "result.json")}
    process = run_helper(job)
    deadline = time.monotonic() + 8
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline: time.sleep(.03)
    assert not (tmp_path / "result.json").exists()
    parent.wait(timeout=10)
    assert process.wait(timeout=15) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == ("saved_and_exited" if saved else "failed")
    assert (data / "last-exit.json").exists() is saved


def run_helper(job):
    root = Path(__file__).resolve().parents[2]
    encoded_job = base64.b64encode(json.dumps(job).encode()).decode()
    script = "$job = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_job + "')) | ConvertFrom-Json\n" + (root / "desktop/portable_maintenance.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(script.encode("utf-16-le")).decode()
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.Popen([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-lock rollback")
def test_upgrade_rolls_back_program_entries_on_replacement_failure(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "student-work.txt").write_text("preserve this")
    (data / ".edugate.lock").write_bytes(b"0")
    write_json(data / "launcher-state.json", {"run_id": "rollback", "saved": True, "status": "stopped"})
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal/original.txt").write_text("original runtime")
    (tmp_path / "README.txt").write_text("original readme")
    (tmp_path / "EduGate-Standalone.exe").write_bytes(b"original exe")
    package = tmp_path / "release.zip"
    archive_fixture(package, {"README.txt": b"new readme"})
    stage_upgrade(package, data / "staged")
    write_json(data / "pending-upgrade.json", {"staging": "staged"})
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(tmp_path / "README.txt"), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        parent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(2)"], creationflags=subprocess.CREATE_NO_WINDOW)
        job = {"root": str(tmp_path), "data": str(data), "pid": parent.pid, "run_id": "rollback", "action": "upgrade",
            "notice": False, "ready": str(tmp_path / "ready"), "result": str(tmp_path / "result.json"), "no_browser": True}
        process = run_helper(job)
        parent.wait(timeout=10)
        process.wait(timeout=20)
    finally:
        kernel.CloseHandle(handle)
    outcome = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "failed"
    assert (tmp_path / "_internal/original.txt").read_text() == "original runtime"
    assert (tmp_path / "README.txt").read_text() == "original readme"
    assert (tmp_path / "EduGate-Standalone.exe").read_bytes() == b"original exe"
    assert (data / "student-work.txt").read_text() == "preserve this"
    assert not (data / "pending-upgrade.json").exists()
