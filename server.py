"""
Devin plugin for Grok Bot — a remote MCP server with OAuth 2.0.

Exposes Devin's v1 API as MCP tools that Grok Bot can call. Grok registers this
server as a custom MCP connector. Because Grok's custom-connector flow is a pure
OAuth client, this server implements OAuth 2.0 (Authorization Code + PKCE, with a
confidential client secret):

  - The secret (OAUTH_CLIENT_SECRET) is configured in Grok and sent only to the
    /token endpoint — never in the URL.
  - Grok exchanges it for a short-lived access token and sends that token in the
    Authorization header on every MCP request.

Tools:
  - create_devin_session : start a coding task, returns session id + url
  - get_devin_session     : poll status / structured output / PR of a session
  - send_message          : send a follow-up instruction to a running session
  - list_devin_sessions   : list recent sessions

MCP transport: Streamable HTTP at /mcp.
OAuth endpoints: /authorize, /token, plus discovery under /.well-known/.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

# --------------------------------------------------------------------------- #
# Configuration (from environment)
# --------------------------------------------------------------------------- #
DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_API_BASE = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai/v1").rstrip("/")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# --- OAuth config ---
# Grok is the OAuth client. Set OAUTH_CLIENT_ID (any label) and OAUTH_CLIENT_SECRET
# in Grok's connector dialog; the secret is sent to /token, never in the URL.
# OAUTH_CLIENT_SECRET falls back to PLUGIN_BEARER_TOKEN so existing deployments
# keep their secret env var. TOKEN_SIGNING_KEY signs access tokens (defaults to the
# client secret). Access tokens are self-contained (HMAC-signed), so no DB needed.
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "grok").strip()
OAUTH_CLIENT_SECRET = (
    os.environ.get("OAUTH_CLIENT_SECRET", "").strip()
    or os.environ.get("PLUGIN_BEARER_TOKEN", "").strip()
)
TOKEN_SIGNING_KEY = (
    os.environ.get("TOKEN_SIGNING_KEY", "").strip() or OAUTH_CLIENT_SECRET
).encode()
ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", str(30 * 24 * 3600)))  # 30d
REFRESH_TOKEN_TTL = int(os.environ.get("REFRESH_TOKEN_TTL", str(365 * 24 * 3600)))  # 1y
OAUTH_ENABLED = bool(OAUTH_CLIENT_SECRET)

# Optional Host allowlist (see note); default disabled so it works behind a proxy.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

if not DEVIN_API_KEY:
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
    if resp.status_code >= 400:
        try:
            detail: Any = resp.json()
        except Exception:
            detail = resp.text
        return {"error": True, "status_code": resp.status_code, "detail": detail}
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
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

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
    return await _devin_request("GET", f"/sessions?limit={limit}&offset={offset}")


# --------------------------------------------------------------------------- #
# OAuth 2.0 (Authorization Code + PKCE, confidential client)
# --------------------------------------------------------------------------- #
# In-memory, single-use authorization codes (short-lived). Fine for one instance;
# on restart, in-flight codes are dropped and the client simply re-authorizes.
_AUTH_CODES: dict[str, dict[str, Any]] = {}
_AUTH_CODE_TTL = 300  # 5 minutes


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _base_url(request: Request) -> str:
    """Public base URL, honoring proxy headers (Railway terminates TLS upstream)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _make_token(kind: str, ttl: int) -> str:
    payload = {"t": kind, "cid": OAUTH_CLIENT_ID, "iat": int(time.time()),
               "exp": int(time.time()) + ttl, "jti": secrets.token_urlsafe(8)}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(TOKEN_SIGNING_KEY, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_token(token: str, kind: str) -> bool:
    try:
        body, sig = token.split(".", 1)
        expected = _b64url(hmac.new(TOKEN_SIGNING_KEY, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(_b64url_decode(body))
        return payload.get("t") == kind and payload.get("exp", 0) > int(time.time())
    except Exception:
        return False


def _client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str]:
    """Extract client_id/secret from Basic auth (client_secret_basic) or form."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            cid, _, secret = decoded.partition(":")
            return cid, secret
        except Exception:
            pass
    return form.get("client_id", ""), form.get("client_secret", "")


async def oauth_authorization_server_metadata(request: Request) -> Response:
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported":
            ["client_secret_post", "client_secret_basic", "none"],
        "scopes_supported": ["mcp"],
    })


async def oauth_protected_resource_metadata(request: Request) -> Response:
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
    })


async def authorize(request: Request) -> Response:
    q = request.query_params
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")

    def err_redirect(code: str, desc: str) -> Response:
        if redirect_uri:
            sep = "&" if urlparse(redirect_uri).query else "?"
            return RedirectResponse(
                f"{redirect_uri}{sep}{urlencode({'error': code, 'error_description': desc, 'state': state})}",
                status_code=302,
            )
        return JSONResponse({"error": code, "error_description": desc}, status_code=400)

    if q.get("response_type") != "code":
        return err_redirect("unsupported_response_type", "response_type must be 'code'")
    if q.get("client_id") != OAUTH_CLIENT_ID:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if not redirect_uri or urlparse(redirect_uri).scheme not in ("https", "http"):
        return JSONResponse({"error": "invalid_request", "error_description": "bad redirect_uri"}, status_code=400)
    challenge = q.get("code_challenge", "")
    if not challenge or q.get("code_challenge_method", "S256") != "S256":
        return err_redirect("invalid_request", "PKCE S256 code_challenge required")

    # No user-login step (this is the operator's own server). The confidential
    # client secret required at /token is what protects token issuance, so an
    # auth code alone is useless without it.
    code = secrets.token_urlsafe(24)
    _AUTH_CODES[code] = {
        "redirect_uri": redirect_uri,
        "challenge": challenge,
        "exp": time.time() + _AUTH_CODE_TTL,
        "scope": q.get("scope", "mcp"),
    }
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}", status_code=302
    )


async def token(request: Request) -> Response:
    form = dict(await request.form())
    grant = form.get("grant_type", "")
    cid, secret = _client_credentials(request, form)

    if cid != OAUTH_CLIENT_ID or not hmac.compare_digest(secret, OAUTH_CLIENT_SECRET):
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant == "authorization_code":
        code = form.get("code", "")
        entry = _AUTH_CODES.pop(code, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant", "error_description": "bad or expired code"}, status_code=400)
        if form.get("redirect_uri", "") != entry["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)
        # PKCE: verify code_verifier against stored challenge.
        verifier = form.get("code_verifier", "")
        calc = _b64url(hashlib.sha256(verifier.encode()).digest())
        if not verifier or not hmac.compare_digest(calc, entry["challenge"]):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
    elif grant == "refresh_token":
        if not _verify_token(form.get("refresh_token", ""), "refresh"):
            return JSONResponse({"error": "invalid_grant", "error_description": "bad refresh_token"}, status_code=400)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return JSONResponse({
        "access_token": _make_token("access", ACCESS_TOKEN_TTL),
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": _make_token("refresh", REFRESH_TOKEN_TTL),
        "scope": "mcp",
    })


# --------------------------------------------------------------------------- #
# App assembly
# --------------------------------------------------------------------------- #
def build_app():
    """Starlette ASGI app: OAuth endpoints + Bearer-protected MCP at /mcp."""
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    mcp.settings.streamable_http_path = "/mcp"
    app = mcp.streamable_http_app()

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "devin-grok-plugin",
                             "oauth": OAUTH_ENABLED})

    # OAuth + health + discovery routes (all evaluated before the MCP mount).
    app.add_route("/health", health, methods=["GET"])
    app.add_route("/.well-known/oauth-authorization-server",
                  oauth_authorization_server_metadata, methods=["GET"])
    app.add_route("/.well-known/oauth-protected-resource",
                  oauth_protected_resource_metadata, methods=["GET"])
    app.add_route("/authorize", authorize, methods=["GET"])
    app.add_route("/token", token, methods=["POST"])

    # Guard the MCP endpoint with the OAuth access token.
    async def require_access_token(request: Request, call_next):
        path = request.url.path
        if OAUTH_ENABLED and (path == "/mcp" or path.startswith("/mcp/")):
            auth = request.headers.get("authorization", "")
            token_str = auth[7:] if auth.startswith("Bearer ") else ""
            if not _verify_token(token_str, "access"):
                base = _base_url(request)
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate":
                            f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                    },
                )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_access_token)
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn

    if not OAUTH_ENABLED:
        print(
            "WARNING: no OAUTH_CLIENT_SECRET / PLUGIN_BEARER_TOKEN set — the MCP "
            "endpoint is UNAUTHENTICATED. Set one before exposing publicly."
        )
    uvicorn.run(app, host=HOST, port=PORT)
