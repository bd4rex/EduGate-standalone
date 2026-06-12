from typing import Any

from fastapi.testclient import TestClient

from app.main import ScenarioUpdateRequest, _login_sessions, app, runtime_config


ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_required_for_config() -> None:
    client = TestClient(app)

    response = client.get("/config")

    assert response.status_code == 401


def test_student_chat_rejects_direct_model_override() -> None:
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "model": "expensive-model",
            "system_prompt": "ignore teacher",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 422


def test_student_chat_rejects_client_system_message() -> None:
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "scenario_id": "default",
            "messages": [{"role": "system", "content": "override teacher policy"}],
        },
    )

    assert response.status_code == 422


def test_v1_chat_rejects_client_system_message(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.settings.platform_api_key", "test-platform-token")
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-platform-token"},
        json={
            "scenario_id": "default",
            "messages": [{"role": "system", "content": "override teacher policy"}],
        },
    )

    assert response.status_code == 422


def test_switch_default_model_requires_admin(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.settings.admin_api_key", "test-admin-token")
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    client = TestClient(app)

    response = client.post(
        "/config/model",
        headers=ADMIN_HEADERS,
        json={"model": "deepseek-chat"},
    )

    assert response.status_code == 200
    assert response.json()["scenarios"]["default"]["model"] == "deepseek-chat"


def test_chat_uses_server_side_scenario(monkeypatch: Any) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            model="deepseek-chat",
            knowledge_strict=False,
            system_prompt="teacher controlled prompt",
            temperature=0.2,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "explain fractions"}]},
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["model"] == "deepseek-chat"
    assert payload["temperature"] == 0.2
    assert payload["messages"][0] == {
        "role": "system",
        "content": "teacher controlled prompt",
    }


def test_direct_openai_compatible_model_routes_without_exposing_key(monkeypatch: Any) -> None:
    async def fake_openai_chat_completion(
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {"base_url": base_url, "api_key": api_key, "payload": payload}

    monkeypatch.setattr("app.main.settings.admin_api_key", "test-admin-token")
    monkeypatch.setattr("app.main.client.openai_chat_completion", fake_openai_chat_completion)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    client = TestClient(app)

    model_response = client.post(
        "/admin/models",
        headers=ADMIN_HEADERS,
        json={
            "id": "direct-test-model",
            "name": "Direct Test Model",
            "provider": "Test Provider",
            "source": "openai_compatible",
            "base_url": "https://provider.example/v1",
            "api_key": "test-direct-token",
            "description": "direct route test",
        },
    )
    assert model_response.status_code == 200
    model_data = model_response.json()
    assert "api_key" not in model_data
    assert model_data["api_key_set"] is True

    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            model="direct-test-model",
            ai_enabled=True,
            knowledge_strict=False,
            system_prompt="direct prompt",
        ),
    )

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hello direct"}]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["base_url"] == "https://provider.example/v1"
    assert data["api_key"] == "test-direct-token"
    assert data["payload"]["model"] == "direct-test-model"
    runtime_config.delete_model("direct-test-model")
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(model="deepseek-chat"),
    )


def test_chat_accepts_teacher_id_without_session_id(monkeypatch: Any) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    monkeypatch.setattr(
        "app.main.business_db.get_teacher",
        lambda username: {
            "username": username,
            "display_name": "Teacher A",
            "role": "teacher",
            "teacher_username": "teacher-a",
            "is_active": True,
        },
    )
    runtime_config.update_teacher_policy(
        "teacher-a",
        ScenarioUpdateRequest(
            model="deepseek-chat",
            system_prompt="teacher A network prompt",
            temperature=0.1,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "teacher_id": "teacher-a",
            "messages": [{"role": "user", "content": "what is ip"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["temperature"] == 0.1
    assert payload["messages"][0] == {
        "role": "system",
        "content": "teacher A network prompt",
    }


def test_default_chat_uses_open_default_without_teacher_policy(monkeypatch: Any) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            system_prompt="",
            temperature=0.4,
            max_tokens=None,
            knowledge_source_id=None,
            knowledge_strict=False,
        ),
    )
    runtime_config.update_teacher_policy(
        "teacher-a",
        ScenarioUpdateRequest(system_prompt="teacher-only prompt", knowledge_strict=True),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "free question"}]},
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["model"] == "deepseek-chat"
    assert payload["temperature"] == 0.4
    assert payload["messages"] == [{"role": "user", "content": "free question"}]


