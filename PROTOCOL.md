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

### 3. SSE — CLI Worker Inside CCR VM

```
GET /v2/session_ingress/session/{session_id}/events/stream
```

Used by the CLI worker process running inside the Anthropic-managed VM.
Auth via session-specific ingress token (`sk-ant-sid` cookie or JWT), NOT the user's
OAuth token. This token is injected into the VM at creation time.

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
| `sk-ant-oat` | OAuth access token | CLI Keychain / `~/.claude/.credentials.json` | HTTP API calls |
| `sk-ant-sid` | Session key | Browser cookie (`sessionKey`) | WebSocket via claude.ai |
| JWT | Session ingress | Injected into CCR VM | SSE/POST inside VM |

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

## Open Questions

Things suspected but not confirmed. Contributions welcome.

- **Tool approval via HTTP POST** — the web UI sends `control_response` events via
  `POST /v1/sessions/{id}/events` (confirmed in HAR capture), which is the same
  endpoint cc-tap uses. Attempts from cc-tap didn't unblock the session, but the
  web UI's do. The difference may be timing, session state, or an additional
  mechanism not yet identified. Needs a controlled test with precise timing.

- **WebSocket at api.anthropic.com** — returns 403 with the CLI OAuth token for
  `internal_session` type sessions. May work for sessions created via
  `/remote-control` (local CLI exposed to web). Needs verification with a
  remote-control session.

- **Session creation** — `POST /v1/sessions` likely exists but hasn't been captured
  or tested. Would enable spinning up new CC sessions programmatically.

- **Token exchange** — the web UI authenticates with `sessionKey` (`sk-ant-sid`),
  the CLI with OAuth (`sk-ant-oat`). There may be an endpoint to exchange one for
  the other, which would unlock WebSocket access from non-browser clients.

- **Event pagination** — `GET /v1/sessions/{id}/events?limit=1000` fetches up to 1000
  events. No cursor/pagination parameter has been discovered. Sessions with >1000
  events may lose early history.

- **MCP Channels** — CC has experimental channel support (`claude/channel` capability)
  gated behind the `tengu_harbor` feature flag and a server-side allowlist. If opened
  up, channels would enable real-time push notifications from the MCP server to the
  CC session, replacing polling.

