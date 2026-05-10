"""Layered configuration: defaults < TOML file < environment variables.

The proxy reads its config from (in order, first found wins):

  1. ``$CORTEX_OLLAMA_CONFIG``                          (explicit override)
  2. ``./cortex_ollama.toml``                           (project-local)
  3. ``~/.config/cortex-ollama/config.toml``            (per-user)

If no file exists, built-in defaults apply. Environment variables override
either source so users can still do one-shot tweaks like
``OLLAMA_MODEL=… pixi run tui`` without editing files.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "backend": "ollama",
    "backends": {
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen3.6:35b-a3b",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "",
            "model": "claude-sonnet-4-5",
            "api_version": "2023-06-01",
        },
    },
    "snowflake": {
        "agent_connection": "ollama",
        "sql_connection": "",
    },
    "proxy": {
        "http_port": 2031,
        "https_port": 2443,
    },
}


@dataclass
class BackendConfig:
    name: str
    base_url: str
    model: str
    api_key: str = ""
    api_version: str = ""


@dataclass
class Config:
    backend: BackendConfig
    agent_connection: str
    sql_connection: str
    http_port: int
    https_port: int
    source: Path | None = None
    _all_backends: dict[str, dict[str, Any]] = field(default_factory=dict)

    def with_backend(self, name: str) -> BackendConfig:
        """Return a BackendConfig for an arbitrary backend name (used by /backend)."""
        raw = self._all_backends.get(name)
        if raw is None:
            raise KeyError(f"unknown backend {name!r}")
        return BackendConfig(
            name=name,
            base_url=raw.get("base_url", ""),
            model=raw.get("model", ""),
            api_key=_resolve_api_key(name, raw),
            api_version=raw.get("api_version", ""),
        )


def _candidate_paths() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get("CORTEX_OLLAMA_CONFIG", "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
    out.append(Path("cortex_ollama.toml").resolve())
    out.append(Path.home() / ".config" / "cortex-ollama" / "config.toml")
    return out


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_api_key(backend_name: str, raw: dict[str, Any]) -> str:
    if raw.get("api_key"):
        return str(raw["api_key"])
    env_keys = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    env_key = env_keys.get(backend_name)
    if env_key:
        return os.environ.get(env_key, "")
    return ""


def load() -> Config:
    """Load layered configuration. Returns a fully-resolved Config object."""
    merged: dict[str, Any] = DEFAULTS
    source: Path | None = None
    for p in _candidate_paths():
        if p.is_file():
            try:
                file_data = tomllib.loads(p.read_text())
                merged = _deep_merge(DEFAULTS, file_data)
                source = p
                break
            except Exception as e:
                print(
                    f"cortex_ollama: error parsing {p}: {e}; falling back to defaults",
                    file=sys.stderr,
                )

    # Env-var overrides — preserved for back-compat with the older wrapper UX.
    env_backend = os.environ.get("CORTEX_OLLAMA_BACKEND", "").strip()
    if env_backend:
        merged["backend"] = env_backend

    backends = merged.get("backends", {})
    ollama = backends.setdefault("ollama", {})
    if os.environ.get("OLLAMA_BASE_URL"):
        ollama["base_url"] = os.environ["OLLAMA_BASE_URL"]
    if os.environ.get("OLLAMA_MODEL"):
        ollama["model"] = os.environ["OLLAMA_MODEL"]

    snowflake = merged.setdefault("snowflake", {})
    if os.environ.get("CORTEX_AGENT_CONNECTION"):
        snowflake["agent_connection"] = os.environ["CORTEX_AGENT_CONNECTION"]
    if os.environ.get("CORTEX_SQL_CONNECTION"):
        snowflake["sql_connection"] = os.environ["CORTEX_SQL_CONNECTION"]

    proxy = merged.setdefault("proxy", {})
    if os.environ.get("CORTEX_PROXY_HTTP_PORT"):
        proxy["http_port"] = int(os.environ["CORTEX_PROXY_HTTP_PORT"])
    if os.environ.get("CORTEX_PROXY_HTTPS_PORT"):
        proxy["https_port"] = int(os.environ["CORTEX_PROXY_HTTPS_PORT"])

    backend_name = merged.get("backend", "ollama")
    backend_raw = backends.get(backend_name)
    if backend_raw is None:
        raise SystemExit(
            f"cortex_ollama: backend {backend_name!r} is not defined under [backends]"
        )

    backend = BackendConfig(
        name=backend_name,
        base_url=backend_raw.get("base_url", ""),
        model=backend_raw.get("model", ""),
        api_key=_resolve_api_key(backend_name, backend_raw),
        api_version=backend_raw.get("api_version", ""),
    )

    return Config(
        backend=backend,
        agent_connection=str(snowflake.get("agent_connection", "ollama")),
        sql_connection=str(snowflake.get("sql_connection", "")),
        http_port=int(proxy.get("http_port", 2031)),
        https_port=int(proxy.get("https_port", 2443)),
        source=source,
        _all_backends=backends,
    )
