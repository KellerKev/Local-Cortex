"""Backend abstraction.

Every backend consumes:
  * ``messages``: OpenAI chat-shape messages (role/content/tool_calls/tool_call_id)
  * ``tools``: OpenAI tool definitions (``{"type":"function","function":{...}}``)

…and yields a stream of :class:`BackendEvent` until the turn completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


class BackendError(RuntimeError):
    """Raised on protocol-level failures (HTTP 4xx/5xx, malformed responses)."""


@dataclass
class TextDeltaEvent:
    text: str


@dataclass
class ToolUseEvent:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoneEvent:
    finish_reason: str = "stop"  # "stop" | "length" | "tool_calls" | "error"
    error: str = ""


BackendEvent = TextDeltaEvent | ToolUseEvent | DoneEvent


class Backend(Protocol):
    name: str
    base_url: str
    model: str

    def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[BackendEvent]: ...

    async def list_models(self) -> list[str]:
        """Return the names of models available to this backend (best-effort)."""
        ...
