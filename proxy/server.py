"""Cortex Code ↔ pluggable LLM backend translator.

Implements the endpoint Cortex Code hits when
``CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1`` is set:

    POST http://localhost:2031/v1/agent-run

Cortex sends a Snowflake-flavored agent:run request (JSON; its ``messages`` /
``tools`` / ``experimental`` fields are themselves JSON strings). We:

 1. translate messages from Cortex/Anthropic shape → OpenAI chat shape,
    including tool_use ↔ tool_call / tool_result ↔ tool role round-trips;
 2. forward to the configured backend (Ollama, OpenAI/compat, or Anthropic)
    via the abstraction in :mod:`proxy.backends`;
 3. stream the response back as the Anthropic-style SSE events Cortex
    understands (response.text.delta, message.stop with optional tool_use,
    ``[DONE]``).

The active backend, model, and Snowflake routing all come from
:mod:`proxy.config` (cortex_ollama.toml + env-var overrides).
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

from . import config as proxy_config
from .backends import (
    Backend,
    BackendEvent,
    DoneEvent,
    TextDeltaEvent,
    ToolUseEvent,
    get_backend,
)
from .snowflake_stubs import router as snowflake_router
from .toolspecs import to_openai_tools

logger = logging.getLogger("cortex_ollama")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CONFIG = proxy_config.load()
if CONFIG.source:
    logger.info("config loaded from %s", CONFIG.source)
else:
    logger.info("no cortex_ollama.toml found; using built-in defaults + env")

# Mutable per-process state — what every agent:run reads. /backend and /model
# endpoints can swap these at runtime without a proxy restart.
_state = {
    "backend_name": CONFIG.backend.name,
    "backend": get_backend(
        CONFIG.backend.name,
        CONFIG.backend.base_url,
        CONFIG.backend.model,
        CONFIG.backend.api_key,
        CONFIG.backend.api_version,
    ),
}


def _active_backend() -> Backend:
    return _state["backend"]


# Hybrid mode: Cortex natively supports a split between agent-connection (for
# inference) and sql-connection (for database queries). When SQL_CONNECTION_NAME
# is set the proxy injects ``connection:`` into any Snowflake-family tool_use
# the model emits without one — belt-and-suspenders against the model
# forgetting (Cortex would otherwise fall back to the agent connection, which
# is our stub).
SQL_CONNECTION_NAME = CONFIG.sql_connection
AGENT_CONNECTION_NAME = CONFIG.agent_connection or "ollama"

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


async def _stream_from_backend(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> AsyncIterator[bytes]:
    """Run a turn through the active backend; emit Cortex-shaped SSE.

    Text tokens stream out as ``response.text.delta`` events; any tool_calls
    are buffered (and the safety-net pinned to the SQL connection if hybrid
    mode is on), then emitted as a single ``message.stop`` carrying
    ``tool_use`` content blocks.
    """
    backend = _active_backend()
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    if os.environ.get("CORTEX_OLLAMA_DEBUG"):
        debug_path = f"/tmp/cortex_ollama_debug_{int(time.time())}.json"
        with open(debug_path, "w") as fh:
            json.dump(
                {"backend": backend.name, "model": backend.model, "messages": messages, "tools": tools},
                fh, indent=2, default=str,
            )
        logger.info("debug payload written to %s", debug_path)

    accumulated_text = ""
    pending_stream_text = ""  # buffered text not yet flushed to SSE
    text_already_sent = 0  # chars already streamed via response.text.delta
    tool_uses: list[dict[str, Any]] = []
    text_block_opened = False
    in_think_block = False
    carry = ""
    finish_reason = "stop"
    backend_error = ""

    async for ev in backend.stream(messages, tools):
        if isinstance(ev, TextDeltaEvent):
            visible, in_think_block, carry = _strip_think(
                ev.text, in_think_block, carry
            )
            if not visible:
                continue
            pending_stream_text += visible
            # Hold back any text that might be the start of a tool-call tag
            # ("<function" / "<tool_call") so we don't leak partial markup.
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
                        {"index": 0, "content_block": {"type": "text", "text": ""}},
                    )
                    text_block_opened = True
                yield _sse("response.text.delta", {"text": to_send})
                text_already_sent = safe_upto

        elif isinstance(ev, ToolUseEvent):
            tool_uses.append(
                {
                    "type": "tool_use",
                    "id": ev.id,
                    "name": ev.name,
                    "input": ev.input or {},
                    "client_side_execute": True,
                }
            )

        elif isinstance(ev, DoneEvent):
            finish_reason = ev.finish_reason or "stop"
            backend_error = ev.error
            break

    if backend_error:
        logger.error("backend error: %s", backend_error[:400])
        yield _sse(
            "response.text.delta",
            {"text": f"[proxy: backend error — {backend_error[:300]}]"},
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

    backend = _active_backend()
    logger.info(
        "agent-run: backend=%s model=%s msgs=%d tools=%d",
        backend.name,
        backend.model,
        len(openai_messages),
        len(openai_tools),
    )

    return StreamingResponse(
        _stream_from_backend(openai_messages, openai_tools),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "connection": "keep-alive"},
    )


@app.get("/healthz")
async def healthz():
    backend = _active_backend()
    return {
        "ok": True,
        "backend": backend.name,
        "base_url": backend.base_url,
        "model": backend.model,
        "ts": time.time(),
    }


@app.get("/model")
async def get_model():
    backend = _active_backend()
    return {
        "model": backend.model,
        "default": CONFIG.backend.model,
        "backend": backend.name,
    }


@app.post("/model")
async def set_model(request: Request):
    """Hot-swap the active model. Validates against the backend's model list.

    Body: ``{"model": "<name>"}``. Persists for the life of the process.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    new = (body.get("model") or "").strip()
    if not new:
        return JSONResponse(status_code=400, content={"error": "missing 'model' field"})

    backend = _active_backend()
    try:
        available = await backend.list_models()
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": f"could not list models on backend {backend.name}: {e}"},
        )

    # If the backend can't list (e.g., no api_key), accept any name — the user
    # knows best, and we'll surface the error on the next agent turn anyway.
    if available and new not in available:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"model {new!r} not available on backend {backend.name!r}",
                "available": available,
            },
        )

    previous = backend.model
    backend.model = new  # mutate in place; all backends store this attr
    logger.info("model swapped (%s): %s → %s", backend.name, previous, new)
    return {"ok": True, "backend": backend.name, "previous": previous, "model": new}


