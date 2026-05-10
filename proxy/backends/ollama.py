"""Ollama backend — talks to a local (or remote) Ollama server's native API.

Uses ``/api/chat`` instead of the OpenAI-compat ``/v1/chat/completions`` because
the native endpoint applies per-model tool-call template parsing (qwen-coder,
devstral, etc.) which the OpenAI shim does not.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import Backend, BackendError, DoneEvent, TextDeltaEvent, ToolUseEvent

logger = logging.getLogger("cortex_ollama.backends.ollama")


class OllamaBackend:
    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def list_models(self) -> list[str]:
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            return sorted(m.get("name", "") for m in r.json().get("models", []))

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_ollama(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        timeout = httpx.Timeout(300.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=payload
            ) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode(errors="replace")
                    yield DoneEvent(finish_reason="error", error=f"ollama {resp.status_code}: {err[:300]}")
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message") or {}

                    delta_text = msg.get("content", "")
                    if delta_text:
                        yield TextDeltaEvent(text=delta_text)

                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"_raw_arguments": args}
                        yield ToolUseEvent(
                            id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            name=fn.get("name", ""),
                            input=args or {},
                        )

                    if chunk.get("done"):
                        yield DoneEvent(finish_reason=chunk.get("done_reason", "stop"))
                        return


def _messages_to_ollama(openai_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adjust OpenAI-shape messages to Ollama's /api/chat shape.

    Ollama's tool_calls expect ``arguments`` as an OBJECT, not a JSON string.
    """
    out: list[dict[str, Any]] = []
    for m in openai_messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            new = {"role": "assistant", "content": m.get("content") or ""}
            new_calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_val = fn.get("arguments", "{}")
                if isinstance(args_val, str):
                    try:
                        args_val = json.loads(args_val)
                    except json.JSONDecodeError:
                        args_val = {"_raw": args_val}
                new_calls.append(
                    {"function": {"name": fn.get("name", ""), "arguments": args_val}}
                )
            new["tool_calls"] = new_calls
            out.append(new)
        else:
            out.append(m)
    return out
