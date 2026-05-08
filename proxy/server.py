"""Cortex Code ↔ Ollama translator.

Implements the endpoint Cortex Code hits when
`CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1` is set:

    POST http://localhost:2031/v1/agent-run

Cortex sends a Snowflake-flavored agent:run request (JSON; its `messages` /
`tools` / `experimental` fields are themselves JSON strings). We:

 1. translate messages from Cortex/Anthropic shape → OpenAI chat shape,
    including tool_use ↔ tool_call / tool_result ↔ tool role round-trips;
 2. call Ollama's OpenAI-compatible /v1/chat/completions with streaming;
 3. stream the response back as the Anthropic-style SSE events Cortex
    understands (message.delta text deltas, message.stop with optional
    tool_use content, `[DONE]`).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .snowflake_stubs import router as snowflake_router
from .toolspecs import to_openai_tools

logger = logging.getLogger("cortex_ollama")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.6:35b-a3b")
# Native Ollama chat path — /v1/chat/completions drops per-model tool-call
# parsing for families like Qwen, producing raw text like "<function=read>…"
# instead of structured tool_calls. /api/chat does the parsing correctly.
OLLAMA_CHAT_PATH = "/api/chat"

# Hybrid mode: Cortex natively supports a split between agent-connection (for
# inference) and sql-connection (for database queries). We read the SQL
# connection name from the same env var Cortex itself uses so the two paths
# stay consistent. When set, the proxy also injects `connection:` into any
# Snowflake-family tool_use that the model emits without one — belt-and-
# suspenders in case the model forgets and Cortex would otherwise fall back
# to the agent connection (our stub, which returns no rows).
SQL_CONNECTION_NAME = os.environ.get("CORTEX_SQL_CONNECTION", "").strip()
# Default to "ollama" so the safety-net override fires even when the proxy
# was launched without inheriting the wrapper's CORTEX_AGENT_CONNECTION.
# Only the wrapper sets this; pixi-run-serve in another shell typically does not.
AGENT_CONNECTION_NAME = os.environ.get("CORTEX_AGENT_CONNECTION", "ollama").strip() or "ollama"

# Snowflake-family tools whose input_schema carries an optional `connection:`
# parameter. Both old (snowflake_sql_execute, Cortex 1.0.48) and new
# (sql_execute, Cortex 1.0.73+) names are recognized — Cortex renamed and
# generalized the tool to also support Postgres in 1.0.73.
SNOWFLAKE_ROUTED_TOOLS = frozenset(
    {
        "sql_execute",
        "snowflake_sql_execute",
        "snowflake_object_search",
        "snowflake_product_docs",
        "snowflake_table_lookup",
        "snowflake_multi_cortex_analyst",
        "semantic_view_search",
    }
)

app = FastAPI()
app.include_router(snowflake_router)


# ---------------------------------------------------------------------------
# Message translation: Cortex (Anthropic-shaped) → OpenAI chat completions
# ---------------------------------------------------------------------------

_REMINDER_OPEN = "<system-reminder>"
_REMINDER_CLOSE = "</system-reminder>"


def _is_reminder(text: str) -> bool:
    t = text.strip()
    return t.startswith(_REMINDER_OPEN) and t.endswith(_REMINDER_CLOSE)


def _translate_messages(messages_json: str) -> list[dict[str, Any]]:
    """Flatten Cortex's multi-part messages into OpenAI chat messages.

    Rules:
    - user message text parts: non-reminder texts become user content;
      reminder texts become a system message prepended on the first turn.
    - user message tool_result parts: emitted as {role:"tool", tool_call_id, content}.
    - assistant message text parts: joined into the assistant `content`.
    - assistant message tool_use parts: emitted as tool_calls on an
      assistant message (OpenAI function-call shape).
    """
    try:
        messages = json.loads(messages_json)
    except Exception:
        return [{"role": "user", "content": messages_json}]

    out: list[dict[str, Any]] = []
    reminder_system_added = False
    pending_reminders: list[str] = []

    for msg in messages:
        role = msg.get("role", "user")
        parts = msg.get("content") or []

        if role == "user":
            user_texts: list[str] = []
            tool_results: list[dict[str, Any]] = []
            for p in parts:
                t = p.get("type")
                if t == "text":
                    text = p.get("text", "")
                    if _is_reminder(text):
                        pending_reminders.append(text)
                    else:
                        user_texts.append(text)
                elif t == "tool_result":
                    # Cortex nests the payload under "tool_result"; fall back
                    # to flat shape for other providers.
                    tr = p.get("tool_result") or p
                    content = tr.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id") or tr.get("tool_call_id") or "",
                            "name": tr.get("name", ""),
                            "content": content if isinstance(content, str) else json.dumps(content),
                        }
                    )
            if not reminder_system_added and pending_reminders:
                out.append({"role": "system", "content": "\n\n".join(pending_reminders)})
                reminder_system_added = True
                pending_reminders = []
            out.extend(tool_results)
            if user_texts:
                out.append({"role": "user", "content": "\n\n".join(user_texts)})

        elif role == "assistant":
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for p in parts:
                t = p.get("type")
                if t == "text":
                    text_chunks.append(p.get("text", ""))
                elif t == "thinking":
                    # thinking is not replayed to the model; it was internal state
                    continue
                elif t == "tool_use":
                    tu = p.get("tool_use") or p
                    tool_calls.append(
                        {
                            "id": tu.get("tool_use_id") or tu.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": tu.get("name", ""),
                                "arguments": json.dumps(tu.get("input") or {}),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant"}
            entry["content"] = "\n".join(c for c in text_chunks if c) or None
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if entry["content"] is not None or tool_calls:
                out.append(entry)

        else:
            # system / other roles: pass through raw text
            joined = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if joined:
                out.append({"role": role, "content": joined})

    if not out:
        out.append({"role": "user", "content": "(empty)"})
    return out


# ---------------------------------------------------------------------------
# SSE framing helpers (Anthropic-style events Cortex's processSSEData parses)
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


import re

_FUNCTION_OPEN_RE = re.compile(r"<(function|tool_call)(?:=([^\s>]+))?>")
_FUNCTION_CLOSE_RE = re.compile(r"</(function|tool_call)>")
_PARAM_RE = re.compile(
    r"<parameter(?:=([^\s>]+))?(?:\s+name=\"([^\"]+)\")?>(.*?)</parameter>",
    re.DOTALL,
)


def _parse_text_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse any ``<function=NAME>…</function>`` blocks out of the given text.

    Returns (text_with_blocks_removed, list_of_tool_uses). Supports both
    ``<function=name>`` and ``<tool_call>{json}</tool_call>`` fallback formats
    emitted by qwen/llama-family models when structured tool-calling fails.
    """
    tool_uses: list[dict[str, Any]] = []
    cursor = 0
    clean: list[str] = []

    while cursor < len(text):
        open_m = _FUNCTION_OPEN_RE.search(text, cursor)
        if not open_m:
            clean.append(text[cursor:])
            break
        clean.append(text[cursor:open_m.start()])
        close_m = _FUNCTION_CLOSE_RE.search(text, open_m.end())
        if not close_m:
            # Unterminated block — drop the rest (it's garbled tool output)
            break
        body = text[open_m.end():close_m.start()]
        name = open_m.group(2) or ""
        args: dict[str, Any] = {}
        if name:
            for pm in _PARAM_RE.finditer(body):
                key = pm.group(1) or pm.group(2) or ""
                val = pm.group(3).strip()
                if key:
                    args[key] = val
        else:
            # <tool_call>{json}</tool_call> variant: body should be JSON
            try:
                parsed = json.loads(body.strip())
                if isinstance(parsed, dict):
                    name = parsed.get("name", "")
                    args = parsed.get("arguments") or parsed.get("parameters") or {}
            except json.JSONDecodeError:
                pass
        if name:
            tool_uses.append(
                {
                    "type": "tool_use",
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": name,
                    "input": args,
                    "client_side_execute": True,
                }
            )
        cursor = close_m.end()
        # Swallow any trailing stray </tool_call> a model sometimes appends
        stray = re.match(r"\s*</tool_call>", text[cursor:])
        if stray:
            cursor += stray.end()

    return "".join(clean), tool_uses


