#!/usr/bin/env python3
"""
Test client for the Devin Grok plugin (remote MCP server).

Runs the MCP handshake against your deployed server, lists the tools, and
(optionally) calls create_devin_session for a real end-to-end check.

Usage:
    pip3 install httpx

    # Safe check — proves the MCP protocol + auth work, spends NO Devin credits:
    python test_plugin.py \
        --url https://YOUR-DOMAIN.up.railway.app/mcp \
        --token YOUR_PLUGIN_BEARER_TOKEN

    # Full end-to-end — ALSO starts a real Devin session (spends ACUs):
    python test_plugin.py \
        --url https://YOUR-DOMAIN.up.railway.app/mcp \
        --token YOUR_PLUGIN_BEARER_TOKEN \
        --create --prompt "Say hello and describe what repo you have access to. Do not make changes."

Nothing here is sent anywhere except your own server.
"""
import argparse
import json
import sys

import httpx


def parse_mcp_response(resp: httpx.Response) -> dict:
    """Handle both SSE (event: message / data: {...}) and plain JSON responses."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise RuntimeError(f"No data frame in SSE response:\n{text}")
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="MCP endpoint, ending in /mcp")
    ap.add_argument("--token", required=True, help="PLUGIN_BEARER_TOKEN")
    ap.add_argument("--create", action="store_true",
                    help="Actually call create_devin_session (spends Devin ACUs)")
    ap.add_argument("--prompt",
                    default="Say hello and confirm you received this. Do not make any code changes.",
                    help="Prompt for the test session (only used with --create)")
    args = ap.parse_args()

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    with httpx.Client(timeout=90.0) as client:
        # 1. initialize
        print("→ initialize ...")
        r = client.post(args.url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        })
        if r.status_code == 401:
            print("✗ 401 Unauthorized — your --token doesn't match PLUGIN_BEARER_TOKEN.")
            return 1
        r.raise_for_status()
        init = parse_mcp_response(r)
        server = init.get("result", {}).get("serverInfo", {})
        print(f"✓ connected to server: {server.get('name')} v{server.get('version')}")

        session_id = r.headers.get("mcp-session-id")
        sess_headers = dict(headers)
        if session_id:
            sess_headers["mcp-session-id"] = session_id

        # 2. initialized notification
        client.post(args.url, headers=sess_headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

        # 3. tools/list
        print("→ tools/list ...")
        r = client.post(args.url, headers=sess_headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        r.raise_for_status()
        tools = parse_mcp_response(r)["result"]["tools"]
        print(f"✓ {len(tools)} tools available:")
        for t in tools:
            print(f"    - {t['name']}: {t['description'].splitlines()[0]}")

        if not args.create:
            print("\n✓ PROTOCOL + AUTH OK. "
                  "Re-run with --create to test a real Devin session.")
            return 0

        # 4. tools/call create_devin_session (REAL — spends ACUs)
        print(f"\n→ create_devin_session (REAL session) ...\n    prompt: {args.prompt!r}")
        r = client.post(args.url, headers=sess_headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "create_devin_session",
                "arguments": {"prompt": args.prompt, "title": "Plugin smoke test"},
            },
        })
        r.raise_for_status()
        result = parse_mcp_response(r)

        if "error" in result:
            print(f"✗ MCP error: {json.dumps(result['error'], indent=2)}")
            return 1

        content = result["result"]["content"]
        payload_text = content[0]["text"] if content else "{}"
        try:
            payload = json.loads(payload_text)
        except Exception:
            payload = payload_text
        print("✓ tool returned:")
        print(json.dumps(payload, indent=2))

        if isinstance(payload, dict) and payload.get("error"):
            print("\n✗ Devin API returned an error (see 'detail' above). "
                  "Check DEVIN_API_KEY / DEVIN_API_BASE on Railway.")
            return 1
        if isinstance(payload, dict) and payload.get("session_id"):
            print(f"\n✓ END-TO-END OK. Devin session: {payload.get('url')}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
