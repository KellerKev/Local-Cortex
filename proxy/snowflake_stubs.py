"""Minimal Snowflake session endpoints for the `ollama` proxy connection.

When cortex-code starts up with a non-streamgen connection, the bundled
Snowflake Node SDK calls a handful of HTTPS endpoints on the account host to
authenticate and keep the session alive. We stub just enough of those so the
SDK thinks it has a valid session — without ever contacting Snowflake.

Shapes derived from the SDK's minified source and the public reference
documentation. They're kept intentionally small: populate only the fields the
SDK validates.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _token() -> str:
    return f"ollama-proxy-{uuid.uuid4().hex}"


@router.post("/session/v1/login-request")
async def login_request(request: Request):
    """PAT login: SDK posts LOGIN_NAME + PASSWORD; we return a session token."""
    await request.body()  # drain; we don't need the content
    now_ms = int(time.time() * 1000)
    return {
        "data": {
            "token": _token(),
            "masterToken": _token(),
            "masterValidityInSeconds": 14400,
            "validityInSeconds": 3600,
            "sessionId": 1,
            "serverVersion": "9.99.0 ollama-proxy",
            "idToken": None,
            "idTokenValidityInSeconds": 0,
            "mfaToken": None,
            "mfaTokenValidityInSeconds": 0,
            "sessionInfo": {
                "databaseName": None,
                "schemaName": None,
                "warehouseName": None,
                "roleName": "PUBLIC",
            },
            "parameters": [
                {"name": "CLIENT_SESSION_KEEP_ALIVE", "value": False},
                {"name": "CLIENT_PREFETCH_THREADS", "value": 4},
                {"name": "AUTOCOMMIT", "value": True},
                {"name": "TIMEZONE", "value": "UTC"},
            ],
            "responseData": None,
            "healthCheckInterval": 45,
            "newClientForUpgrade": None,
            "firstLogin": False,
            "remMeToken": None,
            "remMeValidityInSeconds": 0,
            "serverTimeStamp": now_ms,
        },
        "code": None,
        "message": None,
        "success": True,
    }


@router.post("/session/token-request")
async def token_refresh(request: Request):
    await request.body()
    return {
        "data": {
            "sessionToken": _token(),
            "masterToken": _token(),
            "validityInSecondsST": 3600,
            "validityInSecondsMT": 14400,
            "sessionId": 1,
        },
        "code": None,
        "message": None,
        "success": True,
    }


@router.post("/session/authenticator-request")
async def authenticator_request(request: Request):
    await request.body()
    return {
        "data": {"tokenUrl": "", "ssoUrl": "", "proofKey": ""},
        "code": None,
        "message": None,
        "success": True,
    }


@router.post("/session/heartbeat")
async def heartbeat(request: Request):
    await request.body()
    return {"data": {}, "code": None, "message": None, "success": True}


@router.post("/session")
async def session_delete(request: Request):
    await request.body()
    return {"data": {}, "code": None, "message": None, "success": True}


@router.post("/queries/v1/query-request")
async def query_request(request: Request):
    # Cortex Code shouldn't actually issue SQL through this proxy, but the
    # SDK could send a dummy validation query — respond with an empty success.
    await request.body()
    return {
        "data": {
            "parameters": [],
            "rowtype": [],
            "rowsetBase64": "",
            "rowset": [],
            "total": 0,
            "returned": 0,
            "queryId": str(uuid.uuid4()),
            "statementTypeId": 4096,
            "version": 1,
            "sendResultTime": int(time.time() * 1000),
            "queryResultFormat": "json",
            "queryContext": {"entries": []},
        },
        "code": None,
        "message": None,
        "success": True,
    }


@router.post("/queries/v1/abort-request")
async def abort_request(request: Request):
    await request.body()
    return {"data": {}, "code": None, "message": "aborted", "success": True}


@router.get("/ocsp_response_cache")
@router.get("/ocsp_response_cache.json")
async def ocsp_stub():
    return {}


@router.post("/telemetry/send")
async def telemetry_send(request: Request):
    await request.body()
    return {"data": {}, "code": None, "message": None, "success": True}