@app.get("/models")
async def list_models():
    backend = _active_backend()
    try:
        names = await backend.list_models()
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": f"could not reach backend {backend.name}: {e}"},
        )
    return {
        "backend": backend.name,
        "current": backend.model,
        "default": CONFIG.backend.model,
        "available": names,
    }


@app.get("/backend")
async def get_backend_info():
    backend = _active_backend()
    return {
        "name": backend.name,
        "base_url": backend.base_url,
        "model": backend.model,
        "configured": list(CONFIG._all_backends.keys()),
    }


@app.post("/backend")
async def set_backend(request: Request):
    """Switch to a different configured backend at runtime.

    Body: ``{"backend": "ollama"|"openai"|"anthropic", "model": "<optional>"}``.
    The backend must be defined in cortex_ollama.toml under ``[backends.<name>]``.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    new_name = (body.get("backend") or "").strip()
    if not new_name:
        return JSONResponse(status_code=400, content={"error": "missing 'backend' field"})

    try:
        cfg = CONFIG.with_backend(new_name)
    except KeyError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    override_model = (body.get("model") or "").strip()
    if override_model:
        cfg.model = override_model

    new_backend = get_backend(
        cfg.name, cfg.base_url, cfg.model, cfg.api_key, cfg.api_version
    )
    previous = _active_backend().name
    _state["backend_name"] = new_backend.name
    _state["backend"] = new_backend
    logger.info("backend swapped: %s → %s (model=%s)", previous, new_backend.name, new_backend.model)
    return {
        "ok": True,
        "previous": previous,
        "backend": new_backend.name,
        "model": new_backend.model,
    }


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
