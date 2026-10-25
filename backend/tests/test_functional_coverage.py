from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import chat_service
from app.api_docs import tag_for_path
from app.main import app
from app.schemas import (
    ChatRequest,
    ModelCatalogItem,
    ScenarioUpdateRequest,
    TeachingScenario,
)
from app.state import (
    business_db,
    classroom_access,
    rate_limiter,
    runtime_config,
    secret_store,
    sessions,
    settings,
    student_sessions,
)
from app.system_control import system_control
from app.secret_store import SecretStore
from app.security import StudentIdentity
from app.state import ModelConcurrencyLimiter


ADMIN_PASSWORD = "functional-admin-password"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    business_db.init()
    if business_db.is_admin_initialized(settings.admin_username):
        business_db.change_teacher_password(settings.admin_username, ADMIN_PASSWORD)
    else:
        business_db.setup_admin(
            username=settings.admin_username,
            password=ADMIN_PASSWORD,
            display_name="Functional Test Administrator",
        )
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return {"X-Admin-Token": sessions.issue(settings.admin_username)}


@pytest.fixture(autouse=True)
def isolate_global_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Starlette's TestClient reports the synthetic host name ``testclient``.
    # Production Uvicorn requests always provide a numeric peer IP, so map the
    # synthetic test transport to loopback without weakening the application.
    monkeypatch.setattr("app.dependencies._client_ip", lambda _: "127.0.0.1")
    config_snapshot = runtime_config.data.model_copy(deep=True)
    with secret_store._lock:
        secret_snapshot = dict(secret_store._data)
    with rate_limiter._lock:
        rate_limiter._events.clear()
    classroom_access.start()
    student_sessions.revoke_all()
    yield
    classroom_access.start()
    student_sessions.revoke_all()
    runtime_config.data = config_snapshot
    runtime_config.save()
    with secret_store._lock:
        secret_store._data = secret_snapshot
        secret_store._save_locked()


def _configured_model(model_id: str = "functional-model") -> ModelCatalogItem:
    return runtime_config.upsert_model(
        ModelCatalogItem(
            id=model_id,
            name="Functional Model",
            provider="Functional Provider",
            source="openai_compatible",
            base_url="https://provider.invalid/v1",
            api_key="functional-secret",
        )
    )