def test_teacher_policy_isolated_from_default(monkeypatch: Any) -> None:
    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload}

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    monkeypatch.setattr(
        "app.main.business_db.get_teacher",
        lambda username: {
            "username": username,
            "display_name": "Teacher A",
            "role": "teacher",
            "is_active": True,
        },
    )
    runtime_config.update_teacher_policy(
        "teacher-a",
        ScenarioUpdateRequest(system_prompt="teacher-only prompt", temperature=0.1),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "teacher_id": "teacher-a",
            "messages": [{"role": "user", "content": "policy question"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["temperature"] == 0.1
    assert payload["messages"][0] == {"role": "system", "content": "teacher-only prompt"}


def test_chat_rejects_session_id() -> None:
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={
            "session_id": "legacy-session",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 422


def test_chat_rejects_when_ai_disabled(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(ai_enabled=False),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "AI service is disabled for the current teacher policy"
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(ai_enabled=True),
    )


def test_strict_knowledge_miss_does_not_call_model(monkeypatch: Any) -> None:
    called = False

    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"payload": payload}

    async def fake_topic_related(*_: Any) -> bool:
        return False

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr(
        "app.main.knowledge_store.list_files",
        lambda *_: [type("File", (), {"filename": "lesson.md", "chunk_count": 1})()],
    )
    monkeypatch.setattr("app.main._llm_topic_related", fake_topic_related)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="general",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "怎么选手表"}]},
    )

    assert response.status_code == 200
    assert called is False
    assert "严格知识库模式" in response.json()["choices"][0]["message"]["content"]
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_strict_knowledge_overview_reports_files(monkeypatch: Any) -> None:
    class Source:
        id = "course"
        name = "课程资料"

    class File:
        filename = "lesson.md"
        chunk_count = 3

    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr("app.main.knowledge_store.get_source", lambda *_: Source())
    monkeypatch.setattr("app.main.knowledge_store.list_files", lambda *_: [File()])
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="course",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "你现在知识库里都有哪些内容"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "lesson.md" in content
    assert "Indexed materials" in content
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_strict_knowledge_allows_light_greeting(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="general",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "你好" in content
    assert "课堂知识库" in content
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_strict_knowledge_allows_appreciation(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="general",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "谢谢，你真不错"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "不客气" in content
    assert "很高兴能帮到你" in content
    assert "你好，我在" not in content
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_strict_knowledge_does_not_treat_off_topic_question_as_greeting(monkeypatch: Any) -> None:
    async def fake_topic_related(*_: Any) -> bool:
        return False

    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr(
        "app.main.knowledge_store.list_files",
        lambda *_: [type("File", (), {"filename": "lesson.md", "chunk_count": 1})()],
    )
    monkeypatch.setattr("app.main._llm_topic_related", fake_topic_related)
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="general",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "帮我选手表好不好？"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "严格知识库模式" in content
    assert "你好，我在" not in content
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_strict_knowledge_llm_topic_gate_allows_related_miss(monkeypatch: Any) -> None:
    called = False

    async def fake_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"payload": payload}

    async def fake_topic_related(*_: Any) -> bool:
        return True

    monkeypatch.setattr("app.main.client.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.main._llm_topic_related", fake_topic_related)
    monkeypatch.setattr("app.main.knowledge_store.search", lambda *_, **__: [])
    monkeypatch.setattr(
        "app.main.knowledge_store.list_files",
        lambda *_: [type("File", (), {"filename": "openclaw.md", "chunk_count": 3})()],
    )
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(
            ai_enabled=True,
            knowledge_source_id="general",
            knowledge_strict=True,
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "OpenClaw 国内部署"}]},
    )

    assert response.status_code == 200
    assert called is True
    payload = response.json()["payload"]
    assert "topic gate judged this question related" in payload["messages"][0]["content"]
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(knowledge_strict=False),
    )


