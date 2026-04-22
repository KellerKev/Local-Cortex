"""Capture-mode server.

Logs every request Cortex Code sends us, then returns a stub error so we can
read real payloads before committing to a response schema. Run via:

    pixi run capture

then point a fake Snowflake connection at http://127.0.0.1:8765 and invoke
`cortex -c ollama -p "hello world"`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CAPTURE_DIR = Path(__file__).resolve().parent.parent / "captures"
CAPTURE_DIR.mkdir(exist_ok=True)

app = FastAPI()


@app.middleware("http")
async def log_everything(request: Request, call_next):
    body = await request.body()
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    safe_path = request.url.path.strip("/").replace("/", "_").replace(":", "_") or "root"
    outfile = CAPTURE_DIR / f"{stamp}-{request.method}-{safe_path}.txt"
    with outfile.open("w") as fh:
        fh.write(f"{request.method} {request.url}\n")
        for k, v in request.headers.items():
            fh.write(f"{k}: {v}\n")
        fh.write("\n")
        try:
            parsed = json.loads(body) if body else None
            fh.write(json.dumps(parsed, indent=2, default=str))
        except Exception:
            fh.write(body.decode("utf-8", errors="replace"))
    print(f"[capture] {request.method} {request.url.path} → {outfile.name}")
    response = await call_next(request)
    return response


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(full_path: str):
    return JSONResponse(
        status_code=501,
        content={
            "error": "capture_mode",
            "message": "Proxy is in capture mode; request was logged.",
            "path": full_path,
        },
    )
