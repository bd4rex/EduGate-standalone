from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import (
    ModelCatalogItem,
    RuntimeConfig,
    RuntimeConfigData,
    ScenarioUpdateRequest,
    TeachingScenario,
    _sync_portable_admin_password,
    _stream_with_heartbeat,
    app,
    business_db,
    classroom_access,
    knowledge_store,
    rate_limiter,
    runtime_config,
    secret_store,
    sessions,
    settings,
    student_sessions,
)


ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        if not business_db.is_admin_initialized(settings.admin_username):
            business_db.setup_admin(
                username=settings.admin_username,
                password=ADMIN_PASSWORD,
                display_name="Test Administrator",
            )
        yield test_client


@pytest.fixture(autouse=True)
def isolate_runtime_config() -> Iterator[None]:
    snapshot = runtime_config.data.model_copy(deep=True)
    with rate_limiter._lock:
        rate_limiter._events.clear()
    classroom_access.start()
    student_sessions.revoke_all()
    yield
    classroom_access.start()
    student_sessions.revoke_all()
    runtime_config.data = snapshot
    runtime_config.save()


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    token = sessions.issue(settings.admin_username)
    return {"X-Admin-Token": token}


@pytest.fixture()
def classroom_headers() -> dict[str, str]:
    return {"X-Class-Token": classroom_access.token()}


def test_health_and_frontend_are_served_from_one_origin(client: TestClient) -> None:
    health = client.get("/health")
    assert health.json() == {"status": "ok"}
    assert health.headers["X-EduGate-App"] == "EduGate"
    page = client.get("/student.html")
    assert page.status_code == 200
    assert "window.location.origin" in page.text


