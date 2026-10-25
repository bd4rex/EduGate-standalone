"""Teacher-owned classroom topic policy, independent of retrieval scenarios."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

REFUSAL_MESSAGE = "这个话题不在本节课允许讨论的范围内。请围绕老师布置的学习内容提问。"
POLICY_UNAVAILABLE_MESSAGE = "暂时无法完成课堂话题检查，本次未生成答案。请稍后重试或联系老师。"


class BlockedTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=120)
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=12)

    @field_validator("label")
    @classmethod
    def meaningful_label(cls, value: str) -> str:
        if not normalize_policy_text(value):
            raise ValueError("禁答话题需要包含文字或数字")
        return value


class KnowledgePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool = True
    topics: list[BlockedTopic] = Field(default_factory=list, max_length=64)
    message: str = Field(default=REFUSAL_MESSAGE, min_length=1, max_length=400)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.topics)

    @property
    def signature(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


def normalize_policy_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def literal_topic(text: str, policy: KnowledgePolicy) -> str | None:
    if not policy.active:
        return None
    normalized = normalize_policy_text(text)
    for topic in policy.topics:
        for alias in [topic.label, *topic.aliases]:
            needle = normalize_policy_text(alias)
            if needle and needle in normalized:
                return topic.label
    return None
