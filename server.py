"""
Devin plugin for Grok Bot — a remote MCP server.

Exposes Devin's v1 API as MCP tools that Grok Bot can call. Grok registers this
server as a remote MCP connector (Streamable HTTP transport). The Devin API key
lives on THIS server (env var), never in Grok. Grok authenticates to this server
with a shared bearer token (PLUGIN_BEARER_TOKEN) so the endpoint isn't open to
the world.

Tools:
  - create_devin_session : start a coding task, returns session id + url
  - get_devin_session     : poll status / structured output / PR of a session
  - send_message          : send a follow-up instruction to a running session
  - list_devin_sessions   : list recent sessions

Transport: Streamable HTTP at path /mcp  (Grok supports Streaming HTTP and SSE).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configuration (from environment)
# --------------------------------------------------------------------------- #
DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_API_BASE = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai/v1").rstrip("/")
# Shared secret Grok must present in its Authorization header when calling us.
PLUGIN_BEARER_TOKEN = os.environ.get("PLUGIN_BEARER_TOKEN", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
# Comma-separated hostnames allowed in the Host header. Behind a proxy (Railway,
# Render, ...) the request's Host is your public domain, not localhost, so the
# MCP SDK's DNS-rebinding protection rejects it with 421 unless it's listed here.
# Leave empty to disable that check entirely (safe here: the /mcp endpoint is
# already gated by PLUGIN_BEARER_TOKEN). Set it to lock the server to your
# domain, e.g. ALLOWED_HOSTS=your-app.up.railway.app
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

if not DEVIN_API_KEY:
    # Fail loud at import so a misconfigured deploy is obvious.
    raise RuntimeError(
        "DEVIN_API_KEY is not set. Create a Devin API key "
        "(Settings -> API Keys, key starts with 'apk_') and set it in the env."
    )


# --------------------------------------------------------------------------- #
# Thin Devin API client
# --------------------------------------------------------------------------- #
def _devin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }


async def _devin_request(
    method: str, path: str, *, json_body: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    url = f"{DEVIN_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(
            method, url, headers=_devin_headers(), json=json_body
        )
    # Surface Devin errors as structured data instead of raising, so the model
    # can read and react to them.
    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        return {
            "error": True,
            "status_code": resp.status_code,
            "detail": detail,
        }
    try:
        return resp.json()
    except Exception:
        return {"error": True, "status_code": resp.status_code, "detail": resp.text}


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #
if ALLOWED_HOSTS:
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS + [f"{h}:*" for h in ALLOWED_HOSTS],
        allowed_origins=[f"https://{h}" for h in ALLOWED_HOSTS]
        + [f"http://{h}" for h in ALLOWED_HOSTS],
    )
else:
    # No host allowlist configured — turn off DNS-rebinding protection so the
    # server works behind any proxy/domain. The bearer-token gate still guards
    # the endpoint.
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

mcp = FastMCP(
    name="devin",
    transport_security=_transport_security,
    instructions=(
        "Tools to delegate coding tasks to Devin, the autonomous AI software "
        "engineer. Use create_devin_session to start a task, then "
        "get_devin_session to check progress. Devin works best with a clear "
        "prompt that includes explicit, verifiable completion criteria and "
        "names the target repository."
    ),
)


@mcp.tool()
async def create_devin_session(
    prompt: str,
    title: Optional[str] = None,
    tags: Optional[list[str]] = None,
    idempotent: bool = False,
    max_acu_limit: Optional[int] = None,
    unlisted: bool = False,
) -> dict[str, Any]:
    """Start a new Devin coding session (a task).

    Args:
        prompt: The task for Devin. Be explicit: describe the change, the target
            repository, and clear completion criteria (e.g. "tests pass",
            "open a PR"). Vague prompts produce poor results.
        title: Optional human-readable session title. Auto-generated if omitted.
        tags: Optional list of tags for later filtering (max 50).
        idempotent: If true, a duplicate call with the same prompt returns the
            existing session instead of creating a new one.
        max_acu_limit: Optional cap on compute units for the session.
        unlisted: If true, the session is not listed in the org's session list.

    Returns:
        A dict with session_id, url (the Devin session link to share), and
        is_new_session.
    """
    body: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent, "unlisted": unlisted}
    if title is not None:
        body["title"] = title
    if tags is not None:
        body["tags"] = tags
    if max_acu_limit is not None:
        body["max_acu_limit"] = max_acu_limit
    return await _devin_request("POST", "/sessions", json_body=body)


@mcp.tool()
async def get_devin_session(session_id: str) -> dict[str, Any]:
    """Retrieve the current status and details of a Devin session.

    Args:
        session_id: The id returned by create_devin_session.

    Returns:
        Session details including status_enum (working, blocked, finished,
        expired, ...), messages, structured_output, and pull_request (the PR
        Devin opened, if any).
    """
    return await _devin_request("GET", f"/sessions/{session_id}")


@mcp.tool()
async def send_message(session_id: str, message: str) -> dict[str, Any]:
    """Send a follow-up message / instruction to a running Devin session.

    Use this to answer a question Devin asked, refine scope, or unblock it.

    Args:
        session_id: The session to message.
        message: The instruction or reply to send to Devin.
    """
    return await _devin_request(
        "POST", f"/sessions/{session_id}/messages", json_body={"message": message}
    )


@mcp.tool()
async def list_devin_sessions(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """List recent Devin sessions for the organization.

    Args:
        limit: Max number of sessions to return (default 20).
        offset: Pagination offset (default 0).
    """
    return await _devin_request(
        "GET", f"/sessions?limit={limit}&offset={offset}"
    )


# --------------------------------------------------------------------------- #
# Auth middleware: require Grok's bearer token on the MCP endpoint
# --------------------------------------------------------------------------- #
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Health check stays open so platforms can probe it.
        if request.url.path in ("/", "/health", "/healthz"):
            return await call_next(request)

        if PLUGIN_BEARER_TOKEN:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {PLUGIN_BEARER_TOKEN}"
            if auth != expected:
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401
                )
        return await call_next(request)


def build_app():
    """Return the Starlette ASGI app (Streamable HTTP MCP at /mcp) with auth."""
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)

    # Simple health endpoint.
    async def health(_request: Request):
        return JSONResponse({"status": "ok", "service": "devin-grok-plugin"})

    app.add_route("/health", health, methods=["GET"])
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    if not PLUGIN_BEARER_TOKEN:
        print(
            "WARNING: PLUGIN_BEARER_TOKEN is not set — the MCP endpoint is "
            "UNAUTHENTICATED. Set it before exposing this server publicly."
        )
    uvicorn.run(app, host=HOST, port=PORT)