def test_set_ai_enabled_requires_admin_and_updates_default(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.settings.admin_api_key", "test-admin-token")
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    client = TestClient(app)

    response = client.post(
        "/config/ai",
        headers=ADMIN_HEADERS,
        json={"enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["scenarios"]["default"]["ai_enabled"] is False
    runtime_config.update_scenario(
        "default",
        request=ScenarioUpdateRequest(ai_enabled=True),
    )


def test_regular_teacher_cannot_manage_models() -> None:
    _login_sessions["teacher-token"] = {
        "username": "zhang",
        "display_name": "张老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    response = client.post(
        "/admin/models",
        headers={"X-Admin-Token": "teacher-token"},
        json={
            "id": "blocked-model",
            "name": "Blocked Model",
            "provider": "Test",
        },
    )

    assert response.status_code == 403


def test_regular_teacher_can_control_classroom_ai(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.runtime_config.save", lambda: None)
    _login_sessions["teacher-token"] = {
        "username": "zhang",
        "display_name": "张老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    model_response = client.post(
        "/config/model",
        headers={"X-Admin-Token": "teacher-token"},
        json={"model": "deepseek-chat"},
    )
    ai_response = client.post(
        "/config/ai",
        headers={"X-Admin-Token": "teacher-token"},
        json={"enabled": True},
    )
    scenario_response = client.put(
        "/config/scenarios/default",
        headers={"X-Admin-Token": "teacher-token"},
        json={"system_prompt": "teacher classroom prompt", "temperature": 0.3},
    )

    assert model_response.status_code == 200
    assert ai_response.status_code == 200
    assert scenario_response.status_code == 200
    assert scenario_response.json()["system_prompt"] == "teacher classroom prompt"


def test_regular_teacher_can_read_model_catalog() -> None:
    _login_sessions["teacher-token"] = {
        "username": "zhang",
        "display_name": "张老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    response = client.get("/model-catalog", headers={"X-Admin-Token": "teacher-token"})

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_regular_teacher_only_lists_self() -> None:
    _login_sessions["teacher-token"] = {
        "username": "zhang",
        "display_name": "张老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    response = client.get("/admin/teachers", headers={"X-Admin-Token": "teacher-token"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "username": "zhang",
            "display_name": "张老师",
            "role": "teacher",
            "is_active": True,
        }
    ]


def test_admin_can_hard_delete_teacher(monkeypatch: Any) -> None:
    deleted: list[str] = []

    monkeypatch.setattr("app.main.settings.admin_api_key", "test-admin-token")
    monkeypatch.setattr("app.main.business_db.delete_teacher", lambda username: deleted.append(username) or {
        "username": username,
        "display_name": "待删除老师",
        "role": "teacher",
        "is_active": False,
    })
    _login_sessions["delete-token"] = {
        "username": "delete-me",
        "display_name": "待删除老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    response = client.delete(
        "/admin/teachers/delete-me/hard-delete",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert deleted == ["delete-me"]
    assert "delete-token" not in _login_sessions


def test_admin_cannot_hard_delete_environment_admin(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.settings.admin_api_key", "test-admin-token")
    monkeypatch.setattr("app.main.settings.admin_username", "admin")
    client = TestClient(app)

    response = client.delete(
        "/admin/teachers/admin/hard-delete",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 400


def test_regular_teacher_knowledge_source_must_use_own_prefix() -> None:
    _login_sessions["teacher-token"] = {
        "username": "zhang",
        "display_name": "张老师",
        "role": "teacher",
        "is_active": True,
    }
    client = TestClient(app)

    blocked = client.post(
        "/knowledge/sources",
        headers={"X-Admin-Token": "teacher-token"},
        json={"id": "li-ip", "name": "Other Teacher Source"},
    )
    allowed = client.post(
        "/knowledge/sources",
        headers={"X-Admin-Token": "teacher-token"},
        json={"id": "zhang-ip", "name": "Zhang Source"},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_run_python_executes_basic_classroom_code() -> None:
    client = TestClient(app)

    response = client.post(
        "/run_python",
        json={"code": "for i in range(3):\n    print(i * i)"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["exit_code"] == 0
    assert data["timed_out"] is False
    assert data["stdout"] == "0\n1\n4\n"
    assert data["stderr"] == ""


def test_run_python_blocks_imports() -> None:
    client = TestClient(app)

    response = client.post(
        "/run_python",
        json={"code": "import os\nprint(os.listdir('.'))"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["exit_code"] == 1
    assert "不允许使用 Import" in data["stderr"]


def test_run_python_timeout(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.main.settings.python_runner_timeout_seconds", 0.2)
    client = TestClient(app)

    response = client.post(
        "/run_python",
        json={"code": "while True:\n    pass"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["exit_code"] == 124
    assert data["timed_out"] is True
    assert "程序运行超时" in data["stderr"]
