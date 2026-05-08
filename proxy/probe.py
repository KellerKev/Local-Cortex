"""Robustness check — do the anchor points this proxy depends on still exist
in the installed Cortex Code binary?

The proxy relies on a handful of stable-looking strings inside Cortex's
bundled JavaScript (runtime on Bun, Mach-O on macOS). When Cortex releases a
new version, minified identifiers like ``i3L``, ``gz``, ``aE$`` can be
renamed — but the underlying protocol anchors (the env-var name
``CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR``, the URL ``/v1/agent-run``, the SSE
event types like ``message.delta``) are far less likely to change because
they're part of the wire contract with Snowflake's hosted orchestrator.

Run ``pixi run probe`` against the current binary before trusting a new
Cortex version. If any REQUIRED anchor is missing, the proxy is likely
broken; fix the translator before using it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


class Anchor(NamedTuple):
    name: str
    required: bool
    description: str
    # regex is a raw pattern; it's searched against the stripped binary strings
    pattern: str


ANCHORS: list[Anchor] = [
    Anchor(
        name="local_orchestrator_env_var",
        required=True,
        description="Env var that redirects agent:run to localhost:2031",
        pattern=r"CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR",
    ),
    Anchor(
        name="local_orchestrator_url",
        required=True,
        description="Hardcoded URL used when the env var is on",
        pattern=r"http://localhost:2031/v1/agent-run",
    ),
    Anchor(
        name="agent_run_url",
        required=True,
        description="Snowflake-hosted agent:run URL (sanity — confirms we still hit the same orchestrator family)",
        pattern=r"/api/v2/cortex/agent:run",
    ),
    Anchor(
        name="sse_message_delta",
        required=True,
        description="SSE event name the client parses for text/tool-use deltas",
        pattern=r'"message\.delta"',
    ),
    Anchor(
        name="sse_message_stop",
        required=True,
        description="SSE event name for the final message with content[]",
        pattern=r'"message\.stop"',
    ),
    Anchor(
        name="sse_content_block_start",
        required=True,
        description="SSE content_block_start used when opening a tool_use block",
        pattern=r'"content_block_start"',
    ),
    Anchor(
        name="sse_content_block_stop",
        required=True,
        description="SSE content_block_stop that matches start",
        pattern=r'"content_block_stop"',
    ),
    Anchor(
        name="sse_input_json_delta",
        required=False,
        description="Streaming tool-input delta — useful if we ever stream tool calls",
        pattern=r'"input_json_delta"',
    ),
    Anchor(
        name="sse_response_text_delta",
        required=True,
        description="SSE event the TUI's startup Inference API probe reads (XHo parser)",
        pattern=r'response\.text\.delta',
    ),
    Anchor(
        name="sse_done_sentinel",
        required=True,
        description="SSE stream terminator we emit",
        pattern=r'"\[DONE\]"',
    ),
    Anchor(
        name="tool_use_client_side_execute",
        required=True,
        description="Field Cortex reads to know the tool runs client-side",
        pattern=r"client_side_execute",
    ),
    Anchor(
        name="proto_str_response_param",
        required=True,
        description="Request parameter Cortex sends with agent:run",
        pattern=r"CORTEX_AGENT_USE_PROTO_STR_RESPONSE",
    ),
    Anchor(
        name="pat_authenticator",
        required=True,
        description="PAT authenticator enum used by the connection config",
        pattern=r"PROGRAMMATIC_ACCESS_TOKEN",
    ),
    Anchor(
        name="login_endpoint",
        required=True,
        description="Snowflake SDK login URL that our HTTPS stub handles",
        pattern=r"/session/v1/login-request",
    ),
    Anchor(
        name="node_tls_reject_env",
        required=True,
        description="Node.js env var the proxy relies on to accept self-signed cert",
        pattern=r"NODE_TLS_REJECT_UNAUTHORIZED",
    ),
    Anchor(
        name="account_url_field",
        required=True,
        description="Field name in the agent:run body; used in our translator",
        pattern=r"account_url",
    ),
    Anchor(
        name="sql_execute_tool_name",
        required=True,
        description="SQL execution tool name (Cortex renamed snowflake_sql_execute → sql_execute in 1.0.73+; either is fine — we route on both)",
        pattern=r'name:"(?:snowflake_)?sql_execute",',
    ),
    Anchor(
        name="sql_execute_connection_param",
        required=True,
        description="`connection` parameter on sql_execute — what we inject for hybrid-mode SQL routing",
        pattern=r'connection:\{type:"string",description:"Optional connection name',
    ),
]


def _find_binary() -> Path:
    """Locate the cortex executable the user actually runs."""
    candidate = os.environ.get("CORTEX_BIN")
    if candidate and Path(candidate).exists():
        return Path(candidate).resolve()
    which = shutil.which("cortex")
    if which:
        return Path(which).resolve()
    default = Path.home() / ".local" / "bin" / "cortex"
    if default.exists():
        return default.resolve()
    raise SystemExit(
        "Could not find the cortex binary. Set CORTEX_BIN or put `cortex` on PATH."
    )


def _extract_strings(path: Path) -> str:
    """Run `strings` over the binary and return its full output."""
    result = subprocess.run(
        ["strings", "-a", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"`strings` failed on {path}: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout


def _detect_version(binary_path: Path) -> str:
    # Cortex installs under `~/.local/share/cortex/<version>/cortex`; the
    # version is in the directory name.
    parent = binary_path.parent.name
    if re.match(r"^\d+\.\d+\.\d+", parent):
        return parent
    return "unknown"


def run(json_output: bool = False) -> int:
    binary = _find_binary()
    version = _detect_version(binary)
    content = _extract_strings(binary)
    results: list[dict] = []
    missing_required = 0
    for a in ANCHORS:
        found = bool(re.search(a.pattern, content))
        results.append(
            {
                "name": a.name,
                "required": a.required,
                "description": a.description,
                "pattern": a.pattern,
                "found": found,
            }
        )
        if a.required and not found:
            missing_required += 1

    if json_output:
        print(
            json.dumps(
                {
                    "cortex_binary": str(binary),
                    "cortex_version": version,
                    "missing_required": missing_required,
                    "anchors": results,
                },
                indent=2,
            )
        )
    else:
        print(f"Cortex binary:  {binary}")
        print(f"Cortex version: {version}")
        print()
        width = max(len(a.name) for a in ANCHORS)
        for r in results:
            mark = "OK  " if r["found"] else ("MISS" if r["required"] else "skip")
            tag = "required" if r["required"] else "optional"
            print(f"  [{mark}] {r['name']:<{width}}  ({tag})  {r['description']}")
        print()
        if missing_required:
            print(
                f"FAIL: {missing_required} required anchor(s) missing. "
                "Do NOT trust the proxy until the translator is updated."
            )
        else:
            print("OK — all required anchors present. Proxy should still work.")
    return 1 if missing_required else 0


if __name__ == "__main__":
    sys.exit(run(json_output="--json" in sys.argv))
