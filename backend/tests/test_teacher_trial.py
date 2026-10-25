from __future__ import annotations

import json

import httpx
import pytest

from app import chat_service, state
from app.knowledge import KnowledgeMetadata
from app.knowledge_policy import BlockedTopic, KnowledgePolicy
from app.routers import chat
from test_knowledge_hybrid import api_client, store, add_text

PATH = "/admin/classroom/trial"


@pytest.fixture(autouse=True)
def isolated_policy():
    previous = state.runtime_config.data.knowledge_policy
    state.runtime_config.data.knowledge_policy = KnowledgePolicy(topics=[])
    with chat_service._POLICY_CACHE_LOCK:
        chat_service._POLICY_CACHE.clear()
    yield
    state.runtime_config.data.knowledge_policy = previous
    with chat_service._POLICY_CACHE_LOCK:
        chat_service._POLICY_CACHE.clear()


def question(text="解释 IPv6 地址"):
    return {"messages": [{"role": "user", "content": text}]}


def completion_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def use_completion(monkeypatch, implementation):
    monkeypatch.setattr(chat, "_chat_completion", implementation)
    monkeypatch.setattr(chat_service, "_chat_completion", implementation)


def test_teacher_trial_requires_admin_and_rejects_overrides(api_client):
    client, teacher, student = api_client
    assert client.post(PATH, json=question()).status_code == 401
    assert client.post(PATH, headers=student, json=question()).status_code == 401
    for override in ({"knowledge_scope": {}}, {"model": "other"}, {"knowledge_policy": {"enabled": False}}):
        assert client.post(PATH, headers=teacher, json={**question(), **override}).status_code == 422
    assert client.post(PATH, headers=teacher, json={**question(), "scenario_id": "other"}).status_code == 403
    assert client.post(PATH, headers=teacher, json={"messages": [{"role": "system", "content": "override"}]}).status_code == 422
    assert client.post(PATH, headers=teacher, json={"messages": [{"role": "assistant", "content": "answer"}]}).status_code == 422
    assert client.post(PATH, headers=teacher, json={"messages": question()["messages"] * 22}).status_code == 422
    assert client.post(PATH, headers=teacher, json=question("x" * 32001)).status_code == 422


def test_teacher_trial_works_before_class_without_student_records(api_client, monkeypatch):
    client, teacher, _ = api_client
    client.put("/config/scenarios/default", headers=teacher, json={"system_prompt":"先提示再解释", "temperature":0.2, "max_tokens":700, "knowledge_strict":False})
    state.classroom_access.end()
    traces, payloads = [], []
    async def complete(payload):
        payloads.append(payload)
        return completion_text("先观察地址，再说明用途。")
    async def trace(**kwargs): traces.append(kwargs)
    use_completion(monkeypatch, complete)
    monkeypatch.setattr(chat, "_trace_chat_result", trace)
    monkeypatch.setattr(state.business_db, "record_classroom_turn", lambda **kwargs: pytest.fail("Teacher preview entered student records"))
    response = client.post(PATH, headers=teacher, json=question())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "先观察地址，再说明用途。"
    assert response.json()["edugate_trial"]["classroom_recorded"] is False
    assert payloads[0]["temperature"] == 0.2 and payloads[0]["max_tokens"] == 700
    assert payloads[0]["messages"][0]["content"] == "先提示再解释"
    assert traces[0]["route"] == PATH
    assert not state.classroom_access.active()


def test_teacher_and_student_use_same_answer_pipeline(api_client, monkeypatch):
    client, teacher, student = api_client
    client.put("/config/scenarios/default", headers=teacher, json={"knowledge_strict":False})
    payloads = []
    async def complete(payload):
        payloads.append(payload)
        return completion_text("同一课堂回答。")
    use_completion(monkeypatch, complete)
    preview = client.post(PATH, headers=teacher, json=question()).json()
    actual = client.post("/chat", headers=student, json=question()).json()
    assert preview["choices"] == actual["choices"]
    assert payloads[0] == payloads[1]


