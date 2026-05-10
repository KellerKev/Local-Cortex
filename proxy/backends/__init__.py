"""Pluggable inference backends for the cortex_ollama proxy.

Each backend is a thin async generator that takes OpenAI-shape messages +
tools and yields normalised :class:`BackendEvent` records. The translator in
``proxy/server.py`` consumes those events and emits Cortex-shaped SSE.

Selecting a backend at runtime is the responsibility of :func:`get_backend`,
which reads the loaded :class:`Config` and returns the matching adapter.
"""

from __future__ import annotations

from typing import AsyncIterator

from .base import (
    Backend,
    BackendEvent,
    BackendError,
    DoneEvent,
    TextDeltaEvent,
    ToolUseEvent,
)
from .ollama import OllamaBackend
from .openai_compat import OpenAIBackend
from .anthropic_msgs import AnthropicBackend

__all__ = [
    "Backend",
    "BackendEvent",
    "BackendError",
    "DoneEvent",
    "TextDeltaEvent",
    "ToolUseEvent",
    "OllamaBackend",
    "OpenAIBackend",
    "AnthropicBackend",
    "get_backend",
]


def get_backend(name: str, base_url: str, model: str, api_key: str = "", api_version: str = "") -> Backend:
    name = (name or "ollama").lower()
    if name == "ollama":
        return OllamaBackend(base_url=base_url, model=model)
    if name in ("openai", "openai-compat", "openai_compat"):
        return OpenAIBackend(base_url=base_url, model=model, api_key=api_key)
    if name == "anthropic":
        return AnthropicBackend(
            base_url=base_url, model=model, api_key=api_key, api_version=api_version or "2023-06-01"
        )
    raise BackendError(f"unknown backend {name!r}")
