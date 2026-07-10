from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import (
    ModelCatalogItem,
    ScenarioUpdateRequest,
    _stream_with_heartbeat,
    app,
    business_db,
    classroom_access,
    rate_limiter,
    runtime_config,
    secret_store,
    sessions,
    settings,
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
    yield
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
    assert client.get("/health").json() == {"status": "ok"}
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


def test_config_requires_teacher_session(client: TestClient) -> None:
    assert client.get("/config").status_code == 401


def test_logout_revokes_session(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.get("/config", headers=admin_headers).status_code == 200
    assert client.post("/auth/logout", headers=admin_headers).status_code == 200
    assert client.get("/config", headers=admin_headers).status_code == 401


def test_chat_requires_current_classroom_token(client: TestClient) -> None:
    response = client.post("/chat", json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 401


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
