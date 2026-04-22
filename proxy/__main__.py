"""Entrypoint that runs the proxy on both HTTP (agent:run) and HTTPS (Snowflake auth).

Cortex Code hardcodes ``http://localhost:2031/v1/agent-run`` when
``CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1``, while the Snowflake Node SDK
insists on HTTPS for any account host. We serve the same FastAPI app on two
ports to satisfy both: 2031 plaintext, 2443 TLS with a self-signed cert.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from .probe import run as run_probe
from .server import app

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"
CERT_FILE = CERT_DIR / "localhost.crt"
KEY_FILE = CERT_DIR / "localhost.key"


async def _serve() -> None:
    # Fail fast if the installed cortex binary no longer contains the strings
    # we depend on. Set CORTEX_SKIP_PROBE=1 to skip (e.g. in CI sandboxes).
    if not os.environ.get("CORTEX_SKIP_PROBE"):
        print("cortex_ollama: probing installed cortex binary for protocol anchors...")
        rc = run_probe(json_output=False)
        if rc != 0:
            raise SystemExit(
                "cortex_ollama: aborting. Fix the translator before serving."
            )
        print()

    http_port = int(os.environ.get("CORTEX_PROXY_HTTP_PORT", "2031"))
    https_port = int(os.environ.get("CORTEX_PROXY_HTTPS_PORT", "2443"))
    host = os.environ.get("CORTEX_PROXY_HOST", "127.0.0.1")

    http_cfg = uvicorn.Config(
        app, host=host, port=http_port, log_level="info", access_log=True
    )
    servers = [uvicorn.Server(http_cfg).serve()]

    if CERT_FILE.exists() and KEY_FILE.exists():
        https_cfg = uvicorn.Config(
            app,
            host=host,
            port=https_port,
            ssl_keyfile=str(KEY_FILE),
            ssl_certfile=str(CERT_FILE),
            log_level="info",
            access_log=True,
        )
        servers.append(uvicorn.Server(https_cfg).serve())
        print(
            f"cortex_ollama: HTTP on :{http_port} (agent:run) + HTTPS on :{https_port} (Snowflake)"
        )
    else:
        print(
            f"cortex_ollama: HTTP on :{http_port}. No cert at {CERT_FILE} — HTTPS listener disabled."
        )

    await asyncio.gather(*servers)


if __name__ == "__main__":
    asyncio.run(_serve())
