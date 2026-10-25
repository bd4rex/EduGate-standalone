from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import chat_service, state
from app.knowledge_policy import KnowledgePolicy, BlockedTopic, literal_topic
from app.schemas import ChatRequest, TeachingScenario
from test_knowledge_hybrid import api_client, store, add_text  # Shared isolated HTTP/storage fixtures.


@pytest.fixture(autouse=True)
def policy():
    previous = state.runtime_config.data.knowledge_policy.model_copy(deep=True)
    value = KnowledgePolicy(topics=[BlockedTopic(label="游戏",aliases=["王者荣耀", "game"])])
    state.runtime_config.data.knowledge_policy = value
    with chat_service._POLICY_CACHE_LOCK:
        chat_service._POLICY_CACHE.clear()
    yield value
    state.runtime_config.data.knowledge_policy = previous
    state.runtime_config.save()
    with chat_service._POLICY_CACHE_LOCK:
        chat_service._POLICY_CACHE.clear()


def request(text):
    return ChatRequest(messages=[{"role":"user","content":text}])


@pytest.mark.parametrize("text", ["游戏攻略", "游\u200b 戏怎么充值", "ＧＡＭＥ攻略", "王者荣耀有哪些英雄"])
def test_literal_and_alias_block_before_retrieval_or_model(policy, monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail("Blocked input reached retrieval/model execution")
    monkeypatch.setattr(state.knowledge_store,"search_details",forbidden)
    monkeypatch.setattr(chat_service,"_chat_completion",forbidden)
    query = request(text)
    assert asyncio.run(chat_service._strict_miss_decision(query,TeachingScenario(knowledge_source_id="general"))) == (True,False)
    assert query._knowledge_decision == "blocked_topic"
    assert chat_service._strict_knowledge_message(query,TeachingScenario()) == policy.message


@pytest.mark.parametrize("answer,expected", [
    ('{"blocked":true,"topic":"游戏"}',"blocked"),
    ('{"blocked":false,"topic":""}',"allowed"),
    ('RELATED',"unavailable"),
    ('{"blocked":true,"topic":"unknown"}',"unavailable"),
    ('{"blocked":1,"topic":"游戏"}',"unavailable"),
    ('[]',"unavailable"),
    ('{"blocked":false,"topic":"游戏"}',"unavailable"),
])
def test_semantic_policy_decisions_are_validated(policy, monkeypatch, answer, expected):
    async def classify(payload):
        assert "forbidden_topics" in payload["messages"][-1]["content"]
        return {"choices":[{"message":{"content":answer}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",classify)
    result = asyncio.run(chat_service._classify_blacklist("推荐能联机打怪升级的软件",policy,"test-model",kind="question"))
    assert result[0] == expected


def test_policy_service_failure_fails_closed_without_retrieval(policy, monkeypatch):
    async def offline(payload):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(chat_service,"_chat_completion",offline)
    monkeypatch.setattr(state.knowledge_store,"search_details",lambda *args,**kwargs: pytest.fail("retrieval must not run"))
    query = request("IPv6有多少位？")
    assert asyncio.run(chat_service._strict_miss_decision(query,TeachingScenario(knowledge_source_id="general"))) == (True,False)
    assert query._knowledge_decision == "policy_check_unavailable"


def test_followup_history_and_new_topic_are_distinguished(policy, monkeypatch):
    seen = []
    async def classify(payload):
        body = json.loads(payload["messages"][-1]["content"])
        seen.append(body["text"])
        blocked = "再详细说说" in body["text"]
        return {"choices":[{"message":{"content":json.dumps({"blocked":blocked,"topic":"游戏" if blocked else ""})}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",classify)
    history = [{"role":"user","content":"游戏攻略"},{"role":"assistant","content":policy.message}]
    followup = ChatRequest(messages=history+[{"role":"user","content":"那再详细说说"}])
    assert asyncio.run(chat_service._check_blacklist(followup,TeachingScenario()))
    new_topic = ChatRequest(messages=history+[{"role":"user","content":"为什么IPv6地址更多？"}])
    assert not asyncio.run(chat_service._check_blacklist(new_topic,TeachingScenario()))
    assert seen and "游戏攻略" in seen[0]


def test_complete_answer_is_checked_before_any_stream_text(policy, monkeypatch):
    query = request("IPv6有多少位？")
    query._knowledge_policy = policy
    query._knowledge_decision = "assisted"
    async def completion(payload):
        assert payload["stream"] is False
        return {"choices":[{"message":{"content":"游戏攻略不应被输出"}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",completion)
    async def collect():
        return b"".join([chunk async for chunk in chat_service._answer_stream({"model":"test","stream":True},query,TeachingScenario())])
    body = asyncio.run(collect()).decode()
    assert "游戏攻略不应被输出" not in body
    assert policy.message in body and "[DONE]" in body
    assert '"sources": []' in body


def test_blacklist_checks_full_output_not_truncated_classroom_log(policy):
    query = request("说明网络地址")
    query._knowledge_policy = policy
    query._knowledge_decision = "assisted"
    forbidden = "A" * (state.settings.classroom_record_max_content_chars + 1) + "游戏攻略"
    response = {"choices":[{"message":{"content":forbidden}}]}
    checked = asyncio.run(chat_service._check_generated_answer(response,query,TeachingScenario()))
    assert checked["choices"][0]["message"]["content"] == policy.message
    assert query._knowledge_decision == "blocked_topic"


def test_allowed_answer_and_its_references_are_checked_together(policy, monkeypatch):
    from app.knowledge import KnowledgeHit, KnowledgeSearchResult
    query = request("IPv6有多少位")
    query._knowledge_policy = policy
    query._knowledge_decision = "supported"
    query._knowledge_result = KnowledgeSearchResult(query="IPv6",hits=[KnowledgeHit(source_id="general",file_id="f",filename="IPv6.md",chunk_index=0,score=1,text="IPv6地址长度是128位。")])
    seen = []
    async def classify(payload):
        body=json.loads(payload["messages"][-1]["content"])
        seen.append(body)
        return {"choices":[{"message":{"content":'{"blocked":false,"topic":""}'}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",classify)
    response={"choices":[{"message":{"content":"IPv6有128位[K1]。","reasoning_content":"游戏内容不能透传"}},{"message":{"content":"游戏攻略不能透传"}}]}
    checked=asyncio.run(chat_service._check_generated_answer(response,query,TeachingScenario()))
    assert checked["choices"][0]["message"]["content"]=="IPv6有128位[K1]。"
    assert "不能透传" not in json.dumps(checked,ensure_ascii=False)
    assert seen[0]["kind"]=="answer_and_references" and "资料依据" in seen[0]["text"]
    assert chat_service._knowledge_metadata(query)["sources"]


def test_policy_edit_during_question_check_is_not_silently_ignored(policy, monkeypatch):
    async def classify(payload):
        state.runtime_config.data.knowledge_policy=KnowledgePolicy(topics=[BlockedTopic(label="娱乐")])
        return {"choices":[{"message":{"content":'{"blocked":false,"topic":""}'}}]}
    monkeypatch.setattr(chat_service,"_chat_completion",classify)
    query=request("IPv6有多少位")
    assert asyncio.run(chat_service._check_blacklist(query,TeachingScenario()))
    assert query._policy_decision=="unavailable"


def test_heartbeat_does_not_release_answer_while_policy_check_is_pending(policy, monkeypatch):
    query = request("IPv6有多少位？")
    query._knowledge_policy = policy
    query._knowledge_decision = "assisted"
    monkeypatch.setattr(state.settings,"stream_heartbeat_seconds",0.01)
    async def exercise():
        checking, release = asyncio.Event(), asyncio.Event()
        async def complete(payload):
            return {"choices":[{"message":{"content":"128位"}}]}
        async def verify(response, *args):
            checking.set(); await release.wait(); return response
        monkeypatch.setattr(chat_service,"_chat_completion",complete)
        monkeypatch.setattr(chat_service,"_check_generated_answer",verify)
        stream = chat_service._answer_stream({"model":"test","stream":True},query,TeachingScenario())
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(checking.wait(),1)
        first = await asyncio.wait_for(pending,1)
        assert first.startswith(b":") and b"128" not in first
        release.set()
        remaining = b"".join([chunk async for chunk in stream])
        assert "128位" in remaining.decode() and b"[DONE]" in remaining
    asyncio.run(exercise())


def test_global_policy_cannot_be_bypassed_by_v1_scenario_or_client_fields(api_client, monkeypatch, policy):
    client, teacher, student = api_client
    state.runtime_config.data.scenarios["other"] = TeachingScenario(model="other-model")
    monkeypatch.setattr("app.dependencies.effective_platform_key",lambda:"policy-test-key")
    monkeypatch.setattr(chat_service,"_chat_completion",lambda *args,**kwargs: pytest.fail("model called on literal blocked input"))
    headers={"Authorization":"Bearer policy-test-key"}
    payload={"model":"edugate:other","messages":[{"role":"user","content":"游戏攻略"}],"knowledge_policy":{"enabled":False}}
    response=client.post("/v1/chat/completions",headers=headers,json=payload)
    assert response.status_code==200 and response.json()["choices"][0]["message"]["content"]==policy.message
    response=client.post("/v1/chat/completions",headers=headers,json={**payload,"stream":True})
    assert policy.message in response.text and "[DONE]" in response.text
    assert client.post("/chat",headers=student,json={"messages":payload["messages"],"knowledge_policy":{"enabled":False}}).status_code==422


def test_teacher_policy_persists_and_student_cannot_change_it(api_client):
    from app.runtime_config import RuntimeConfig
    client, teacher, student = api_client
    body={"enabled":True,"topics":[{"label":"游戏"}],"message":"请讨论课堂任务。"}
    assert client.put("/knowledge/policy",headers=student,json=body).status_code==401
    assert client.put("/knowledge/policy",headers=teacher,json=body).status_code==200
    reloaded=RuntimeConfig(str(state.runtime_config._path),secret_store=state.secret_store)
    assert reloaded.data.knowledge_policy.topics[0].label=="游戏"
    assert reloaded.data.knowledge_policy.message==body["message"]
    assert client.get("/knowledge/policy",headers=student).status_code==401


def test_teacher_probe_and_existing_student_reference_obey_current_blacklist(api_client, store, policy):
    client, teacher, student = api_client
    file=add_text(store,"lesson.md","# 游戏\n游戏攻略是禁答内容。")
    client.put("/config/scenarios/default",headers=teacher,json={"knowledge_source_id":"general"})
    response=client.post("/knowledge/search",headers=teacher,json={"source_id":"general","query":"游戏攻略"})
    assert response.json()["blocked"] is True and response.json()["hits"]==[]
    hit=store.search("general","游戏")[0]
    assert client.get(f"/knowledge/citations/{hit.chunk_id}",headers=student).status_code==403
    # Teachers retain access to inspect and manage the original material.
    assert client.get(f"/knowledge/files/{file.id}/preview",headers=teacher).status_code==200


def test_disabled_or_empty_policy_has_no_model_overhead(monkeypatch):
    state.runtime_config.data.knowledge_policy=KnowledgePolicy(enabled=False,topics=[BlockedTopic(label="游戏")])
    monkeypatch.setattr(chat_service,"_chat_completion",lambda *args,**kwargs: pytest.fail("disabled policy invoked model"))
    assert not asyncio.run(chat_service._check_blacklist(request("游戏"),TeachingScenario()))
    assert literal_topic("游戏",state.runtime_config.data.knowledge_policy) is None
