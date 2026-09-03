# Devin plugin for Grok Bot

A remote **MCP server** that turns Devin (the autonomous AI software engineer)
into a tool your Grok Bot can call. Grok reads a task, then hands it to Devin
through this plugin.

Grok Bot extends via remote MCP connectors ("Bring Your Own MCP"). This server
speaks the MCP **Streamable HTTP** transport that Grok supports, wraps Devin's
v1 REST API, and keeps your Devin API key server-side — Grok never sees it.

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
  use **Railway** (see step 3b) — it builds the included Dockerfile as-is and
  gives you an HTTPS domain. Grok connects over the internet, so `localhost`
  won't do for production.

## 2. Configure

```bash
cp .env.example .env
# edit .env:
#   DEVIN_API_KEY        -> your apk_ key
#   PLUGIN_BEARER_TOKEN  -> a long random secret (see below)
```

Generate the shared secret Grok will use to authenticate to this server:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Run

**Local (for testing):**
```bash
pip install -r requirements.txt
python server.py            # serves MCP at http://localhost:8000/mcp
```

**Docker:**
```bash
docker build -t devin-grok-plugin .
docker run -p 8000:8000 --env-file .env devin-grok-plugin
```

Health check: `GET /health` → `{"status":"ok"}` (open, no auth).
MCP endpoint: `POST /mcp` (requires `Authorization: Bearer <PLUGIN_BEARER_TOKEN>`).

## 3b. Deploy to Railway (recommended)

Railway runs this as a normal long-lived container — no code changes needed. It
builds the Dockerfile, injects a `PORT` the server already reads, and serves it
over HTTPS.

**Option A — from a GitHub repo (simplest):**

1. Push this folder to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**, pick the repo.
   Railway detects the Dockerfile (`railway.toml` points at it) and builds.
3. Open the service → **Variables** and add:
   - `DEVIN_API_KEY` = your `apk_…` key
   - `PLUGIN_BEARER_TOKEN` = your long random secret
   - (optional) `DEVIN_API_BASE` if you're on a dedicated Devin host
   Do **not** set `PORT` — Railway sets it for you.
4. Service → **Settings → Networking → Generate Domain**. You'll get something
   like `https://devin-grok-plugin-production.up.railway.app`.
5. Your MCP URL is that domain **+ `/mcp`**.

**Option B — from the CLI:**

```bash
npm i -g @railway/cli
railway login
railway init                       # create a project
railway up                         # build & deploy this folder
railway variables --set DEVIN_API_KEY=apk_... \
                   --set PLUGIN_BEARER_TOKEN=your_secret
railway domain                     # generate the public HTTPS domain
```

Verify it's live:
```bash
curl https://YOUR-RAILWAY-DOMAIN/health      # -> {"status":"ok",...}
```

## 4. Register with Grok

Grok takes remote MCP servers in the `tools` array of an API request. Point it
at your deployed URL and pass your bearer token as `authorization`:

```json
{
  "tools": [
    {
      "type": "mcp",
      "server_label": "devin",
      "server_url": "https://YOUR-DEPLOYED-HOST/mcp",
      "server_description": "Delegate coding tasks to Devin, the autonomous AI software engineer.",
      "authorization": "YOUR_PLUGIN_BEARER_TOKEN"
    }
  ]
}
```

- `server_url` must be the full path ending in `/mcp`.
- `authorization` is sent by Grok as the `Authorization: Bearer …` header to
  this server; it must equal your `PLUGIN_BEARER_TOKEN`. (This is Grok→plugin
  auth. The plugin→Devin auth uses your `DEVIN_API_KEY` internally.)

## 5. Use it

Once registered, message your Grok Bot naturally, e.g.:

> "Use the devin tool to start a session: implement rate limiting on the
> /login endpoint in the acme/api repo, add tests, and open a PR. Then give me
> the session link and poll it until it's finished."

Grok will call `create_devin_session`, then `get_devin_session` to track it.

## Notes & guardrails

- **Prompt quality drives results.** Devin performs best with explicit scope,
  the target repository, and verifiable completion criteria. Have Grok confirm
  the extracted task with you before creating a session.
- **Cost control.** Pass `max_acu_limit` on `create_devin_session` to cap
  compute per task.
- **Auth.** Never expose `/mcp` without `PLUGIN_BEARER_TOKEN` set — anyone who
  finds the URL could otherwise spend your Devin credits.
- **API base.** If you're on a dedicated/enterprise Devin host, change
  `DEVIN_API_BASE` in `.env`.

## Where this fits in the bigger workflow

This is step one of the Telegram → Grok → Devin chain: it gives Grok the ability
to *drive Devin*. The next step is having Grok read the task from Telegram and
feed it into `create_devin_session`.
