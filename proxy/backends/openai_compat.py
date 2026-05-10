"""OpenAI-compatible backend — covers OpenAI plus xAI/Groq/OpenRouter/Together/vLLM.

Uses ``/v1/chat/completions`` with SSE streaming. Tool-calling is OpenAI's
function-call format (``tool_calls[].function.{name, arguments}``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import Backend, BackendError, DoneEvent, TextDeltaEvent, ToolUseEvent

logger = logging.getLogger("cortex_ollama.backends.openai")


class OpenAIBackend:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if r.status_code != 200:
                return []
            return sorted(m.get("id", "") for m in r.json().get("data", []))

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator:
        if not self.api_key:
            yield DoneEvent(
                finish_reason="error",
                error="OpenAI backend selected but no api_key configured (set api_key in cortex_ollama.toml or OPENAI_API_KEY env)",
            )
            return

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        # Buffer for streamed tool-call argument fragments. Index → state.
        accum: dict[int, dict[str, Any]] = {}

        timeout = httpx.Timeout(300.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode(errors="replace")
                    yield DoneEvent(finish_reason="error", error=f"openai {resp.status_code}: {err[:300]}")
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    content = delta.get("content")
                    if content:
                        yield TextDeltaEvent(text=content)

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = accum.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

                    finish = choice.get("finish_reason")
                    if finish:
                        # Flush any complete tool calls before terminating.
                        for _, slot in sorted(accum.items()):
                            if not slot["name"]:
                                continue
                            try:
                                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {"_raw_arguments": slot["arguments"]}
                            yield ToolUseEvent(
                                id=slot["id"] or f"call_{uuid.uuid4().hex[:8]}",
                                name=slot["name"],
                                input=args or {},
                            )
                        yield DoneEvent(finish_reason=finish)
                        return

                yield DoneEvent(finish_reason="stop")
