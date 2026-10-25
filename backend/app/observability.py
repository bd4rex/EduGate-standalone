from __future__ import annotations

import base64
import time
import uuid
from typing import Any

import httpx

from app.config import settings


class LangfuseClient:
    def __init__(self) -> None:
        self.enabled = bool(
            settings.langfuse_base_url
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        )

    async def trace_chat(
        self,
        *,
        name: str,
        input_text: str,
        output_text: str | None,
        metadata: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        auth = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode("utf-8")
        ).decode("ascii")
        event = {
            "batch": [
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _timestamp(),
                    "type": "trace-create",
                    "body": {
                        "id": str(uuid.uuid4()),
                        "name": name,
                        "input": input_text,
                        "output": output_text,
                        "metadata": metadata,
                    },
                }
            ]
        }
        if usage:
            event["batch"].append(
                {
                    "id": str(uuid.uuid4()),
                    "timestamp": _timestamp(),
                    "type": "generation-create",
                    "body": {
                        "id": str(uuid.uuid4()),
                        "name": metadata.get("model") or "chat-completion",
                        "model": metadata.get("model"),
                        "usage": usage,
                        "metadata": metadata,
                    },
                }
            )
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                await client.post(
                    f"{settings.langfuse_base_url.rstrip('/')}/api/public/ingestion",
                    json=event,
                    headers={"Authorization": f"Basic {auth}"},
                )
        except httpx.HTTPError:
            return


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