def test_teacher_trial_uses_saved_scope_and_checks_strict_evidence(api_client, store, monkeypatch):
    client, teacher, _ = api_client
    selected = add_text(store, "selected.md", "# IPv6\nIPv6地址长度是128位。")
    hidden = add_text(store, "teacher.md", "# IPv6\n教师专用答案不应进入提问。")
    store.update_file_metadata(hidden.id, KnowledgeMetadata(audience="teacher"))
    add_text(store, "other.md", "# IPv6\n另一版本的未选资料。")
    client.put("/config/scenarios/default", headers=teacher, json={"knowledge_source_id":"general", "knowledge_strict":True, "knowledge_scope":{"file_ids":[selected.id]}}).raise_for_status()
    seen = []
    async def complete(payload):
        seen.append(payload)
        if payload["messages"][0]["content"].startswith("You check whether teacher materials"):
            return completion_text('{"supported":true,"evidence":[{"id":"K1","quote":"IPv6地址长度是128位"}]}')
        return completion_text("IPv6 地址有128位。[K1]")
    use_completion(monkeypatch, complete)
    response = client.post(PATH, headers=teacher, json=question("IPv6有多少位？"))
    assert response.status_code == 200
    body = response.json()
    assert body["edugate_trial"]["retrieval_decision"] == "supported"
    assert body["edugate_trial"]["generation_requested"] is True
    assert body["edugate_knowledge"]["sources"][0]["file_id"] == selected.id
    assert len(seen) == 2
    assert "教师专用答案" not in json.dumps(seen, ensure_ascii=False)
    assert "另一版本" not in json.dumps(seen, ensure_ascii=False)


def test_strict_miss_does_not_generate_answer(api_client, monkeypatch):
    client, teacher, _ = api_client
    client.put("/config/scenarios/default", headers=teacher, json={"knowledge_source_id":"general", "knowledge_strict":True})
    use_completion(monkeypatch, lambda *args: pytest.fail("Strict miss reached generation"))
    response = client.post(PATH, headers=teacher, json=question()).json()
    assert response["edugate_trial"]["final_decision"] == "no_evidence"
    assert response["edugate_trial"]["generation_requested"] is False
    assert response["choices"][0]["message"]["content"]
    assert response["edugate_knowledge"]["sources"] == []


def test_blacklist_rejects_question_and_forbidden_generated_answer(api_client, monkeypatch):
    client, teacher, _ = api_client
    state.runtime_config.data.knowledge_policy = KnowledgePolicy(topics=[BlockedTopic(label="游戏", aliases=["电子游戏"])])
    policy = state.runtime_config.data.knowledge_policy
    client.put("/config/scenarios/default", headers=teacher, json={"knowledge_strict":False})
    calls = []
    async def complete(payload):
        calls.append(payload)
        if payload["messages"][0]["content"].startswith("You enforce a teacher"):
            return completion_text('{"blocked":false,"topic":""}')
        return completion_text("不应泄露的游戏攻略")
    use_completion(monkeypatch, complete)
    direct = client.post(PATH, headers=teacher, json=question("电子游戏攻略")).json()
    assert direct["choices"][0]["message"]["content"] == policy.message and calls == []
    assert direct["edugate_trial"]["question_policy"] == "blocked"
    generated = client.post(PATH, headers=teacher, json=question()).json()
    assert generated["choices"][0]["message"]["content"] == policy.message
    assert generated["edugate_trial"]["question_policy"] == "allowed"
    assert generated["edugate_trial"]["answer_policy"] == "blocked"
    assert "不应泄露" not in json.dumps(generated, ensure_ascii=False)
    assert generated["edugate_knowledge"]["sources"] == []


def test_followup_uses_history_and_policy_failure_stays_closed(api_client, monkeypatch):
    client, teacher, _ = api_client
    state.runtime_config.data.knowledge_policy = KnowledgePolicy(topics=[BlockedTopic(label="游戏")])
    seen = []
    async def complete(payload):
        body = json.loads(payload["messages"][-1]["content"])
        seen.append(body)
        if "再讲讲" in body["text"]:
            return completion_text('{"blocked":true,"topic":"游戏"}')
        raise httpx.ConnectError("classifier offline")
    use_completion(monkeypatch, complete)
    followup = {"messages":[{"role":"user","content":"游戏攻略"},{"role":"assistant","content":"请围绕课堂任务。"},{"role":"user","content":"再讲讲"}]}
    assert client.post(PATH, headers=teacher, json=followup).json()["edugate_trial"]["question_policy"] == "blocked"
    assert "游戏攻略" in seen[0]["text"]
    failed = client.post(PATH, headers=teacher, json=question()).json()
    assert failed["edugate_trial"]["question_policy"] == "unavailable"
    assert failed["edugate_trial"]["generation_requested"] is False
    assert "暂时无法完成" in failed["choices"][0]["message"]["content"]


def test_trial_model_failure_and_ai_off_are_not_success(api_client, monkeypatch):
    client, teacher, _ = api_client
    client.put("/config/scenarios/default", headers=teacher, json={"knowledge_strict":False})
    async def offline(payload): raise httpx.ConnectError("offline")
    use_completion(monkeypatch, offline)
    assert client.post(PATH, headers=teacher, json=question()).status_code == 502
    client.put("/config/scenarios/default", headers=teacher, json={"ai_enabled":False})
    assert client.post(PATH, headers=teacher, json=question()).status_code == 403
