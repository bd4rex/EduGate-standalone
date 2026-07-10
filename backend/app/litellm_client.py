from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings


class LiteLLMClient:
    def __init__(self) -> None:
        self._base_url = settings.litellm_base_url
        self._prefix = settings.litellm_api_prefix
        self._timeout = settings.request_timeout_seconds
        self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        self._stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=min(self._timeout, 15),
                read=settings.stream_read_timeout_seconds,
                write=min(self._timeout, 30),
                pool=min(self._timeout, 15),
            ),
            trust_env=False,
        )

    def _url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}{self._prefix}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.litellm_api_key:
            headers["Authorization"] = f"Bearer {settings.litellm_api_key}"
        return headers

    async def close(self) -> None:
        await self._client.aclose()
        await self._stream_client.aclose()

    async def list_models(self) -> dict[str, Any]:
        response = await self._client.get(self._url("/models"), headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self._url("/chat/completions"),
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def stream_chat_completion(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        async with self._stream_client.stream(
            "POST",
            self._url("/chat/completions"),
            headers=self._headers(),
            json={**payload, "stream": True},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def openai_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            _openai_url(base_url, "/chat/completions"),
            headers=_openai_headers(api_key),
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def stream_openai_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        async with self._stream_client.stream(
            "POST",
            _openai_url(base_url, "/chat/completions"),
            headers=_openai_headers(api_key),
            json={**payload, "stream": True},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def probe_openai_provider(self, *, base_url: str, api_key: str) -> dict[str, Any]:
        response = await self._client.get(
            _openai_url(base_url, "/models"),
            headers=_openai_headers(api_key),
        )
        response.raise_for_status()
        data = response.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        return {"ok": True, "model_count": len(models)}


def _openai_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.split("?", 1)[0].endswith("/chat/completions"):
        if path.rstrip("/") == "/chat/completions":
            return base
        base_without_query = base.split("?", 1)[0]
        return f"{base_without_query[:-len('/chat/completions')]}{path}"
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    path = path if path.startswith("/") else f"/{path}"
    return f"{base}{path}"


def _openai_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