def _strip_think(delta: str, in_block: bool, carry: str) -> tuple[str, bool, str]:
    """Remove <think>…</think> sections from a streaming text chunk.

    Keeps a small rolling carry for partial open/close tags split across chunks.
    """
    OPEN, CLOSE = "<think>", "</think>"
    text = carry + delta
    out: list[str] = []
    i = 0
    while i < len(text):
        if in_block:
            close_idx = text.find(CLOSE, i)
            if close_idx == -1:
                for n in range(1, len(CLOSE)):
                    if text.endswith(CLOSE[:n]):
                        return "".join(out), True, text[-n:]
                return "".join(out), True, ""
            i = close_idx + len(CLOSE)
            in_block = False
        else:
            open_idx = text.find(OPEN, i)
            if open_idx == -1:
                tail = text[i:]
                for n in range(1, len(OPEN)):
                    if tail.endswith(OPEN[:n]):
                        out.append(tail[:-n])
                        return "".join(out), False, tail[-n:]
                out.append(tail)
                return "".join(out), False, ""
            out.append(text[i:open_idx])
            i = open_idx + len(OPEN)
            in_block = True
    return "".join(out), in_block, ""


# ---------------------------------------------------------------------------
# Streaming core
# ---------------------------------------------------------------------------


def _translate_messages_to_ollama(openai_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adjust OpenAI-shape messages to Ollama's /api/chat shape.

    Differences:
    - Ollama's tool role uses a plain {role:"tool", content:"..."} — no tool_call_id
      field needed (it matches by position/name), but including it is harmless.
    - assistant tool_calls expect arguments as an OBJECT, not a JSON string.
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
                    {
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args_val,
                        }
                    }
                )
            new["tool_calls"] = new_calls
            out.append(new)
        else:
            out.append(m)
    return out


