# Claude Code Remote Session Protocol

Reverse-engineered from browser HAR captures and API exploration, April 2026.

## Overview

Claude Code Remote (CCR) sessions run on Anthropic-managed VMs. Communication uses:
- **HTTP REST** — for session management (list, read events, send messages)
- **WebSocket** — for real-time streaming and tool approval relay (browser only)
- **Polling** — for external clients that can't access the WebSocket

## Architecture

```
┌─────────────┐    HTTP POST (messages)     ┌──────────────────────┐
│  External   │ ───────────────────────────> │                      │
│  Client     │    HTTP GET (poll events)    │  api.anthropic.com   │
│  (cc-tap)   │ <─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │                      │
└─────────────┘                             └──────────────────────┘

┌─────────────┐    WebSocket (streaming +   ┌──────────────────────┐
│  Web UI     │     tool approval relay)    │                      │
│  (browser)  │ <═══════════════════════>   │  claude.ai (proxy)   │
└─────────────┘    (Cloudflare protected)   └──────────────────────┘
```

Key limitation: tool approval (`control_response` events) only reach the session
runtime via WebSocket. HTTP-posted control_responses are stored in event history
but not pushed to the running session. The WebSocket at `claude.ai` is behind
Cloudflare bot protection, making it inaccessible from non-browser clients.

## Base URLs

| Client | Base URL |
|--------|----------|
| CLI (direct) | `https://api.anthropic.com` |
| Web UI (proxied) | `https://claude.ai` (same paths, cookie auth) |

## Authentication

### CLI: OAuth Bearer Token

```
Authorization: Bearer {access_token}
```

OAuth scopes required: `user:inference`, `user:profile`, `user:sessions:claude_code`, `user:mcp_servers`, `user:file_upload`

OAuth client ID: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`

The access token is obtained via standard OAuth flow through `https://claude.com/cai/oauth/authorize`.
Tokens are stored locally by the CLI after `/login`.

### Web UI: Session Cookies

The web UI uses claude.ai session cookies (same auth as regular claude.ai chat).

### Required Headers (both)

```
anthropic-version: 2023-06-01
anthropic-beta: ccr-byoc-2025-07-29
x-organization-uuid: {org_uuid}
Content-Type: application/json
```

## Session ID Formats

Two prefixes for the same session:
- `session_` prefix: used with `/v1/sessions/` endpoints
- `cse_` prefix: used with `/v1/code/sessions/` endpoints

Example: `session_01KfYqc1hzxuRoQ3rp4b4aGm` and `cse_01KfYqc1hzxuRoQ3rp4b4aGm` refer to the same session.

---

## HTTP Endpoints

### 1. List Sessions

```
GET /v1/sessions
```

**Response:**
```json
{
  "data": [
    {
      "id": "session_01KfYqc1hzxuRoQ3rp4b4aGm",
      "type": "internal_session",
      "session_status": "idle",
      "connection_status": "connected",
      "title": "Session title",
      "created_at": "2026-04-13T00:03:42.934311Z",
      "updated_at": "2026-04-13T01:30:43.893285Z",
      "environment_id": "",
      "tags": [],
      "active_mount_paths": [],
      "metadata": {},
      "external_metadata": {
        "pending_action": null,
        "task_summary": null
      },
      "session_context": {
        "allowed_tools": [],
        "cwd": "",
        "disallowed_tools": [],
        "environment_variables": {},
        "outcomes": [],
        "sources": []
      }
    }
  ],
  "has_more": false,
  "first_id": "...",
  "last_id": "..."
}
```

Session statuses: `requires_action`, `running`, `idle`, `archived`

### 2. Get Session

```
GET /v1/sessions/{session_id}
```

### 3. Update Session Title

```
PATCH /v1/sessions/{session_id}
```

```json
{ "title": "New title" }
```

### 4. Get Session Events (History)

```
GET /v1/sessions/{session_id}/events?limit=1000
```

Event types: `user`, `assistant`, `control_request`, `control_response`, `control_cancel_request`, `result`

### 5. Send Message (Write Path)

```
POST /v1/sessions/{session_id}/events
```

```json
{
  "events": [
    {
      "type": "user",
      "uuid": "random-uuid-v4",
      "session_id": "session_...",
      "parent_tool_use_id": null,
      "message": {
        "role": "user",
        "content": "your message here"
      }
    }
  ]
}
```

