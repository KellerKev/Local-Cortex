"""Anthropic Messages API backend — talks to /v1/messages with SSE.

Anthropic's tool format is ``{"name": "...", "input_schema": {...}}`` — we
convert from the OpenAI ``{"type":"function","function":{...}}`` shape Cortex
sends. Streaming events are different too: ``content_block_start``,
``content_block_delta`` (text + input_json_delta), ``content_block_stop``,
``message_delta`` (final usage).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import Backend, BackendError, DoneEvent, TextDeltaEvent, ToolUseEvent

logger = logging.getLogger("cortex_ollama.backends.anthropic")


class AnthropicBackend:
    name = "anthropic"

    def __init__(
        self, base_url: str, model: str, api_key: str = "", api_version: str = "2023-06-01"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.api_version = api_version

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                f"{self.base_url}/models",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.api_version,
                },
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
                error="Anthropic backend selected but no api_key configured (set api_key in cortex_ollama.toml or ANTHROPIC_API_KEY env)",
            )
            return

        anthropic_messages, system_prompt = _messages_to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": 4096,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = _tools_to_anthropic(tools)

        # Per-block state for assembling tool_use input from input_json_delta.
        block_state: dict[int, dict[str, Any]] = {}

        timeout = httpx.Timeout(300.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/messages",
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.api_version,
                    "content-type": "application/json",
                },
            ) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode(errors="replace")
                    yield DoneEvent(finish_reason="error", error=f"anthropic {resp.status_code}: {err[:300]}")
                    return

                event_type = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    body = line.split(":", 1)[1].strip()
                    if not body:
                        continue
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "content_block_start":
                        idx = chunk.get("index", 0)
                        block = chunk.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            block_state[idx] = {
                                "id": block.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                "name": block.get("name", ""),
                                "arguments": "",
                            }

                    elif event_type == "content_block_delta":
                        idx = chunk.get("index", 0)
                        delta = chunk.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield TextDeltaEvent(text=text)
                        elif delta.get("type") == "input_json_delta":
                            slot = block_state.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            slot["arguments"] += delta.get("partial_json", "")

                    elif event_type == "content_block_stop":
                        idx = chunk.get("index", 0)
                        slot = block_state.pop(idx, None)
                        if slot and slot.get("name"):
                            try:
                                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {"_raw_arguments": slot["arguments"]}
                            yield ToolUseEvent(
                                id=slot["id"], name=slot["name"], input=args or {}
                            )

                    elif event_type == "message_delta":
                        delta = chunk.get("delta") or {}
                        stop_reason = delta.get("stop_reason")
                        if stop_reason:
                            mapped = {
                                "end_turn": "stop",
                                "max_tokens": "length",
                                "tool_use": "tool_calls",
                                "stop_sequence": "stop",
                            }.get(stop_reason, "stop")
                            yield DoneEvent(finish_reason=mapped)
                            return

                yield DoneEvent(finish_reason="stop")


def _messages_to_anthropic(
    openai_messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Translate OpenAI chat → Anthropic messages.

    Anthropic uses a top-level ``system`` field (string), and the messages
    array can only contain ``user`` and ``assistant`` roles. ``tool`` results
    are folded into a user message with ``tool_result`` content blocks.
    """
    system_chunks: list[str] = []
    out: list[dict[str, Any]] = []
    for m in openai_messages:
        role = m.get("role")
        if role == "system":
            content = m.get("content") or ""
            if content:
                system_chunks.append(str(content))
        elif role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": str(m.get("content") or "")}]})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": str(text)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "input": args or {},
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            content = m.get("content") or ""
            tool_call_id = m.get("tool_call_id") or ""
            # Anthropic carries tool results inside a user message
            if out and out[-1]["role"] == "user":
                out[-1]["content"].append(
                    {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}
                )
            else:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}
                        ],
                    }
                )
    return out, "\n\n".join(system_chunks)


def _tools_to_anthropic(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in openai_tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out