def test_lan_cors_preflight_is_not_required(client: TestClient) -> None:
    response = client.options(
        "/chat",
        headers={
            "Origin": "http://192.168.1.25:8000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code != 400
    assert "Disallowed CORS origin" not in response.text


def test_remote_first_setup_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main._is_loopback", lambda _: False)
    monkeypatch.setattr(business_db, "is_admin_initialized", lambda _: False)
    response = client.post(
        "/auth/setup",
        json={"username": "admin", "password": "a-secure-password"},
    )
    assert response.status_code == 403


def test_login_uses_database_password_and_returns_expiring_session(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        json={"username": settings.admin_username, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["expires_in"] == settings.session_ttl_seconds
    assert data["teacher"]["role"] == "admin"


def test_portable_local_session_opens_teacher_console_without_password(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "portable_mode", True)
    monkeypatch.setattr(settings, "portable_auto_login", True)
    monkeypatch.setattr("app.main._is_loopback", lambda _: True)

    response = client.post("/auth/local-session")

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["teacher"]["username"] == settings.admin_username


def test_portable_local_session_is_rejected_for_student_devices(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "portable_mode", True)
    monkeypatch.setattr(settings, "portable_auto_login", True)
    monkeypatch.setattr("app.main._is_loopback", lambda _: False)

    response = client.post("/auth/local-session")

    assert response.status_code == 403


def test_portable_primary_password_is_kept_in_the_folder_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config" / "edugate.env"
    monkeypatch.setattr(settings, "portable_mode", True)
    monkeypatch.setattr(settings, "config_path", str(config_path))
    monkeypatch.setattr(settings, "admin_password", "old-password")

    _sync_portable_admin_password(settings.admin_username, "new classroom password")

    assert "ADMIN_PASSWORD='new classroom password'" in config_path.read_text(encoding="utf-8")
    assert settings.admin_password == "new classroom password"


def test_config_requires_teacher_session(client: TestClient) -> None:
    assert client.get("/config").status_code == 401


def test_logout_revokes_session(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.get("/config", headers=admin_headers).status_code == 200
    assert client.post("/auth/logout", headers=admin_headers).status_code == 200
    assert client.get("/config", headers=admin_headers).status_code == 401


def test_chat_requires_current_classroom_token(client: TestClient) -> None:
    response = client.post("/chat", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 401


def test_student_sessions_rate_limit_students_independently_behind_one_ip(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr(settings, "classroom_rate_limit", 1)
    class_headers = {"X-Class-Token": classroom_access.token()}
    first = client.post("/classroom/join", headers=class_headers).json()["student_token"]
    second = client.post("/classroom/join", headers=class_headers).json()["student_token"]
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    first_response = client.post("/chat", headers={"X-Student-Token": first}, json=payload)
    second_response = client.post("/chat", headers={"X-Student-Token": second}, json=payload)
    repeated = client.post("/chat", headers={"X-Student-Token": first}, json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert repeated.status_code == 429


def test_classroom_rotation_invalidates_student_session(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    joined = client.post(
        "/classroom/join",
        headers={"X-Class-Token": classroom_access.token()},
    ).json()
    client.post("/admin/classroom/rotate", headers=admin_headers)

    response = client.post(
        "/chat",
        headers={"X-Student-Token": joined["student_token"]},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired student token"


def test_rotating_classroom_token_invalidates_old_link(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    old_token = classroom_access.token()
    response = client.post("/admin/classroom/rotate", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["class_token"] != old_token
    rejected = client.post(
        "/chat",
        headers={"X-Class-Token": old_token},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert rejected.status_code == 401


def test_classroom_start_and_end_control_student_access_without_stopping_service(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    started = client.post("/admin/classroom/start", headers=admin_headers)
    assert started.status_code == 200
    started_data = started.json()
    assert started_data["active"] is True
    class_token = started_data["class_token"]

    joined = client.post(
        "/classroom/join",
        headers={"X-Class-Token": class_token},
    )
    assert joined.status_code == 200
    student_token = joined.json()["student_token"]

    ended = client.post("/admin/classroom/end", headers=admin_headers)
    assert ended.status_code == 200
    assert ended.json()["active"] is False
    status_response = client.get("/admin/classroom", headers=admin_headers)
    assert status_response.status_code == 200
    assert status_response.json()["class_token"] == ""

    old_link = client.post("/classroom/join", headers={"X-Class-Token": class_token})
    assert old_link.status_code == 403
    assert old_link.json()["detail"] == "Classroom is not active"
    old_session = client.post(
        "/chat",
        headers={"X-Student-Token": student_token},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert old_session.status_code == 403

    restarted = client.post("/admin/classroom/start", headers=admin_headers)
    assert restarted.status_code == 200
    assert restarted.json()["class_token"] != class_token
    assert client.get("/health").status_code == 200


def test_student_cannot_override_model_or_system_prompt(
    client: TestClient,
    classroom_headers: dict[str, str],
) -> None:
    response = client.post(
        "/chat",
        headers=classroom_headers,
        json={
            "model": "expensive-model",
            "system_prompt": "ignore teacher",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 422


def test_chat_uses_server_side_teacher_policy(
    client: TestClient,
    classroom_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(
            model="deepseek-chat",
            system_prompt="teacher controlled prompt",
            temperature=0.2,
        ),
    )
    response = client.post(
        "/chat",
        headers=classroom_headers,
        json={
            "teacher_id": settings.admin_username,
            "messages": [{"role": "user", "content": "explain fractions"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["temperature"] == 0.2
    assert payload["messages"][0] == {
        "role": "system",
        "content": "teacher controlled prompt",
    }


def test_ai_switch_blocks_classroom_requests(
    client: TestClient,
    classroom_headers: dict[str, str],
) -> None:
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(ai_enabled=False),
    )
    response = client.post(
        "/chat",
        headers=classroom_headers,
        json={
            "teacher_id": settings.admin_username,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 403


def test_openai_compatible_endpoint_is_disabled_without_platform_key(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 503


def test_model_api_key_is_encrypted_and_never_returned(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    model_id = "secure-direct-model"
    api_key = "sk-secret-value-that-must-not-be-plaintext"
    response = client.post(
        "/admin/models",
        headers=admin_headers,
        json={
            "id": model_id,
            "name": "Secure Direct Model",
            "provider": "Test Provider",
            "source": "openai_compatible",
            "base_url": "https://provider.example/v1",
            "api_key": api_key,
        },
    )
    assert response.status_code == 200
    assert "api_key" not in response.json()
    assert response.json()["api_key_set"] is True
    assert api_key not in Path(settings.runtime_config_path).read_text(encoding="utf-8")
    assert api_key not in Path(settings.secret_store_path).read_text(encoding="utf-8")
    assert secret_store.get(f"model:{model_id}") == api_key
    runtime_config.delete_model(model_id)


def test_direct_model_routes_with_decrypted_key(
    client: TestClient,
    classroom_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_direct(
        *, base_url: str, api_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"base_url": base_url, "api_key": api_key, "payload": payload}

    monkeypatch.setattr("app.main.client.openai_chat_completion", fake_direct)
    runtime_config.upsert_model(
        ModelCatalogItem(
            id="direct-test",
            name="Direct Test",
            provider="Test",
            source="openai_compatible",
            base_url="https://provider.example/v1/chat/completions",
            api_key="direct-secret",
        )
    )
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(model="direct-test"),
    )
    response = client.post(
        "/chat",
        headers=classroom_headers,
        json={
            "teacher_id": settings.admin_username,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["api_key"] == "direct-secret"


def test_provider_test_performs_real_probe(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config.upsert_model(
        ModelCatalogItem(
            id="probe-model",
            name="Probe Model",
            provider="Test",
            source="openai_compatible",
            base_url="https://provider.example/custom/v1",
            api_key="probe-secret",
        )
    )
    observed: dict[str, str] = {}

    async def fake_probe(*, base_url: str, api_key: str) -> dict[str, Any]:
        observed.update(base_url=base_url, api_key=api_key)
        return {"ok": True, "model_count": 4}

    monkeypatch.setattr("app.main.client.probe_openai_provider", fake_probe)
    response = client.post("/admin/providers/probe-model/test", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["model_count"] == 4
    assert observed == {
        "base_url": "https://provider.example/custom/v1",
        "api_key": "probe-secret",
    }


def test_provider_models_can_be_discovered_and_batch_imported(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_keys: list[str] = []

    async def fake_list(*, base_url: str, api_key: str) -> list[dict[str, str]]:
        assert base_url == "https://provider.example/custom/v1"
        observed_keys.append(api_key)
        return [
            {"id": "deepseek-v4-flash-0731", "owned_by": "test"},
            {"id": "qwen-max", "owned_by": "test"},
        ]

    monkeypatch.setattr("app.main.client.list_openai_models", fake_list)
    connection = {
        "provider": "Test Provider",
        "base_url": "https://provider.example/custom/v1",
        "api_key": "batch-secret",
    }
    discovered = client.post(
        "/admin/models/discover",
        headers=admin_headers,
        json=connection,
    )
    imported = client.post(
        "/admin/models/batch-import",
        headers=admin_headers,
        json={
            **connection,
            "model_ids": ["qwen-max", "deepseek-v4-flash-0731"],
            "display_names": {"qwen-max": "通义千问 Max"},
        },
    )

    assert discovered.status_code == 200
    assert discovered.json()["model_count"] == 2
    assert imported.status_code == 200
    assert imported.json()["imported_count"] == 2
    assert all(model["api_key_set"] for model in imported.json()["models"])
    assert next(model for model in imported.json()["models"] if model["id"] == "qwen-max")["name"] == "通义千问 Max"
    assert secret_store.get("model:qwen-max") == "batch-secret"

    renamed = client.patch(
        "/admin/models/qwen-max",
        headers=admin_headers,
        json={
            "id": "qwen-max",
            "name": "课堂用千问 Max",
            "provider": "Test Provider",
            "description": "Renamed after import",
            "source": "openai_compatible",
            "base_url": "https://provider.example/custom/v1",
            "api_key": None,
        },
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "课堂用千问 Max"
    assert secret_store.get("model:qwen-max") == "batch-secret"

    reused = client.post(
        "/admin/models/discover",
        headers=admin_headers,
        json={
            "provider": "Test Provider",
            "base_url": "https://provider.example/custom/v1",
            "credential_model_id": "qwen-max",
        },
    )
    assert reused.status_code == 200
    assert reused.json()["used_saved_api_key"] is True
    assert observed_keys == ["batch-secret", "batch-secret", "batch-secret"]
    runtime_config.delete_model("qwen-max")
    runtime_config.delete_model("deepseek-v4-flash-0731")


def test_completed_runtime_migration_does_not_restore_legacy_default(tmp_path: Path) -> None:
    path = tmp_path / "runtime_config.json"
    data = RuntimeConfigData(
        scenarios={"default": TeachingScenario(model="replacement-model")},
        legacy_runtime_migration_complete=True,
    )
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")

    reloaded = RuntimeConfig(str(path))

    assert reloaded.data.scenarios["default"].model == "replacement-model"
    assert settings.default_model not in reloaded.data.model_catalog


def test_regular_teacher_cannot_manage_models_but_can_control_own_policy(
    client: TestClient,
) -> None:
    business_db.upsert_teacher(
        username="teacher-one",
        password="teacher-password",
        display_name="Teacher One",
        role="teacher",
    )
    headers = {"X-Admin-Token": sessions.issue("teacher-one")}
    blocked = client.post(
        "/admin/models",
        headers=headers,
        json={"id": "blocked", "name": "Blocked", "provider": "Test"},
    )
    allowed = client.post(
        "/config/ai",
        headers=headers,
        json={"enabled": False},
    )
    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["scenarios"]["default"]["ai_enabled"] is False


def test_python_runner_is_disabled_by_default(
    client: TestClient,
    classroom_headers: dict[str, str],
) -> None:
    response = client.post(
        "/run_python",
        headers=classroom_headers,
        json={"code": "print(1)"},
    )
    assert response.status_code == 503


def test_python_runner_requires_classroom_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "python_runner_enabled", True)
    response = client.post("/run_python", json={"code": "print(1)"})
    assert response.status_code == 401


def test_python_runner_blocks_imports(
    client: TestClient,
    classroom_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "python_runner_enabled", True)
    response = client.post(
        "/run_python",
        headers=classroom_headers,
        json={"code": "import os\nprint(os.listdir('.'))"},
    )
    assert response.status_code == 200
    assert response.json()["exit_code"] == 1
    assert "Import" in response.json()["stderr"]


def test_python_runner_unavailable_is_reported_as_503(
    client: TestClient,
    classroom_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.python_runner import PythonRunnerUnavailable

    def unavailable(*args, **kwargs):
        raise PythonRunnerUnavailable("separate interpreter required")

    monkeypatch.setattr(settings, "python_runner_enabled", True)
    monkeypatch.setattr("app.main.run_python_code", unavailable)
    response = client.post(
        "/run_python",
        headers=classroom_headers,
        json={"code": "print(1)"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "separate interpreter required"


def test_python_runner_stream_reports_queue_output_and_result(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.python_runner import PythonRunResult

    def fake_runner(code: str, *, on_output=None, **kwargs) -> PythonRunResult:
        assert on_output is not None
        on_output("stdout", "2\n")
        return PythonRunResult("2\n", "", 0, False, 5)

    monkeypatch.setattr(settings, "python_runner_enabled", True)
    monkeypatch.setattr("app.main.run_python_code", fake_runner)
    joined = client.post(
        "/classroom/join",
        headers={"X-Class-Token": classroom_access.token()},
    ).json()
    response = client.post(
        "/run_python/stream",
        headers={"X-Student-Token": joined["student_token"]},
        json={"code": "print(1 + 1)", "teacher_id": settings.admin_username},
    )

    assert response.status_code == 200
    assert "event: queued" in response.text
    assert "event: running" in response.text
    assert "event: stdout" in response.text
    assert '"content": "2\\n"' in response.text
    assert "event: done" in response.text
    matching_turns = []
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not matching_turns:
        records = client.get("/teacher/classroom-records", headers=admin_headers).json()["records"]
        for record in records:
            detail = client.get(
                f"/teacher/classroom-records/{record['id']}",
                headers=admin_headers,
            ).json()
            matching_turns.extend(
                turn for turn in detail["turns"] if turn["input_content"] == "print(1 + 1)"
            )
        if not matching_turns:
            time.sleep(0.01)
    assert len(matching_turns) == 1
    assert matching_turns[0]["kind"] == "python"
    assert matching_turns[0]["output_content"] == "2\n"


def test_teacher_can_view_only_owned_identified_classroom_records(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher_username = "record-teacher"
    business_db.upsert_teacher(
        username=teacher_username,
        password="record-teacher-password",
        display_name="Record Teacher",
        role="teacher",
    )
    runtime_config.update_teacher_policy(
        teacher_username,
        ScenarioUpdateRequest(model="record-model", system_prompt="record policy"),
    )

    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "记录里的回答"}}]}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main._client_ip", lambda _: "192.168.10.27")
    join_response = client.post(
        "/classroom/join",
        headers={"X-Class-Token": classroom_access.token()},
        json={"device_id": "classroom-device-LAB027"},
    )
    assert join_response.status_code == 200
    joined = join_response.json()
    assert joined["computer_name"] == "电脑-LAB027"
    assert joined["client_ip"] == "192.168.10.27"
    repeated_join = client.post(
        "/classroom/join",
        headers={"X-Class-Token": classroom_access.token()},
        json={"device_id": "classroom-device-LAB027"},
    ).json()
    assert repeated_join["student_session_id"] == joined["student_session_id"]
    response = client.post(
        "/chat",
        headers={"X-Student-Token": joined["student_token"]},
        json={
            "teacher_id": teacher_username,
            "messages": [{"role": "user", "content": "记录里的问题"}],
        },
    )
    assert response.status_code == 200

    teacher_headers = {"X-Admin-Token": sessions.issue(teacher_username)}
    own_records = client.get("/teacher/classroom-records", headers=teacher_headers).json()["records"]
    assert own_records
    assert {record["teacher_username"] for record in own_records} == {teacher_username}
    detail = client.get(
        f"/teacher/classroom-records/{own_records[0]['id']}",
        headers=teacher_headers,
    ).json()
    assert detail["turns"][-1]["input_content"] == "记录里的问题"
    assert detail["turns"][-1]["output_content"] == "记录里的回答"
    assert detail["turns"][-1]["student_session_id"] == joined["student_session_id"]
    assert detail["turns"][-1]["computer_name"] == "电脑-LAB027"
    assert detail["turns"][-1]["client_ip"] == "192.168.10.27"

    admin_records = client.get("/teacher/classroom-records", headers=admin_headers).json()["records"]
    assert any(record["teacher_username"] == teacher_username for record in admin_records)
    other_teacher = business_db.upsert_teacher(
        username="record-other",
        password="record-other-password",
        display_name="Other",
        role="teacher",
    )
    assert other_teacher
    other_headers = {"X-Admin-Token": sessions.issue("record-other")}
    assert client.get(
        f"/teacher/classroom-records/{own_records[0]['id']}",
        headers=other_headers,
    ).status_code == 404
    assert client.delete(
        f"/teacher/classroom-records/{own_records[0]['id']}",
        headers=teacher_headers,
    ).status_code == 200
    assert client.get(
        f"/teacher/classroom-records/{own_records[0]['id']}",
        headers=teacher_headers,
    ).status_code == 404


def test_classroom_content_recording_can_be_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher_username = "recording-disabled"
    business_db.upsert_teacher(
        username=teacher_username,
        password="recording-disabled-password",
        display_name="Disabled Recording",
        role="teacher",
    )

    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "不应保存"}}]}

    monkeypatch.setattr(settings, "classroom_recording_enabled", False)
    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    response = client.post(
        "/chat",
        headers={"X-Class-Token": classroom_access.token()},
        json={
            "teacher_id": teacher_username,
            "messages": [{"role": "user", "content": "不要保存"}],
        },
    )
    assert response.status_code == 200
    teacher_headers = {"X-Admin-Token": sessions.issue(teacher_username)}
    assert client.get("/teacher/classroom-records", headers=teacher_headers).json()["records"] == []


def test_streamed_chat_is_saved_as_one_classroom_turn(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_stream(payload: dict[str, Any]):
        first_event = 'data: {"choices":[{"delta":{"content":"实时"}}]}\n\n'.encode()
        split_at = first_event.index("实".encode()) + 1
        yield first_event[:split_at]
        yield first_event[split_at:]
        yield 'data: {"choices":[{"delta":{"content":"回答"}}]}\n\n'.encode()
        yield b"data: [DO"
        yield b"NE]\n\n"

    monkeypatch.setattr("app.main._stream_chat_completion", fake_stream)
    joined = client.post(
        "/classroom/join",
        headers={"X-Class-Token": classroom_access.token()},
    ).json()
    response = client.post(
        "/chat/stream",
        headers={"X-Student-Token": joined["student_token"]},
        json={
            "teacher_id": settings.admin_username,
            "messages": [{"role": "user", "content": "流式问题"}],
        },
    )
    assert response.status_code == 200
    records = client.get("/teacher/classroom-records", headers=admin_headers).json()["records"]
    matching_turns = []
    for record in records:
        detail = client.get(
            f"/teacher/classroom-records/{record['id']}",
            headers=admin_headers,
        ).json()
        matching_turns.extend(
            turn for turn in detail["turns"] if turn["input_content"] == "流式问题"
        )
    assert len(matching_turns) == 1
    assert matching_turns[0]["output_content"] == "实时回答"


def test_stream_heartbeat_is_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(settings, "stream_heartbeat_seconds", 0.01)

    async def slow_stream():
        await asyncio.sleep(0.03)
        yield b"data: [DONE]\n\n"

    async def collect() -> list[bytes]:
        chunks = []
        async for chunk in _stream_with_heartbeat(slow_stream()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())
    assert b": edugate-keep-alive\n\n" in chunks
    assert chunks[-1] == b"data: [DONE]\n\n"


def test_runtime_config_is_valid_json_after_updates() -> None:
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(system_prompt="atomic write test"),
    )
    data = json.loads(Path(settings.runtime_config_path).read_text(encoding="utf-8"))
    assert data["teacher_policies"][settings.admin_username]["system_prompt"] == "atomic write test"


def test_model_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import app.main as main_module

    active = 0
    peak = 0

    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return payload

    monkeypatch.setattr(main_module.client, "chat_completion", fake_chat_completion)

    async def exercise() -> None:
        limiter = main_module.ModelConcurrencyLimiter(2)
        monkeypatch.setattr(main_module, "model_semaphore", limiter)
        tasks = [
            asyncio.create_task(main_module._chat_completion({"model": "concurrency-test"}))
            for _ in range(64)
        ]
        await asyncio.sleep(0.005)
        assert limiter.stats() == {"running": 2, "waiting": 62, "capacity": 2}
        await asyncio.gather(*tasks)
        assert limiter.stats() == {"running": 0, "waiting": 0, "capacity": 2}

    asyncio.run(exercise())
    assert peak == 2


def test_referenced_model_can_be_replaced_while_knowledge_source_stays_protected(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    model_id = "referenced-model"
    replacement_id = "replacement-model"
    source_id = "referenced-source"
    runtime_config.upsert_models(
        [
            ModelCatalogItem(
                id=model_id,
                name="Referenced Model",
                provider="Test",
                source="openai_compatible",
                base_url="https://provider.example/v1",
                api_key="test-secret",
            ),
            ModelCatalogItem(
                id=replacement_id,
                name="Replacement Model",
                provider="Test",
                source="openai_compatible",
                base_url="https://provider.example/v1",
                api_key="replacement-secret",
            ),
        ]
    )
    source_response = client.post(
        "/knowledge/sources",
        headers=admin_headers,
        json={"id": source_id, "name": "Referenced Source"},
    )
    assert source_response.status_code == 200
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(model=model_id, knowledge_source_id=source_id),
    )
    runtime_config.update_scenario("default", ScenarioUpdateRequest(model=model_id))

    model_response = client.delete(f"/model-catalog/{model_id}", headers=admin_headers)
    replaced_response = client.delete(
        f"/model-catalog/{model_id}?replacement_model_id={replacement_id}",
        headers=admin_headers,
    )
    source_response = client.delete(f"/knowledge/sources/{source_id}", headers=admin_headers)

    assert model_response.status_code == 409
    assert replaced_response.status_code == 200
    assert set(replaced_response.json()["replaced_references"]) == {
        "default",
        f"teacher:{settings.admin_username}",
    }
    assert runtime_config.data.scenarios["default"].model == replacement_id
    assert runtime_config.get_teacher_policy(settings.admin_username).model == replacement_id
    assert model_id not in runtime_config.data.model_catalog
    assert secret_store.get(f"model:{model_id}") is None
    assert source_response.status_code == 409
    runtime_config.update_teacher_policy(
        settings.admin_username,
        ScenarioUpdateRequest(model=settings.default_model, knowledge_source_id=None),
    )
    runtime_config.update_scenario("default", ScenarioUpdateRequest(model=settings.default_model))
    runtime_config.delete_model(replacement_id)
    knowledge_store.delete_source(source_id)


def test_system_management_requires_supervised_launcher(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    status_response = client.get("/admin/system/status", headers=admin_headers)
    action_response = client.post(
        "/admin/system/action",
        headers=admin_headers,
        json={"action": "restart"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["supervised"] is False
    assert status_response.json()["model_pool"] == {
        "running": 0,
        "waiting": 0,
        "capacity": settings.model_max_concurrency,
    }
    assert status_response.json()["database_writer"]["enabled"] is True
    assert status_response.json()["database_writer"]["queue_capacity"] >= 128
    assert action_response.status_code == 409


def test_admin_can_open_fixed_application_directory(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[bool] = []

    def fake_open_app_directory() -> dict[str, str]:
        opened.append(True)
        return {"status": "opened", "path": "C:\\EduGate"}

    monkeypatch.setattr("app.main.open_app_directory", fake_open_app_directory)
    response = client.post("/admin/system/open-app-dir", headers=admin_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "opened", "path": "C:\\EduGate"}
    assert opened == [True]


def test_admin_can_open_fixed_knowledge_source_directory(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "general"
    source_dir.mkdir()
    opened: list[Path] = []

    monkeypatch.setattr(knowledge_store, "source_directory", lambda source_id: source_dir)

    def fake_open_local_directory(path: Path, *, missing_detail: str) -> dict[str, str]:
        opened.append(path)
        return {"status": "opened", "path": str(path)}

    monkeypatch.setattr("app.main.open_local_directory", fake_open_local_directory)
    response = client.post("/knowledge/sources/general/open-folder", headers=admin_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "opened", "path": str(source_dir)}
    assert opened == [source_dir]


def test_admin_can_scan_knowledge_source_directory(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_store,
        "scan_source",
        lambda source_id: {
            "source_id": source_id,
            "added": 2,
            "updated": 1,
            "removed": 0,
            "unchanged": 3,
            "skipped": 1,
            "errors": [],
        },
    )

    response = client.post("/knowledge/sources/general/scan", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["added"] == 2
    assert response.json()["unchanged"] == 3


def test_platform_key_is_managed_in_encrypted_store(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.put(
        "/admin/system/platform-key",
        headers=admin_headers,
        json={"api_key": "platform-secret"},
    )
    assert response.status_code == 200
    assert secret_store.get("system:platform_api_key") == "platform-secret"
    client.put(
        "/admin/system/platform-key",
        headers=admin_headers,
        json={"api_key": None},
    )