Content can be a string or an array of content blocks (text, image, etc.) following the Anthropic messages API spec.

The endpoint may block up to ~30s waiting for the CCR worker to be ready (cold-start containers).

### 6. Client Presence (Heartbeat)

```
POST /v1/code/sessions/cse_{id}/client/presence
```

```json
{ "client_id": "uuid-v4" }
```

Response: `{ "refresh_after_seconds": 20 }`

### 7. Share Status

```
GET /v1/sessions/{session_id}/share-status
```

### 8. MCP Server Proxy

```
POST /v1/toolbox/shttp/mcp/{server_uuid}
```

Proxies MCP JSON-RPC calls to remote MCP servers. Response is SSE.

---

## Real-Time Streaming

There are **three** streaming mechanisms, each for a different context:

### 1. WebSocket — Web UI (claude.ai)

```
wss://claude.ai/v1/sessions/ws/{session_id}/subscribe?organization_uuid={org_uuid}&from_event_id={last_event_id}
```

The web UI at claude.ai/code uses this for real-time streaming. Confirmed via Chrome
DevTools (101 Switching Protocols).

**Auth**: Cookie-based, no Bearer token.
```
Cookie: sessionKey=sk-ant-sid02-...
```

The `sessionKey` is the claude.ai browser session credential (`sk-ant-sid` prefix).
This is different from the CLI's OAuth token (`sk-ant-oat` prefix).

**Parameters**:
- `organization_uuid` — required
- `from_event_id` — optional, for resumption after reconnect

**Cloudflare protection**: This endpoint is behind Cloudflare's bot detection.
Connecting from a non-browser client (Python, Node.js) returns 403 even with valid
cookies, because Cloudflare verifies TLS fingerprint, JS challenge solutions
(`cf_clearance` cookie), and browser fingerprinting. This makes programmatic WebSocket
access impractical without a headless browser.

### 2. WebSocket — CLI (api.anthropic.com)

```
wss://api.anthropic.com/v1/sessions/ws/{session_id}/subscribe?organization_uuid={org_uuid}
```

Used by the CLI for remote-control viewer mode (`claude assistant`, `/remote-control`).
Connects directly to the API, bypassing Cloudflare's browser checks.

**Auth**: `Authorization: Bearer {oauth_access_token}`

**Status**: Returns 403 with the CLI OAuth token for cloud-hosted internal sessions.
May only work for sessions created via the `/remote-control` command (local CLI
exposed to web). Further investigation needed.

### 3. WebSocket — Bridge/Worker (session_ingress)

```
wss://api.anthropic.com/v1/session_ingress/ws/{session_id}
```

Used by the CLI bridge process (the actual session worker). This is where the worker
receives control_responses and sends events. NOT the same as the `/subscribe` endpoint.

**Auth**: `Authorization: Bearer {session_ingress_token}`

The `session_ingress_token` is a short-lived, session-scoped credential obtained
through the worker registration flow:
1. CLI registers as worker via `POST /v1/code/sessions/{id}/worker/register`
2. API returns a `WorkSecret` (base64url-encoded JSON) containing the token
3. CLI uses that token for all `session_ingress` endpoints

This token is NOT the user's OAuth token or the browser session key. External clients
cannot obtain it without going through the worker registration flow.

**URL construction** (from source):
- Production: `wss://{host}/v1/session_ingress/ws/{sessionId}` (Envoy rewrites v1→v2)
- Localhost: `ws://{host}/v2/session_ingress/ws/{sessionId}` (direct)

### 4. SSE — CLI Worker (v2 path)

```
GET /v2/session_ingress/session/{session_id}/events/stream
```

Alternative to the WebSocket above, used by the v2 transport path (SSETransport).
Same `session_ingress_token` auth.

POST endpoint (for writing): same path without `/stream`:
```
POST /v2/session_ingress/session/{session_id}/events
```

### 4. Polling — External Clients

```
GET /v1/sessions/{session_id}/events?limit=1000
```

For external clients without access to the WebSocket or SSE endpoints, polling is the
viable approach. Works with OAuth Bearer token against `api.anthropic.com`. Poll every
1-2 seconds for near-real-time updates (~1.5s latency).

---

## Token Types