async def _stream_from_ollama(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> AsyncIterator[bytes]:
    """Call Ollama /api/chat with streaming and yield Cortex-shaped SSE frames.

    Text tokens stream as message.delta events; any tool_calls are buffered
    and emitted in a final message.stop with tool_use content parts.
    """
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": _translate_messages_to_ollama(messages),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools

    if os.environ.get("CORTEX_OLLAMA_DEBUG"):
        debug_path = f"/tmp/cortex_ollama_debug_{int(time.time())}.json"
        with open(debug_path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        logger.info("debug payload written to %s", debug_path)

    accumulated_text = ""
    pending_stream_text = ""  # buffered text not yet flushed to SSE
    text_already_sent = 0  # chars already streamed via message.delta
    tool_uses: list[dict[str, Any]] = []
    text_block_opened = False
    in_think_block = False
    carry = ""
    finish_reason: str | None = None

    timeout = httpx.Timeout(300.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_BASE_URL}{OLLAMA_CHAT_PATH}",
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode(errors="replace")
                logger.error("Ollama %s: %s", resp.status_code, err[:400])
                yield _sse(
                    "message.delta",
                    {
                        "delta": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"[proxy error from Ollama {resp.status_code}: {err[:300]}]",
                                }
                            ]
                        }
                    },
                )
                yield _sse(
                    "message.stop",
                    {
                        "message": {
                            "id": message_id,
                            "role": "assistant",
                            "content": [{"type": "text", "text": ""}],
                            "stop_reason": "end_turn",
                        }
                    },
                )
                yield _sse_done()
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

                # Text delta
                delta_text = msg.get("content", "")
                if delta_text:
                    visible, in_think_block, carry = _strip_think(
                        delta_text, in_think_block, carry
                    )
                    if visible:
                        pending_stream_text += visible
                        # Only flush the text *before* any half-started "<function" /
                        # "<tool_call" tag. If a tag is in progress we must wait to
                        # see if it's a real tool call (and therefore shouldn't be
                        # streamed to the user) or just prose containing "<".
                        safe_upto = len(pending_stream_text)
                        for marker in ("<function", "<tool_call"):
                            idx = pending_stream_text.find("<", text_already_sent)
                            if idx == -1:
                                continue
                            suffix = pending_stream_text[idx:]
                            if marker.startswith(suffix) or suffix.startswith(marker):
                                safe_upto = min(safe_upto, idx)
                        to_send = pending_stream_text[text_already_sent:safe_upto]
                        if to_send:
                            if not text_block_opened:
                                yield _sse(
                                    "content_block_start",
                                    {
                                        "index": 0,
                                        "content_block": {"type": "text", "text": ""},
                                    },
                                )
                                text_block_opened = True
                            # `response.text.delta` is accepted by BOTH code
                            # paths — the main agent loop (gz / processSSEData)
                            # and the startup passthrough probe (XHo). Emitting
                            # `message.delta` in addition would double-count:
                            # gz extracts text from either event.
                            yield _sse("response.text.delta", {"text": to_send})
                            text_already_sent = safe_upto

                # Tool calls (arrive complete in one message for Ollama)
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw_arguments": args}
                    tool_uses.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "name": fn.get("name", ""),
                            "input": args or {},
                            "client_side_execute": True,
                        }
                    )

                if chunk.get("done"):
                    finish_reason = chunk.get("done_reason", "stop")
                    break

    # Parse any embedded <function=…>…</function> blocks out of the pending text
    residual_text, text_tool_uses = _parse_text_tool_calls(pending_stream_text)
    tool_uses.extend(text_tool_uses)

    # Hybrid-mode safety net: when the user has configured a real SQL
    # connection, pin every Snowflake-family tool_use to it. Cortex's
    # sqlConnectionName fallback handles the common case, but models
    # occasionally emit `connection:` explicitly and get it wrong (e.g. they
    # echo the agent connection `ollama` from prior tool results). We only
    # override in the clearly-wrong case; an explicit, different connection
    # from the model is respected as an intentional override.
    if SQL_CONNECTION_NAME:
        overridden = 0
        for tu in tool_uses:
            if tu.get("name") not in SNOWFLAKE_ROUTED_TOOLS:
                continue
            inp = tu.setdefault("input", {})
            current = (inp.get("connection") or "").strip()
            if not current or (
                AGENT_CONNECTION_NAME and current == AGENT_CONNECTION_NAME
            ):
                inp["connection"] = SQL_CONNECTION_NAME
                overridden += 1
        if overridden:
            logger.info(
                "routed %d Snowflake tool_use(s) → connection=%s",
                overridden,
                SQL_CONNECTION_NAME,
            )

    # If tool calls were found in the text, the model's preamble was already
    # streamed up to the first "<"; the residual includes post-function prose
    # we would normally stream too, but since the turn is now a tool-use turn
    # we'll finalize without streaming more text (Cortex will replay after
    # the tool result comes back). Update accumulated_text for the final stop.
    if text_tool_uses:
        accumulated_text = pending_stream_text[:text_already_sent] + residual_text.strip()
    else:
        # No tool calls — flush whatever we held back.
        leftover = pending_stream_text[text_already_sent:]
        if leftover:
            if not text_block_opened:
                yield _sse(
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "text", "text": ""}},
                )
                text_block_opened = True
            yield _sse("response.text.delta", {"text": leftover})
        accumulated_text = pending_stream_text

    if text_block_opened:
        yield _sse("content_block_stop", {"index": 0})

    final_content: list[dict[str, Any]] = []
    if accumulated_text.strip():
        final_content.append({"type": "text", "text": accumulated_text})
    final_content.extend(tool_uses)
    if not final_content:
        final_content.append({"type": "text", "text": ""})

    stop_reason = "end_turn"
    if tool_uses:
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"

    yield _sse(
        "message.stop",
        {
            "message": {
                "id": message_id,
                "role": "assistant",
                "content": final_content,
                "stop_reason": stop_reason,
            }
        },
    )
    yield _sse_done()


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/agent-run")
async def agent_run(request: Request):
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})

    if os.environ.get("CORTEX_OLLAMA_DEBUG"):
        raw_path = f"/tmp/cortex_ollama_raw_{int(time.time() * 1000)}.json"
        with open(raw_path, "w") as fh:
            fh.write(body.decode("utf-8", errors="replace"))
        logger.info("raw request written to %s", raw_path)

    messages_json = payload.get("messages", "[]")
    tools_json = payload.get("tools", "[]")

    openai_messages = _translate_messages(messages_json)
    try:
        cortex_tools = json.loads(tools_json) if isinstance(tools_json, str) else tools_json
    except Exception:
        cortex_tools = []
    openai_tools = to_openai_tools(cortex_tools or [])

    logger.info(
        "agent-run: model=%s msgs=%d tools=%d",
        OLLAMA_MODEL,
        len(openai_messages),
        len(openai_tools),
    )

    return StreamingResponse(
        _stream_from_ollama(openai_messages, openai_tools),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "connection": "keep-alive"},
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ollama": OLLAMA_BASE_URL, "model": OLLAMA_MODEL, "ts": time.time()}


# ---------------------------------------------------------------------------
# Fallback: log any other path Cortex tries to hit — useful when we pivot to
# running it without a real Snowflake connection (see docs/offline.md).
# ---------------------------------------------------------------------------


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def fallback(full_path: str, request: Request):
    body = await request.body()
    logger.warning(
        "unhandled %s /%s (%d bytes)",
        request.method,
        full_path,
        len(body),
    )
    return JSONResponse(status_code=404, content={"error": "not_found", "path": full_path})