def test_saved_provider_can_be_reloaded_without_importing_models(client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.runtime_config import RuntimeConfig

    payload = {"provider": "未导入模型的供应商", "base_url": "https://saved.example/v1", "api_key": "upstream-saved-key"}
    assert client.post("/admin/providers", json=payload).status_code == 401
    response = client.post("/admin/providers", json=payload, headers=admin_headers)
    assert response.status_code == 200
    provider_id = response.json()["provider_id"]
    assert "upstream-saved-key" not in runtime_config._path.read_text(encoding="utf-8")
    reloaded_store = SecretStore(str(secret_store.path), mode=secret_store.mode)
    reloaded = RuntimeConfig(str(runtime_config._path), secret_store=reloaded_store)
    saved = reloaded.data.providers[provider_id]
    assert saved.name == payload["provider"]
    assert reloaded_store.get(saved.credential_id) == payload["api_key"]
    listed = client.get("/admin/providers", headers=admin_headers)
    assert "upstream-saved-key" not in listed.text
    assert next(p for p in listed.json() if p["id"] == provider_id)["model_count"] == 0

    async def discover(*, base_url: str, api_key: str) -> list[dict[str, str]]:
        assert base_url == payload["base_url"] and api_key == payload["api_key"]
        return [{"id": "saved-provider-model"}]

    monkeypatch.setattr(chat_service.state.client, "list_openai_models", discover)
    reused = {**payload, "api_key": None, "provider_id": provider_id}
    assert client.post("/admin/models/discover", headers=admin_headers, json=reused).json()["used_saved_api_key"] is True
    changed_address = client.post("/admin/providers", headers=admin_headers, json={**reused, "base_url": "https://different.example/v1"})
    assert changed_address.status_code == 400
    assert client.delete(f"/admin/providers/{provider_id}", headers=admin_headers).status_code == 200
    assert provider_id not in runtime_config.data.providers
    assert not secret_store.has(saved.credential_id)


def test_provider_configuration_updates_imported_models_and_rejects_invalid_addresses(client: TestClient, admin_headers: dict[str, str]) -> None:
    model = _configured_model("provider-update-model")
    payload = {"provider": "Renamed", "provider_id": model.provider_id, "base_url": "https://new.example/v1", "api_key": "replacement-key"}
    assert client.post("/admin/providers", headers=admin_headers, json=payload).status_code == 200
    updated = runtime_config.data.model_catalog[model.id]
    assert updated.base_url == payload["base_url"] and updated.provider == "Renamed"
    assert secret_store.get(updated.credential_id) == "replacement-key"
    for address in ("ftp://server", "not-a-url", "http://server:bad/v1", "https://user:pass@server/v1"):
        assert client.post("/admin/providers", headers=admin_headers, json={**payload, "base_url": address}).status_code == 400


def test_platform_key_reveal_is_teacher_only_and_not_cached(client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    secret_store.set("system:platform_api_key", "reveal-test-key")
    assert client.get("/admin/system/platform-key").status_code == 401
    assert client.get("/admin/system/platform-key", headers={"Authorization": "Bearer reveal-test-key"}).status_code == 401
    revealed = client.get("/admin/system/platform-key", headers=admin_headers)
    assert revealed.json()["api_key"] == "reveal-test-key"
    assert revealed.headers["cache-control"] == "no-store"
    assert "reveal-test-key" not in client.get("/admin/system/status", headers=admin_headers).text
    monkeypatch.setattr(settings, "allow_lan_admin", False)
    monkeypatch.setattr("app.dependencies._client_ip", lambda _: "192.168.50.30")
    assert client.get("/admin/system/platform-key", headers=admin_headers).status_code == 403


def test_route_inventory_openapi_and_root_redirect_are_complete(client: TestClient) -> None:
    expected = {
        ("GET", "/health"),
        ("GET", "/auth/status"),
        ("POST", "/auth/local-session"),
        ("POST", "/auth/setup"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/password"),
        ("POST", "/classroom/join"),
        ("POST", "/chat"),
        ("POST", "/chat/stream"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
        ("GET", "/config"),
        ("POST", "/config/model"),
        ("POST", "/config/ai"),
        ("PUT", "/config/scenarios/{scenario_id}"),
        ("GET", "/admin/dashboard"),
        ("GET", "/admin/logs"),
        ("GET", "/admin/system/status"),
        ("GET", "/admin/system/launcher-logs"),
        ("GET", "/admin/system/settings"),
        ("POST", "/admin/system/open-app-dir"),
        ("PUT", "/admin/system/settings"),
        ("PUT", "/admin/system/platform-key"),
        ("GET", "/admin/system/platform-key"),
        ("POST", "/admin/system/platform-key/generate"),
        ("DELETE", "/admin/system/platform-key"),
        ("GET", "/admin/system/backup"),
        ("POST", "/admin/system/restore"),
        ("POST", "/admin/system/action"),
        ("POST", "/admin/system/upgrade"),
        ("GET", "/admin/classroom"),
        ("POST", "/admin/classroom/rotate"),
        ("POST", "/admin/classroom/start"),
        ("POST", "/admin/classroom/trial"),
            ("POST", "/admin/classroom/end"),
            ("GET", "/admin/published-pages"),
            ("POST", "/admin/published-pages"),
            ("POST", "/admin/published-pages/{page_id}/activate"),
            ("POST", "/admin/published-pages/deactivate"),
            ("DELETE", "/admin/published-pages/{page_id}"),
            ("GET", "/published-pages/{page_id}"),
            ("GET", "/published-pages/{page_id}/assets/{asset_path}"),
        ("GET", "/admin/classroom-records"),
        ("GET", "/admin/classroom-records/{run_id}"),
        ("DELETE", "/admin/classroom-records/{run_id}"),
        ("GET", "/admin/models"),
        ("POST", "/admin/models/discover"),
        ("POST", "/admin/models/batch-import"),
        ("POST", "/admin/models"),
        ("PATCH", "/admin/models/{model_id}"),
        ("POST", "/admin/models/{model_id}/set-default"),
        ("DELETE", "/admin/models/{model_id}"),
        ("GET", "/admin/providers"),
        ("POST", "/admin/providers"),
        ("DELETE", "/admin/providers/{provider_id}"),
        ("POST", "/admin/providers/{name}/test"),
        ("GET", "/knowledge/status"),
        ("GET", "/knowledge/policy"),
        ("PUT", "/knowledge/policy"),
        ("POST", "/knowledge/search"),
        ("POST", "/knowledge/jobs"),
        ("PATCH", "/knowledge/files/{file_id}/metadata"),
        ("GET", "/knowledge/files/{file_id}/preview"),
        ("GET", "/knowledge/files/{file_id}/original"),
        ("GET", "/knowledge/citations/{chunk_id}"),
        ("GET", "/knowledge/sources"),
        ("POST", "/knowledge/sources"),
        ("DELETE", "/knowledge/sources/{source_id}"),
        ("POST", "/knowledge/sources/{source_id}/open-folder"),
        ("POST", "/knowledge/sources/{source_id}/scan"),
        ("GET", "/knowledge/files"),
        ("POST", "/knowledge/files"),
        ("DELETE", "/knowledge/files/{file_id}"),
        ("POST", "/run_python"),
        ("POST", "/run_python/stream"),
        ("GET", "/classroom/python-config"),
        ("POST", "/classroom/python-result"),
    }
    schema = client.get("/openapi.json").json()
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    assert actual == expected
    for method, path in expected:
        operation = schema["paths"][path][method.lower()]
        assert operation["tags"] == [tag_for_path(path)]
        assert operation["summary"]
        assert operation["description"]
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code in {302, 307}
    assert redirect.headers["location"] == "/admin.html"


@pytest.mark.parametrize(
    "path",
    [
        "/config",
        "/admin/dashboard",
        "/admin/logs",
        "/admin/system/status",
        "/admin/system/launcher-logs",
        "/admin/system/settings",
        "/admin/classroom",
        "/admin/classroom-records",
        "/admin/models",
        "/admin/providers",
        "/knowledge/sources",
        "/knowledge/files",
    ],
)
def test_all_read_only_teacher_surfaces_require_a_session(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_auth_status_setup_login_limits_and_local_session_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = client.get("/auth/status")
    assert status.status_code == 200
    assert status.json()["initialized"] is True

    monkeypatch.setattr(settings, "portable_mode", False)
    assert client.post("/auth/local-session").status_code == 404

    monkeypatch.setattr("app.routers.auth._is_loopback", lambda _: True)
    assert client.post(
        "/auth/setup",
        json={"username": settings.admin_username, "password": "another-secure-password"},
    ).status_code == 409
    assert client.post(
        "/auth/login",
        json={"username": settings.admin_username, "password": "wrong-password"},
    ).status_code == 401

    monkeypatch.setattr(rate_limiter, "allow", lambda *args, **kwargs: False)
    assert client.post(
        "/auth/login",
        json={"username": settings.admin_username, "password": ADMIN_PASSWORD},
    ).status_code == 429


def test_first_setup_success_and_validation_are_simulated_without_replacing_the_database(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = {
        "username": settings.admin_username,
        "display_name": "First Run Admin",
        "role": "admin",
        "is_active": True,
    }
    monkeypatch.setattr("app.routers.auth._is_loopback", lambda _: True)
    monkeypatch.setattr(business_db, "is_admin_initialized", lambda _: False)
    monkeypatch.setattr(business_db, "setup_admin", lambda **kwargs: teacher)

    wrong_name = client.post(
        "/auth/setup",
        json={"username": "somebody", "password": "first-run-password"},
    )
    assert wrong_name.status_code == 400
    created = client.post(
        "/auth/setup",
        json={"username": settings.admin_username, "password": "first-run-password"},
    )
    assert created.status_code == 200
    assert created.json()["teacher"]["role"] == "admin"


def test_password_change_rejects_bad_password_revokes_old_token_and_returns_a_new_one(
    client: TestClient,
) -> None:
    business_db.change_teacher_password(settings.admin_username, ADMIN_PASSWORD)
    old_token = sessions.issue(settings.admin_username)
    headers = {"X-Admin-Token": old_token}
    assert client.post(
        "/auth/password",
        headers=headers,
        json={"current_password": "wrong", "new_password": "new-functional-password"},
    ).status_code == 401

    changed = client.post(
        "/auth/password",
        headers=headers,
        json={"current_password": ADMIN_PASSWORD, "new_password": "new-functional-password"},
    )
    assert changed.status_code == 200
    new_token = changed.json()["access_token"]
    assert client.get("/config", headers=headers).status_code == 401
    assert client.get("/config", headers={"X-Admin-Token": new_token}).status_code == 200
    business_db.change_teacher_password(settings.admin_username, ADMIN_PASSWORD)
    sessions.revoke_user(settings.admin_username)


def test_config_model_ai_and_scenario_endpoints_round_trip(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    model = _configured_model()
    switched = client.post("/config/model", headers=admin_headers, json={"model": model.id})
    assert switched.status_code == 200
    assert switched.json()["scenarios"]["default"]["model"] == model.id

    disabled = client.post("/config/ai", headers=admin_headers, json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["scenarios"]["default"]["ai_enabled"] is False

    updated = client.put(
        "/config/scenarios/default",
        headers=admin_headers,
        json={"system_prompt": "Use the lesson plan", "temperature": 0.25, "max_tokens": 256},
    )
    assert updated.status_code == 200
    assert updated.json()["system_prompt"] == "Use the lesson plan"
    assert updated.json()["max_tokens"] == 256
    assert client.post(
        "/config/model",
        headers=admin_headers,
        json={"model": "missing-model"},
    ).status_code == 404
    assert client.put(
        "/config/scenarios/default",
        headers=admin_headers,
        json={"knowledge_source_id": "missing-source"},
    ).status_code == 404


def test_model_catalog_provider_listing_patch_default_and_error_paths(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    model = _configured_model("functional-catalog-model")
    listed = client.get("/admin/models", headers=admin_headers)
    assert model.id in {item["id"] for item in listed.json()}

    patched = client.patch(
        f"/admin/models/{model.id}",
        headers=admin_headers,
        json={
            "id": "ignored-id",
            "name": "Renamed Functional Model",
            "provider": "Ignored Provider Rename",
            "source": "openai_compatible",
            "base_url": "https://new.invalid/v1",
            "api_key": "replacement-secret",
        },
    )
    assert patched.status_code == 200
    assert patched.json()["id"] == model.id
    assert patched.json()["upstream_model_id"] == model.id
    assert client.patch(
        "/admin/models/missing",
        headers=admin_headers,
        json={"id": "missing", "name": "Missing"},
    ).status_code == 404

    defaulted = client.post(f"/admin/models/{model.id}/set-default", headers=admin_headers)
    assert defaulted.status_code == 200
    assert defaulted.json()["scenarios"]["default"]["model"] == model.id
    providers = client.get("/admin/providers", headers=admin_headers).json()
    assert any(item.get("name") == "Ignored Provider Rename" for item in providers)
    assert client.post("/admin/providers/openai_compatible/test", headers=admin_headers).status_code == 200
    assert client.post("/admin/providers/langfuse/test", headers=admin_headers).status_code == 200
    assert client.post("/admin/providers/not-a-provider/test", headers=admin_headers).status_code == 404
    assert client.delete("/admin/models/not-a-model", headers=admin_headers).status_code == 404
    assert client.delete("/admin/providers/not-a-provider", headers=admin_headers).status_code == 404


def test_batch_import_rejects_empty_and_unknown_upstream_selections(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def discovered(_: Any) -> tuple[list[dict[str, str]], str, bool]:
        return ([{"id": "known", "owned_by": "test"}], "key", False)

    monkeypatch.setattr("app.routers.models._discover_provider_models", discovered)
    base = {
        "provider": "Test",
        "base_url": "https://provider.invalid/v1",
        "api_key": "key",
    }
    empty = client.post(
        "/admin/models/batch-import",
        headers=admin_headers,
        json={**base, "model_ids": [" "]},
    )
    unknown = client.post(
        "/admin/models/batch-import",
        headers=admin_headers,
        json={**base, "model_ids": ["unknown"]},
    )
    assert empty.status_code == 400
    assert unknown.status_code == 400


def test_knowledge_source_file_lifecycle_uses_real_storage_and_api(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "functional-source"
    created = client.post(
        "/knowledge/sources",
        headers=admin_headers,
        json={"id": source_id, "name": "Functional Source", "description": "API lifecycle"},
    )
    assert created.status_code == 200
    assert source_id in {item["id"] for item in client.get("/knowledge/sources", headers=admin_headers).json()}

    uploaded = client.post(
        "/knowledge/files",
        headers=admin_headers,
        data={"source_id": source_id},
        files={"file": ("lesson.txt", b"fractions numerator denominator", "text/plain")},
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["id"]
    assert [item["id"] for item in client.get(
        "/knowledge/files", headers=admin_headers, params={"source_id": source_id}
    ).json()] == [file_id]

    monkeypatch.setattr(
        "app.routers.knowledge.open_local_directory",
        lambda path, **kwargs: {"status": "opened", "path": str(path)},
    )
    opened = client.post(f"/knowledge/sources/{source_id}/open-folder", headers=admin_headers)
    assert opened.status_code == 200
    assert opened.json()["status"] == "opened"
    assert client.post(f"/knowledge/sources/{source_id}/scan", headers=admin_headers).status_code == 200

    runtime_config.update_scenario(
        "default",
        ScenarioUpdateRequest(knowledge_source_id=source_id),
    )
    assert client.delete(f"/knowledge/sources/{source_id}", headers=admin_headers).status_code == 409
    runtime_config.data.scenarios["default"].knowledge_source_id = None
    runtime_config.save()
    assert client.delete(f"/knowledge/files/{file_id}", headers=admin_headers).status_code == 200
    assert client.delete(f"/knowledge/sources/{source_id}", headers=admin_headers).status_code == 200
    assert client.delete(f"/knowledge/sources/{source_id}", headers=admin_headers).status_code == 404


def test_system_read_endpoints_settings_backup_restore_and_actions(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert client.get("/admin/dashboard", headers=admin_headers).status_code == 200
    assert client.get("/admin/logs", headers=admin_headers, params={"limit": 999}).status_code == 200
    status = client.get("/admin/system/status", headers=admin_headers)
    assert status.status_code == 200
    assert {"model_pool", "python_execution", "database_writer"} <= status.json().keys()

    monkeypatch.setattr("app.routers.system.launcher_log_tail", lambda limit: [f"limit={limit}"])
    assert client.get(
        "/admin/system/launcher-logs", headers=admin_headers, params={"limit": 7}
    ).json() == {"lines": ["limit=7"]}
    monkeypatch.setattr("app.routers.system.read_advanced_settings", lambda: {"TEST": "current"})
    monkeypatch.setattr("app.routers.system.update_advanced_settings", lambda values: values)
    assert client.get("/admin/system/settings", headers=admin_headers).json() == {"TEST": "current"}
    assert client.put(
        "/admin/system/settings", headers=admin_headers, json={"values": {"TEST": "updated"}}
    ).json() == {"TEST": "updated"}

    archive = tmp_path / "functional-backup.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    removed: list[Path] = []
    monkeypatch.setattr("app.routers.system.create_backup", lambda: archive)
    monkeypatch.setattr("app.routers.system.remove_backup_file", lambda path: removed.append(path))
    backup = client.get("/admin/system/backup", headers=admin_headers)
    assert backup.status_code == 200
    assert backup.content.startswith(b"PK")
    assert removed == [archive]

    system_control.unbind()
    unsupervised = client.post(
        "/admin/system/restore",
        headers=admin_headers,
        files={"file": ("backup.zip", backup.content, "application/zip")},
    )
    assert unsupervised.status_code == 409

    saved: list[str] = []

    async def fake_save(upload: Any) -> Path:
        saved.append(upload.filename)
        return tmp_path / "pending-restore.zip"

    system_control.bind(lambda action: None)
    monkeypatch.setattr("app.routers.system.save_restore_archive", fake_save)
    monkeypatch.setattr(system_control, "request", lambda action: action == "restart")
    restored = client.post(
        "/admin/system/restore",
        headers=admin_headers,
        files={"file": ("backup.zip", backup.content, "application/zip")},
    )
    assert restored.status_code == 200
    assert saved == ["backup.zip"]
    assert client.post(
        "/admin/system/action", headers=admin_headers, json={"action": "restart"}
    ).status_code == 200
    assert client.post(
        "/admin/system/action", headers=admin_headers, json={"action": "shutdown"}
    ).status_code == 409
    system_control.unbind()


def test_platform_key_protects_nonstream_and_stream_openai_compatible_requests(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_store.set("system:platform_api_key", "functional-platform-key")
    captured: dict[str, Any] = {}

    async def fake_completion(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"choices": [{"message": {"content": payload["messages"][-1]["content"]}}]}

    async def fake_stream(payload: dict[str, Any]):
        yield b'data: {"choices":[{"delta":{"content":"streamed"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("app.routers.chat._chat_completion", fake_completion)
    monkeypatch.setattr("app.routers.chat._stream_with_errors", fake_stream)
    auth = {"Authorization": "Bearer functional-platform-key"}
    denied = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["type"] == "authentication_error"
    models = client.get("/v1/models", headers=auth)
    assert models.status_code == 200
    assert models.json()["object"] == "list"
    assert models.json()["data"][0]["id"] == "edugate"
    normal = client.post(
        "/v1/chat/completions",
        headers=auth,
        json={
            "model": "edugate",
            "temperature": 0.8,
            "max_tokens": 123,
            "messages": [
                {"role": "system", "content": "Use short sentences."},
                {"role": "user", "content": "hello"},
            ],
        },
    )
    assert normal.status_code == 200
    assert normal.json()["choices"][0]["message"]["content"] == "hello"
    assert normal.json()["model"] == "edugate"
    assert captured["temperature"] == 0.8
    assert captured["max_tokens"] == 123
    assert {"role": "system", "content": "Use short sentences."} in captured["messages"]
    unknown = client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "not-a-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    streamed = client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert streamed.status_code == 200
    assert "streamed" in streamed.text and "[DONE]" in streamed.text
    assert streamed.headers["cache-control"] == "no-cache, no-transform"
    assert streamed.headers["x-accel-buffering"] == "no"


@pytest.mark.parametrize("disable", ["delete", "empty", "null"])
def test_platform_disable_overrides_env_after_reload(client, admin_headers, monkeypatch, tmp_path, disable):
    import app.state as state
    import app.routers.system as system_router
    store = SecretStore(str(tmp_path / "platform-secrets.json"), mode="portable")
    monkeypatch.setattr(state, "secret_store", store)
    monkeypatch.setattr(system_router, "secret_store", store)
    monkeypatch.setattr(settings, "platform_api_key", "environment-key")
    auth = {"Authorization": "Bearer environment-key"}
    assert client.get("/v1/models", headers=auth).status_code == 200
    if disable == "delete":
        response = client.delete("/admin/system/platform-key", headers=admin_headers)
    else:
        response = client.put("/admin/system/platform-key", headers=admin_headers, json={"api_key": "" if disable == "empty" else None})
    assert response.status_code == 200
    for reload in (False, True):
        if reload:
            reloaded = SecretStore(str(store.path), mode="portable")
            monkeypatch.setattr(state, "secret_store", reloaded)
            monkeypatch.setattr(system_router, "secret_store", reloaded)
        assert client.get("/v1/models", headers=auth).status_code == 503
        for stream in (False, True):
            response = client.post("/v1/chat/completions", headers=auth, json={"stream": stream, "messages": [{"role": "user", "content": "hello"}]})
            assert response.status_code == 503
        assert client.get("/admin/system/status", headers=admin_headers).json()["platform_api_key_set"] is False
    key = client.post("/admin/system/platform-key/generate", headers=admin_headers).json()["api_key"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert client.get("/v1/models", headers=auth).status_code == 401


@pytest.mark.parametrize("action", ["end", "start", "rotate"])
@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
def test_http_classroom_switch_cancels_model_queue(client, admin_headers, monkeypatch, action, route):
    import app.state as state
    calls = []
    async def complete(*args, **kwargs):
        calls.append("complete")
        return {"choices": [{"message": {"content": "new classroom answer"}}]}
    async def stream(*args, **kwargs):
        calls.append("stream")
        yield b'data: {"choices":[{"delta":{"content":"new classroom answer"}}]}\n\n'
        yield b"data: [DONE]\n\n"
    monkeypatch.setattr(state.client, "chat_completion", complete)
    monkeypatch.setattr(state.client, "openai_chat_completion", complete)
    monkeypatch.setattr(state.client, "stream_chat_completion", stream)
    monkeypatch.setattr(state.client, "stream_openai_chat_completion", stream)
    async def check():
        limiter = ModelConcurrencyLimiter(1)
        monkeypatch.setattr(state, "model_semaphore", limiter)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            async with limiter:
                pending = asyncio.create_task(http.post(route, headers={"X-Class-Token": classroom_access.token()}, json={"messages": [{"role": "user", "content": "old classroom request"}]}))
                async def wait_queued():
                    while limiter.stats()["waiting"] != 1:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(wait_queued(), 2)
                response = await http.post(f"/admin/classroom/{action}", headers=admin_headers)
                assert response.status_code == 200
                denied = await asyncio.wait_for(pending, 2)
                if route.endswith("stream"):
                    assert '"status_code": 403' in denied.text
                    assert "[DONE]" not in denied.text
                else:
                    assert denied.status_code == 403
                assert limiter.stats()["waiting"] == 0
                assert calls == []
            assert limiter.stats()["running"] == 0
            if action == "end":
                await http.post("/admin/classroom/start", headers=admin_headers)
            valid = await http.post(route, headers={"X-Class-Token": classroom_access.token()}, json={"messages": [{"role": "user", "content": "new classroom request"}]})
            assert valid.status_code == 200
            assert "new classroom answer" in valid.text
            assert len(calls) == 1
    asyncio.run(check())


def test_stream_revocation_closes_upstream_and_records_original_class(client, admin_headers, monkeypatch):
    import app.state as state
    old_id = classroom_access.classroom_id()
    async def check():
        entered = asyncio.Event()
        closed = asyncio.Event()
        limiter = ModelConcurrencyLimiter(1)
        monkeypatch.setattr(state, "model_semaphore", limiter)
        async def stream(*args, **kwargs):
            try:
                yield b'data: {"choices":[{"delta":{"content":"partial old answer"}}]}\n\n'
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        monkeypatch.setattr(state.client, "stream_chat_completion", stream)
        monkeypatch.setattr(state.client, "stream_openai_chat_completion", stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            pending = asyncio.create_task(http.post("/chat/stream", headers={"X-Class-Token": classroom_access.token()}, json={"messages": [{"role": "user", "content": "original classroom question"}]}))
            await asyncio.wait_for(entered.wait(), 2)
            await http.post("/admin/classroom/start", headers=admin_headers)
            result = await asyncio.wait_for(pending, 2)
            assert "partial old answer" in result.text
            assert '"status_code": 403' in result.text
            assert closed.is_set()
            assert limiter.stats() == {"running": 0, "waiting": 0, "capacity": 1}
    asyncio.run(check())
    records = business_db.list_classroom_records(teacher_username=settings.admin_username)
    original_id = hashlib.sha256(f"{old_id}:{settings.admin_username}".encode()).hexdigest()[:32]
    new_id = hashlib.sha256(f"{classroom_access.classroom_id()}:{settings.admin_username}".encode()).hexdigest()[:32]
    original = next(record for record in records if record["id"] == original_id)
    assert original["ended_at"] is not None
    assert not any(record["id"] == new_id for record in records)


def test_late_record_does_not_reopen_an_ended_classroom(client):
    lease = classroom_access.lease()
    identity = StudentIdentity("late-student", "PC", "127.0.0.1", lease.id, lease)
    def record():
        chat_service._record_classroom_turn(teacher_id=settings.admin_username, student=identity, kind="chat", input_content="old question", output_content="late answer", status_code=403, latency_ms=1)
    record()
    business_db.end_classroom_instance(lease.id)
    classroom_access.start()
    record()
    records = business_db.list_classroom_records(teacher_username=settings.admin_username)
    original_id = hashlib.sha256(f"{lease.id}:{settings.admin_username}".encode()).hexdigest()[:32]
    original = next(item for item in records if item["id"] == original_id)
    assert original["ended_at"] is not None

@pytest.mark.parametrize("kind", ["status", "connection"])
def test_chat_translates_upstream_http_failures(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    if kind == "status":
        request = httpx.Request("POST", "https://provider.invalid/chat")
        response = httpx.Response(429, request=request, json={"error": "quota"})

        async def fail(_: dict[str, Any]) -> dict[str, Any]:
            raise httpx.HTTPStatusError("quota", request=request, response=response)

        expected = 429
    else:

        async def fail(_: dict[str, Any]) -> dict[str, Any]:
            raise httpx.ConnectError("offline")

        expected = 502
    monkeypatch.setattr("app.routers.chat._chat_completion", fail)
    response = client.post(
        "/chat",
        headers={"X-Class-Token": classroom_access.token()},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == expected


def test_strict_knowledge_gate_never_substitutes_topic_for_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.knowledge import KnowledgeHit, KnowledgeSearchResult
    scenario = TeachingScenario(model="strict-model", knowledge_source_id="strict-source", knowledge_strict=True)
    hit = KnowledgeHit(source_id="strict-source",file_id="file",filename="NAT.txt",chunk_index=0,score=1,text="NAT converts a private address to a public address.")
    question = ChatRequest(messages=[{"role":"user", "content":"Who invented NAT?"}])
    question._knowledge_result = KnowledgeSearchResult(query="Who invented NAT?",hits=[hit])
    async def unrelated_evidence(payload):
        return {"choices":[{"message":{"content":'{"supported":false,"evidence":[]}'}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",unrelated_evidence)
    assert asyncio.run(chat_service._strict_miss_decision(question,scenario)) == (True,False)
    assert question._knowledge_decision == "insufficient_evidence"
    chunks = b"".join(chat_service._strict_knowledge_miss_sse(question,scenario))
    assert b"[DONE]" in chunks
    missing = ChatRequest(messages=[{"role":"user","content":"Explain NAT"}])
    missing._knowledge_result = KnowledgeSearchResult(query="Explain NAT")
    assert asyncio.run(chat_service._strict_miss_decision(missing,scenario)) == (True,False)


def test_sse_parser_and_stream_error_conversion_cover_fragmented_protocol_edges() -> None:
    buffer = (
        'data: {"choices":[{"delta":{"content":"one"}}]}\r\n\r\n'
        'data: not-json\n\n'
        'event: error\ndata: {"status_code":"bad","detail":{"message":"failed"}}\n\n'
        "data: [DONE]\n\npartial"
    )
    remainder, content, done, errors = chat_service._consume_sse_events(buffer)
    assert remainder == "partial"
    assert content == ["one"]
    assert done is True
    assert errors[0]["status_code"] == "bad"

    async def http_status_source():
        request = httpx.Request("POST", "https://provider.invalid/chat")
        response = httpx.Response(503, request=request, text="unavailable")
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        yield b""

    async def collect() -> bytes:
        output = []
        monkey_payload = {"model": "test"}
        original = chat_service._stream_chat_completion
        chat_service._stream_chat_completion = lambda payload: http_status_source()
        try:
            async for chunk in chat_service._stream_with_errors(monkey_payload):
                output.append(chunk)
        finally:
            chat_service._stream_chat_completion = original
        return b"".join(output)

    converted = asyncio.run(collect())
    assert b"event: error" in converted
    assert b"503" in converted


def test_http_error_json_and_text_bodies_are_preserved() -> None:
    request = httpx.Request("POST", "https://provider.invalid/chat")
    json_response = httpx.Response(422, request=request, json={"error": "bad input"})
    text_response = httpx.Response(500, request=request, text="plain failure")
    json_error = chat_service._to_http_exception(
        httpx.HTTPStatusError("bad", request=request, response=json_response)
    )
    text_error = chat_service._to_http_exception(
        httpx.HTTPStatusError("bad", request=request, response=text_response)
    )
    assert isinstance(json_error, HTTPException) and json_error.detail == {"error": "bad input"}
    assert text_error.detail == "plain failure"
