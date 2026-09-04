# Devin plugin for Grok Bot

A remote **MCP server** that turns Devin (the autonomous AI software engineer)
into a tool your **Grok Bot** can call. You message the bot; it hands the coding
task to Devin and gives you back the session link (and any PR Devin opens).

The server wraps Devin's v1 REST API and keeps your Devin API key server-side —
Grok never sees it. Grok authenticates via OAuth 2.0 (see [Auth model](#auth-model-oauth-20)).

## Quick start (use the published template)

If you just want to use this with your Grok Bot and not read the internals:

1. **Import the Grok Bot template:** <https://x.ai/bot/RwNXRkVfIUpxKV6jeXRsR>
2. **Deploy your own instance** of this server (see [Deploy to Railway](#deploy-to-railway-recommended)) and get your `https://…/mcp` URL.
3. **Connect your MCP** to the bot using your own deployment's OAuth values (see [Register with Grok Bot](#register-with-grok-bot)).
4. **Ask it to start a session**, e.g. *"@devin add rate limiting to the login route in acme/api and open a PR."*

> ⚠️ **Deploy your own — don't share one URL.** Every Devin session started
> through a deployment spends **that deployment's** `DEVIN_API_KEY` (and its ACU
> credits). The template does **not** ship a shared endpoint: each person points
> the connector at their **own** Railway URL with their **own** `DEVIN_API_KEY`.
> Never hand your MCP URL + client secret to others — it's your Devin bill.

## Tools exposed to Grok

| Tool | What it does | Key Devin call |
|------|--------------|----------------|
| `create_devin_session` | Start a coding task; returns a `session_id` and a shareable `url` | `POST /v1/sessions` |
| `get_devin_session` | Check status (`working`/`blocked`/`finished`/…), structured output, and any PR Devin opened | `GET /v1/sessions/{id}` |
| `send_message` | Send a follow-up / answer Devin's question | `POST /v1/sessions/{id}/messages` |
| `list_devin_sessions` | List recent sessions | `GET /v1/sessions` |

## 1. Prerequisites

- A **Devin API key** — app.devin.ai → Settings → API Keys. It starts with `apk_`.
- A place to host this server with a **public HTTPS URL**. These instructions
  use **Railway** — it builds the included Dockerfile as-is and gives you an
  HTTPS domain. Grok connects over the internet, so `localhost` won't do.

## 2. Configure

```bash
cp .env.example .env
```

Set these in `.env` (the server reads all of them):

| Env var | Required | Purpose |
|---------|----------|---------|
| `DEVIN_API_KEY` | ✅ | Your `apk_…` Devin key. Server-side only. |
| `OAUTH_CLIENT_SECRET` | ✅ | The client secret Grok sends to `/token`. Falls back to `PLUGIN_BEARER_TOKEN` if unset. |
| `OAUTH_CLIENT_ID` | – | Client id label; must match what you enter in Grok. Default `grok`. |
| `TOKEN_SIGNING_KEY` | – | Signs access tokens. Defaults to `OAUTH_CLIENT_SECRET`. |
| `DEVIN_API_BASE` | – | Change only on a dedicated/enterprise Devin host. |
| `ACCESS_TOKEN_TTL` / `REFRESH_TOKEN_TTL` | – | Token lifetimes (default 30d / 1y). |

Generate a strong secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Run locally (optional test)

```bash
pip install -r requirements.txt
python server.py            # serves MCP at http://localhost:8000/mcp
```

Health check: `GET /health` → `{"status":"ok","oauth":true}`.

## Deploy to Railway (recommended)

Railway runs this as a normal long-lived container — no code changes needed. It
builds the Dockerfile, injects a `PORT` the server already reads, and serves it
over HTTPS.

**From a GitHub repo (simplest):**

1. Push this folder to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**, pick the repo.
   Railway detects the Dockerfile (`railway.toml` points at it) and builds.
3. Service → **Variables**: add `DEVIN_API_KEY` and `OAUTH_CLIENT_SECRET` (and
   optionally `OAUTH_CLIENT_ID`). Do **not** set `PORT` — Railway sets it.
4. Service → **Settings → Networking → Generate Domain** (expose port **8080**
   if asked; that's what the container listens on under Railway).
5. Your MCP URL is that domain **+ `/mcp`**.

**From the CLI:**

```bash
npm i -g @railway/cli
railway login
railway init
railway up
railway variables --set DEVIN_API_KEY=apk_... --set OAUTH_CLIENT_SECRET=your_secret
railway domain
```

Verify it's live:
```bash
curl https://YOUR-RAILWAY-DOMAIN/health
curl https://YOUR-RAILWAY-DOMAIN/.well-known/oauth-authorization-server
```

## Auth model (OAuth 2.0)

Grok's custom-connector flow is a pure OAuth client, so this server is an OAuth
2.0 authorization server (Authorization Code + PKCE, confidential client):

- You configure a **client secret** in Grok. Grok sends it to `/token` (in the
  request body), never in the URL.
- Grok exchanges it for a short-lived **access token** and sends that token in
  the `Authorization: Bearer` header on every MCP request.
- The Devin API key stays server-side and never reaches Grok.

Endpoints: `/authorize`, `/token`, `/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`, and the MCP endpoint at `/mcp`.

> **No dynamic client registration (DCR).** Grok Bot / Cursor cannot self-register
> an OAuth client against this server — DCR isn't supported. You must give it the
> **pre-registered** `Client ID` and `Client Secret` below. (If you skip them, the
> connector fails to load with an auth error.)

## Register with Grok Bot

This is the path for the **Grok Bot app** (AI teammate). Add a **Custom** MCP
connector (grok.com/connectors → New Connector → Custom, or ask the bot to add
the Devin connector). When Grok shows the **OAuth** dialog, fill it in with
**your own deployment's** values:

| Grok field | Value |
|------------|-------|
| MCP server URL | `https://YOUR-DOMAIN.up.railway.app/mcp` |
| Client ID | `grok` (or your `OAUTH_CLIENT_ID`) |
| Client Secret | your `OAUTH_CLIENT_SECRET` |
| Authorization Endpoint | `https://YOUR-DOMAIN.up.railway.app/authorize` |
| Token Endpoint | `https://YOUR-DOMAIN.up.railway.app/token` |
| Scopes | `mcp` (or leave blank) |
| Token Auth Method | `client_secret_post` |

> Choose **client_secret_post** (not "none / PKCE only") — the client secret is
> what secures token issuance.

After it connects, the connector shows up under **Settings → Plugins** in the Bot
app (connectors are account-wide). In a chat, type **`@`** and pick **devin** to
attach it, then give your instruction.

## Use it

Once connected, message your Grok Bot naturally, e.g.:

> "@devin implement rate limiting on the /login endpoint in the acme/api repo,
> add tests, and open a PR. Then give me the session link."

Grok calls `create_devin_session`, then `get_devin_session` to track it.

> Devin itself must have the target repo connected on its side (Devin → Settings
> → integrations), or it starts a session without access to the code.

## Notes & guardrails

- **Prompt quality drives results.** Devin performs best with explicit scope,
  the target repository, and verifiable completion criteria.
- **Cost control.** Pass `max_acu_limit` on `create_devin_session` to cap
  compute per task; and remember every session spends **your** Devin key.
- **Keep the secret out of the URL.** The client secret travels in the `/token`
  request body, never in the connector URL. Rotate it by changing
  `OAUTH_CLIENT_SECRET` in Railway and updating the connector.
- **HTTP 421 behind a proxy.** The MCP SDK's DNS-rebinding protection rejects
  non-localhost Host headers unless allowlisted. This server disables that check
  by default; set `ALLOWED_HOSTS` (e.g. `your-app.up.railway.app`) to lock it.

## Where this fits in the bigger workflow

This gives Grok the ability to *drive Devin*. The next step in the larger project
is having Grok read a task from Telegram and feed it into `create_devin_session`.