| Prefix | Type | Source | Used For |
|--------|------|--------|----------|
| `sk-ant-oat` | OAuth access token | CLI Keychain / `~/.claude/.credentials.json` | HTTP API calls, worker registration |
| `sk-ant-sid` | Session key | Browser cookie (`sessionKey`) | WebSocket subscribe via claude.ai |
| (opaque) | Session ingress token | `WorkSecret` from worker registration | `session_ingress` WebSocket + SSE |

## Complete Client Flow (Polling)

1. **Authenticate** — Read OAuth token from Keychain or credentials file
2. **Get org UUID** — `GET /api/oauth/claude_cli/roles`
3. **List sessions** — `GET /v1/sessions`
4. **Start heartbeat** — `POST /v1/code/sessions/cse_{id}/client/presence` every 20s
5. **Send message** — `POST /v1/sessions/{id}/events` with user event
6. **Poll for response** — `GET /v1/sessions/{id}/events?limit=1000` every 1-2s
7. **Tool approval** — see "Open Questions" below

## OAuth Token Location

On macOS, stored in Keychain:
```
Service: "Claude Code-credentials"
Account: $USER
Key path: claudeAiOauth.accessToken
```

Fallback (Linux/other):
```
~/.claude/.credentials.json
```

## Tool Approval: How It Actually Works

The web UI uses **two channels** simultaneously:
- **HTTP POST** (`/v1/sessions/{id}/events`) — for sending user messages only
- **WebSocket** (`wss://claude.ai/v1/sessions/ws/{id}/subscribe`) — for receiving
  events AND sending control_responses (tool approvals)

Control_responses sent via HTTP POST are stored in event history but **never broadcast
to WebSocket subscribers**. The session worker only receives control_responses through
its WebSocket connection. This is why HTTP-posted approvals don't unblock the session.

```
Web UI (browser)
  ├── HTTP POST /events        → user messages only (stored + consumed)
  └── WebSocket /ws/subscribe  → control_responses (broadcast to session worker)
                               ← assistant events, control_requests (received)

cc-tap (no WebSocket)
  └── HTTP POST /events        → user messages (stored + consumed)
                               → control_responses (stored but NOT consumed)
```

The WebSocket at `claude.ai` is behind Cloudflare bot protection. TLS fingerprint
impersonation (`curl_cffi` with Chrome impersonation) bypasses Cloudflare for HTTP
POST but not for the WebSocket upgrade handshake.

## Open Questions

Things suspected but not confirmed. Contributions welcome.

- **WebSocket at api.anthropic.com** — three endpoints tested, all return 403:
  - `/v1/sessions/ws/{id}/subscribe` with OAuth token (403)
  - `/v1/session_ingress/ws/{id}` with OAuth token (403 — needs session_ingress_token)
  - `/v1/sessions/ws/{id}/subscribe` with `sk-ant-sid` cookie (403) and Bearer (401)

  The `/subscribe` endpoint may require a specific token scope or session type.
  The `/session_ingress` endpoint requires a worker-registration-issued token that
  external clients cannot obtain.

- **Cloudflare bypass** — `curl_cffi` with Chrome TLS impersonation passes
  Cloudflare for HTTP POST to `claude.ai` (200) but fails for WebSocket upgrade
  (403). Headless Playwright also blocked. Only a real browser with existing
  session passes Cloudflare for WebSocket.

- **Session creation** — `POST /v1/sessions` likely exists but hasn't been captured
  or tested. Would enable spinning up new CC sessions programmatically.

- **Token exchange** — the web UI authenticates with `sessionKey` (`sk-ant-sid`),
  the CLI with OAuth (`sk-ant-oat`). There may be an endpoint to exchange one for
  the other, which would unlock WebSocket access from non-browser clients.

- **Session ID mapping** — local CC sessions have a UUID (from `/status`) that
  doesn't map to the API's `session_01...` ID. The API ID is server-assigned when
  remote control connects. No mapping endpoint discovered. Sessions can only be
  identified by title (which isn't unique).

- **Event pagination** — `GET /v1/sessions/{id}/events?limit=1000` fetches up to 1000
  events. No cursor/pagination parameter has been discovered. Sessions with >1000
  events may lose early history.

- **MCP Channels** — CC has experimental channel support (`claude/channel` capability)
  gated behind the `tengu_harbor` feature flag and a server-side allowlist. If opened
  up, channels would enable real-time push notifications from the MCP server to the
  CC session, replacing polling.

